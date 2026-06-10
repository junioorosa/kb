#!/usr/bin/env python3
"""Tests for the manager's folder-picker backend (fs_list / fs_mkdir).

Pure-function tests against a temp tree — no HTTP server, no real home browsed
beyond the default-path check. Run: python manager/server_fs_test.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "alpha").mkdir()
        (root / "Beta").mkdir()
        (root / ".hidden-dir").mkdir()
        (root / "a-file.txt").write_text("x", encoding="utf-8")

        print("test_fs_list")
        r = server.fs_list(str(root))
        check("lists the resolved path", Path(r["path"]) == root.resolve())
        check("dirs only — files excluded", "a-file.txt" not in r["dirs"])
        check("dot-dirs included (vaults live in ~/.kb)", ".hidden-dir" in r["dirs"])
        check("case-insensitive sort", r["dirs"] == sorted(r["dirs"], key=str.lower))
        check("parent points one level up", Path(r["parent"]) == root.resolve().parent)
        check("home is reported", r["home"] == str(Path.home()))
        check("at least one root", isinstance(r["roots"], list) and len(r["roots"]) >= 1)

        r = server.fs_list(None)
        check("no path defaults to home", Path(r["path"]) == Path.home().resolve())

        r = server.fs_list(str(root / "a-file.txt"))
        check("file path refused", "error" in r)
        r = server.fs_list(str(root / "nope"))
        check("missing path refused", "error" in r)

        roots = server.fs_list(str(root))["roots"]
        top = server.fs_list(roots[0])
        check("filesystem root has no parent", top.get("parent") is None or "error" in top)

        print("test_fs_mkdir")
        r = server.fs_mkdir(str(root), "new-vault")
        check("creates the folder", "created" in r and (root / "new-vault").is_dir())
        r = server.fs_mkdir(str(root), "new-vault")
        check("refuses an existing name", "error" in r)
        for bad in ("", "..", "a/b", "a\\b", "x:y", "  "):
            r = server.fs_mkdir(str(root), bad)
            check(f"refuses invalid name {bad!r}", "error" in r)
        r = server.fs_mkdir(str(root / "nope"), "x")
        check("refuses a missing parent", "error" in r)
        r = server.fs_mkdir(str(root / "a-file.txt"), "x")
        check("refuses a file as parent", "error" in r)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
