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
KB="${KB_HOME:-$HOME/.kb}"
[ -f "$KB/hooks-disabled" ] && exit 0
[ -f "$HOME/.claude/kb-hooks-disabled" ] && exit 0  # legacy kill-switch, still honored
[ "${KB_HOOKS_DISABLED:-0}" = "1" ] && exit 0

# ========== BRANCH (additive ticket context — not a gate) ==========
if [ -z "${KB_BRANCH:-}" ]; then
  KB_BRANCH=$(git branch --show-current 2>/dev/null || true)
fi
export KB_BRANCH

# ========== PYTHON DISPATCH ==========
PY="${KB_PYTHON:-}"
# Prefer the managed venv (installer/install.sh) before PATH python — that's
# where the optional deps live on PEP 668 / externally-managed systems.
if [ -z "$PY" ]; then
  for cand in "$KB/venv/bin/python" "$KB/venv/Scripts/python.exe"; do
    [ -x "$cand" ] && { PY="$cand"; break; }
  done
fi
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
SCRIPT="$KB/engine/kb.py"
[ -f "$SCRIPT" ] || SCRIPT="$HOME/.claude/hooks/kb.py"  # pre-0.11 layout
[ -f "$SCRIPT" ] || exit 0

exec "$PY" "$SCRIPT" retrieve
