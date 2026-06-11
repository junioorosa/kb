#!/usr/bin/env python3
"""kb-statusline.py -- Claude Code statusLine command for the KB integration.

stdin: the Claude Code statusline payload (JSON); the fields read are
session_id, model.display_name and workspace.current_dir.

stdout (single line, ANSI 256-color):
    <model> | <dir> | [KB <health> B:<branch>* <tier> <hits>/<total>]

With --fragment only the [KB ...] segment is emitted, for users who already
have a statusline and want to embed the KB segment in their own composition:
    bash ~/.kb/engine/kb-statusline.sh --fragment

State read (never written):
    <KB_HOME>/state/kb-tier-<session>.json          last_tier + hits/total
    <KB_HOME>/state/kb-session-branch-<sid>.json    branch + manual_override
    <KB_HOME>/state/kb-embed-daemon.lock            daemon port (health ping)
    <KB_HOME>/hooks-disabled                        kill switch (+ legacy
    ~/.claude/kb-hooks-disabled and KB_HOOKS_DISABLED=1)

Output examples:
    [KB OK B:feat/foo* H 4/7]   daemon up, model loaded     (check, green)
    [KB ~  B:feat/foo* H 4/7]   daemon up, model loading    (loop,  cyan)
    [KB W  B:feat/foo* H 4/7]   daemon down/mute -> BM25    (warn,  orange)
    [KB X ]                     hooks disabled (kill)       (cross, gray)

Health is a ping: green only when the daemon answers AND model_loaded is true
(embedding retrieval ready). Reachable-but-loading is a distinct state, NOT the
BM25-fallback warn, so a fresh daemon's background model load doesn't flash a
false warning. A port that connects but won't answer the ping (defunct socket
after sleep) stays warn.

A statusline must never break the host UI: any unexpected error prints nothing
and exits 0.
"""

import json
import os
import re
import socket
import sys
from pathlib import Path

GLYPH_OK = "✓"      # check mark
GLYPH_WARN = "❗"    # heavy exclamation
GLYPH_LOAD = "↻"    # clockwise open-circle arrow, "model warming up"
GLYPH_CROSS = "✗"   # ballot x
GLYPH_DASH = "—"    # em dash, "tier=none"

BLUE = "75"
GREEN = "82"
ORANGE = "214"
CYAN = "44"
GRAY = "244"
RED = "196"
SEP_COLOR = "240"
MODEL_COLOR = "141"
DIR_COLOR = "250"


def color(code: str, text: str) -> str:
    return f"\x1b[38;5;{code}m{text}\x1b[0m"


def kb_home() -> Path:
    env = os.environ.get("KB_HOME")
    if env and env.strip():
        return Path(env.strip())
    return Path.home() / ".kb"


def hooks_disabled() -> bool:
    if os.environ.get("KB_HOOKS_DISABLED") == "1":
        return True
    if (kb_home() / "hooks-disabled").exists():
        return True
    return (Path.home() / ".claude" / "kb-hooks-disabled").exists()  # legacy kill-switch


def read_json_safe(path: Path):
    """Sidecar reader: refuses symlinks and oversized files, never raises."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > 65536:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def daemon_health(state_dir: Path) -> tuple:
    """(glyph, color) per the ping contract in the module docstring."""
    lock = read_json_safe(state_dir / "kb-embed-daemon.lock")
    port = 0
    if isinstance(lock, dict):
        try:
            port = int(lock.get("port", 0))
        except Exception:
            port = 0
    if port <= 0:
        return GLYPH_WARN, ORANGE
    try:
        # `ping` never needs the model loaded, so it answers instantly even
        # mid-load -- letting us tell "loading" from "down". A single TCP
        # segment carries the whole reply, so one recv gets the full line.
        with socket.create_connection(("127.0.0.1", port), timeout=0.2) as s:
            s.settimeout(0.3)
            s.sendall(b'{"op":"ping"}\n')
            data = s.recv(1024)
        if not data:
            return GLYPH_WARN, ORANGE
        pong = json.loads(data.decode("utf-8"))
        if isinstance(pong, dict) and pong.get("model_loaded") is False:
            return GLYPH_LOAD, CYAN
        return GLYPH_OK, GREEN  # up + ready (or an old daemon without the flag)
    except Exception:
        return GLYPH_WARN, ORANGE


def kb_segment(payload: dict) -> str:
    if hooks_disabled():
        return color(GRAY, f"[KB {GLYPH_CROSS}]")

    sid = str(payload.get("session_id") or "")
    safe = re.sub(r"[^a-zA-Z0-9\-_]", "", sid)
    if not safe:
        return ""
    state = kb_home() / "state"

    branch_seg = ""
    sidecar = read_json_safe(state / f"kb-session-branch-{safe}.json")
    if isinstance(sidecar, dict):
        branch = str(sidecar.get("branch") or "").strip()
        if branch:
            # Shortens "feat/39458-dashboard-..." to "feat/39458"; falls back
            # to the full name.
            m = re.match(r"^([^/]+/(?:\d+|[A-Za-z]+-\d+))", branch)
            short = m.group(1) if m else branch
            star = "*" if sidecar.get("manual_override") else ""
            branch_seg = f" B:{short}{star}"

    tier, hits, total = "none", 0, 0
    tier_state = read_json_safe(state / f"kb-tier-{safe}.json")
    if isinstance(tier_state, dict):
        tier = str(tier_state.get("last_tier") or "none")
        try:
            hits = int(tier_state.get("hits", 0))
        except Exception:
            hits = 0
        try:
            total = int(tier_state.get("total", 0))
        except Exception:
            total = 0

    tier_glyph = {"high": "H", "mid": "M", "low": "L"}.get(tier, GLYPH_DASH)
    tier_color = {"high": RED, "mid": ORANGE, "low": GRAY}.get(tier, GRAY)
    health_glyph, health_color = daemon_health(state)

    # Each segment carries its own ANSI color so the [0m reset between them
    # does not bleed into the next part.
    parts = [color(BLUE, "[KB "), color(health_color, health_glyph)]
    if branch_seg:
        parts.append(color(BLUE, branch_seg))
    parts.append(color(BLUE, " "))
    parts.append(color(tier_color, tier_glyph))
    if total > 0:
        parts.append(color(BLUE, f" {hits}/{total}"))
    parts.append(color(BLUE, "]"))
    return "".join(parts)


def build_line(payload: dict, fragment: bool) -> str:
    kb = kb_segment(payload)
    if fragment:
        return kb

    bits = []
    model = payload.get("model")
    if isinstance(model, dict):
        name = str(model.get("display_name") or "").strip()
        if name:
            bits.append(color(MODEL_COLOR, name))
    ws = payload.get("workspace")
    if isinstance(ws, dict):
        cur = str(ws.get("current_dir") or "").strip()
        if cur:
            bits.append(color(DIR_COLOR, Path(cur).name or cur))
    if kb:
        bits.append(kb)
    return color(SEP_COLOR, " | ").join(bits)


def main() -> None:
    fragment = "--fragment" in sys.argv[1:]
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    out = build_line(payload, fragment)
    if out:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        sys.stdout.write(out)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a statusline never breaks the host UI
    sys.exit(0)
