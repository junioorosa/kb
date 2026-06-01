#!/usr/bin/env python
"""kb-mark-intercept — UserPromptSubmit hook.

Detects `/kb-mark <branch>` (and the --experimental / --done / --remove flags)
in the user prompt, writes to the session sidecar directly, and blocks the
prompt before the LLM is invoked. Zero token cost.

Output format expected by Claude Code (UserPromptSubmit):
    {"decision": "block", "reason": "..."}  -> blocks prompt, shows reason
    (nothing / exit 0)                       -> normal flow
"""
from __future__ import annotations

import json
import os
import re
import sys
import time


SLASH_RE = re.compile(r'^/kb-mark(\s+.*)?$')


def emit(msg: str) -> None:
    sys.stdout.write(json.dumps({"decision": "block", "reason": msg}))
    sys.stdout.flush()


def sidecar_path(session_id: str) -> str:
    state_dir = os.path.join(os.path.expanduser("~"), ".claude", "state")
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, f"kb-session-branch-{session_id}.json")


def load_sidecar(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_sidecar(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def find_kb_folders(branch: str) -> list:
    """Vault-wide list of existing KB folders matching this branch name.

    The branch name is the KB's global match key, so the lookup is
    project-agnostic — it does NOT depend on cwd (the user may run /kb-mark
    from a parent dir, not the repo root). Layout mirrors kb-sync:
    <vault>/<ws>/<project>/<type>/<slug>/ for a branch with a "/", else
    <vault>/<ws>/<project>/<branch>/. Returns vault-relative paths.
    """
    try:
        import glob as _glob
        vault = _vault_root()
        if not vault:
            return []
        if "/" in branch:
            type_, slug = branch.split("/", 1)
            pat = os.path.join(vault, "*", "*", type_, slug)
        else:
            pat = os.path.join(vault, "*", "*", branch)
        out = []
        for p in _glob.glob(pat):
            if os.path.isdir(p) and os.path.exists(os.path.join(p, "_index.md")):
                rel = os.path.relpath(p, vault).replace("\\", "/")
                if rel not in out:
                    out.append(rel)
        return out
    except Exception:
        return []


def _vault_root():
    """Vault root as a string, or None. Delegates to the shared engine resolver
    (kb_config, sibling) so the hook honors KB_VAULT and the single resolution
    order. strict=False: unresolved -> None (callers already handle None)."""
    try:
        from kb_config import resolve_vault
        v = resolve_vault(strict=False)
        return str(v) if v else None
    except Exception:
        return None


def set_index_status(rel_folder: str, status: str) -> bool:
    """Patch `status:` in <vault>/<rel_folder>/_index.md frontmatter. True on write."""
    vault = _vault_root()
    if not vault or not rel_folder:
        return False
    idx = os.path.join(vault, rel_folder.replace("/", os.sep), "_index.md")
    if not os.path.isfile(idx):
        return False
    try:
        with open(idx, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return False
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end < 0:
        return False
    fm, rest = text[:end], text[end:]
    if re.search(r"(?m)^status:\s*.*$", fm):
        fm = re.sub(r"(?m)^status:\s*.*$", f"status: {status}", fm, count=1)
    else:
        fm = fm.rstrip("\n") + f"\nstatus: {status}\n"
    try:
        with open(idx, "w", encoding="utf-8") as f:
            f.write(fm + rest)
        return True
    except Exception:
        return False


def main() -> int:
    if os.environ.get("KB_HOOKS_DISABLED") == "1":
        return 0
    if os.path.isfile(os.path.expanduser("~/.claude/kb-hooks-disabled")):
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    prompt = (payload.get("prompt") or "").strip()
    session_id = payload.get("session_id") or ""

    m = SLASH_RE.match(prompt)
    if not m:
        return 0

    args = (m.group(1) or "").split()
    remove_mode = "--remove" in args or "-r" in args
    done_mode = "--done" in args or "-d" in args
    exp_mode = "--experimental" in args or "--exp" in args
    branch_args = [a for a in args if not a.startswith("-")]

    if not session_id:
        emit("kb-mark: session_id missing from payload")
        return 0

    path = sidecar_path(session_id)

    if remove_mode:
        if not os.path.exists(path):
            emit("kb-mark: nothing to remove (session is not marked)")
            return 0
        prev = load_sidecar(path).get("branch", "?")
        try:
            os.remove(path)
        except Exception as e:
            emit(f"kb-mark: failed to remove sidecar: {e}")
            return 0
        emit(f"kb-mark removed (was {prev})")
        return 0

    if done_mode:
        data = load_sidecar(path)
        branch = branch_args[0] if branch_args else data.get("branch", "")
        if not branch:
            emit("kb-mark --done: no branch (session is not marked). Use /kb-mark --done <branch>")
            return 0
        data["session_id"] = session_id
        data["branch"] = branch
        data["cwd"] = payload.get("cwd") or data.get("cwd") or ""
        data["manual_done"] = True
        data["done_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            save_sidecar(path, data)
        except Exception as e:
            emit(f"kb-mark: failed to write sidecar: {e}")
            return 0
        emit(f"kb-mark --done -> {branch} (finalized on the next sync)")
        return 0

    if exp_mode:
        data = load_sidecar(path)
        branch = branch_args[0] if branch_args else data.get("branch", "")
        if not branch:
            emit("kb-mark --experimental: no branch (session is not marked). "
                 "Use /kb-mark --experimental <branch>")
            return 0
        cwd = payload.get("cwd") or data.get("cwd") or ""
        data["session_id"] = session_id
        data["branch"] = branch
        data["cwd"] = cwd
        data["mark_experimental"] = True
        data["marked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            save_sidecar(path, data)
        except Exception as e:
            emit(f"kb-mark: failed to write sidecar: {e}")
            return 0
        folders = find_kb_folders(branch)
        if len(folders) == 1 and set_index_status(folders[0], "experimental"):
            emit(f"kb-mark --experimental -> {branch}\n"
                 f"status=experimental in {folders[0]}/_index.md — retrieval down-weight. "
                 f"Reverts automatically once the branch merges (sync sets resolved).")
        elif len(folders) > 1:
            emit(f"kb-mark --experimental -> {branch}\n"
                 f"! multiple KB folders match this branch ({', '.join(folders)}) — ambiguous, "
                 f"not patching now. The next sync marks the right one (by project) at capture.")
        else:
            emit(f"kb-mark --experimental -> {branch} (no _index.md yet; "
                 f"the next sync marks it experimental at capture)")
        return 0

    if not branch_args:
        emit("kb-mark: pass a branch. e.g. /kb-mark feat/my-feature  |  "
             "/kb-mark --experimental  |  /kb-mark --done  |  /kb-mark --remove")
        return 0

    branch = branch_args[0]
    data = load_sidecar(path)
    cwd = payload.get("cwd") or data.get("cwd") or ""
    data["session_id"] = session_id
    data["branch"] = branch
    data["cwd"] = cwd
    if "started_at" not in data:
        data["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    data["manual_override"] = True
    data["marked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if "auto" not in data:
        data["auto"] = False

    try:
        save_sidecar(path, data)
    except Exception as e:
        emit(f"kb-mark: failed to write sidecar: {e}")
        return 0

    existing = find_kb_folders(branch)
    if existing:
        more = f" (+{len(existing) - 1} more)" if len(existing) > 1 else ""
        emit(f"kb-mark -> {branch}\n! a KB folder with this name already exists: {existing[0]}{more} — "
             f"the sync will UPDATE it (it won't create a new one). If this is new work, rename the branch.")
    else:
        emit(f"kb-mark -> {branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
