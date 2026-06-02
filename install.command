#!/usr/bin/env bash
# KB installer -- double-click this on macOS (Finder runs .command in Terminal).
# Installs/updates KB and opens the manager in your browser.
# Extra args pass through to install.sh (e.g. --no-manager, --time 02:30).
cd "$(dirname "$0")" || exit 1
bash installer/install.sh --apply "$@"
