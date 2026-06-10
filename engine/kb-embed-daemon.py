#!/usr/bin/env python3
"""Long-lived sidecar holding the embedding model + vault vectors in RAM.

The kb_retrieve hook (and any other caller) connects to this daemon over TCP
loopback instead of re-loading a 220MB model per invocation. Cold model load
costs ~4s and only happens once per reboot; subsequent queries are ~5-50ms.

Protocol (JSON over TCP, line-delimited, UTF-8):
  client -> daemon: {"op": "search", "query": "...", "k": 10, "filter": {...}}
  daemon -> client: {"ok": true, "hits": [...], "took_ms": 42}

  client -> daemon: {"op": "embed", "texts": ["..."]}
  daemon -> client: {"ok": true, "vectors": [[...384...], ...]}

  client -> daemon: {"op": "ping"}
  daemon -> client: {"ok": true, "model": "...", "chunks": 79, "uptime_s": 123}

  client -> daemon: {"op": "reindex"}        -> reload store from disk
  client -> daemon: {"op": "shutdown"}       -> graceful exit

Concurrency: single-threaded request loop is fine — embedding is fast, GIL
holds during the (rare) model.encode call. Multiple concurrent hook calls
serialize, no race on the store.

Lockfile (<kb home>/state/kb-embed-daemon.lock) holds {pid, port, started_at}
so clients/auto-spawn know if it's already running.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME", os.path.expanduser("~")))
KB_HOME = Path(os.environ.get("KB_HOME") or (HOME / ".kb"))
STATE_DIR = KB_HOME / "state"
LOCK_PATH = STATE_DIR / "kb-embed-daemon.lock"
LOG_PATH = KB_HOME / "logs" / "kb-embed-daemon.log"

DEFAULT_PORT = int(os.environ.get("KB_EMBED_DAEMON_PORT", "47821"))
BIND_HOST = "127.0.0.1"
IDLE_SHUTDOWN_S = int(os.environ.get("KB_EMBED_DAEMON_IDLE", "0"))  # 0 = never


def _load_kb_embed():
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("kb_embed", here / "kb-embed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def log(msg: str):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


class DaemonState:
    """Holds the loaded model + store + bookkeeping. Single shared instance."""

    def __init__(self, kbe):
        self.kbe = kbe
        self.store = kbe.VectorStore()
        self.model_loaded = False
        self.started_at = time.time()
        self.last_request = time.time()
        self.req_count = 0
        self.lock = threading.Lock()

    def ensure_model(self):
        if not self.model_loaded:
            t0 = time.perf_counter()
            self.kbe.get_model()
            self.model_loaded = True
            log(f"model loaded ({(time.perf_counter()-t0)*1000:.0f} ms, "
                f"store has {len(self.store.meta)} chunks)")

    def reindex(self):
        """Reload manifest+vectors from disk (someone else ran kb-sync's reindex)."""
        with self.lock:
            self.store = self.kbe.VectorStore()
            log(f"reindexed from disk -> {len(self.store.meta)} chunks")
        return {"chunks": len(self.store.meta)}

    def info(self):
        return {
            "model": self.kbe.MODEL_NAME,
            "dim": self.kbe.DIM,
            "chunks": len(self.store.meta),
            "model_loaded": self.model_loaded,
            "uptime_s": round(time.time() - self.started_at, 1),
            "requests": self.req_count,
        }


STATE: DaemonState | None = None


class Handler(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self):
        global STATE
        try:
            raw = self.rfile.readline()
        except OSError:
            return
        if not raw:
            return
        try:
            msg = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as e:
            self._send({"ok": False, "error": f"bad json: {e}"})
            return
        op = msg.get("op")
        STATE.req_count += 1
        STATE.last_request = time.time()
        try:
            if op == "ping":
                self._send({"ok": True, **STATE.info()})
            elif op == "search":
                self._handle_search(msg)
            elif op == "embed":
                self._handle_embed(msg)
            elif op == "reindex":
                self._send({"ok": True, **STATE.reindex()})
            elif op == "shutdown":
                self._send({"ok": True})
                log("shutdown requested")
                threading.Thread(target=lambda: (time.sleep(0.1), os._exit(0)), daemon=True).start()
            else:
                self._send({"ok": False, "error": f"unknown op: {op}"})
        except Exception as e:
            log(f"handler error op={op}: {type(e).__name__}: {e}")
            self._send({"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _send(self, obj):
        try:
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()
        except OSError:
            pass

    def _handle_search(self, msg):
        t0 = time.perf_counter()
        query = msg.get("query", "")
        k = int(msg.get("k", 10))
        flt = msg.get("filter") or {}
        scope = set(flt["scope"]) if flt.get("scope") else None
        project = flt.get("project")
        branch = flt.get("branch")
        kind = set(flt["kind"]) if flt.get("kind") else None
        min_score = float(flt.get("min_score", 0.0))
        with STATE.lock:
            STATE.ensure_model()
            hits = STATE.kbe.retrieve_top_k(
                query, k=k, scope=scope, project=project, branch=branch,
                kind=kind, store=STATE.store,
            )
        hits = [h for h in hits if h.get("score", 0.0) >= min_score]
        # Strip the bulky raw text from "search" responses — caller can request
        # bodies separately or re-read .md from path. Keep preview + path + score.
        slim = []
        for h in hits:
            slim.append({
                "path": h.get("path"),
                "kind": h.get("kind"),
                "scope": h.get("scope"),
                "project": h.get("project"),
                "branch": h.get("branch"),
                "name": h.get("name"),
                "preview": h.get("preview"),
                "score": h.get("score"),
            })
        self._send({"ok": True, "hits": slim,
                    "took_ms": round((time.perf_counter() - t0) * 1000, 1)})

    def _handle_embed(self, msg):
        t0 = time.perf_counter()
        texts = msg.get("texts") or []
        if not isinstance(texts, list):
            self._send({"ok": False, "error": "texts must be a list"})
            return
        with STATE.lock:
            STATE.ensure_model()
            vecs = STATE.kbe.embed(texts)
        self._send({"ok": True,
                    "vectors": vecs.tolist(),
                    "took_ms": round((time.perf_counter() - t0) * 1000, 1)})


class DaemonTCPServer(socketserver.ThreadingTCPServer):
    # SO_REUSEADDR intentionally OFF. On Windows it means "last bind wins": it
    # would let any number of daemons bind the same port, so a flaky singleton
    # probe (e.g. a listen socket gone defunct after sleep/resume) silently
    # piled up dozens of orphaned daemons. With reuse off, a second instance
    # FAILS to bind -- main() then either reuses the live one or kills the
    # defunct lock owner and rebinds. Exactly one daemon at a time.
    allow_reuse_address = False
    daemon_threads = True


def write_lock(port: int):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(json.dumps({
        "pid": os.getpid(), "port": port,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }), encoding="utf-8")


def remove_lock():
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


def existing_daemon_port():
    """Return the live daemon's port if reachable, else None."""
    if not LOCK_PATH.exists():
        return None
    try:
        info = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    port = info.get("port")
    if not isinstance(port, int):
        return None
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect((BIND_HOST, port))
        s.sendall(b'{"op":"ping"}\n')
        s.recv(64)  # any answer means alive
        return port
    except OSError:
        return None
    finally:
        s.close()


def idle_watchdog():
    while True:
        time.sleep(60)
        if IDLE_SHUTDOWN_S > 0 and (time.time() - STATE.last_request) > IDLE_SHUTDOWN_S:
            log(f"idle > {IDLE_SHUTDOWN_S}s -> exiting")
            os._exit(0)


def kill_stale_lock_owner():
    """Terminate the pid recorded in the lock. Called only when the port is held
    but nobody answers a ping -- e.g. a daemon whose listen socket went defunct
    after sleep/resume. Frees the port so we can rebind. No-op if no/own pid."""
    try:
        pid = json.loads(LOCK_PATH.read_text(encoding="utf-8")).get("pid")
    except Exception:
        return
    if isinstance(pid, int) and pid > 0 and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGTERM)  # Windows: maps to TerminateProcess
            log(f"killed stale daemon pid={pid} holding the port")
        except OSError:
            pass


def bind_server(port: int):
    """Bind exclusively (no SO_REUSEADDR). If the port is held by a daemon that
    no longer answers, kill the lock owner once and retry. Raises on failure."""
    try:
        return DaemonTCPServer((BIND_HOST, port), Handler)
    except OSError as e:
        log(f"bind {BIND_HOST}:{port} failed ({e}); killing stale owner, retrying")
        kill_stale_lock_owner()
        time.sleep(0.5)
        return DaemonTCPServer((BIND_HOST, port), Handler)


def main():
    global STATE
    # Refuse to start if a daemon is already running on the recorded port.
    existing = existing_daemon_port()
    if existing:
        print(f"daemon already alive on {BIND_HOST}:{existing}", file=sys.stderr)
        return 1

    kbe = _load_kb_embed()
    STATE = DaemonState(kbe)
    log(f"starting (pid={os.getpid()}, chunks={len(STATE.store.meta)})")

    # Bind + write the lock BEFORE loading the model so clients (statusline TCP
    # probe, retrieve hook, spawn-hook reuse check) see a healthy daemon within
    # milliseconds instead of during the ~2-4s model load. Previously the model
    # loaded first, so a freshly spawned daemon was unreachable for seconds and
    # the statusline flashed the BM25-fallback warning until the next refresh.
    port = DEFAULT_PORT
    try:
        server = bind_server(port)
    except OSError as e:
        print(f"cannot bind {BIND_HOST}:{port}: {e}", file=sys.stderr)
        log(f"giving up: cannot bind {BIND_HOST}:{port}: {e}")
        return 1
    write_lock(port)
    log(f"listening on {BIND_HOST}:{port}")
    print(f"kb-embed-daemon listening on {BIND_HOST}:{port}", flush=True)

    # KB_EMBED_DAEMON_PRELOAD=1: load the model now, but off the accept path so
    # the port answers immediately. `ping` needs no model; the first search
    # waits on ensure_model() under STATE.lock (serialized with this preload).
    if os.environ.get("KB_EMBED_DAEMON_PRELOAD") == "1":
        def _preload():
            with STATE.lock:
                STATE.ensure_model()
        threading.Thread(target=_preload, daemon=True).start()

    threading.Thread(target=idle_watchdog, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("KeyboardInterrupt")
    finally:
        remove_lock()
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
