#!/usr/bin/env python3
"""Tests for the budgeted interactive re-auth (kb-sync fetch path).

Contract under test:
  * run_git is non-interactive by default — credential helpers are suppressed
    (the overnight bug: GCM popped one browser OAuth tab PER repo) — and the
    suppression is lifted only with interactive=True.
  * is_auth_failure: auth-shaped stderr only; offline/DNS/timeout never retried.
  * origin_host: https/ssh/scp-like parsed; file:// and garbage yield "" (a
    hostless origin never earns a credential prompt).
  * retry_auth_fetches: at most ONE interactive attempt per host per run
    (attempted_hosts shared across calls), every auth-failed repo re-fetched
    non-interactively afterwards, non-auth failures pass through untouched.

Run: python engine/kb_sync_auth_retry_test.py
"""

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("kb_sync_under_test", HERE / "kb-sync.py")
kb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kb)

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def test_run_git_noninteractive_default():
    print("test_run_git_noninteractive_default")
    r = kb.run_git(["version"], cwd=str(HERE))
    check("plain call works with credential suppression flags", r.returncode == 0)
    r = kb.run_git(["version"], cwd=str(HERE), interactive=True)
    check("interactive=True call works", r.returncode == 0)


def test_is_auth_failure():
    print("test_is_auth_failure")
    yes = [
        "fatal: Authentication failed for 'https://bitbucket.org/x/y.git/'",
        "fatal: could not read Username for 'https://bitbucket.org': terminal prompts disabled",
        "fatal: could not read Password for 'https://x@host': No such device",
        "Cannot prompt because user interactivity has been disabled.",
        "remote: HTTP Basic: Access denied",
        "remote: Invalid credentials",
        "The requested URL returned error: 403",
        "The requested URL returned error: 401",
    ]
    no = [
        "fatal: unable to access 'https://x/': Could not resolve host: bitbucket.org",
        "ssh: connect to host github.com port 22: Connection timed out",
        "fatal: 'C:/Temp/tmp123/remote.git' does not appear to be a git repository",
        "",
        None,
    ]
    for s in yes:
        check(f"auth: {s[:48]}...", kb.is_auth_failure(s))
    for s in no:
        check(f"not auth: {str(s)[:48]!r}", not kb.is_auth_failure(s))


def test_origin_host():
    print("test_origin_host")
    cases = [
        ("https://juniorrosa05@bitbucket.org/conob/conob_8.git", "bitbucket.org"),
        ("https://github.com/me/repo.git", "github.com"),
        ("git@github.com:me/repo.git", "github.com"),
        ("ssh://git@gitlab.com:2222/group/proj.git", "gitlab.com"),
        ("git+ssh://git@host.example/x.git", "host.example"),
        ("file:///C:/Users/x/repo.git", ""),
        ("/srv/git/local.git", ""),
        ("", ""),
        (None, ""),
    ]
    for url, want in cases:
        got = kb.origin_host(url)
        check(f"{str(url)[:44]!r} -> {want!r}", got == want, f"got {got!r}")


class Calls:
    """Recorder + stubs swapped into the module under test."""

    def __init__(self, refetch_ok=True):
        self.interactive = []   # (args, host-repo) of interactive run_git calls
        self.refetched = []     # repos passed to fetch_repo
        self.refetch_ok = refetch_ok

    def run_git(self, args, cwd, check=False, timeout=30, interactive=False):
        if interactive:
            self.interactive.append((tuple(args), str(cwd)))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    def fetch_repo(self, repo, names):
        self.refetched.append(str(repo))
        return (True, "") if self.refetch_ok else (False, "still failing")


def _with_stubs(calls, origins):
    """Swap run_git/fetch_repo/repo_origin on the module; return restore fn."""
    saved = (kb.run_git, kb.fetch_repo, kb.repo_origin)
    kb.run_git = calls.run_git
    kb.fetch_repo = calls.fetch_repo
    kb.repo_origin = lambda repo: origins.get(str(repo), "")

    def restore():
        kb.run_git, kb.fetch_repo, kb.repo_origin = saved
    return restore


AUTH_ERR = "fatal: Authentication failed for 'https://bitbucket.org/x.git/'"


