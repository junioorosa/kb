#!/usr/bin/env python3
"""KB installer/updater — OS-agnostic orchestrator (topology A: repo -> host).

One command for first-install AND "update certinho":

    python installer/install.py            # dry-run: show exactly what would change
    python installer/install.py --apply    # do it (backs up everything it touches)
    python installer/install.py --rollback # restore the most recent deploy backup
    python installer/install.py --status   # what's installed (version, scheduler)

Steps (each idempotent, each safe to re-run):
  1. migrate     pre-0.11 ~/.claude data -> ~/.kb (config/state/stamps; retires
                 the old deployed engine into a backup)
  2. deploy      engine into <kb home>/engine + slash commands into ~/.claude
  3. settings    merge KB hooks into settings.json (additive; repoints our own
                 stale entries, never clobbers foreign ones)
  4. mcp         wire the KB MCP server into detected hosts (codex/cursor/...)
  5. scheduler   register the daily kb-sync job (per-OS)
  6. version     stamp <kb home>/.version with the repo's VERSION
  7. config      check the config file; never fabricate a vault path

The per-OS bootstrap scripts (install.ps1 / install.sh) only ensure Python + deps
exist, then call this. Everything load-bearing lives here so all hosts agree.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import deploy            # noqa: E402
import mcp_wire          # noqa: E402
import scheduler         # noqa: E402
import shortcut          # noqa: E402
from settings_merge import merge_settings, SettingsMergeError  # noqa: E402


def claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env and env.strip():
        return Path(env.strip())
    return Path.home() / ".claude"


def kb_dir() -> Path:
    env = os.environ.get("KB_HOME")
    if env and env.strip():
        return Path(env.strip())
    return Path.home() / ".kb"


def repo_version() -> str:
    f = REPO_ROOT / "VERSION"
    return f.read_text(encoding="utf-8").strip() if f.exists() else "0.0.0"


def installed_version(kdir: Path) -> str | None:
    f = kdir / ".version"
    if not f.exists():  # pre-0.11 stamp location
        f = claude_dir() / ".kb-version"
    return f.read_text(encoding="utf-8").strip() if f.exists() else None


# --- Steps -------------------------------------------------------------------

# The exact files the pre-0.11 deploy scattered into ~/.claude. Retiring them is
# name-exact: anything else in hooks/ or scripts/ belongs to the user.
_LEGACY_ENGINE = {
    "hooks": ["kb.py", "kb_config.py", "kb_retrieve.py", "kb_mcp.py",
              "kb-context.sh", "kb-bodyread-track.py", "kb-bodyread-track.sh",
              "kb-embed-daemon-spawn.sh", "kb-mark-intercept.py", "kb-mark-intercept.sh",
              "kb-stats-intercept.py", "kb-stats-intercept.sh",
              "kb-statusline-fragment.ps1"],
    "scripts": ["kb-sync.py", "kb-embed.py", "kb-embed-daemon.py"],
}


def step_migrate(kdir: Path, cdir: Path, apply: bool) -> dict:
    """One-time (idempotent) move of a pre-0.11 install from ~/.claude to ~/.kb.

    Copies what the engine reads (config, state, stamps, kill-switch) and
    retires the old deployed engine files into <kb home>/backups/migrate-<ts>/
    so nothing stale answers a hook or a scheduled run. The legacy config FILE
    is left in place (read-fallback for anything not yet repointed); once the
    canonical config exists it is ignored by resolution.
    """
    import shutil
    import time as _t
    rep = {"needed": False, "config": None, "state_files": 0, "stamps": [],
           "retired": [], "kill_switch": False, "dry_run": not apply}

    legacy_cfg = cdir / "kb-workspaces.json"
    new_cfg = kdir / "config.json"
    if legacy_cfg.exists() and not new_cfg.exists():
        rep["needed"] = True
        rep["config"] = f"{legacy_cfg} -> {new_cfg}"
        if apply:
            kdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_cfg, new_cfg)

    legacy_state = cdir / "state"
    new_state = kdir / "state"
    if legacy_state.is_dir():
        for f in sorted(legacy_state.glob("kb-*")):
            dst = new_state / f.name
            if dst.exists():
                continue
            rep["needed"] = True
            rep["state_files"] += 1
            if apply:
                new_state.mkdir(parents=True, exist_ok=True)
                if f.is_dir():
                    shutil.copytree(f, dst)
                else:
                    shutil.copy2(f, dst)

    for legacy_name, new_name in ((".kb-version", ".version"), (".kb-source", ".source")):
        lf, nf = cdir / legacy_name, kdir / new_name
        if lf.exists() and not nf.exists():
            rep["needed"] = True
            rep["stamps"].append(new_name)
            if apply:
                kdir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(lf, nf)

    if (cdir / "kb-hooks-disabled").exists() and not (kdir / "hooks-disabled").exists():
        rep["needed"] = True
        rep["kill_switch"] = True
        if apply:
            kdir.mkdir(parents=True, exist_ok=True)
            (kdir / "hooks-disabled").write_text("", encoding="utf-8")

    retire: list[Path] = []
    for sub, names in _LEGACY_ENGINE.items():
        for name in names:
            f = cdir / sub / name
            if f.exists():
                retire.append(f)
    if retire:
        rep["needed"] = True
        rep["retired"] = [str(f) for f in retire]
        if apply:
            ts = _t.strftime("%Y%m%dT%H%M%S")
            bdir = kdir / "backups" / f"migrate-{ts}"
            for f in retire:
                dst = bdir / f.relative_to(cdir)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dst))
            rep["backup_dir"] = str(bdir)
    return rep


def step_deploy(kdir: Path, cdir: Path, apply: bool) -> dict:
    if apply:
        return deploy.apply(REPO_ROOT, kdir, cdir)
    return deploy.diff(REPO_ROOT, kdir, cdir)


def step_settings(kdir: Path, cdir: Path, apply: bool) -> dict:
    try:
        return merge_settings(cdir / "settings.json", kdir, dry_run=not apply)
    except SettingsMergeError as e:
        return {"error": str(e)}


def step_mcp(kdir: Path, apply: bool) -> dict:
    return mcp_wire.wire_all(kdir, dry_run=not apply)


def step_scheduler(kdir: Path, apply: bool, time_hhmm: str) -> dict:
    return scheduler.register(kdir, time_hhmm=time_hhmm, dry_run=not apply)


def step_shortcut(apply: bool) -> dict:
    """Create the clickable 'KB Manager' shortcut so teammates open the config UI
    without a terminal. The manager runs from the repo, so the shortcut targets it."""
    return shortcut.create_shortcut(REPO_ROOT, dry_run=not apply)


def step_version(kdir: Path, apply: bool) -> dict:
    rep = {"from": installed_version(kdir), "to": repo_version()}
    if apply:
        kdir.mkdir(parents=True, exist_ok=True)
        (kdir / ".version").write_text(repo_version() + "\n", encoding="utf-8")
        # Record where the clone lives so the deployed CLI (`kb manage`) can find
        # the manager app, which runs from the source tree (Phase 2: no separate
        # deploy of the manager — the repo is already required to install/update).
        (kdir / ".source").write_text(str(REPO_ROOT) + "\n", encoding="utf-8")
        rep["stamped"] = True
        rep["source"] = str(REPO_ROOT)
    return rep


def step_config(kdir: Path) -> dict:
    """Report config presence. NEVER fabricate a vault path (poisons retrieval)."""
    cfg = kdir / "config.json"
    if not cfg.exists():
        legacy = claude_dir() / "kb-workspaces.json"
        if legacy.exists():
            cfg = legacy  # un-migrated machine: resolution still honors it
        else:
            return {
                "present": False,
                "note": (f"config missing. Copy config.example.json to {cfg} and set "
                         "'vault' to your vault path. KB hooks degrade safely (no injection) until set."),
            }
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        vault = data.get("vault")
        return {"present": True, "path": str(cfg), "vault": vault,
                "vault_exists": bool(vault) and Path(vault).exists()}
    except json.JSONDecodeError as e:
        return {"present": True, "path": str(cfg), "error": f"config is not valid JSON: {e}"}


# --- Orchestration -----------------------------------------------------------

def run(apply: bool, time_hhmm: str, scheduler_apply: bool | None = None,
        shortcut_apply: bool | None = None, mcp_apply: bool | None = None) -> dict:
    cdir = claude_dir()
    kdir = kb_dir()
    # The scheduler keys off a global task name, not claude_dir; `scheduler_apply`
    # lets a caller install everything else while leaving the schedule untouched
    # (also how the E2E test avoids clobbering a real ClaudeKbSync from a temp dir).
    if scheduler_apply is None:
        scheduler_apply = apply
    # The shortcut writes to the real user Desktop (not claude_dir); `shortcut_apply`
    # lets the E2E test exercise everything else without littering the live Desktop.
    if shortcut_apply is None:
        shortcut_apply = apply
    # MCP wiring writes to OTHER hosts' config files (real HOME, not claude_dir);
    # `mcp_apply` lets tests and `--skip-mcp-wire` leave them untouched.
    if mcp_apply is None:
        mcp_apply = apply
    report = {
        "mode": "apply" if apply else "dry-run",
        "repo": str(REPO_ROOT),
        "kb_dir": str(kdir),
        "claude_dir": str(cdir),
        "version": {"from": installed_version(kdir), "to": repo_version()},
        "migrate": step_migrate(kdir, cdir, apply),
        "deploy": step_deploy(kdir, cdir, apply),
        "settings": step_settings(kdir, cdir, apply),
        "mcp": step_mcp(kdir, mcp_apply),
        "scheduler": step_scheduler(kdir, scheduler_apply, time_hhmm),
        "shortcut": step_shortcut(shortcut_apply),
        "config": step_config(kdir),
    }
    if apply:
        report["version_stamp"] = step_version(kdir, apply)
    return report


def status() -> dict:
    kdir = kb_dir()
    return {
        "kb_dir": str(kdir),
        "claude_dir": str(claude_dir()),
        "installed_version": installed_version(kdir),
        "repo_version": repo_version(),
        "scheduler": scheduler.status(kdir),
        "config": step_config(kdir),
    }


# --- Update (remote-aware) ---------------------------------------------------
# "Is there a newer KB on the remote, and pull + apply it." Always deliberate and
# user-driven (a button or a CLI flag), never automatic. Two hard rules:
#   * the check only ever READS the remote (git fetch) — it writes nothing there;
#   * the apply is fast-forward-only and refuses a dirty or diverged tree, so it
#     can never clobber local work (e.g. a development clone with unpushed commits).
# The "new version" signal is the VERSION file on the tracked remote branch: a
# bumped VERSION is the author's deliberate release marker, so a mid-work commit on
# the branch tip doesn't read as an update.

def _git_at(cwd: Path, *args, timeout: int = 60):
    # GIT_TERMINAL_PROMPT=0: never block on a credential prompt — a missing auth
    # setup must fail fast (this runs under the non-interactive manager), not hang.
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _git(*args, timeout: int = 60):
    return _git_at(REPO_ROOT, *args, timeout=timeout)


def _semver(s: str) -> tuple:
    nums = re.findall(r"\d+", s or "")[:3]
    nums += ["0"] * (3 - len(nums))
    return tuple(int(n) for n in nums)


def _upstream() -> tuple[str, str]:
    """The remote/branch this clone tracks (e.g. ('origin', 'main')). Falls back to
    origin/main when there's no configured upstream."""
    r = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if r.returncode == 0 and "/" in r.stdout.strip():
        remote, branch = r.stdout.strip().split("/", 1)
        return remote, branch
    return "origin", "main"


