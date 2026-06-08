#!/usr/bin/env python3
"""Tests for the vault-remote path (install.vault_remote_status / _connect / _pull).

The safety-critical contract under test:
  * status only READS git (no writes), and reports local-only vs connected.
  * connect adds 'origin' + pushes ONCE. It refuses to clobber an existing remote,
    never force-pushes, and when the push fails (e.g. a local-only guard hook) it
    leaves the remote configured and says so — nothing is lost.
  * pull does fetch + merge. Disjoint per-file learnings auto-merge. It refuses a
    dirty tree, and on a real content conflict it ABORTS the merge so no conflict
    marker ever lands in a learning — the pre-merge tree is restored verbatim.

Everything runs against throwaway temp git repos (a bare repo stands in for the
remote). No network, no real vault touched.

Run: python installer/vault_remote_test.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile

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
                          cwd=str(repo), capture_output=True, text=True)


def _ident(repo):
    git(repo, "config", "user.email", "dev@x.com")
    git(repo, "config", "user.name", "Dev")


def init_vault(path, files=(("a.md", "# a"),)):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    _ident(path)
    for name, body in files:
        (path / name).write_text(body + "\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return path


def init_bare(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "--bare", "-b", "main")
    return path


def clone(bare, dest):
    git(bare.parent, "clone", "-q", str(bare), str(dest))
    _ident(dest)
    return dest


def file_url(path):
    """A file:// URL git accepts cross-OS (forward slashes, drive letter kept)."""
    return "file:///" + str(path).replace("\\", "/").lstrip("/")


def commit_file(repo, name, body, msg):
    (repo / name).write_text(body + "\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)


def make_cdir(tmp, vault_path):
    """A throwaway claude_dir whose kb-workspaces.json points at the test vault."""
    cdir = tmp / "claude"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "kb-workspaces.json").write_text(
        json.dumps({"vault": str(vault_path)}), encoding="utf-8")
    return cdir


# --- status ------------------------------------------------------------------

def test_status_no_vault():
    print("test_status_no_vault")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cdir = tmp / "claude"
        cdir.mkdir()  # no kb-workspaces.json
        s = install.vault_remote_status(cdir)
        check("reports no vault", s["is_git"] is False and "no vault" in (s["reason"] or "").lower())


def test_status_local_only():
    print("test_status_local_only")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        v = init_vault(tmp / "vault")
        cdir = make_cdir(tmp, v)
        s = install.vault_remote_status(cdir)
        check("is_git True", s["is_git"] is True)
        check("has_remote False (local only)", s["has_remote"] is False)
        check("branch main", s["branch"] == "main")
        check("no reason", s["reason"] is None)


def test_status_has_remote():
    print("test_status_has_remote")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        v = init_vault(tmp / "vault")
        bare = init_bare(tmp / "remote.git")
        git(v, "remote", "add", "origin", str(bare))
        cdir = make_cdir(tmp, v)
        s = install.vault_remote_status(cdir)
        check("has_remote True", s["has_remote"] is True)
        check("remote name origin", s["remote"] == "origin")
        check("url is the bare path", s["url"] == str(bare))


# --- connect -----------------------------------------------------------------

def test_connect_rejects_bad_url():
    print("test_connect_rejects_bad_url")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        v = init_vault(tmp / "vault")
        cdir = make_cdir(tmp, v)
        r = install.vault_connect_remote("ftp://nope/x.git", cdir)
        check("refused", r["connected"] is False)
        check("reason about URL scheme", "url must start" in (r["reason"] or "").lower())
        # nothing was added
        check("no remote left behind", "origin" not in git(v, "remote").stdout.split())


def test_connect_happy_push():
    print("test_connect_happy_push")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        v = init_vault(tmp / "vault")
        bare = init_bare(tmp / "remote.git")
        cdir = make_cdir(tmp, v)
        r = install.vault_connect_remote(file_url(bare), cdir)
        check("connected", r["connected"] is True)
        check("pushed", r["pushed"] is True)
        check("origin set on the vault", "origin" in git(v, "remote").stdout.split())
        check("bare received main", git(bare, "rev-parse", "main").returncode == 0)
        check("note confirms publish", "published" in (r["note"] or "").lower())


def test_connect_refuses_existing():
    print("test_connect_refuses_existing")
    # No-clobber contract: an already-connected vault is left untouched.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        v = init_vault(tmp / "vault")
        bare = init_bare(tmp / "remote.git")
        git(v, "remote", "add", "origin", str(bare))
        cdir = make_cdir(tmp, v)
        r = install.vault_connect_remote(file_url(tmp / "other.git"), cdir)
        check("refused", r["connected"] is False)
        check("reason about existing remote", "already has" in (r["reason"] or "").lower())
        check("origin url unchanged", git(v, "remote", "get-url", "origin").stdout.strip() == str(bare))


def test_connect_push_blocked_by_guard_hook():
    print("test_connect_push_blocked_by_guard_hook")
    # A local-only guard hook (the author's wall) blocks the push. Remote must stay
    # configured and the report must say push failed — exactly the documented path.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        v = init_vault(tmp / "vault")
        bare = init_bare(tmp / "remote.git")
        hook = v / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        cdir = make_cdir(tmp, v)
        r = install.vault_connect_remote(file_url(bare), cdir)
        check("connected (remote added)", r["connected"] is True)
        check("not pushed (hook blocked)", r["pushed"] is False)
        check("remote stays configured", "origin" in git(v, "remote").stdout.split())
        check("guidance to push by hand", "push" in (r["note"] or "").lower())


