#!/usr/bin/env python
"""kb-bodyread-track — PostToolUse hook.

Counts tokens of vault body-reads so /kb-stats can show how much of the KB is
actually *consumed* (not just injected). Two signals:

  - cited body-reads : a Read / obsidian-MCP read of a learning whose basename
    was cited in a <vault-context> injection this session. This is the real
    "KB was used" metric.
  - vault reads total: any read of a file under the vault (includes
    maintenance, e.g. editing _index.md). Superset of cited.

Read-only by design: the injected footer directs body reads through the
built-in Read tool (absolute vault path), which empirically does ~16x more
vault reads than the obsidian MCP (146 vs 9 across history). Read is also
grep-able and behaves consistently across sessions. The MCP read path is not
tracked — the footer steers everything to Read.

Writes its own sidecar (kb-bodyread-{session}.json); reads cited_keys from the
token sidecar (kb-tokens-{session}.json) written by kb_retrieve.py. No write
contention between the two hooks.

PostToolUse contract: react to the result; emit nothing (exit 0) for normal flow.
"""
from __future__ import annotations

import json
import os
import re
import sys


_TIKTOKEN_ENC = None
_TIKTOKEN_CAP = 400_000  # bytes; above this, skip tiktoken (use fast fallback)


def _count_tokens(text: str):
    """(count, exact). tiktoken cl100k_base when available and input not huge,
    else len(utf8)//4. Mirrors kb_retrieve._count_tokens."""
    global _TIKTOKEN_ENC
    if not isinstance(text, str) or not text:
        return (0, False)
    nbytes = len(text.encode("utf-8"))
    if nbytes <= _TIKTOKEN_CAP:
        if _TIKTOKEN_ENC is None:
            try:
                import tiktoken  # type: ignore
                _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
            except Exception:
                _TIKTOKEN_ENC = False
        if _TIKTOKEN_ENC and _TIKTOKEN_ENC is not False:
            try:
                return (len(_TIKTOKEN_ENC.encode(text)), True)
            except Exception:
                pass
    return (max(1, nbytes // 4), False)


def _vault_root():
    try:
        cfg = os.path.join(os.path.expanduser("~"), ".claude", "kb-workspaces.json")
        with open(cfg, "r", encoding="utf-8") as f:
            return json.load(f).get("vault")
    except Exception:
        return os.environ.get("KB_VAULT")


def _state_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), ".claude", "state")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _sanitize(session_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\-_]", "", session_id or "")


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _read_path(tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("filePath") or ""


def _basename_key(path: str) -> str:
    base = path.replace("\\", "/").rstrip("/").split("/")[-1].strip()
    if base.lower().endswith(".md"):
        base = base[:-3]
    return base.strip().lower()


def _is_vault_read(path: str, vault) -> bool:
    if not path:
        return False
    p = path.replace("\\", "/").lower()
    if "/obsidian vault/" in p or p.endswith("/obsidian vault"):
        return True
    if vault:
        vp = str(vault).replace("\\", "/").rstrip("/").lower()
        if vp and (p == vp or p.startswith(vp + "/")):
            return True
    return False


def _response_text(resp) -> str:
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, list):
        parts = []
        for b in resp:
            if isinstance(b, dict):
                t = b.get("text") or b.get("content")
                parts.append(t if isinstance(t, str) else "")
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(p for p in parts if p)
    if isinstance(resp, dict):
        for k in ("content", "text", "stdout", "output", "file"):
            v = resp.get(k)
            if isinstance(v, str):
                return v
            if isinstance(v, (list, dict)):
                t = _response_text(v)
                if t:
                    return t
        return json.dumps(resp, ensure_ascii=False)
    return str(resp)


def main() -> int:
    if os.environ.get("KB_HOOKS_DISABLED") == "1":
        return 0
    if os.path.isfile(os.path.expanduser("~/.claude/kb-hooks-disabled")):
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    resp = payload.get("tool_response")
    if resp is None:
        resp = payload.get("tool_result")
    session_id = payload.get("session_id") or ""

    if tool_name != "Read":
        return 0
    if not session_id:
        return 0

    vault = _vault_root()
    path = _read_path(tool_input)
    if not _is_vault_read(path, vault):
        return 0

    import time
    key = _basename_key(path) if path else ""
    text = _response_text(resp)
    tokens, exact = _count_tokens(text)

    sd = _state_dir()
    safe = _sanitize(session_id)
    if not safe:
        return 0
    tok_sidecar = _load_json(os.path.join(sd, f"kb-tokens-{safe}.json"))
    cited_keys = set(tok_sidecar.get("cited_keys", []) or [])
    is_cited = bool(key) and key in cited_keys

    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    p = os.path.join(sd, f"kb-bodyread-{safe}.json")
    state = _load_json(p)
    if not isinstance(state, dict) or not state:
        state = {"session_id": session_id, "vault_reads": 0, "vault_read_tokens": 0,
                 "cited_reads": 0, "cited_read_tokens": 0, "by_tool": {},
                 "exact_tokens": exact, "first_at": now}
    state["session_id"] = session_id
    state["vault_reads"] = int(state.get("vault_reads", 0)) + 1
    state["vault_read_tokens"] = int(state.get("vault_read_tokens", 0)) + tokens
    if is_cited:
        state["cited_reads"] = int(state.get("cited_reads", 0)) + 1
        state["cited_read_tokens"] = int(state.get("cited_read_tokens", 0)) + tokens
    bt = state.setdefault("by_tool", {})
    bt[tool_name] = int(bt.get(tool_name, 0)) + 1
    state["exact_tokens"] = exact
    state["last_at"] = now
    state["last"] = {"tool": tool_name, "key": key, "tokens": tokens,
                     "cited": is_cited, "at": now}
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
