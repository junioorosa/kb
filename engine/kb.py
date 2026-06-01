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
import sys

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
    sub.add_parser("sync", help="(stub) capture + finalize from git").set_defaults(func=_stub("sync"))
    sub.add_parser("stats", help="(stub) token/tier stats").set_defaults(func=_stub("stats"))
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
