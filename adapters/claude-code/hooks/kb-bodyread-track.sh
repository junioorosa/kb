#!/usr/bin/env bash
# kb-bodyread-track.sh — wrapper for the PostToolUse body-read tracker.
# Mirrors the other KB hook wrappers (bash wrapper + separate executable).

set -u

[ -f "$HOME/.claude/kb-hooks-disabled" ] && exit 0
[ "${KB_HOOKS_DISABLED:-0}" = "1" ] && exit 0

PY="${KB_PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python >/dev/null 2>&1; then PY=python
  elif command -v python3 >/dev/null 2>&1; then PY=python3
  else exit 0
  fi
fi

script="$HOME/.claude/hooks/kb-bodyread-track.py"
if command -v cygpath >/dev/null 2>&1; then
  script=$(cygpath -w "$script")
fi

exec "$PY" "$script"
