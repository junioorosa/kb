# KB — Knowledge Base for AI coding agents

KB captures engineering learnings **straight from your coding sessions and git
history**, keyed to the unit of work (workspace → project → ticket=branch →
learning), and feeds them back into AI coding tools (Claude Code today; Codex,
MCP clients later) as on-prompt context. Knowledge is anchored to the SDLC —
branch lifecycle, merge → resolved — not a flat vector blob.

> Status: early. Validating against a real vault before packaging an installer.

## Two repos, two destinations (hard rule)

- **This repo (tool SOURCE)** — the engine + adapters. Public-bound, no user data.
- **Your vault (DATA)** — your knowledge, a separate **local-only** git repo with
  **no remote, ever**. It never lives in this repo. When someone installs KB, their
  vault is their own private business and has nothing to do with this code.

## Layout

```
engine/     model/OS-agnostic core: kb_config, kb (CLI), kb_retrieve, kb-sync, kb-embed, kb-embed-daemon
adapters/   per-host glue — claude-code/ (hooks + slash commands + statusline)
installer/  deploy/update (copies engine+adapters into the host, wires hooks) — WIP
tests/      retrieval test rig + PT eval
docs/       KB-ARCHITECTURE.md (decisions + phase map)
```

## Configure

Copy `config.example.json` and point it at your vault + code repos. The vault
location is resolved by `kb_config` (env `KB_VAULT` or the config's `vault` key) —
never hardcoded.

## Requirements

Python 3.x. Optional: `fastembed` + `numpy` (semantic retrieval; degrades to BM25
without them), `tiktoken` (exact token stats; falls back to an estimate).

## License

Not yet decided — all rights reserved until a license is added.
