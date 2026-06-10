#!/usr/bin/env python
"""kb_config — shared config + path resolution for the KB engine.

Single source of truth for WHERE the KB lives, so the hooks, the CLI, the MCP
server, the sync and the installer converge instead of each hardcoding paths.

KB home (the data dir):
  1. KB_HOME env var
  2. ~/.kb                              [canonical]
  Layout: engine/ (deployed code), config.json, state/, cache/, logs/,
  backups/, .version, .source, hooks-disabled (kill-switch).

Config file:
  1. <kb home>/config.json              [canonical]
  2. ~/.claude/kb-workspaces.json       [legacy — pre-0.11 installs; read-only
     fallback so an un-migrated machine keeps working. The installer migrates.]

Vault resolution order:
  1. KB_VAULT env var
  2. the config file's "vault" key
  3. unresolved -> None

CONTRACT (load-bearing):
  - Hook hot-path calls resolve_vault(strict=False): unresolved returns None,
    the caller degrades (emits nothing) and NEVER raises into the prompt flow.
  - CLI / setup calls resolve_vault(strict=True): unresolved raises
    KBConfigError loudly. No "best guess" fallback — guessing the vault poisons
    retrieval — the one failure mode KB must never have. Degrade != guess.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class KBConfigError(Exception):
    """Raised by strict config resolution when the vault cannot be located."""


def _home() -> Path:
    return Path(os.environ.get("HOME", os.path.expanduser("~")))


def kb_home() -> Path:
    """The KB data dir: KB_HOME env -> ~/.kb. Never auto-created here; writers
    mkdir the specific subdir they need."""
    env = os.environ.get("KB_HOME")
    if env and env.strip():
        return Path(env.strip())
    return _home() / ".kb"


def state_dir() -> Path:
    return kb_home() / "state"


def cache_dir() -> Path:
    return kb_home() / "cache"


def logs_dir() -> Path:
    return kb_home() / "logs"


def legacy_workspaces_path() -> Path:
    """Pre-0.11 config location (~/.claude/kb-workspaces.json)."""
    return _home() / ".claude" / "kb-workspaces.json"


def workspaces_path() -> Path:
    """The config file in effect: <kb home>/config.json when present (or when
    nothing exists yet — new installs land there), else the legacy location if
    only it exists. Read and write resolve through the SAME path so a
    half-migrated machine can't split-brain between two configs."""
    canonical = kb_home() / "config.json"
    if canonical.exists():
        return canonical
    legacy = legacy_workspaces_path()
    if legacy.exists():
        return legacy
    return canonical


def hooks_disabled() -> bool:
    """Kill-switch: KB_HOOKS_DISABLED env, <kb home>/hooks-disabled, or the
    legacy ~/.claude/kb-hooks-disabled flag (still honored — it's a safety
    switch; never strand one someone already set)."""
    if os.environ.get("KB_HOOKS_DISABLED"):
        return True
    if (kb_home() / "hooks-disabled").exists():
        return True
    return (_home() / ".claude" / "kb-hooks-disabled").exists()


def load_config() -> dict:
    """Load the workspaces/config JSON. Missing or malformed -> {}."""
    path = workspaces_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve_vault(strict: bool = False):
    """Resolve the vault root. Returns a Path, or None when strict is False.

    Order: KB_VAULT env -> kb-workspaces.json "vault". When unresolved:
      strict=False (hook): return None  -> caller degrades, no raise.
      strict=True  (CLI) : raise KBConfigError.
    """
    env = os.environ.get("KB_VAULT")
    if env and env.strip():
        return Path(env.strip())
    vault = load_config().get("vault")
    if isinstance(vault, str) and vault.strip():
        return Path(vault.strip())
    if strict:
        raise KBConfigError(
            "KB vault not configured. Set KB_VAULT or add a \"vault\" key to "
            f"{workspaces_path()}."
        )
    return None


