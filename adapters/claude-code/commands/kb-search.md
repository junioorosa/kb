---
description: Semantic search across the whole Obsidian KB — tickets and learnings relevant to a query
allowed-tools: mcp__obsidian-vault__list_directory, mcp__obsidian-vault__read_file, mcp__obsidian-vault__search_files
argument-hint: <natural-language query>
---

# /kb-search

Cross-ticket search across the Obsidian KB to find learnings, patterns, business rules, or prior tickets relevant to the user's query.

## KB structure — 3 learning scopes

| Scope | Path | Trait |
|---|---|---|
| **workspace** | `<vault>/<workspace>/Learnings/` | Cross-project patterns within a workspace |
| **project** | `<vault>/<workspace>/<project>/Learnings/` | Reusable within the project |
| **ticket** | `<vault>/<workspace>/<project>/[<type>/]<slug>/Learnings/` | Specific to one ticket/branch |

Branch names are free-form: a `<type>/` prefix (e.g. `feat/`, `fix/`) groups the folder; with no `/`, the ticket sits directly under the project. Each ticket folder holds an `_index.md` plus a `Learnings/` directory.

## Query

$ARGUMENTS

## Strategy

**Semantic**, not lexical. Don't rely on exact word match — interpret the query's theme and search conceptually.

Relevance ranking: a **workspace** or **project** learning that matches the theme is usually more useful than a ticket-level one (reusable knowledge density). But prior tickets still count because they carry the decision's context.

## Steps

1. **Map the vault**:
   - `list_directory` at the root to discover workspaces.
   - `list_directory` in `<workspace>/Learnings/` → workspace-level learnings.
   - For each project:
     - `list_directory` in `<workspace>/<project>/Learnings/` → project-level learnings.
     - `list_directory` in `<workspace>/<project>/` → ticket folders (and `<type>/` groups).
     - For grouped types, list the ticket folders inside each `<type>/`.

2. **Collect cheap metadata first** (coarse filtering):
   - Workspace learnings: list file names in `<workspace>/Learnings/`.
   - Project learnings (per project): list file names in `<workspace>/<project>/Learnings/`.
   - For each ticket, read **only the frontmatter** of `_index.md` (`module`, `tags`, `title`, `apparent_problem`, `actual_solution`, `status`).
   - Ticket-level learnings: list file names in each `<ticket>/Learnings/`.

3. **Semantic filtering in three layers**:

   **Layer 1 — workspace learnings** (always check; the most reusable scope):
   - Match the query against the names (kebab-case carries the theme).
   - Select candidates.

   **Layer 2 — project learnings**:
   - Filter by relevant project first (if the query names one explicitly or the context points to one). If ambiguous, consider all.
   - Match the query against the names.
   - Select candidates.

   **Layer 3 — tickets + ticket-level learnings**:
   - Match the query against `title + module + tags + apparent_problem + actual_solution` of the tickets.
   - Match the query against ticket-level learning names of the tickets that passed filtering.
   - Select candidates.

4. **Read the body of the selected candidates** (`read_file`) to confirm relevance and extract useful excerpts.

5. **Present a ranked result**, separated by scope:

   ```
   ## Workspace learnings
   - [[<workspace>/Learnings/<name>]]
     <1-2 line synthesis>
     Why it's relevant: <explicit connection>

   ## Project learnings (<project>)
   - [[<workspace>/<project>/Learnings/<name>]]
     <synthesis>
     Why it's relevant: <connection>

   ## Ticket learnings
   - [[<workspace>/<project>/<type>/<slug>/Learnings/<name>]]
     <synthesis>
     Why it's relevant: <connection>

   ## Relevant tickets
   - [<title>] (status: <status>) — <workspace>/<project>/<type>/<slug>/_index.md
     Solution: <actual_solution or "still open">
     Why it's relevant: <connection>
   ```

6. **If nothing relevant**: say "nothing in the KB about <interpreted topic>". Don't invent weak connections.

## Constraints

- Total limit: top 5 learnings (across the 3 scopes) + top 5 tickets. More than that becomes noise.
- Preferred distribution: at least 1 workspace and 1 project learning if relevant candidates exist — diversity helps more than 5 ticket-level learnings from the same ticket.
- Don't read a body unless it passed step 3's filtering — saves tokens.
- Always justify **why** each result is relevant. "It showed up in the search" is not a justification — state the thematic connection.
- If the query is ambiguous (e.g. "validation"), ask for refinement before searching.
- Status `discarded` / `experimental` on tickets → mention but with a warning ("(solution was discarded)" / "(branch is experimental — may not ship)").
