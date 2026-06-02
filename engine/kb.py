#!/usr/bin/env python
"""kb — engine CLI skeleton (Phase 0 boundary).

Model-facing entrypoint for the KB engine. Today only `doctor` is real (prints
the resolved config so install/setup can verify a machine); retrieve/sync/stats
are stubs that later phases wire to the existing scripts.

Lives next to the hook for now so it can import kb_config as a sibling; the
target home is engine/ once the repo is carved out (see KB-ARCHITECTURE.md).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from kb_config import resolve_vault, workspaces_path, load_config, KBConfigError


def cmd_doctor(args) -> int:
    print("kb doctor")
    print(f"  workspaces file : {workspaces_path()}")
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


def _claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env and env.strip():
        return Path(env.strip())
    return workspaces_path().parent  # ~/.claude


def _resolve_manager() -> Path | None:
    """Find the manager's server.py whether `kb` runs from the repo (engine/
    sibling manager/) or deployed to ~/.claude/hooks (read the install-recorded
    `.kb-source` to find the clone). Returns the path, or None if not found.
    """
    here = Path(__file__).resolve().parent
    candidates = [here.parent / "manager" / "server.py"]
    src_file = _claude_dir() / ".kb-source"
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
    sub.add_parser("sync", help="(stub) capture + finalize from git").set_defaults(func=_stub("sync"))
    sub.add_parser("stats", help="(stub) token/tier stats").set_defaults(func=_stub("stats"))
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    # Only `manage` forwards unknown args through to the manager; every other
    # subcommand stays strict and rejects them.
    if getattr(args, "cmd", None) == "manage":
        args.extra = unknown
    elif unknown:
        parser.error("unrecognized arguments: " + " ".join(unknown))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
