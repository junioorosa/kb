#!/usr/bin/env bash
# install.sh -- macOS/Linux bootstrap for KB.
#
# The only Unix-specific layer: locate Python, install optional deps, then hand
# off to the OS-agnostic orchestrator (install.py), which does the real work.
#
# Usage (from the repo root):
#   bash installer/install.sh                 # dry-run
#   bash installer/install.sh --apply         # install/update
#   bash installer/install.sh --apply --time 02:30
set -euo pipefail

TIME="01:00"
APPLY=""
NOMANAGER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY="--apply"; shift ;;
        --time)  TIME="$2"; shift 2 ;;
        --no-manager) NOMANAGER="--no-manager"; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"

# --- Locate a base Python ---------------------------------------------------
BASE_PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then BASE_PY="$c"; break; fi
done
if [ -z "$BASE_PY" ]; then
    echo "ERROR: Python 3 not found on PATH. Install Python 3, then re-run." >&2
    exit 1
fi

# --- Managed venv -----------------------------------------------------------
# Optional deps install into a private venv, never the system Python: modern
# distros (and Homebrew) mark the system Python PEP 668 "externally managed",
# which refuses `pip install`. A venv is exempt and leaves the host untouched.
# install.py then runs under this interpreter, so the scheduled-sync job and the
# MCP server command (both recorded from sys.executable) point at it for free.
KB_HOME="${KB_HOME:-$HOME/.kb}"
VENV="$KB_HOME/venv"
PY="$BASE_PY"
if [ -x "$VENV/bin/python" ]; then
    PY="$VENV/bin/python"
elif "$BASE_PY" -m venv "$VENV" >/dev/null 2>&1; then
    PY="$VENV/bin/python"
else
    echo "  note: could not create a virtualenv (Debian/Ubuntu: sudo apt install python3-venv)."
    echo "        Falling back to system Python; optional deps may not install (degrade to BM25)."
fi
echo "Python: $PY"

# --- Optional deps (graceful: KB degrades to BM25 without them) -------------
echo "Installing optional deps (fastembed, numpy, tiktoken)..."
"$PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
if ! "$PY" -m pip install --quiet fastembed numpy tiktoken; then
    echo "  deps install failed -- continuing (semantic retrieval will degrade to BM25)."
fi

# --- Hand off to the orchestrator -------------------------------------------
echo ""
# shellcheck disable=SC2086
"$PY" "$HERE/install.py" --time "$TIME" $APPLY $NOMANAGER
code=$?

if [ -z "$APPLY" ]; then
    echo ""
    echo "Dry-run only. Re-run with --apply to install/update."
    exit $code
fi
if [ "$code" -ne 0 ]; then
    echo "Install reported errors." >&2
    exit $code
fi
echo ""
echo "Done. On a first install the manager opens by itself; any other time, search your apps for 'KB Manager'."
exit $code
