#!/usr/bin/env bash
# SessionStart hook — writes a sidecar linking session_id <-> current branch.
#
# Resolves "which branch does this session belong to?" deterministically at
# start time. Schedulers read ~/.claude/state/kb-session-branch-*.json instead
# of grepping the JSONL transcripts.
#
# Manual override: /kb-mark <branch> updates the sidecar (manual_override=true flag).
#
# Safety:
#   - Kill-switch shared with the other KB hooks.
#   - Silent failure (exit 0) on any error — SessionStart is a critical path.
#   - Does not block stdin/stdout (only writes a file).

set -u

[ -f "$HOME/.claude/kb-hooks-disabled" ] && exit 0
[ "${KB_HOOKS_DISABLED:-0}" = "1" ] && exit 0

input=$(cat 2>/dev/null || true)
[ -z "$input" ] && exit 0

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

# Parse payload
session_id=$(printf '%s' "$input" | "$PY" -c "import sys,json
try: print(json.load(sys.stdin).get('session_id',''))
except: pass" 2>/dev/null)
cwd=$(printf '%s' "$input" | "$PY" -c "import sys,json
try: print(json.load(sys.stdin).get('cwd',''))
except: pass" 2>/dev/null)

[ -z "$session_id" ] && exit 0
[ -z "$cwd" ] && cwd="$PWD"

# Branch via git in the session's cwd
branch=$(git -C "$cwd" branch --show-current 2>/dev/null || true)

state_dir="$HOME/.claude/state"
mkdir -p "$state_dir" 2>/dev/null
sidecar="$state_dir/kb-session-branch-${session_id}.json"

# cygpath converts a Git Bash path (/c/...) to Windows (C:\...) — Python on Windows can't mount /c/
if command -v cygpath >/dev/null 2>&1; then
  sidecar_win=$(cygpath -w "$sidecar")
else
  sidecar_win="$sidecar"
fi

KB_SIDECAR="$sidecar_win" \
KB_SID="$session_id" \
KB_BRANCH_VAL="$branch" \
KB_CWD="$cwd" \
"$PY" <<'PYEOF' 2>/dev/null
import json, os, time
sidecar = os.environ['KB_SIDECAR']

# Preserve the manual mark on session resume: if the sidecar exists with
# manual_override=true, keep branch + manual_override and only update started_at + cwd.
existing = None
try:
    with open(sidecar, 'r', encoding='utf-8') as f:
        existing = json.load(f)
except Exception:
    existing = None

if existing and existing.get('manual_override') is True:
    data = dict(existing)
    data['session_id'] = os.environ.get('KB_SID', data.get('session_id', ''))
    data['cwd'] = os.environ.get('KB_CWD', data.get('cwd', ''))
    data['resumed_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
else:
    data = {
        'session_id': os.environ.get('KB_SID', ''),
        'branch': os.environ.get('KB_BRANCH_VAL', ''),
        'cwd': os.environ.get('KB_CWD', ''),
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'auto': True,
        'manual_override': False,
    }

with open(sidecar, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False)
PYEOF

exit 0
