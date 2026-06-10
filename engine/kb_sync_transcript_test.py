#!/usr/bin/env python3
"""Tests for token-marked transcript resolution in kb-sync (any-host capture).

The deterministic key under test: the `kb_mark` MCP tool returns a mark token
as its result, the host persists that result inside its own session log, and
the sync locates the session by grepping the token across the configured
transcript stores. These tests build fake Codex-style stores (rollout *.jsonl,
date-nested, optionally .zst-compressed) and assert resolution, caching,
mirroring, degradation and non-regression of the Claude Code path.

Run: python engine/kb_sync_transcript_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("kb_sync_under_test", HERE / "kb-sync.py")
kb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kb)

try:
    from compression import zstd as _zstd  # Python 3.14+
    HAS_ZSTD = True
except ImportError:
    _zstd = None
    HAS_ZSTD = False

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def rollout_line(text: str) -> str:
    """One Codex-style rollout line: a timestamped wrapper around an item."""
    return json.dumps({
        "timestamp": "2026-06-10T12:00:00Z",
        "item": {"type": "tool_result", "content": [{"type": "text", "text": text}]},
    }) + "\n"


def make_store(root: Path, name: str = "codex") -> Path:
    store = root / name / "sessions" / "2026" / "06" / "10"
    store.mkdir(parents=True)
    return store.parent.parent.parent  # the sessions/ root


def write_rollout(store_root: Path, fname: str, lines: list[str], day="2026/06/10") -> Path:
    d = store_root / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / fname
    f.write_text("".join(lines), encoding="utf-8")
    return f


def sidecar_for(state_dir: Path, branch: str, token: str, sid: str = "", marked_at: str = "") -> Path:
    sid = sid or f"mcp-{token.rsplit(':', 1)[-1]}"
    data = {
        "session_id": sid,
        "branch": branch,
        "cwd": "",
        "mark_token": token,
        "marked_at": marked_at or "2026-06-10T11:00:00",
        "manual_override": True,
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / f"kb-session-branch-{sid}.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def wire(tmp: Path, stores: list[Path]):
    """Point the module's globals at the sandbox."""
    kb.STATE_DIR = tmp / "state"
    kb.PROJECTS_DIR = tmp / "projects"
    kb.CONFIG = tmp / "kb-workspaces.json"
    kb.CONFIG.write_text(json.dumps({
        "workspaces": [], "vault": str(tmp / "vault"),
        "transcript_stores": [str(s) for s in stores],
    }), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        # ---- plain .jsonl resolution -------------------------------------
        print("test_resolve_plain_jsonl")
        store = make_store(tmp, "codexA")
        token = "KB-MARK:feat/x:aa11bb22"
        target = write_rollout(store, "rollout-2026-06-10-uuid1.jsonl", [
            rollout_line("user asked about locks"),
            rollout_line(f"Session marked under feat/x. Mark token: {token}"),
            rollout_line("assistant continued working"),
        ])
        write_rollout(store, "rollout-2026-06-10-uuid2.jsonl", [rollout_line("unrelated session")])
        wire(tmp, [store])
        sc = sidecar_for(kb.STATE_DIR, "feat/x", token)
        data = json.loads(sc.read_text(encoding="utf-8"))
        found = kb.resolve_marked_transcript(data, sc)
        check("finds the file containing the token", found == target, str(found))
        check("caches transcript_path on the sidecar",
              json.loads(sc.read_text(encoding="utf-8")).get("transcript_path") == str(target))

        print("test_resolve_uses_cache_without_rescanning")
        # poison the store: if it re-scanned, it would now fail
        target2 = target  # cached path must keep winning even with stores emptied
        wire(tmp, [tmp / "no-such-store"])
        data = json.loads(sc.read_text(encoding="utf-8"))
        found = kb.resolve_marked_transcript(data, sc)
        check("cached path resolves with stores gone", found == target2, str(found))

        print("test_find_sessions_integration")
        wire(tmp, [store])
        sessions = kb.find_sessions_for_branch("feat/x")
        check("marked sidecar yields a session entry", len(sessions) == 1)
        check("entry points at the located transcript",
              sessions and sessions[0]["jsonl_path"] == target)

        print("test_session_hints_count_lines")
        hints = kb.session_hints("feat/x", {})
        check("hints expose the marked transcript", len(hints) == 1)
        check("line range covers the file", hints and hints[0]["to_line"] == 3 and hints[0]["from_line"] == 1)

        print("test_claude_path_regression")
        # a classic Claude Code sidecar must keep resolving via PROJECTS_DIR
        cwd = "C:/work/repo" if os.name == "nt" else "/work/repo"
        enc = kb.encode_cwd(cwd)
        (kb.PROJECTS_DIR / enc).mkdir(parents=True, exist_ok=True)
        claude_jsonl = kb.PROJECTS_DIR / enc / "abc123.jsonl"
        claude_jsonl.write_text('{"role":"user"}\n', encoding="utf-8")
        (kb.STATE_DIR / "kb-session-branch-abc123.json").write_text(json.dumps({
            "session_id": "abc123", "branch": "feat/x", "cwd": cwd,
        }), encoding="utf-8")
        sessions = kb.find_sessions_for_branch("feat/x")
        kinds = sorted(str(s["jsonl_path"]) for s in sessions if s["jsonl_path"])
        check("both adapter families coexist on one branch",
              len(sessions) == 2 and str(claude_jsonl) in kinds and str(target) in kinds)

        # ---- degradation cases -------------------------------------------
        print("test_token_not_found")
        sc2 = sidecar_for(kb.STATE_DIR, "feat/ghost", "KB-MARK:feat/ghost:dead0000")
        data2 = json.loads(sc2.read_text(encoding="utf-8"))
        check("unfound token resolves to None", kb.resolve_marked_transcript(data2, sc2) is None)
        check("sidecar not polluted with a path",
              "transcript_path" not in json.loads(sc2.read_text(encoding="utf-8")))
        sessions = kb.find_sessions_for_branch("feat/ghost")
        check("session listed with jsonl_path=None (git-only capture)",
              len(sessions) == 1 and sessions[0]["jsonl_path"] is None)

        print("test_sidecar_without_token_untouched")
        check("no mark_token -> None fast-path",
              kb.resolve_marked_transcript({"branch": "b"}, sc2) is None)

        print("test_mtime_floor_skips_old_files")
        old_token = "KB-MARK:feat/old:0ld00000"
        old_file = write_rollout(store, "rollout-ancient.jsonl", [rollout_line(old_token)], day="2026/01/01")
        ancient = time.mktime(time.strptime("2026-01-01", "%Y-%m-%d"))
        os.utime(old_file, (ancient, ancient))
        sc3 = sidecar_for(kb.STATE_DIR, "feat/old", old_token, marked_at="2026-06-10T11:00:00")
        data3 = json.loads(sc3.read_text(encoding="utf-8"))
        check("file older than marked_at-24h is skipped",
              kb.resolve_marked_transcript(data3, sc3) is None)

        print("test_non_jsonl_files_ignored")
        notes_token = "KB-MARK:feat/notes:abcd1234"
        (store / "2026" / "06" / "10" / "notes.txt").write_text(notes_token, encoding="utf-8")
        sc4 = sidecar_for(kb.STATE_DIR, "feat/notes", notes_token)
        data4 = json.loads(sc4.read_text(encoding="utf-8"))
        check("token inside a .txt is not a session match",
              kb.resolve_marked_transcript(data4, sc4) is None)

        print("test_multiple_stores")
        store_b = make_store(tmp, "codexB")
        tok_b = "KB-MARK:feat/b:bbbb1111"
        target_b = write_rollout(store_b, "rollout-b.jsonl", [rollout_line(tok_b)])
        wire(tmp, [store, store_b, tmp / "missing-store"])
        sc5 = sidecar_for(kb.STATE_DIR, "feat/b", tok_b)
        data5 = json.loads(sc5.read_text(encoding="utf-8"))
        check("second store is searched, missing store ignored",
              kb.resolve_marked_transcript(data5, sc5) == target_b)

        print("test_default_store_fallback_config_empty")
        kb.CONFIG.write_text(json.dumps({"workspaces": [], "vault": str(tmp / "vault")}), encoding="utf-8")
        stores = kb.transcript_stores()
        codex_default = Path.home() / ".codex" / "sessions"
        check("defaults to ~/.codex/sessions only when it exists",
              stores == ([codex_default] if codex_default.is_dir() else []))

        # ---- zst mirror ----------------------------------------------------
        print("test_zst_mirror")
        if HAS_ZSTD:
            wire(tmp, [store])
            tok_z = "KB-MARK:feat/z:zzzz9999"
            raw = (rollout_line("compressed session start")
                   + rollout_line(f"tool result with {tok_z}")
                   + rollout_line("more turns"))
            zf = store / "2026" / "06" / "10" / "rollout-z.jsonl.zst"
            zf.write_bytes(_zstd.compress(raw.encode("utf-8")))
            sc6 = sidecar_for(kb.STATE_DIR, "feat/z", tok_z, sid="mcp-zzzz9999")
            data6 = json.loads(sc6.read_text(encoding="utf-8"))
            found_z = kb.resolve_marked_transcript(data6, sc6)
            mirror = kb.STATE_DIR / "kb-transcripts" / "mcp-zzzz9999.jsonl"
            check("zst resolves to a decompressed mirror", found_z == mirror, str(found_z))
            check("mirror holds the full plain text",
                  mirror.is_file() and tok_z in mirror.read_text(encoding="utf-8")
                  and mirror.read_text(encoding="utf-8") == raw)
            hints = kb.session_hints("feat/z", {})
            check("hints work on the mirror", len(hints) == 1 and hints[0]["to_line"] == 3)
        else:
            check("skipped (no compression.zstd on this Python)", True)

        # ---- end to end: the MCP tool output is what the store contains ----
        print("test_e2e_mark_tool_to_hints")
        wire(tmp, [store])
        os.environ["KB_VAULT"] = str(tmp / "vault")
        (tmp / "vault").mkdir(exist_ok=True)
        sys.path.insert(0, str(HERE))
        import kb_mcp  # noqa: E402
        kb_mcp.kbr.HOME = tmp  # sidecars under the sandbox, not the real HOME
        out = kb_mcp.tool_kb_mark({"branch": "feat/e2e", "cwd": str(tmp)})
        tok_line = next((ln for ln in out.splitlines() if "KB-MARK:" in ln), "")
        e2e_token = tok_line.split("Mark token:")[-1].strip()
        check("tool returned a parseable token", e2e_token.startswith("KB-MARK:feat/e2e:"), out[:120])
        # the host would persist the tool RESULT — simulate that verbatim
        write_rollout(store, "rollout-e2e.jsonl", [rollout_line(out)])
        kb.STATE_DIR = tmp / ".claude" / "state"   # where the tool wrote (HOME=tmp)
        sessions = kb.find_sessions_for_branch("feat/e2e")
        check("sync finds the session marked by the real tool output",
              len(sessions) == 1 and sessions[0]["jsonl_path"] is not None)
        hints = kb.session_hints("feat/e2e", {})
        check("e2e hints ready for capture", len(hints) == 1 and hints[0]["new_lines"] == 1)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
