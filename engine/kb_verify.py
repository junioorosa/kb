#!/usr/bin/env python3
"""kb_verify.py — verify-by-ablation: the empirical gold gate.

Capture and finalize self-judge whether a learning is gold (the WORTH_SAVING_TEST
gates). But the producer writes the note with the diff and the solution in front of
it — curse of knowledge — so it cannot reliably tell whether a FUTURE model lacking
that context would already know the thing. This module settles it empirically:

  derive the question the note answers  ->  answer it COLD (no note, no diff)  ->
  judge whether the cold answer already states the note's specific claim.

If the cold answer already nails it, the note is regenerable from the model's training
and general reasoning -> it is noise that dilutes retrieval -> DROP. If the cold answer
misses/contradicts it, the note carries something the model can't produce on its own ->
GOLD -> KEEP. (A note derivable from the *code* but not from training is kept: the cold
baseline has no repo access, so it cannot reproduce code-hidden facts — exactly the
"from the code but cross-file / hidden -> SAVE" half of GATE 2.)

Producer-agnostic: runs on whatever learning files a capture/finalize call wrote or
modified (covers BOTH; never touches human edits made between syncs), and also as a
batch sweep over the existing vault (`kb verify --workspace X`).

Safety — the vault is the sensitive point ("a bad write poisons future retrieval"):
  * CONSERVATIVE. Any uncertainty, judge low-confidence, parse error, or call failure
    -> KEEP. A flaky judge must never delete gold; a drop needs a clear, confident
    verdict (regenerable AND high confidence).
  * STRICT judge. The cold answer must state the note's SPECIFIC actionable claim as a
    clear/primary point — not merely gesture near the topic among a list of guesses
    (the "10 plausible bugs" failure mode observed in testing).
  * NO-LEAK probe. The derived probe is a forward task that must not reveal the note's
    own conclusion, else the baseline trivially "regenerates" it.
  * AUDITABLE + no re-add loop. A drop appends a `## Verify-dropped` trace to the ticket
    `_index.md` (same shape as `## Consolidation history`); capture/finalize read it and
    must not re-propose the dropped note. The file is removed but the reason survives.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import date
from pathlib import Path


DEFAULT_MAX_TURNS = 8        # each verify sub-call is a single focused question
DEFAULT_VERIFY_CAP = 40      # learnings verified per run (batch sweep); rest deferred


# --- headless claude (local, so verify is decoupled from kb-sync, like consolidate) ---

def claude_run(prompt: str, max_turns: int = DEFAULT_MAX_TURNS, timeout: int = 300):
    """Returns (rc, stdout, stderr). rc != 0 on any failure — callers treat failure as
    KEEP (never delete on a broken call)."""
    cmd = ["claude", "--print", "--max-turns", str(max_turns), "--dangerously-skip-permissions"]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except Exception as e:  # noqa: BLE001 - any failure is non-fatal -> KEEP
        return 1, "", f"{type(e).__name__}: {e}"


# --- learning text helpers ---------------------------------------------------

def is_learning(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    return "/Learnings/" in rel and rel.endswith(".md") and not rel.endswith("/_index.md")


def learning_claim(text: str) -> str:
    """The note's core assertion to hand the judge: frontmatter description + the title
    line + first body paragraph. Enough to define 'the specific claim' without dumping
    the whole tutorial."""
    desc = ""
    m = re.search(r"(?m)^description:\s*(.+)$", text)
    if m:
        desc = m.group(1).strip().strip('"').strip("'")
    body = text.split("\n---", 1)[-1] if text.startswith("---") else text
    title = ""
    paras: list[str] = []
    buf: list[str] = []
    for line in body.splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if line.strip().startswith("## "):
            break  # first section heading — first paragraph block captured above it
        if line.strip():
            buf.append(line.strip())
        elif buf:
            paras.append(" ".join(buf))
            buf = []
            if paras:
                break
    if buf and not paras:
        paras.append(" ".join(buf))
    first_para = paras[0] if paras else ""
    parts = [p for p in (desc, title, first_para) if p]
    return "\n".join(parts)[:1200]


def _project_of(rel: str) -> str:
    parts = rel.replace("\\", "/").split("/")
    return parts[1] if len(parts) > 1 else (parts[0] if parts else "this project")


def _ticket_index(vault: Path, learning_rel: str) -> Path | None:
    """The `_index.md` governing a learning: the nearest ancestor that has one. For
    `<ws>/<proj>/<...>/Learnings/x.md` it's `<...>/_index.md`; project/workspace-scope
    learnings have no ticket index (returns None)."""
    p = (vault / learning_rel).parent  # .../Learnings
    p = p.parent                       # ticket (or project/workspace) folder
    for _ in range(3):
        idx = p / "_index.md"
        if idx.is_file():
            return idx
        p = p.parent
    return None


# --- the three ablation sub-calls (prompts kept simple; placeholders, not f-strings,
#     so the JSON example braces below never collide with str.format) ----------------

_DERIVE = """You are preparing a knowledge-retrieval test. Below is a note captured while working on project "<<PROJECT>>".

