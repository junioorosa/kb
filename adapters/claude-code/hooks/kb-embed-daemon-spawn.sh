#!/usr/bin/env bash
# SessionStart hook — ensures the kb-embed-daemon is running so the
# UserPromptSubmit retrieval hook gets <50ms semantic search instead of
# falling back to BM25 on its first call after reboot.
#
# Safe: if the daemon is already alive (lockfile + reachable), do nothing.
# Non-blocking: spawns detached via `cmd /c start` and exits ~immediately.
# Silent: never echoes to stdin/stdout (SessionStart respects that).

set -u

KB="${KB_HOME:-$HOME/.kb}"
[ -f "$KB/hooks-disabled" ] && exit 0
[ -f "$HOME/.claude/kb-hooks-disabled" ] && exit 0  # legacy kill-switch, still honored
[ "${KB_HOOKS_DISABLED:-0}" = "1" ] && exit 0

LOCK="$KB/state/kb-embed-daemon.lock"

# Resolve the interpreter up front (the probes below use it too): KB_PYTHON,
# then the managed venv where the optional deps live, then PATH. Bare `python`
# does not exist on the many systems that ship only `python3`.
PY="${KB_PYTHON:-}"
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

# Already running? Verify lockfile and that something answers the port.
if [ -f "$LOCK" ]; then
  PORT=$("$PY" -c "import json,sys
try:
    d=json.load(open(r'$LOCK',encoding='utf-8'))
    print(d.get('port',''))
except Exception:
    pass" 2>/dev/null)
  if [ -n "$PORT" ]; then
    # quick TCP probe; if anything answers, assume alive
    if "$PY" -c "
import socket
s=socket.socket(); s.settimeout(0.4)
try:
    s.connect(('127.0.0.1', $PORT)); s.sendall(b'{\"op\":\"ping\"}\n'); s.recv(64)
    raise SystemExit(0)
except OSError:
    raise SystemExit(1)
finally:
    s.close()
" 2>/dev/null; then
      exit 0
    fi
  fi
  # Lock present but nobody answered. Leave it in place: the daemon's main()
  # reads the recorded pid to terminate the defunct owner (socket gone after
  # sleep/resume) and rebind. Removing it here would discard that pid.
fi

DAEMON="$KB/engine/kb-embed-daemon.py"
[ -f "$DAEMON" ] || DAEMON="$HOME/.claude/scripts/kb-embed-daemon.py"  # pre-0.11 layout
[ -f "$DAEMON" ] || exit 0

# Detached spawn that survives the SessionStart hook process.
# Python's Popen with Windows DETACHED_PROCESS flag is the most reliable way —
# `cmd /c start` here loses the child when this bash shell finishes.
KB_EMBED_DAEMON_PRELOAD=1 "$PY" - "$DAEMON" >/dev/null 2>&1 <<'PYEOF'
import os, sys, subprocess
daemon = sys.argv[1]
kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True)
if os.name == "nt":
    DETACHED = 0x00000008
    NEW_PG   = 0x00000200
    NO_WIN   = 0x08000000
    kwargs["creationflags"] = DETACHED | NEW_PG | NO_WIN
else:
    kwargs["start_new_session"] = True
env = os.environ.copy()
env["KB_EMBED_DAEMON_PRELOAD"] = "1"
subprocess.Popen([sys.executable, daemon], env=env, **kwargs)
PYEOF

exit 0
