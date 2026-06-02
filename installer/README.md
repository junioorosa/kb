# KB installer

Topology A: this repo is the source of truth; the installer copies the engine +
Claude Code adapter into your host (`~/.claude`) and wires the host up. One
command does both first-install and updates — re-running is always safe.

## Install / update

```bash
# Windows
powershell -ExecutionPolicy Bypass -File installer\install.ps1            # dry-run (shows what would change)
powershell -ExecutionPolicy Bypass -File installer\install.ps1 -Apply     # install / update

# macOS / Linux
bash installer/install.sh            # dry-run
bash installer/install.sh --apply    # install / update
```

Pick the daily sync time with `-Time 02:30` (PowerShell) / `--time 02:30` (bash).
Default is `01:00`.

The bootstrap script only locates Python and installs the optional deps
(`fastembed`, `numpy`, `tiktoken` — semantic retrieval degrades to BM25 without
them). All the real work is in the OS-agnostic orchestrator `install.py`.

## What it does (each step idempotent, each re-runnable)

1. **deploy** — copies engine (`hooks/` + `scripts/`) and adapter (`hooks/` +
   `commands/`) into `~/.claude`. Diffs first; backs up every file it overwrites
   into `~/.claude/.kb-backups/deploy-<ts>/`; a pure CRLF↔LF difference is left
   alone. Copy-only — never deletes host files it doesn't own.
2. **settings** — merges KB's hooks into `settings.json` **additively**. Foreign
   hooks (other tools) are never touched; an unparseable `settings.json` is left
   untouched and the install refuses rather than risk clobbering it. Backs up
   before writing.
3. **scheduler** — registers the daily `kb-sync` job (Windows Task Scheduler /
   macOS launchd / Linux cron).
4. **version** — stamps `~/.claude/.kb-version` with this repo's `VERSION`.
5. **config** — checks `~/.claude/kb-workspaces.json`. It never fabricates a
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
