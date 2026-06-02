# KB manager

The human face of KB: a localhost web app to configure the engine without
hand-editing JSON. It sits on top of the engine + installer and reuses their
validated logic — it configures and controls, it does not deploy files.

## Run

```bash
python manager/server.py            # prints a URL and opens your browser
python manager/server.py --no-open  # just print the URL
python manager/server.py --port 7700
```

Open the printed `http://127.0.0.1:<port>/?t=<token>`. The token is per-launch.

## What you can do

- **Status** — installed version, vault (and whether it exists), embedding daemon
  health, scheduler state.
- **Vault** — set the vault path (validated: must be an existing folder).
- **Workspaces** — name → the folder holding the code repos KB watches.
- **Schedule** — the daily time kb-sync runs (registers the OS job).
- **Integration** — wire the Claude Code hooks, or mute KB instantly via the
  kill-switch without uninstalling.

Config writes go through the engine's `kb_config.write_config` (validated,
atomic, load-merge — never clobbers the load-bearing branch sets, refuses an
invalid or unparseable file). The manager never fabricates a vault path.

## Why stdlib

No web framework — just `http.server`. Zero extra dependencies for a teammate to
install: if Python runs, the manager runs, the same on Windows/macOS/Linux. A
native shell (e.g. Tauri wrapping this same UI) is a later cosmetic option, not a
separate build.

## Security

Internal localhost tool, not a public service: binds `127.0.0.1`, rejects
non-localhost `Host` headers (anti DNS-rebind), and gates every `/api/*` call with
the per-launch token. It runs while the window is open, not as a daemon.
