#!/usr/bin/env python3
"""End-to-end install test: the WIRED orchestrator producing a complete fresh host.

This is the teammate use case the unit tests don't cover — run(apply=True) against
an empty KB_HOME + CLAUDE_CONFIG_DIR, deploying the REAL repo files. The scheduler is
forced to dry-run: scheduler.register keys off a global task name (ClaudeKbSync), NOT
the target dirs, so a real register from a temp dir would repoint the live scheduled
job at a path we are about to delete. (The Windows register path is validated
separately via a throwaway task.)

Also covers the pre-0.11 migration: a host with config/state/engine under ~/.claude
must come out with everything under ~/.kb and the old engine retired.

Run: python installer/install_e2e_test.py
"""

import json
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


class _env:
    """Scoped env override for CLAUDE_CONFIG_DIR + KB_HOME."""

    def __init__(self, cdir: Path, kdir: Path):
        self.vals = {"CLAUDE_CONFIG_DIR": str(cdir), "KB_HOME": str(kdir)}

    def __enter__(self):
        self.prev = {k: os.environ.get(k) for k in self.vals}
        os.environ.update(self.vals)

    def __exit__(self, *exc):
        for k, v in self.prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_fresh_host_install():
    print("test_fresh_host_install")
    with tempfile.TemporaryDirectory() as d:
        cdir = Path(d) / ".claude"  # does not exist yet
        kdir = Path(d) / ".kb"
        with _env(cdir, kdir):
            # Wired orchestrator, real apply, scheduler + shortcut + mcp held to
            # dry-run (footgun guard: all touch global state outside the temp dirs).
            rep = install.run(apply=True, time_hhmm="01:00", scheduler_apply=False,
                              shortcut_apply=False, mcp_apply=False)

            check("kb_dir is the temp dir", rep["kb_dir"] == str(kdir))
            check("claude_dir is the temp dir", rep["claude_dir"] == str(cdir))
            check("fresh host needs no migration", rep["migrate"].get("needed") is False)

            # deploy: every manifest file written into a fresh host
            import deploy as _deploy
            expected = len(_deploy.deploy_pairs(install.REPO_ROOT, kdir, cdir))
            dep = rep["deploy"]
            check("deploy wrote the whole manifest", dep.get("wrote") == expected)
            check("engine kb.py in engine/", (kdir / "engine" / "kb.py").exists())
            check("kb_config.py in engine/", (kdir / "engine" / "kb_config.py").exists())
            check("kb-sync.py in engine/", (kdir / "engine" / "kb-sync.py").exists())
            check("kb-embed-daemon.py in engine/", (kdir / "engine" / "kb-embed-daemon.py").exists())
            check("kb-context.sh in engine/", (kdir / "engine" / "kb-context.sh").exists())
            check("kb-mark.md in claude commands/", (cdir / "commands" / "kb-mark.md").exists())
            check("nothing engine-shaped under claude hooks/", not (cdir / "hooks").exists())

            # settings.json created with KB hooks wired at the engine path
            sp = cdir / "settings.json"
            check("settings.json created", sp.exists())
            txt = sp.read_text(encoding="utf-8")
            check("settings has kb-context.sh", "kb-context.sh" in txt)
            check("settings hooks point at the kb engine", "/engine/kb-context.sh" in txt.replace("\\\\", "/"))
            check("settings has kb-bodyread under PostToolUse", "kb-bodyread-track.sh" in txt)
            st = rep["settings"]
            check("settings reports 5 added", len(st.get("added", [])) == 5)

            # version + source stamped (source lets the deployed `kb manage` find the clone)
            check(".version stamped", (kdir / ".version").exists())
            check(".version == repo VERSION", (kdir / ".version").read_text(encoding="utf-8").strip() == install.repo_version())
            check(".source stamped", (kdir / ".source").exists())
            check(".source == repo root", (kdir / ".source").read_text(encoding="utf-8").strip() == str(install.REPO_ROOT))

            # scheduler held to dry-run -> not registered, no global task touched
            check("scheduler not applied", rep["scheduler"].get("registered") is False)

            # config absent -> reported missing, NO vault path fabricated
            cfg = rep["config"]
            check("config reported missing", cfg.get("present") is False)
            check("no config.json created", not (kdir / "config.json").exists())

            # idempotency: a second wired apply changes nothing
            rep2 = install.run(apply=True, time_hhmm="01:00", scheduler_apply=False,
                               shortcut_apply=False, mcp_apply=False)
            check("second apply deploys 0", rep2["deploy"].get("wrote") == 0)
            check("second apply adds 0 settings", len(rep2["settings"].get("added", [])) == 0)
            check("second apply migrates nothing", rep2["migrate"].get("needed") is False)


