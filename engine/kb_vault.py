#!/usr/bin/env python3
"""Lightweight, read-only reader over the KB vault — the manager's window into
"what is being learned" without opening an external editor.

Deliberately stdlib-only and decoupled from the retrieval engine (no embeddings /
numpy): the manager imports this, not the heavy stack. It NEVER writes the vault —
the vault is the knowledge source of truth; the manager only reads.

What it exposes:
  - overview(vault)        : totals, by-status, by-project, tag histogram, a growth
                             series built from TICKET authored dates (see below).
  - list_learnings(vault)  : filterable/searchable list of learning records.
  - read_item(vault, rel)  : one learning/ticket as frontmatter + safe rendered HTML.

Dates: learnings carry no date of their own and the vault was copied once (so file
mtime and the first git commit are both the copy date — useless). The only reliable
"when" is the sibling ticket's authored frontmatter (resolved > last_update > opened).
A learning with no governing ticket (project/workspace scope) is simply dateless and
excluded from the time series rather than dated wrongly.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

# Frontmatter keys that legacy (PT) tickets used before the EN schema.
_LEGACY = {"title": "título", "module": "módulo"}
_SKIP_DIRS = {".git", ".obsidian", ".trash", "node_modules", "__pycache__"}


# --- frontmatter + scope (ported from the retrieval engine, kept in sync) -----

def parse_frontmatter(content: str) -> dict:
    """Flat YAML-subset frontmatter parser (keys, scalars, and `- ` lists). Nested
    blocks (e.g. `metadata:`) are intentionally ignored — scope comes from the path."""
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end < 0:
        return {}
    block = content[3:end]
    fm: dict = {}
    current_key = None
    in_list = False
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip():
            in_list = False
            continue
        if in_list and re.match(r"^\s*-\s+", line):
            val = re.sub(r"^\s*-\s+", "", line).strip().strip('"').strip("'")
            fm.setdefault(current_key, []).append(val)
            continue
        m = re.match(r"^([A-Za-zÀ-ſ_][\wÀ-ſ]*):\s*(.*)$", line)
        if not m:
            in_list = False
            continue
        key, value = m.group(1), m.group(2).strip()
        if value == "":
            fm[key] = []
            current_key = key
            in_list = True
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            fm[key] = [v.strip().strip('"').strip("'") for v in inner.split(",") if v.strip()]
            in_list = False
        else:
            fm[key] = value.strip('"').strip("'")
            in_list = False
    return fm


def fm_get(fm: dict, key: str) -> str:
    """Frontmatter value with EN->legacy-PT fallback, never None."""
    v = fm.get(key) or fm.get(_LEGACY.get(key, ""), "")
    return v if isinstance(v, str) else (v or "")


def classify_scope(rel_path: str):
    """(scope, project) from the vault-relative path. project is None for workspace.
       <ws>/Learnings/x.md                      -> workspace
       <ws>/<proj>/Learnings/x.md               -> project
       <ws>/<proj>/.../<slug>/Learnings/x.md    -> ticket
       <ws>/<proj>/.../<slug>/_index.md         -> index
    """
    parts = rel_path.split("/")
    if len(parts) < 3 or not parts[-1].endswith(".md"):
        return None, None
    if parts[1] == "Learnings":
        return "workspace", None
    if len(parts) == 4 and parts[2] == "Learnings":
        return "project", parts[1]
    if parts[-1] == "_index.md" and len(parts) >= 4:
        return "index", parts[1]
    if "Learnings" in parts[2:-1]:
        return "ticket", parts[1]
    return None, None


def ticket_dir_of(rel_path: str):
    """The folder holding the _index.md that governs this file, or None."""
    if rel_path.endswith("/_index.md"):
        return rel_path[: -len("/_index.md")]
    i = rel_path.find("/Learnings/")
    return rel_path[:i] if i >= 0 else None


def workspace_of(rel_path: str):
    parts = rel_path.split("/")
    return parts[0] if len(parts) >= 2 else None


# --- walking ------------------------------------------------------------------

def _iter_md(vault: Path):
    """Yield vault-relative posix paths for every .md, skipping VCS/cache dirs."""
    vault = Path(vault)
    for p in vault.rglob("*.md"):
        if any(part in _SKIP_DIRS for part in p.relative_to(vault).parts[:-1]):
            continue
        yield p.relative_to(vault).as_posix()


def _ticket_date(vault: Path, rel_path: str):
    """Authored date governing a file: sibling ticket's resolved > last_update > opened.
    None when there's no governing _index.md (project/workspace-scope learnings)."""
    td = ticket_dir_of(rel_path)
    if not td:
        return None
    idx = Path(vault) / td / "_index.md"
    if not idx.exists():
        return None
    fm = parse_frontmatter(idx.read_text(encoding="utf-8", errors="replace"))
    for k in ("resolved", "last_update", "opened"):
        v = fm.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _learning_record(vault: Path, rel_path: str, with_date: bool = True) -> dict:
    fm = parse_frontmatter((Path(vault) / rel_path).read_text(encoding="utf-8", errors="replace"))
    scope, project = classify_scope(rel_path)
    tags = fm.get("tags") if isinstance(fm.get("tags"), list) else []
    rec = {
        "rel_path": rel_path,
        "name": fm_get(fm, "name") or Path(rel_path).stem,
        "description": fm_get(fm, "description"),
        "scope": fm_get(fm, "scope") or scope or "",
        "project": project or "",
        "workspace": workspace_of(rel_path) or "",
        "tags": tags,
        "ticket_origin": fm_get(fm, "ticket_origin"),
        "ticket_dir": ticket_dir_of(rel_path) or "",
    }
    if with_date:
        rec["date"] = _ticket_date(vault, rel_path)
    return rec


