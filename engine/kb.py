#!/usr/bin/env python
"""kb — engine CLI skeleton (Phase 0 boundary).

Model-facing entrypoint for the KB engine. Today only `doctor` is real (prints
the resolved config so install/setup can verify a machine); retrieve/sync/stats
are stubs that later phases wire to the existing scripts.

Lives next to the hook for now so it can import kb_config as a sibling; the
target home is engine/ once the repo is carved out.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from kb_config import resolve_vault, workspaces_path, load_config, kb_home, state_dir, KBConfigError


def cmd_doctor(args) -> int:
    print("kb doctor")
    print(f"  kb home         : {kb_home()}")
    print(f"  config file     : {workspaces_path()}")
    cfg = load_config()
    ws = cfg.get("workspaces") or []
    print(f"  workspaces      : {len(ws)}")
    for w in ws:
        if isinstance(w, dict):
            print(f"    - {w.get('name', '?')} -> {w.get('path', '?')}")
    try:
        vault = resolve_vault(strict=True)
    except KBConfigError as exc:
        print("  vault           : UNRESOLVED")
        print(f"  ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"  vault           : {vault}")
    print(f"  vault exists    : {vault.exists()}")
    return 0


def cmd_retrieve(args) -> int:
    """Engine entrypoint for context injection — the hook's only door inward.

    The retrieval pipeline still lives in kb_retrieve (engine internals): import
    runs module-level resolution, main() reads the hook payload on stdin and
    prints the <vault-context>. This thin adapter just owns the contract the hook
    relies on — it must NEVER crash into the prompt flow, so any failure degrades
    to exit 0 (mirrors kb_retrieve's own __main__ guard).
    """
    try:
        import kb_retrieve
        kb_retrieve.main()
    except Exception as exc:
        try:
            import kb_retrieve as _kr
            _kr.log_budget(f"kb retrieve crash: {exc}")
        except Exception:
            pass
    return 0


def _resolve_manager() -> Path | None:
    """Find the manager's server.py whether `kb` runs from the repo (engine/
    sibling manager/) or deployed to <kb home>/engine (read the install-recorded
    `.source` stamp to find the clone). Returns the path, or None if not found.
    """
    here = Path(__file__).resolve().parent
    candidates = [here.parent / "manager" / "server.py"]
    # Current stamp, then the pre-0.11 location an un-migrated install still has.
    for src_file in (kb_home() / ".source",
                     Path(os.path.expanduser("~")) / ".claude" / ".kb-source"):
        if src_file.exists():
            src = src_file.read_text(encoding="utf-8").strip()
            if src:
                candidates.append(Path(src) / "manager" / "server.py")
    return next((c for c in candidates if c.exists()), None)


def cmd_manage(args) -> int:
    """Launch the manager (config UI). It serves a localhost page and opens the browser."""
    server = _resolve_manager()
    if server is None:
        print("kb manage: manager not found. Run the installer, or launch it from the repo "
              "(python manager/server.py).", file=sys.stderr)
        return 1
    extra = getattr(args, "extra", [])
    return subprocess.call([sys.executable, str(server), *extra])


def cmd_consolidate(args) -> int:
    """Run the non-destructive vault consolidation pass (sibling engine script).
    Forwards its flags (--workspace, --dry-run, --cap, --max-turns) through."""
    script = Path(__file__).resolve().parent / "kb-consolidate.py"
    if not script.exists():
        print("kb consolidate: kb-consolidate.py not found beside the CLI.", file=sys.stderr)
        return 1
    return subprocess.call([sys.executable, str(script), *getattr(args, "extra", [])])


def _detect_branch() -> str:
    """Current git branch of the working dir, or "" (non-repo / detached / no git)."""
    try:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=3)
        branch = (r.stdout or "").strip()
        if r.returncode == 0 and branch and branch != "HEAD":
            return branch
    except Exception:
        pass
    return ""


def cmd_mark(args) -> int:
    """Ticket maintenance from ANY host or terminal — the host-neutral subset of
    the Claude Code `/kb-mark` intercept.

    Session→branch marking proper exists to locate a host's TRANSCRIPTS, so it
    lives in each host adapter (Claude Code today). Closing or down-weighting a
    ticket, though, is keyed by BRANCH alone — the sync aggregates the sidecar
    flags per branch — so it works from anywhere. The sidecar gets a synthetic
    `cli-<epoch>` session id: harmless, since no transcript lookup will match it.
    """
    import time as _time
    branch = (args.branch or "").strip() or _detect_branch()
    if not branch:
        print("kb mark: no branch given and the current dir is not on a git branch.\n"
              "Usage: kb mark --done [branch] | kb mark --experimental [branch]", file=sys.stderr)
        return 2
    if not (args.done or args.experimental):
        print("kb mark: session marking is host-specific (Claude Code: /kb-mark).\n"
              "From here you can close or down-weight a ticket:\n"
              "  kb mark --done [branch]          close on the next sync (status: resolved)\n"
              "  kb mark --experimental [branch]  down-weight in retrieval", file=sys.stderr)
        return 2
    sdir = state_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    sid = f"cli-{int(_time.time())}"
    data = {
        "session_id": sid,
        "branch": branch,
        "cwd": os.getcwd(),
        "marked_at": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "manual_override": True,
    }
    if args.done:
        data["manual_done"] = True
        data["done_at"] = data["marked_at"]
    if args.experimental:
        data["mark_experimental"] = True
    path = sdir / f"kb-session-branch-{sid}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    what = "done -> resolved on the next sync" if args.done else "experimental (retrieval down-weight)"
    print(f"kb mark: \"{branch}\" marked {what}.")
    return 0


def cmd_mcp(args) -> int:
    """Serve the KB over MCP (stdio) — the pull adapter for hook-less hosts.

    The host (Codex CLI, Cursor, Claude Desktop, ...) spawns this process and
    owns its stdio; kb_mcp speaks newline-delimited JSON-RPC until EOF.
    """
    import kb_mcp
    return kb_mcp.serve()


def _stub(name: str):
    def run(args) -> int:
        print(f"kb {name}: not wired yet (Phase 0 skeleton). "
              f"Use the existing hook/script for now.", file=sys.stderr)
        return 2
    return run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kb", description="KB engine CLI (skeleton).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="print resolved config + vault (loud error if unresolved)").set_defaults(func=cmd_doctor)
    sub.add_parser("retrieve", help="inject vault context for a prompt (reads hook payload on stdin)").set_defaults(func=cmd_retrieve)
    sub.add_parser("manage", help="launch the manager config UI in the browser"
                   ).set_defaults(func=cmd_manage)
    sub.add_parser("mcp", help="serve the KB over MCP stdio (for hook-less hosts: Codex, Cursor, ...)"
                   ).set_defaults(func=cmd_mcp)
    p_mark = sub.add_parser("mark", help="ticket maintenance from any terminal: close or down-weight a branch")
    p_mark.add_argument("branch", nargs="?", default="", help="branch (default: current git branch)")
    p_mark.add_argument("--done", action="store_true", help="close the ticket on the next sync (status: resolved)")
    p_mark.add_argument("--experimental", action="store_true", help="down-weight the ticket in retrieval")
    p_mark.set_defaults(func=cmd_mark)
    sub.add_parser("consolidate", help="non-destructive vault cleanup pass (merge dups, resolve contradictions) on a review branch"
                   ).set_defaults(func=cmd_consolidate)
    sub.add_parser("sync", help="(stub) capture + finalize from git").set_defaults(func=_stub("sync"))
    sub.add_parser("stats", help="(stub) token/tier stats").set_defaults(func=_stub("stats"))
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    # `manage` and `consolidate` forward unknown args through to their sibling
    # script; every other subcommand stays strict and rejects them.
    if getattr(args, "cmd", None) in ("manage", "consolidate"):
        args.extra = unknown
    elif unknown:
        parser.error("unrecognized arguments: " + " ".join(unknown))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