# --- pull --------------------------------------------------------------------

def test_pull_no_remote():
    print("test_pull_no_remote")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        v = init_vault(tmp / "vault")
        cdir = make_cdir(tmp, v)
        r = install.vault_pull_remote(cdir)
        check("refused", r["pulled"] is False)
        check("reason: connect first", "connect it first" in (r["reason"] or "").lower())


def test_pull_refuses_dirty():
    print("test_pull_refuses_dirty")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        bare = init_bare(tmp / "remote.git")
        seed = init_vault(tmp / "seed")
        git(seed, "remote", "add", "origin", str(bare))
        git(seed, "push", "-q", "-u", "origin", "main")
        v = clone(bare, tmp / "vault")
        (v / "a.md").write_text("uncommitted edit\n", encoding="utf-8")  # dirty
        cdir = make_cdir(tmp, v)
        r = install.vault_pull_remote(cdir)
        check("refused", r["pulled"] is False)
        check("reason mentions uncommitted", "uncommitted" in (r["reason"] or "").lower())


def test_pull_up_to_date():
    print("test_pull_up_to_date")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        bare = init_bare(tmp / "remote.git")
        seed = init_vault(tmp / "seed")
        git(seed, "remote", "add", "origin", str(bare))
        git(seed, "push", "-q", "-u", "origin", "main")
        v = clone(bare, tmp / "vault")
        cdir = make_cdir(tmp, v)
        r = install.vault_pull_remote(cdir)
        check("pulled True", r["pulled"] is True)
        check("already up to date", r["already_up_to_date"] is True)
        check("0 merged commits", r["merged_commits"] == 0)


def test_pull_fast_forward():
    print("test_pull_fast_forward")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        bare = init_bare(tmp / "remote.git")
        seed = init_vault(tmp / "seed")
        git(seed, "remote", "add", "origin", str(bare))
        git(seed, "push", "-q", "-u", "origin", "main")
        v = clone(bare, tmp / "vault")
        # a teammate (seed) adds a learning and pushes
        commit_file(seed, "b.md", "# teammate learning", "add b")
        git(seed, "push", "-q", "origin", "main")
        cdir = make_cdir(tmp, v)
        r = install.vault_pull_remote(cdir)
        check("pulled True", r["pulled"] is True)
        check("not up-to-date", r["already_up_to_date"] is False)
        check(">=1 merged commit", r["merged_commits"] >= 1)
        check("teammate file landed on disk", (v / "b.md").exists())


def test_pull_merge_disjoint():
    print("test_pull_merge_disjoint")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        bare = init_bare(tmp / "remote.git")
        seed = init_vault(tmp / "seed")
        git(seed, "remote", "add", "origin", str(bare))
        git(seed, "push", "-q", "-u", "origin", "main")
        v = clone(bare, tmp / "vault")
        # local diverges: my own learning, not yet pushed
        commit_file(v, "mine.md", "# my learning", "local mine")
        # teammate adds a DIFFERENT file and pushes -> disjoint, should auto-merge
        commit_file(seed, "theirs.md", "# their learning", "remote theirs")
        git(seed, "push", "-q", "origin", "main")
        cdir = make_cdir(tmp, v)
        r = install.vault_pull_remote(cdir)
        check("merged cleanly", r["pulled"] is True and r["conflict"] is False)
        check("both files present", (v / "mine.md").exists() and (v / "theirs.md").exists())
        check("tree clean after merge", git(v, "status", "--porcelain").stdout.strip() == "")


def test_pull_conflict_aborts():
    print("test_pull_conflict_aborts")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        bare = init_bare(tmp / "remote.git")
        seed = init_vault(tmp / "seed", files=(("shared.md", "base line"),))
        git(seed, "remote", "add", "origin", str(bare))
        git(seed, "push", "-q", "-u", "origin", "main")
        v = clone(bare, tmp / "vault")
        # both sides edit the SAME line of the SAME file -> real conflict
        commit_file(v, "shared.md", "MY version", "local edit")
        commit_file(seed, "shared.md", "THEIR version", "remote edit")
        git(seed, "push", "-q", "origin", "main")
        cdir = make_cdir(tmp, v)
        r = install.vault_pull_remote(cdir)
        check("conflict reported", r["conflict"] is True)
        check("not pulled", r["pulled"] is False)
        # the merge was aborted: my content is intact, no conflict markers, clean tree
        check("my content intact", (v / "shared.md").read_text().strip() == "MY version")
        check("no conflict markers", "<<<<<<<" not in (v / "shared.md").read_text())
        check("tree clean (merge aborted)", git(v, "status", "--porcelain").stdout.strip() == "")


def main():
    # _vault_path honors KB_VAULT first (mirrors resolve_vault); drop it so the
    # tests resolve the vault only from each temp config, never the real env.
    os.environ.pop("KB_VAULT", None)
    test_status_no_vault()
    test_status_local_only()
    test_status_has_remote()
    test_connect_rejects_bad_url()
    test_connect_happy_push()
    test_connect_refuses_existing()
    test_connect_push_blocked_by_guard_hook()
    test_pull_no_remote()
    test_pull_refuses_dirty()
    test_pull_up_to_date()
    test_pull_fast_forward()
    test_pull_merge_disjoint()
    test_pull_conflict_aborts()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
