#!/usr/bin/env python3
"""Tests for settings_merge — the bulletproof-merge guarantees.

Run: python installer/settings_merge_test.py
No deps. Uses a temp dir; never touches a real settings.json.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from settings_merge import merge_settings, SettingsMergeError, kb_desired_hooks  # noqa: E402

KB_DIR = Path("C:/Users/example/.kb")
PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def _commands(settings, event, matcher=None):
    out = []
    for g in settings.get("hooks", {}).get(event, []):
        if (matcher is None and "matcher" not in g) or g.get("matcher") == matcher:
            for h in g.get("hooks", []):
                out.append(h.get("command", ""))
    return out


def test_fresh_install():
    print("test_fresh_install")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"  # does not exist
        rep = merge_settings(p, KB_DIR)
        check("wrote a new file", rep["wrote"] and p.exists())
        check("no backup (nothing to back up)", rep["backup"] is None)
        s = json.loads(p.read_text(encoding="utf-8"))
        ups = _commands(s, "UserPromptSubmit")
        check("kb-context.sh present", any("kb-context.sh" in c for c in ups))
        check("kb-mark + kb-stats present", sum("kb-mark-intercept.sh" in c or "kb-stats-intercept.sh" in c for c in ups) == 2)
        post = _commands(s, "PostToolUse", matcher="Read")
        check("kb-bodyread under Read matcher", any("kb-bodyread-track.sh" in c for c in post))
        ss = _commands(s, "SessionStart")
        check("daemon-spawn present (sole KB SessionStart hook)", any("kb-embed-daemon-spawn.sh" in c for c in ss))
        check("session-branch NOT registered (manual-only marking)", not any("kb-session-branch.sh" in c for c in ss))


def test_preserves_foreign_hooks():
    print("test_preserves_foreign_hooks")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        existing = {
            "model": "opus",
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [
                        {"type": "command", "command": 'node "X/caveman-mode-tracker.js"', "timeout": 5},
                    ]}
                ],
                "SessionStart": [
                    {"hooks": [
                        {"type": "command", "command": 'node "X/caveman-activate.js"', "timeout": 5},
                    ]}
                ],
            },
            "statusLine": {"type": "command", "command": "powershell ... combined-statusline.ps1"},
        }
        p.write_text(json.dumps(existing), encoding="utf-8")
        rep = merge_settings(p, KB_DIR)
        s = json.loads(p.read_text(encoding="utf-8"))
        ups = _commands(s, "UserPromptSubmit")
        check("foreign caveman tracker preserved", any("caveman-mode-tracker.js" in c for c in ups))
        check("kb hooks added alongside foreign", any("kb-context.sh" in c for c in ups))
        ss = _commands(s, "SessionStart")
        check("foreign caveman-activate preserved", any("caveman-activate.js" in c for c in ss))
        check("statusLine untouched", s["statusLine"]["command"].endswith("combined-statusline.ps1"))
        check("backup created (file existed)", rep["backup"] is not None and Path(rep["backup"]).exists())
        check("model key untouched", s.get("model") == "opus")


def test_idempotent():
    print("test_idempotent")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        r1 = merge_settings(p, KB_DIR)
        check("first run changed", r1["changed"] and r1["wrote"])
        before = p.read_text(encoding="utf-8")
        r2 = merge_settings(p, KB_DIR)
        check("second run no change", not r2["changed"] and not r2["wrote"])
        check("second run no backup", r2["backup"] is None)
        check("file byte-identical after re-run", p.read_text(encoding="utf-8") == before)
        check("all entries skipped on re-run", len(r2["skipped"]) == 5 and len(r2["added"]) == 0)


def test_recognizes_localized_variant():
    print("test_recognizes_localized_variant")
    # An entry already wired at the CURRENT path with a different timeout AND a
    # localized statusMessage must be recognized (skipped), not duplicated.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        h = KB_DIR.as_posix()
        existing = {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [
                        {"type": "command", "command": f'bash "{h}/engine/kb-context.sh"',
                         "timeout": 99, "statusMessage": "KB: recall cross-ticket (PT)..."},
                    ]}
                ],
            },
        }
        p.write_text(json.dumps(existing), encoding="utf-8")
        rep = merge_settings(p, KB_DIR)
        s = json.loads(p.read_text(encoding="utf-8"))
        ups = _commands(s, "UserPromptSubmit")
        check("kb-context.sh not duplicated", sum("kb-context.sh" in c for c in ups) == 1)
        check("localized variant skipped", "UserPromptSubmit:kb-context.sh" in rep["skipped"])
        # The pre-existing one keeps its localized timeout (we never mutate foreign-shaped entries).
        ctx = next(h for g in s["hooks"]["UserPromptSubmit"] for h in g["hooks"] if "kb-context.sh" in h["command"])
        check("existing timeout preserved (non-destructive)", ctx["timeout"] == 99)


def test_repoints_stale_own_entry():
    print("test_repoints_stale_own_entry")
    # The pre-0.11 install wired bash ".../.claude/hooks/kb-context.sh". The merge
    # must recognize that entry as OURS, rewrite ONLY its command to the new
    # engine path, and preserve the user-tuned timeout/statusMessage.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        existing = {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [
                        {"type": "command",
                         "command": 'bash "C:/Users/example/.claude/hooks/kb-context.sh"',
                         "timeout": 99, "statusMessage": "KB: recall cross-ticket (PT)..."},
                    ]}
                ],
            },
        }
        p.write_text(json.dumps(existing), encoding="utf-8")
        rep = merge_settings(p, KB_DIR)
        s = json.loads(p.read_text(encoding="utf-8"))
        ups = _commands(s, "UserPromptSubmit")
        check("stale entry repointed, not duplicated", sum("kb-context.sh" in c for c in ups) == 1)
        check("reported as updated", "UserPromptSubmit:kb-context.sh" in rep["updated"])
        ctx = next(h for g in s["hooks"]["UserPromptSubmit"] for h in g["hooks"] if "kb-context.sh" in h["command"])
        check("command now targets the engine dir", "/.kb/engine/kb-context.sh" in ctx["command"])
        check("custom timeout survives the repoint", ctx["timeout"] == 99)
        check("localized statusMessage survives", ctx["statusMessage"].endswith("(PT)..."))
        # Second run: nothing left to change.
        r2 = merge_settings(p, KB_DIR)
        check("repoint is idempotent", not r2["changed"])


def test_refuses_malformed_json():
    print("test_refuses_malformed_json")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        garbage = '{ "hooks": { not valid json '
        p.write_text(garbage, encoding="utf-8")
        raised = False
        try:
            merge_settings(p, KB_DIR)
        except SettingsMergeError:
            raised = True
        check("raised SettingsMergeError", raised)
        check("malformed file left untouched", p.read_text(encoding="utf-8") == garbage)


def test_atomic_no_partial_on_existing():
    print("test_atomic_no_partial_on_existing")
    # Sanity: after a successful merge, no stray temp file remains.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        merge_settings(p, KB_DIR)
        tmp = p.with_name(p.name + ".kb-tmp")
        check("no leftover temp file", not tmp.exists())


def main():
    # Smoke: the factory builds 5 entries across 3 events.
    desired = kb_desired_hooks(KB_DIR)
    total = sum(len(v["entries"]) for v in desired.values())
    check("factory yields 5 entries", total == 5)

    test_fresh_install()
    test_preserves_foreign_hooks()
    test_idempotent()
    test_recognizes_localized_variant()
    test_repoints_stale_own_entry()
    test_refuses_malformed_json()
    test_atomic_no_partial_on_existing()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
