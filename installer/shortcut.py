#!/usr/bin/env python3
"""Create a clickable "KB Manager" shortcut, per OS.

After the one-time install, a teammate should never touch a terminal to open the
config UI — they double-click a shortcut, which launches the manager (a local web
server that opens the browser itself). This is the only other genuinely per-OS
installer piece besides the bootstrap and the scheduler:
  - Windows: a .lnk on the Desktop (created via WScript.Shell COM, from PowerShell)
  - macOS:   a double-clickable ~/Desktop/KB Manager.command
  - Linux:   a ~/.local/share/applications/kb-manager.desktop entry

Dispatch is on sys.platform, so only the host OS's path runs. `dry_run` returns the
exact artifact (path + content/target) without writing — used by the orchestrator's
diff and by tests on a foreign OS. Idempotent: re-running overwrites the same target.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SHORTCUT_NAME = "KB Manager"
DESCRIPTION = "Open the KB manager (config UI)"


def _manager_server(repo_root: Path) -> Path:
    return Path(repo_root) / "manager" / "server.py"


def _desktop() -> Path:
    return Path(os.path.expanduser("~")) / "Desktop"


# --- Windows -----------------------------------------------------------------

def _create_windows(repo_root: Path, py: str, server: Path, dry_run: bool) -> dict:
    lnk = _desktop() / f"{SHORTCUT_NAME}.lnk"
    report = {"os": "windows", "path": str(lnk), "target": f"{py} {server}"}
    if dry_run:
        report["dry_run"] = True
        return report
    # WScript.Shell single-quotes the args; Windows user paths effectively never
    # contain a literal single quote, so this is safe in practice. Arguments wraps
    # the script path in double quotes so a path with spaces survives.
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{lnk}'); "
        f"$s.TargetPath = '{py}'; "
        f"$s.Arguments = '\"{server}\"'; "
        f"$s.WorkingDirectory = '{repo_root}'; "
        f"$s.IconLocation = '{py},0'; "
        f"$s.Description = '{DESCRIPTION}'; "
        "$s.Save()"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        report["created"] = r.returncode == 0 and lnk.exists()
        if not report["created"]:
            report["error"] = (r.stderr or r.stdout or "powershell create failed").strip()[:300]
    except Exception as e:
        report["created"] = False
        report["error"] = f"{type(e).__name__}: {e}"
    return report


# --- macOS -------------------------------------------------------------------

def _create_macos(repo_root: Path, py: str, server: Path, dry_run: bool) -> dict:
    path = _desktop() / f"{SHORTCUT_NAME}.command"
    content = f'#!/bin/bash\nexec "{py}" "{server}"\n'
    report = {"os": "darwin", "path": str(path)}
    if dry_run:
        report["dry_run"] = True
        report["content"] = content
        return report
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o755)
        report["created"] = True
    except Exception as e:
        report["created"] = False
        report["error"] = f"{type(e).__name__}: {e}"
    return report


# --- Linux -------------------------------------------------------------------

def _create_linux(repo_root: Path, py: str, server: Path, dry_run: bool) -> dict:
    path = Path(os.path.expanduser("~")) / ".local" / "share" / "applications" / "kb-manager.desktop"
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={SHORTCUT_NAME}\n"
        f"Comment={DESCRIPTION}\n"
        f'Exec="{py}" "{server}"\n'
        "Terminal=true\n"
        "Icon=utilities-terminal\n"
        "Categories=Development;\n"
    )
    report = {"os": "linux", "path": str(path)}
    if dry_run:
        report["dry_run"] = True
        report["content"] = content
        return report
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o755)
        report["created"] = True
    except Exception as e:
        report["created"] = False
        report["error"] = f"{type(e).__name__}: {e}"
    return report


# --- Dispatch ----------------------------------------------------------------

def create_shortcut(repo_root, python_exe: str | None = None, dry_run: bool = False) -> dict:
    """Create the "KB Manager" shortcut for the current OS. `python_exe` defaults to
    the running interpreter (the one the installer resolved), so the shortcut launches
    the manager with the same Python that installed it."""
    repo_root = Path(repo_root)
    py = python_exe or sys.executable or "python"
    server = _manager_server(repo_root)
    plat = sys.platform
    if plat.startswith("win"):
        return _create_windows(repo_root, py, server, dry_run)
    if plat == "darwin":
        return _create_macos(repo_root, py, server, dry_run)
    return _create_linux(repo_root, py, server, dry_run)
