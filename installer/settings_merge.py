#!/usr/bin/env python3
"""Idempotent, additive merge of KB hook entries into a Claude Code settings.json.

The single most dangerous step of installing onto someone else's machine: their
settings.json holds THEIR hooks, statusline and config — none of which we can
predict. A clobber on first install destroys trust on contact. So this module:

  * adds only KB's own entries, keyed by a stable substring of the command;
  * never touches foreign entries (other tools' hooks live side by side);
  * is idempotent — re-running adds nothing and writes nothing;
  * backs up settings.json before any write;
  * REFUSES to write if the existing file won't parse (never destroy config);
  * writes atomically (temp + os.replace).

Pure-ish: one entry point, `merge_settings(...)`. No global state, no network.
Reused by the installer and (later) the manager app's "enable integration" action.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


class SettingsMergeError(Exception):
    """Raised when the merge cannot proceed safely (e.g. unparseable settings)."""


# --- Desired KB entries ------------------------------------------------------
# Keyed by `key`: a stable substring of the command unique to the KB hook. The
# merge treats "any existing command containing `key`" as already-present, so a
# machine that wired these earlier (possibly with different timeouts or a
# localized statusMessage) is recognized and left untouched. Status messages are
# English here (distributable tool); the key match means we never duplicate an
# existing localized entry.


def kb_desired_hooks(kb_dir: Path) -> dict:
    """Build the KB hook entries with absolute commands for this target.

    `kb_dir` is the KB home (e.g. ~/.kb); the hook scripts live flat in its
    engine/ subdir. Paths use POSIX separators because the commands are handed
    to bash, matching the format Claude Code already writes on Windows
    ("C:/Users/.../hook.sh").
    """
    h = kb_dir.as_posix().rstrip("/")

    def bash(script: str) -> str:
        return f'bash "{h}/engine/{script}"'

    return {
        "UserPromptSubmit": {
            "matcher": None,
            "entries": [
                {
                    "key": "kb-mark-intercept.sh",
                    "hook": {
                        "type": "command",
                        "command": bash("kb-mark-intercept.sh"),
                        "timeout": 5,
                        "statusMessage": "KB: checking /kb-mark...",
                    },
                },
                {
                    "key": "kb-stats-intercept.sh",
                    "hook": {
                        "type": "command",
                        "command": bash("kb-stats-intercept.sh"),
                        "timeout": 5,
                        "statusMessage": "KB: checking /kb-stats...",
                    },
                },
                {
                    "key": "kb-context.sh",
                    "hook": {
                        "type": "command",
                        "command": bash("kb-context.sh"),
                        "timeout": 10,
                        "statusMessage": "KB: retrieval (hybrid embedding + BM25)...",
                    },
                },
            ],
        },
        "PostToolUse": {
            "matcher": "Read",
            "entries": [
                {
                    "key": "kb-bodyread-track.sh",
                    "hook": {
                        "type": "command",
                        "command": bash("kb-bodyread-track.sh"),
                        "timeout": 5,
                    },
                },
            ],
        },
        "SessionStart": {
            "matcher": None,
            "entries": [
                # Branch marking is manual-only (`/kb-mark`, via the UserPromptSubmit
                # intercept). There is deliberately NO SessionStart marking hook: the
                # session<->branch sidecar is a file keyed by session_id that the sync
                # reads directly, so a manual mark already survives a resume on its own.
                {
                    "key": "kb-embed-daemon-spawn.sh",
                    "hook": {
                        "type": "command",
                        "command": bash("kb-embed-daemon-spawn.sh"),
                        "timeout": 5,
                        "statusMessage": "KB: ensuring embedding daemon...",
                    },
                },
            ],
        },
    }


# --- statusLine ---------------------------------------------------------------
# settings.json holds at most ONE statusLine, so this is the one place the merge
# could collide with the user's own setup. The key below matches both the new
# kb-statusline.sh and the legacy kb-statusline-fragment.ps1, so an entry
# containing it is OURS to repoint; anything else is foreign and never touched.

STATUSLINE_KEY = "kb-statusline"


def kb_desired_statusline(kb_dir: Path) -> dict:
    """The statusLine entry the installer wires on machines that have none."""
    h = kb_dir.as_posix().rstrip("/")
    return {"type": "command", "command": f'bash "{h}/engine/kb-statusline.sh"'}


# --- Merge core --------------------------------------------------------------


def _load_settings(path: Path) -> dict:
    """Load settings.json. Missing -> {} (fresh install). Unparseable -> refuse.

    Refusing on a malformed-but-present file is deliberate: overwriting a file we
    cannot parse could destroy a config we simply failed to read.
    """
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SettingsMergeError(f"cannot read {path}: {e}") from e
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SettingsMergeError(
            f"{path} exists but is not valid JSON ({e}); refusing to overwrite. "
            "Fix or remove the file, then re-run."
        ) from e
    if not isinstance(data, dict):
        raise SettingsMergeError(f"{path} is valid JSON but not an object; refusing to touch it.")
    return data


def _find_group(groups: list, matcher) -> dict | None:
    """Locate the hook group matching `matcher` (None = the group with no matcher)."""
    for g in groups:
        if not isinstance(g, dict):
            continue
        if matcher is None:
            if "matcher" not in g:
                return g
        else:
            if g.get("matcher") == matcher:
                return g
    return None


def _entry_with_key(group: dict, key: str) -> dict | None:
    for hook in group.get("hooks", []):
        if isinstance(hook, dict) and key in str(hook.get("command", "")):
            return hook
    return None


def merge_settings(settings_path: Path, kb_dir: Path, dry_run: bool = False) -> dict:
    """Ensure KB hook entries exist in settings.json. Returns a change report.

    An entry whose command contains our key but points somewhere else (the
    pre-0.11 ~/.claude/hooks path, or a moved KB home) is OURS — its command is
    rewritten to the current target, preserving the user's timeout /
    statusMessage customizations. That one field is the only thing ever updated;
    foreign entries stay untouched.

    The statusLine key follows the same ownership rules: absent -> set ours;
    contains STATUSLINE_KEY -> ours, command repointed when stale (other fields,
    e.g. padding, preserved); anything else -> foreign, never touched — the
    installer summary tells the user how to compose the KB segment into their
    own statusline instead.

    Report shape:
        {
          "changed": bool,
          "added":   ["UserPromptSubmit:kb-context.sh", ...],
          "updated": ["UserPromptSubmit:kb-context.sh", ...],  # command repointed
          "skipped": ["UserPromptSubmit:kb-mark-intercept.sh", ...],  # already current
          "created_groups": ["PostToolUse[matcher=Read]", ...],
          "statusline": "set" | "updated" | "current" | "foreign",
          "backup": "<path or None>",
          "wrote": bool,
        }
    """
    settings_path = Path(settings_path)
    kb_dir = Path(kb_dir)

    settings = _load_settings(settings_path)
    desired = kb_desired_hooks(kb_dir)

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsMergeError("settings['hooks'] is present but not an object; refusing to touch it.")

    report = {
        "changed": False,
        "added": [],
        "updated": [],
        "skipped": [],
        "created_groups": [],
        "statusline": None,
        "backup": None,
        "wrote": False,
    }

    for event, spec in desired.items():
        matcher = spec["matcher"]
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise SettingsMergeError(f"settings['hooks']['{event}'] is not a list; refusing to touch it.")

        group = _find_group(groups, matcher)
        if group is None:
            group = {"hooks": []}
            if matcher is not None:
                group["matcher"] = matcher
            groups.append(group)
            tag = f"{event}[matcher={matcher}]" if matcher is not None else event
            report["created_groups"].append(tag)
            report["changed"] = True
        group.setdefault("hooks", [])

        for entry in spec["entries"]:
            key = entry["key"]
            label = f"{event}:{key}"
            existing = _entry_with_key(group, key)
            if existing is None:
                group["hooks"].append(entry["hook"])
                report["added"].append(label)
                report["changed"] = True
            elif existing.get("command") != entry["hook"]["command"]:
                existing["command"] = entry["hook"]["command"]
                report["updated"].append(label)
                report["changed"] = True
            else:
                report["skipped"].append(label)

    desired_sl = kb_desired_statusline(kb_dir)
    sl = settings.get("statusLine")
    if sl is None:
        settings["statusLine"] = desired_sl
        report["statusline"] = "set"
        report["changed"] = True
    elif isinstance(sl, dict) and STATUSLINE_KEY in str(sl.get("command", "")):
        if sl.get("command") != desired_sl["command"]:
            sl["command"] = desired_sl["command"]
            report["statusline"] = "updated"
            report["changed"] = True
        else:
            report["statusline"] = "current"
    else:
        report["statusline"] = "foreign"

    if not report["changed"]:
        return report  # idempotent no-op: nothing written, no backup spam

    if dry_run:
        return report

    # Back up the existing file (only when it exists and we are about to change it).
    if settings_path.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = settings_path.with_name(f"{settings_path.name}.kb-bak-{ts}")
        shutil.copy2(settings_path, backup)
        report["backup"] = str(backup)

    # Atomic write: temp in the same dir, then os.replace.
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_name(f"{settings_path.name}.kb-tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, settings_path)
    report["wrote"] = True
    return report


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Merge KB hooks into a Claude Code settings.json.")
    ap.add_argument("--settings", required=True, help="path to settings.json")
    ap.add_argument("--kb-dir", required=True, help="KB home (e.g. ~/.kb)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    rep = merge_settings(Path(args.settings), Path(args.kb_dir), dry_run=not args.apply)
    print(json.dumps(rep, indent=2))
