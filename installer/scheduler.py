#!/usr/bin/env python3
"""Register the kb-sync background job, per OS.

kb-sync does the daily git-aware capture/finalize + vault auto-commit + daemon
reindex. The host must run it on a schedule. This is the only genuinely per-OS
piece of the installer (besides the bootstrap script): Windows Task Scheduler,
macOS launchd, Linux cron.

Dispatch is on sys.platform, so on any given machine only that OS's path runs.
Idempotent: re-registering replaces the prior definition under a stable name.
`dry_run` returns the exact command/artifact without touching the system — used
by the orchestrator's diff and by tests on a foreign OS.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TASK_NAME = "ClaudeKbSync"          # Windows task / launchd label stem / cron marker
DEFAULT_TIME = "01:00"             # daily, local time


def _python_exe(explicit: str | None) -> str:
    return explicit or sys.executable or "python"


def _paths(kb_dir: Path):
    kb_dir = Path(kb_dir)
    script = kb_dir / "engine" / "kb-sync.py"
    log = kb_dir / "logs" / "kb-sync.log"
    return script, log


# --- Windows -----------------------------------------------------------------

def _register_windows(kb_dir: Path, python_exe: str, time_hhmm: str, dry_run: bool) -> dict:
    script, log = _paths(kb_dir)
    log.parent.mkdir(parents=True, exist_ok=True)
    # cmd /c so we can redirect stdout+stderr to the log, matching the proven task.
    tr = f'cmd /c "{python_exe} {script} >> {log} 2>&1"'
    cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", tr,
           "/SC", "DAILY", "/ST", time_hhmm, "/F"]
    report = {"os": "windows", "task": TASK_NAME, "time": time_hhmm,
              "command": subprocess.list2cmdline(cmd), "registered": False}
    if dry_run:
        return report
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    report["registered"] = r.returncode == 0
    report["stdout"] = r.stdout.strip()
    if r.returncode != 0:
        report["error"] = r.stderr.strip()
    return report


def _status_windows() -> dict:
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                       capture_output=True, text=True, errors="replace")
    return {"os": "windows", "task": TASK_NAME, "exists": r.returncode == 0}


def _unregister_windows(dry_run: bool) -> dict:
    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    if dry_run:
        return {"os": "windows", "command": subprocess.list2cmdline(cmd)}
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    return {"os": "windows", "removed": r.returncode == 0, "error": r.stderr.strip() or None}


# --- macOS (launchd) ---------------------------------------------------------

def _macos_plist(kb_dir: Path, python_exe: str, time_hhmm: str) -> str:
    script, log = _paths(kb_dir)
    hh, mm = time_hhmm.split(":")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.kb.sync</string>
  <key>ProgramArguments</key>
  <array><string>{python_exe}</string><string>{script}</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>{int(hh)}</integer><key>Minute</key><integer>{int(mm)}</integer></dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
"""


def _register_macos(kb_dir: Path, python_exe: str, time_hhmm: str, dry_run: bool) -> dict:
    plist = _macos_plist(kb_dir, python_exe, time_hhmm)
    target = Path.home() / "Library" / "LaunchAgents" / "com.kb.sync.plist"
    report = {"os": "macos", "plist_path": str(target), "time": time_hhmm, "artifact": plist, "registered": False}
    if dry_run:
        return report
    target.parent.mkdir(parents=True, exist_ok=True)
    _paths(kb_dir)[1].parent.mkdir(parents=True, exist_ok=True)
    target.write_text(plist, encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(target)], capture_output=True, text=True, errors="replace")
    r = subprocess.run(["launchctl", "load", str(target)], capture_output=True, text=True, errors="replace")
    report["registered"] = r.returncode == 0
    if r.returncode != 0:
        report["error"] = r.stderr.strip()
    return report


# --- Linux (cron) ------------------------------------------------------------

_CRON_BEGIN = "# >>> KB-SYNC (managed) >>>"
_CRON_END = "# <<< KB-SYNC (managed) <<<"


def _linux_cron_block(kb_dir: Path, python_exe: str, time_hhmm: str) -> str:
    script, log = _paths(kb_dir)
    hh, mm = time_hhmm.split(":")
    line = f"{int(mm)} {int(hh)} * * * {python_exe} {script} >> {log} 2>&1"
    return f"{_CRON_BEGIN}\n{line}\n{_CRON_END}"


def _register_linux(kb_dir: Path, python_exe: str, time_hhmm: str, dry_run: bool) -> dict:
    block = _linux_cron_block(kb_dir, python_exe, time_hhmm)
    report = {"os": "linux", "time": time_hhmm, "artifact": block, "registered": False}
    if dry_run:
        return report
    _paths(kb_dir)[1].parent.mkdir(parents=True, exist_ok=True)
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, errors="replace")
    existing = cur.stdout if cur.returncode == 0 else ""
    # Drop any prior managed block, then append the fresh one (idempotent replace).
    lines, skip = [], False
    for ln in existing.splitlines():
        if ln.strip() == _CRON_BEGIN:
            skip = True
            continue
        if ln.strip() == _CRON_END:
            skip = False
            continue
        if not skip:
            lines.append(ln)
    new_tab = "\n".join([*lines, block, ""]).lstrip("\n")
    w = subprocess.run(["crontab", "-"], input=new_tab, capture_output=True, text=True, errors="replace")
    report["registered"] = w.returncode == 0
    if w.returncode != 0:
        report["error"] = w.stderr.strip()
    return report


# --- Dispatch ----------------------------------------------------------------

def register(kb_dir: Path, python_exe: str | None = None, time_hhmm: str = DEFAULT_TIME, dry_run: bool = False) -> dict:
    py = _python_exe(python_exe)
    if sys.platform == "win32":
        return _register_windows(Path(kb_dir), py, time_hhmm, dry_run)
    if sys.platform == "darwin":
        return _register_macos(Path(kb_dir), py, time_hhmm, dry_run)
    return _register_linux(Path(kb_dir), py, time_hhmm, dry_run)


def status(kb_dir: Path | None = None) -> dict:
    if sys.platform == "win32":
        return _status_windows()
    if sys.platform == "darwin":
        p = Path.home() / "Library" / "LaunchAgents" / "com.kb.sync.plist"
        return {"os": "macos", "exists": p.exists(), "plist_path": str(p)}
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return {"os": "linux", "exists": _CRON_BEGIN in (cur.stdout or "")}


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Register the kb-sync scheduled job.")
    ap.add_argument("--claude-dir", required=True)
    ap.add_argument("--python", default=None, help="python exe for the job (default: this interpreter)")
    ap.add_argument("--time", default=DEFAULT_TIME, help="daily HH:MM (default 01:00)")
    ap.add_argument("--apply", action="store_true", help="register (default: show what would be done)")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        print(json.dumps(status(Path(args.kb_dir)), indent=2))
    else:
        print(json.dumps(register(Path(args.kb_dir), args.python, args.time, dry_run=not args.apply), indent=2))
