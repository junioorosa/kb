#!/usr/bin/env python3
"""Tests for mcp_wire — wiring the KB MCP server into detected hosts.

Everything runs against a fake HOME/APPDATA; the real machine is never
touched. Covers: fresh configs, additive merges that preserve foreign
servers, non-destructive skips of existing kb entries, malformed-file
refusal, TOML append + reparse, AGENTS.md marker block, host detection,
idempotency, backups, dry-run, and the absolute-python command contract.
"""
from __future__ import annotations

import json
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_wire  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def wire(home: Path, cdir: Path, dry_run=False):
    # local_packages MUST be injected too: without it the MSIX fallback would
    # scan the real %LOCALAPPDATA%\Packages and wire the machine's actual
    # Claude Desktop from inside the test suite.
    return mcp_wire.wire_all(cdir, home=home, appdata=home / "AppData" / "Roaming",
                             local_packages=home / "AppData" / "Local" / "Packages",
                             dry_run=dry_run)


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cdir = tmp / ".claude"
        cdir.mkdir()

        print("test_nothing_detected")
        home0 = tmp / "home0"
        home0.mkdir()
        rep = wire(home0, cdir)
        check("all hosts not-detected",
              all(r["status"] == "not-detected" for r in rep["hosts"].values()))
        check("nothing created on disk", list(home0.iterdir()) == [])

        print("test_server_command_contract")
        srv = rep["server"]
        check("command is the absolute python", srv["command"] == str(Path(sys.executable)).replace("\\", "/"))
        check("args point at deployed kb.py + mcp",
              srv["args"][0].endswith(".claude/hooks/kb.py") and srv["args"][1] == "mcp")

        # ---- JSON hosts -----------------------------------------------------
        print("test_json_fresh_config")
        home1 = tmp / "home1"
        (home1 / ".cursor").mkdir(parents=True)
        rep = wire(home1, cdir)
        cur = rep["hosts"]["cursor"]
        check("cursor wired", cur["status"] == "wired", str(cur))
        data = json.loads((home1 / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
        check("kb entry written", "kb" in data.get("mcpServers", {}))
        check("entry carries absolute python",
              data["mcpServers"]["kb"]["command"] == srv["command"])
        check("no backup for a fresh file", cur["backup"] is None)

        print("test_json_additive_preserves_foreign")
        home2 = tmp / "home2"
        gem = home2 / ".gemini"
        gem.mkdir(parents=True)
        (gem / "settings.json").write_text(json.dumps({
            "theme": "dark",
            "mcpServers": {"other-tool": {"command": "node", "args": ["x.js"]}},
        }), encoding="utf-8")
        rep = wire(home2, cdir)
        g = rep["hosts"]["gemini"]
        data = json.loads((gem / "settings.json").read_text(encoding="utf-8"))
        check("gemini wired", g["status"] == "wired")
        check("foreign server preserved", data["mcpServers"]["other-tool"]["command"] == "node")
        check("unrelated keys preserved", data.get("theme") == "dark")
        check("backup taken for existing file", g["backup"] and Path(g["backup"]).exists())

        print("test_json_existing_kb_never_overwritten")
        home3 = tmp / "home3"
        cur3 = home3 / ".cursor"
        cur3.mkdir(parents=True)
        divergent = {"mcpServers": {"kb": {"command": "my-custom-python", "args": ["mine.py"]}}}
        (cur3 / "mcp.json").write_text(json.dumps(divergent), encoding="utf-8")
        before = (cur3 / "mcp.json").read_bytes()
        rep = wire(home3, cdir)
        check("divergent kb entry skipped", rep["hosts"]["cursor"]["status"] == "already")
        check("file byte-identical", (cur3 / "mcp.json").read_bytes() == before)

        print("test_json_malformed_refused")
        home4 = tmp / "home4"
        ws = home4 / ".codeium" / "windsurf"
        ws.mkdir(parents=True)
        (ws / "mcp_config.json").write_text("{ not valid", encoding="utf-8")
        rep = wire(home4, cdir)
        w = rep["hosts"]["windsurf"]
        check("malformed json refused", w["status"] == "refused-malformed")
        check("malformed file untouched",
              (ws / "mcp_config.json").read_text(encoding="utf-8") == "{ not valid")

        print("test_json_top_level_array_refused")
        (ws / "mcp_config.json").write_text("[1, 2]", encoding="utf-8")
        rep = wire(home4, cdir)
        check("non-object json refused", rep["hosts"]["windsurf"]["status"] == "refused-malformed")

        print("test_claude_desktop_appdata_path")
        home5 = tmp / "home5"
        appdata = home5 / "AppData" / "Roaming"
        (appdata / "Claude").mkdir(parents=True)
        rep = mcp_wire.wire_all(cdir, home=home5, appdata=appdata,
                                local_packages=home5 / "AppData" / "Local" / "Packages")
        cd = rep["hosts"]["claude-desktop"]
        if sys.platform == "win32":
            check("desktop wired under APPDATA", cd["status"] == "wired", str(cd))
            cfg = appdata / "Claude" / "claude_desktop_config.json"
            check("desktop config created", cfg.is_file()
                  and "kb" in json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"])
        else:
            check("skipped (non-windows desktop path)", True)
            check("skipped (non-windows desktop path)", True)

        print("test_claude_desktop_msix_layout")
        home5b = tmp / "home5b"
        pkgs = home5b / "AppData" / "Local" / "Packages"
        msix_cfg_dir = pkgs / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
        msix_cfg_dir.mkdir(parents=True)
        rep = mcp_wire.wire_all(cdir, home=home5b, appdata=home5b / "AppData" / "Roaming",
                                local_packages=pkgs)
        cd = rep["hosts"]["claude-desktop"]
        if sys.platform == "win32":
            check("MSIX desktop detected and wired", cd["status"] == "wired", str(cd))
            cfg = msix_cfg_dir / "claude_desktop_config.json"
            check("config written inside the virtualized AppData", cfg.is_file()
                  and "kb" in json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"])
            # precedence: when BOTH layouts exist, the classic one wins
            (home5b / "AppData" / "Roaming" / "Claude").mkdir(parents=True)
            spec = next(s for s in mcp_wire.host_specs(
                home5b, home5b / "AppData" / "Roaming", pkgs) if s["name"] == "claude-desktop")
            check("classic location wins over MSIX when both exist",
                  "Packages" not in str(spec["config"]))
        else:
            check("skipped (non-windows msix path)", True)
            check("skipped (non-windows msix path)", True)
            check("skipped (non-windows msix path)", True)

        # ---- TOML host (codex) ----------------------------------------------
        print("test_toml_fresh")
        home6 = tmp / "home6"
        (home6 / ".codex").mkdir(parents=True)
        rep = wire(home6, cdir)
        cx = rep["hosts"]["codex"]
        check("codex wired", cx["status"] == "wired", str(cx))
        toml_text = (home6 / ".codex" / "config.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(toml_text)
        check("toml parses with kb server", parsed["mcp_servers"]["kb"]["args"][-1] == "mcp")
        check("command is absolute python", parsed["mcp_servers"]["kb"]["command"] == srv["command"])

        print("test_toml_append_preserves_existing")
        home7 = tmp / "home7"
        (home7 / ".codex").mkdir(parents=True)
        existing = 'model = "o4-mini"\n\n[mcp_servers.linear]\ncommand = "npx"\nargs = ["linear-mcp"]\n'
        (home7 / ".codex" / "config.toml").write_text(existing, encoding="utf-8")
        rep = wire(home7, cdir)
        toml_text = (home7 / ".codex" / "config.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(toml_text)
        check("codex wired alongside existing", rep["hosts"]["codex"]["status"] == "wired")
        check("existing top-level key preserved", parsed.get("model") == "o4-mini")
        check("existing mcp server preserved", parsed["mcp_servers"]["linear"]["command"] == "npx")
        check("kb appended", "kb" in parsed["mcp_servers"])
        check("original text is a prefix (pure append)", toml_text.startswith(existing.rstrip("\n")))

        print("test_toml_existing_kb_skipped")
        before = (home7 / ".codex" / "config.toml").read_bytes()
        rep = wire(home7, cdir)
        check("second run skips", rep["hosts"]["codex"]["status"] == "already")
        check("toml byte-identical", (home7 / ".codex" / "config.toml").read_bytes() == before)

        print("test_toml_malformed_refused")
        home8 = tmp / "home8"
        (home8 / ".codex").mkdir(parents=True)
        (home8 / ".codex" / "config.toml").write_text("model = [unclosed", encoding="utf-8")
        rep = wire(home8, cdir)
        check("malformed toml refused", rep["hosts"]["codex"]["status"] == "refused-malformed")
        check("malformed toml untouched",
              (home8 / ".codex" / "config.toml").read_text(encoding="utf-8") == "model = [unclosed")

        # ---- AGENTS.md nudge -------------------------------------------------
        print("test_agents_md")
        agents = home6 / ".codex" / "AGENTS.md"
        check("created on fresh codex wire", agents.is_file()
              and mcp_wire.AGENTS_BEGIN in agents.read_text(encoding="utf-8"))
        existing_agents = home7 / ".codex" / "AGENTS.md"
        check("appended next to existing config",
              existing_agents.is_file() and "kb_search" in existing_agents.read_text(encoding="utf-8"))
        own = "# My own rules\nAlways be terse.\n"
        existing_agents.write_text(own + existing_agents.read_text(encoding="utf-8"), encoding="utf-8")
        rep = wire(home7, cdir)
        text = existing_agents.read_text(encoding="utf-8")
        check("user content preserved on re-run", text.startswith("# My own rules"))
        check("block not duplicated", text.count(mcp_wire.AGENTS_BEGIN) == 1)

        # ---- idempotency / dry-run -------------------------------------------
        print("test_idempotent_wire_all")
        snap = {p: p.read_bytes() for p in home6.rglob("*") if p.is_file()}
        rep = wire(home6, cdir)
        check("everything already on re-run",
              all(r["status"] in ("already", "not-detected") for r in rep["hosts"].values()))
        check("no byte changed on re-run",
              all(p.read_bytes() == b for p, b in snap.items()))

        print("test_dry_run_writes_nothing")
        home9 = tmp / "home9"
        (home9 / ".cursor").mkdir(parents=True)
        (home9 / ".codex").mkdir(parents=True)
        rep = wire(home9, cdir, dry_run=True)
        check("dry-run reports would-wire",
              rep["hosts"]["cursor"]["status"] == "would-wire"
              and rep["hosts"]["codex"]["status"] == "would-wire")
        check("dry-run leaves the disk empty",
              not (home9 / ".cursor" / "mcp.json").exists()
              and not (home9 / ".codex" / "config.toml").exists()
              and not (home9 / ".codex" / "AGENTS.md").exists())

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
