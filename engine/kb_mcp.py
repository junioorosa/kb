#!/usr/bin/env python
"""kb_mcp.py — MCP stdio server over the KB engine (the universal pull adapter).

Hosts with prompt hooks get KB context PUSHED (the hook adapter). Every other
agentic host — Codex CLI, Cursor, Claude Desktop, Windsurf, Zed, anything that
speaks MCP — gets the PULL door: the model calls tools against the same local
engine. No LLM, no network beyond the loopback embedding daemon, no new deps.

Protocol: Model Context Protocol over stdio — newline-delimited JSON-RPC 2.0.
Implemented: initialize, notifications/* (ignored), ping, tools/list,
tools/call. Anything else gets -32601. One message per line, responses flushed
immediately; EOF on stdin ends the server.

Tools:
  kb_search(query, k)  — ranked vault notes (hybrid cosine+BM25, BM25 fallback)
  kb_context(prompt)   — the same <vault-context> block the push hook injects
  kb_read(path)        — full body of one note, sandboxed to the vault

Run:  kb mcp   (or: python kb_mcp.py)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import kb_retrieve as kbr

PROTOCOL_VERSION = "2024-11-05"
MAX_READ_BYTES = 64_000


def _server_version() -> str:
    """Installed KB version (stamped by the installer), or 'dev' from the repo."""
    try:
        stamp = kbr.HOME / ".claude" / ".kb-version"
        if stamp.exists():
            return stamp.read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        pass
    return "dev"


# ====== TOOLS ======

TOOLS = [
    {
        "name": "kb_search",
        "description": (
            "Search the user's private engineering knowledge base — distilled "
            "learnings from their past branches: bug root causes, integration "
            "patterns, project conventions, decisions. Call this BEFORE "
            "proposing a technical solution whenever the task may relate to "
            "prior work in this codebase or team. Returns ranked note paths "
            "with one-line descriptions; open a result with kb_read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you are trying to solve, in natural language (any language)."},
                "k": {"type": "integer", "description": "Max results (default 8)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kb_context",
        "description": (
            "Get the ready-to-use context block for a task — the same "
            "<vault-context> the KB injects automatically on hosts with prompt "
            "hooks. Call it once at the start of a task with the user's request "
            "to ground yourself in relevant past learnings before answering. "
            "Pass the git branch you are working on to also pull that ticket's "
            "own notes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The user's request or a summary of the task."},
                "branch": {"type": "string", "description": "Optional: current git branch — adds the matching ticket's notes."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "kb_read",
        "description": (
            "Read the full body of one knowledge-base note by its vault-relative "
            "path, exactly as returned by kb_search or kb_context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative note path, e.g. ws/project/Learnings/note.md"},
            },
            "required": ["path"],
        },
    },
]


def _candidates_for(text: str):
    """Shared retrieval front-half: manifest -> BM25 -> hybrid. Mirrors the hook."""
    manifest = kbr.load_manifest()
    entries = manifest.get("entries", [])
    if not entries:
        return [], entries
    q_tokens = kbr.tokenize(text)
    if not q_tokens:
        return [], entries
    scored = kbr.bm25_score(q_tokens, entries)
    bm25_top = [(s, i) for s, i in scored[:kbr.TOP_BM25] if s > 0]
    candidates = bm25_top
    if kbr.EMBED_RETRIEVAL:
        hybrid = kbr.hybrid_candidates(text, q_tokens, entries, bm25_top, kbr.EMBED_TOP_N)
        if hybrid:
            candidates = hybrid
    return candidates, entries


def tool_kb_search(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    k = args.get("k") or 8
    k = max(1, min(int(k), 25))
    if kbr.VAULT is None:
        return ("KB vault is not configured on this machine (no KB_VAULT and no "
                "'vault' key in the config). Run the KB manager to set it.")
    candidates, entries = _candidates_for(query)
    if not candidates:
        return "No matching notes in the knowledge base for this query."
    lines = [f"Knowledge-base matches for: {query}"]
    for score, i in candidates[:k]:
        e = entries[i]
        desc = e.get("desc", "")[:160]
        lines.append(f"- {e['path']} (score={score:.2f}) — {desc}")
    lines.append("")
    lines.append("Open any note with the kb_read tool to get its full body.")
    return "\n".join(lines)


def tool_kb_context(args: dict) -> str:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    if kbr.VAULT is None:
        return ("KB vault is not configured on this machine (no KB_VAULT and no "
                "'vault' key in the config). Run the KB manager to set it.")
    branch = (args.get("branch") or "").strip()
    ticket_match = kbr.find_ticket_folder(branch) if branch else None
    candidates, entries = _candidates_for(prompt)
    if not candidates and not ticket_match:
        return "No relevant knowledge-base context for this prompt."
    return kbr.emit_output(branch, ticket_match, candidates, entries)


def tool_kb_read(args: dict) -> str:
    rel = (args.get("path") or "").strip().replace("\\", "/")
    if not rel:
        raise ValueError("path is required")
    if kbr.VAULT is None:
        return "KB vault is not configured on this machine."
    vault_root = Path(kbr.VAULT).resolve()
    target = (vault_root / rel).resolve()
    # Sandbox: never serve anything outside the vault, whatever the input.
    if vault_root != target and vault_root not in target.parents:
        raise ValueError("path escapes the vault")
    if target.suffix.lower() != ".md" or not target.is_file():
        raise ValueError(f"not a vault note: {rel}")
    text = target.read_text(encoding="utf-8", errors="ignore")
    raw = text.encode("utf-8")
    if len(raw) > MAX_READ_BYTES:
        text = raw[:MAX_READ_BYTES].decode("utf-8", errors="ignore") + "\n... [truncated]"
    return text


TOOL_IMPL = {
    "kb_search": tool_kb_search,
    "kb_context": tool_kb_context,
    "kb_read": tool_kb_read,
}


# ====== JSON-RPC / MCP PLUMBING ======


def _result(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(msg: dict):
    """One JSON-RPC message in -> response dict out, or None for notifications."""
    method = msg.get("method", "")
    req_id = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        params = msg.get("params") or {}
        client_proto = params.get("protocolVersion") or PROTOCOL_VERSION
        return _result(req_id, {
            "protocolVersion": client_proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "kb", "version": _server_version()},
        })
    if method.startswith("notifications/"):
        return None
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        impl = TOOL_IMPL.get(name)
        if impl is None:
            return _error(req_id, -32602, f"unknown tool: {name}")
        try:
            text = impl(params.get("arguments") or {})
            return _result(req_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:
            # Tool-level failure travels as tool output (isError), not protocol error.
            return _result(req_id, {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                                    "isError": True})
    if is_notification:
        return None
    return _error(req_id, -32601, f"method not found: {method}")


def serve() -> int:
    # The host owns this process's stdio; everything must be UTF-8 regardless
    # of the OS console codepage (Windows defaults to cp1252 otherwise).
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            sys.stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            sys.stdout.flush()
            continue
        try:
            resp = handle(msg)
        except Exception as exc:
            resp = _error(msg.get("id"), -32603, f"internal error: {exc}")
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(serve())