NOTE:
<<CLAIM>>

Write ONE concrete forward task or question a future engineer on this project could face, for which this note would be the helpful answer. Rules:
- Phrase it as the SITUATION/GOAL, never reveal the note's conclusion, fix, rule, or specific values (no leaking the answer).
- It must be answerable by someone who has NOT seen this note.
- One or two sentences. Output ONLY the task, nothing else."""

_BASELINE = """You are a senior engineer working on project "<<PROJECT>>". Answer this concretely and briefly from your general engineering knowledge and reasoning. Do NOT open or search any files.

TASK: <<PROBE>>

Give your best concrete answer: the decision you'd make and the key facts/gotchas you'd apply. A few bullets, primary points only."""

_JUDGE = """You are strictly judging whether a knowledge note is worth storing, or is redundant with what any competent model already produces.

The TASK posed to a baseline model (which had NO access to the note or the codebase):
<<PROBE>>

The baseline model's ANSWER:
<<BASELINE>>

The NOTE's specific claim (what storing it would add):
<<CLAIM>>

Question: does the baseline answer ALREADY state the note's specific, actionable claim as a CLEAR / PRIMARY point? Judge strictly:
- "regenerable": true ONLY if the baseline clearly and correctly states the SAME specific claim (not just touches the general topic, not buried as one guess among many, not a vaguer/weaker version).
- If the baseline misses it, contradicts it, only gestures near the area, or states it as one of many speculative options -> regenerable: false.
- If the note's value is a project/domain-specific FACT, enum, constant, contract, or non-obvious gotcha the baseline could not know -> regenerable: false.
- "confidence": "high" only when the comparison is clear-cut; otherwise "low".

