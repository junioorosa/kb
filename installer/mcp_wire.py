#!/usr/bin/env python3
"""Wire the KB MCP server into the hosts installed on this machine.

Detect-and-wire, with the same care contract as settings_merge: only hosts
that actually exist get touched, the write is additive (an existing `kb`
entry — even a divergent one — is never overwritten), every modified file is
backed up first, an unparseable config is refused untouched, and re-running
is a no-op. The recorded command uses the absolute Python executable: GUI
hosts (Claude Desktop, Cursor) spawn MCP servers without the user's shell
PATH, so a bare "python" breaks exactly where it's hardest to debug.

Hosts and their config surfaces:
  codex           ~/.codex/config.toml             [mcp_servers.kb]   (TOML)
  cursor          ~/.cursor/mcp.json               mcpServers.kb      (JSON)
  claude-desktop  <per-OS>/Claude/claude_desktop_config.json          (JSON)
  gemini          ~/.gemini/settings.json          mcpServers.kb      (JSON)
  windsurf        ~/.codeium/windsurf/mcp_config.json                 (JSON)

Codex additionally gets a marked block appended to ~/.codex/AGENTS.md telling
the model WHEN to reach for the tools — pull hosts under-call tools they are
never reminded of.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - engine targets 3.x, degrade to refusal
    tomllib = None

AGENTS_BEGIN = "<!-- kb:mcp:begin -->"
AGENTS_END = "<!-- kb:mcp:end -->"
AGENTS_BLOCK = f"""{AGENTS_BEGIN}
## Knowledge base (KB)

