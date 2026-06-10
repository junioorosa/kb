#!/usr/bin/env python3
"""Tests for deploy — diff classification, apply/backup, rollback, EOL handling.

Run: python installer/deploy_test.py
Builds a synthetic repo + host in a temp dir; never touches a real ~/.claude.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy import diff, apply, rollback, classify, deploy_pairs, ENGINE_TO_HOOKS, ENGINE_TO_SCRIPTS  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def make_repo(root: Path):
    """Synthetic repo with the engine files + a couple adapter files."""
    eng = root / "engine"
    eng.mkdir(parents=True)
    for n in ENGINE_TO_HOOKS + ENGINE_TO_SCRIPTS:
        (eng / n).write_text(f"# {n}\nprint('hi')\n", encoding="utf-8")
    hooks = root / "adapters" / "claude-code" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "kb-context.sh").write_text("#!/usr/bin/env bash\necho ctx\n", encoding="utf-8")
    cmds = root / "adapters" / "claude-code" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "kb-mark.md").write_text("# kb-mark\n", encoding="utf-8")


def test_fresh_apply():
    print("test_fresh_apply")
    with tempfile.TemporaryDirectory() as d:
        repo, claude = Path(d) / "repo", Path(d) / ".claude"
        make_repo(repo)
        expected = len(ENGINE_TO_HOOKS) + len(ENGINE_TO_SCRIPTS) + 2  # +2 adapter files
        dd = diff(repo, claude)
        check("all new on empty host", len(dd["buckets"]["new"]) == dd["total"] and dd["total"] == expected)
        rep = apply(repo, claude)
        check("wrote all manifest files", rep["wrote"] == expected)
        check("no backup on fresh", rep["backup_dir"] is None)
        check("engine landed in hooks/", (claude / "hooks" / "kb.py").exists())
        check("sync landed in scripts/", (claude / "scripts" / "kb-sync.py").exists())
        check("command landed in commands/", (claude / "commands" / "kb-mark.md").exists())
        dd2 = diff(repo, claude)
        check("all same after apply", len(dd2["buckets"]["same"]) == dd2["total"])


def test_changed_backup_and_rollback():
    print("test_changed_backup_and_rollback")
    with tempfile.TemporaryDirectory() as d:
        repo, claude = Path(d) / "repo", Path(d) / ".claude"
        make_repo(repo)
        apply(repo, claude)
        # Host file drifts (simulate a local edit), then repo changes too.
        target = claude / "hooks" / "kb.py"
        target.write_text("# kb.py LOCAL EDIT\n", encoding="utf-8")
        (repo / "engine" / "kb.py").write_text("# kb.py NEW REPO VERSION\n", encoding="utf-8")
        check("classified as changed", classify(repo / "engine" / "kb.py", target) == "changed")
        rep = apply(repo, claude)
        check("backup dir created", rep["backup_dir"] is not None)
        check("host now has repo version", "NEW REPO VERSION" in target.read_text(encoding="utf-8"))
        # Rollback restores the local edit that was overwritten.
        rb = rollback(claude)
        check("rollback restored 1", rb["restored"] == 1)
        check("host back to local edit", "LOCAL EDIT" in target.read_text(encoding="utf-8"))


def test_eol_only():
    print("test_eol_only")
    with tempfile.TemporaryDirectory() as d:
        repo, claude = Path(d) / "repo", Path(d) / ".claude"
        make_repo(repo)
        # Pin the repo source to known LF bytes (write_bytes — no platform translation).
        src = repo / "adapters" / "claude-code" / "hooks" / "kb-context.sh"
        src.write_bytes(b"#!/usr/bin/env bash\necho ctx\n")
        apply(repo, claude)  # host gets a byte copy (LF)
        # Host file now drifts to CRLF — identical content, different line endings.
        t = claude / "hooks" / "kb-context.sh"
        t.write_bytes(b"#!/usr/bin/env bash\r\necho ctx\r\n")
        check("classified eol-only", classify(src, t) == "eol-only")
        rep = apply(repo, claude)  # default: skip eol-only
        check("eol-only not rewritten by default", b"\r\n" in t.read_bytes() and rep["wrote"] == 0)
        rep2 = apply(repo, claude, normalize_eol=True)
        check("eol-only rewritten with --normalize-eol", rep2["wrote"] == 1 and b"\r\n" not in t.read_bytes())


def test_idempotent():
    print("test_idempotent")
    with tempfile.TemporaryDirectory() as d:
        repo, claude = Path(d) / "repo", Path(d) / ".claude"
        make_repo(repo)
        apply(repo, claude)
        rep = apply(repo, claude)
        check("second apply writes nothing", rep["wrote"] == 0 and rep["backup_dir"] is None)


def test_missing_src_is_error():
    print("test_missing_src_is_error")
    with tempfile.TemporaryDirectory() as d:
        repo, claude = Path(d) / "repo", Path(d) / ".claude"
        make_repo(repo)
        (repo / "engine" / "kb_config.py").unlink()  # packaging bug
        rep = apply(repo, claude)
        check("apply refuses with missing source", "error" in rep and rep["wrote"] == 0)


def main():
    test_fresh_apply()
    test_changed_backup_and_rollback()
    test_eol_only()
    test_idempotent()
    test_missing_src_is_error()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
