#!/usr/bin/env bash
# kb-stats-intercept.sh — wrapper for the Python interceptor.
# Mirrors kb-mark-intercept.sh (bash wrapper + separate executable).

set -u

KB="${KB_HOME:-$HOME/.kb}"
[ -f "$KB/hooks-disabled" ] && exit 0
[ -f "$HOME/.claude/kb-hooks-disabled" ] && exit 0  # legacy kill-switch, still honored
[ "${KB_HOOKS_DISABLED:-0}" = "1" ] && exit 0

PY="${KB_PYTHON:-}"
# Prefer the managed venv (installer/install.sh) before PATH python — that's
# where the optional deps live on PEP 668 / externally-managed systems.
if [ -z "$PY" ]; then
  for cand in "$KB/venv/bin/python" "$KB/venv/Scripts/python.exe"; do
    [ -x "$cand" ] && { PY="$cand"; break; }
  done
fi
if [ -z "$PY" ]; then
  if command -v python >/dev/null 2>&1; then PY=python
  elif command -v python3 >/dev/null 2>&1; then PY=python3
  else exit 0
  fi
fi

script="$KB/engine/kb-stats-intercept.py"
[ -f "$script" ] || script="$HOME/.claude/hooks/kb-stats-intercept.py"  # pre-0.11 layout
if command -v cygpath >/dev/null 2>&1; then
  script=$(cygpath -w "$script")
fi

exec "$PY" "$script"
