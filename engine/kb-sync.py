#!/usr/bin/env python3
"""KB sync — filesystem-based, cross-OS, single unified routine.

Scans workspaces from ~/.claude/kb-workspaces.json for git repos, dedupes by
remote.origin.url, then for each canonical repo:
  - Refreshes ONLY the integration/production refs it needs, by exact refspec
    (read-only on the remote; no all-heads wildcard, no --prune, nothing deleted
    locally). Best-effort: offline -> proceed on stale refs.
  - For each free-form work branch (the branch name is the match key; an
    optional "<type>/" prefix groups the KB folder), captures author commits
    and asks Claude to create/update the KB entry.
  - For each open ticket, detects whether its branch landed on an integration
    branch (merge-commit/ff via ancestry, rebase/squash via patch-id) or was
    deleted; if so, asks Claude to finalize the entry (resolved status, audit
    of learnings vs final diff). Manual close via `/kb-mark --done` is honored.

No platform API. Remote access limited to `git fetch`.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

CONFIG = Path.home() / ".claude" / "kb-workspaces.json"
STATE_DIR = Path.home() / ".claude" / "state"
SESSION_OFFSETS = STATE_DIR / "kb-session-offsets.json"
RUN_STATE = STATE_DIR / "kb-run-state.json"
SYNC_HISTORY = STATE_DIR / "kb-sync-history.json"
HWM_CAP_DAYS = 7
SYNC_HISTORY_CAP = 100  # keep the last N run records (rolling)
PROJECTS_DIR = Path.home() / ".claude" / "projects"
FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


# Embedding-backed retrieval (optional). When fastembed/numpy are installed,
# capture and finalize pre-fetch top-K relevant Learnings/transcripts and inject
# them inline — Claude headless no longer needs to discover-and-read via MCP.
# When deps are missing, kb_embed is loaded but get_model() raises
# EmbeddingsUnavailable on use; we catch and fall back to the legacy prompt.
def _load_kb_embed():
    import importlib.util as ilu
    here = Path(__file__).resolve().parent
    spec = ilu.spec_from_file_location("kb_embed", here / "kb-embed.py")
    if spec is None or spec.loader is None:
        return None
    mod = ilu.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


kb_embed = _load_kb_embed()


# Shared engine config resolver (kb_config). Transitional: it currently lives
# under hooks/; collapses into the engine package later. Loaded by path so the
# scheduled run honors KB_VAULT and the single vault-resolution order.
def _load_kb_config():
    import importlib.util as ilu
    path = Path(__file__).resolve().parent.parent / "hooks" / "kb_config.py"
    if not path.exists():
        return None
    spec = ilu.spec_from_file_location("kb_config", path)
    if spec is None or spec.loader is None:
        return None
    mod = ilu.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


kb_config = _load_kb_config()


def parse_branch(branch: str):
    """Map a free-form branch name to its KB folder layout.

    The branch name itself is the durable match key (no numeric id required).
    An optional "<type>/" prefix groups the folder; without a slash the ticket
    folder sits directly under the project.

      "feat/foo"          -> tipo="feat", slug="foo",         folder="feat/foo"
      "feat/39703-gnre"   -> tipo="feat", slug="39703-gnre",  folder="feat/39703-gnre"
      "experimento"       -> tipo=None,   slug="experimento", folder="experimento"
    """
    branch = branch.strip()
    if "/" in branch:
        tipo, slug = branch.split("/", 1)
        return tipo, slug, f"{tipo}/{slug}"
    return None, branch, branch


def encode_cwd(cwd: str) -> str:
    return re.sub(r"[:/\\_]", "-", cwd)


def load_session_offsets() -> dict:
    if not SESSION_OFFSETS.exists():
        return {}
    try:
        return json.loads(SESSION_OFFSETS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_session_offsets(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_OFFSETS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def transcript_stores() -> list[Path]:
    """Directories holding OTHER hosts' session logs, for token-marked sessions.

    Config key `transcript_stores` (list of paths) in the workspaces config;
    default: ~/.codex/sessions when it exists. The Claude Code transcripts in
    ~/.claude/projects are NOT a store — they resolve by session_id directly.
    """
    stores: list[Path] = []
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
        for p in cfg.get("transcript_stores", []) or []:
            if isinstance(p, str) and p.strip():
                stores.append(Path(os.path.expanduser(p.strip())))
    except Exception:
        pass
    if not stores:
        codex = Path.home() / ".codex" / "sessions"
        if codex.is_dir():
            stores.append(codex)
    return [s for s in stores if s.is_dir()]


def _read_store_file_text(path: Path) -> str | None:
    """Text of a session-log file; transparently decompresses .zst when the
    stdlib codec exists (Python 3.14+). None = unreadable here, skip."""
    try:
        if path.suffix == ".zst":
            try:
                from compression import zstd  # Python 3.14+
            except ImportError:
                return None
            return zstd.decompress(path.read_bytes()).decode("utf-8", errors="ignore")
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None


def resolve_marked_transcript(data: dict, sidecar: Path) -> Path | None:
    """Locate the session log holding this sidecar's mark token.

    The deterministic key is the token itself: the `kb_mark` MCP tool returned
    it as a tool result, so the host persisted it inside the session log — the
    file that CONTAINS the token IS the marked session, whatever the format.
    The found path is cached on the sidecar so later syncs don't re-scan.
    .zst logs are mirrored decompressed under STATE_DIR/kb-transcripts so the
    whole downstream (line hints, offsets, Read) keeps working on plain text.
    """
    token = data.get("mark_token", "")
    if not token:
        return None
    cached = data.get("transcript_path", "")
    if cached and Path(cached).is_file():
        return Path(cached)

    # Only look at files that could contain a mark made at `marked_at` (with a
    # generous margin) — keeps the scan cheap on big stores.
    floor = 0.0
    marked_at = data.get("marked_at", "")
    if marked_at:
        try:
            floor = time.mktime(time.strptime(marked_at[:19], "%Y-%m-%dT%H:%M:%S")) - 86400
        except Exception:
            floor = 0.0

    for store in transcript_stores():
        for f in sorted(store.rglob("*.jsonl*")):
            name = f.name.lower()
            if not (name.endswith(".jsonl") or name.endswith(".jsonl.zst")):
                continue
            try:
                if floor and f.stat().st_mtime < floor:
                    continue
            except OSError:
                continue
            text = _read_store_file_text(f)
            if text is None or token not in text:
                continue
            found = f
            if f.suffix == ".zst":
                mirror_dir = STATE_DIR / "kb-transcripts"
                mirror_dir.mkdir(parents=True, exist_ok=True)
                mirror = mirror_dir / f"{data.get('session_id', 'marked')}.jsonl"
                mirror.write_text(text, encoding="utf-8")
                found = mirror
            data["transcript_path"] = str(found)
            try:
                sidecar.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
            return found
    return None


def find_sessions_for_branch(branch: str) -> list[dict]:
    """Returns sessions where the session sidecar maps to this branch.

    Two resolution paths, one per adapter family:
      * Claude Code sidecars carry the host session_id -> the transcript lives
        at a deterministic path under PROJECTS_DIR.
      * Token-marked sidecars (the `kb_mark` MCP tool, any host) carry a
        mark_token -> the transcript is whichever store file contains it.
    Each entry: {session_id, jsonl_path (Path or None), cwd}.
    """
    out = []
    if not STATE_DIR.exists():
        return out
    for sidecar in STATE_DIR.glob("kb-session-branch-*.json"):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("branch") != branch:
            continue
        sid = data.get("session_id", "")
        cwd = data.get("cwd", "")
        if data.get("mark_token"):
            jsonl = resolve_marked_transcript(data, sidecar)
            if sid:
                out.append({
                    "session_id": sid,
                    "cwd": cwd,
                    "jsonl_path": jsonl,
                })
            continue
        if not sid or not cwd:
            continue
        jsonl = PROJECTS_DIR / encode_cwd(cwd) / f"{sid}.jsonl"
        out.append({
            "session_id": sid,
            "cwd": cwd,
            "jsonl_path": jsonl if jsonl.exists() else None,
        })
    return out


def session_hints(branch: str, offsets: dict) -> list[dict]:
    """For sessions of this branch, compute incremental reading hints.

    Returns only sessions with NEW content since last recorded offset.
    """
    hints = []
    for s in find_sessions_for_branch(branch):
        if not s["jsonl_path"]:
            continue
        try:
            with s["jsonl_path"].open("r", encoding="utf-8", errors="ignore") as f:
                current_lines = sum(1 for _ in f)
        except OSError:
            continue
        prev = int(offsets.get(s["session_id"], 0) or 0)
        if current_lines <= prev:
            continue
        hints.append({
            "session_id": s["session_id"],
            "path": str(s["jsonl_path"]),
            "from_line": prev + 1,
            "to_line": current_lines,
            "new_lines": current_lines - prev,
            "prev_offset": prev,
        })
    return hints


def bump_session_offsets(hints: list[dict]) -> None:
    if not hints:
        return
    offsets = load_session_offsets()
    for h in hints:
        offsets[h["session_id"]] = h["to_line"]
    save_session_offsets(offsets)


def load_run_state() -> dict:
    if not RUN_STATE.exists():
        return {}
    try:
        return json.loads(RUN_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_run_state(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RUN_STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_sync_history(record: dict, cap: int = SYNC_HISTORY_CAP) -> None:
    """Append one run record to the rolling sync-history sidecar (last `cap` kept).
    Atomic write; never fatal — a history hiccup must not fail the sync."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        hist = []
        if SYNC_HISTORY.exists():
            try:
                loaded = json.loads(SYNC_HISTORY.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    hist = loaded
            except json.JSONDecodeError:
                hist = []
        hist.append(record)
        hist = hist[-cap:]
        tmp = SYNC_HISTORY.with_name(SYNC_HISTORY.name + ".tmp")
        tmp.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SYNC_HISTORY)
    except Exception as e:
        print(f"sync-history append failed (non-fatal): {type(e).__name__}: {e}")


def ensure_installed_marker(state: dict) -> dict:
    if "installed_at" not in state:
        state["installed_at"] = datetime.now().strftime("%Y-%m-%d")
    return state


