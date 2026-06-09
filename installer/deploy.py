#!/usr/bin/env python3
"""Deploy KB engine + Claude Code adapter from the repo into the host (~/.claude).

Topology A: the repo is the source of truth; this step copies files into the
live host layout. It is the spine of both first-install and "update certinho".

Care taken (the host may hold a *working* setup we must not silently break):
  * content-hash diff BEFORE any write — reports new / changed / eol-only / same;
  * EOL-aware — a pure CRLF<->LF difference is NOT real divergence and is left
    alone by default (don't churn a working .sh over line endings);
  * backs up every file it overwrites into ~/.claude/.kb-backups/deploy-<ts>/,
    with a restore manifest, so a bad deploy is one `--rollback` away;
  * copy-only — never deletes host files it doesn't own.

The live layout is deliberately the proven, scattered one (engine split between
hooks/ and scripts/ because kb-context.sh resolves $HOME/.claude/hooks/kb.py and
kb-sync.py imports ../hooks/kb_config.py). Consolidation into ~/.claude/kb/ is a
future cleanup, not this step.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

# --- Manifest ----------------------------------------------------------------
# Engine splits across two host dirs (load-bearing — see module docstring).
ENGINE_TO_HOOKS = ["kb.py", "kb_config.py", "kb_retrieve.py"]
ENGINE_TO_SCRIPTS = ["kb-sync.py", "kb-embed.py", "kb-embed-daemon.py"]


def _is_test_file(name: str) -> bool:
    """Test files live next to the adapter hooks for convenience but must never
    deploy into a user's hooks dir (the host would not run them; they'd just be
    clutter shipped to end users)."""
    return name.endswith("_test.py") or name.endswith("_tests.py")


def deploy_pairs(repo_root: Path, claude_dir: Path) -> list[tuple[Path, Path]]:
    """Return [(src, dst)] for every file to deploy. Auditable + explicit.

    Engine files are mapped explicitly (the hooks/scripts split matters). Adapter
    hooks and commands are taken wholesale from their repo dirs (our controlled
    source) so adding an adapter file to the repo includes it automatically.
    """
    repo_root = Path(repo_root)
    claude_dir = Path(claude_dir)
    pairs: list[tuple[Path, Path]] = []

    eng = repo_root / "engine"
    for name in ENGINE_TO_HOOKS:
        pairs.append((eng / name, claude_dir / "hooks" / name))
    for name in ENGINE_TO_SCRIPTS:
        pairs.append((eng / name, claude_dir / "scripts" / name))

    adapter = repo_root / "adapters" / "claude-code"
    hooks_src = adapter / "hooks"
    if hooks_src.is_dir():
        for f in sorted(hooks_src.iterdir()):
            if f.is_file() and not _is_test_file(f.name):
                pairs.append((f, claude_dir / "hooks" / f.name))
    cmds_src = adapter / "commands"
    if cmds_src.is_dir():
        for f in sorted(cmds_src.glob("*.md")):
            pairs.append((f, claude_dir / "commands" / f.name))

    return pairs


# --- Diff --------------------------------------------------------------------

def _norm(b: bytes) -> bytes:
    """Normalize line endings for content comparison (CRLF/CR -> LF)."""
    return b.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def classify(src: Path, dst: Path) -> str:
    """new | changed | eol-only | same | missing-src."""
    if not src.exists():
        return "missing-src"
    if not dst.exists():
        return "new"
    sb, db = src.read_bytes(), dst.read_bytes()
    if sb == db:
        return "same"
    if _norm(sb) == _norm(db):
        return "eol-only"
    return "changed"


def diff(repo_root: Path, claude_dir: Path) -> dict:
    """Classify every manifest pair. No writes."""
    pairs = deploy_pairs(repo_root, claude_dir)
    buckets: dict[str, list[str]] = {k: [] for k in ("new", "changed", "eol-only", "same", "missing-src")}
    for src, dst in pairs:
        buckets[classify(src, dst)].append(str(dst))
    return {"total": len(pairs), "buckets": buckets}


# --- Apply -------------------------------------------------------------------

def apply(repo_root: Path, claude_dir: Path, normalize_eol: bool = False, dry_run: bool = False) -> dict:
    """Deploy. Copies `new` + `changed` (and `eol-only` only if normalize_eol).

    Backs up every overwritten target into ~/.claude/.kb-backups/deploy-<ts>/
    mirroring the path relative to claude_dir, plus a restore.json manifest.
    Returns a report.
    """
    repo_root = Path(repo_root)
    claude_dir = Path(claude_dir)
    pairs = deploy_pairs(repo_root, claude_dir)

    to_write: list[tuple[Path, Path, str]] = []
    missing: list[str] = []
    for src, dst in pairs:
        state = classify(src, dst)
        if state == "missing-src":
            missing.append(str(src))
        elif state in ("new", "changed"):
            to_write.append((src, dst, state))
        elif state == "eol-only" and normalize_eol:
            to_write.append((src, dst, state))

    report = {
        "would_write": [str(d) for _, d, _ in to_write],
        "missing_src": missing,
        "backup_dir": None,
        "wrote": 0,
        "dry_run": dry_run,
    }
    if missing:
        # A manifest file that doesn't exist in the repo is a packaging bug; surface it.
        report["error"] = f"manifest references {len(missing)} missing source file(s)"
        return report
    if not to_write or dry_run:
        return report

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = claude_dir / ".kb-backups" / f"deploy-{ts}"
    restored: list[dict] = []

    for src, dst, state in to_write:
        if dst.exists():  # only 'changed'/'eol-only' have an existing target to save
            rel = dst.relative_to(claude_dir)
            bpath = backup_dir / rel
            bpath.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, bpath)
            restored.append({"original": str(dst), "backup": str(bpath)})
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        report["wrote"] += 1

    if restored:
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "restore.json").write_text(
            json.dumps({"created": ts, "files": restored}, indent=2), encoding="utf-8"
        )
        report["backup_dir"] = str(backup_dir)
    return report


def rollback(claude_dir: Path, backup_dir: Path | None = None) -> dict:
    """Restore files from a deploy backup. Defaults to the most recent one."""
    claude_dir = Path(claude_dir)
    root = claude_dir / ".kb-backups"
    if backup_dir is None:
        candidates = sorted(root.glob("deploy-*"), reverse=True) if root.is_dir() else []
        if not candidates:
            return {"error": "no deploy backups found", "restored": 0}
        backup_dir = candidates[0]
    backup_dir = Path(backup_dir)
    manifest = backup_dir / "restore.json"
    if not manifest.exists():
        return {"error": f"no restore.json in {backup_dir}", "restored": 0}
    data = json.loads(manifest.read_text(encoding="utf-8"))
    n = 0
    for entry in data.get("files", []):
        bpath, orig = Path(entry["backup"]), Path(entry["original"])
        if bpath.exists():
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bpath, orig)
            n += 1
    return {"backup_dir": str(backup_dir), "restored": n}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Deploy KB engine + adapter into the host.")
    ap.add_argument("--repo", required=True, help="repo root (dev/kb)")
    ap.add_argument("--claude-dir", required=True, help="host Claude config dir (~/.claude)")
    ap.add_argument("--apply", action="store_true", help="write (default: diff only)")
    ap.add_argument("--normalize-eol", action="store_true", help="also rewrite eol-only files to repo EOL")
    ap.add_argument("--rollback", action="store_true", help="restore most recent deploy backup")
    args = ap.parse_args()

    if args.rollback:
        print(json.dumps(rollback(Path(args.claude_dir)), indent=2))
    elif args.apply:
        print(json.dumps(apply(Path(args.repo), Path(args.claude_dir), normalize_eol=args.normalize_eol), indent=2))
    else:
        print(json.dumps(diff(Path(args.repo), Path(args.claude_dir)), indent=2))
