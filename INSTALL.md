# KB — install runbook

Written for **AI coding agents** (humans welcome). If a user says *"read
INSTALL.md and install KB for me"* — or an install broke and they want it
fixed — follow this top to bottom. Every step is idempotent: re-running is
always safe, nothing here deletes user data.

## 0. Prerequisites

Check, and fix only what's missing:

| Need | Check | Fix |
|------|-------|-----|
| git | `git --version` | install via the OS package manager (`winget install Git.Git` / `brew install git` / `apt install git`) |
| Python 3.10+ | `python --version` (or `python3`) | `winget install Python.Python.3.12` / `brew install python` / `apt install python3` |
| Git Bash (Windows only) | `Test-Path "C:\Program Files\Git\bin\bash.exe"` | comes with Git for Windows |

> **Windows trap:** `bash` on PATH usually resolves to the WSL launcher
> (`C:\Windows\System32\bash.exe`), which silently breaks the hooks. KB's own
> scripts avoid it, but never invoke the hooks through bare `bash` yourself —
> use the full Git Bash path.

## 1. Get the repo

```bash
# fresh machine
git clone https://github.com/junioorosa/kb.git ~/.kb/app

# already cloned (any path works — ~/.kb/app is just the convention)
git -C ~/.kb/app pull --ff-only
```

`bootstrap.sh` / `bootstrap.ps1` at the repo root do this clone-or-update plus
step 2 in one go — prefer them when starting from nothing.

## 2. Install / update

```bash
# macOS / Linux / Git Bash
bash <repo>/installer/install.sh --apply

# Windows PowerShell
powershell -ExecutionPolicy Bypass -File <repo>\installer\install.ps1 -Apply
```

What it does (all idempotent, every overwrite backed up): deploys the engine
into `~/.claude`, merges the Claude Code hooks into `settings.json`
(additively — foreign hooks untouched), wires the KB MCP server into every
detected host (Codex / Cursor / Claude Desktop / Gemini / Windsurf; skip with
`--no-mcp-wire`), registers the nightly sync job, stamps the version.

Run without `--apply` first if the user wants to review: it prints the full
diff and writes nothing.

## 3. Configure (first install only)

KB **never guesses** the vault path. If `~/.claude/kb-workspaces.json` is
missing, the installer says so — then either:

- open the manager (`python <repo>/manager/server.py` or the "KB Manager"
  shortcut) and set the vault + workspaces in the UI, or
- copy `<repo>/config.example.json` to `~/.claude/kb-workspaces.json` and set
  `vault` (an existing folder you create for it, e.g. `~/.kb/vault` after
  `git init`) and `workspaces` (folders holding the user's code repos).

Until the vault is set, hooks stay silent by design — nothing breaks.

## 4. Verify

```bash
python ~/.claude/hooks/kb.py doctor          # vault + config resolve loudly
python <repo>/installer/install.py --status  # version, scheduler, config
```

Hook smoke test (should print a `<vault-context>` block when the vault has
content; silence is CORRECT for an empty/unset vault):

```bash
echo '{"prompt": "test retrieval", "session_id": "smoke"}' | "C:\Program Files\Git\bin\bash.exe" ~/.claude/hooks/kb-context.sh
```

MCP smoke test (should answer one JSON line with `serverInfo`):

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' | python ~/.claude/hooks/kb.py mcp
```

## 5. When something is broken

| Symptom | Cause | Fix |
|---------|-------|-----|
| Hooks never fire, no `<vault-context>` ever | vault not configured | step 3; confirm with `kb doctor` |
| Hook exits instantly, silent | invoked via WSL bash (`System32\bash.exe`) | use the Git Bash full path (see step 0) |
| installer says settings.json is invalid | the file was hand-edited into broken JSON | fix the JSON by hand (the installer REFUSES to touch a malformed file — that's deliberate); its backups live beside it as `settings.json.kb-bak-*` |
| Retrieval works but feels lexical-only | `fastembed`/`numpy` missing or daemon down | `pip install fastembed numpy`; the daemon auto-spawns on the next prompt |
| An MCP host doesn't list the kb server | host installed after KB, or wiring opted out | re-run step 2 (wire is idempotent), then restart the host app |
| A deploy went wrong | — | `python <repo>/installer/install.py --rollback` restores the latest backup |
| Update wanted | — | re-run step 1 (pull) + step 2; or `install.py --update` (fast-forward + redeploy) |

Backups: every deploy keeps a restorable copy under
`~/.claude/.kb-backups/deploy-<timestamp>/`; every touched host config gets a
sibling `*.kb-bak-<timestamp>` file. Nothing the installer does is one-way.

## Rules for agents

- Never edit `~/.claude/settings.json` or another host's MCP config by hand
  when the installer can do it — it backs up, merges additively, and refuses
  malformed files. Hand-edits are how installs break.
- Never invent a vault path. If the user has no vault, ask where they want it
  (or offer `~/.kb/vault`), create it explicitly, `git init` it, then set it
  in the config.
- The vault is the user's private data: local git, **no remote**, never push
  it anywhere.