def _last_line(s: str) -> str:
    lines = (s or "").strip().splitlines()
    return lines[-1] if lines else ""


def update_check(cdir: Path | None = None) -> dict:
    """Read-only: fetch the tracked remote branch and compare its VERSION to the
    local one. Never writes the remote; fail-soft so the UI always renders."""
    out = {"local_version": None, "remote_version": None, "ref": None,
           "update_available": False, "checked": False, "reason": None}
    try:
        out["local_version"] = repo_version()
        remote, branch = _upstream()
        out["ref"] = f"{remote}/{branch}"
        fr = _git("fetch", "--no-tags", remote, branch, timeout=20)
        if fr.returncode != 0:
            out["reason"] = "offline or fetch failed: " + (_last_line(fr.stderr) or f"rc={fr.returncode}")
            return out
        sr = _git("show", f"{remote}/{branch}:VERSION")
        if sr.returncode != 0:
            out["reason"] = "remote has no VERSION file"
            return out
        out["remote_version"] = sr.stdout.strip()
        out["checked"] = True
        out["update_available"] = _semver(out["remote_version"]) > _semver(out["local_version"])
    except Exception as e:
        out["reason"] = f"{type(e).__name__}: {e}"
    return out


def update_apply(cdir: Path | None = None) -> dict:
    """Pull the tracked remote branch (fast-forward only) and re-deploy. Refuses a
    dirty or diverged tree. Reversible: deploy backs every overwritten file up."""
    cdir = cdir or claude_dir()
    rep = {"updated": False, "from": None, "to": None, "ref": None,
           "reason": None, "deploy": None, "backup_dir": None, "note": None}
    try:
        rep["from"] = repo_version()
        dirty = _git("status", "--porcelain")
        if dirty.returncode != 0:
            rep["reason"] = "git status failed: " + (_last_line(dirty.stderr) or "unknown")
            return rep
        if dirty.stdout.strip():
            rep["reason"] = "working tree has uncommitted changes — commit or stash first."
            return rep
        remote, branch = _upstream()
        rep["ref"] = f"{remote}/{branch}"
        fr = _git("fetch", "--no-tags", remote, branch, timeout=30)
        if fr.returncode != 0:
            rep["reason"] = "fetch failed (offline?): " + (_last_line(fr.stderr) or f"rc={fr.returncode}")
            return rep
        mg = _git("merge", "--ff-only", f"{remote}/{branch}")
        if mg.returncode != 0:
            rep["reason"] = (f"can't fast-forward {rep['ref']} — local commits diverge. "
                             "Resolve manually (expected on a development clone).")
            return rep
        rep["to"] = repo_version()
        # Re-deploy the pulled tree. Skip scheduler + shortcut: the deployed script
        # path is unchanged, so the existing schedule still points right — don't
        # reset the user's configured sync time, and don't recreate the shortcut.
        out = run(apply=True, time_hhmm=scheduler.DEFAULT_TIME,
                  scheduler_apply=False, shortcut_apply=False)
        rep["deploy"] = out.get("deploy")
        rep["backup_dir"] = (out.get("deploy") or {}).get("backup_dir")
        rep["updated"] = True
        rep["note"] = ("Hooks and sync update live on their next run. The manager itself "
                       "runs from this tree — restart it and reload this page to pick up "
                       "server/UI changes.")
    except Exception as e:
        rep["reason"] = f"{type(e).__name__}: {e}"
    return rep


