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
the SDLC — branch lifecycle, merge → resolved — not a flat memory blob. The engine is
host-agnostic and ships with two adapters: **Claude Code** (hooks — context is pushed into
every prompt) and **MCP** (tools — any MCP host pulls the same engine: Codex CLI, Cursor,
Claude Desktop, Windsurf, Zed...).

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

**One line.** Clones to `~/.kb/app` and runs the installer; re-run the same line to update.

```bash
# macOS / Linux / WSL / Git Bash
curl -fsSL https://raw.githubusercontent.com/junioorosa/kb/main/bootstrap.sh | bash

# Windows (PowerShell 5.1+)
irm https://raw.githubusercontent.com/junioorosa/kb/main/bootstrap.ps1 | iex
```

> Installing from a **private** clone or fork? Raw URLs won't serve it — use `gh` (it carries
> your GitHub auth): `gh repo clone <owner>/kb ~/.kb/app`, then `bash ~/.kb/app/bootstrap.sh`
> (it detects the existing clone, updates it, and runs the installer). The script also falls
> back to `gh` on its own when a plain clone of a private `KB_REPO` is rejected.

**Agent-driven.** Already in an AI coding tool? Just say: *"Read INSTALL.md and install KB for
me."* [`INSTALL.md`](INSTALL.md) is a runbook written for agents — prerequisites, install,
verification, and **repair** when an install breaks.

Already have a clone? The installer alone is enough — it is idempotent: the same command does
first-install and updates, and re-running is always safe (it diffs, backs up every file it
overwrites, and never deletes files it doesn't own).

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

That wires the Claude Code hooks, **wires the KB MCP server into every detected host**
(Codex CLI, Cursor, Claude Desktop, Gemini CLI, Windsurf — additively, backed up, skippable
with `--no-mcp-wire`), registers a daily capture job, and opens the manager so you can point
KB at your vault and code folders. Requirements: **Python 3.x** (the bootstrap also installs
the optional `fastembed`, `numpy`, `tiktoken` — see [Requirements](#requirements)).

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

| Command | Where | What it does |
|---------|-------|--------------|
| `/kb-mark [branch]` | Claude Code | Tie the session's transcript to a branch for capture (defaults to the current git branch). Flags: `--experimental`, `--done`, `--remove`. |
| `/kb-search <query>` | Claude Code | Deep manual search across the vault's three scopes. |
| `/kb-stats` | Claude Code | Token cost of the KB context injected into your prompts this session. |
| `kb mark --done\|--experimental [branch]` | any terminal | Close or down-weight a ticket from anywhere — the host-neutral half of `/kb-mark`. |
| `kb manage` | any terminal | Open the localhost manager (vault path, schedule, integration toggle). |
| `kb doctor` | any terminal | Print the resolved config + vault, or fail loudly if unresolved. |
| `kb mcp` | MCP hosts | Serve the KB over MCP stdio (configured once per host, see below). |

Capture and finalize run as a **nightly job** registered at install — retime it in the manager.

## Use it from any agent (MCP)

Hosts without prompt hooks talk to the same local engine through MCP — the model **pulls**
context instead of having it pushed. Four tools: `kb_search` (ranked notes), `kb_context`
(the same block the hook injects), `kb_read` (one note's body, sandboxed to the vault) and
`kb_mark` (tie the session to a branch for capture — see below).

**The installer wires detected hosts automatically** (Codex, Cursor, Claude Desktop, Gemini,
Windsurf — `--no-mcp-wire` to opt out; on Codex it also drops a when-to-use note into the
global `AGENTS.md`). For manual setup or any other MCP host, point it at the deployed CLI
(`<home>/.claude/hooks/kb.py mcp`):

```jsonc
// Cursor (.cursor/mcp.json) / Claude Desktop (claude_desktop_config.json)
{
  "mcpServers": {
    "kb": { "command": "python", "args": ["/home/you/.claude/hooks/kb.py", "mcp"] }
  }
}
```

```toml
# Codex CLI (~/.codex/config.toml)
[mcp_servers.kb]
command = "python"
args = ["/home/you/.claude/hooks/kb.py", "mcp"]
```

Tip for pull hosts: add a line to your agent instructions (e.g. `AGENTS.md`) such as
*"before proposing a technical solution, call `kb_search` with the task"* — models
under-call tools they're never reminded of.

What you get per adapter today: **retrieval works everywhere** (it is fully local, no LLM),
and **conversation capture works on any host that persists its session logs** — which is how
`kb_mark` pulls it off:

1. You (or the model, when you ask it to) call `kb_mark` with the branch you're working on.
2. The tool returns a **mark token** — and since the host persists tool results in its own
   session log, the token is now literally written inside that session's file.
3. The nightly sync greps the configured `transcript_stores` (default: `~/.codex/sessions`)
   for the token. The file that contains it **is** the marked session — a deterministic key,
   no host session-id needed, no guessing. `.jsonl.zst` logs are mirrored decompressed.

A session never marked still gets captured from **git alone** — same learnings, minus the
conversation hints. Ticket maintenance — closing (`--done`) or down-weighting
(`--experimental`) — works from any terminal via `kb mark`, and `kb_context` accepts your
current branch to pull the ticket's notes on demand.

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
engine/     host/OS-agnostic core: kb_config, kb (CLI), kb_retrieve, kb_mcp (MCP server), kb-sync, kb-embed, kb-embed-daemon
adapters/   per-host glue — claude-code/ (hooks + slash commands + statusline); other hosts use `kb mcp`
manager/    localhost config app (set vault, schedule, toggles)
installer/  deploy/update + per-OS scheduler
```

Tests live beside the code they cover (`*_test.py` in `engine/`, `installer/`, `adapters/`).

## License

Not yet decided — all rights reserved until a license is added.