def test_retry_one_prompt_per_host():
    print("test_retry_one_prompt_per_host")
    calls = Calls(refetch_ok=True)
    origins = {"repoA": "https://u@bitbucket.org/a.git", "repoB": "https://u@bitbucket.org/b.git"}
    restore = _with_stubs(calls, origins)
    try:
        attempted = set()
        failed = [("repoA", AUTH_ERR), ("repoB", AUTH_ERR)]
        recovered, still = kb.retry_auth_fetches(failed, ["master"], attempted)
        check("one interactive attempt for two same-host repos", len(calls.interactive) == 1)
        check("interactive attempt is a read-only ls-remote", calls.interactive[0][0][0] == "ls-remote")
        check("both repos re-fetched", calls.refetched == ["repoA", "repoB"])
        check("both recovered", recovered == ["repoA", "repoB"] and still == [])
        check("host recorded in the run budget", attempted == {"bitbucket.org"})
    finally:
        restore()


def test_retry_host_already_attempted():
    print("test_retry_host_already_attempted")
    calls = Calls(refetch_ok=True)
    origins = {"repoC": "https://u@bitbucket.org/c.git"}
    restore = _with_stubs(calls, origins)
    try:
        attempted = {"bitbucket.org"}
        recovered, still = kb.retry_auth_fetches([("repoC", AUTH_ERR)], ["master"], attempted)
        check("no second prompt for an attempted host", len(calls.interactive) == 0)
        check("still re-fetched non-interactively (cache may have the token)",
              calls.refetched == ["repoC"] and recovered == ["repoC"])
    finally:
        restore()


def test_retry_distinct_hosts():
    print("test_retry_distinct_hosts")
    calls = Calls(refetch_ok=True)
    origins = {"bb": "https://u@bitbucket.org/x.git", "gh": "git@github.com:me/y.git"}
    restore = _with_stubs(calls, origins)
    try:
        attempted = set()
        failed = [("bb", AUTH_ERR), ("gh", "fatal: Authentication failed for 'https://github.com/'")]
        recovered, still = kb.retry_auth_fetches(failed, ["master"], attempted)
        check("one prompt per distinct host", len(calls.interactive) == 2)
        check("both hosts budgeted", attempted == {"bitbucket.org", "github.com"})
    finally:
        restore()


def test_retry_non_auth_passthrough():
    print("test_retry_non_auth_passthrough")
    calls = Calls(refetch_ok=True)
    origins = {"off": "https://u@bitbucket.org/x.git"}
    restore = _with_stubs(calls, origins)
    try:
        offline = "fatal: unable to access: Could not resolve host: bitbucket.org"
        recovered, still = kb.retry_auth_fetches([("off", offline)], ["master"], set())
        check("offline failure never prompts", len(calls.interactive) == 0)
        check("offline failure never re-fetched", calls.refetched == [])
        check("passes through to the stale-refs degrade", still == [("off", offline)] and recovered == [])
    finally:
        restore()


def test_retry_still_failing_keeps_degrade():
    print("test_retry_still_failing_keeps_degrade")
    calls = Calls(refetch_ok=False)
    origins = {"repoA": "https://u@bitbucket.org/a.git"}
    restore = _with_stubs(calls, origins)
    try:
        recovered, still = kb.retry_auth_fetches([("repoA", AUTH_ERR)], ["master"], set())
        check("nothing recovered when re-fetch fails", recovered == [])
        check("repo stays failed with the fresh reason", still == [("repoA", "still failing")])
    finally:
        restore()


def test_hostless_origin_never_prompts():
    print("test_hostless_origin_never_prompts")
    calls = Calls(refetch_ok=False)
    origins = {"loc": "file:///C:/bare/repo.git"}
    restore = _with_stubs(calls, origins)
    try:
        recovered, still = kb.retry_auth_fetches([("loc", AUTH_ERR)], ["master"], set())
        check("file:// origin gets no interactive attempt", len(calls.interactive) == 0)
        check("but still gets the cheap re-fetch", calls.refetched == ["loc"])
    finally:
        restore()


def main():
    test_run_git_noninteractive_default()
    test_is_auth_failure()
    test_origin_host()
    test_retry_one_prompt_per_host()
    test_retry_host_already_attempted()
    test_retry_distinct_hosts()
    test_retry_non_auth_passthrough()
    test_retry_still_failing_keeps_degrade()
    test_hostless_origin_never_prompts()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