def effective_since(state: dict, origin_norm: str, branch: str, repo: Path) -> dict:
    """Decide capture window for (origin, branch).

    Returns dict with keys:
      - kind: "commit" | "date"
      - sha (when kind=commit) or iso (when kind=date)
      - source: "hwm" | "bootstrap" | "cap-7d-truncated"
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cap_date = (datetime.now() - timedelta(days=HWM_CAP_DAYS)).strftime("%Y-%m-%d")
    installed = state.get("installed_at", today)

    hwm = state.get("last_processed_commit", {}).get(origin_norm, {}).get(branch)
    if hwm:
        r = run_git(["merge-base", "--is-ancestor", hwm, branch], repo)
        if r.returncode == 0:
            return {"kind": "commit", "sha": hwm, "source": "hwm"}
        print(f"    [warn] HWM {hwm[:10]} not ancestor of {branch} (rebase/force-push?), date fallback")

    # No per-branch SHA-HWM: the floor is this REPO's last_examined_at date (the date
    # HWM — branch-independent). It only advances on a clean, error-free, fetch-OK run,
    # so it's a safe floor: every commit before it was already seen. cap-7d/installed is
    # only the absolute first-run bootstrap, before any date has been recorded.
    last_exam = state.get("last_examined_at", {}).get(origin_norm)
    if last_exam:
        return {"kind": "date", "iso": last_exam, "source": "last-examined"}

    bootstrap_date = max(installed, cap_date)
    source = "cap-7d-truncated" if cap_date > installed else "bootstrap"
    if source == "cap-7d-truncated" and hwm:
        print(f"    [warn] HWM stale (>{HWM_CAP_DAYS}d), capped at {bootstrap_date}, gap lost")
    return {"kind": "date", "iso": bootstrap_date, "source": source}


def bump_hwm(state: dict, origin_norm: str, branch: str, repo: Path) -> bool:
    head_sha = run_git(["rev-parse", branch], repo).stdout.strip()
    if not head_sha:
        return False
    state.setdefault("last_processed_commit", {}).setdefault(origin_norm, {})[branch] = head_sha
    return True


def advance_examined_dates(state: dict, fetched_ok: set, incomplete: set, today: str) -> set:
    """Advance the date HWM: set last_examined_at[origin]=today for origins fetched
    cleanly AND examined with zero errors/deferrals this run (fetched_ok MINUS
    incomplete). Returns the sealed set; held-back origins keep their old date so
    their branches are re-examined next run. This is the error-gate that stops
    uncaptured work (error / cap-deferral / stale fetch) from being sealed behind
    the date skip."""
    sealed = set(fetched_ok) - set(incomplete)
    if sealed:
        le = state.setdefault("last_examined_at", {})
        for o in sealed:
            le[o] = today
    return sealed


def load_config():
    if not CONFIG.exists():
        sys.exit(f"missing config: {CONFIG}")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    # Unified vault resolution: honor KB_VAULT and the single resolution order
    # (same contract as the hook/CLI). Falls back to the file's "vault" on any
    # miss. kb-sync is CLI-side, so strict=True surfaces a misconfig loudly.
    if kb_config is not None:
        try:
            cfg["vault"] = str(kb_config.resolve_vault(strict=True))
        except Exception:
            pass
    return cfg


def run_git(args, cwd, check=False, timeout=30):
    # GIT_TERMINAL_PROMPT=0: a headless sync must never block on a credential
    # prompt — fail fast instead of hanging to the timeout.
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args} failed in {cwd}: {r.stderr}")
    return r


def commit_vault(vault: Path) -> None:
    """Commit the vault's changes to its LOCAL git repo, if any. NEVER pushes.

    The vault is local-only by default; even when the user has connected it to a
    remote, the sync only ever commits locally — publishing stays a deliberate
    action (the manager's connect/pull), never an automatic push. Safe no-op when
    the vault isn't a git repo or has nothing to commit. Failures are logged, never
    fatal — a sync run must not break because versioning hiccuped.
    """
    try:
        if not (vault / ".git").exists():
            return  # vault versioning not enabled (not a git repo)
        run_git(["add", "-A"], cwd=vault, timeout=30)
        status = run_git(["status", "--porcelain"], cwd=vault, timeout=30)
        if not status.stdout.strip():
            return  # nothing changed
        msg = f"kb-sync: vault update {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        r = run_git(["commit", "-m", msg], cwd=vault, timeout=30)
        if r.returncode == 0:
            print(f"vault committed: {msg}")
        else:
            print(f"vault commit skipped/failed (non-fatal): {r.stderr.strip()[:200]}")
    except Exception as e:
        print(f"vault commit error (non-fatal): {type(e).__name__}: {e}")


def fetch_vault(vault: Path) -> None:
    """If the vault is connected to a remote, refresh from it BEFORE the sync runs.

    Read side of a shared/team vault: fetch the remote, then fast-forward the local
    branch ONLY when it's strictly behind and the tree is clean (zero-risk). A
    diverged local — your own captures not yet pushed — is left untouched: reconcile
    it deliberately via the manager's 'Pull from remote' (a real merge that aborts on
    conflict). This never pushes, never force, and never runs an unattended merge that
    could leave a conflict in a learning. No-op for a local-only vault (no remote) —
    the default case — so a private vault is never touched. Fully non-fatal.
    """
    try:
        if not (vault / ".git").exists():
            return
        remotes = run_git(["remote"], cwd=vault, timeout=15)
        if "origin" not in remotes.stdout.split():
            return  # local-only vault: nothing to fetch (the default)
        branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=vault, timeout=15).stdout.strip() or "main"
        fr = run_git(["fetch", "--no-tags", "origin", branch], cwd=vault, timeout=60)
        if fr.returncode != 0:
            print(f"vault fetch skipped (non-fatal): {fr.stderr.strip()[:200]}")
            return
        # Fast-forward only when strictly behind on a clean tree. Diverged or dirty ->
        # leave it for the deliberate manual Pull (no unattended merge / conflicts).
        if run_git(["status", "--porcelain"], cwd=vault, timeout=15).stdout.strip():
            print("vault fetched; local changes present -> not fast-forwarding (use manual Pull if needed).")
            return
        before = run_git(["rev-parse", "HEAD"], cwd=vault, timeout=15).stdout.strip()
        mg = run_git(["merge", "--ff-only", f"origin/{branch}"], cwd=vault, timeout=30)
        if mg.returncode != 0:
            print(f"vault fetched; local diverged from origin/{branch} -> use the manager's 'Pull from remote' to merge.")
            return
        after = run_git(["rev-parse", "HEAD"], cwd=vault, timeout=15).stdout.strip()
        if after != before:
            cnt = run_git(["rev-list", "--count", f"{before}..{after}"], cwd=vault, timeout=15).stdout.strip()
            print(f"vault fast-forwarded to team's latest ({cnt} commit(s) from origin/{branch}).")
    except Exception as e:
        print(f"vault fetch error (non-fatal): {type(e).__name__}: {e}")


def discover_repos(workspace_path: Path, max_depth: int = 4):
    repos = []

    def walk(p: Path, depth: int):
        if depth > max_depth:
            return
        try:
            entries = sorted(p.iterdir())
        except (PermissionError, OSError):
            return
        for sub in entries:
            if not sub.is_dir():
                continue
            if sub.name in (".git", "node_modules", "target", ".venv", "__pycache__"):
                continue
            if (sub / ".git").exists():
                repos.append(sub)
                continue
            walk(sub, depth + 1)

    walk(workspace_path, 1)
    return repos


def repo_origin(repo: Path):
    r = run_git(["config", "--get", "remote.origin.url"], repo)
    return r.stdout.strip() or None


def normalize_origin(url: str | None):
    if not url:
        return None
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    u = re.sub(r"^(git@|https?://)[^/:]+[/:]", "", u)
    return u.lower()


def dedupe_repos(repos):
    by_key, no_origin = {}, []
    for r in repos:
        origin = repo_origin(r)
        key = normalize_origin(origin)
        if not key:
            no_origin.append((origin, r))
            continue
        by_key.setdefault(key, []).append((origin, r))
    canonical = []
    for key, pairs in by_key.items():
        pairs.sort(key=lambda p: len(str(p[1])))
        canonical.append(pairs[0])
    canonical.extend(no_origin)
    return canonical


def current_branch(repo: Path):
    return run_git(["branch", "--show-current"], repo).stdout.strip()


def list_candidate_branches(repo: Path, excluded: set):
    """Local branches eligible for capture: everything except default/integration
    branches (those are not tickets). Free-form names allowed."""
    r = run_git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], repo)
    out = []
    for b in r.stdout.split("\n"):
        b = b.strip()
        if b and b not in excluded:
            out.append(b)
    return out


def fetch_repo(repo: Path, branch_names):
    """Read-only refresh of ONLY the integration/production refs the sync needs.

    Non-destructive, by design and verifiably:
      * `git fetch` never writes the remote and never touches `refs/heads/*` or the
        working tree — it only reads the remote and updates the local mirror under
        `refs/remotes/origin/*`. Your branches, commits and uncommitted work are
        untouched.
      * We fetch each needed branch by its EXACT refspec
        (`+refs/heads/<b>:refs/remotes/origin/<b>`), one at a time — never a `*`
        pattern. A remote with branches differing only in case (e.g. `Feat/x` and
        `feat/x`) makes a wildcard `fetch --prune` of all heads fail on a
        case-insensitive filesystem; an exact ref name cannot collide with itself.
      * No `--prune`: nothing is deleted locally (the old all-heads prune was what
        churned/deleted mirror refs).

    Returns (ok, reason): ok=True if at least one requested ref fetched cleanly;
    reason carries the last git error when nothing fetched (offline / unreachable).
    A branch the repo's remote doesn't have ("couldn't find remote ref") is expected
    and never counts as a failure on its own.
    """
    any_ok, last_err = False, ""
    for b in branch_names:
        refspec = f"+refs/heads/{b}:refs/remotes/origin/{b}"
        try:
            r = run_git(["fetch", "--no-tags", "origin", refspec], repo, timeout=120)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        if r.returncode == 0:
            any_ok = True
        elif "couldn't find remote ref" not in (r.stderr or "").lower():
            err = (r.stderr or "").strip().splitlines()
            last_err = err[-1] if err else f"rc={r.returncode}"
    return any_ok, last_err


def branch_unchanged_since_hwm(state: dict, origin_norm: str, branch: str, repo: Path) -> bool:
    """True iff this branch's tip equals the stored per-branch SHA high-water mark —
    i.e. it has ZERO new commits since the last successful capture. Exact (sha
    equality), not a date horizon: a branch with an old-but-never-captured commit has
    tip != HWM and is still walked."""
    hwm = state.get("last_processed_commit", {}).get(origin_norm, {}).get(branch)
    if not hwm:
        return False  # never captured -> defer to the date floor / walk
    tip = run_git(["rev-parse", branch], repo).stdout.strip()
    return bool(tip) and tip == hwm


def branch_skippable(state: dict, origin_norm: str, branch: str, repo: Path) -> bool:
    """Nothing new to examine on this branch, by either safe signal:

      * tip == per-branch SHA-HWM (exact: this tip was already captured), or
      * tip's commit date < the repo's `last_examined_at` (the date HWM): every
        commit on the branch predates the last full, error-free examination of this
        repo, so it was already seen.

    Both are safe against the sync-was-down case because `last_examined_at` only
    advances on a clean, fetch-OK, error-free run — a never-examined backlog (e.g. a
    repo whose fetch was failing) has no date recorded for it and is fully walked."""
    if branch_unchanged_since_hwm(state, origin_norm, branch, repo):
        return True
    last_exam = state.get("last_examined_at", {}).get(origin_norm)
    if last_exam:
        tip_date = run_git(["log", "-1", "--format=%cs", branch], repo).stdout.strip()
        if tip_date and tip_date < last_exam:
            return True
    return False


def resolve_default_branch(repo: Path, candidates):
    for b in candidates:
        if run_git(["rev-parse", "--verify", b], repo).returncode == 0:
            return b
    r = run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], repo)
    if r.returncode == 0:
        return r.stdout.strip().split("/")[-1]
    return None


def user_email(repo: Path):
    return run_git(["config", "user.email"], repo).stdout.strip()


def author_commits_for_branch(repo: Path, branch: str, author: str, since_spec: dict, int_refs=()):
    """Resolve the branch's OWN authored commits (HWM range / --since window).

    Excludes merge commits (`--no-merges`) and anything already reachable from an
    integration branch (`--not <int_refs>`), so a branch created off — and not
    ahead of — dev/master yields zero commits and is skipped instead of being
    credited with the whole dev->master gap. `--not` takes ALL refs in one clause:
    a per-ref `--not a --not b` toggles sense back and re-includes `b`."""
    tail = ["--pretty=format:%H%x09%ad%x09%s", "--date=iso-strict"]
    kind = since_spec["kind"]
    if kind == "commit":
        rng, win = [f"{since_spec['sha']}..{branch}"], []
    elif since_spec.get("hours") is not None:
        rng, win = [branch], [f"--since={since_spec['hours']} hours ago"]
    else:
        rng, win = [branch], [f"--since={since_spec['iso']}"]
    not_clause = ["--not", *int_refs] if int_refs else []
    args = ["log", *rng, "--no-merges", f"--author={author}", *win, *tail, *not_clause]
    r = run_git(args, repo)
    return [line for line in r.stdout.split("\n") if line.strip()]


def _since_window_args(since_spec: dict):
    """The `--since`/range-window flags for a date-kind since_spec. Commit-kind has
    no date window (it's a sha..branch range), so returns []."""
    if since_spec.get("kind") == "commit":
        return []
    if since_spec.get("hours") is not None:
        return [f"--since={since_spec['hours']} hours ago"]
    return [f"--since={since_spec['iso']}"]


def merge_parent_base(repo: Path, branch: str, int_refs):
    """If `branch` was integrated via a MERGE COMMIT on any integration ref, return that
    merge's first-parent SHA (the integration side). Then `<base>..branch` is EXACTLY the
    branch's own commits — no trunk bleed. Returns None for ff/squash merges (no merge
    commit references the branch tip as a parent).

    Found by scanning each int_ref's recent merges for one whose non-first parent is the
    branch tip — i.e. the commit that merged this branch in. First-parent = the trunk it
    landed on, so excluding it isolates the branch's contribution."""
    tip = run_git(["rev-parse", branch], repo).stdout.strip()
    if not tip:
        return None
    for ref in int_refs:
        r = run_git(["rev-list", "--merges", "--parents", "-n", "500", ref], repo)
        for line in r.stdout.splitlines():
            parts = line.split()
            # "<merge> <p1> <p2> ...": tip as a 2nd+ parent => this merge brought it in.
            if len(parts) >= 3 and tip in parts[2:]:
                return parts[1]
    return None


def _landed_range(branch: str, base, since_spec: dict):
    """Range + window flags for mining a merged branch's own work, bounded so backfill
    never reaches before the branch's processing boundary (the bug that mass-re-fired).

      - commit-kind (the branch HAS an HWM): mine `hwm..branch` — only commits AFTER the
        last processed one. Mining `base..branch` (merge fork) would reach back before the
        HWM; an already-fully-processed branch (HWM==tip) yields EMPTY here -> no backfill.
      - date-kind, base given (merge first-parent): `base..branch` + `--since` window —
        exact own-range (no trunk bleed) AND date-bounded so an OLD merge isn't mined.
      - date-kind, no base (ff/squash): `branch` + `--since` window (bleed bounded by it)."""
    if since_spec.get("kind") == "commit":
        return [f"{since_spec['sha']}..{branch}"], []
    if base:
        return [f"{base}..{branch}"], _since_window_args(since_spec)
    return [branch], _since_window_args(since_spec)


def author_landed_commits(repo: Path, branch: str, author: str, since_spec: dict, base=None):
    """The author's own non-merge commits for a merged branch (see _landed_range).
    Commit-anchored on purpose: once merged, the tip is an ancestor of the integration
    ref, so a ref-anchored `<int_ref>..branch` collapses to empty."""
    rng, win = _landed_range(branch, base, since_spec)
    args = ["log", *rng, "--no-merges", f"--author={author}", *win,
            "--pretty=format:%H%x09%ad%x09%s", "--date=iso-strict"]
    r = run_git(args, repo)
    return [line for line in r.stdout.split("\n") if line.strip()]


def author_landed_diff(repo: Path, branch: str, author: str, since_spec: dict, base=None, max_chars: int = 60000):
    """Per-commit patches of the author's own commits for a merged branch (mining diff)."""
    rng, win = _landed_range(branch, base, since_spec)
    args = ["log", *rng, "--no-merges", f"--author={author}", *win,
            "-p", "--reverse", "--pretty=format:%n=== commit %h %ad %s ===%n", "--date=short"]
    out = run_git(args, repo, timeout=120).stdout
    if len(out) > max_chars:
        return out[:max_chars] + f"\n\n[... truncated, full diff was {len(out)} chars ...]"
    return out


def author_landed_stat(repo: Path, branch: str, author: str, since_spec: dict, base=None):
    """Diff stat for the author's own commits for a merged branch."""
    rng, win = _landed_range(branch, base, since_spec)
    args = ["log", *rng, "--no-merges", f"--author={author}", *win, "--stat", "--pretty=format:"]
    return run_git(args, repo, timeout=60).stdout.strip()


def merged_ticketless_backfill(repo: Path, branch: str, author: str, since_spec: dict,
                               int_refs, vault: Path, workspace: str, project: str):
    """Detect the capture<->finalize crack and return (commits, base) to backfill, or None.

    The crack: a branch committed AND merged within one sync interval is invisible to
    BOTH passes — capture finds zero own-commits (`--not <integration>` excludes the now-
    merged commits) and finalize has no ticket to resolve (capture never created one).

    Returns (commits, base) — `base` is the merge first-parent for an exact, bleed-free
    own-range (None for the HWM/ff cases, where _landed_range bounds differently) — when:
      - normal capture is empty for this branch (its work is integration-reachable = merged),
      - no KB ticket exists yet for the branch (any status),
      - the author has own commits to mine WITHIN the branch's window (HWM or --since).
    Self-contained (re-checks capture-empty) so it's unit-testable in isolation.

    A branch with an HWM is bounded by `hwm..branch` (commit-kind), so a fully-processed
    branch (HWM==tip) yields no commits and is NOT backfilled — only genuinely new merged
    work is. merge_parent_base is only meaningful for the date-kind (no-HWM) path."""
    if author_commits_for_branch(repo, branch, author, since_spec, int_refs):
        return None  # not merged — normal capture handles it
    if ticket_status_for_branch(vault, workspace, project, branch) is not None:
        return None  # already ticketed (any status) — not our gap
    base = None if since_spec.get("kind") == "commit" else merge_parent_base(repo, branch, int_refs)
    commits = author_landed_commits(repo, branch, author, since_spec, base=base)
    if not commits:
        return None
    return commits, base


def diff_stat(repo: Path, branch: str, base: str):
    r = run_git(["diff", "--stat", f"{base}...{branch}"], repo, timeout=60)
    return r.stdout.strip()


def diff_full(repo: Path, branch: str, base: str, max_chars: int = 60000):
    r = run_git(["diff", f"{base}...{branch}"], repo, timeout=120)
    out = r.stdout
    if len(out) > max_chars:
        return out[:max_chars] + f"\n\n[... truncated, full diff was {len(out)} chars ...]"
    return out


def diff_two_dot(repo: Path, base: str, ref: str, max_chars: int = 60000):
    """Net contribution of a branch (base..ref) — what it landed on merge."""
    if not base or not ref:
        return ""
    out = run_git(["diff", f"{base}..{ref}"], repo, timeout=120).stdout
    if len(out) > max_chars:
        return out[:max_chars] + f"\n\n[... truncated, full diff was {len(out)} chars ...]"
    return out


def parse_frontmatter(path: Path):
    txt = path.read_text(encoding="utf-8")
    m = FM_RE.match(txt)
    if not m:
        return {}
    out = {}
    for line in m.group(1).split("\n"):
        mm = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if mm:
            out[mm.group(1)] = mm.group(2).strip()
    return out


def _iter_ticket_dirs(base: Path):
    """Yield ticket folders under a project, supporting both layouts:
       <project>/<slug>/         (ungrouped — branch with no "/")
       <project>/<type>/<slug>/  (type-grouped)
    A ticket folder is identified by containing `_index.md`."""
    if not base.exists():
        return
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name == "Learnings":
            continue
        if (child / "_index.md").exists():
            yield child                       # ungrouped ticket
            continue
        for sub in sorted(child.iterdir()):   # type dir -> tickets
            if sub.is_dir() and (sub / "_index.md").exists():
                yield sub


def ticket_status_for_branch(vault: Path, workspace: str, project: str, branch: str):
    for tdir in _iter_ticket_dirs(vault / workspace / project):
        fm = parse_frontmatter(tdir / "_index.md")
        if fm.get("branch", "") == branch:
            return fm.get("status", "")
    return None


def list_open_tickets(vault: Path, workspace: str, project: str):
    out = []
    for tdir in _iter_ticket_dirs(vault / workspace / project):
        fm = parse_frontmatter(tdir / "_index.md")
        status = fm.get("status", "")
        # experimental included so a merge-to-production can finalize it; capture
        # still skips experimental (paused until prod merge or manual in-progress).
        if status in ("open", "in-progress", "experimental", ""):
            out.append({
                "id": fm.get("id", ""),
                "slug": fm.get("slug", ""),
                "branch": fm.get("branch", ""),
                "title": fm.get("title", ""),
                "path": tdir,
            })
    return out


def resolve_ref(repo: Path, name: str):
    """Resolve a branch to a usable ref, preferring the remote-tracking copy
    (origin/<name>) so merge state reflects the remote, not the stale local."""
    for ref in (f"origin/{name}", name):
        if run_git(["rev-parse", "--verify", "--quiet", ref], repo).returncode == 0:
            return ref
    return None


def resolved_integration_refs(repo: Path, integration_branches: list):
    """Existing integration refs for this clone, preferring origin/<name> (freshest
    remote state) and dropping names that resolve nowhere (e.g. develop/main absent).
    Used for attribution: what counts as 'already integrated, not this branch's work'."""
    out = []
    for b in integration_branches:
        ref = resolve_ref(repo, b)
        if ref and ref not in out:
            out.append(ref)
    return out


def nearest_integration_base(repo: Path, branch: str, int_refs: list):
    """The integration ref the branch forks from = the one with the FEWEST commits
    in `<ref>..branch`. Diff base, so a branch made off dev is diffed against dev,
    not a stale master hundreds of commits behind."""
    best, best_n = None, None
    for ref in int_refs:
        r = run_git(["rev-list", "--count", f"{ref}..{branch}"], repo)
        try:
            n = int(r.stdout.strip())
        except ValueError:
            continue
        if best_n is None or n < best_n:
            best, best_n = ref, n
    return best


def patch_id_of_diff(diff_text: str):
    if not diff_text.strip():
        return None
    try:
        r = subprocess.run(["git", "patch-id", "--stable"], input=diff_text,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
    except Exception:
        return None
    parts = r.stdout.split()
    return parts[0] if parts else None


def integration_patch_ids(repo: Path, merge_base: str, int_ref: str, limit: int = 400):
    """patch-ids of (non-merge) commits on int_ref since merge_base, in one pass.
    A squash-merge commit's diff equals the branch's combined diff -> same id."""
    try:
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "-p", "--no-merges", f"-n{limit}",
             f"{merge_base}..{int_ref}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        if not log.stdout.strip():
            return set()
        pid = subprocess.run(["git", "patch-id", "--stable"], input=log.stdout,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=120)
    except Exception:
        return set()
    ids = set()
    for line in pid.stdout.splitlines():
        parts = line.split()
        if parts:
            ids.add(parts[0])
    return ids


def detect_merge(repo: Path, integration_branches: list, branch: str):
    """Positive merge evidence for a branch in ONE clone. Returns a dict or None.

    Requires the branch to be resolvable in this clone (local or origin/). Pure
    absence is NOT handled here — a branch missing from one clone may still be
    alive in another; "gone" is decided across clones by the caller.

    status="merged" -> work landed on an integration branch. `method`:
        ancestor : merge-commit / fast-forward (git ancestry)
        cherry   : rebase / single-commit squash (patch-id equivalence)
        patchid  : multi-commit squash (combined patch-id match)
    """
    ref = resolve_ref(repo, branch)
    if ref is None:
        return None

    for intb in integration_branches:
        int_ref = resolve_ref(repo, intb)
        if int_ref is None:
            continue
        mb = run_git(["merge-base", int_ref, ref], repo).stdout.strip()
        if not mb:
            continue
        method = None
        if run_git(["merge-base", "--is-ancestor", ref, int_ref], repo).returncode == 0:
            method = "ancestor"
        else:
            cherry = run_git(["cherry", int_ref, ref], repo).stdout.strip().splitlines()
            if cherry and all(l.startswith("-") for l in cherry):
                method = "cherry"
            else:
                combined = run_git(["diff", f"{mb}..{ref}"], repo, timeout=120).stdout
                cpid = patch_id_of_diff(combined)
                if cpid and cpid in integration_patch_ids(repo, mb, int_ref):
                    method = "patchid"
        if method:
            landed = run_git(["log", "-1", "--format=%ad", "--date=short", ref], repo).stdout.strip()
            return {"status": "merged", "integration": intb, "method": method,
                    "ref": ref, "merge_base": mb,
                    "landed_date": landed or datetime.now().strftime("%Y-%m-%d")}
    return None


def manually_done_branches() -> set:
    """Branches the user closed via `/kb-mark --done` (sidecar flag manual_done)."""
    out = set()
    if not STATE_DIR.exists():
        return out
    for sc in STATE_DIR.glob("kb-session-branch-*.json"):
        try:
            d = json.loads(sc.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("manual_done") and d.get("branch"):
            out.add(d["branch"])
    return out


def clear_manual_done(branch: str):
    """Drop manual_done from any sidecar pointing at this branch. Called after
    a successful finalize so a future branch reusing the name doesn't re-fire it."""
    if not STATE_DIR.exists():
        return
    for sc in STATE_DIR.glob("kb-session-branch-*.json"):
        try:
            d = json.loads(sc.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("branch") == branch and d.get("manual_done"):
            d.pop("manual_done", None)
            d.pop("done_at", None)
            try:
                sc.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass


def manually_experimental_branches() -> set:
    """Branches the user flagged via `/kb-mark --experimental` (sidecar flag
    mark_experimental). Their freshly-captured _index.md is force-set to
    status=experimental so retrieval down-weights them until they merge."""
    out = set()
    if not STATE_DIR.exists():
        return out
    for sc in STATE_DIR.glob("kb-session-branch-*.json"):
        try:
            d = json.loads(sc.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("mark_experimental") and d.get("branch"):
            out.add(d["branch"])
    return out


def clear_mark_experimental(branch: str):
    """Drop mark_experimental from sidecars for this branch after it's been
    applied, so a future branch reusing the name doesn't inherit the flag."""
    if not STATE_DIR.exists():
        return
    for sc in STATE_DIR.glob("kb-session-branch-*.json"):
        try:
            d = json.loads(sc.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("branch") == branch and d.get("mark_experimental"):
            d.pop("mark_experimental", None)
            try:
                sc.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass


def set_index_status(vault: Path, folder_rel: str, status: str) -> bool:
    """Patch `status:` in <vault>/<folder_rel>/_index.md frontmatter. True on write."""
    idx = vault / folder_rel / "_index.md"
    if not idx.is_file():
        return False
    try:
        text = idx.read_text(encoding="utf-8")
    except Exception:
        return False
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end < 0:
        return False
    fm, rest = text[:end], text[end:]
    if re.search(r"(?m)^status:\s*.*$", fm):
        fm = re.sub(r"(?m)^status:\s*.*$", f"status: {status}", fm, count=1)
    else:
        fm = fm.rstrip("\n") + f"\nstatus: {status}\n"
    try:
        idx.write_text(fm + rest, encoding="utf-8")
        return True
    except Exception:
        return False


def claude_run(prompt: str, max_turns: int, dry_run: bool):
    if dry_run:
        print(f"  [DRY] would invoke claude --print (--max-turns={max_turns}, prompt={len(prompt)} chars)")
        return 0, "", ""
    cmd = ["claude", "--print", "--max-turns", str(max_turns), "--dangerously-skip-permissions"]
    try:
        r = subprocess.run(
            cmd, input=prompt, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=900,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        # Don't let one hung claude call kill the whole sync; caller treats
        # rc!=0 as failure and skips HWM bump so the next run retries.
        partial = e.stdout
        if isinstance(partial, (bytes, bytearray)):
            partial = partial.decode("utf-8", errors="replace")
        return 124, (partial or ""), "TimeoutExpired after 900s"
    except Exception as e:  # encoding/oserror/etc — keep going
        return 1, "", f"claude_run exception: {type(e).__name__}: {e}"


def build_pre_extracted_block(topk_learnings: list, topk_transcripts: list, vault: Path) -> str:
    """Format embedding-retrieved snippets for inline injection into prompts.

    Each entry's full text comes from the chunk's stored `text` field (set during
    reindex). For older cache rows that predate that field, falls back to reading
    the underlying .md from disk for "md" kind, or to the short preview otherwise.
    """
    if not topk_learnings and not topk_transcripts:
        return ""
    parts = []
    if topk_learnings:
        parts.append("## Pre-extracted learnings (top-K by embedding similarity)")
        parts.append("These were semantically filtered against the diff/branch. **Audit the diff AGAINST them.** Don't browse other `Learnings/*.md` via MCP — they already passed the filter.")
        parts.append("")
        for item in topk_learnings:
            body = item.get("text") or ""
            if not body and item.get("kind") == "md" and kb_embed:
                body = kb_embed.read_md_body(vault, item.get("path", ""), max_chars=1500)
            if not body:
                body = item.get("preview", "")
            body = body.strip()[:1500]
            sim = item.get("score", 0.0)
            scope = item.get("scope", "?")
            path = item.get("path", "?")
            parts.append(f"### [[{path}]]  (sim={sim:.2f}, scope={scope})")
            parts.append("```markdown")
            parts.append(body)
            parts.append("```")
            parts.append("")
    if topk_transcripts:
        parts.append("## Excerpts from previous sessions (top-K relevant turns)")
        parts.append("Design/intent insights from the user captured in these conversations. Pre-curated — no need to open `.jsonl` via Read.")
        parts.append("")
        for item in topk_transcripts:
            text = (item.get("text") or item.get("preview", "")).strip()[:1200]
            sid = (item.get("session_id") or "?")[:12]
            sim = item.get("score", 0.0)
            parts.append(f"### session {sid} turn {item.get('turn_idx','?')} (sim={sim:.2f})")
            parts.append("```")
            parts.append(text)
            parts.append("```")
            parts.append("")
    return "\n".join(parts)


def retrieve_for_branch(store, branch: str, project: str, commits: list, stat: str,
                         k_learn: int = 5, k_trans: int = 3, min_score: float = 0.25):
    """Run two retrievals for a (branch, project): learnings + transcripts.

    Returns (learnings, transcripts). Always succeeds — returns ([], []) on any
    error so the caller can proceed with the legacy prompt path."""
    if store is None or kb_embed is None:
        return [], []
    try:
        commit_subjects = " | ".join(c.split("\t", 2)[-1] for c in commits[:10])
        query = f"{branch} | {commit_subjects} | {stat[:1500]}"
        learnings = kb_embed.retrieve_top_k(
            query, k=k_learn,
            scope={"workspace", "project", "ticket"},
            project=project,
            kind={"md"},
            store=store,
        )
        transcripts = kb_embed.retrieve_top_k(
            query, k=k_trans,
            kind={"transcript"},
            branch=branch,
            store=store,
        )
        # drop noise: low-similarity hits typically aren't relevant and just bloat
        # the prompt. Below ~0.25 cosine the chunk is barely related.
        learnings = [x for x in learnings if x.get("score", 0.0) >= min_score]
        transcripts = [x for x in transcripts if x.get("score", 0.0) >= min_score]
        return learnings, transcripts
    except Exception as e:
        print(f"  [embed] retrieve failed for {branch}: {e}")
        return [], []


def dedup_scan(report, vault: Path, threshold: float = 0.80, max_pairs: int = 25) -> list[dict]:
    """Flag learnings WRITTEN THIS RUN that overlap another learning — a same-run
    sibling (the audit can't see files being created in its own pass) OR an existing
    vault learning the capture-time retrieval failed to surface.

    Detection only. It never merges or edits: a blind vault write is forbidden
    (`escrita errada envenena consultas`) and merging is lossy — the unique delta on
    the 'duplicate' side can be the thing worth keeping. The output is a report the
    human reviews; the fix is theirs. The primary defense against twins is the capture
    prompt's "one insight = one file" rule; this is the safety net behind it.

    Mechanism: re-embed the vault (incremental — only this run's writes cost anything),
    then for each touched learning retrieve its nearest neighbours by body and keep
    pairs at/above `threshold` cosine where at least one side was written this run.
    `_index.md` neighbours are skipped (a ticket summary legitimately overlaps its own
    learnings — not a duplicate).

    Threshold is recall-biased on purpose. Calibrated on a real vault: a same-run twin
    (two learning files written for one fix) scored ~0.84; distinct same-topic siblings
    topped out ~0.82; clearly-unrelated pairs sat <0.66. Cosine cannot fully separate
    a twin from a distinct sibling (~0.01 apart), so for a REVIEW signal a false positive
    (a glance) is cheaper than a false negative (a dup that persists) — hence 0.80, catching
    the twin with margin and surfacing a few near-siblings for the human to dismiss.
    Embedding-bound and fail-open: any embedding error returns [] (never blocks sync)."""
    if kb_embed is None:
        return []
    touched = [p for p in report.changed_files(vault)
               if "/Learnings/" in p.as_posix() and p.name != "_index.md"]
    if not touched:
        return []
    touched_rels = {p.relative_to(vault).as_posix() for p in touched}
    try:
        store = kb_embed.VectorStore()
        kb_embed.reindex_vault(vault, store, verbose=False)
        seen = set()
        out = []
        for p in touched:
            rel = p.relative_to(vault).as_posix()
            body = kb_embed.read_md_body(vault, rel, max_chars=2000)
            if not body.strip():
                continue
            hits = kb_embed.retrieve_top_k(body, k=6, kind={"md"}, store=store)
            best: dict[str, float] = {}
            for h in hits:
                hp = (h.get("path") or "").replace("\\", "/")
                if not hp or hp == rel or hp.endswith("_index.md"):
                    continue
                best[hp] = max(best.get(hp, 0.0), float(h.get("score", 0.0)))
            for hp, s in best.items():
                if s < threshold:
                    continue
                pair = tuple(sorted([rel, hp]))
                if pair in seen:
                    continue
                seen.add(pair)
                a_new = pair[0] in touched_rels
                b_new = pair[1] in touched_rels
                # both sides written this run = a same-run twin: the exact case the
                # capture audit is blind to (it can't see siblings born in its own
                # pass). One side new = a cross-run near-miss retrieval didn't surface.
                # Twins are the higher-confidence flag — the distinct-sibling noise at
                # 0.80-0.82 is almost all cross-run, so this split lets the human triage.
                out.append({
                    "a": pair[0], "b": pair[1], "score": round(s, 3),
                    "a_new": a_new, "b_new": b_new,
                    "kind": "twin" if (a_new and b_new) else "review",
                })
        # twins first, then by score — surface the intra-run case the user flagged.
        out.sort(key=lambda d: (d["kind"] != "twin", -d["score"]))
        return out[:max_pairs]
    except kb_embed.EmbeddingsUnavailable as e:
        print(f"  [dedup] skipped (embeddings unavailable): {e}")
        return []
    except Exception as e:
        print(f"  [dedup] error (non-fatal): {type(e).__name__}: {e}")
        return []


def format_session_hints_block(hints: list[dict]) -> str:
    if not hints:
        return ""
    lines = [
        "",
        "Claude Code session transcripts associated with this branch (incremental):",
        "Each entry shows where to RESUME reading. Use the Read tool with `offset` and `limit`",
        "to fetch only the new lines. Distill design intent, instructions, or decisions from the",
        "user that don't appear in the diff — those are the strongest learning candidates.",
        "",
    ]
    for h in hints:
        lines.append(
            f"- session {h['session_id'][:8]} — `{h['path']}` — "
            f"resume at line {h['from_line']} (new lines: {h['new_lines']}; "
            f"total now: {h['to_line']}; previously consumed: {h['prev_offset']})"
        )
    return "\n".join(lines) + "\n"


def capture_prompt(cfg, workspace, repo, branch, tipo, slug, folder_rel, default, commits, stat, diff, hints, pre_extracted: str = ""):
    vault = cfg["vault"]
    hints_block = "" if pre_extracted else format_session_hints_block(hints)
    folder = f"{workspace}/{repo.name}/{folder_rel}"
    type_line = f"type: {tipo}\n" if tipo else ""
    pre_block = f"\n{pre_extracted}\n" if pre_extracted else ""
    if pre_extracted:
        step3_text = (
            "3. **Audit mode (pre-fetched):** the relevant existing learnings (ticket + project + "
            "workspace scope) are provided in the \"Pre-extracted learnings\" block above — "
            "they passed the semantic filter against the diff. Audit the diff AGAINST them. Do "
            "NOT browse other `Learnings/*.md` — assume that block is the complete relevant set. "
            f"The only extra file you may Read is this ticket's own `{vault}/{folder}/_index.md` "
            "(if it exists)."
        )
    else:
        step3_text = (
            f"3. If it exists: read current `{vault}/{folder}/_index.md` and existing "
            f"`{vault}/{folder}/Learnings/*.md` (ticket scope), plus "
            f"`{vault}/{workspace}/{repo.name}/Learnings/*.md` (project scope) and "
            f"`{vault}/{workspace}/Learnings/*.md` (workspace scope) by frontmatter + first paragraph."
        )
    return f"""You are running headless in KB sync mode. Do not ask for confirmation — execute.

Vault root (absolute): `{vault}`
**Use the standard Read/Write/Edit/Glob tools — do NOT use any MCP server.** Every KB path below is RELATIVE to the vault root; prefix it (e.g. create `{vault}/{folder}/_index.md`). The headless run's cwd is a code dir, NOT the vault — always use the absolute vault path above.
Workspace folder: `{workspace}/`
Project folder: `{workspace}/{repo.name}/`
Branch: `{branch}` (the branch name is the KB match key; type={tipo or '—'}, slug={slug})
Default branch: {default}

Recent commits by user on this branch:
```
{chr(10).join(commits[:50])}
```

Diff stat ({default}...{branch}):
```
{stat}
```

Full diff (may be truncated):
```diff
{diff}
```
{hints_block}{pre_block}
Task:
1. Check if the KB folder `{vault}/{folder}/` already exists (exact path — derives from the branch, not a numeric id). Use Glob or Bash `ls`.
2. If missing: create `{vault}/{folder}/Learnings/` and `{vault}/{folder}/_index.md` with this frontmatter (English schema):
```yaml
---
project: {repo.name}
{type_line}module:
slug: {slug}
title:
status: open
opened: <YYYY-MM-DD today>
resolved:
last_update: <YYYY-MM-DD today>
apparent_problem: |-
  <derived from commit messages — concise>
actual_solution:
tags: []
related_tickets: []
branch: {branch}
pr:
---
```
{step3_text}
4. Audit current learnings against the diff. For each one classify:
   - CONFIRMS — diff matches the learning, keep.
   - REFUTES — diff contradicts; correct the file, add `## Correction history` line.
   - ADJUSTS — partially right; edit to incorporate nuance. You MAY trim an over-long
     existing learning toward its delta (drop tutorial scaffolding), but never delete a file.
   - ADDS — diff/commits reveal a pattern that PASSES the worth-saving test below;
     create a new learning file. Classify scope:
     - ticket: `{vault}/{folder}/Learnings/<name>.md`
     - project: `{vault}/{workspace}/{repo.name}/Learnings/<name>.md`
     - workspace: `{vault}/{workspace}/Learnings/<name>.md`
   Default conservative: ticket.

   **Worth-saving test — the bar for ADDS (when in doubt, SKIP):**
   A learning earns its place ONLY if it carries what a strong model + the visible
   code/diff CANNOT regenerate. Ask "where else could this come from?":
   - Nowhere but a human decision/fact — a magic constant's meaning (`202`=RETURN_ORDER),
     a rule confirmed with a person/PR ("confirmed with the tax team…"; "specific to
     this carrier's API — PR #1084"), a deliberate trade-off + its rationale → SAVE (highest value).
   - From the code but cross-file / hidden — existence + contract of a project util/hook
     (a Tuple-to-POJO mapper util, a canonical post-emission hook method), a naming
     convention (an `*ActiveOnly` DAO suffix), a stack-specific silent-fail gotcha (Jackson
     reads `active` not `isActive`) → SAVE.
   - Only from the model's general knowledge — a framework how-to it already emits
     (Chart.js dual axis, CSS `min-width:0`, "extract a value object"), a generic best
     practice → DO NOT SAVE (noise; it dilutes retrieval).
   - EXCEPTION: a generic technique the team deliberately standardizes AGAINST the
     model's default (a house convention enforced in review) → a SHORT note capturing
     the DECISION, not the tutorial.

   **Write the delta, not a tutorial.** Lead with the surprising part — the fact/gotcha/
   decision the model wouldn't already say. Strip what a competent dev already knows (what
   the framework is, standard API usage). If nothing non-regenerable survives the strip,
   SKIP rather than create the file.

   **One insight = one file — no twin learnings this run.** When this diff yields several
   ADDS, audit them against EACH OTHER, not only the existing vault: if two candidates
   restate the same delta, emit the single most general one and let `_index.md` cross-link
   it — never write near-duplicate siblings. The existing-learnings view above does NOT
   include files you are creating in this same run, so this dedupe is yours to enforce.
   Prefer ADJUSTS on an existing file over a new near-duplicate.
5. Update `{vault}/{folder}/_index.md` apparent_problem / tags / related_tickets based on commits + diff. **Always set `last_update: <YYYY-MM-DD today>`**. Keep `branch: {branch}` intact — it is the match key. Do NOT set status=resolved here — only the finalize routine does that. Use English frontmatter keys: project, type, module, slug, title, opened, resolved, last_update, apparent_problem, actual_solution, related_tickets, branch, pr. Status enum: open|in-progress|resolved|discarded. Scope enum: ticket|project|workspace.
6. Report briefly: created/updated file paths, CONFIRMS/REFUTES/ADJUSTS/ADDS counts and names.

YAML rules: long text fields use literal `|-` block; tags as `[a, b, c]`. Validate YAML before writing.
Do not ask. Default conservative: ticket scope; skip on ambiguity rather than promote.
"""


def finalize_prompt(cfg, workspace, repo, ticket, res, landed_diff, hints, pre_extracted: str = ""):
    vault = cfg["vault"]
    hints_block = "" if pre_extracted else format_session_hints_block(hints)
    pre_block = f"\n{pre_extracted}\n" if pre_extracted else ""
    if pre_extracted:
        steps12_text = (
            f"1. Read `{vault}/{ticket['path']}/_index.md` and all `{vault}/{ticket['path']}/Learnings/*.md` of THIS ticket (Read/Glob).\n"
            "2. **Cross-scope (pre-fetched mode):** the relevant project- and workspace-scope "
            "learnings are provided in the \"Pre-extracted learnings\" block above. Audit "
            "AGAINST those — do NOT browse other `Learnings/*.md`."
        )
    else:
        steps12_text = (
            f"1. Read `{vault}/{ticket['path']}/_index.md` and all `{vault}/{ticket['path']}/Learnings/*.md` (Read/Glob).\n"
            f"2. Also read `{vault}/{workspace}/{repo.name}/Learnings/*.md` (project scope) and "
            f"`{vault}/{workspace}/Learnings/*.md` (workspace scope) — frontmatter + first paragraph of each."
        )
    if res["status"] == "merged":
        how = (f"The branch landed on `{res['integration']}` (detected via "
               f"{res['method']}). The diff it contributed is below.")
        diff_block = f"\nWhat the branch landed:\n```diff\n{landed_diff}\n```\n"
    elif res["status"] == "manual":
        how = ("The user explicitly closed this ticket via `/kb-mark --done`. "
               "Synthesize `actual_solution` from the accumulated `_index.md` + Learnings.")
        diff_block = ""
    else:  # gone
        how = ("The branch no longer exists (deleted on merge). No final diff is "
               "available — synthesize `actual_solution` from the accumulated "
               "`_index.md` + Learnings and the earlier captured commits.")
        diff_block = ""
    return f"""You are running headless in KB finalize mode. Do not ask — execute.

Vault: `{vault}` (use obsidian-vault MCP).
KB folder: `{ticket['path']}`
Project: {repo.name}
Branch (match key): {ticket.get('branch','')}

Resolution: {how}
Resolved date: {res['landed_date']}
{diff_block}{hints_block}{pre_block}
Task:
{steps12_text}
3. Synthesize `actual_solution` (2-4 lines, past tense: what was actually done) from the evidence above.
4. Audit each existing learning (ticket + project + workspace scope):
   - CONFIRMS — keep.
   - REFUTES — correct the file, add `## Correction history` line.
   - ADJUSTS — edit to incorporate nuance.
   - ADDS — new pattern not yet recorded; create new learning file (default scope: ticket).
   Workspace/project-scope corrections need concrete evidence; in doubt, leave them alone and add a ticket-scope note.
5. Update `_index.md`:
   - status: resolved
   - resolved: {res['landed_date']}
   - last_update: <YYYY-MM-DD today>
   - actual_solution: <synthesized>
   - keep `branch` and `pr` as-is.
6. Report: status set, learnings audit counts (CONFIRMS/REFUTES/ADJUSTS/ADDS), ambiguous decisions noted.

YAML rules: long text fields use literal `|-` block; tags as `[a, b, c]`. English schema keys only. Status enum: open|in-progress|resolved|discarded.
Do not ask.
"""


class RunReport:
    def __init__(self):
        self.start_ts = time.time()
        self.start_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.captures: list[dict] = []
        self.finalizes: list[dict] = []
        self.errors: list[dict] = []
        self.fetch_failures: list[dict] = []
        self.duplicates: list[dict] = []
        self.repos_discovered = 0
        self.repos_fetched = 0

    def add_fetch_failure(self, workspace: str, repo_rel: str, origin: str, reason: str = ""):
        self.fetch_failures.append({"workspace": workspace, "repo_rel": repo_rel,
                                    "origin": origin, "reason": reason})

    def note_scan(self, discovered: int, fetched: int):
        """Accumulate repo scan/fetch counts across workspaces (sync-health scope)."""
        self.repos_discovered += discovered
        self.repos_fetched += fetched

    def add_capture(self, workspace: str, repo_name: str, repo_rel: str, branch: str,
                    ticket_id: str, slug: str, commits: int, hints: list,
                    bumped: bool, rc: int, stdout: str, stderr: str, since_source: str = "unknown"):
        self.captures.append({
            "workspace": workspace,
            "repo_name": repo_name,
            "repo_rel": repo_rel,
            "branch": branch,
            "ticket_id": ticket_id,
            "slug": slug,
            "commits": commits,
            "hints": hints,
            "bumped": bumped,
            "rc": rc,
            "stdout": stdout,
            "stderr": stderr,
            "since_source": since_source,
        })

    def add_finalize(self, workspace: str, repo_name: str, repo_rel: str, ticket: dict,
                     merge: dict, hints: list, bumped: bool, rc: int, stdout: str, stderr: str):
        self.finalizes.append({
            "workspace": workspace,
            "repo_name": repo_name,
            "repo_rel": repo_rel,
            "ticket_id": ticket.get("id", ""),
            "slug": ticket.get("slug", ""),
            "type": ticket.get("type", ""),
            "branch": ticket.get("branch", ""),
            "merge_hash": merge.get("hash", ""),
            "merge_subject": merge.get("subject", ""),
            "merge_branch": merge.get("branch", ""),
            "merge_date": merge.get("date", ""),
            "hints": hints,
            "bumped": bumped,
            "rc": rc,
            "stdout": stdout,
            "stderr": stderr,
        })

    def changed_files(self, vault: Path) -> list[Path]:
        if not vault.exists():
            return []
        out = []
        for p in vault.rglob("*.md"):
            if ".obsidian" in p.parts:
                continue
            try:
                if p.stat().st_mtime >= self.start_ts:
                    out.append(p)
            except OSError:
                continue
        return sorted(out)

    def has_activity(self) -> bool:
        return bool(self.captures or self.finalizes)

    def to_record(self, vault: Path, dry_run: bool) -> dict:
        """A compact, structured record of this run for the sync-history sidecar the
        manager reads. `learned_files` (vault .md touched this run, via changed_files)
        links a run to the actual knowledge it produced — the 'what did this sync
        teach' signal — reliable because it's mtime within THIS run's window."""
        def action(c):
            return "backfill" if c.get("since_source") == "backfill-merged" else "capture"
        touched = [{"repo": c.get("repo_name", ""), "branch": c.get("branch", ""), "action": action(c)}
                   for c in self.captures]
        touched += [{"repo": f.get("repo_name", ""), "branch": f.get("branch", ""), "action": "finalize"}
                    for f in self.finalizes]
        errors_detail = (
            [{"repo": c.get("repo_name", ""), "branch": c.get("branch", ""), "action": action(c), "rc": c.get("rc")}
             for c in self.captures if c.get("rc")]
            + [{"repo": f.get("repo_name", ""), "branch": f.get("branch", ""), "action": "finalize", "rc": f.get("rc")}
               for f in self.finalizes if f.get("rc")]
        )
        learned = []
        try:
            if vault and Path(vault).exists():
                learned = [p.relative_to(vault).as_posix() for p in self.changed_files(Path(vault))]
        except Exception:
            learned = []
        # Split touched files into new/updated knowledge so the manager can say
        # "this run added N learnings, touched M tickets" and link to them.
        learned_split = {
            "learnings": [p for p in learned if "/Learnings/" in p],
            "tickets": [p for p in learned if p.endswith("/_index.md")],
        }
        return {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "duration_s": int(time.time() - self.start_ts),
            "dry_run": bool(dry_run),
            "repos": {"discovered": self.repos_discovered, "fetched": self.repos_fetched},
            "captures": len(self.captures),
            "backfills": sum(1 for c in self.captures if action(c) == "backfill"),
            "finalizes": len(self.finalizes),
            "errors": len(errors_detail),
            "errors_detail": errors_detail,
            "fetch_failures": [f.get("repo_rel", "") for f in self.fetch_failures],
            "touched": touched,
            "learned_files": learned,
            "learned": learned_split,
            "duplicates": self.duplicates,
        }

    def write_html(self, vault: Path, out_path: Path):
        changed = self.changed_files(vault)
        end_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        duration_s = int(time.time() - self.start_ts)

        def esc(s):
            return html.escape(str(s)) if s is not None else ""

        def file_card(p: Path) -> str:
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                return ""
            fm = ""
            m = FM_RE.match(txt)
            if m:
                fm = m.group(1)
            try:
                rel = p.relative_to(vault).as_posix()
            except ValueError:
                rel = str(p)
            return (
                f'<div class="file"><div class="path">{esc(rel)}</div>'
                f'<pre class="fm">{esc(fm)}</pre></div>'
            )

        def offset_block(hints: list, bumped: bool) -> str:
            if not hints:
                return '<div class="offsets none">Session offsets: <b>no session with a delta</b> (nothing to read incrementally).</div>'
            bumped_tag = (
                '<span class="pill ok">bumped</span>'
                if bumped
                else '<span class="pill warn">NOT bumped</span> <small>(dry-run or rc≠0; next run reprocesses)</small>'
            )
            rows = "".join(
                f'<tr><td><code>{esc(h["session_id"][:8])}</code></td>'
                f'<td>{h["prev_offset"]}</td>'
                f'<td>{h["from_line"]}–{h["to_line"]}</td>'
                f'<td><b>{h["new_lines"]}</b></td>'
                f'<td><small>{esc(h["path"])}</small></td></tr>'
                for h in hints
            )
            return (
                f'<div class="offsets"><div class="off-head">Session offsets: {len(hints)} session(s) with a delta · {bumped_tag}</div>'
                f'<table class="off-tbl"><thead><tr><th>session</th><th>prev</th><th>range</th><th>new</th><th>jsonl</th></tr></thead>'
                f'<tbody>{rows}</tbody></table></div>'
            )

        cap_html = []
        for c in self.captures:
            status = "ok" if c["rc"] == 0 else "err"
            cap_html.append(
                f'<details class="card {status}"><summary>'
                f'<span class="tag">capture</span> '
                f'<b>{esc(c["repo_rel"])}</b> · {esc(c["branch"])} '
                f'<span class="meta">id={esc(c["ticket_id"])} · {c["commits"]} commits · window={esc(c.get("since_source","?"))} · hints={len(c["hints"])} · rc={c["rc"]}</span>'
                f'</summary>'
                + offset_block(c["hints"], c["bumped"])
                + f'<pre class="out">{esc(c["stdout"][:8000])}</pre>'
                + (f'<pre class="err">{esc(c["stderr"][:2000])}</pre>' if c["stderr"].strip() else "")
                + '</details>'
            )

        fin_html = []
        for f in self.finalizes:
            status = "ok" if f["rc"] == 0 else "err"
            fin_html.append(
                f'<details class="card {status}"><summary>'
                f'<span class="tag fin">finalize</span> '
                f'<b>{esc(f["repo_rel"])}</b> · {esc(f["slug"] or f["ticket_id"])} '
                f'<span class="meta">{("merged into " + esc(f["merge_branch"])) if f["merge_branch"] else "resolved"} · {esc(f["merge_date"])} · rc={f["rc"]}</span>'
                f'</summary>'
                f'<div class="subject">{esc(f["merge_subject"])}</div>'
                + offset_block(f["hints"], f["bumped"])
                + f'<pre class="out">{esc(f["stdout"][:8000])}</pre>'
                + (f'<pre class="err">{esc(f["stderr"][:2000])}</pre>' if f["stderr"].strip() else "")
                + '</details>'
            )

        files_html = "".join(file_card(p) for p in changed) or "<p class='empty'>No files changed.</p>"

        warn_html = ""
        if self.fetch_failures:
            rows = "".join(
                f'<li><code>{esc(f["repo_rel"])}</code> '
                f'<small style="color:#5a6473">({esc(f["workspace"])})</small> — {esc(f["origin"])}</li>'
                for f in self.fetch_failures
            )
            warn_html = (
                '<div class="card err" style="border-left-color:#fcd34d">'
                '<b style="color:#fcd34d">&#9888; Fetch failed — merge detection ran on STALE refs</b>'
                '<div style="font-size:.85rem;color:#8a93a0;margin:4px 0 2px">'
                'origin was unreachable for the repos below (offline / bad credentials). Finalize merge '
                'detection and capture base used stale remote-tracking refs, so a branch merged on the '
                'remote may be missed (ticket not finalized) and a capture diff may be slightly off until '
                'the next successful fetch. Re-run after restoring access.'
                f'</div><ul style="margin:6px 0 2px">{rows}</ul></div>'
            )

        doc = f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>kb-sync report — {end_iso}</title>
<style>
  body {{ background:#0b0d10; color:#d7dde6; font-family: ui-sans-serif, system-ui, sans-serif; margin:0; padding:24px 28px 60px; line-height:1.5; }}
  h1 {{ margin:0 0 4px; font-size:1.5rem; }}
  .lede {{ color:#8a93a0; font-size:.9rem; margin-bottom:18px; }}
  h2 {{ margin:28px 0 10px; font-size:1.1rem; border-bottom:1px solid #1f2731; padding-bottom:6px; }}
  .card {{ background:#11151a; border:1px solid #1f2731; border-radius:10px; padding:10px 14px; margin:8px 0; }}
  .card.ok {{ border-left:3px solid #6ee7b7; }}
  .card.err {{ border-left:3px solid #fca5a5; }}
  summary {{ cursor:pointer; }}
  .tag {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:.72rem; font-weight:600; background:rgba(110,231,183,.12); color:#6ee7b7; border:1px solid rgba(110,231,183,.3); margin-right:6px; }}
  .tag.fin {{ background:rgba(147,197,253,.12); color:#93c5fd; border-color:rgba(147,197,253,.3); }}
  .meta {{ color:#5a6473; font-size:.8rem; font-family: ui-monospace, Consolas, monospace; margin-left:8px; }}
  .subject {{ color:#fdba74; margin:6px 0; font-style:italic; }}
  pre {{ background:#0e1217; border:1px solid #1c2530; border-radius:6px; padding:10px 12px; overflow-x:auto; font-size:.82rem; color:#c8d3e0; white-space:pre-wrap; }}
  pre.err {{ border-color:rgba(252,165,165,.3); color:#fca5a5; }}
  .file {{ background:#11151a; border:1px solid #1f2731; border-radius:8px; padding:8px 12px; margin:6px 0; }}
  .file .path {{ color:#6ee7b7; font-family: ui-monospace, Consolas, monospace; font-size:.82rem; margin-bottom:4px; }}
  .file .fm {{ font-size:.78rem; margin:0; }}
  .empty {{ color:#5a6473; }}
  .stats {{ display:flex; gap:18px; margin:12px 0; font-size:.88rem; }}
  .stats b {{ color:#fff; }}
  .offsets {{ margin:8px 0; padding:8px 10px; background:#0e1217; border:1px solid #1c2530; border-radius:6px; font-size:.84rem; }}
  .offsets.none {{ color:#5a6473; font-style:italic; }}
  .off-head {{ margin-bottom:6px; color:#8a93a0; }}
  .off-tbl {{ width:100%; border-collapse:collapse; font-size:.8rem; }}
  .off-tbl th, .off-tbl td {{ padding:3px 8px; text-align:left; border-bottom:1px solid #1c2530; }}
  .off-tbl th {{ color:#5a6473; font-weight:600; }}
  .off-tbl td code {{ background:transparent; border:0; padding:0; color:#6ee7b7; }}
  .off-tbl td small {{ color:#5a6473; font-family: ui-monospace, Consolas, monospace; }}
  .pill {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:.7rem; font-weight:600; }}
  .pill.ok {{ background:rgba(110,231,183,.15); color:#6ee7b7; border:1px solid rgba(110,231,183,.3); }}
  .pill.warn {{ background:rgba(252,211,77,.15); color:#fcd34d; border:1px solid rgba(252,211,77,.3); }}
</style></head>
<body>
<h1>kb-sync — run report</h1>
<div class="lede">Start <b>{esc(self.start_iso)}</b> · End <b>{esc(end_iso)}</b> · Duration {duration_s}s</div>
<div class="stats">
  <div>Captures: <b>{len(self.captures)}</b></div>
  <div>Finalizes: <b>{len(self.finalizes)}</b></div>
  <div>Files changed: <b>{len(changed)}</b></div>
  <div>Vault: <b>{esc(vault)}</b></div>
</div>
{warn_html}
<h2>Captures ({len(self.captures)})</h2>
{"".join(cap_html) or "<p class='empty'>No captures.</p>"}

<h2>Finalizes ({len(self.finalizes)})</h2>
{"".join(fin_html) or "<p class='empty'>No finalizes.</p>"}

<h2>Files modified in the vault ({len(changed)})</h2>
{files_html}

</body></html>"""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(doc, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-hours", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="enumerate work without calling claude")
    ap.add_argument("--workspace", help="limit to one workspace by name")
    ap.add_argument("--repo", help="limit to one repo by name (basename)")
    ap.add_argument("--branch", help="limit capture/finalize to one branch by exact name")
    ap.add_argument("--skip-finalize", action="store_true", help="capture only, skip merge detection")
    ap.add_argument("--skip-capture", action="store_true", help="finalize only")
    ap.add_argument("--include-resolved", action="store_true", help="re-process tickets already marked resolved in KB")
    ap.add_argument("--ignore-hwm", action="store_true", help="skip HWM lookup; force bootstrap window (debug/reprocess)")
    args = ap.parse_args()

    cfg = load_config()
    vault = Path(cfg["vault"])
    default_candidates = cfg.get("default_branches", ["master", "main"])
    integration_branches = cfg.get("integration_branches", default_candidates)
    # Resolution set: only a merge into production (master/main) finalizes a ticket.
    # Distinct from integration_branches (attribution; includes dev) on purpose —
    # merging to dev must NOT flip a ticket (esp. experimental) to resolved.
    production_branches = cfg.get("production_branches", ["master", "main"])
    excluded_branches = set(default_candidates) | set(integration_branches)
    max_turns = cfg.get("max_turns", 30)
    backfill_cap = cfg.get("backfill_cap", 5)  # max backfills per run (drains a backlog gradually, never floods)
    capture_cap = cfg.get("capture_cap", 25)   # max normal captures per run — a recovered
    #                                             multi-day outage opens wide windows on many
    #                                             branches; cap so it drains over nights instead
    #                                             of mass-firing (and exhausting the model budget).

    state = load_run_state()
    state = ensure_installed_marker(state)
    save_run_state(state)

    captures, finalizes, errors = 0, 0, 0
    # Per-origin gate for advancing last_examined_at (the date HWM). An origin's date
    # only advances when it was fetched cleanly AND fully examined with zero errors /
    # zero cap-deferrals this run — so a branch left uncaptured (error, outage, cap) is
    # never sealed behind the date skip and is re-examined next run.
    fetched_ok_origins, incomplete_origins = set(), set()
    # A run scoped by any of these flags is partial -> it must NOT advance the date.
    partial_run = bool(args.repo or args.branch or args.since_hours is not None
                       or args.ignore_hwm or args.skip_capture or args.skip_finalize)
    report = RunReport()

    # Team read-side: if the vault is connected to a remote, refresh from it before
    # capturing. Fast-forwards when the local vault is strictly behind; a diverged
    # local (your own un-pushed captures) only gets the fetch and is reconciled via
    # the manager's manual Pull. No-op for a local-only vault. Never pushes.
    if not args.dry_run:
        fetch_vault(vault)

    for ws in cfg["workspaces"]:
        if args.workspace and ws["name"] != args.workspace:
            continue
        wpath = Path(ws["path"])
        print(f"\n=== workspace {ws['name']} -> {wpath} ===")
        if not wpath.exists():
            print(f"  path missing")
            continue

        repos = discover_repos(wpath)
        print(f"  discovered={len(repos)} repos")
        # Refresh ONLY the refs the sync needs: integration (capture's `--not`) plus
        # production (finalize's merge target), by exact refspec. Read-only on the
        # remote; no all-heads wildcard, no --prune.
        fetch_names = list(dict.fromkeys([*integration_branches, *production_branches]))
        ok_count, fetch_failed = 0, []
        for r in repos:
            if not repo_origin(r):
                continue  # local-only repo: nothing to fetch, no staleness risk
            ok, reason = fetch_repo(r, fetch_names)
            o = normalize_origin(repo_origin(r)) or str(r)
            if ok:
                ok_count += 1
                fetched_ok_origins.add(o)
            else:
                # has a remote but no integration ref fetched -> origin/* may be stale.
                fetch_failed.append((r, reason))
                incomplete_origins.add(o)  # stale refs -> examination not trustworthy
        print(f"  fetched {ok_count} repos (integration refs, read-only)")
        report.note_scan(len(repos), ok_count)
        for r, reason in fetch_failed:
            rel = r.relative_to(wpath)
            print(f"    [warn] fetch FAILED ({reason or 'origin unreachable'}): {rel} — capture/finalize using STALE refs")
            report.add_fetch_failure(ws["name"], str(rel), repo_origin(r) or "", reason)

        # Embedding-backed retrieval store: reindex vault (and transcripts of
        # open branches) so capture/finalize can inject top-K snippets inline
        # instead of telling Claude headless to discover-and-read via MCP.
        embed_store = None
        if kb_embed is not None:
            try:
                embed_store = kb_embed.VectorStore()
                kb_embed.reindex_vault(Path(cfg["vault"]), embed_store, verbose=True)
                # collect branches relevant for transcript indexing
                relevant_branches = set()
                for rr in repos:
                    if args.repo and rr.name != args.repo:
                        continue
                    relevant_branches.update(list_candidate_branches(rr, excluded_branches))
                    for t in list_open_tickets(vault, ws["name"], rr.name):
                        if t.get("branch"):
                            relevant_branches.add(t["branch"])
                kb_embed.reindex_transcripts(relevant_branches, STATE_DIR, PROJECTS_DIR,
                                              embed_store, verbose=True)
                embed_store.save()
            except kb_embed.EmbeddingsUnavailable as e:
                print(f"  [embed] disabled: {e} — falling back to legacy prompt path")
                embed_store = None
            except Exception as e:
                print(f"  [embed] reindex failed: {type(e).__name__}: {e}")
                embed_store = None

        if not args.skip_capture:
            candidates = []
            for r in repos:
                if args.repo and r.name != args.repo:
                    continue
                email = user_email(r)
                int_refs = resolved_integration_refs(r, integration_branches)
                origin_norm = normalize_origin(repo_origin(r)) or str(r)
                for branch in list_candidate_branches(r, excluded_branches):
                    if args.branch and branch != args.branch:
                        continue
                    # Cheap skip: nothing new since this repo was last examined
                    # (tip == SHA-HWM, or tip's commit date < the repo's date HWM).
                    # Collapses the full local-branch walk to the few that moved.
                    # Skipped only on the normal path (overrides force a re-walk).
                    if (args.since_hours is None and not args.ignore_hwm
                            and branch_skippable(state, origin_norm, branch, r)):
                        continue
                    tipo, slug, folder_rel = parse_branch(branch)
                    if args.since_hours is not None:
                        since_spec = {"kind": "date", "hours": args.since_hours, "source": "cli-override"}
                    elif args.ignore_hwm:
                        today = datetime.now().strftime("%Y-%m-%d")
                        cap_date = (datetime.now() - timedelta(days=HWM_CAP_DAYS)).strftime("%Y-%m-%d")
                        installed = state.get("installed_at", today)
                        bootstrap_date = max(installed, cap_date)
                        src = "cap-7d-truncated" if cap_date > installed else "bootstrap"
                        since_spec = {"kind": "date", "iso": bootstrap_date, "source": src}
                    else:
                        since_spec = effective_since(state, origin_norm, branch, r)
                    commits = author_commits_for_branch(r, branch, email, since_spec, int_refs)
                    backfill, mine_base = False, None
                    if not commits:
                        # Backfill the capture<->finalize crack: a branch merged within
                        # one sync interval has zero own-commits here (already integration-
                        # reachable) and no ticket, so it would slip past both passes.
                        bf = merged_ticketless_backfill(r, branch, email, since_spec, int_refs,
                                                        vault, ws["name"], r.name)
                        if not bf:
                            print(f"  [skip] {r.relative_to(wpath)}:{branch} — no new authored commits to capture "
                                  f"(window={since_spec['source']}, excl. merges + {', '.join(int_refs) or 'no integration ref'})")
                            continue
                        commits, mine_base = bf
                        backfill = True
                    base = nearest_integration_base(r, branch, int_refs) or resolve_default_branch(r, default_candidates)
                    tr = run_git(["log", branch, "-1", "--pretty=format:%at"], r)
                    try:
                        ts = int(tr.stdout.strip())
                    except ValueError:
                        ts = 0
                    candidates.append((ts, origin_norm, branch, r, tipo, slug, folder_rel, commits, email, since_spec, base, backfill, mine_base))

            candidates.sort(reverse=True)
            exp_branches = manually_experimental_branches()
            seen = set()
            bf_done, cap_done = 0, 0  # backfills / normal captures executed this run (cap guards)
            for ts, origin_norm, branch, repo, tipo, slug, folder_rel, commits, email, since_spec, base, backfill, mine_base in candidates:
                key = (origin_norm, branch)
                if key in seen:
                    print(f"  [dup-skip] {repo.relative_to(wpath)}:{branch} — already processed in fresher clone")
                    continue
                seen.add(key)
                # Cap backfills per run so a one-time backlog drains gradually (a few/night)
                # instead of flooding (and exhausting the model budget). A deferred item
                # leaves its origin incomplete so the date HWM won't seal past it.
                if backfill:
                    if bf_done >= backfill_cap:
                        print(f"  [backfill-deferred] {repo.relative_to(wpath)}:{branch} — over cap "
                              f"({backfill_cap}/run); will retry next run")
                        incomplete_origins.add(origin_norm)
                        continue
                    bf_done += 1
                if not args.include_resolved:
                    st = ticket_status_for_branch(vault, ws["name"], repo.name, branch)
                    if st in ("resolved", "experimental"):
                        why = "already resolved" if st == "resolved" else "experimental (paused until prod merge or manual in-progress)"
                        print(f"  [skip] {repo.relative_to(wpath)}:{branch} — KB {why}")
                        continue
                    # Skip capture when an existing open ticket's branch is already
                    # merged — finalize will resolve it from accumulated state. Saves
                    # an expensive Claude call. Only applies when the folder already
                    # exists (st is not None); new branches still capture to create
                    # the initial record.
                    if st is not None:
                        mres = detect_merge(repo, integration_branches, branch)
                        if mres:
                            print(f"  [skip-merged] {repo.relative_to(wpath)}:{branch} — "
                                  f"already {mres['method']} into {mres['integration']}; finalize handles")
                            continue
                if not base:
                    print(f"  [skip] {repo.relative_to(wpath)}:{branch} — no integration base / default branch")
                    continue
                # Cap normal captures too: a recovered multi-day outage opens wide
                # windows on many branches; drain over nights rather than mass-fire.
                # Deferred -> origin incomplete so the date HWM won't seal past it.
                if not backfill:
                    if cap_done >= capture_cap:
                        print(f"  [capture-deferred] {repo.relative_to(wpath)}:{branch} — over cap "
                              f"({capture_cap}/run); will retry next run")
                        incomplete_origins.add(origin_norm)
                        continue
                    cap_done += 1
                # Backfill mines commit-anchored (range=branch) because the branch is
                # already merged — a ref-anchored base...branch diff would be empty.
                src_label = "backfill-merged" if backfill else since_spec["source"]
                kind = "backfill" if backfill else "capture"
                print(f"  [{kind}] {repo.relative_to(wpath)}:{branch} — {len(commits)} commits vs {base} (window: {src_label})")
                if backfill:
                    stat = author_landed_stat(repo, branch, email, since_spec, base=mine_base)
                    diff = author_landed_diff(repo, branch, email, since_spec, base=mine_base)
                else:
                    stat = diff_stat(repo, branch, base)
                    diff = diff_full(repo, branch, base)
                offsets = load_session_offsets()
                hints = session_hints(branch, offsets)
                if hints:
                    print(f"    session hints: {len(hints)} session(s) with new content")
                topk_l, topk_t = retrieve_for_branch(embed_store, branch, repo.name, commits, stat)
                pre_extracted = build_pre_extracted_block(topk_l, topk_t, Path(cfg["vault"])) if (topk_l or topk_t) else ""
                if pre_extracted:
                    print(f"    [embed] injected: {len(topk_l)} learnings + {len(topk_t)} transcript turns")
                prompt = capture_prompt(cfg, ws["name"], repo, branch, tipo, slug, folder_rel, base, commits, stat, diff, hints, pre_extracted=pre_extracted)
                rc, out, err = claude_run(prompt, max_turns, args.dry_run)
                captures += 1
                bumped = rc == 0 and not args.dry_run
                report.add_capture(ws["name"], repo.name, str(repo.relative_to(wpath)), branch,
                                   slug, slug, len(commits), hints, bumped, rc, out, err, src_label)
                if rc != 0:
                    errors += 1
                    incomplete_origins.add(origin_norm)  # uncaptured -> don't seal the date
                    print(f"    claude rc={rc}")
                    if err.strip():
                        print(f"    stderr: {err.strip()[:500]}")
                elif not args.dry_run:
                    bump_session_offsets(hints)
                    if bump_hwm(state, origin_norm, branch, repo):
                        save_run_state(state)
                    if branch in exp_branches:
                        if set_index_status(vault, f"{ws['name']}/{repo.name}/{folder_rel}", "experimental"):
                            print(f"    [experimental] status=experimental forced on {folder_rel}/_index.md")
                        clear_mark_experimental(branch)
                if out.strip():
                    for line in out.splitlines()[:60]:
                        print(f"    {line}")

        if not args.skip_finalize:
            done_branches = manually_done_branches()
            # Group clones by origin so a ticket's branch is judged across ALL its
            # clones, not just the first one encountered. A branch absent from one
            # clone may still be alive (unmerged) in another.
            clones_by_origin = {}
            for r in repos:
                if args.repo and r.name != args.repo:
                    continue
                o = normalize_origin(repo_origin(r)) or str(r)
                clones_by_origin.setdefault(o, []).append(r)

            seen_f = set()
            for origin_norm, clones in clones_by_origin.items():
                proj = clones[0].name
                for t in list_open_tickets(vault, ws["name"], proj):
                    tbranch = t.get("branch") or ""
                    if not tbranch:
                        continue
                    if args.branch and tbranch != args.branch:
                        continue
                    key = (origin_norm, tbranch)
                    if key in seen_f:
                        continue
                    seen_f.add(key)

                    merged = None
                    alive = False
                    ctx_repo = clones[0]
                    for r in clones:
                        if resolve_ref(r, tbranch) is None:
                            continue
                        alive = True
                        ctx_repo = r
                        mr = detect_merge(r, production_branches, tbranch)
                        if mr:
                            merged = mr
                            break

                    if merged:
                        res = merged
                        desc = f"merged into {res['integration']} ({res['method']})"
                        landed_diff = diff_two_dot(ctx_repo, res["merge_base"], res["ref"])
                    elif alive:
                        # branch still exists in some clone and is not merged -> still open
                        continue
                    elif tbranch in done_branches:
                        res = {"status": "manual", "integration": "", "method": "manual",
                               "ref": None, "merge_base": None,
                               "landed_date": datetime.now().strftime("%Y-%m-%d")}
                        desc = "manual /kb-mark --done"
                        landed_diff = ""
                    else:
                        res = {"status": "gone", "integration": "", "method": "gone",
                               "ref": None, "merge_base": None,
                               "landed_date": datetime.now().strftime("%Y-%m-%d")}
                        desc = "branch gone (deleted on merge / removed everywhere)"
                        landed_diff = ""
                    r = ctx_repo
                    print(f"  [finalize] {r.relative_to(wpath)}:{tbranch} — {desc}")
                    offsets = load_session_offsets()
                    hints = session_hints(tbranch, offsets)
                    if hints:
                        print(f"    session hints: {len(hints)} session(s) with new content")
                    # synthesize a "fake commits" list from the resolution context so the
                    # retrieval query carries some lexical content for finalize too
                    f_commits = [f"\t\t{desc} on {res.get('integration','')}"]
                    topk_l, topk_t = retrieve_for_branch(embed_store, tbranch, r.name, f_commits, landed_diff[:1500])
                    pre_extracted = build_pre_extracted_block(topk_l, topk_t, Path(cfg["vault"])) if (topk_l or topk_t) else ""
                    if pre_extracted:
                        print(f"    [embed] injected: {len(topk_l)} learnings + {len(topk_t)} transcript turns")
                    prompt = finalize_prompt(cfg, ws["name"], r, t, res, landed_diff, hints, pre_extracted=pre_extracted)
                    rc, out, err = claude_run(prompt, max_turns, args.dry_run)
                    finalizes += 1
                    bumped = rc == 0 and not args.dry_run
                    merge_compat = {"branch": res.get("integration", ""), "hash": "",
                                    "subject": desc, "date": res["landed_date"]}
                    report.add_finalize(ws["name"], r.name, str(r.relative_to(wpath)), t, merge_compat,
                                        hints, bumped, rc, out, err)
                    if bumped:
                        bump_session_offsets(hints)
                        clear_manual_done(tbranch)
                    if rc != 0:
                        errors += 1
                        incomplete_origins.add(origin_norm)  # unfinalized -> don't seal the date
                        print(f"    claude rc={rc}")
                    if out.strip():
                        for line in out.splitlines()[:40]:
                            print(f"    {line}")

    print(f"\n=== done. captures={captures} finalizes={finalizes} errors={errors} dry_run={args.dry_run} ===")

    # Advance the date HWM (last_examined_at) for origins examined cleanly THIS run:
    # fetched OK and with zero capture/finalize errors and zero cap-deferrals. Skipped
    # entirely on a partial run (any scoping flag) or a dry run. Next run's date skip +
    # window floor then move forward only for repos we fully and successfully examined.
    if not args.dry_run and not partial_run:
        today = datetime.now().strftime("%Y-%m-%d")
        sealed = advance_examined_dates(state, fetched_ok_origins, incomplete_origins, today)
        if sealed:
            save_run_state(state)
            print(f"  last_examined_at -> {today} for {len(sealed)} origin(s); "
                  f"{len(incomplete_origins)} held back (errors/deferrals/stale fetch)")

    # Flag duplicate learnings touched this run (same-run twins the capture audit
    # can't see, or existing siblings retrieval missed). Detect + report only — the
    # merge decision is the human's (auto-merge would be a blind, lossy vault write).
    # Lands in the sync-history record below and the manager's sync-health strip.
    if not args.dry_run:
        report.duplicates = dedup_scan(report, vault)
        if report.duplicates:
            twins = sum(1 for d in report.duplicates if d.get("kind") == "twin")
            print(f"\n  [dedup] {len(report.duplicates)} possible duplicate learning pair(s) "
                  f"({twins} same-run twin) — review:")
            for d in report.duplicates:
                tag = "TWIN  " if d.get("kind") == "twin" else "review"
                print(f"    {tag} {d['score']:.3f}  {d['a']}  <->  {d['b']}")

    # Record this run for the manager's sync-history view (real runs only).
    if not args.dry_run:
        append_sync_history(report.to_record(vault, args.dry_run))

    # Notify the embed daemon (if running) that its on-disk store changed so
    # the next interactive UserPromptSubmit retrieval sees the new learnings
    # without having to wait for a daemon restart.
    if kb_embed is not None and not args.dry_run:
        try:
            resp = kb_embed.daemon_request({"op": "reindex"}, timeout=3.0)
            if resp and resp.get("ok"):
                print(f"daemon reindex notified: chunks={resp.get('chunks','?')}")
        except Exception as e:
            print(f"daemon reindex notify failed (non-fatal): {type(e).__name__}: {e}")

    if report.has_activity() and not args.dry_run:
        report_path = Path.home() / ".claude" / "logs" / f"kb-sync-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
        report.write_html(vault, report_path)
        print(f"\nreport: {report_path}")

    # Version the vault locally (commit-only-if-changes; never pushes). The KB is
    # a local-only repo by design — the vault is isolated from every remote.
    # This is what makes the scheduled run also snapshot history.
    if not args.dry_run:
        commit_vault(vault)


if __name__ == "__main__":
    main()
