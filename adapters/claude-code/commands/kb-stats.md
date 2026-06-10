---
description: Show KB token usage and tier stats for the current session (zero-token intercept)
allowed-tools: Bash
argument-hint: (no arguments)
---

# /kb-stats

Reports per-session token usage of the KB context injection (`<vault-context>` block) — what the retrieval hook is actually adding to your prompts, billed every request whether the model attends to it or not.

The numbers come from a sidecar updated by `kb_retrieve.py` after each prompt:

- **Per-prompt total** with section breakdown (header, matches, body excerpt, GraphRAG, ticket, footer).
- **Cumulative session total** and average per prompt.
- **Tier distribution** (high / mid / low / none) and hit-rate (>= mid).

## Counter precision

- If `tiktoken` is installed (`pip install tiktoken`), counts via `cl100k_base` — within ~5-10% of Claude's tokenizer for mixed PT/EN/code.
- Otherwise falls back to `len(utf8) // 4` — within ~15-20%.

The output labels which counter was used.

## Scope: what `/kb-stats` covers

- **Injection** (`UserPromptSubmit`): every token in the `<vault-context>` block. Guaranteed KB cost — billed every prompt.
- **Body-reads** (`PostToolUse`): bodies of vault files pulled into context *after* injection, when the model opens a citation. Tracked via two signals:
  - **cited** — a read of a learning whose basename was cited this session = the real "KB was consumed" metric.
  - **vault total** — any read of a file under the vault (superset; includes maintenance like editing `_index.md`).

  Only the built-in `Read` tool is hooked (filtered to paths under the vault). The injection footer steers all body reads through `Read` with an absolute vault path — it's grep-able and consistent across sessions, and dominates in practice (~16x the obsidian-MCP reads historically).

## Execution

> Note: the `kb-stats-intercept` hook handles `/kb-stats` directly in `UserPromptSubmit` (zero token cost) and blocks the prompt before the LLM. The bash below is a fallback for when the hook is disabled.

Run the following bash:

```bash
state_dir="${KB_HOME:-$HOME/.kb}/state"

# Pick the most recent token sidecar — assume current session
latest=$(ls -t "$state_dir"/kb-tokens-*.json 2>/dev/null | head -1)

if [ -z "$latest" ]; then
  echo "ERROR: no KB token sidecar found — no KB injection has happened yet in any session."
  exit 1
fi

if command -v cygpath >/dev/null 2>&1; then
  latest_win=$(cygpath -w "$latest")
else
  latest_win="$latest"
fi

PY="${KB_PYTHON:-python}"
KB_PATH="$latest_win" "$PY" - <<'PYEOF'
import json, os
p = os.environ['KB_PATH']
with open(p, 'r', encoding='utf-8') as f:
    d = json.load(f)
print(f"session : {d.get('session_id','?')[:12]}...")
print(f"counter : {'tiktoken cl100k_base' if d.get('exact_tokens') else 'len(utf8)/4 fallback'}")
print(f"prompts : {d.get('prompts',0)}")
print(f"total   : {d.get('total',0)} tokens")
prompts = d.get('prompts', 0)
if prompts:
    print(f"avg     : {int(d.get('total',0) / prompts)} tokens / prompt")
print(f"by_tier   : {d.get('by_tier', {})}")
print(f"by_section: {d.get('by_section', {})}")
last = d.get('last') or {}
print(f"last    : {last.get('total',0)} tokens (tier={last.get('tier','?')})")
PYEOF
```

After running, summarize the numbers for the user.
