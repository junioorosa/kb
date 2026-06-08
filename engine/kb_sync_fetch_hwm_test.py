#!/usr/bin/env python3
"""Tests for the read-only integration-only fetch + the exact tip==HWM walk skip.

Two changes under test (kb-sync):
  1. fetch_repo(repo, names): refresh ONLY the named integration/production refs by
     EXACT refspec (no all-heads wildcard, no --prune). Proves it updates the named
     ref and does NOT create mirror refs for other remote branches — which is what
     sidesteps the case-insensitive-filesystem ref collision and avoids deleting
     anything locally. Unreachable origin -> (False, reason), never a hang.
  2. branch_unchanged_since_hwm(...): skip a branch iff its tip == stored HWM sha.
     The regression guard: an old-but-never-captured commit (tip != HWM) must STILL
     be walked, so a sync that was down for days can't silently drop work. (A date
     horizon would fail exactly this case.)

Run: python engine/kb_sync_fetch_hwm_test.py
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("kb_sync_under_test", HERE / "kb-sync.py")
kb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kb)

EMAIL = "dev@x.com"
ORIGIN_NORM = "test-origin"
PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def git(repo, *args, date=None):
    env = {**os.environ}
    if date:
        env.update({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args],
                          cwd=repo, capture_output=True, text=True, env=env)


def commit(repo, path, content, msg, date=None):
    (repo / path).write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg, date=date)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "master")
    git(path, "config", "user.email", EMAIL)
    git(path, "config", "user.name", "Dev")
    return path


def test_fetch_integration_only():
    print("test_fetch_integration_only")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        origin = init_repo(root / "origin")
        commit(origin, "a.txt", "base\n", "init on master")

        # Clone while origin has ONLY master -> the clone mirrors just origin/master.
        clone = root / "clone"
        git(root, "clone", "-q", str(origin), str(clone))

        # After clone, origin gains a NEW master commit AND a new feature branch.
        new_master = commit(origin, "a.txt", "v2\n", "advance master")
        git(origin, "checkout", "-q", "-b", "Feat/40636_x")
        commit(origin, "f.txt", "feat\n", "feature work")
        git(origin, "checkout", "-q", "master")

        # Fetch ONLY integration/production names. dev doesn't exist -> non-fatal.
        ok, reason = kb.fetch_repo(clone, ["master", "dev"])
        check("returns ok when an integration ref fetched", ok is True)

        got_master = git(clone, "rev-parse", "refs/remotes/origin/master").stdout.strip()
        check("origin/master refreshed to the new tip", got_master == new_master)

        # The feature branch added on origin must NOT have a local mirror: we never
        # fetched it. This is the property that avoids the case-collision + churn.
        feat_ref = git(clone, "rev-parse", "--verify", "refs/remotes/origin/Feat/40636_x")
        check("feature branch NOT mirrored (only named refs fetched)", feat_ref.returncode != 0)

        # A missing branch (dev) alone is not a failure.
        check("missing 'dev' alone is non-fatal", ok is True)


def test_fetch_unreachable_is_fast_failure():
    print("test_fetch_unreachable_is_fast_failure")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        origin = init_repo(root / "origin")
        commit(origin, "a.txt", "base\n", "init")
        clone = root / "clone"
        git(root, "clone", "-q", str(origin), str(clone))

        # Point origin at a path that doesn't exist -> fetch must fail (not hang) and
        # report a reason, never raise.
        git(clone, "remote", "set-url", "origin", str(root / "gone" / "nope.git"))
        ok, reason = kb.fetch_repo(clone, ["master"])
        check("unreachable origin -> ok False", ok is False)
        check("unreachable origin -> non-empty reason", bool(reason))


def test_branch_unchanged_since_hwm():
    print("test_branch_unchanged_since_hwm")
    with tempfile.TemporaryDirectory() as d:
        repo = init_repo(Path(d) / "repo")
        commit(repo, "a.txt", "base\n", "init")
        git(repo, "checkout", "-q", "-b", "feat/x")
        c1 = commit(repo, "x.txt", "one\n", "step1", date="2026-05-01T10:00:00")

        state = {}
        # No HWM recorded yet -> never skip (must walk).
        check("no HWM -> walk", kb.branch_unchanged_since_hwm(state, ORIGIN_NORM, "feat/x", repo) is False)

        # HWM == tip -> skip (zero new commits since last capture).
        state = {"last_processed_commit": {ORIGIN_NORM: {"feat/x": c1}}}
        check("tip == HWM -> skip", kb.branch_unchanged_since_hwm(state, ORIGIN_NORM, "feat/x", repo) is True)

        # New commit -> tip != HWM -> walk.
        commit(repo, "x.txt", "two\n", "step2", date="2026-05-02T10:00:00")
        check("new commit -> walk", kb.branch_unchanged_since_hwm(state, ORIGIN_NORM, "feat/x", repo) is False)

        # REGRESSION GUARD (sync-was-down): HWM stuck at c1, the only new commit is
        # OLD (dated 8 days before "now"). A date horizon would drop it; the exact
        # sha check must still walk it.
        old = commit(repo, "x.txt", "stale\n", "old-but-uncaptured", date="2026-05-01T10:00:00")
        state = {"last_processed_commit": {ORIGIN_NORM: {"feat/x": c1}}}
        check("old-but-uncaptured commit (tip != HWM) -> walk, not dropped",
              kb.branch_unchanged_since_hwm(state, ORIGIN_NORM, "feat/x", repo) is False and old != c1)


def test_date_floor_skip():
    print("test_date_floor_skip")
    with tempfile.TemporaryDirectory() as d:
        repo = init_repo(Path(d) / "repo")
        commit(repo, "a.txt", "base\n", "init")
        git(repo, "checkout", "-q", "-b", "feat/x")
        commit(repo, "x.txt", "old\n", "old work", date="2026-05-01T10:00:00")

        # No SHA-HWM, no date floor -> walk (must examine).
        check("no HWM + no date -> walk",
              kb.branch_skippable({}, ORIGIN_NORM, "feat/x", repo) is False)
        # last_examined_at AFTER the tip's date -> already seen -> skip.
        st = {"last_examined_at": {ORIGIN_NORM: "2026-05-10"}}
        check("tip date < last_examined -> skip", kb.branch_skippable(st, ORIGIN_NORM, "feat/x", repo) is True)
        # last_examined_at BEFORE the tip's date -> new since last exam -> walk.
        st = {"last_examined_at": {ORIGIN_NORM: "2026-04-25"}}
        check("tip date >= last_examined -> walk", kb.branch_skippable(st, ORIGIN_NORM, "feat/x", repo) is False)
        # SHA-HWM exact match still skips even with no date floor.
        tip = git(repo, "rev-parse", "feat/x").stdout.strip()
        st = {"last_processed_commit": {ORIGIN_NORM: {"feat/x": tip}}}
        check("tip == SHA-HWM -> skip (exact)", kb.branch_skippable(st, ORIGIN_NORM, "feat/x", repo) is True)


def test_effective_since_floor():
    print("test_effective_since_floor")
    with tempfile.TemporaryDirectory() as d:
        repo = init_repo(Path(d) / "repo")
        commit(repo, "a.txt", "base\n", "init")
        git(repo, "checkout", "-q", "-b", "feat/x")
        c1 = commit(repo, "x.txt", "one\n", "step1")

        # No SHA-HWM but a date recorded -> window floor IS last_examined_at.
        st = {"last_examined_at": {ORIGIN_NORM: "2026-05-10"}}
        s = kb.effective_since(st, ORIGIN_NORM, "feat/x", repo)
        check("floor uses last_examined_at (not cap-7d)",
              s["kind"] == "date" and s["iso"] == "2026-05-10" and s["source"] == "last-examined")

        # SHA-HWM present (ancestor) -> commit window, precedence over the date.
        st = {"last_processed_commit": {ORIGIN_NORM: {"feat/x": c1}},
              "last_examined_at": {ORIGIN_NORM: "2026-05-10"}}
        s = kb.effective_since(st, ORIGIN_NORM, "feat/x", repo)
        check("SHA-HWM precedence (commit window)", s["kind"] == "commit" and s["sha"] == c1)

        # Neither -> first-run bootstrap date.
        s = kb.effective_since({"installed_at": "2026-05-01"}, ORIGIN_NORM, "feat/x", repo)
        check("no date -> bootstrap", s["kind"] == "date" and s["source"] in ("bootstrap", "cap-7d-truncated"))


def test_two_run_and_error_gate():
    print("test_two_run_and_error_gate")
    # The advance gate: only (fetched_ok - incomplete) origins seal the date.
    state = {}
    sealed = kb.advance_examined_dates(state, {"A", "B"}, {"B"}, "2026-06-08")
    check("clean origin A sealed", sealed == {"A"})
    check("A date recorded", state["last_examined_at"]["A"] == "2026-06-08")
    check("errored origin B held back (no date)", "B" not in state.get("last_examined_at", {}))

    # Consequence on a real old-tip branch: run2 after the seal.
    with tempfile.TemporaryDirectory() as d:
        repo = init_repo(Path(d) / "repo")
        commit(repo, "a.txt", "base\n", "init")
        git(repo, "checkout", "-q", "-b", "feat/x")
        commit(repo, "x.txt", "old\n", "old", date="2026-05-01T10:00:00")
        # Sealed origin A: its unchanged old branch is now skipped.
        check("sealed origin -> old branch skipped next run",
              kb.branch_skippable(state, "A", "feat/x", repo) is True)
        # Held-back origin B (error this run): re-examined, NOT sealed -> not dropped.
        check("held-back origin -> branch re-examined (regression guard)",
              kb.branch_skippable(state, "B", "feat/x", repo) is False)


def main():
    test_fetch_integration_only()
    test_fetch_unreachable_is_fast_failure()
    test_branch_unchanged_since_hwm()
    test_date_floor_skip()
    test_effective_since_floor()
    test_two_run_and_error_gate()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
