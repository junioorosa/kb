#!/usr/bin/env python3
"""Tests for the per-OS "KB Manager" shortcut creation.

dry_run is asserted on every platform (no side effects). A real create is exercised
for the host OS into a temp HOME/USERPROFILE so it never touches the real Desktop.

Run: python installer/shortcut_test.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shortcut  # noqa: E402

PASS, FAIL = 0, 0
REPO = Path(__file__).resolve().parent.parent  # repo root (manager/server.py lives here)


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def test_dry_run():
    print("test_dry_run")
    # Isolate HOME so the asserted path is clean temp space — otherwise "nothing
    # created" would read the real Desktop, which a live install may have populated.
    prev = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
    with tempfile.TemporaryDirectory() as d:
        os.environ["USERPROFILE"] = d
        os.environ["HOME"] = d
        try:
            rep = shortcut.create_shortcut(REPO, dry_run=True)
            check("dry_run flagged", rep.get("dry_run") is True)
            check("os reported", rep.get("os") in ("windows", "darwin", "linux"))
            check("path points at a KB Manager artifact", "kb" in rep["path"].lower() and
                  ("KB Manager" in rep["path"] or "kb-manager" in rep["path"]))
            server = str(REPO / "manager" / "server.py")
            blob = rep.get("target", "") + rep.get("content", "")
            check("artifact references manager/server.py", server in blob)
            check("nothing created on dry_run", not Path(rep["path"]).exists())
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def test_real_create():
    print("test_real_create")
    prev = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        (home / "Desktop").mkdir(parents=True, exist_ok=True)
        os.environ["USERPROFILE"] = str(home)
        os.environ["HOME"] = str(home)
        try:
            rep = shortcut.create_shortcut(REPO, dry_run=False)
            check("create reported success", rep.get("created") is True)
            if rep.get("created"):
                p = Path(rep["path"])
                check("shortcut file exists", p.exists())
                check("shortcut is under the temp home", str(home) in str(p))
                check("shortcut non-empty", p.exists() and p.stat().st_size > 0)
                if sys.platform != "win32":
                    # script-based shortcuts embed the server path verbatim and are executable
                    check("script references server.py", "manager" in p.read_text(encoding="utf-8"))
                    check("script is executable", os.access(p, os.X_OK))
            else:
                print(f"    (create error: {rep.get('error')})")
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def main():
    test_dry_run()
    test_real_create()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
