# KB — Architecture

How KB is put together and the decisions that hold it up. This is an overview of
the design, not a manual.

## What it is

A knowledge base that captures engineering learnings **straight from coding
sessions and git history**, keyed to the unit of work (workspace → project →
ticket = branch → learning), and feeds them back into AI coding tools as context.
What sets it apart from flat "memory" tools: knowledge is **anchored to the SDLC**
(branch lifecycle, merge → resolved, experimental down-weight), not a flat vector
blob.

## Mental model: two faces, one config, one vault

KB has two faces that never talk to each other directly — they converge on a
single config file plus the vault.

```
   HUMAN FACE                              MODEL FACE
   manager app (a person)                  engine / kb CLI (the AI)
   - set vault + workspaces                - retrieve (inject context)
   - edit the schedule                     - sync (capture from git)
   - status / light viewer                 - stats
   - enable the integration                        |
          |                                        |
          +------------>  config (+ vault)  <-------+
                          CONVERGENCE POINT
```

- **The manager writes** config (and the host hooks, and the OS scheduler).
- **The engine reads** config and runs in the model hot-path. The manager is never
  in that hot-path — it configures and observes.
- No coupling between the two faces beyond the shared config + vault.

## Load-bearing decisions

1. **Engine ⟂ adapter.** A model/OS-agnostic core (a Python library + the `kb` CLI)
   does retrieve / sync / capture / stats. Everything else — the Claude Code hooks,
   a future Codex hook, a future MCP server, the manager app — is a thin adapter.
   The engine is the invariant.

2. **Config resolution: the hook degrades, the CLI errors.** Vault/config
   resolution is centralized in `kb_config`.
   - Hook hot-path → `resolve_vault(strict=False)`: unresolved returns `None`; the
     caller emits nothing and exits 0. It must **never** raise into the prompt flow
     (module-level resolution runs at import, before any `try/except` in `main()`).
   - CLI / setup → `resolve_vault(strict=True)`: unresolved raises `KBConfigError`
     loudly.
   - **No "best guess" fallback** — guessing the vault would silently poison
     retrieval. Degrade ≠ guess.
   - Resolution order: `KB_VAULT` env → the config's `vault` key. A vendor-neutral
     config location can be added to the chain later without breaking this one.

3. **The vault is a local git repo (no remote by default).** The vault is a git
   repo on your machine — version history, a foundation for future team sync — but
   with **no remote and no push/pull** out of the box.
   - **Remote-safety:** commit identity is just metadata; it has zero effect on
     whether anything goes remote. A branch exists remotely only after an explicit
     `push` to a configured remote. The vault repo configures **no remote**, and
     `kb sync` **only commits, never pushes**. An optional `pre-push` hook can block
     pushes outright. So nothing leaves the machine by accident.
   - **Commit identity** is set repo-local (never `--global`), pre-wiring for a
     possible team-repo future where git history doubles as a sync audit. Knowledge
     attribution is the `author` frontmatter (the session owner), independent of the
     committer.

4. **The vault is a KB-owned data dir — plain markdown, file = record.** It is a
   folder KB owns (the config `vault` path), **not** "the Obsidian folder."
   Obsidian is an *optional viewer* you may point at that folder — never required,
   never the source of truth. So `.git` belongs to the KB vault dir whether or not
   Obsidian is installed. `.gitignore` excludes `.obsidian/` (per-user viewer
   settings, not knowledge) and any embedding cache.

5. **Local-first scope.** The tool runs entirely on your machine (engine + manager
   + local vault). A hosted/networked multi-tenant service is out of scope for this
   design.

6. **Designed for a public repo.** Zero hardcoded local paths, zero secrets,
   English tooling.

7. **Two repos, never crossed (load-bearing guardrail).** There are exactly two git
   repos and they must never mix:
   - **Vault DATA repo** — your private knowledge. **No remote, ever.** A leak from
     a private/public repo is as bad as any other. Guarded by: no remote configured
     + `kb sync` never pushes + an optional `pre-push` block hook.
   - **Tool SOURCE repo** (`engine/`, `manager/`, `adapters/`, `installer/`,
     `docs/`) — the code. The **only** repo that gets a remote. Contains **no** vault
     data.

## Repo layout

```
engine/      Python library + `kb` CLI (model/OS-agnostic core)
manager/     human-facing config app (localhost web UI)
adapters/    per-host glue — claude-code/ (hooks + commands + statusline)
installer/   deploy/update: settings merge, file deploy, per-OS scheduler
docs/        this file
```
