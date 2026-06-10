#!/usr/bin/env python3
"""Tests for the merged-ticketless backfill path in kb-sync.

The bug it fixes: a branch committed AND merged within one sync interval is invisible
to both passes — capture finds zero own-commits (already integration-reachable, excluded
by `--not <integration>`) and finalize has no ticket to resolve. These tests build a real
temp git repo with exactly that shape and assert the backfill detects it and mines a
NON-EMPTY diff that contains the branch's actual change (not just that detection fired).

Run: python engine/kb_sync_backfill_test.py
"""

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("kb_sync_under_test", HERE / "kb-sync.py")
kb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kb)

EMAIL = "dev@x.com"
SINCE = {"kind": "date", "iso": "2000-01-01", "source": "test"}
CHANGE_MARKER = "ALTERNATE_AISLE_PICKING_CHANGE"

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def git(repo, *args, author=None, date=None):
    env = None
    if author or date:
        import os
        env = {**os.environ}
        if author:
            env.update({"GIT_AUTHOR_EMAIL": author, "GIT_AUTHOR_NAME": author,
                        "GIT_COMMITTER_EMAIL": EMAIL, "GIT_COMMITTER_NAME": "Committer"})
        if date:
            env.update({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args],
                          cwd=repo, capture_output=True, text=True, env=env)


def commit(repo, path, content, msg, author=None, date=None):
    (repo / path).write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg, author=author, date=date)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def build_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    git(repo, "config", "user.email", EMAIL)
    git(repo, "config", "user.name", "Dev")
    commit(repo, "a.txt", "base\n", "init")
    git(repo, "checkout", "-q", "-b", "dev")

    # MERGED branch: own commit, then merged back into dev via a no-ff merge (PR-style),
    # so its commit is reachable from dev -> normal capture (--not dev) finds nothing.
    git(repo, "checkout", "-q", "-b", "fix/foo")
    commit(repo, "feature.txt", CHANGE_MARKER + "\n", "fix: route picking through the alternate aisle")
    git(repo, "checkout", "-q", "dev")
    git(repo, "merge", "--no-ff", "-q", "fix/foo", "-m", "Merged in fix/foo (PR #1)")

    # UNMERGED branch: own commit NOT on dev -> normal capture handles it (no backfill).
    git(repo, "checkout", "-q", "dev")
    git(repo, "checkout", "-q", "-b", "fix/bar")
    commit(repo, "bar.txt", "wip\n", "fix: bar wip")

    # MERGED branch authored by SOMEONE ELSE -> nothing for THIS author to mine.
    git(repo, "checkout", "-q", "dev")
    git(repo, "checkout", "-q", "-b", "fix/baz")
    commit(repo, "baz.txt", "other\n", "fix: baz", author="other@x.com")
    git(repo, "checkout", "-q", "dev")
    git(repo, "merge", "--no-ff", "-q", "fix/baz", "-m", "Merged in fix/baz (PR #2)")

    git(repo, "checkout", "-q", "fix/foo")
    return repo


def write_ticket(vault: Path, ws: str, proj: str, branch: str):
    tdir = vault / ws / proj / branch  # branch "fix/foo" -> nested fix/foo dir
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "_index.md").write_text(
        f"---\nproject: {proj}\nslug: foo\nstatus: open\nbranch: {branch}\n---\nbody\n",
        encoding="utf-8")


def test_backfill():
    print("test_backfill")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        repo = build_repo(root)
        vault = root / "vault"
        ws, proj = "WS", "repo"
        intrefs = ["dev"]

        # --- the trigger condition: normal capture is blind to the merged branch ---
        normal = kb.author_commits_for_branch(repo, "fix/foo", EMAIL, SINCE, intrefs)
        check("normal capture finds 0 own-commits on merged branch", normal == [])

        # --- backfill detects it and returns (commits, base) to mine ---
        bf = kb.merged_ticketless_backfill(repo, "fix/foo", EMAIL, SINCE, intrefs, vault, ws, proj)
        check("backfill detected (ticketless merged)", bool(bf))
        commits, mbase = bf
        check("merge-commit base resolved (exact own-range, no bleed)", bool(mbase))

        # --- the mined diff is NON-EMPTY and contains the real change (core requirement) ---
        diff = kb.author_landed_diff(repo, "fix/foo", EMAIL, SINCE, base=mbase)
        check("mined diff is non-empty", bool(diff.strip()))
        check("mined diff contains the actual change", CHANGE_MARKER in diff)
        check("mined diff has NO trunk bleed (init file absent)", "a.txt" not in diff)
        stat = kb.author_landed_stat(repo, "fix/foo", EMAIL, SINCE, base=mbase)
        check("mined stat is non-empty", "feature.txt" in stat)

        # --- negative: a ticket already exists -> not our gap ---
        write_ticket(vault, ws, proj, "fix/foo")
        check("backfill suppressed once a ticket exists",
              kb.merged_ticketless_backfill(repo, "fix/foo", EMAIL, SINCE, intrefs, vault, ws, proj) is None)

        # --- negative: unmerged branch -> normal capture handles it, no backfill ---
        check("unmerged branch is NOT a backfill candidate",
              kb.merged_ticketless_backfill(repo, "fix/bar", EMAIL, SINCE, intrefs, vault, ws, proj) is None)
        check("unmerged branch IS a normal-capture candidate",
              kb.author_commits_for_branch(repo, "fix/bar", EMAIL, SINCE, intrefs) != [])

        # --- negative: merged branch authored by someone else -> nothing to mine ---
        check("merged branch by other author is NOT a backfill candidate",
              kb.merged_ticketless_backfill(repo, "fix/baz", EMAIL, SINCE, intrefs, vault, ws, proj) is None)


