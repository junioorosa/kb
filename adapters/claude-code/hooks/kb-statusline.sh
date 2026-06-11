#!/usr/bin/env bash
# kb-statusline.sh -- wrapper for the Python statusline (Claude Code statusLine
# command). Unlike the hook wrappers it does NOT exit silently on the
# kill-switch: the Python emits a gray [KB x] so the user can SEE the
# integration is off. stdin (the statusline payload) passes through exec.
# Extra args (e.g. --fragment) are forwarded.

set -u

KB="${KB_HOME:-$HOME/.kb}"

PY="${KB_PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python >/dev/null 2>&1; then PY=python
  elif command -v python3 >/dev/null 2>&1; then PY=python3
  else exit 0
  fi
fi

script="$KB/engine/kb-statusline.py"
[ -f "$script" ] || exit 0
if command -v cygpath >/dev/null 2>&1; then
  script=$(cygpath -w "$script")
fi

exec "$PY" "$script" "$@"