# --- public API ---------------------------------------------------------------

def list_learnings(vault, project=None, scope=None, tag=None, q=None, limit=500) -> list:
    """Learning records, newest-ticket-date first (dateless last). Filters are AND-ed;
    `q` matches name/description/tags case-insensitively."""
    vault = Path(vault)
    ql = (q or "").strip().lower()
    out = []
    for rel in _iter_md(vault):
        if "/Learnings/" not in rel:
            continue
        rec = _learning_record(vault, rel)
        if project and rec["project"] != project:
            continue
        if scope and rec["scope"] != scope:
            continue
        if tag and tag not in rec["tags"]:
            continue
        if ql:
            hay = (rec["name"] + " " + rec["description"] + " " + " ".join(rec["tags"])).lower()
            if ql not in hay:
                continue
        out.append(rec)
    out.sort(key=lambda r: (r["date"] or "", r["rel_path"]), reverse=True)
    return out[:limit]


def overview(vault) -> dict:
    """Aggregate snapshot of the vault: totals, status/project/scope breakdowns, a tag
    histogram, and a month-by-month growth series from ticket authored dates."""
    vault = Path(vault)
    learnings = 0
    tickets_by_status: dict = {}
    by_project: dict = {}
    by_scope: dict = {}
    tag_hist: dict = {}
    growth: dict = {}  # "YYYY-MM" -> {"learnings": n, "tickets_resolved": n}

    for rel in _iter_md(vault):
        scope, project = classify_scope(rel)
        if rel.endswith("/_index.md") or scope == "index":
            fm = parse_frontmatter((vault / rel).read_text(encoding="utf-8", errors="replace"))
            status = (fm_get(fm, "status") or "unknown").strip() or "unknown"
            tickets_by_status[status] = tickets_by_status.get(status, 0) + 1
            resolved = fm.get("resolved")
            if isinstance(resolved, str) and len(resolved) >= 7:
                m = resolved[:7]
                growth.setdefault(m, {"learnings": 0, "tickets_resolved": 0})["tickets_resolved"] += 1
            continue
        if "/Learnings/" not in rel:
            continue
        learnings += 1
        by_scope[scope or "?"] = by_scope.get(scope or "?", 0) + 1
        if project:
            slot = by_project.setdefault(project, {"learnings": 0})
            slot["learnings"] += 1
        fm = parse_frontmatter((vault / rel).read_text(encoding="utf-8", errors="replace"))
        for t in (fm.get("tags") if isinstance(fm.get("tags"), list) else []):
            tag_hist[t] = tag_hist.get(t, 0) + 1
        d = _ticket_date(vault, rel)
        if d and len(d) >= 7:
            growth.setdefault(d[:7], {"learnings": 0, "tickets_resolved": 0})["learnings"] += 1

    tickets_total = sum(tickets_by_status.values())
    return {
        "totals": {"learnings": learnings, "tickets": tickets_total},
        "tickets_by_status": dict(sorted(tickets_by_status.items(), key=lambda kv: -kv[1])),
        "by_scope": by_scope,
        "by_project": [
            {"project": p, "learnings": v["learnings"]}
            for p, v in sorted(by_project.items(), key=lambda kv: -kv[1]["learnings"])
        ],
        "top_tags": sorted(tag_hist.items(), key=lambda kv: (-kv[1], kv[0]))[:30],
        "growth": [{"month": m, **growth[m]} for m in sorted(growth)],
    }


