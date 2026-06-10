#!/usr/bin/env python3
"""manager/server.py — KB manager: a localhost config UI.

The human face of KB. A dependency-free (stdlib http.server) localhost web app to
configure the engine without hand-editing JSON: set the vault + workspaces, the
daily sync time, and toggle the Claude Code integration. It sits ON TOP of the
engine + installer and reuses their validated, tested logic — it never
re-implements config writes (kb_config owns that) and never deploys files (the
installer/bootstrap owns that). Its job is config + control, nothing else.

Run:
    python manager/server.py             # opens http://127.0.0.1:7666/?t=<token>
    python manager/server.py --no-open   # don't auto-open a browser

Security (internal localhost tool, not a public service):
  * binds 127.0.0.1 only;
  * rejects requests whose Host header isn't 127.0.0.1/localhost (anti DNS-rebind);
  * a per-launch token gates every /api/* call (the page reads it from its own
    URL); other local pages don't know it, so they cannot drive this server.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "installer"))

import kb_config                                   # engine: config read/write
import kb_vault                                    # engine: read-only vault reader
import install                                     # installer: status orchestration
import scheduler                                   # installer: per-OS schedule
from settings_merge import merge_settings, SettingsMergeError  # installer: hook wiring

STATIC = HERE / "static"
# Per-launch token. KB_MANAGER_TOKEN lets a launcher (or a test) pin it; otherwise
# it's random and printed in the URL.
TOKEN = os.environ.get("KB_MANAGER_TOKEN") or secrets.token_urlsafe(16)
HOST, DEFAULT_PORT = "127.0.0.1", 7666
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CTYPES = {".html": "text/html; charset=utf-8", ".css": "text/css",
          ".js": "application/javascript", ".svg": "image/svg+xml"}


# --- Filesystem browse (folder picker) ----------------------------------------
# The manager runs ON the user's machine, so it can offer a real folder picker
# where a plain browser can't (file inputs never expose absolute paths).
# Directories only — names, never file contents — plus an explicit mkdir for
# "create the vault right here". Token-gated like every other API route.

def fs_list(path_str) -> dict:
    """List the sub-DIRECTORIES of a path (default: home). Per-child permission
    errors skip the child; an unreadable path itself reports an error."""
    try:
        p = Path(path_str).expanduser() if path_str else Path.home()
        p = p.resolve()
    except (OSError, ValueError) as e:
        return {"error": f"bad path: {e}"}
    if not p.is_dir():
        return {"error": f"not a directory: {p}"}
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            try:
                if child.is_dir():
                    dirs.append(child.name)
            except OSError:
                continue
    except OSError as e:
        return {"error": f"cannot list {p}: {e}"}
    if os.name == "nt":
        try:
            roots = os.listdrives()  # Python 3.12+
        except AttributeError:  # pragma: no cover - older Python
            roots = [f"{Path.home().drive}\\"]
    else:
        roots = ["/"]
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "dirs": dirs,
            "home": str(Path.home()), "sep": os.sep, "roots": roots}


def fs_mkdir(parent: str, name: str) -> dict:
    """Create ONE new directory inside an existing one. The name must be a single
    path segment — separators / traversal are refused, never normalized away."""
    name = (name or "").strip()
    if not name or name in (".", "..") or any(c in name for c in '/\\<>:"|?*'):
        return {"error": "invalid folder name"}
    try:
        base = Path(parent).expanduser().resolve()
    except (OSError, ValueError) as e:
        return {"error": f"bad parent: {e}"}
    if not base.is_dir():
        return {"error": f"parent is not a directory: {base}"}
    target = base / name
    if target.exists():
        return {"error": f"already exists: {target}"}
    try:
        target.mkdir()
    except OSError as e:
        return {"error": f"mkdir failed: {e}"}
    return {"created": str(target)}


def daemon_ping() -> dict:
    """Probe the embedding daemon with the same {"op":"ping"} the statusline uses."""
    lock = install.kb_dir() / "state" / "kb-embed-daemon.lock"
    if not lock.exists():
        return {"up": False, "reason": "no daemon lock"}
    try:
        port = int(json.loads(lock.read_text(encoding="utf-8")).get("port", 0))
    except Exception:
        return {"up": False, "reason": "unreadable lock"}
    if port <= 0:
        return {"up": False, "reason": "no port"}
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3) as s:
            s.sendall(b'{"op":"ping"}\n')
            s.settimeout(0.4)
            data = s.recv(1024)
        pong = json.loads(data.decode("utf-8"))
        return {"up": True, "model_loaded": bool(pong.get("model_loaded", True)), "port": port}
    except Exception as e:
        return {"up": False, "reason": f"no answer ({type(e).__name__})", "port": port}


class Handler(BaseHTTPRequestHandler):
    # --- guards -------------------------------------------------------------
    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def _token_ok(self) -> bool:
        t = self.headers.get("X-KB-Token") or parse_qs(urlparse(self.path).query).get("t", [None])[0]
        return bool(t) and secrets.compare_digest(t, TOKEN)

    # --- io helpers ---------------------------------------------------------
    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return None

    def log_message(self, *a):  # keep the console clean
        return

    def _serve_static(self, rel: str) -> None:
        target = (STATIC / rel).resolve()
        if target != STATIC and STATIC not in target.parents:
            return self._send(403, b"forbidden", "text/plain")
        if not target.is_file():
            return self._send(404, b"not found", "text/plain")
        self._send(200, target.read_bytes(), CTYPES.get(target.suffix, "application/octet-stream"))

    # --- status -------------------------------------------------------------
    def _status(self) -> dict:
        st = install.status()
        st["daemon"] = daemon_ping()
        return st

    # --- knowledge (read-only vault) ----------------------------------------
    def _vault(self):
        """Resolve the configured vault, or None if config is missing/unresolvable."""
        try:
            return Path(kb_config.resolve_vault(strict=True))
        except Exception:
            try:
                v = kb_config.load_config().get("vault")
                return Path(v) if v else None
            except Exception:
                return None

    def _sync_history(self, limit: int = 50) -> list:
        f = install.kb_dir() / "state" / "kb-sync-history.json"
        if not f.exists():
            return []
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return data[-limit:][::-1] if isinstance(data, list) else []
        except Exception:
            return []

    def _integration(self, enable: bool) -> dict:
        cdir = install.claude_dir()
        kdir = install.kb_dir()
        killfile = kdir / "hooks-disabled"
        legacy_kill = cdir / "kb-hooks-disabled"
        if enable:
            # Enable = remove the kill-switch + ensure hooks are wired (idempotent).
            for kf in (killfile, legacy_kill):
                if kf.exists():
                    kf.unlink()
            try:
                merged = merge_settings(cdir / "settings.json", kdir, dry_run=False)
            except SettingsMergeError as e:
                return {"enabled": True, "settings_error": str(e)}
            return {"enabled": True, "added": merged.get("added"), "skipped": merged.get("skipped")}
        # Disable = the kill-switch file (instant; never strip settings.json — that
        # merge is add-only). Both kb-context.sh and the statusline honor this file.
        killfile.write_text("disabled by KB manager\n", encoding="utf-8")
        return {"enabled": False, "kill_switch": True}

    # --- routes -------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        # API (token-gated)
        if not self._token_ok():
            return self._send(403, {"error": "bad token"})
        if path == "/api/status":
            return self._send(200, self._status())
        if path == "/api/fs":
            q = parse_qs(urlparse(self.path).query)
            out = fs_list((q.get("path") or [None])[0])
            return self._send(400 if "error" in out else 200, out)
        if path == "/api/config":
            return self._send(200, {"config": kb_config.load_config(), "path": str(kb_config.workspaces_path())})
        if path == "/api/sync-history":
            return self._send(200, {"runs": self._sync_history()})
        if path == "/api/update-check":
            # Read-only remote probe (git fetch). Split off /api/status so a slow or
            # offline network never blocks the page load; install.update_check is
            # fail-soft and always returns a reason instead of raising.
            return self._send(200, install.update_check())
        if path == "/api/vault-remote":
            # Read-only: does the vault have a git remote, and which. The connect
            # action is POST-only (it writes git config + pushes).
            return self._send(200, install.vault_remote_status())
        if path.startswith("/api/knowledge/"):
            return self._knowledge(path[len("/api/knowledge/"):])
        return self._send(404, {"error": "not found"})

    def _knowledge(self, sub: str):
        vault = self._vault()
        if vault is None or not vault.exists():
            return self._send(400, {"error": "vault not configured or missing"})
        qs = parse_qs(urlparse(self.path).query)
        one = lambda k: (qs.get(k) or [None])[0]  # noqa: E731
        try:
            if sub == "overview":
                return self._send(200, kb_vault.overview(vault))
            if sub == "learnings":
                return self._send(200, {"learnings": kb_vault.list_learnings(
                    vault, project=one("project"), scope=one("scope"),
                    tag=one("tag"), q=one("q"))})
            if sub == "item":
                rel = one("path")
                if not rel:
                    return self._send(400, {"error": "missing path"})
                return self._send(200, kb_vault.read_item(vault, rel))
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        return self._send(404, {"error": "not found"})

    def do_PUT(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if not self._token_ok():
            return self._send(403, {"error": "bad token"})
        if urlparse(self.path).path != "/api/config":
            return self._send(404, {"error": "not found"})
        body = self._read_json()
        if body is None:
            return self._send(400, {"error": "bad json"})
        updates = body.get("updates", {})
        errors = kb_config.validate_config_update(updates)
        if errors:
            return self._send(400, {"error": "invalid", "errors": errors})
        try:
            return self._send(200, {"config": kb_config.write_config(updates)})
        except kb_config.KBConfigError as e:
            return self._send(400, {"error": str(e)})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if not self._token_ok():
            return self._send(403, {"error": "bad token"})
        path = urlparse(self.path).path
        body = self._read_json()
        if body is None:
            return self._send(400, {"error": "bad json"})
        if path == "/api/fs/mkdir":
            out = fs_mkdir(str(body.get("parent") or ""), str(body.get("name") or ""))
            return self._send(400 if "error" in out else 200, out)
        if path == "/api/schedule":
            t = str(body.get("time", scheduler.DEFAULT_TIME))
            if not _HHMM.match(t):
                return self._send(400, {"error": "time must be HH:MM (24h)"})
            return self._send(200, scheduler.register(install.kb_dir(), time_hhmm=t, dry_run=False))
        if path == "/api/integration":
            return self._send(200, self._integration(bool(body.get("enable", True))))
        if path == "/api/update":
            # Fast-forward the source tree to the remote + re-deploy (delegates to the
            # installer; the manager never deploys files itself). Refuses a dirty or
            # diverged tree and is reversible via the deploy backup.
            return self._send(200, install.update_apply())
        if path == "/api/vault-remote/connect":
            # One-time: connect the vault to a user-owned remote + push. Delegates to
            # the installer (adds 'origin', never force-pushes, never clobbers an
            # existing remote). The nightly sync still never auto-pushes.
            url = str((body or {}).get("url", "")).strip()
            return self._send(200, install.vault_connect_remote(url))
        if path == "/api/vault-remote/pull":
            # Read side of a shared vault: fetch + merge the team's latest. Delegates
            # to the installer (refuses a dirty tree, aborts on conflict so no marker
            # lands in a learning, never force, never pushes).
            return self._send(200, install.vault_pull_remote())
        return self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description="KB manager — localhost config UI.")
    ap.add_argument("--no-open", action="store_true", help="don't auto-open a browser")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    server = ThreadingHTTPServer((HOST, args.port), Handler)
    url = f"http://{HOST}:{args.port}/?t={TOKEN}"
    print(f"KB manager running: {url}")
    print("Ctrl+C to stop.")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
