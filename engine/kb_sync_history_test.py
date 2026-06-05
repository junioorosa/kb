#!/usr/bin/env python3
"""Tests for the sync-history record + append in kb-sync (the manager's ops feed).

Run: python engine/kb_sync_history_test.py
"""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("kbs_hist", HERE / "kb-sync.py")
kb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kb)

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(("  ok   " if cond else "  FAIL ") + name)


def test():
    print("test_sync_history")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        vault = d / "vault"
        learning = vault / "Acme" / "gateway" / "fix" / "x" / "Learnings" / "a.md"
        learning.parent.mkdir(parents=True)
        learning.write_text("learned\n", encoding="utf-8")
        state = d / "state"; state.mkdir()
        kb.STATE_DIR = state
        kb.SYNC_HISTORY = state / "kb-sync-history.json"

        rep = kb.RunReport()
        rep.start_ts -= 60  # so the just-written learning counts as "touched this run"
        rep.note_scan(26, 25)
        rep.add_capture("Acme", "gateway", "gateway", "fix/x", "", "x", 2, [], True, 0, "", "", "cap-7d-truncated")
        rep.add_capture("Acme", "gateway", "gateway", "fix/y", "", "y", 1, [], True, 0, "", "", "backfill-merged")
        rep.add_capture("Acme", "gateway", "gateway", "fix/z", "", "z", 1, [], False, 1, "", "boom", "cap-7d-truncated")

        rec = rep.to_record(vault, dry_run=False)
        check("captures counted", rec["captures"] == 3)
        check("backfills counted", rec["backfills"] == 1)
        check("finalizes zero", rec["finalizes"] == 0)
        check("repos scan recorded", rec["repos"] == {"discovered": 26, "fetched": 25})
        check("errors counted", rec["errors"] == 1)
        check("errors_detail names the failing branch",
              any(e["branch"] == "fix/z" and e["rc"] == 1 for e in rec["errors_detail"]))
        check("learned_files links the new learning",
              "Acme/gateway/fix/x/Learnings/a.md" in rec["learned_files"])
        check("learned split: learning present, no tickets",
              "Acme/gateway/fix/x/Learnings/a.md" in rec["learned"]["learnings"] and rec["learned"]["tickets"] == [])
        check("touched marks backfill action",
              any(t["branch"] == "fix/y" and t["action"] == "backfill" for t in rec["touched"]))
        check("ts present", isinstance(rec["ts"], str) and "T" in rec["ts"])

        kb.append_sync_history(rec)
        kb.append_sync_history(rec)
        hist = json.loads(kb.SYNC_HISTORY.read_text(encoding="utf-8"))
        check("history accumulates", len(hist) == 2 and hist[-1]["captures"] == 3)

        kb.append_sync_history(rec, cap=1)
        hist = json.loads(kb.SYNC_HISTORY.read_text(encoding="utf-8"))
        check("history rolls to cap", len(hist) == 1)

        # malformed existing file -> recovers, doesn't crash
        kb.SYNC_HISTORY.write_text("{bad json", encoding="utf-8")
        kb.append_sync_history(rec)
        hist = json.loads(kb.SYNC_HISTORY.read_text(encoding="utf-8"))
        check("recovers from malformed history", isinstance(hist, list) and len(hist) == 1)


def main():
    test()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
