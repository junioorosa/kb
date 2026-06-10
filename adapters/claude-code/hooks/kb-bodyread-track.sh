#!/usr/bin/env bash
# kb-bodyread-track.sh — wrapper for the PostToolUse body-read tracker.
# Mirrors the other KB hook wrappers (bash wrapper + separate executable).

set -u

KB="${KB_HOME:-$HOME/.kb}"
[ -f "$KB/hooks-disabled" ] && exit 0
[ -f "$HOME/.claude/kb-hooks-disabled" ] && exit 0  # legacy kill-switch, still honored
[ "${KB_HOOKS_DISABLED:-0}" = "1" ] && exit 0

PY="${KB_PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python >/dev/null 2>&1; then PY=python
  elif command -v python3 >/dev/null 2>&1; then PY=python3
  else exit 0
  fi
fi

script="$KB/engine/kb-bodyread-track.py"
[ -f "$script" ] || script="$HOME/.claude/hooks/kb-bodyread-track.py"  # pre-0.11 layout
if command -v cygpath >/dev/null 2>&1; then
  script=$(cygpath -w "$script")
fi

exec "$PY" "$script"
