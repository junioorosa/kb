#!/usr/bin/env python
"""kb_mcp_test.py — protocol tests for the MCP stdio server (kb_mcp.py).

Spawns the real server (`kb.py mcp`) as a subprocess with an isolated HOME and
a fixture vault, speaks newline-delimited JSON-RPC to it, and asserts on the
responses. KB_FAST_MODE=1 keeps retrieval on the deterministic BM25-only path
(no embedding daemon needed). No network, no live vault.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
KB_CLI = ENGINE / "kb.py"

PASSED = 0
FAILED = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}  {extra}")


def make_vault(root: Path) -> Path:
    vault = root / "vault"
    learn = vault / "ws" / "proj" / "Learnings"
    learn.mkdir(parents=True)
    (learn / "jwt-clock-skew.md").write_text(
        "---\n"
        "description: Clock-skew tolerance for JWT expiry checks\n"
        "tags: [jwt, auth, clock-skew]\n"
        "scope: project\n"
        "---\n"
        "# JWT clock skew\n"
        "Compare now <= exp + SKEW so tokens are not read as expired early. MARKER-JWT-BODY\n"
        "See [[rate-limit-retry]].\n",
        encoding="utf-8",
    )
    (learn / "rate-limit-retry.md").write_text(
        "---\n"
        "description: Retry with backoff for rate-limited carrier API calls\n"
        "tags: [retry, rate-limit]\n"
        "scope: project\n"
        "---\n"
        "# Rate-limit retry\n"
        "Generate the request once, retry with backoff. MARKER-RETRY-BODY\n",
        encoding="utf-8",
    )
    ws_learn = vault / "ws" / "Learnings"
    ws_learn.mkdir(parents=True)
    (ws_learn / "short-methods.md").write_text(
        "---\n"
        "description: Long methods split into private sub-methods\n"
        "tags: [lint]\n"
        "scope: workspace\n"
        "---\n"
        "Short-method rule.\n",
        encoding="utf-8",
    )
    ticket = vault / "ws" / "proj" / "fix" / "login"
    ticket.mkdir(parents=True)
    (ticket / "_index.md").write_text(
        "---\n"
        "title: Fix login timeout\n"
        "status: in-progress\n"
        "branch: fix/login\n"
        "---\n"
        "Login ticket notes. MARKER-TICKET-INDEX\n",
        encoding="utf-8",
    )
    return vault


def run_server(home: Path, vault: Path, requests: list) -> list:
    """Send all requests, close stdin, return the list of parsed responses."""
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["KB_VAULT"] = str(vault)
    env["KB_FAST_MODE"] = "1"
    env.pop("KB_HOOKS_DISABLED", None)
    payload = "".join(json.dumps(r) + "\n" for r in requests)
    proc = subprocess.run(
        [sys.executable, str(KB_CLI), "mcp"],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        encoding="utf-8",
    )
    responses = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line:
            responses.append(json.loads(line))
    return responses


def by_id(responses: list, req_id) -> dict:
    return next((r for r in responses if r.get("id") == req_id), {})


def tool_text(resp: dict) -> str:
    content = (resp.get("result") or {}).get("content") or []
    return "".join(c.get("text", "") for c in content if c.get("type") == "text")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / "home"
        home.mkdir()
        vault = make_vault(Path(d))

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "kb_search", "arguments": {"query": "jwt clock skew expiry"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "kb_context", "arguments": {"prompt": "jwt clock skew expiry"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "kb_read", "arguments": {"path": "ws/proj/Learnings/jwt-clock-skew.md"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "kb_read", "arguments": {"path": "../../outside.md"}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": "kb_read", "arguments": {"path": "ws/proj/Learnings/missing.md"}}},
            {"jsonrpc": "2.0", "id": 8, "method": "bogus/method"},
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": "nope", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 10, "method": "ping"},
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
             "params": {"name": "kb_context",
                        "arguments": {"prompt": "jwt clock skew expiry", "branch": "fix/login"}}},
            {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
             "params": {"name": "kb_mark", "arguments": {"branch": "feat/from-codex", "cwd": "/repo/x"}}},
            {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
             "params": {"name": "kb_mark", "arguments": {}}},
        ]
        responses = run_server(home, vault, requests)

        print("test_initialize")
        init = by_id(responses, 1)
        res = init.get("result") or {}
        check("responds to initialize", bool(res))
        check("echoes client protocolVersion", res.get("protocolVersion") == "2025-03-26")
        check("serverInfo.name is kb", (res.get("serverInfo") or {}).get("name") == "kb")
        check("declares tools capability", "tools" in (res.get("capabilities") or {}))
        check("notification got no response", all(r.get("id") is not None for r in responses))

        print("test_tools_list")
        tl = by_id(responses, 2)
        tools = (tl.get("result") or {}).get("tools") or []
        names = {t.get("name") for t in tools}
        check("four tools", names == {"kb_search", "kb_context", "kb_read", "kb_mark"}, str(names))
        check("every tool has inputSchema", all(t.get("inputSchema") for t in tools))
        check("search description is prescriptive (when-to-call)",
              "BEFORE proposing" in next((t["description"] for t in tools if t["name"] == "kb_search"), ""))

        print("test_kb_search")
        sr = by_id(responses, 3)
        stext = tool_text(sr)
        check("search not an error", not (sr.get("result") or {}).get("isError"), stext[:120])
        check("search finds the jwt note", "jwt-clock-skew.md" in stext, stext[:200])
        check("search ranks jwt above retry",
              stext.find("jwt-clock-skew.md") < (stext.find("rate-limit-retry.md") if "rate-limit-retry.md" in stext else len(stext)))

        print("test_kb_context")
        cr = by_id(responses, 4)
        ctext = tool_text(cr)
        check("context not an error", not (cr.get("result") or {}).get("isError"), ctext[:120])
        check("context emits vault-context block", "<vault-context>" in ctext)
        check("context cites the jwt note", "jwt-clock-skew.md" in ctext)

        print("test_kb_read")
        rr = by_id(responses, 5)
        rtext = tool_text(rr)
        check("read returns the body", "MARKER-JWT-BODY" in rtext, rtext[:120])

        print("test_kb_read_sandbox")
        esc = by_id(responses, 6)
        check("traversal flagged as tool error", (esc.get("result") or {}).get("isError") is True)
        miss = by_id(responses, 7)
        check("missing note flagged as tool error", (miss.get("result") or {}).get("isError") is True)

        print("test_kb_context_with_branch")
        cb = by_id(responses, 11)
        cbt = tool_text(cb)
        check("branch arg pulls the ticket block", "fix/login" in cbt and "Fix login timeout" in cbt, cbt[:200])

        print("test_kb_mark_mcp_tool")
        mkres = by_id(responses, 12)
        mktext = tool_text(mkres)
        check("kb_mark not an error", not (mkres.get("result") or {}).get("isError"), mktext[:120])
        check("result carries a KB-MARK token", "KB-MARK:feat/from-codex:" in mktext)
        mcp_sidecars = list((home / ".kb" / "state").glob("kb-session-branch-mcp-*.json"))
        check("writes one mcp sidecar", len(mcp_sidecars) == 1)
        mdata = json.loads(mcp_sidecars[0].read_text(encoding="utf-8")) if mcp_sidecars else {}
        check("sidecar token matches the returned token",
              bool(mdata.get("mark_token")) and mdata.get("mark_token", "") in mktext
              and mdata.get("branch") == "feat/from-codex")
        check("sidecar keeps the cwd", mdata.get("cwd") == "/repo/x")
        mkbad = by_id(responses, 13)
        check("kb_mark without branch is a tool error", (mkbad.get("result") or {}).get("isError") is True)

        print("test_kb_mark_cli")
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["KB_VAULT"] = str(vault)
        mk = subprocess.run([sys.executable, str(KB_CLI), "mark", "--done", "feat/from-codex"],
                            capture_output=True, text=True, env=env, timeout=30)
        check("kb mark --done exits 0", mk.returncode == 0, mk.stderr[:200])
        sidecars = list((home / ".kb" / "state").glob("kb-session-branch-cli-*.json"))
        check("writes one cli sidecar", len(sidecars) == 1)
        data = json.loads(sidecars[0].read_text(encoding="utf-8")) if sidecars else {}
        check("sidecar carries branch + manual_done",
              data.get("branch") == "feat/from-codex" and data.get("manual_done") is True)
        bare = subprocess.run([sys.executable, str(KB_CLI), "mark"],
                              capture_output=True, text=True, env=env, timeout=30,
                              cwd=str(home))
        check("bare kb mark refuses with usage", bare.returncode == 2 and "--done" in bare.stderr)

        print("test_protocol_errors")
        bogus = by_id(responses, 8)
        check("unknown method -> -32601", (bogus.get("error") or {}).get("code") == -32601)
        unknown_tool = by_id(responses, 9)
        check("unknown tool -> -32602", (unknown_tool.get("error") or {}).get("code") == -32602)
        ping = by_id(responses, 10)
        check("ping answered", ping.get("result") == {})

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
