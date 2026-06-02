#!/usr/bin/env python3
"""Tests for kb_config's write surface — the cardinal-rule path.

A bad config write poisons retrieval silently, so these lock the guarantees:
validate refuses typo'd/partial input, write is load-merge (never clobbers the
load-bearing branch sets), and an unparseable existing file is refused, not
overwritten.

Run: python engine/kb_config_test.py
Synthetic data only (no real vault, no real identifiers). Redirects HOME to a temp.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_config  # noqa: E402
from kb_config import validate_config_update, write_config, KBConfigError  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def test_validate():
    print("test_validate")
    with tempfile.TemporaryDirectory() as d:
        vault = Path(d) / "vault"; vault.mkdir()
        repo = Path(d) / "repos"; repo.mkdir()
        check("valid vault passes", validate_config_update({"vault": str(vault)}) == [])
        check("missing vault dir fails", validate_config_update({"vault": str(Path(d) / "nope")}) != [])
        check("empty vault fails", validate_config_update({"vault": "   "}) != [])
        check("valid workspaces pass", validate_config_update({"workspaces": [{"name": "ws", "path": str(repo)}]}) == [])
        check("workspace bad path fails", validate_config_update({"workspaces": [{"name": "ws", "path": str(Path(d) / "x")}]}) != [])
        check("workspace empty name fails", validate_config_update({"workspaces": [{"name": "", "path": str(repo)}]}) != [])
        check("branch set ok", validate_config_update({"production_branches": ["main", "master"]}) == [])
        check("empty branch set fails", validate_config_update({"production_branches": []}) != [])
        check("since_hours positive ok", validate_config_update({"since_hours": 24}) == [])
        check("since_hours zero fails", validate_config_update({"since_hours": 0}) != [])
        check("bool is not a valid int", validate_config_update({"max_turns": True}) != [])
        check("unknown key fails", validate_config_update({"banana": 1}) != [])


def _with_home(d):
    prev = os.environ.get("HOME")
    os.environ["HOME"] = str(d)
    return prev


def _restore_home(prev):
    if prev is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = prev


def test_write_merges_and_preserves():
    print("test_write_merges_and_preserves")
    with tempfile.TemporaryDirectory() as d:
        prev = _with_home(Path(d))
        try:
            vault = Path(d) / "vault"; vault.mkdir()
            cfgpath = kb_config.workspaces_path()
            cfgpath.parent.mkdir(parents=True, exist_ok=True)
            # Pre-existing config with load-bearing keys the form does NOT know about.
            cfgpath.write_text(json.dumps({
                "vault": "/old/vault",
                "production_branches": ["main"],
                "since_hours": 48,
            }), encoding="utf-8")
            merged = write_config({"vault": str(vault)})
            check("vault updated", merged["vault"] == str(vault))
            check("production_branches preserved", merged["production_branches"] == ["main"])
            check("since_hours preserved", merged["since_hours"] == 48)
            on_disk = json.loads(cfgpath.read_text(encoding="utf-8"))
            check("disk matches merged", on_disk == merged)
            check("no temp file left", not cfgpath.with_name(cfgpath.name + ".kb-tmp").exists())
        finally:
            _restore_home(prev)


def test_write_creates_when_absent():
    print("test_write_creates_when_absent")
    with tempfile.TemporaryDirectory() as d:
        prev = _with_home(Path(d))
        try:
            vault = Path(d) / "vault"; vault.mkdir()
            merged = write_config({"vault": str(vault)})
            check("created file with vault", kb_config.workspaces_path().exists() and merged["vault"] == str(vault))
        finally:
            _restore_home(prev)


def test_write_refuses_invalid():
    print("test_write_refuses_invalid")
    with tempfile.TemporaryDirectory() as d:
        prev = _with_home(Path(d))
        try:
            raised = False
            try:
                write_config({"vault": str(Path(d) / "does-not-exist")})
            except KBConfigError:
                raised = True
            check("refused invalid vault", raised)
            check("no file written", not kb_config.workspaces_path().exists())
        finally:
            _restore_home(prev)


def test_write_refuses_malformed_existing():
    print("test_write_refuses_malformed_existing")
    with tempfile.TemporaryDirectory() as d:
        prev = _with_home(Path(d))
        try:
            vault = Path(d) / "vault"; vault.mkdir()
            cfgpath = kb_config.workspaces_path()
            cfgpath.parent.mkdir(parents=True, exist_ok=True)
            garbage = '{ "vault": broken'
            cfgpath.write_text(garbage, encoding="utf-8")
            raised = False
            try:
                write_config({"vault": str(vault)})
            except KBConfigError:
                raised = True
            check("refused malformed existing", raised)
            check("malformed file untouched", cfgpath.read_text(encoding="utf-8") == garbage)
        finally:
            _restore_home(prev)


def main():
    test_validate()
    test_write_merges_and_preserves()
    test_write_creates_when_absent()
    test_write_refuses_invalid()
    test_write_refuses_malformed_existing()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