def test_pre011_migration():
    print("test_pre011_migration")
    with tempfile.TemporaryDirectory() as d:
        cdir = Path(d) / ".claude"
        kdir = Path(d) / ".kb"
        # Fabricate a pre-0.11 install: config + state + stamps + deployed engine.
        (cdir / "state").mkdir(parents=True)
        (cdir / "hooks").mkdir()
        (cdir / "scripts").mkdir()
        (cdir / "kb-workspaces.json").write_text(json.dumps({"vault": d}), encoding="utf-8")
        (cdir / "state" / "kb-session-branch-s1.json").write_text("{}", encoding="utf-8")
        (cdir / "state" / "kb-sync-history.json").write_text("[]", encoding="utf-8")
        (cdir / "state" / "unrelated.json").write_text("{}", encoding="utf-8")  # not ours
        (cdir / ".kb-version").write_text("0.10.0\n", encoding="utf-8")
        (cdir / ".kb-source").write_text("C:/old/clone\n", encoding="utf-8")
        (cdir / "hooks" / "kb.py").write_text("# old engine\n", encoding="utf-8")
        (cdir / "hooks" / "kb-context.sh").write_text("# old hook\n", encoding="utf-8")
        (cdir / "hooks" / "my-own-hook.sh").write_text("# user file\n", encoding="utf-8")
        (cdir / "scripts" / "kb-sync.py").write_text("# old sync\n", encoding="utf-8")
        # Old settings wired at the old path.
        (cdir / "settings.json").write_text(json.dumps({
            "hooks": {"UserPromptSubmit": [{"hooks": [
                {"type": "command",
                 "command": f'bash "{cdir.as_posix()}/hooks/kb-context.sh"',
                 "timeout": 10},
            ]}]},
        }), encoding="utf-8")

        with _env(cdir, kdir):
            rep = install.run(apply=True, time_hhmm="01:00", scheduler_apply=False,
                              shortcut_apply=False, mcp_apply=False)

            mig = rep["migrate"]
            check("migration detected", mig.get("needed") is True)
            check("config copied to kb home",
                  (kdir / "config.json").exists()
                  and json.loads((kdir / "config.json").read_text(encoding="utf-8"))["vault"] == d)
            check("legacy config left in place", (cdir / "kb-workspaces.json").exists())
            check("kb-* state migrated", (kdir / "state" / "kb-session-branch-s1.json").exists()
                  and (kdir / "state" / "kb-sync-history.json").exists())
            check("foreign state file NOT migrated", not (kdir / "state" / "unrelated.json").exists())
            check("old engine retired from hooks/", not (cdir / "hooks" / "kb.py").exists()
                  and not (cdir / "hooks" / "kb-context.sh").exists())
            check("old sync retired from scripts/", not (cdir / "scripts" / "kb-sync.py").exists())
            check("user's own hook untouched", (cdir / "hooks" / "my-own-hook.sh").exists())
            check("retired files preserved in a backup",
                  mig.get("backup_dir") and (Path(mig["backup_dir"]) / "hooks" / "kb.py").exists())

            # settings: the old-path entry was repointed, not duplicated
            s = json.loads((cdir / "settings.json").read_text(encoding="utf-8"))
            cmds = [h["command"] for g in s["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
            ours = [c for c in cmds if "kb-context.sh" in c]
            check("old settings entry repointed", len(ours) == 1 and "/engine/kb-context.sh" in ours[0])
            check("settings reports the repoint", "UserPromptSubmit:kb-context.sh" in rep["settings"].get("updated", []))

            # config now resolves at the new location
            check("config step sees the migrated file", rep["config"].get("present") is True)

            # the new engine is deployed and the version stamp moved over
            check("new engine deployed", (kdir / "engine" / "kb.py").exists())
            check("version stamp at kb home", (kdir / ".version").exists())

            # idempotency: nothing left to migrate on a second run
            rep2 = install.run(apply=True, time_hhmm="01:00", scheduler_apply=False,
                               shortcut_apply=False, mcp_apply=False)
            check("second run migrates nothing", rep2["migrate"].get("needed") is False)


def main():
    test_fresh_host_install()
    test_pre011_migration()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
