#!/usr/bin/env bash
# UserPromptSubmit hook — KB context injection (hybrid embedding + BM25).
#
# Pipeline lives in kb_retrieve.py. This script:
#   1. Honors kill-switch (file + env).
#   2. Detects current git branch and exports it (additive ticket context).
#   3. Pipes stdin JSON to Python script.
#
# DESIGN: always-on. No folder-existence gate. Cross-ticket from start.
# Budget/caps/safety enforced inside kb_retrieve.py.

set -u

# ========== KILL-SWITCH ==========
[ -f "$HOME/.claude/kb-hooks-disabled" ] && exit 0
[ "${KB_HOOKS_DISABLED:-0}" = "1" ] && exit 0

# ========== BRANCH (additive ticket context — not a gate) ==========
if [ -z "${KB_BRANCH:-}" ]; then
  KB_BRANCH=$(git branch --show-current 2>/dev/null || true)
fi
export KB_BRANCH

# ========== PYTHON DISPATCH ==========
PY="${KB_PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python >/dev/null 2>&1; then
    PY=python
  elif command -v python3 >/dev/null 2>&1; then
    PY=python3
  else
    exit 0
  fi
fi

# Engine boundary: the hook talks to the `kb` CLI, not to internals directly.
# `kb retrieve` reads this stdin payload and prints the <vault-context>. The
# retrieval pipeline still lives in kb_retrieve.py (imported by the CLI), which
# also stays runnable standalone for back-compat.
SCRIPT="$HOME/.claude/hooks/kb.py"
[ -f "$SCRIPT" ] || exit 0

exec "$PY" "$SCRIPT" retrieve
