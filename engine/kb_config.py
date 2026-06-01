#!/usr/bin/env python
"""kb_config — shared config resolution for the KB engine (Phase 0 boundary).

First brick of the model-facing engine. Resolves the vault location (and, later,
full config) from a single source so the hook, the CLI, and kb-sync converge
instead of each hardcoding paths.

Resolution order (current):
  1. KB_VAULT env var
  2. ~/.claude/kb-workspaces.json  ("vault" key)   [legacy/transitional location]
  3. unresolved -> None

CONTRACT (load-bearing — see KB-ARCHITECTURE.md):
  - Hook hot-path calls resolve_vault(strict=False): unresolved returns None,
    the caller degrades (emits nothing) and NEVER raises into the prompt flow.
  - CLI / setup calls resolve_vault(strict=True): unresolved raises
    KBConfigError loudly. No "best guess" fallback — guessing the vault poisons
    retrieval (see the "ponto sensível" rule). Degrade != guess.

The vendor-neutral target location (~/.kb/config.json) is intentionally NOT read
yet — its schema is undesigned. Adding it to the chain later is non-breaking.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class KBConfigError(Exception):
    """Raised by strict config resolution when the vault cannot be located."""


def _home() -> Path:
    return Path(os.environ.get("HOME", os.path.expanduser("~")))


def workspaces_path() -> Path:
    return _home() / ".claude" / "kb-workspaces.json"


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
