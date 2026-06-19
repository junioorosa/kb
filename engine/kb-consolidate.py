#!/usr/bin/env python3
"""kb-consolidate.py — non-destructive vault consolidation (the "dreams" pass).

Capture/finalize only ever audit learnings against the diff in front of them, so
the vault accumulates the things a per-diff pass can't see: near-duplicate
learnings from sibling tickets, contradictions between scopes, and notes that
quietly went stale when the code moved. This is the periodic cleanup pass.

Cardinal safety — the vault is the sensitive point ("a bad write poisons future
retrieval"), so consolidation is built to be impossible to trust by accident:

  * NON-DESTRUCTIVE. It never touches the live vault branch. It cuts a
    `consolidation/<workspace>-<ts>` branch, writes every change there, and stops.
    You review the diff (git/Obsidian, or the manager later) and merge or delete
    the branch. The working state is the human's to accept — never auto-merged.
  * CONSERVATIVE by default. Merge near-exact duplicates and resolve
    contradictions (the value that LANDED in production wins, with a
    `## Consolidation history` trace); scope PROMOTIONS (ticket -> project ->
    workspace) are only SUGGESTED in the report, never applied — a smaller diff
    that's easy to review.
  * DETERMINISTIC keys. Every merge/rewrite references its source learnings by
    exact vault path. No "most recent / best guess" — the rule the whole engine
    lives by.
  * BUDGETED. A workspace can hold hundreds of learnings; consolidating it in one
    LLM call would blow context and budget (the backfill incident is the scar).
    So it's map-reduce: one bounded pass per project (the map), then a small
    reduce over project-level summaries to catch cross-project duplicates — never
    a single monster call. A per-run project cap drains a large workspace
    gradually; deferred projects run next time.

Repo-signal freshness: learnings cite concrete code symbols (a class, a method).
Before the map pass, each learning's most distinctive symbols are grepped against
the project's PRODUCTION branch (origin/master|main, preferring the remote-tracking
ref), NOT the working tree — staleness is only meaningful against the landed code,
not whatever feature branch is checked out. "symbol no longer in production" is fed
to the model as a staleness HINT (never an automatic delete — conservative). This is
the cheap, deterministic "is this still true about the code" check; a full semantic
re-verification against live code is deliberately out of scope.

CLI:
  kb consolidate --workspace Pauta --dry-run   # plan + token estimate, no writes, no LLM
  kb consolidate --workspace Pauta             # cut the branch and consolidate
  kb consolidate --workspace Pauta --cap 4     # at most 4 projects this run
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import kb_config
except Exception:  # pragma: no cover - kb_config is always a sibling in deploy
    kb_config = None

DEFAULT_CAP = 6          # projects consolidated per run (rest deferred)
DEFAULT_MAX_TURNS = 40
PER_CALL_TOKEN_CEILING = 60000   # soft ceiling per map call; a project over it is flagged to split


# --- token estimate ----------------------------------------------------------

def estimate_tokens(text: str) -> int:
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return max(1, len(text.encode("utf-8")) // 4)


# --- git + headless claude (kept local so consolidation is decoupled from kb-sync) ---

def run_git(args, cwd, timeout=30):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-c", "credential.interactive=false", *args],
                          cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def claude_run(prompt: str, max_turns: int, dry_run: bool):
    if dry_run:
        print(f"  [DRY] would invoke claude --print (--max-turns={max_turns}, prompt={len(prompt)} chars, ~{estimate_tokens(prompt)} tok)")
        return 0, "", ""
    cmd = ["claude", "--print", "--max-turns", str(max_turns), "--dangerously-skip-permissions"]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        partial = e.stdout
        if isinstance(partial, (bytes, bytearray)):
            partial = partial.decode("utf-8", errors="replace")
        return 124, (partial or ""), "TimeoutExpired"
    except Exception as e:
        return 1, "", f"claude_run exception: {type(e).__name__}: {e}"


# --- scope gather ------------------------------------------------------------

def _is_learning(rel: str) -> bool:
    return "/Learnings/" in rel and not rel.endswith("/_index.md") and rel.endswith(".md")


def gather_workspace(vault: Path, workspace: str) -> list[dict]:
    """Project buckets under one workspace: [{project, learnings:[rel...],
    indexes:[rel...]}]. A learning is any */Learnings/*.md; the project is the
    second path segment. Projects are returned sorted, each learning list sorted —
    deterministic so a dry-run plan and the real run agree."""
    ws_root = vault / workspace
    projects: dict[str, dict] = {}
    if not ws_root.is_dir():
        return []
    for md in ws_root.rglob("*.md"):
        if ".obsidian" in md.parts:
            continue
        rel = str(md.relative_to(vault)).replace("\\", "/")
        parts = rel.split("/")
        if len(parts) < 3 or parts[1] == "Learnings":
            continue  # workspace-scope learning (<ws>/Learnings/x.md) — not a project bucket
        project = parts[1]
        bucket = projects.setdefault(project, {"project": project, "learnings": [], "indexes": []})
        if _is_learning(rel):
            bucket["learnings"].append(rel)
        elif rel.endswith("/_index.md"):
            bucket["indexes"].append(rel)
    out = []
    for name in sorted(projects):
        b = projects[name]
        if not b["learnings"]:
            continue  # nothing to consolidate in this project
        b["learnings"].sort()
        b["indexes"].sort()
        out.append(b)
    return out


# --- repo-signal (freshness probe) -------------------------------------------

_SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`")
_CAMEL_RE = re.compile(r"\b([A-Z][a-z0-9]+[A-Z][A-Za-z0-9]{3,})\b")


def distinctive_symbols(text: str, limit: int = 6) -> list[str]:
    """Code symbols worth probing: backticked identifiers + CamelCase tokens.
    Longest-first, de-duplicated, dotted paths reduced to their last segment too."""
    found: list[str] = []
    for m in _SYMBOL_RE.finditer(text):
        sym = m.group(1)
        tail = sym.split(".")[-1]
        for s in (sym, tail):
            if len(s) >= 6 and s not in found:
                found.append(s)
    for m in _CAMEL_RE.finditer(text):
        s = m.group(1)
        if s not in found:
            found.append(s)
    found.sort(key=len, reverse=True)
    return found[:limit]


_REPO_CACHE: dict = {}


def _discover_repos(workspace_path: Path, max_depth: int = 4) -> list:
    """Every git repo under the workspace (a dir containing .git), depth-limited,
    NOT descending into a found repo. Mirrors kb-sync's discovery so a project's
    vault folder name (== repo dir basename) maps back to the same repo, even when
    the repo is nested (e.g. <ws>/<group>/<project>). Memoized per workspace."""
    key = str(workspace_path)
    if key in _REPO_CACHE:
        return _REPO_CACHE[key]
    repos: list = []

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
            if sub.name in (".git", "node_modules", "target", ".venv", "__pycache__", ".obsidian"):
                continue
            if (sub / ".git").exists():
                repos.append(sub)
                continue
            walk(sub, depth + 1)

    walk(workspace_path, 1)
    _REPO_CACHE[key] = repos
    return repos


def _repo_origin(repo: Path) -> str:
    """Normalized origin URL of a repo, or "" (local-only / unreadable)."""
    try:
        r = run_git(["config", "--get", "remote.origin.url"], repo, timeout=10)
    except Exception:
        return ""
    u = (r.stdout or "").strip().rstrip("/")
    return u[:-4] if u.endswith(".git") else u


def locate_repo(workspace_path: Path, project: str) -> Path | None:
    """The git repo whose dir basename == project, found anywhere under the
    workspace. DETERMINISTIC, no guessing — the nested-repo lesson: a wrong repo
    would feed false staleness hints and poison the consolidation.

      * a direct child <ws>/<project>/.git wins (fast, unambiguous);
      * otherwise EXACT-basename matches in the depth-limited walk;
      * several matches that are CLONES OF THE SAME ORIGIN -> any one is fine
        (same code, same grep answer) -> return it;
      * matches spanning DIFFERENT origins (or local-only repos we can't tell
        apart) -> ambiguous -> None (refuse to guess);
      * no match -> None. Either way the probe just runs without hints.
    """
    direct = workspace_path / project
    if (direct / ".git").exists():
        return direct
    matches = [r for r in _discover_repos(workspace_path) if r.name == project]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    by_origin: dict = {}
    for r in matches:
        by_origin.setdefault(_repo_origin(r), r)
    if len(by_origin) == 1 and "" not in by_origin:
        return next(iter(by_origin.values()))   # clones of one origin == one codebase
    print(f"  [freshness] '{project}': {len(matches)} dirs share that name across "
          f"{len(by_origin)} origin(s) — ambiguous, skipping repo probe (no guess).")
    return None


def production_ref(repo: Path, production_branches: list[str]) -> str | None:
    """The tree to grep for the freshness probe: the project's PRODUCTION branch,
    preferring the remote-tracking ref (`origin/<b>` — the shared 'what's actually
    in production' truth) over the local branch (which may be behind, or be whatever
    arbitrary feature branch happens to be checked out). First that resolves wins;
    None if none do. Staleness must be judged against production, never the working
    tree — a symbol absent from a feature checkout (or present only on an unmerged
    branch) says nothing about whether the landed code still has it."""
    for b in (production_branches or []):
        for ref in (f"origin/{b}", b):
            r = run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repo, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return ref
    return None


def freshness_hints(vault: Path, repo: Path | None, learnings: list[str],
                    production_branches: list[str]) -> dict[str, list[str]]:
    """{learning_rel: [symbols not found in the project's PRODUCTION branch]}. Empty
    when the repo can't be located OR no production branch resolves (graceful: no
    hints, never a false 'stale' off an arbitrary working tree)."""
    hints: dict[str, list[str]] = {}
    if repo is None or not (repo / ".git").exists():
        return hints
    ref = production_ref(repo, production_branches)
    if ref is None:
        # No production branch to validate against — do NOT grep the working tree;
        # a feature checkout would yield misleading staleness. No signal beats a
        # wrong one.
        return hints
    for rel in learnings:
        try:
            text = (vault / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        missing = []
        for sym in distinctive_symbols(text):
            # grep the production TREE (ref), not the checkout. -e guards a symbol
            # that could start with '-'. 0 = found, 1 = not found, >=2 = error (ignore).
            r = run_git(["grep", "-F", "-q", "-e", sym, ref], cwd=repo, timeout=20)
            if r.returncode == 1:
                missing.append(sym)
        if missing:
            hints[rel] = missing
    return hints


# --- prompts (the policy lives here; pinned by kb_consolidate_test) -----------

def map_prompt(vault: Path, workspace: str, project: str, learnings: list[str],
               indexes: list[str], hints: dict[str, list[str]]) -> str:
    hint_lines = []
    for rel, syms in hints.items():
        hint_lines.append(f"- `{rel}` — symbols not found in current code: {', '.join(syms)}")
    hint_block = ("\nFreshness probe (symbols grepped against the project's PRODUCTION "
                  "branch (origin/master|main), not the working tree; treat as a "
                  "STALENESS HINT, never an automatic delete):\n"
                  + "\n".join(hint_lines) + "\n") if hint_lines else ""
    learn_block = "\n".join(f"- {rel}" for rel in learnings)
    idx_block = "\n".join(f"- {rel}" for rel in indexes) or "- (none)"
    return f"""You are running headless in KB consolidation mode. Do not ask — execute.

Vault root (absolute): `{vault}`
**Use the standard Read/Write/Edit/Glob tools — do NOT use any MCP server.** Every path below is RELATIVE to the vault root; prefix it with the root. You are on a throwaway `consolidation/*` git branch — the live vault is untouched, so edit freely, but stay CONSERVATIVE per the rules.

Workspace: `{workspace}/`   Project: `{workspace}/{project}/`

Learnings in scope (project + its tickets):
{learn_block}

Governing _index.md files (for status + ticket context):
{idx_block}
{hint_block}
Task — consolidate ONLY this project's learnings:
1. Read the learnings above (Read/Glob). Audit them AGAINST EACH OTHER.
2. **Merge near-duplicates.** Two learnings stating the same delta -> keep ONE
   (the clearest), fold in anything unique from the other, and DELETE the
   redundant file. Reference both source paths in a `## Consolidation history`
   line on the survivor (`merged <path-a> + <path-b> on <YYYY-MM-DD>`).
3. **Resolve contradictions.** When two learnings disagree, the value that
   LANDED in production wins — a learning whose `_index.md` is `status: resolved`
   outranks one that is `open`/`experimental`. Correct the loser (or delete it if
   fully subsumed), and add a `## Consolidation history` line citing the winner.
4. **Staleness.** For a learning flagged by the freshness probe, VERIFY by reading
   it: if it describes code that is clearly gone, mark it — add a
   `## Consolidation history` line `flagged stale (<symbol> absent) <YYYY-MM-DD>`
   and, if it is now actively misleading, delete it. In doubt, KEEP and flag —
   never delete on the probe alone.
5. **Scope promotion — SUGGEST ONLY, do not apply.** If a pattern recurs across
   several tickets and deserves to live at project/workspace scope, do NOT move or
   create it. Instead list it under a `### Promotion suggestions` heading in your
   final text report, as `- <insight> (from <paths>) -> <project|workspace>`.

Hard rules:
- DETERMINISTIC: reference every source by its exact vault path. Never guess.
- One insight = one file. Prefer editing/merging over creating near-duplicates.
- Every deletion or rewrite leaves a `## Consolidation history` trace naming its
  sources — a reviewer must be able to see what happened and why.
- Touch ONLY files under `{workspace}/{project}/`. Cross-project work is a later
  pass. Do not edit any `_index.md` status.
- YAML rules: long fields use literal `|-`; tags as `[a, b, c]`. English keys.

Report (plain text, brief): merged (survivor <- sources), contradictions resolved,
stale flagged/removed, and the `### Promotion suggestions` list.
"""


def reduce_prompt(vault: Path, workspace: str, project_summaries: list[dict]) -> str:
    """Cross-project pass over SUMMARIES only (cheap): catch duplicates that live
    in different projects of the same workspace — the reason workspace scope exists.
    Operates on learning headers, not full bodies, to stay small."""
    blocks = []
    for s in project_summaries:
        lines = [f"### {s['project']}"]
        for rel, desc in s.get("headers", []):
            lines.append(f"- `{rel}` — {desc}")
        blocks.append("\n".join(lines))
    catalog = "\n\n".join(blocks)
    return f"""You are running headless in KB consolidation mode (cross-project reduce). Do not ask — execute.

Vault root (absolute): `{vault}`
**Use the standard Read/Write/Edit/Glob tools — do NOT use any MCP server.** Paths are relative to the vault root. You are on the same throwaway `consolidation/*` branch.

Workspace: `{workspace}/`. Below is a catalog of every project's learnings
(path + one-line description only — NOT the full bodies):

{catalog}

Task — cross-project duplicates ONLY:
1. Scan the catalog for learnings in DIFFERENT projects that state the same
   general delta (e.g. the same idempotency rule recorded separately under two
   services).
2. For each genuine cross-project duplicate cluster, Read just those files to
   confirm, then keep ONE at WORKSPACE scope (`{workspace}/Learnings/<name>.md`),
   fold in unique nuances, and DELETE the per-project copies — each with a
   `## Consolidation history` line naming every source path.
3. If a cluster is only superficially similar (same area, different fact), LEAVE
   it — do not over-merge. When in doubt, do nothing.

Hard rules:
- DETERMINISTIC: exact vault paths, never guessed. Confirm by reading before any merge.
- Only act on TRUE cross-project duplicates. Single-project cleanup already ran.
- Every change leaves a `## Consolidation history` trace.

Report (plain text, brief): each workspace-scope survivor and the per-project
sources it absorbed; "no cross-project duplicates" if none.
"""


def project_headers(vault: Path, learnings: list[str], limit: int = 200) -> list[tuple]:
    """(rel, one-line description) for the reduce catalog — frontmatter description
    or first non-heading line, truncated. Cheap; no LLM."""
    out = []
    for rel in learnings[:limit]:
        desc = ""
        try:
            text = (vault / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        m = re.search(r"^description:\s*(.+)$", text, re.M)
        if m:
            desc = m.group(1).strip().strip('"').strip("'")
        if not desc:
            body = text.split("\n---", 1)[-1] if text.startswith("---") else text
            for line in body.splitlines():
                ls = line.strip()
                if ls and not ls.startswith("#") and not ls.startswith("---"):
                    desc = ls
                    break
        out.append((rel, desc[:160]))
    return out


# --- orchestration -----------------------------------------------------------

def _vault_is_clean(vault: Path) -> bool:
    return not run_git(["status", "--porcelain"], cwd=vault, timeout=30).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Non-destructive vault consolidation pass.")
    ap.add_argument("--workspace", help="workspace to consolidate (default: the only one, or fail if many)")
    ap.add_argument("--project", help="consolidate only this project (by name); a targeted, smallest-blast run")
    ap.add_argument("--dry-run", action="store_true", help="print the plan + token estimate; no branch, no LLM, no writes")
    ap.add_argument("--cap", type=int, default=None, help=f"max projects this run (default config consolidate_cap or {DEFAULT_CAP})")
    ap.add_argument("--max-turns", type=int, default=None, help=f"claude --max-turns per pass (default {DEFAULT_MAX_TURNS})")
    args = ap.parse_args()

    if kb_config is None:
        print("ERROR: kb_config not importable; cannot resolve the vault.", file=sys.stderr)
        return 2
    try:
        vault = Path(kb_config.resolve_vault(strict=True))
    except Exception as e:
        print(f"ERROR: vault unresolved ({e}). Configure it first.", file=sys.stderr)
        return 2
    cfg = kb_config.load_config()
    workspaces = cfg.get("workspaces", []) or []
    prod_branches = cfg.get("production_branches") or ["master", "main"]
    cap = args.cap if args.cap is not None else int(cfg.get("consolidate_cap", DEFAULT_CAP))
    max_turns = args.max_turns if args.max_turns is not None else int(cfg.get("max_turns", DEFAULT_MAX_TURNS))

    ws_name = args.workspace
    if not ws_name:
        if len(workspaces) == 1:
            ws_name = workspaces[0].get("name")
        else:
            print(f"ERROR: multiple workspaces; pass --workspace (one of: {[w.get('name') for w in workspaces]})", file=sys.stderr)
            return 2
    ws_path = None
    for w in workspaces:
        if w.get("name") == ws_name:
            ws_path = Path(w.get("path", ""))
    if ws_path is None:
        print(f"ERROR: workspace '{ws_name}' not in config.", file=sys.stderr)
        return 2

    projects = gather_workspace(vault, ws_name)
    if not projects:
        print(f"Nothing to consolidate under workspace '{ws_name}'.")
        return 0
    if args.project:
        projects = [p for p in projects if p["project"] == args.project]
        if not projects:
            print(f"ERROR: project '{args.project}' has no learnings under workspace '{ws_name}'.", file=sys.stderr)
            return 2

    # --- plan + token estimate ----------------------------------------------
    print(f"KB consolidation — workspace '{ws_name}'  ({len(projects)} project(s) with learnings)")
    planned = projects[:cap]
    deferred = projects[cap:]
    total_tok = 0
    plans = []
    for b in planned:
        repo = locate_repo(ws_path, b["project"])
        # Freshness is computed for the plan too — the dry-run's stale-hint counts
        # are exactly the signal you want before deciding to spend a real run.
        hints = freshness_hints(vault, repo, b["learnings"], prod_branches)
        prompt = map_prompt(vault, ws_name, b["project"], b["learnings"], b["indexes"], hints)
        # Approximate the real cost: the prompt lists files; the model also reads them.
        bodies = 0
        for rel in b["learnings"]:
            try:
                bodies += estimate_tokens((vault / rel).read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
        tok = estimate_tokens(prompt) + bodies
        total_tok += tok
        over = " [OVER CEILING — consider a smaller --cap or split]" if tok > PER_CALL_TOKEN_CEILING else ""
        plans.append((b, repo, hints, prompt, tok))
        print(f"  • {b['project']}: {len(b['learnings'])} learnings, "
              f"repo={'yes' if repo else 'NOT FOUND'}, stale-hints={sum(len(v) for v in hints.values())}, ~{tok} tok{over}")
    if deferred:
        print(f"  deferred this run (cap={cap}): {', '.join(b['project'] for b in deferred)}")
    print(f"  reduce pass (cross-project): ~{estimate_tokens(reduce_prompt(vault, ws_name, [{'project': b['project'], 'headers': project_headers(vault, b['learnings'])} for b in planned]))} tok")
    print(f"  estimated total: ~{total_tok} tok (map) + reduce  [excludes model output]")

    if args.dry_run:
        print("\nDry run — no branch created, no LLM invoked, nothing written.")
        return 0

    # --- cut the consolidation branch (non-destructive) ----------------------
    if not (vault / ".git").exists():
        print("ERROR: vault is not a git repo; consolidation needs the branch mechanism.", file=sys.stderr)
        return 2
    if not _vault_is_clean(vault):
        print("ERROR: vault working tree is dirty; commit or stash first (consolidation must branch from a clean state).", file=sys.stderr)
        return 2
    base = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=vault, timeout=15).stdout.strip() or "main"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"consolidation/{ws_name}-{ts}"
    cr = run_git(["checkout", "-b", branch], cwd=vault, timeout=30)
    if cr.returncode != 0:
        print(f"ERROR: could not create branch {branch}: {cr.stderr.strip()}", file=sys.stderr)
        return 2
    print(f"\nConsolidating on branch {branch} (base {base}) — live vault untouched.")

    summaries = []
    errors = 0
    try:
        for b, repo, hints, prompt, _tok in plans:
            print(f"\n[map] {b['project']} ...")
            rc, out, err = claude_run(prompt, max_turns, dry_run=False)
            if rc != 0:
                errors += 1
                print(f"  map FAILED (rc={rc}): {err.strip()[:200]}")
            else:
                print("  " + (out.strip()[:400] or "(no report)"))
            summaries.append({"project": b["project"], "headers": project_headers(vault, b["learnings"])})

        print("\n[reduce] cross-project duplicates ...")
        rc, out, err = claude_run(reduce_prompt(vault, ws_name, summaries), max_turns, dry_run=False)
        if rc != 0:
            errors += 1
            print(f"  reduce FAILED (rc={rc}): {err.strip()[:200]}")
        else:
            print("  " + (out.strip()[:400] or "(no report)"))

        # commit whatever the passes wrote, on the branch only
        run_git(["add", "-A"], cwd=vault, timeout=30)
        if run_git(["status", "--porcelain"], cwd=vault, timeout=30).stdout.strip():
            run_git(["commit", "-m", f"kb-consolidate: {ws_name} {ts}"], cwd=vault, timeout=30)
            print(f"\nCommitted to {branch}.")
        else:
            print("\nNo changes produced — nothing to review.")
    finally:
        # Always hand the tree back on the base branch: the consolidation work is
        # isolated on its branch, the live vault is exactly as it was.
        run_git(["checkout", base], cwd=vault, timeout=30)

    print(f"\nReview:  git -C \"{vault}\" diff {base}...{branch}")
    print(f"Accept:  git -C \"{vault}\" merge --no-ff {branch}")
    print(f"Discard: git -C \"{vault}\" branch -D {branch}")
    if errors:
        print(f"\n{errors} pass(es) errored — review the branch carefully before merging.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
