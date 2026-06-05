#!/usr/bin/env python3
"""Register a "KB Manager" entry in the OS app menu/search, per OS.

After the one-time install, a teammate opens the config UI by searching their apps
(Win key / Spotlight / app launcher) and hitting Enter — not by typing a terminal
command. We deliberately do NOT drop an icon on the Desktop (intrusive); the user
pins it or makes a Desktop shortcut themselves if they want. The entry launches the
manager (a local web server that opens the browser itself on start).

Registered where each OS's search looks, so the model is identical everywhere
("it's in your apps, search for 'kb'"):
  - Windows: a .lnk in the Start Menu Programs folder (WScript.Shell COM, via PowerShell)
  - macOS:   a minimal KB Manager.app bundle in ~/Applications (Spotlight-indexed)
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


def _start_menu() -> Path:
    """Windows Start Menu 'Programs' folder (what Win-key search indexes)."""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path(os.path.expanduser("~")) / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs"


# --- Windows -----------------------------------------------------------------

def _create_windows(repo_root: Path, py: str, server: Path, dry_run: bool) -> dict:
    lnk = _start_menu() / f"{SHORTCUT_NAME}.lnk"
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
        lnk.parent.mkdir(parents=True, exist_ok=True)
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

def _macos_launcher(py: str, server: Path) -> str:
    return f'#!/bin/bash\nexec "{py}" "{server}"\n'


def _macos_plist() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        '  <key>CFBundleName</key><string>KB Manager</string>\n'
        '  <key>CFBundleDisplayName</key><string>KB Manager</string>\n'
        '  <key>CFBundleIdentifier</key><string>dev.kb.manager</string>\n'
        '  <key>CFBundleExecutable</key><string>kb-manager</string>\n'
        '  <key>CFBundlePackageType</key><string>APPL</string>\n'
        '  <key>CFBundleVersion</key><string>1.0</string>\n'
        '</dict></plist>\n'
    )


def _create_macos(repo_root: Path, py: str, server: Path, dry_run: bool) -> dict:
    # A minimal .app bundle in ~/Applications so Spotlight indexes it as an app
    # (a bare .command isn't surfaced by Cmd+Space). Bundle = a dir with an
    # executable in Contents/MacOS + Info.plist.
    app = Path(os.path.expanduser("~")) / "Applications" / f"{SHORTCUT_NAME}.app"
    launcher = _macos_launcher(py, server)
    report = {"os": "darwin", "path": str(app)}
    if dry_run:
        report["dry_run"] = True
        report["content"] = launcher
        return report
    try:
        macos_dir = app / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True, exist_ok=True)
        exe = macos_dir / "kb-manager"
        exe.write_text(launcher, encoding="utf-8")
        os.chmod(exe, 0o755)
        (app / "Contents" / "Info.plist").write_text(_macos_plist(), encoding="utf-8")
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