def read_item(vault, rel_path: str) -> dict:
    """One learning/ticket as frontmatter + safe rendered HTML. Path-guarded: the
    resolved target must stay inside the vault and be an existing .md."""
    vault = Path(vault).resolve()
    target = (vault / rel_path).resolve()
    if vault not in target.parents or target.suffix != ".md" or not target.is_file():
        raise ValueError("path outside vault or not a markdown file")
    text = target.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(text)
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            body = text[end + 4:].lstrip("\n")
    scope, project = classify_scope(rel_path)
    return {
        "rel_path": rel_path,
        "scope": scope or "",
        "project": project or "",
        "title": fm_get(fm, "title") or fm_get(fm, "name") or Path(rel_path).stem,
        "frontmatter": {k: v for k, v in fm.items() if k != "metadata"},
        "html": render_markdown(body),
    }


# --- minimal, safe markdown -> HTML (no third-party dependency) ----------------

def _inline(text: str) -> str:
    text = html.escape(text)

    def link(m):
        label, url = m.group(1), m.group(2)
        if not re.match(r"^(https?:|/|\.|#|mailto:)", url, re.I):
            url = "#"
        return f'<a href="{url}" target="_blank" rel="noopener">{label}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    return text


def render_markdown(md: str) -> str:
    """A small, safe Markdown subset: headings, fenced code, lists, blockquotes,
    paragraphs, and inline code/bold/italic/links. Everything is HTML-escaped before
    any tag is emitted, so vault content can never inject markup."""
    out: list = []
    para: list = []
    list_tag = None
    in_code = False
    code: list = []

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            if not in_code:
                flush_para(); close_list(); in_code = True; code = []
            else:
                out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                in_code = False
            continue
        if in_code:
            code.append(line)
            continue
        if not line.strip():
            flush_para(); close_list(); continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            flush_para(); close_list()
            lvl = len(h.group(1))
            out.append(f"<h{lvl}>{_inline(h.group(2).strip())}</h{lvl}>")
            continue
        if line.lstrip().startswith(">"):
            flush_para(); close_list()
            out.append("<blockquote>" + _inline(line.lstrip()[1:].strip()) + "</blockquote>")
            continue
        ul = re.match(r"^\s*[-*]\s+(.*)$", line)
        ol = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if ul or ol:
            flush_para()
            want = "ul" if ul else "ol"
            if list_tag != want:
                close_list(); out.append(f"<{want}>"); list_tag = want
            out.append("<li>" + _inline((ul or ol).group(1).strip()) + "</li>")
            continue
        para.append(line)

    flush_para(); close_list()
    if in_code:
        out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
    return "\n".join(out)
