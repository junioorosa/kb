---
description: Mark the current session with a branch in the KB sidecar (override the auto-detect) and close or down-weight tickets manually
allowed-tools: Bash
argument-hint: <free branch, e.g. feat/my-feature or experiment> | --experimental | --done | --remove
---

# /kb-mark

Updates the current session's sidecar with the given branch. Schedulers use the sidecar to locate the branch's transcripts without grepping the JSONL.

The branch name is free-form and is the KB **match key**. A `<type>/` prefix (e.g. `feat/`, `fix/`, or anything else) groups the folder; with no `/`, the ticket sits directly under the project. No numeric id is required.

## Usage

```
/kb-mark feat/my-feature         # groups under the feat/ folder
/kb-mark fix/login-timeout       # groups under the fix/ folder
/kb-mark experiment-x            # no "/" -> directly under the project
/kb-mark --experimental [branch] # mark status=experimental (retrieval down-weight)
/kb-mark --done [branch]         # close the ticket (status=resolved on the next sync)
/kb-mark --remove                # remove the session's mark
```

Marks the session with the branch. Overrides the auto-detect (which uses `git branch --show-current` in the `cwd` at startup).

If a KB folder with that name already exists, the command **warns** (does not block): the sync will update the existing folder instead of creating a new one. Rename the branch if this is genuinely new, distinct work.

## When to use it

- Switched branch mid-session via `git checkout` and want the KB to associate the session with the new branch
- Working on code outside any repo (auto-detect returned empty)
- Want to force association with a branch other than the detected one (e.g. a cross-project refactor)

## Close a ticket (`--done`)

The sync finalizes tickets automatically when it detects the branch was merged into an integration branch (merge-commit/ff by ancestry, rebase/squash by patch-id) **or** when the branch is gone from every clone (delete-on-merge).

Use `/kb-mark --done` when automatic finalization doesn't fire:
- merge with **"delete branch on merge" turned off** (the branch stays alive, so the sync never sees it removed)
- squash of several commits **with conflict resolution** (the patch-id diverges and won't match)

`--done` sets `manual_done` in the sidecar; the next sync finalizes the ticket (status=resolved) from the knowledge already accumulated.

## Mark as experimental (`--experimental`)

For a branch that may not go anywhere. Retrieval applies a down-weight (weight 0.4) to the ticket, so it stops polluting the mid/high tiers of tangential prompts — but it still shows up if you ask about its exact topic. The branch is **optional**: with no argument it marks the session's current branch; pass one explicitly to mark a different branch (handy when a single session touched real work on one branch and a secondary, experimental idea on another).

If the `_index.md` already exists, the status is written on the spot; otherwise a flag is stored in the sidecar and the next sync forces it at capture. Fully reversible: if the branch merges, the finalize step sets `resolved` and the weight returns to 1.0 (or use `--done` to force it).

## Execution

> Note: the `kb-mark-intercept` hook handles `/kb-mark` directly in UserPromptSubmit (zero token cost) and blocks the prompt before the LLM. The bash below is a fallback for when the hook is disabled.

Received argument: `$ARGUMENTS`

Run the following bash:

```bash
branch="$ARGUMENTS"
if [ -z "$branch" ]; then
  echo "ERROR: branch is required. Usage: /kb-mark feat/my-feature  |  /kb-mark experiment  |  /kb-mark --done  |  /kb-mark --remove"
  exit 1
fi

state_dir="$HOME/.claude/state"
mkdir -p "$state_dir"

# Pick the most recent sidecar — assume the current session
latest=$(ls -t "$state_dir"/kb-session-branch-*.json 2>/dev/null | head -1)

if [ -z "$latest" ]; then
  echo "ERROR: no sidecar found — the SessionStart hook didn't run."
  echo "       Check that kb-session-branch.sh is registered in settings.json."
  exit 1
fi

# Convert Git Bash path (/c/...) to Windows (C:\...) — Python on Windows can't mount /c/
if command -v cygpath >/dev/null 2>&1; then
  latest_win=$(cygpath -w "$latest")
else
  latest_win="$latest"
fi

# Update branch + manual_override=true
python - <<PYEOF
import json, sys, time
p = r'$latest_win'
try:
    with open(p, 'r', encoding='utf-8') as f:
        data = json.load(f)
except Exception as e:
    print(f"ERROR: failed to read sidecar: {e}")
    sys.exit(1)
data['branch'] = '$branch'
data['manual_override'] = True
data['marked_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
with open(p, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False)
print(f"OK: session={data.get('session_id','?')[:12]}... branch={data['branch']}")
print(f"     sidecar: {p}")
PYEOF
```

After running, report to the user what was marked.
