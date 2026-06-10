#!/usr/bin/env bash
# KB bootstrap — one-line install/update (macOS / Linux / WSL / Git Bash).
#
#   curl -fsSL https://raw.githubusercontent.com/junioorosa/kb/main/bootstrap.sh | bash
#
# Re-running is always safe: the clone is updated (ff-only) and the installer
# is idempotent (diffs first, backs up what it overwrites).
#
# Overrides: KB_REPO (clone URL or local path), KB_APP_DIR (clone destination,
# default ~/.kb/app), KB_BOOTSTRAP_NO_INSTALL=1 (clone/update only, no install).
set -eu

REPO_URL="${KB_REPO:-https://github.com/junioorosa/kb.git}"
APP_DIR="${KB_APP_DIR:-$HOME/.kb/app}"

if ! command -v git >/dev/null 2>&1; then
  echo "bootstrap: git is required" >&2
  exit 1
fi

if [ -d "$APP_DIR/.git" ]; then
  echo "bootstrap: updating $APP_DIR"
  git -C "$APP_DIR" pull --ff-only --quiet
else
  echo "bootstrap: cloning $REPO_URL -> $APP_DIR"
  mkdir -p "$(dirname "$APP_DIR")"
  if ! git clone --quiet "$REPO_URL" "$APP_DIR" 2>/dev/null; then
    # A plain https clone of a private fork fails without credentials; gh
    # carries the user's auth, so retry through it before giving up.
    if command -v gh >/dev/null 2>&1; then
      echo "bootstrap: plain clone failed — retrying via gh"
      slug=$(printf '%s' "$REPO_URL" | sed -E 's#(git@github\.com:|https://github\.com/)##; s#\.git$##')
      gh repo clone "$slug" "$APP_DIR"
    else
      echo "bootstrap: clone failed. Check the URL (KB_REPO) and your network; a private fork needs 'gh auth login'." >&2
      exit 1
    fi
  fi
fi

if [ "${KB_BOOTSTRAP_NO_INSTALL:-0}" = "1" ]; then
  echo "bootstrap: clone ready (install skipped). Next: bash \"$APP_DIR/installer/install.sh\" --apply"
  exit 0
fi

exec bash "$APP_DIR/installer/install.sh" --apply
