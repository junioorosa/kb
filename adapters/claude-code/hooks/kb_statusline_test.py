#!/usr/bin/env python3
"""Tests for kb-statusline.py — the Claude Code statusLine command.

Runs the script as a subprocess exactly like the host does (payload on stdin,
line on stdout), against a temp KB_HOME with fixture sidecars and a fake
embedding daemon on loopback. Asserts on ANSI-stripped text.

The cardinal contract: a statusline NEVER breaks the host UI — garbage input,
missing state, dead sockets all exit 0.

Run: python adapters/claude-code/hooks/kb_statusline_test.py
"""

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "kb-statusline.py"
PASS, FAIL = 0, 0

GLYPH_OK, GLYPH_WARN, GLYPH_LOAD, GLYPH_CROSS, GLYPH_DASH = (
    "✓", "❗", "↻", "✗", "—")


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def run(payload, home: Path, args=(), env_extra=None):
    """Run the statusline like the host: JSON on stdin, capture stdout."""
    env = dict(os.environ)
    env["KB_HOME"] = str(home / ".kb")
    # Point HOME/USERPROFILE at the temp dir so the legacy kill-switch check
    # never reads the developer's real ~/.claude.
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("KB_HOOKS_DISABLED", None)
    if env_extra:
        env.update(env_extra)
    raw = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    p = subprocess.run([sys.executable, str(SCRIPT), *args],
                       input=raw, capture_output=True, env=env, timeout=30)
    return p.returncode, strip_ansi(p.stdout.decode("utf-8", "replace"))


def payload(session="abc-123", model="Opus 4.8", cwd="C:/work/myproj"):
    return {"session_id": session,
            "model": {"display_name": model},
            "workspace": {"current_dir": cwd}}


def write_state(home: Path, name: str, obj) -> None:
    state = home / ".kb" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / name).write_text(json.dumps(obj), encoding="utf-8")


class FakeDaemon:
    """One-shot loopback daemon answering the ping like kb-embed-daemon."""

    def __init__(self, model_loaded):
        self.model_loaded = model_loaded
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.t = threading.Thread(target=self._serve, daemon=True)
        self.t.start()

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
            conn.settimeout(2)
            conn.recv(1024)
            conn.sendall((json.dumps({"ok": True, "model_loaded": self.model_loaded}) + "\n").encode())
            conn.close()
        except Exception:
            pass

    def close(self):
        try:
            self.srv.close()
        except Exception:
            pass


def test_never_breaks():
    print("test_never_breaks")
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        rc, out = run("", home)
        check("empty stdin exits 0", rc == 0)
        rc, out = run(b"{not json", home)
        check("garbage stdin exits 0", rc == 0)
        rc, out = run(json.dumps([1, 2]), home)
        check("non-object payload exits 0", rc == 0)
        rc, out = run(payload(session="../../etc"), home)
        check("hostile session_id exits 0", rc == 0)


def test_prefix_without_session():
    print("test_prefix_without_session")
    with tempfile.TemporaryDirectory() as d:
        rc, out = run(payload(session=""), Path(d))
        check("model shown", "Opus 4.8" in out)
        check("dir basename shown", "myproj" in out and "C:/work" not in out)
        check("no KB segment without session", "[KB" not in out)


def test_no_state_no_daemon():
    print("test_no_state_no_daemon")
    with tempfile.TemporaryDirectory() as d:
        rc, out = run(payload(), Path(d))
        check("KB segment present", "[KB" in out)
        check("daemon down -> warn", GLYPH_WARN in out)
        check("no retrieval yet -> dash tier", GLYPH_DASH in out)
        check("no branch segment", "B:" not in out)


def test_sidecars():
    print("test_sidecars")
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        write_state(home, "kb-tier-abc-123.json", {"last_tier": "high", "hits": 3, "total": 4})
        write_state(home, "kb-session-branch-abc-123.json",
                    {"branch": "feat/39458-dashboard-revamp", "manual_override": True})
        rc, out = run(payload(), home)
        check("branch shortened", "B:feat/39458" in out and "dashboard" not in out)
        check("manual star", "B:feat/39458*" in out)
        check("tier glyph H", " H" in out)
        check("hit ratio", "3/4" in out)


def test_branch_no_id_kept_whole():
    print("test_branch_no_id_kept_whole")
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        write_state(home, "kb-session-branch-abc-123.json", {"branch": "feat/login"})
        rc, out = run(payload(), home)
        check("non-numeric slug kept whole", "B:feat/login" in out)
        check("no star without manual_override", "B:feat/login*" not in out)


def test_kill_switch():
    print("test_kill_switch")
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        rc, out = run(payload(), home, env_extra={"KB_HOOKS_DISABLED": "1"})
        check("env kill-switch -> cross", f"[KB {GLYPH_CROSS}]" in out)
        (home / ".kb").mkdir(parents=True, exist_ok=True)
        (home / ".kb" / "hooks-disabled").write_text("", encoding="utf-8")
        rc, out = run(payload(), home)
        check("flag-file kill-switch -> cross", f"[KB {GLYPH_CROSS}]" in out)
        check("model prefix still shown when killed", "Opus 4.8" in out)


def test_fragment_mode():
    print("test_fragment_mode")
    with tempfile.TemporaryDirectory() as d:
        rc, out = run(payload(), Path(d), args=("--fragment",))
        check("fragment has KB segment", "[KB" in out)
        check("fragment has no model prefix", "Opus 4.8" not in out)
        check("fragment has no dir", "myproj" not in out)


def test_daemon_states():
    print("test_daemon_states")
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        daemon = FakeDaemon(model_loaded=True)
        try:
            write_state(home, "kb-embed-daemon.lock", {"port": daemon.port})
            rc, out = run(payload(), home)
            check("loaded daemon -> check", GLYPH_OK in out)
        finally:
            daemon.close()

        daemon = FakeDaemon(model_loaded=False)
        try:
            write_state(home, "kb-embed-daemon.lock", {"port": daemon.port})
            rc, out = run(payload(), home)
            check("loading daemon -> warming glyph", GLYPH_LOAD in out)
        finally:
            daemon.close()

        # Lock points at a port nobody listens on -> warn, fast.
        write_state(home, "kb-embed-daemon.lock", {"port": daemon.port})
        rc, out = run(payload(), home)
        check("dead port -> warn", GLYPH_WARN in out)


def main():
    test_never_breaks()
    test_prefix_without_session()
    test_no_state_no_daemon()
    test_sidecars()
    test_branch_no_id_kept_whole()
    test_kill_switch()
    test_fragment_mode()
    test_daemon_states()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
