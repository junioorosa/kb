#!/usr/bin/env python3
"""Tests for deploy — diff classification, apply/backup, rollback, EOL handling.

Run: python installer/deploy_test.py
Builds a synthetic repo + host in a temp dir; never touches a real ~/.kb or ~/.claude.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy import diff, apply, rollback, classify, deploy_pairs, ENGINE_FILES  # noqa: E402

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
    for n in ENGINE_FILES:
        (eng / n).write_text(f"# {n}\nprint('hi')\n", encoding="utf-8")
    hooks = root / "adapters" / "claude-code" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "kb-context.sh").write_text("#!/usr/bin/env bash\necho ctx\n", encoding="utf-8")
    cmds = root / "adapters" / "claude-code" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "kb-mark.md").write_text("# kb-mark\n", encoding="utf-8")


def roots(d: str):
    return Path(d) / "repo", Path(d) / ".kb", Path(d) / ".claude"


def test_fresh_apply():
    print("test_fresh_apply")
    with tempfile.TemporaryDirectory() as d:
        repo, kb, claude = roots(d)
        make_repo(repo)
        expected = len(ENGINE_FILES) + 2  # +2 adapter files
        dd = diff(repo, kb, claude)
        check("all new on empty host", len(dd["buckets"]["new"]) == dd["total"] and dd["total"] == expected)
        rep = apply(repo, kb, claude)
        check("wrote all manifest files", rep["wrote"] == expected)
        check("no backup on fresh", rep["backup_dir"] is None)
        check("engine landed flat in engine/", (kb / "engine" / "kb.py").exists())
        check("sync is an engine sibling", (kb / "engine" / "kb-sync.py").exists())
        check("adapter hook joined the engine dir", (kb / "engine" / "kb-context.sh").exists())
        check("command landed in claude commands/", (claude / "commands" / "kb-mark.md").exists())
        dd2 = diff(repo, kb, claude)
        check("all same after apply", len(dd2["buckets"]["same"]) == dd2["total"])


def test_changed_backup_and_rollback():
    print("test_changed_backup_and_rollback")
    with tempfile.TemporaryDirectory() as d:
        repo, kb, claude = roots(d)
        make_repo(repo)
        apply(repo, kb, claude)
        # Host file drifts (simulate a local edit), then repo changes too.
        target = kb / "engine" / "kb.py"
        target.write_text("# kb.py LOCAL EDIT\n", encoding="utf-8")
        (repo / "engine" / "kb.py").write_text("# kb.py NEW REPO VERSION\n", encoding="utf-8")
        check("classified as changed", classify(repo / "engine" / "kb.py", target) == "changed")
        rep = apply(repo, kb, claude)
        check("backup dir created", rep["backup_dir"] is not None)
        check("backup lives under kb backups/", str(kb / "backups") in rep["backup_dir"])
        check("host now has repo version", "NEW REPO VERSION" in target.read_text(encoding="utf-8"))
        # Rollback restores the local edit that was overwritten.
        rb = rollback(kb)
        check("rollback restored 1", rb["restored"] == 1)
        check("host back to local edit", "LOCAL EDIT" in target.read_text(encoding="utf-8"))


def test_commands_backup_root():
    """A changed file under claude_dir must back up under the claude/ mirror —
    the dual-root backup layout keeps the two trees apart."""
    print("test_commands_backup_root")
    with tempfile.TemporaryDirectory() as d:
        repo, kb, claude = roots(d)
        make_repo(repo)
        apply(repo, kb, claude)
        cmd = claude / "commands" / "kb-mark.md"
        cmd.write_text("# kb-mark LOCAL EDIT\n", encoding="utf-8")
        (repo / "adapters" / "claude-code" / "commands" / "kb-mark.md").write_text(
            "# kb-mark NEW\n", encoding="utf-8")
        rep = apply(repo, kb, claude)
        check("backup dir created", rep["backup_dir"] is not None)
        mirrored = Path(rep["backup_dir"]) / "claude" / "commands" / "kb-mark.md"
        check("claude-root file mirrored under claude/", mirrored.exists())
        rb = rollback(kb)
        check("rollback crosses roots", "LOCAL EDIT" in cmd.read_text(encoding="utf-8") and rb["restored"] == 1)


def test_eol_only():
    print("test_eol_only")
    with tempfile.TemporaryDirectory() as d:
        repo, kb, claude = roots(d)
        make_repo(repo)
        # Pin the repo source to known LF bytes (write_bytes — no platform translation).
        src = repo / "adapters" / "claude-code" / "hooks" / "kb-context.sh"
        src.write_bytes(b"#!/usr/bin/env bash\necho ctx\n")
        apply(repo, kb, claude)  # host gets a byte copy (LF)
        # Host file now drifts to CRLF — identical content, different line endings.
        t = kb / "engine" / "kb-context.sh"
        t.write_bytes(b"#!/usr/bin/env bash\r\necho ctx\r\n")
        check("classified eol-only", classify(src, t) == "eol-only")
        rep = apply(repo, kb, claude)  # default: skip eol-only
        check("eol-only not rewritten by default", b"\r\n" in t.read_bytes() and rep["wrote"] == 0)
        rep2 = apply(repo, kb, claude, normalize_eol=True)
        check("eol-only rewritten with --normalize-eol", rep2["wrote"] == 1 and b"\r\n" not in t.read_bytes())


def test_idempotent():
    print("test_idempotent")
    with tempfile.TemporaryDirectory() as d:
        repo, kb, claude = roots(d)
        make_repo(repo)
        apply(repo, kb, claude)
        rep = apply(repo, kb, claude)
        check("second apply writes nothing", rep["wrote"] == 0 and rep["backup_dir"] is None)


def test_missing_src_is_error():
    print("test_missing_src_is_error")
    with tempfile.TemporaryDirectory() as d:
        repo, kb, claude = roots(d)
        make_repo(repo)
        (repo / "engine" / "kb_config.py").unlink()  # packaging bug
        rep = apply(repo, kb, claude)
        check("apply refuses with missing source", "error" in rep and rep["wrote"] == 0)


def main():
    test_fresh_apply()
    test_changed_backup_and_rollback()
    test_commands_backup_root()
    test_eol_only()
    test_idempotent()
    test_missing_src_is_error()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
