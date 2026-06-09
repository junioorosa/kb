#!/usr/bin/env python
"""kb-stats-intercept — UserPromptSubmit hook.

Detects `/kb-stats` in the user prompt, reads the session's KB token and tier
sidecars, and blocks the prompt before the LLM is invoked. Zero token cost.

Output format expected by Claude Code (UserPromptSubmit):
    {"decision": "block", "reason": "..."}  -> blocks prompt, shows reason
    (nothing / exit 0)                       -> normal flow
"""
from __future__ import annotations

import json
import os
import re
import sys


SLASH_RE = re.compile(r'^/kb-stats(\s+.*)?$')


def emit(msg: str) -> None:
    sys.stdout.write(json.dumps({"decision": "block", "reason": msg}))
    sys.stdout.flush()


def _state_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "state")


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _fmt_n(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _bar(value: int, total: int, width: int = 18) -> str:
    if total <= 0 or value <= 0:
        return ""
    pct = value / total
    filled = max(1, int(round(width * pct)))
    filled = min(filled, width)
    return f"[{'#' * filled}{'-' * (width - filled)}] {pct * 100:4.1f}%"


def main() -> int:
    if os.environ.get("KB_HOOKS_DISABLED") == "1":
        return 0
    if os.path.isfile(os.path.expanduser("~/.claude/kb-hooks-disabled")):
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    prompt = (payload.get("prompt") or "").strip()
    session_id = payload.get("session_id") or ""

    if not SLASH_RE.match(prompt):
        return 0

    if not session_id:
        emit("KB · /kb-stats couldn't run: no session id in the hook payload.")
        return 0

    sd = _state_dir()
    tier_state = _load_json(os.path.join(sd, f"kb-tier-{session_id}.json"))
    tok_state = _load_json(os.path.join(sd, f"kb-tokens-{session_id}.json"))
    read_state = _load_json(os.path.join(sd, f"kb-bodyread-{session_id}.json"))

    if not tok_state:
        emit("KB · no stats yet for this session.\n"
             "Send a prompt that triggers KB retrieval, then run /kb-stats again.")
        return 0

    total = int(tok_state.get("total", 0))
    prompts = int(tok_state.get("prompts", 0))
    avg = (total / prompts) if prompts else 0
    exact = bool(tok_state.get("exact_tokens", False))
    last = tok_state.get("last") or {}
    by_tier = tok_state.get("by_tier") or {}
    by_section = tok_state.get("by_section") or {}

    method = "tiktoken cl100k_base (~5-10% vs Claude)" if exact \
        else "len(utf8) / 4 fallback (~15-20%)"
    last_total = int(last.get("total", 0))
    last_tier = last.get("tier", "?")
    last_sections = last.get("sections") or {}

    if tier_state:
        hit_total = int(tier_state.get("total", 0) or 0)
        hits = int(tier_state.get("hits", 0) or 0)
        hit_pct = (hits / hit_total * 100) if hit_total else 0
    else:
        hit_pct = 0

    lines = []
    lines.append("KB · session stats")
    lines.append(f"session : {session_id[:12]}...")
    lines.append(f"counter : {method}")
    lines.append("")
    lines.append(f"prompts with KB injection : {prompts}")
    tier_bits = [f"{t}={by_tier[t]}" for t in ("high", "mid", "low", "none") if by_tier.get(t)]
    if tier_bits:
        lines.append(f"  tiers                   : {' '.join(tier_bits)}")
    lines.append(f"  hit-rate (>=mid)        : {hit_pct:.0f}%")
    lines.append("")
    lines.append(f"tokens injected (cumulative) : {_fmt_n(total)}")
    lines.append(f"avg per prompt               : {_fmt_n(int(avg))}")
    if by_section and total > 0:
        lines.append("breakdown by section:")
        for k, v in sorted(by_section.items(), key=lambda kv: -kv[1]):
            if v > 0:
                lines.append(f"  {k:<14} {_fmt_n(v):>8}  {_bar(v, total)}")
    lines.append("")
    lines.append(f"last prompt : {_fmt_n(last_total)} tokens (tier={last_tier})")
    if last_sections:
        nonzero = [(k, v) for k, v in last_sections.items() if v > 0]
        for k, v in sorted(nonzero, key=lambda kv: -kv[1]):
            lines.append(f"  {k:<14} {_fmt_n(v):>8}")

    # ---- body-reads (KB actually consumed, not just injected) ----
    lines.append("")
    lines.append("body-reads (vault bodies pulled into context):")
    if read_state:
        cited_r = int(read_state.get("cited_reads", 0))
        cited_t = int(read_state.get("cited_read_tokens", 0))
        vault_r = int(read_state.get("vault_reads", 0))
        vault_t = int(read_state.get("vault_read_tokens", 0))
        by_tool = read_state.get("by_tool") or {}
        lines.append(f"  cited (KB consumed) : {cited_r} reads, {_fmt_n(cited_t)} tokens")
        lines.append(f"  vault total         : {vault_r} reads, {_fmt_n(vault_t)} tokens")
        if by_tool:
            tbits = " ".join(f"{t.split('__')[-1]}={n}" for t, n in by_tool.items())
            lines.append(f"  by tool             : {tbits}")
    else:
        lines.append("  none yet (no vault body read this session)")

    emit("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
