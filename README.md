# KB — a knowledge base for AI coding agents

**Your agent forgets every session. KB remembers — and feeds it back on the next prompt.**

![local-first](https://img.shields.io/badge/local--first-yes-green)
![telemetry](https://img.shields.io/badge/telemetry-none-green)
![python](https://img.shields.io/badge/python-3.x-blue)

[Install](#install) • [How it works](#how-it-works) • [Commands](#commands) • [Configuration](#configuration) • [Your data stays local](#your-data-stays-local)

---

KB captures engineering learnings **straight from your coding sessions and git history**,
keyed to the unit of work (workspace → project → branch → learning), and injects the
relevant ones back into your AI coding tool as on-prompt context. Knowledge is anchored to
the SDLC — branch lifecycle, merge → resolved — not a flat memory blob. Claude Code works
today; Codex and MCP clients are thin adapters on the same engine.

## Before / after

**Without KB** — new session on a bug you half-remember solving last month:

> **You:** the JWT expiry check is off by one again
>
> **Agent:** *(no memory of the earlier fix — re-derives from scratch, asks you the same questions)*

**With KB** — same prompt, but a hook injected what you already learned, before the agent answered:

```
<vault-context>
KB cross-ticket (branch=fix/login-timeout):

## Top matches (tier=high, via hybrid embedding (cosine+BM25)):
- [[MyWorkspace/auth-service/fix/session-expiry/Learnings/jwt-clock-skew.md]] (score=0.86)
  — boundary compared with < instead of <=; also missing clock-skew tolerance

### Body excerpt — MyWorkspace/auth-service/fix/session-expiry/Learnings/jwt-clock-skew.md:
Token read as expired one second early: exp compared with `<` instead of `<=`.
Root cause also missing clock-skew tolerance between auth node and API node.
Fix: compare `now <= exp + SKEW` (SKEW=60s).
...
</vault-context>
```

> **Agent:** *(answers with your own past fix in hand)*

The learning came from your own session — captured automatically the night you fixed it.

## Install

Clone the repo, then run the installer. It is idempotent: the same command does first-install
and updates, and re-running is always safe (it diffs, backs up every file it overwrites, and
never deletes files it doesn't own).

**Easiest — double-click** (installs/updates KB **and** opens the manager to configure it):

| OS | Double-click |
|----|--------------|
| Windows | `install.cmd` |
| macOS   | `install.command` |
| Linux   | no standard double-click — run `bash installer/install.sh --apply` |

**From a terminal** (run without `--apply` first for a dry-run that only reports what would change):

```bash
# Windows
powershell -ExecutionPolicy Bypass -File installer\install.ps1 -Apply

# macOS / Linux
bash installer/install.sh --apply
```

That wires the Claude Code hooks, registers a daily capture job, and opens the manager so you
can point KB at your vault and code folders. Requirements: **Python 3.x** (the bootstrap also
installs the optional `fastembed`, `numpy`, `tiktoken` — see [Requirements](#requirements)).

Flags (daily time, skip the manager auto-launch), status, and rollback are in
[`installer/README.md`](installer/README.md).

## What you get

| Capability | What it does |
|------------|--------------|
| **Auto-capture** | A nightly sync reads your authored commits **and** the marked session transcript, and distills them into learnings — one markdown file per insight, keyed to the branch. |
| **On-prompt retrieval** | A hook injects the most relevant past learnings into your agent's context *before* it answers — hybrid semantic + lexical search (local embeddings + BM25), tiered high/mid/low. No API call in the hot path. |
| **SDLC-anchored** | Knowledge is tied to the unit of work (workspace → project → branch → learning). Merge a branch to production and its ticket auto-resolves; mark a dead end `--experimental` and it's down-weighted. |
| **Local-first** | Engine, vault, and manager all run on your machine. The vault is a local-only git repo with no remote. No telemetry. |
| **Plug-and-play** | One installer wires the hooks and the daily job; a localhost manager app configures everything without hand-editing JSON. |
| **Degrades gracefully** | No `fastembed`? Retrieval falls back to BM25. No `tiktoken`? Token stats estimate. The embedding daemon down? BM25 fallback. Nothing hard-fails into your prompt. |

## How it works

1. **Mark your branch.** `/kb-mark` (defaults to the current git branch) tells KB which session to capture under which branch.
2. **Just work.** Code and talk to your agent as usual.
3. **Nightly sync captures.** The daily job reads your authored, non-merge commits plus the marked transcript and writes the learnings into the vault.
4. **Next session, retrieval injects.** When you prompt your agent on related work, the hook adds a `<vault-context>` block with the matching past learnings — no action needed.
5. **Merge resolves.** When the branch merges to a production branch, the ticket flips to `resolved` automatically (or `/kb-mark --done` when auto-detection can't fire).

## Commands

| Command | What it does |
|---------|--------------|
| `/kb-mark [branch]` | Mark the session's branch for capture (defaults to the current git branch). Flags: `--experimental`, `--done`, `--remove`. |
| `/kb-search <query>` | Deep manual search across the vault's three scopes. |
| `/kb-stats` | Token cost of the KB context injected into your prompts this session. |
| `kb manage` | Open the localhost manager (vault path, schedule, integration toggle). |
| `kb doctor` | Print the resolved config + vault, or fail loudly if unresolved. |

Capture and finalize run as a **nightly job** registered at install — retime it in the manager.

## Configuration

Point KB at your vault and your code folders. Two ways:

- **Manager (recommended):** `kb manage` opens a localhost web UI — set the vault path, name your
  workspaces (a workspace is a folder holding code repos KB watches), set the daily time, toggle the
  integration. See [`manager/README.md`](manager/README.md).
- **By hand:** copy [`config.example.json`](config.example.json) to your config and set `vault` +
  `workspaces`. The vault is resolved by `kb_config` from `KB_VAULT` (env) or the config's `vault`
  key — never hardcoded, never guessed.

## Your data stays local

KB is built so your knowledge never leaves your machine by accident:

- **The vault is a separate, local-only git repo.** No remote is configured, and the sync **only
  ever commits locally — it never pushes.** An optional `pre-push` hook can hard-block pushes.
- **Two repos, never crossed.** This repo (the tool) is the only one with a remote and holds **no**
  vault data. Your vault (your knowledge) never lives here and never gets a remote unless you
  deliberately connect a private one you own.
- **Everything runs locally** — engine, the localhost manager (binds `127.0.0.1`, per-launch token),
  and the nightly job. No telemetry, no hosted service.

## Requirements

Python 3.x. Optional, installed by the bootstrap and all degrade gracefully if absent:

- `fastembed` + `numpy` — semantic retrieval (falls back to BM25 lexical search).
- `tiktoken` — exact token stats (falls back to an estimate).

## Layout

```
engine/     model/OS-agnostic core: kb_config, kb (CLI), kb_retrieve, kb-sync, kb-embed, kb-embed-daemon
adapters/   per-host glue — claude-code/ (hooks + slash commands + statusline)
manager/    localhost config app (set vault, schedule, toggles)
installer/  deploy/update + per-OS scheduler
```

Tests live beside the code they cover (`*_test.py` in `engine/`, `installer/`, `adapters/`).

## License

Not yet decided — all rights reserved until a license is added.
