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
CHANGE_MARKER = "ALTERA_SEPARACAO_CORREDOR_ALTERNATIVO"

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def git(repo, *args, author=None):
    env = None
    if author:
        import os
        env = {**os.environ, "GIT_AUTHOR_EMAIL": author, "GIT_AUTHOR_NAME": author,
               "GIT_COMMITTER_EMAIL": EMAIL, "GIT_COMMITTER_NAME": "Committer"}
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args],
                          cwd=repo, capture_output=True, text=True, env=env)


def commit(repo, path, content, msg, author=None):
    (repo / path).write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg, author=author)
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
    commit(repo, "feature.txt", CHANGE_MARKER + "\n", "fix: ajusta separacao por corredor")
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


def main():
    test_backfill()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
