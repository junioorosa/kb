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
import subprocess
import sys
import time


SLASH_RE = re.compile(r'^/kb-mark(\s+.*)?$')


def emit(msg: str) -> None:
    sys.stdout.write(json.dumps({"decision": "block", "reason": msg}))
    sys.stdout.flush()


def _kb_home() -> str:
    return os.environ.get("KB_HOME") or os.path.join(os.path.expanduser("~"), ".kb")


def sidecar_path(session_id: str) -> str:
    state_dir = os.path.join(_kb_home(), "state")
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


def detect_branch(payload: dict) -> str:
    """The current git branch of the dir Claude Code runs in, or "" if none.

    This is the default when `/kb-mark` is called with no branch: the user is
    almost always sitting in the repo they want to mark. It's a DETERMINISTIC read
    of the actual HEAD of `payload.cwd` — not a "best guess" — so it honors the
    KB rule against guessing the match key. Returns "" (caller falls back to the
    usage hint) when the dir isn't a git repo, git is absent, or HEAD is detached
    ("HEAD"), since none of those name a branch to mark."""
    cwd = payload.get("cwd") or os.getcwd()
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        branch = (r.stdout or "").strip()
        if r.returncode == 0 and branch and branch != "HEAD":
            return branch
    except Exception:
        pass
    return ""


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
    if os.path.isfile(os.path.join(_kb_home(), "hooks-disabled")):
        return 0
    if os.path.isfile(os.path.expanduser("~/.claude/kb-hooks-disabled")):  # legacy
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
        emit("KB · /kb-mark couldn't run: no session id in the hook payload.")
        return 0

    path = sidecar_path(session_id)

    if remove_mode:
        if not os.path.exists(path):
            emit("KB · nothing to remove — this session isn't marked.")
            return 0
        prev = load_sidecar(path).get("branch", "?")
        try:
            os.remove(path)
        except Exception as e:
            emit(f"KB · couldn't remove the mark: {e}")
            return 0
        emit(f"✓ KB · mark removed — this session is no longer tracked (was {prev}).")
        return 0

    if done_mode:
        data = load_sidecar(path)
        branch = branch_args[0] if branch_args else (data.get("branch", "") or detect_branch(payload))
        if not branch:
            emit("KB · /kb-mark --done needs a branch — this session isn't marked and the current "
                 "folder isn't a git repo. Try: /kb-mark --done <branch>")
            return 0
        data["session_id"] = session_id
        data["branch"] = branch
        # Marking is manual-only: no SessionStart hook pre-populates the sidecar, so
        # this intercept is the sole cwd source. Prefer the payload's cwd; fall back to
        # the process cwd (the dir the host spawned the hook in) for hosts that omit it.
        data["cwd"] = payload.get("cwd") or data.get("cwd") or os.getcwd()
        data["manual_done"] = True
        data["done_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            save_sidecar(path, data)
        except Exception as e:
            emit(f"KB · couldn't save the mark: {e}")
            return 0
        emit(f"✓ KB · \"{branch}\" marked done — the next sync closes the ticket (status: resolved).")
        return 0

    if exp_mode:
        data = load_sidecar(path)
        branch = branch_args[0] if branch_args else (data.get("branch", "") or detect_branch(payload))
        if not branch:
            emit("KB · /kb-mark --experimental needs a branch — this session isn't marked and the "
                 "current folder isn't a git repo. Try: /kb-mark --experimental <branch>")
            return 0
        cwd = payload.get("cwd") or data.get("cwd") or os.getcwd()
        data["session_id"] = session_id
        data["branch"] = branch
        data["cwd"] = cwd
        data["mark_experimental"] = True
        data["marked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            save_sidecar(path, data)
        except Exception as e:
            emit(f"KB · couldn't save the mark: {e}")
            return 0
        folders = find_kb_folders(branch)
        if len(folders) == 1 and set_index_status(folders[0], "experimental"):
            emit(f"✓ KB · \"{branch}\" marked experimental — down-ranked in retrieval so it "
                 f"won't crowd unrelated searches (in {folders[0]}/_index.md). "
                 f"Reverts automatically when the branch merges.")
        elif len(folders) > 1:
            emit(f"✓ KB · \"{branch}\" marked experimental.\n"
                 f"⚠ Several notes match this branch ({', '.join(folders)}) — leaving their status "
                 f"alone for now; the next sync marks the right one at capture.")
        else:
            emit(f"✓ KB · \"{branch}\" marked experimental — no note exists yet; "
                 f"the next sync marks it at capture.")
        return 0

    # No branch given: default to the current git branch of the dir Claude Code is
    # in (the repo the user is almost always sitting in). Explicit arg still wins.
    if branch_args:
        branch = branch_args[0]
        auto_detected = False
    else:
        branch = detect_branch(payload)
        auto_detected = bool(branch)
        if not branch:
            emit("KB · /kb-mark needs a branch — couldn't read one from the current folder "
                 "(not a git repo?). Pass it explicitly. Examples:\n"
                 "  /kb-mark feat/my-feature   /kb-mark --experimental   "
                 "/kb-mark --done   /kb-mark --remove")
            return 0

    data = load_sidecar(path)
    cwd = payload.get("cwd") or data.get("cwd") or os.getcwd()
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
        emit(f"KB · couldn't save the mark: {e}")
        return 0

    suffix = " (current branch)" if auto_detected else ""
    existing = find_kb_folders(branch)
    if existing:
        more = f" (+{len(existing) - 1} more)" if len(existing) > 1 else ""
        emit(f"✓ KB · marked this session → \"{branch}\"{suffix}\n"
             f"⚠ A note already exists for this branch: {existing[0]}{more}. The sync will update it "
             f"(not create a new one) — rename the branch if this is different work.")
    else:
        emit(f"✓ KB · marked this session → \"{branch}\"{suffix}\n"
             f"The next sync captures this branch's work into your knowledge base.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