Before proposing a technical solution, call the `kb_search` MCP tool with the
task; open promising notes with `kb_read`, or call `kb_context` once at the
start of a task. When asked to track this session's work under a branch
(e.g. "kb mark"), call `kb_mark` with that branch.
{AGENTS_END}
"""


def _posix(p: Path | str) -> str:
    """Forward slashes everywhere: valid on Windows, and avoids both JSON
    double-escaping noise and TOML basic-string backslash escapes."""
    return str(p).replace("\\", "/")


def server_command(claude_dir: Path) -> dict:
    return {
        "command": _posix(sys.executable),
        "args": [_posix(Path(claude_dir) / "hooks" / "kb.py"), "mcp"],
    }


def host_specs(home: Path | None = None, appdata: Path | None = None) -> list[dict]:
    """The known hosts. `home`/`appdata` are injectable for tests."""
    home = Path(home) if home else Path.home()
    if appdata is None:
        env = os.environ.get("APPDATA", "")
        appdata = Path(env) if env else home / "AppData" / "Roaming"
    if sys.platform == "darwin":
        desktop_cfg = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif os.name == "nt":
        desktop_cfg = Path(appdata) / "Claude" / "claude_desktop_config.json"
    else:
        desktop_cfg = home / ".config" / "Claude" / "claude_desktop_config.json"
    return [
        {"name": "codex", "kind": "toml", "detect": home / ".codex",
         "config": home / ".codex" / "config.toml",
         "agents_md": home / ".codex" / "AGENTS.md"},
        {"name": "cursor", "kind": "json", "detect": home / ".cursor",
         "config": home / ".cursor" / "mcp.json", "key": "mcpServers"},
        {"name": "claude-desktop", "kind": "json", "detect": desktop_cfg.parent,
         "config": desktop_cfg, "key": "mcpServers"},
        {"name": "gemini", "kind": "json", "detect": home / ".gemini",
         "config": home / ".gemini" / "settings.json", "key": "mcpServers"},
        {"name": "windsurf", "kind": "json", "detect": home / ".codeium" / "windsurf",
         "config": home / ".codeium" / "windsurf" / "mcp_config.json", "key": "mcpServers"},
    ]


def _backup(path: Path) -> str | None:
    if not path.exists():
        return None
    ts = time.strftime("%Y%m%dT%H%M%S")
    bak = path.with_name(path.name + f".kb-bak-{ts}")
    bak.write_bytes(path.read_bytes())
    return str(bak)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".kb-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def wire_json(cfg_path: Path, key: str, server: dict, dry_run: bool) -> dict:
    """Additively add <key>.kb to a JSON config. Never mutates an existing entry."""
    rep = {"status": "wired", "config": str(cfg_path), "backup": None}
    data: dict = {}
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            rep["status"] = "refused-malformed"
            rep["error"] = f"not valid JSON, left untouched: {e}"
            return rep
        if not isinstance(data, dict):
            rep["status"] = "refused-malformed"
            rep["error"] = "top level is not an object, left untouched"
            return rep
    servers = data.get(key)
    if isinstance(servers, dict) and "kb" in servers:
        rep["status"] = "already"
        return rep
    if dry_run:
        rep["status"] = "would-wire"
        return rep
    if not isinstance(servers, dict):
        servers = {}
        data[key] = servers
    servers["kb"] = server
    rep["backup"] = _backup(cfg_path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(cfg_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return rep


def wire_toml(cfg_path: Path, server: dict, dry_run: bool) -> dict:
    """Append [mcp_servers.kb] to a TOML config when absent.

    A pure append of a new top-level table is the one TOML edit that cannot
    corrupt unknown existing content — and the result is re-parsed afterwards;
    if that ever fails, the backup is restored and the failure reported.
    """
    rep = {"status": "wired", "config": str(cfg_path), "backup": None}
    if tomllib is None:
        rep["status"] = "error"
        rep["error"] = "tomllib unavailable on this Python"
        return rep
    text = ""
    if cfg_path.exists():
        text = cfg_path.read_text(encoding="utf-8")
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            rep["status"] = "refused-malformed"
            rep["error"] = f"not valid TOML, left untouched: {e}"
            return rep
        if "kb" in (data.get("mcp_servers") or {}):
            rep["status"] = "already"
            return rep
    if dry_run:
        rep["status"] = "would-wire"
        return rep
    args = ", ".join(json.dumps(a) for a in server["args"])
    block = (
        "\n# Added by the KB installer — the KB engine served over MCP.\n"
        "[mcp_servers.kb]\n"
        f"command = {json.dumps(server['command'])}\n"
        f"args = [{args}]\n"
    )
    new_text = (text.rstrip("\n") + "\n" + block) if text.strip() else block.lstrip("\n")
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:  # pragma: no cover - belt and braces
        rep["status"] = "error"
        rep["error"] = f"appended TOML would not parse, aborted: {e}"
        return rep
    rep["backup"] = _backup(cfg_path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(cfg_path, new_text)
    return rep


def wire_agents_md(path: Path, dry_run: bool) -> dict:
    """Append the marked KB block to a global AGENTS.md (codex). Skip when marked."""
    rep = {"status": "wired", "config": str(path), "backup": None}
    text = ""
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if AGENTS_BEGIN in text:
            rep["status"] = "already"
            return rep
    if dry_run:
        rep["status"] = "would-wire"
        return rep
    rep["backup"] = _backup(path)
    new_text = (text.rstrip("\n") + "\n\n" + AGENTS_BLOCK) if text.strip() else AGENTS_BLOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, new_text)
    return rep


def wire_all(claude_dir: Path, home: Path | None = None, appdata: Path | None = None,
             dry_run: bool = False) -> dict:
    """Detect installed hosts and wire each. Absent hosts are never created."""
    server = server_command(Path(claude_dir))
    report = {"server": server, "hosts": {}}
    for spec in host_specs(home, appdata):
        if not spec["detect"].is_dir():
            report["hosts"][spec["name"]] = {"status": "not-detected"}
            continue
        try:
            if spec["kind"] == "toml":
                rep = wire_toml(spec["config"], server, dry_run)
            else:
                rep = wire_json(spec["config"], spec["key"], server, dry_run)
            if spec.get("agents_md") and rep["status"] in ("wired", "already", "would-wire"):
                rep["agents_md"] = wire_agents_md(spec["agents_md"], dry_run)
        except Exception as e:
            rep = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        report["hosts"][spec["name"]] = rep
    counts = {}
    for r in report["hosts"].values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    report["counts"] = counts
    return report
