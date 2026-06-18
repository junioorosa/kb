#!/usr/bin/env python3
"""Register the kb-sync background job, per OS.

kb-sync does the daily git-aware capture/finalize + vault auto-commit + daemon
reindex. The host must run it on a schedule. This is the only genuinely per-OS
piece of the installer (besides the bootstrap script): Windows Task Scheduler,
macOS launchd, Linux cron.

One or MANY daily times, one artifact per OS — never one task per time:
  * Windows: a single task created from XML with one CalendarTrigger per time
    (the plain `/SC DAILY /ST` form allows only one trigger);
  * macOS: one plist whose StartCalendarInterval is an array;
  * Linux: one managed cron block with one line per time.
So re-registering stays a wholesale replace under a stable name, and removing
a time can never leave a stale sibling task behind.

Dispatch is on sys.platform, so on any given machine only that OS's path runs.
`dry_run` returns the exact command/artifact without touching the system — used
by the orchestrator's diff and by tests on a foreign OS.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TASK_NAME = "ClaudeKbSync"          # Windows task / launchd label stem / cron marker
DEFAULT_TIME = "01:00"             # daily, local time
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# cron and launchd run with a minimal PATH that omits ~/.local/bin (the default
# Claude Code install dir), so the bare `claude` kb-sync shells out to would not
# resolve. We bake the dir holding `claude` (and the job's python) into the
# job's PATH at register time. Mirrors KB_CLAUDE/KB_PYTHON resolution.
_CLAUDE_BIN_DIRS = (
    "~/.local/bin",
    "~/.claude/local",
    "~/.npm-global/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
)


def _python_exe(explicit: str | None) -> str:
    return explicit or sys.executable or "python"


def _claude_dir() -> str | None:
    """Directory containing the `claude` CLI, or None. Note we take the dirname
    of the entry found on PATH (typically a ~/.local/bin symlink) WITHOUT
    resolving it — resolving would point at the internal versioned target, which
    moves on every CLI update."""
    explicit = os.environ.get("KB_CLAUDE")
    if explicit:
        return str(Path(explicit).expanduser().parent)
    found = shutil.which("claude")
    if found:
        return str(Path(found).parent)
    for d in _CLAUDE_BIN_DIRS:
        p = Path(d).expanduser()
        if (p / "claude").exists():
            return str(p)
    return None


def _path_prefix(python_exe: str) -> str:
    """Colon-joined dirs to prepend to the scheduled job's PATH so the tools
    kb-sync shells out to (chiefly `claude`) resolve under the minimal
    cron/launchd environment. '' when nothing extra is needed."""
    dirs = []
    cdir = _claude_dir()
    if cdir:
        dirs.append(cdir)
    pdir = str(Path(python_exe).parent)
    if os.sep in python_exe and pdir not in dirs:
        dirs.append(pdir)
    return ":".join(dirs)


def _paths(kb_dir: Path):
    kb_dir = Path(kb_dir)
    script = kb_dir / "engine" / "kb-sync.py"
    log = kb_dir / "logs" / "kb-sync.log"
    return script, log


def normalize_times(times) -> list[str]:
    """Accept a single 'HH:MM' or a list of them; validate, dedupe, sort.
    Raises ValueError on anything malformed or an empty result — a schedule
    that silently registered nothing would be worse than a loud refusal."""
    if isinstance(times, str):
        times = [times]
    if not isinstance(times, (list, tuple)) or not times:
        raise ValueError("times must be a non-empty 'HH:MM' or list of them")
    out = []
    for t in times:
        t = str(t).strip()
        if not _HHMM.match(t):
            raise ValueError(f"invalid time (need 24h HH:MM): {t!r}")
        if t not in out:
            out.append(t)
    return sorted(out)


# --- Windows -----------------------------------------------------------------

def _windows_task_xml(kb_dir: Path, python_exe: str, times: list[str]) -> str:
    """Task Scheduler XML: one task, one CalendarTrigger per daily time. Runs as
    the current user's interactive token (same behavior as a plain /SC DAILY
    create: no stored password, runs when the user is logged on)."""
    script, log = _paths(kb_dir)
    triggers = "\n".join(
        f"""    <CalendarTrigger>
      <StartBoundary>2020-01-01T{t}:00</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>""" for t in times)
    args = f'/c "{python_exe} {script} >> {log} 2>&1"'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>KB nightly sync (capture + finalize + reindex)</Description>
  </RegistrationInfo>
  <Triggers>
{triggers}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd</Command>
      <Arguments>{args.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _register_windows(kb_dir: Path, python_exe: str, times: list[str], dry_run: bool) -> dict:
    xml = _windows_task_xml(kb_dir, python_exe, times)
    report = {"os": "windows", "task": TASK_NAME, "times": times,
              "artifact": xml, "registered": False}
    if dry_run:
        return report
    _paths(kb_dir)[1].parent.mkdir(parents=True, exist_ok=True)
    # schtasks reads the XML from a file; UTF-16 matches the declared encoding.
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False,
                                     encoding="utf-16") as fh:
        fh.write(xml)
        tmp = fh.name
    try:
        cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", tmp, "/F"]
        report["command"] = subprocess.list2cmdline(cmd)
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        report["registered"] = r.returncode == 0
        report["stdout"] = r.stdout.strip()
        if r.returncode != 0:
            report["error"] = r.stderr.strip()
    finally:
        try:
            Path(tmp).unlink()
        except OSError:
            pass
    return report


def _status_windows() -> dict:
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"],
                       capture_output=True, text=True, errors="replace")
    out = {"os": "windows", "task": TASK_NAME, "exists": r.returncode == 0}
    if r.returncode == 0:
        out["times"] = parse_times_from_task_xml(r.stdout)
    return out


def parse_times_from_task_xml(xml: str) -> list[str]:
    return sorted(set(re.findall(r"<StartBoundary>[^<]*T(\d\d:\d\d):", xml or "")))


def _unregister_windows(dry_run: bool) -> dict:
    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    if dry_run:
        return {"os": "windows", "command": subprocess.list2cmdline(cmd)}
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    return {"os": "windows", "removed": r.returncode == 0, "error": r.stderr.strip() or None}


# --- macOS (launchd) ---------------------------------------------------------

def _macos_plist(kb_dir: Path, python_exe: str, times: list[str],
                 path_prefix: str = "") -> str:
    script, log = _paths(kb_dir)
    intervals = "\n".join(
        f"    <dict><key>Hour</key><integer>{int(t.split(':')[0])}</integer>"
        f"<key>Minute</key><integer>{int(t.split(':')[1])}</integer></dict>"
        for t in times)
    # launchd starts the job with a bare PATH; prepend the claude/python dirs so
    # the same `claude` resolution that works in the user's shell works here.
    env = (f"  <key>EnvironmentVariables</key>\n"
           f"  <dict><key>PATH</key>"
           f"<string>{path_prefix}:/usr/local/bin:/usr/bin:/bin</string></dict>\n"
           ) if path_prefix else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.kb.sync</string>
{env}  <key>ProgramArguments</key>
  <array><string>{python_exe}</string><string>{script}</string></array>
  <key>StartCalendarInterval</key>
  <array>
{intervals}
  </array>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
"""