# --- Vault remote (one-time connect; NEVER auto-pushes) ----------------------
# The vault is local-only by default. A user may deliberately connect it to a
# private remote they own — as a backup, or to share it as a team knowledge base.
# This is the one-time "let's make a team repo" gesture, not an ongoing automation:
# the nightly sync still only commits locally and never pushes (commit_vault). Two
# rules mirror the update path: nothing is ever force-pushed (a non-fast-forward to
# a non-empty remote is reported, never overwritten), and an existing remote is
# never clobbered. Auth is out-of-band (SSH key / OS credential manager) — no token
# is ever stored here.

def _vault_path(cdir: Path | None = None) -> Path | None:
    """The configured vault path, or None. NEVER fabricated — an absent/invalid
    config yields None, callers report it. Mirrors engine kb_config.resolve_vault's
    order (KB_VAULT env first, then the config file) so this and the manager's
    knowledge view always resolve the SAME vault. An explicit `cdir` scopes the
    lookup to that dir alone (tests / callers overriding the machine config)."""
    env = os.environ.get("KB_VAULT")
    if env and env.strip():
        return Path(env.strip())
    if cdir is not None:
        cfg = Path(cdir) / "config.json"
        if not cfg.exists():
            cfg = Path(cdir) / "kb-workspaces.json"
    else:
        cfg = kb_dir() / "config.json"
        if not cfg.exists():
            cfg = claude_dir() / "kb-workspaces.json"
    if not cfg.exists():
        return None
    try:
        v = json.loads(cfg.read_text(encoding="utf-8")).get("vault")
    except json.JSONDecodeError:
        return None
    return Path(v) if v else None


