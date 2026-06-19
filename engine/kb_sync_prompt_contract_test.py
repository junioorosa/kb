#!/usr/bin/env python3
"""Contract tests for the capture/finalize prompt templates.

The audit rules ARE the knowledge-integrity policy, and they live as prompt
text — nothing else enforces them. These tests pin the load-bearing clauses:

  * Capture (branch OPEN, code unlanded): may edit only THIS ticket's own
    learnings; cross-ticket / project / workspace corrections are NOT edits but
    a `## Pending challenges` bullet in the ticket's `_index.md`. Rationale:
    a branch that never lands must not have rewritten established knowledge.
  * Finalize (landed in production): cross-scope corrections authorized, and
    pending challenges are re-audited against the landed diff, then the
    section is removed.
  * Both prompts use the standard tools (no MCP) and keep the
    CONFIRMS/REFUTES/ADJUSTS/ADDS taxonomy.

Run: python engine/kb_sync_prompt_contract_test.py
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("kb_sync_under_test", HERE / "kb-sync.py")
kb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kb)

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


CFG = {"vault": "/tmp/vault"}
REPO = SimpleNamespace(name="api-orders")


def build_capture(pre=""):
    return kb.capture_prompt(
        CFG, "MyWorkspace", REPO, "feat/123-pix", "feat", "123-pix",
        "feat/123-pix", "origin/master",
        commits=["abc\t2026-06-12\tmsg"], stat="1 file changed",
        diff="diff --git a/x b/x", hints=[], pre_extracted=pre)


def build_finalize(pre=""):
    ticket = {"path": "MyWorkspace/api-orders/feat/123-pix", "branch": "feat/123-pix"}
    res = {"status": "merged", "integration": "origin/master",
           "method": "ancestor", "landed_date": "2026-06-12"}
    return kb.finalize_prompt(CFG, "MyWorkspace", REPO, ticket, res,
                              landed_diff="diff --git a/x b/x", hints=[], pre_extracted=pre)


def _flat(s: str) -> str:
    """Whitespace-normalized view: the templates hard-wrap sentences."""
    return " ".join(s.split())


def test_capture_gates_cross_scope():
    print("test_capture_gates_cross_scope")
    p = build_capture()
    check("states the branch is unlanded", "has NOT landed" in _flat(p))
    check("own-ticket learnings still editable",
          "THIS ticket's own learnings" in p and "feat/123-pix/Learnings/" in p)
    check("cross-scope files are not edited", "do NOT edit it" in p)
    check("challenge bullet goes to the ticket _index",
          "## Pending challenges" in p and "_index.md" in p)
    check("defers promotion to finalize", "finalize pass re-audits" in p)
    check("policy line present",
          "unlanded code never rewrites established knowledge" in _flat(p))
    check("taxonomy intact", all(k in p for k in ("CONFIRMS", "REFUTES", "ADJUSTS", "ADDS")))
    check("never-delete rule survives", "never delete a file" in p)


def test_finalize_promotes_challenges():
    print("test_finalize_promotes_challenges")
    p = build_finalize()
    check("landed diff is the authority", "HAS landed in production" in p)
    check("cross-scope corrections authorized", "corrections are authorized HERE" in p)
    check("re-audits pending challenges", "## Pending challenges" in p and "re-audit" in p)
    check("clears the section afterwards", "remove the section" in p)
    check("evidence bar for broad scopes kept",
          "Workspace/project-scope corrections need concrete evidence" in p)
    check("taxonomy intact", all(k in p for k in ("CONFIRMS", "REFUTES", "ADJUSTS", "ADDS")))
    # anti-twin guard on ADDS: finalize created the 40643 twin because its ADD rule
    # lacked the "one insight = one file / prefer ADJUST over a near-dup" clause capture has.
    check("finalize ADDS has anti-twin guard",
          "One insight = one file" in p and "near-dup" in _flat(p))
    check("finalize anti-twin refers to step 1's own-ticket learnings",
          "you read in step 1" in _flat(p))


def test_no_mcp_in_either():
    print("test_no_mcp_in_either")
    for name, p in (("capture", build_capture()), ("finalize", build_finalize())):
        check(f"{name} forbids MCP and names the standard tools",
              "do NOT use any MCP server" in p and "Read/Write/Edit/Glob" in p)
        check(f"{name} never mentions obsidian-vault MCP", "obsidian-vault" not in p)


def test_pre_extracted_variants_keep_the_gate():
    print("test_pre_extracted_variants_keep_the_gate")
    pre = "Pre-extracted learnings:\n- [[x.md]] body"
    check("capture pre-extract keeps the challenge flow",
          "## Pending challenges" in build_capture(pre))
    check("finalize pre-extract keeps the promotion flow",
          "remove the section" in build_finalize(pre))


def test_capture_pre_extract_reads_own_ticket_learnings():
    """In pre-extract mode capture must still deterministically read THIS ticket's own
    Learnings dir — the semantic block can miss a same-ticket sibling, and trusting only
    the block is what lets a cross-pass twin slip in. The ticket folder is the match key,
    not a similarity score."""
    print("test_capture_pre_extract_reads_own_ticket_learnings")
    pre = "Pre-extracted learnings:\n- [[x.md]] body"
    p = build_capture(pre)
    flat = _flat(p)
    check("reads this ticket's own Learnings dir deterministically",
          "feat/123-pix/Learnings/" in p and "EVERY file under" in flat)
    check("does not trust the semantic block for same-ticket siblings",
          "do NOT rely on the semantic block to have surfaced a same-ticket sibling" in flat)
    check("audits against the union, not the block alone", "UNION" in p)
    check("still avoids other tickets' learnings",
          "Do NOT browse OTHER tickets'" in flat)


def main():
    test_capture_gates_cross_scope()
    test_finalize_promotes_challenges()
    test_no_mcp_in_either()
    test_pre_extracted_variants_keep_the_gate()
    test_capture_pre_extract_reads_own_ticket_learnings()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
