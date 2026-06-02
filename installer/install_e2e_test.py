#!/usr/bin/env python3
"""End-to-end install test: the WIRED orchestrator producing a complete fresh host.

This is the teammate use case the unit tests don't cover — run(apply=True) against
an empty CLAUDE_CONFIG_DIR, deploying the REAL repo files. The scheduler is forced
to dry-run: scheduler.register keys off a global task name (ClaudeKbSync), NOT the
claude_dir, so a real register from a temp dir would repoint the live scheduled job
at a path we are about to delete. (The Windows register path is validated separately
via a throwaway task.)

Run: python installer/install_e2e_test.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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


def test_fresh_host_install():
    print("test_fresh_host_install")
    prev = os.environ.get("CLAUDE_CONFIG_DIR")
    with tempfile.TemporaryDirectory() as d:
        cdir = Path(d) / ".claude"  # does not exist yet
        os.environ["CLAUDE_CONFIG_DIR"] = str(cdir)
        try:
            # Wired orchestrator, real apply, scheduler held to dry-run (footgun guard).
            rep = install.run(apply=True, time_hhmm="01:00", scheduler_apply=False)

            check("claude_dir is the temp dir", rep["claude_dir"] == str(cdir))

            # deploy: all 19 manifest files written into a fresh host
            dep = rep["deploy"]
            check("deploy wrote 19", dep.get("wrote") == 19)
            check("engine kb.py in hooks/", (cdir / "hooks" / "kb.py").exists())
            check("kb_config.py in hooks/", (cdir / "hooks" / "kb_config.py").exists())
            check("kb-sync.py in scripts/", (cdir / "scripts" / "kb-sync.py").exists())
            check("kb-embed-daemon.py in scripts/", (cdir / "scripts" / "kb-embed-daemon.py").exists())
            check("kb-context.sh in hooks/", (cdir / "hooks" / "kb-context.sh").exists())
            check("kb-mark.md in commands/", (cdir / "commands" / "kb-mark.md").exists())

            # settings.json created with KB hooks wired
            sp = cdir / "settings.json"
            check("settings.json created", sp.exists())
            txt = sp.read_text(encoding="utf-8")
            check("settings has kb-context.sh", "kb-context.sh" in txt)
            check("settings has kb-bodyread under PostToolUse", "kb-bodyread-track.sh" in txt)
            st = rep["settings"]
            check("settings reports 6 added", len(st.get("added", [])) == 6)

            # version stamped
            check(".kb-version stamped", (cdir / ".kb-version").exists())
            check(".kb-version == repo VERSION", (cdir / ".kb-version").read_text(encoding="utf-8").strip() == install.repo_version())

            # scheduler held to dry-run -> not registered, no global task touched
            check("scheduler not applied", rep["scheduler"].get("registered") is False)

            # config absent -> reported missing, NO vault path fabricated
            cfg = rep["config"]
            check("config reported missing", cfg.get("present") is False)
            check("no kb-workspaces.json created", not (cdir / "kb-workspaces.json").exists())

            # idempotency: a second wired apply changes nothing
            rep2 = install.run(apply=True, time_hhmm="01:00", scheduler_apply=False)
            check("second apply deploys 0", rep2["deploy"].get("wrote") == 0)
            check("second apply adds 0 settings", len(rep2["settings"].get("added", [])) == 0)
        finally:
            if prev is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = prev


def main():
    test_fresh_host_install()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