def _is_git_repo(path: Path) -> bool:
    r = _git_at(path, "rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def vault_remote_status(cdir: Path | None = None) -> dict:
    """Report whether the configured vault has a git remote (and which). Read-only."""
    out = {"vault": None, "is_git": False, "has_remote": False,
           "remote": None, "url": None, "branch": None, "reason": None}
    try:
        v = _vault_path(cdir)
        if not v:
            out["reason"] = "no vault configured"
            return out
        out["vault"] = str(v)
        if not v.exists():
            out["reason"] = "vault path does not exist"
            return out
        if not _is_git_repo(v):
            out["reason"] = "vault is not a git repo"
            return out
        out["is_git"] = True
        br = _git_at(v, "rev-parse", "--abbrev-ref", "HEAD")
        out["branch"] = br.stdout.strip() if br.returncode == 0 else None
        rem = _git_at(v, "remote")
        remotes = rem.stdout.split() if rem.returncode == 0 else []
        if not remotes:
            return out  # local-only: has_remote stays False
        name = "origin" if "origin" in remotes else remotes[0]
        u = _git_at(v, "remote", "get-url", name)
        out["has_remote"] = True
        out["remote"] = name
        out["url"] = u.stdout.strip() if u.returncode == 0 else None
    except Exception as e:
        out["reason"] = f"{type(e).__name__}: {e}"
    return out


def vault_connect_remote(url: str, cdir: Path | None = None) -> dict:
    """Connect the vault to a remote the user owns and publish it (one-time).
    Adds 'origin' + `git push -u`. Refuses to clobber an existing remote and never
    force-pushes. If the push fails (auth not set up, or a local-only guard hook),
    the remote stays configured and the caller is told to push by hand."""
    rep = {"connected": False, "pushed": False, "vault": None, "remote": "origin",
           "url": None, "branch": None, "reason": None, "note": None}
    try:
        url = (url or "").strip()
        rep["url"] = url
        if not re.match(r"^(https://|git@|ssh://|git://|file://)", url):
            rep["reason"] = "remote URL must start with https://, git@, ssh://, git:// or file://"
            return rep
        v = _vault_path(cdir)
        if not v:
            rep["reason"] = "no vault configured — set the vault path first."
            return rep
        rep["vault"] = str(v)
        if not v.exists() or not _is_git_repo(v):
            rep["reason"] = "vault is missing or not a git repo."
            return rep
        rem = _git_at(v, "remote")
        if "origin" in (rem.stdout.split() if rem.returncode == 0 else []):
            cur = _git_at(v, "remote", "get-url", "origin")
            rep["reason"] = ("vault already has an 'origin' remote (" + (cur.stdout.strip() or "?")
                             + "). Repoint it by hand if that's intended.")
            return rep
        br = _git_at(v, "rev-parse", "--abbrev-ref", "HEAD")
        branch = br.stdout.strip() if br.returncode == 0 else "main"
        rep["branch"] = branch
        add = _git_at(v, "remote", "add", "origin", url)
        if add.returncode != 0:
            rep["reason"] = "git remote add failed: " + (_last_line(add.stderr) or "unknown")
            return rep
        rep["connected"] = True
        # First publish. -u sets the upstream so later pushes are a bare `git push`.
        # No --force, ever: a non-empty remote with diverging history rejects this,
        # which we report rather than overwrite.
        ps = _git_at(v, "push", "-u", "origin", branch, timeout=120)
        if ps.returncode != 0:
            rep["reason"] = "remote added, but the first push failed: " + (_last_line(ps.stderr) or "unknown")
            rep["note"] = ("Set up git auth (SSH key or credential manager) — note a local-only "
                           "guard hook also blocks pushing by design — then run "
                           f"`git push -u origin {branch}` from the vault. The remote stays configured.")
            return rep
        rep["pushed"] = True
        rep["note"] = f"Vault published to {url} ({branch}). Teammates can clone it now."
    except Exception as e:
        rep["reason"] = f"{type(e).__name__}: {e}"
    return rep


def vault_pull_remote(cdir: Path | None = None) -> dict:
    """Pull the team's latest from the vault's remote (the read side of a shared KB).
    fetch + merge: disjoint per-file learnings auto-merge cleanly (the common case).
    Safety: refuses a dirty tree; on a real content conflict it ABORTS the merge,
    restoring the clean pre-merge tree — a conflict marker never lands in a learning,
    the user resolves by hand. The remote is only ever read (fetch); never force, never
    pushed here."""
    rep = {"pulled": False, "vault": None, "branch": None, "remote": "origin",
           "merged_commits": 0, "already_up_to_date": False, "conflict": False,
           "reason": None, "note": None}
    try:
        v = _vault_path(cdir)
        if not v:
            rep["reason"] = "no vault configured."
            return rep
        rep["vault"] = str(v)
        if not v.exists() or not _is_git_repo(v):
            rep["reason"] = "vault is missing or not a git repo."
            return rep
        rem = _git_at(v, "remote")
        if "origin" not in (rem.stdout.split() if rem.returncode == 0 else []):
            rep["reason"] = "vault has no 'origin' remote — connect it first."
            return rep
        dirty = _git_at(v, "status", "--porcelain")
        if dirty.returncode != 0:
            rep["reason"] = "git status failed: " + (_last_line(dirty.stderr) or "unknown")
            return rep
        if dirty.stdout.strip():
            rep["reason"] = ("vault has uncommitted changes — let the next sync commit them "
                             "(or commit by hand) before pulling.")
            return rep
        br = _git_at(v, "rev-parse", "--abbrev-ref", "HEAD")
        branch = br.stdout.strip() if br.returncode == 0 else "main"
        rep["branch"] = branch
        before = _git_at(v, "rev-parse", "HEAD").stdout.strip()
        fr = _git_at(v, "fetch", "--no-tags", "origin", branch, timeout=60)
        if fr.returncode != 0:
            rep["reason"] = "fetch failed (offline?): " + (_last_line(fr.stderr) or f"rc={fr.returncode}")
            return rep
        mg = _git_at(v, "merge", "--no-edit", f"origin/{branch}", timeout=60)
        if mg.returncode != 0:
            # Real conflict (or other merge failure): abort to restore the clean
            # pre-merge tree so no half-merged / conflict-marked learning persists.
            _git_at(v, "merge", "--abort")
            rep["conflict"] = True
            rep["reason"] = (f"the remote and your vault edited the same learning — auto-merge couldn't "
                             "reconcile it, so nothing changed. Resolve by hand in the vault "
                             f"(`git merge origin/{branch}`), then retry.")
            return rep
        after = _git_at(v, "rev-parse", "HEAD").stdout.strip()
        if after == before:
            rep["pulled"] = True
            rep["already_up_to_date"] = True
            rep["note"] = "Already up to date — nothing new from the remote."
            return rep
        cnt = _git_at(v, "rev-list", "--count", f"{before}..{after}")
        rep["merged_commits"] = int(cnt.stdout.strip() or 0) if cnt.returncode == 0 else 0
        rep["pulled"] = True
        rep["note"] = ("Pulled the team's latest. New learnings are on disk now; retrieval picks "
                       "them up on the next sync/reindex.")
    except Exception as e:
        rep["reason"] = f"{type(e).__name__}: {e}"
    return rep


def _summary(rep: dict) -> str:
    lines = [f"KB installer [{rep['mode']}]  v{rep['version']['from'] or '-'} -> v{rep['version']['to']}"]
    mg = rep.get("migrate") or {}
    if mg.get("needed"):
        bits = []
        if mg.get("config"):
            bits.append("config")
        if mg.get("state_files"):
            bits.append(f"{mg['state_files']} state file(s)")
        if mg.get("retired"):
            bits.append(f"{len(mg['retired'])} old engine file(s) retired")
        lines.append(f"  migrate  : ~/.claude -> ~/.kb ({', '.join(bits) or 'stamps'})"
                     + ("" if not mg.get("dry_run") else "  [dry-run]"))
    d = rep["deploy"]
    if "buckets" in d:  # dry-run diff
        b = d["buckets"]
        lines.append(f"  deploy   : {len(b['new'])} new, {len(b['changed'])} changed, "
                     f"{len(b['eol-only'])} eol-only, {len(b['same'])} same (of {d['total']})")
    else:
        lines.append(f"  deploy   : wrote {d.get('wrote', 0)}"
                     + (f", backup {d['backup_dir']}" if d.get("backup_dir") else "")
                     + (f"  ERROR {d['error']}" if d.get("error") else ""))
    s = rep["settings"]
    if "error" in s:
        lines.append(f"  settings : ERROR {s['error']}")
    else:
        lines.append(f"  settings : +{len(s.get('added', []))} added, "
                     f"{len(s.get('updated', []))} repointed, {len(s.get('skipped', []))} present"
                     + (f", backup {s['backup']}" if s.get("backup") else ""))
    m = rep.get("mcp") or {}
    hosts = m.get("hosts", {})
    wired = [n for n, r in hosts.items() if r.get("status") in ("wired", "would-wire")]
    updated = [n for n, r in hosts.items() if r.get("status") in ("updated", "would-update")]
    present = [n for n, r in hosts.items() if r.get("status") == "already"]
    problems = [n for n, r in hosts.items() if r.get("status") in ("refused-malformed", "error")]
    detected = [n for n, r in hosts.items() if r.get("status") != "not-detected"]
    desc = (f"{len(detected)} host(s) detected"
            + (f", wired: {', '.join(wired)}" if wired else "")
            + (f", repointed: {', '.join(updated)}" if updated else "")
            + (f", present: {', '.join(present)}" if present else "")
            + (f", PROBLEM: {', '.join(problems)}" if problems else ""))
    lines.append(f"  mcp      : {desc}")
    sc = rep["scheduler"]
    lines.append(f"  scheduler: {sc.get('os')} task '{sc.get('task', 'kb-sync')}' "
                 + ("registered" if sc.get("registered") else f"@ {sc.get('time')} (dry-run)"))
    sh = rep.get("shortcut", {})
    if sh:
        state = ("created" if sh.get("created") else
                 ("would create" if sh.get("dry_run") else f"ERROR {sh.get('error', 'failed')}"))
        lines.append(f"  shortcut : {sh.get('os')} {state} -> {sh.get('path')}")
    c = rep["config"]
    lines.append(f"  config   : " + ("present, vault=" + str(c.get("vault")) if c.get("present")
                                      else "MISSING — " + c.get("note", "")))
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Install/update KB into the host.")
    ap.add_argument("--apply", action="store_true", help="apply changes (default: dry-run)")
    ap.add_argument("--rollback", action="store_true", help="restore most recent deploy backup")
    ap.add_argument("--status", action="store_true", help="show installed state")
    ap.add_argument("--update-check", action="store_true", help="check the remote for a newer VERSION (read-only)")
    ap.add_argument("--update", action="store_true", help="fast-forward to the remote + re-deploy (reversible)")
    ap.add_argument("--vault-remote", action="store_true", help="show the vault's git remote status")
    ap.add_argument("--vault-connect", metavar="URL", help="connect the vault to a remote you own + push (one-time)")
    ap.add_argument("--vault-pull", action="store_true", help="pull the team's latest into the vault (fetch + merge, aborts on conflict)")
    ap.add_argument("--time", default=scheduler.DEFAULT_TIME, help="daily kb-sync time HH:MM (default 01:00)")
    ap.add_argument("--skip-scheduler", action="store_true", help="install everything but don't touch the OS scheduler")
    ap.add_argument("--skip-shortcut", action="store_true", help="install everything but don't create the desktop shortcut")
    ap.add_argument("--no-mcp-wire", action="store_true", help="don't write the KB MCP server into other hosts' configs")
    ap.add_argument("--json", action="store_true", help="emit raw JSON report")
    args = ap.parse_args()

    if args.rollback:
        out = deploy.rollback(kb_dir())
        print(json.dumps(out, indent=2))
    elif args.status:
        out = status()
        print(json.dumps(out, indent=2))
    elif args.update_check:
        print(json.dumps(update_check(), indent=2))
    elif args.update:
        print(json.dumps(update_apply(), indent=2))
    elif args.vault_remote:
        print(json.dumps(vault_remote_status(), indent=2))
    elif args.vault_connect:
        print(json.dumps(vault_connect_remote(args.vault_connect), indent=2))
    elif args.vault_pull:
        print(json.dumps(vault_pull_remote(), indent=2))
    else:
        sched = False if args.skip_scheduler else None
        shortc = False if args.skip_shortcut else None
        mcpw = False if args.no_mcp_wire else None
        out = run(apply=args.apply, time_hhmm=args.time, scheduler_apply=sched,
                  shortcut_apply=shortc, mcp_apply=mcpw)
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            print(_summary(out))