def _register_macos(kb_dir: Path, python_exe: str, times: list[str], dry_run: bool,
                    path_prefix: str = "") -> dict:
    plist = _macos_plist(kb_dir, python_exe, times, path_prefix)
    target = Path.home() / "Library" / "LaunchAgents" / "com.kb.sync.plist"
    report = {"os": "macos", "plist_path": str(target), "times": times,
              "artifact": plist, "registered": False}
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


def parse_times_from_plist(plist: str) -> list[str]:
    pairs = re.findall(r"<key>Hour</key><integer>(\d+)</integer>"
                       r"<key>Minute</key><integer>(\d+)</integer>", plist or "")
    return sorted({f"{int(h):02d}:{int(m):02d}" for h, m in pairs})


# --- Linux (cron) ------------------------------------------------------------

_CRON_BEGIN = "# >>> KB-SYNC (managed) >>>"
_CRON_END = "# <<< KB-SYNC (managed) <<<"


def _linux_cron_block(kb_dir: Path, python_exe: str, times: list[str],
                      path_prefix: str = "") -> str:
    script, log = _paths(kb_dir)
    # Inline, per-command PATH (scoped to our line; never leaks to other cron
    # jobs the way a free-standing PATH= env line in the crontab would). cron
    # runs each line via /bin/sh, so $PATH expands to cron's default.
    env = f'PATH="{path_prefix}:$PATH" ' if path_prefix else ""
    lines = []
    for t in times:
        hh, mm = t.split(":")
        lines.append(f"{int(mm)} {int(hh)} * * * {env}{python_exe} {script} >> {log} 2>&1")
    return "\n".join([_CRON_BEGIN, *lines, _CRON_END])


