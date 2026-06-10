---
description: Mark the current session with a branch in the KB sidecar, and close or down-weight tickets manually
allowed-tools: Bash
argument-hint: <free branch, e.g. feat/my-feature or experiment> | --experimental | --done | --remove
---

# /kb-mark

Marks the current session with a branch. Run with **no argument** and it defaults to the **current git branch** of the folder you're in; pass a branch to override. Marking stays manual — `/kb-mark` is the sole writer of the session↔branch sidecar, and there is no passive SessionStart auto-marking. The sync reads the sidecar to locate the branch's transcripts without grepping the JSONL.

The branch name is free-form and is the KB **match key**. A `<type>/` prefix (e.g. `feat/`, `fix/`, or anything else) groups the folder; with no `/`, the ticket sits directly under the project. No numeric id is required.

## Usage

```
/kb-mark                         # default: the current git branch of your folder
/kb-mark feat/my-feature         # explicit branch, groups under the feat/ folder
/kb-mark fix/login-timeout       # groups under the fix/ folder
/kb-mark experiment-x            # no "/" -> directly under the project
/kb-mark --experimental [branch] # mark status=experimental (retrieval down-weight)
/kb-mark --done [branch]         # close the ticket (status=resolved on the next sync)
/kb-mark --remove                # remove the session's mark
```

With no `<branch>`, `--experimental` and `--done` also fall back to the current git branch when the session isn't already marked.

## When to use it

Mark a session whenever you want the KB to associate it with a branch — the sync only enriches capture from the transcripts of sessions you've explicitly marked. A session left unmarked is still captured from git, just without its conversation context.

- Starting work you want the KB to capture under a specific branch
- Switched branch mid-session via `git checkout` and want the new branch associated
- Working on code outside any repo, or forcing a branch other than the checked-out one (e.g. a cross-project refactor)

If a KB folder with that name already exists, the command **warns** (does not block): the sync will update the existing folder instead of creating a new one. Rename the branch if this is genuinely new, distinct work.

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

> The `kb-mark-intercept` hook handles `/kb-mark` directly in UserPromptSubmit (zero token cost, deterministic `session_id` from the payload) and blocks the prompt before the LLM. The bash below runs **only as a fallback when KB hooks are disabled**.

Received argument: `$ARGUMENTS`

Run the following bash:

```bash
branch="$ARGUMENTS"
if [ -z "$branch" ]; then
  echo "ERROR: branch is required. Usage: /kb-mark feat/my-feature  |  /kb-mark experiment  |  /kb-mark --done  |  /kb-mark --remove"
  exit 1
fi

# The kb-mark-intercept hook (UserPromptSubmit) is the supported path: it carries
# the session_id in its payload and writes the session-keyed sidecar deterministically.
# If we reached this bash, that hook is disabled — and here no session_id is available.
# Guessing the sidecar (e.g. the most recent one across all sessions) could mark the
# WRONG session, so refuse rather than guess.
echo "ERROR: /kb-mark requires the kb-mark-intercept hook, which appears disabled"
echo "       (KB_HOOKS_DISABLED=1, or ~/.kb/hooks-disabled is present)."
echo "       Re-enable KB hooks, then re-run: /kb-mark ${branch}"
exit 1
```

After running, report to the user what was marked (or, in the fallback case above, that KB hooks must be re-enabled).