Output ONLY one JSON object, nothing else:
{"regenerable": true|false, "confidence": "high"|"low", "reason": "<one sentence>"}"""


def _fill(tmpl: str, **kw) -> str:
    out = tmpl
    for k, v in kw.items():
        out = out.replace("<<" + k + ">>", v)
    return out


def _parse_verdict(stdout: str) -> dict | None:
    m = re.search(r"\{.*\}", stdout, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(obj, dict) or "regenerable" not in obj:
        return None
    return obj


def verify_learning(vault: Path, learning_rel: str, max_turns: int = DEFAULT_MAX_TURNS) -> dict:
    """Run the ablation on one learning. Returns a verdict dict:
        {rel, decision: 'keep'|'drop'|'error', regenerable, confidence, reason, probe}
    DROP only when the judge says regenerable AND confidence=='high'. Every other
    outcome (keep, any error, low confidence, unparseable) is 'keep' — conservative."""
    path = vault / learning_rel
    base = {"rel": learning_rel, "decision": "keep", "regenerable": False,
            "confidence": "", "reason": "", "probe": ""}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return {**base, "decision": "error", "reason": f"unreadable: {e}"}
    claim = learning_claim(text)
    if not claim.strip():
        return {**base, "reason": "no extractable claim — kept (conservative)"}
    project = _project_of(learning_rel)

    rc, probe, err = claude_run(_fill(_DERIVE, PROJECT=project, CLAIM=claim), max_turns)
    probe = probe.strip()
    if rc != 0 or not probe:
        return {**base, "decision": "error", "reason": f"derive failed: {err[:120]}"}

    rc, baseline, err = claude_run(_fill(_BASELINE, PROJECT=project, PROBE=probe), max_turns)
    if rc != 0 or not baseline.strip():
        return {**base, "decision": "error", "probe": probe, "reason": f"baseline failed: {err[:120]}"}

    rc, jout, err = claude_run(
        _fill(_JUDGE, PROBE=probe, BASELINE=baseline.strip(), CLAIM=claim), max_turns)
    if rc != 0:
        return {**base, "decision": "error", "probe": probe, "reason": f"judge failed: {err[:120]}"}
    verdict = _parse_verdict(jout)
    if verdict is None:
        return {**base, "decision": "error", "probe": probe, "reason": "judge output unparseable"}

    regenerable = bool(verdict.get("regenerable"))
    confidence = str(verdict.get("confidence", "")).lower()
    reason = str(verdict.get("reason", ""))[:300]
    drop = regenerable and confidence == "high"
    return {"rel": learning_rel, "decision": "drop" if drop else "keep",
            "regenerable": regenerable, "confidence": confidence, "reason": reason, "probe": probe}


def record_drop(vault: Path, verdict: dict) -> bool:
    """Delete the regenerable learning and append a `## Verify-dropped` trace to its
    ticket `_index.md` so capture/finalize won't re-propose it (no re-add loop) and a
    human can audit. Returns True if the file was removed."""
    rel = verdict["rel"]
    learning = vault / rel
    name = Path(rel).stem
    idx = _ticket_index(vault, rel)
    if idx is not None:
        try:
            cur = idx.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            cur = ""
        stamp = date.today().isoformat()
        line = (f"- {stamp} dropped `{name}` — regenerable from general knowledge "
                f"(verify-by-ablation). {verdict.get('reason', '').strip()}")
        if "## Verify-dropped" in cur:
            cur = cur.rstrip() + "\n" + line + "\n"
        else:
            cur = cur.rstrip() + "\n\n## Verify-dropped\n" + line + "\n"
        try:
            idx.write_text(cur, encoding="utf-8")
        except OSError:
            pass
    try:
        learning.unlink()
        return True
    except OSError:
        return False


# --- snapshot diff: which learnings a capture/finalize call wrote or modified --------

def snapshot_learnings(vault: Path, folder_rel: str) -> dict:
    """{learning_rel: (size, mtime_ns)} for every learning under a ticket/project folder.
    Taken before and after a producer call; the diff is exactly that call's writes —
    so verify never touches human edits made between syncs."""
    snap: dict = {}
    root = vault / folder_rel
    if not root.exists():
        return snap
    for md in root.rglob("*.md"):
        rel = str(md.relative_to(vault)).replace("\\", "/")
        if not is_learning(rel):
            continue
        try:
            st = md.stat()
            snap[rel] = (st.st_size, st.st_mtime_ns)
        except OSError:
            continue
    return snap


def changed_learnings(before: dict, after: dict) -> list[str]:
    """Learnings created or modified between two snapshots (new key, or size/mtime
    changed). Deletions are ignored (already gone)."""
    out = []
    for rel, sig in after.items():
        if rel not in before or before[rel] != sig:
            out.append(rel)
    return sorted(out)


def verify_paths(vault: Path, rels: list[str], cap: int | None = None,
                 dry_run: bool = False, log=print) -> dict:
    """Verify a list of learning rels; drop the high-confidence regenerable ones.
    Returns {checked, dropped:[...], kept, errors, deferred}. Respects `cap` (logs the
    rest as deferred — never a silent truncation)."""
    rels = [r for r in rels if is_learning(r)]
    deferred = []
    if cap is not None and len(rels) > cap:
        deferred = rels[cap:]
        rels = rels[:cap]
    dropped, kept, errors = [], 0, 0
    for rel in rels:
        v = verify_learning(vault, rel)
        if v["decision"] == "drop":
            if dry_run:
                log(f"  [verify] WOULD DROP {rel} — {v['reason']}")
            elif record_drop(vault, v):
                log(f"  [verify] dropped {rel} — regenerable ({v['reason']})")
            dropped.append(rel)
        elif v["decision"] == "error":
            errors += 1
            log(f"  [verify] kept {rel} — check failed, conservative keep ({v['reason']})")
        else:
            kept += 1
    if deferred:
        log(f"  [verify] deferred {len(deferred)} learning(s) over cap={cap}: {', '.join(deferred[:5])}{'…' if len(deferred) > 5 else ''}")
    return {"checked": len(rels), "dropped": dropped, "kept": kept,
            "errors": errors, "deferred": deferred}


# --- batch sweep CLI ---------------------------------------------------------

def _gather_workspace_learnings(vault: Path, workspace: str) -> list[str]:
    out = []
    root = vault / workspace
    if not root.is_dir():
        return out
    for md in root.rglob("*.md"):
        if ".obsidian" in md.parts:
            continue
        rel = str(md.relative_to(vault)).replace("\\", "/")
        if is_learning(rel):
            out.append(rel)
    return sorted(out)


def main() -> int:
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import kb_config
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: kb_config not importable: {e}", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser(description="verify-by-ablation sweep over stored learnings.")
    ap.add_argument("--workspace", help="workspace to sweep (default: the only one)")
    ap.add_argument("--project", help="limit to one project (folder name)")
    ap.add_argument("--cap", type=int, default=DEFAULT_VERIFY_CAP, help=f"max learnings this run (default {DEFAULT_VERIFY_CAP})")
    ap.add_argument("--dry-run", action="store_true", help="report verdicts; never delete")
    args = ap.parse_args()

    try:
        vault = Path(kb_config.resolve_vault(strict=True))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: vault unresolved ({e}).", file=sys.stderr)
        return 2
    cfg = kb_config.load_config()
    workspaces = cfg.get("workspaces", []) or []
    ws = args.workspace or (workspaces[0].get("name") if len(workspaces) == 1 else None)
    if not ws:
        print(f"ERROR: multiple workspaces; pass --workspace (one of {[w.get('name') for w in workspaces]})", file=sys.stderr)
        return 2

    rels = _gather_workspace_learnings(vault, ws)
    if args.project:
        rels = [r for r in rels if _project_of(r) == args.project]
    if not rels:
        print(f"No learnings under '{ws}'" + (f"/{args.project}" if args.project else "") + ".")
        return 0
    print(f"verify-by-ablation: {len(rels)} learning(s) under '{ws}'"
          + (f"/{args.project}" if args.project else "")
          + (f"  (cap {args.cap})" if args.cap else "") + (", DRY-RUN" if args.dry_run else ""))
    res = verify_paths(vault, rels, cap=args.cap, dry_run=args.dry_run)
    print(f"\nchecked={res['checked']} dropped={len(res['dropped'])} kept={res['kept']} "
          f"errors={res['errors']} deferred={len(res['deferred'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