def _register_linux(kb_dir: Path, python_exe: str, times: list[str], dry_run: bool,
                    path_prefix: str = "") -> dict:
    block = _linux_cron_block(kb_dir, python_exe, times, path_prefix)
    report = {"os": "linux", "times": times, "artifact": block, "registered": False}
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


def parse_times_from_cron(tab: str) -> list[str]:
    times, inside = set(), False
    for ln in (tab or "").splitlines():
        s = ln.strip()
        if s == _CRON_BEGIN:
            inside = True
            continue
        if s == _CRON_END:
            inside = False
            continue
        if inside:
            m = re.match(r"^(\d+)\s+(\d+)\s", s)
            if m:
                times.add(f"{int(m.group(2)):02d}:{int(m.group(1)):02d}")
    return sorted(times)


# --- Dispatch ----------------------------------------------------------------

def register(kb_dir: Path, python_exe: str | None = None, time_hhmm=DEFAULT_TIME,
             dry_run: bool = False) -> dict:
    """Register the job at one or many daily times. `time_hhmm` keeps its name
    (and single-string form) for the existing callers; lists are first-class."""
    py = _python_exe(python_exe)
    try:
        times = normalize_times(time_hhmm)
    except ValueError as e:
        return {"os": sys.platform, "registered": False, "error": str(e)}
    if sys.platform == "win32":
        # Windows tasks run under the user's interactive token, which inherits
        # the user PATH, so no prefix is needed there.
        rep = _register_windows(Path(kb_dir), py, times, dry_run)
    elif sys.platform == "darwin":
        rep = _register_macos(Path(kb_dir), py, times, dry_run, _path_prefix(py))
    else:
        rep = _register_linux(Path(kb_dir), py, times, dry_run, _path_prefix(py))
    rep["time"] = times[0]  # legacy single-time field, kept for old readers
    return rep


def status(kb_dir: Path | None = None) -> dict:
    if sys.platform == "win32":
        return _status_windows()
    if sys.platform == "darwin":
        p = Path.home() / "Library" / "LaunchAgents" / "com.kb.sync.plist"
        out = {"os": "macos", "exists": p.exists(), "plist_path": str(p)}
        if p.exists():
            out["times"] = parse_times_from_plist(p.read_text(encoding="utf-8"))
        return out
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    tab = cur.stdout or ""
    out = {"os": "linux", "exists": _CRON_BEGIN in tab}
    if out["exists"]:
        out["times"] = parse_times_from_cron(tab)
    return out


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Register the kb-sync scheduled job.")
    ap.add_argument("--kb-dir", required=True)
    ap.add_argument("--python", default=None, help="python exe for the job (default: this interpreter)")
    ap.add_argument("--time", action="append", default=None,
                    help="daily HH:MM; repeat the flag for multiple times (default 01:00)")
    ap.add_argument("--apply", action="store_true", help="register (default: show what would be done)")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        print(json.dumps(status(Path(args.kb_dir)), indent=2))
    else:
        print(json.dumps(register(Path(args.kb_dir), args.python,
                                  args.time or DEFAULT_TIME, dry_run=not args.apply), indent=2))
