#!/usr/bin/env python3
"""KB installer/updater — OS-agnostic orchestrator (topology A: repo -> host).

One command for first-install AND "update certinho":

    python installer/install.py            # dry-run: show exactly what would change
    python installer/install.py --apply    # do it (backs up everything it touches)
    python installer/install.py --rollback # restore the most recent deploy backup
    python installer/install.py --status   # what's installed (version, scheduler)

Steps (each idempotent, each safe to re-run):
  1. deploy      engine + adapter files into ~/.claude (diff -> backup -> copy)
  2. settings    merge KB hooks into settings.json (additive, never clobbers)
  3. scheduler   register the daily kb-sync job (per-OS)
  4. version     stamp ~/.claude/.kb-version with the repo's VERSION
  5. config      check kb-workspaces.json; never fabricate a vault path

The per-OS bootstrap scripts (install.ps1 / install.sh) only ensure Python + deps
exist, then call this. Everything load-bearing lives here so all hosts agree.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import deploy            # noqa: E402
import scheduler         # noqa: E402
from settings_merge import merge_settings, SettingsMergeError  # noqa: E402


def claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env and env.strip():
        return Path(env.strip())
    return Path.home() / ".claude"


def repo_version() -> str:
    f = REPO_ROOT / "VERSION"
    return f.read_text(encoding="utf-8").strip() if f.exists() else "0.0.0"


def installed_version(cdir: Path) -> str | None:
    f = cdir / ".kb-version"
    return f.read_text(encoding="utf-8").strip() if f.exists() else None


# --- Steps -------------------------------------------------------------------

def step_deploy(cdir: Path, apply: bool) -> dict:
    if apply:
        return deploy.apply(REPO_ROOT, cdir)
    return deploy.diff(REPO_ROOT, cdir)


def step_settings(cdir: Path, apply: bool) -> dict:
    try:
        return merge_settings(cdir / "settings.json", cdir, dry_run=not apply)
    except SettingsMergeError as e:
        return {"error": str(e)}


def step_scheduler(cdir: Path, apply: bool, time_hhmm: str) -> dict:
    return scheduler.register(cdir, time_hhmm=time_hhmm, dry_run=not apply)


def step_version(cdir: Path, apply: bool) -> dict:
    rep = {"from": installed_version(cdir), "to": repo_version()}
    if apply:
        (cdir / ".kb-version").write_text(repo_version() + "\n", encoding="utf-8")
        # Record where the clone lives so the deployed CLI (`kb manage`) can find
        # the manager app, which runs from the source tree (Phase 2: no separate
        # deploy of the manager — the repo is already required to install/update).
        (cdir / ".kb-source").write_text(str(REPO_ROOT) + "\n", encoding="utf-8")
        rep["stamped"] = True
        rep["source"] = str(REPO_ROOT)
    return rep


def step_config(cdir: Path) -> dict:
    """Report config presence. NEVER fabricate a vault path (poisons retrieval)."""
    cfg = cdir / "kb-workspaces.json"
    if not cfg.exists():
        return {
            "present": False,
            "note": ("kb-workspaces.json missing. Copy installer/config.example.json there and set "
                     "'vault' to your vault path. KB hooks degrade safely (no injection) until set."),
        }
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        vault = data.get("vault")
        return {"present": True, "vault": vault, "vault_exists": bool(vault) and Path(vault).exists()}
    except json.JSONDecodeError as e:
        return {"present": True, "error": f"kb-workspaces.json is not valid JSON: {e}"}


# --- Orchestration -----------------------------------------------------------

def run(apply: bool, time_hhmm: str, scheduler_apply: bool | None = None) -> dict:
    cdir = claude_dir()
    # The scheduler keys off a global task name, not claude_dir; `scheduler_apply`
    # lets a caller install everything else while leaving the schedule untouched
    # (also how the E2E test avoids clobbering a real ClaudeKbSync from a temp dir).
    if scheduler_apply is None:
        scheduler_apply = apply
    report = {
        "mode": "apply" if apply else "dry-run",
        "repo": str(REPO_ROOT),
        "claude_dir": str(cdir),
        "version": {"from": installed_version(cdir), "to": repo_version()},
        "deploy": step_deploy(cdir, apply),
        "settings": step_settings(cdir, apply),
        "scheduler": step_scheduler(cdir, scheduler_apply, time_hhmm),
        "config": step_config(cdir),
    }
    if apply:
        report["version_stamp"] = step_version(cdir, apply)
    return report


def status() -> dict:
    cdir = claude_dir()
    return {
        "claude_dir": str(cdir),
        "installed_version": installed_version(cdir),
        "repo_version": repo_version(),
        "scheduler": scheduler.status(cdir),
        "config": step_config(cdir),
    }


def _summary(rep: dict) -> str:
    lines = [f"KB installer [{rep['mode']}]  v{rep['version']['from'] or '-'} -> v{rep['version']['to']}"]
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
        lines.append(f"  settings : +{len(s.get('added', []))} added, {len(s.get('skipped', []))} present"
                     + (f", backup {s['backup']}" if s.get("backup") else ""))
    sc = rep["scheduler"]
    lines.append(f"  scheduler: {sc.get('os')} task '{sc.get('task', 'kb-sync')}' "
                 + ("registered" if sc.get("registered") else f"@ {sc.get('time')} (dry-run)"))
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
    ap.add_argument("--time", default=scheduler.DEFAULT_TIME, help="daily kb-sync time HH:MM (default 01:00)")
    ap.add_argument("--skip-scheduler", action="store_true", help="install everything but don't touch the OS scheduler")
    ap.add_argument("--json", action="store_true", help="emit raw JSON report")
    args = ap.parse_args()

    if args.rollback:
        out = deploy.rollback(claude_dir())
        print(json.dumps(out, indent=2))
    elif args.status:
        out = status()
        print(json.dumps(out, indent=2))
    else:
        sched = False if args.skip_scheduler else None
        out = run(apply=args.apply, time_hhmm=args.time, scheduler_apply=sched)
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            print(_summary(out))
