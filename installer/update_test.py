#!/usr/bin/env python3
"""Tests for the remote-aware update path (install.update_check / update_apply).

What's under test — the safety-critical contract:
  * update_check only READS the remote (git fetch) and is fail-soft (offline ->
    a reason, never a raise). It flags an update iff the remote VERSION is a
    higher semver than the local one.
  * update_apply is fast-forward-only and refuses a dirty OR diverged tree, so it
    can never clobber local work. The happy path fast-forwards then re-deploys,
    skipping scheduler + shortcut (so the user's configured sync time is untouched).

These run git against throwaway temp repos and monkeypatch install.REPO_ROOT so a
real clone is never touched. The deploy step is stubbed in the happy path (we're
testing the git/guard logic here, not deploy.apply — that has its own tests).

Run: python installer/update_test.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import install  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def git(repo, *args):
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args],
                          cwd=repo, capture_output=True, text=True)


def init_origin(path, version="0.1.0"):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "dev@x.com")
    git(path, "config", "user.name", "Dev")
    (path / "VERSION").write_text(version + "\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return path


def bump(repo, version, msg="release"):
    (repo / "VERSION").write_text(version + "\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)


def clone(origin, dest):
    git(origin.parent, "clone", "-q", str(origin), str(dest))
    git(dest, "config", "user.email", "dev@x.com")
    git(dest, "config", "user.name", "Dev")
    return dest


class repo_root:
    """Point install at a temp clone for the duration of a test."""
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self._orig = install.REPO_ROOT
        install.REPO_ROOT = self.path
        return self

    def __exit__(self, *a):
        install.REPO_ROOT = self._orig


def test_semver_ordering():
    print("test_semver_ordering")
    check("0.10.0 > 0.9.0 (numeric, not lexical)", install._semver("0.10.0") > install._semver("0.9.0"))
    check("v-prefix parsed", install._semver("v1.2.3") == (1, 2, 3))
    check("short padded", install._semver("2") == (2, 0, 0))
    check("equal", install._semver("0.1.0") == install._semver("0.1.0"))


def test_update_check_detects_newer():
    print("test_update_check_detects_newer")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        origin = init_origin(root / "origin", "0.1.0")
        cl = clone(origin, root / "clone")
        bump(origin, "0.2.0")  # remote advances AFTER the clone
        with repo_root(cl):
            u = install.update_check()
        check("checked ok", u["checked"] is True)
        check("local stays 0.1.0 (fetch doesn't touch worktree)", u["local_version"] == "0.1.0")
        check("remote seen as 0.2.0", u["remote_version"] == "0.2.0")
        check("update_available True", u["update_available"] is True)
        check("no reason on success", u["reason"] is None)


def test_update_check_up_to_date():
    print("test_update_check_up_to_date")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        origin = init_origin(root / "origin", "0.1.0")
        cl = clone(origin, root / "clone")
        with repo_root(cl):
            u = install.update_check()
        check("checked ok", u["checked"] is True)
        check("update_available False", u["update_available"] is False)


def test_update_check_offline_failsoft():
    print("test_update_check_offline_failsoft")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        origin = init_origin(root / "origin", "0.1.0")
        cl = clone(origin, root / "clone")
        git(cl, "remote", "set-url", "origin", str(root / "gone" / "nope.git"))
        with repo_root(cl):
            u = install.update_check()  # must NOT raise
        check("checked False (couldn't reach remote)", u["checked"] is False)
        check("reason populated", bool(u["reason"]))
        check("update_available stays False", u["update_available"] is False)


def test_update_apply_refuses_dirty():
    print("test_update_apply_refuses_dirty")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        origin = init_origin(root / "origin", "0.1.0")
        cl = clone(origin, root / "clone")
        bump(origin, "0.2.0")
        (cl / "VERSION").write_text("0.1.0-dirty\n", encoding="utf-8")  # uncommitted change
        with repo_root(cl):
            r = install.update_apply()
        check("refused", r["updated"] is False)
        check("reason mentions uncommitted", "uncommitted" in (r["reason"] or "").lower())


def test_update_apply_refuses_diverged():
    print("test_update_apply_refuses_diverged")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        origin = init_origin(root / "origin", "0.1.0")
        cl = clone(origin, root / "clone")
        bump(origin, "0.2.0")               # remote moves ahead
        bump(cl, "0.1.1", "local work")     # local commits too -> diverged, clean tree
        with repo_root(cl):
            r = install.update_apply()
        check("refused (no fast-forward)", r["updated"] is False)
        check("reason mentions fast-forward", "fast-forward" in (r["reason"] or "").lower())
        # The local commit must survive (never clobbered).
        check("local VERSION untouched", (cl / "VERSION").read_text().strip() == "0.1.1")


def test_update_apply_happy_ff_then_deploy():
    print("test_update_apply_happy_ff_then_deploy")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        origin = init_origin(root / "origin", "0.1.0")
        cl = clone(origin, root / "clone")
        bump(origin, "0.2.0")
        captured = {}

        def fake_run(apply, time_hhmm, scheduler_apply=None, shortcut_apply=None):
            captured.update(apply=apply, scheduler_apply=scheduler_apply, shortcut_apply=shortcut_apply)
            return {"deploy": {"wrote": 3, "backup_dir": str(root / "bk")}}

        orig_run = install.run
        install.run = fake_run
        try:
            with repo_root(cl):
                r = install.update_apply()
        finally:
            install.run = orig_run

        check("updated True", r["updated"] is True)
        check("from 0.1.0", r["from"] == "0.1.0")
        check("to 0.2.0 (worktree fast-forwarded)", r["to"] == "0.2.0")
        check("clone VERSION on disk is 0.2.0", (cl / "VERSION").read_text().strip() == "0.2.0")
        check("deploy report surfaced", r["backup_dir"] == str(root / "bk"))
        check("apply=True passed to run", captured.get("apply") is True)
        check("scheduler NOT touched", captured.get("scheduler_apply") is False)
        check("shortcut NOT touched", captured.get("shortcut_apply") is False)
        check("restart note present", "restart" in (r["note"] or "").lower())


def main():
    test_semver_ordering()
    test_update_check_detects_newer()
    test_update_check_up_to_date()
    test_update_check_offline_failsoft()
    test_update_apply_refuses_dirty()
    test_update_apply_refuses_diverged()
    test_update_apply_happy_ff_then_deploy()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
