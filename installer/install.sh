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
while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY="--apply"; shift ;;
        --time)  TIME="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"

# --- Locate Python ----------------------------------------------------------
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    echo "ERROR: Python 3 not found on PATH. Install Python 3, then re-run." >&2
    exit 1
fi
echo "Python: $PY"

# --- Optional deps (graceful: KB degrades to BM25 without them) -------------
echo "Installing optional deps (fastembed, numpy, tiktoken)..."
if ! "$PY" -m pip install --quiet fastembed numpy tiktoken; then
    echo "  deps install failed -- continuing (semantic retrieval will degrade to BM25)."
fi

# --- Hand off to the orchestrator -------------------------------------------
echo ""
# shellcheck disable=SC2086
"$PY" "$HERE/install.py" --time "$TIME" $APPLY
code=$?
if [ -z "$APPLY" ]; then
    echo ""
    echo "Dry-run only. Re-run with --apply to install/update."
fi
exit $code