WINDOW = {"kind": "date", "iso": "2026-05-29", "source": "t"}  # ~7d window for the dated cases


def build_window_repo(root: Path) -> Path:
    """Repo with DATED merges to exercise the window + HWM bounding (the mass-re-fire bug)."""
    repo = root / "wrepo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    git(repo, "config", "user.email", EMAIL)
    git(repo, "config", "user.name", "Dev")
    commit(repo, "a.txt", "base\n", "init", date="2025-12-01T10:00:00")
    git(repo, "checkout", "-q", "-b", "dev")
    # merged long ago (pre-window)
    git(repo, "checkout", "-q", "-b", "fix/old")
    commit(repo, "old.txt", "OLD_CHANGE\n", "fix: old", date="2026-01-01T10:00:00")
    git(repo, "checkout", "-q", "dev"); git(repo, "merge", "--no-ff", "-q", "fix/old", "-m", "Merged fix/old")
    # merged recently (in-window)
    git(repo, "checkout", "-q", "-b", "fix/recent", "dev")
    commit(repo, "recent.txt", "RECENT_CHANGE\n", "fix: recent", date="2026-06-01T10:00:00")
    git(repo, "checkout", "-q", "dev"); git(repo, "merge", "--no-ff", "-q", "fix/recent", "-m", "Merged fix/recent")
    # two commits, for HWM-partial (HWM at step1 -> only step2 is new)
    git(repo, "checkout", "-q", "-b", "fix/twostep", "dev")
    commit(repo, "t1.txt", "STEP_ONE\n", "fix: step1", date="2026-06-01T10:00:00")
    commit(repo, "t2.txt", "STEP_TWO\n", "fix: step2", date="2026-06-02T10:00:00")
    git(repo, "checkout", "-q", "dev"); git(repo, "merge", "--no-ff", "-q", "fix/twostep", "-m", "Merged fix/twostep")
    git(repo, "checkout", "-q", "fix/recent")
    return repo


def test_window_and_hwm():
    print("test_window_and_hwm")
    bf = kb.merged_ticketless_backfill
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        repo = build_window_repo(root)
        vault, ws, proj, ir = root / "vault", "W", "p", ["dev"]

        # Bug A: a merge whose commits are all OLDER than the window must NOT backfill.
        check("pre-window merge NOT backfilled (date bound)",
              bf(repo, "fix/old", EMAIL, WINDOW, ir, vault, ws, proj) is None)

        # In-window merge DOES backfill, mining only the in-window change.
        r = bf(repo, "fix/recent", EMAIL, WINDOW, ir, vault, ws, proj)
        check("in-window merge backfilled", bool(r))
        if r:
            _, base = r
            diff = kb.author_landed_diff(repo, "fix/recent", EMAIL, WINDOW, base=base)
            check("mines the in-window change", "RECENT_CHANGE" in diff)
            check("does not reach the pre-window change", "OLD_CHANGE" not in diff)

        # Bug B: a fully-processed branch (HWM == tip) must NOT re-backfill.
        tip = git(repo, "rev-parse", "fix/recent").stdout.strip()
        hwm_tip = {"kind": "commit", "sha": tip, "source": "hwm"}
        check("HWM==tip NOT re-backfilled",
              bf(repo, "fix/recent", EMAIL, hwm_tip, ir, vault, ws, proj) is None)

        # HWM partial: HWM at step1 -> only the new commit (step2) is mined, never before HWM.
        rev = git(repo, "log", "fix/twostep", "--reverse", "--pretty=%H %s").stdout.strip().splitlines()
        step1 = next(l.split()[0] for l in rev if "step1" in l)
        hwm_step1 = {"kind": "commit", "sha": step1, "source": "hwm"}
        r2 = bf(repo, "fix/twostep", EMAIL, hwm_step1, ir, vault, ws, proj)
        check("HWM partial: new-since-HWM IS backfilled", bool(r2))
        if r2:
            diff2 = kb.author_landed_diff(repo, "fix/twostep", EMAIL, hwm_step1)
            check("HWM partial mines post-HWM only (step2 yes, step1 no)",
                  "STEP_TWO" in diff2 and "STEP_ONE" not in diff2)


def main():
    test_backfill()
    test_window_and_hwm()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