# --- Config writing (manager/setup surface) ----------------------------------
# The config file is what resolves the vault; a bad write poisons retrieval
# silently. So the validated, atomic writer lives in the engine (which owns
# config semantics) and is reused by the manager app — never hand-rolled JSON in
# a UI server. Cardinal rule: refuse an invalid/partial write loudly; no guessing.

# Keys the manager is allowed to set. Branch sets / since_hours / max_turns are
# load-bearing for kb-sync attribution+resolution and must survive a vault edit,
# so writes patch only provided keys (load-merge-write) and never clobber these.
MANAGED_KEYS = {
    "vault", "workspaces",
    "default_branches", "integration_branches", "production_branches",
    "since_hours", "max_turns",
}


def _is_existing_dir(p) -> bool:
    try:
        return bool(p) and Path(str(p)).is_dir()
    except Exception:
        return False


def validate_config_update(updates: dict) -> list[str]:
    """Return a list of human-readable errors for a proposed config patch.

    Empty list == valid. Used both by write_config (to refuse) and by the manager
    (to preview validity without writing). Paths must EXIST: pointing the vault or
    a workspace at a typo'd path would silently break retrieval/capture.
    """
    if not isinstance(updates, dict):
        return ["updates must be an object"]
    errors: list[str] = []

    if "vault" in updates:
        v = updates["vault"]
        if not (isinstance(v, str) and v.strip()):
            errors.append("vault must be a non-empty path string")
        elif not _is_existing_dir(v.strip()):
            errors.append(f"vault path does not exist or is not a directory: {v}")

    if "workspaces" in updates:
        ws = updates["workspaces"]
        if not isinstance(ws, list):
            errors.append("workspaces must be a list")
        else:
            for i, w in enumerate(ws):
                if not isinstance(w, dict):
                    errors.append(f"workspaces[{i}] must be an object")
                    continue
                name, path = w.get("name"), w.get("path")
                if not (isinstance(name, str) and name.strip()):
                    errors.append(f"workspaces[{i}].name must be a non-empty string")
                if not (isinstance(path, str) and path.strip()):
                    errors.append(f"workspaces[{i}].path must be a non-empty string")
                elif not _is_existing_dir(path.strip()):
                    errors.append(f"workspaces[{i}].path does not exist: {path}")

    for key in ("default_branches", "integration_branches", "production_branches"):
        if key in updates:
            val = updates[key]
            if not (isinstance(val, list) and val and all(isinstance(x, str) and x.strip() for x in val)):
                errors.append(f"{key} must be a non-empty list of non-empty strings")

    for key in ("since_hours", "max_turns"):
        if key in updates:
            val = updates[key]
            if not (isinstance(val, int) and not isinstance(val, bool) and val > 0):
                errors.append(f"{key} must be a positive integer")

    unknown = set(updates) - MANAGED_KEYS
    if unknown:
        errors.append(f"unknown config keys: {sorted(unknown)}")
    return errors


def _load_config_for_write() -> dict:
    """Strict load for the write path: a present-but-malformed file is REFUSED.

    load_config() degrades malformed -> {} (right for the read hot-path), but on
    write that would silently drop the load-bearing keys. So here we raise instead
    of overwriting a config we could not parse.
    """
    path = workspaces_path()
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise KBConfigError(
            f"{path} exists but is not valid JSON ({e}); refusing to overwrite."
        ) from e
    if not isinstance(data, dict):
        raise KBConfigError(f"{path} is valid JSON but not an object; refusing to overwrite.")
    return data


def write_config(updates: dict) -> dict:
    """Validate, then load-merge-write the managed keys. Returns the merged config.

    Preserves every existing key not in `updates`. Atomic (temp + os.replace).
    Raises KBConfigError on invalid input or an unparseable existing file rather
    than writing a half-valid config.
    """
    errors = validate_config_update(updates)
    if errors:
        raise KBConfigError("invalid config update: " + "; ".join(errors))
    cfg = _load_config_for_write()
    cfg.update(updates)  # patch only the provided keys
    path = workspaces_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".kb-tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return cfg
