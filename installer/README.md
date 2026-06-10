# KB installer

Topology A: this repo is the source of truth; the installer copies the engine +
adapters into the live layout (`~/.kb` — the KB home; only the Claude Code slash commands land in `~/.claude`) and
wires the Claude Code hooks. One command does both first-install and updates —
re-running is always safe. The MCP server deploys with the engine: point any
MCP host at `<home>/.kb/engine/kb.py mcp` (config snippets in the root README).

## Install / update

Clone the repo, then:

**Easiest — double-click** (installs/updates KB **and opens the manager** in your
browser to configure it):

| OS | Double-click |
|----|--------------|
| Windows | `install.cmd` |
| macOS   | `install.command` (Finder runs it in Terminal) |
| Linux   | no standard double-click — run `bash installer/install.sh --apply` |

**From a terminal** (dry-run first to see what would change):

```bash
# Windows
powershell -ExecutionPolicy Bypass -File installer\install.ps1            # dry-run
powershell -ExecutionPolicy Bypass -File installer\install.ps1 -Apply     # install / update

# macOS / Linux
bash installer/install.sh            # dry-run
bash installer/install.sh --apply    # install / update
```

Flags: daily sync time `-Time 02:30` / `--time 02:30` (default `01:00`); skip the
auto-launch of the manager with `-NoManager` / `--no-manager`.

**Re-open the manager later:** `kb manage` (the install records the clone path so
the deployed CLI finds it), or directly `python <clone>/manager/server.py`.

The bootstrap script only locates Python and installs the optional deps
(`fastembed`, `numpy`, `tiktoken` — semantic retrieval degrades to BM25 without
them). All the real work is in the OS-agnostic orchestrator `install.py`.

## What it does (each step idempotent, each re-runnable)

1. **migrate** — one-time move of a pre-0.11 install: copies the config, the
   `kb-*` state files and the version stamps from `~/.claude` into `~/.kb`, and
   retires the old deployed engine files into `~/.kb/backups/migrate-<ts>/`
   (name-exact — anything else in `hooks/`/`scripts/` is the user's and stays).
   A fresh machine skips this entirely.
2. **deploy** — copies the engine plus the adapter hook scripts FLAT into
   `~/.kb/engine/`, and the Claude Code slash commands into `~/.claude/commands/`
   (the one location Claude Code dictates). Diffs first; backs up every file it
   overwrites into `~/.kb/backups/deploy-<ts>/`; a pure CRLF↔LF difference is
   left alone. Copy-only — never deletes host files it doesn't own.
3. **settings** — merges KB's hooks into `settings.json` **additively**. Foreign
   hooks (other tools) are never touched; an entry that is recognizably OUR OWN
   but points at a previous layout gets its command repointed (timeouts and
   localized messages survive); an unparseable `settings.json` is left untouched
   and the install refuses rather than risk clobbering it. Backs up before writing.
4. **mcp** — wires the KB MCP server into every **detected** host (Codex CLI,
   Cursor, Claude Desktop, Gemini CLI, Windsurf). Same contract as settings:
   additive, a foreign/divergent `kb` entry is never overwritten (our own stale
   wiring IS repointed), malformed configs are refused untouched, every modified
   file gets a `*.kb-bak-<ts>` sibling. The recorded command uses the absolute
   Python path (GUI hosts spawn MCP servers without your shell PATH). Skip
   entirely with `--no-mcp-wire`.
5. **scheduler** — registers the daily `kb-sync` job (Windows Task Scheduler /
   macOS launchd / Linux cron), pointing at `~/.kb/engine/kb-sync.py`.
6. **version** — stamps `~/.kb/.version` with this repo's `VERSION`, and
   records the clone path in `~/.kb/.source` so `kb manage` can find the
   manager (which runs from the clone).
7. **config** — checks `~/.kb/config.json` (or the pre-0.11 `~/.claude/kb-workspaces.json`). It never fabricates a
   vault path; if the config is missing it tells you to set `vault` yourself
   (copy `config.example.json`). KB hooks degrade safely (no injection) until then.

## Status / rollback

```bash
python installer/install.py --status      # installed version, scheduler, config
python installer/install.py --rollback    # restore the most recent deploy backup
```

`--rollback` restores the files from the latest `deploy-<ts>` backup (it does not
revert the settings.json merge — that has its own timestamped `.kb-bak-*` beside
`settings.json`).

## Two repos, never crossed

This repo (the tool) is the only one with a remote. **Your vault is a separate,
local-only git repo with no remote, ever** — it is your private data and has
nothing to do with this code. The installer touches the tool, never the vault.

## Tests

```bash
python installer/settings_merge_test.py
python installer/deploy_test.py
```
