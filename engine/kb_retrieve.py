#!/usr/bin/env python
"""
kb_retrieve.py — KB context injection (two-stage retrieval).

Pipeline:
  1. BM25 lexical over manifest (top 20 candidates).
  2. Haiku rerank with confidence + reasoning (top 5).
  3. GraphRAG 1-hop expansion via Obsidian [[wikilinks]] from top 2.
  4. Tier emission (high/mid/low) based on top-1 confidence.

Additive: if branch matches a canonical ticket folder, inject that ticket's
frontmatter + ticket-level learnings (preserves former Path A behavior).

Safety:
  - Kill-switch checked by parent shell hook.
  - Daily cap on Haiku calls (KB_HAIKU_DAILY_CAP, default 150).
  - Prompt-hash dedupe (60s) to skip duplicate consecutive prompts.
  - Hard timeout on Haiku (KB_HAIKU_TIMEOUT, default 3s).
  - Fallback to BM25-only when Haiku fails / over cap / times out.
  - Output budget guard (KB_BUDGET_BYTES, default 8000).
"""
import sys
import os
import re
import json
import time
import glob
import hashlib
import subprocess
import unicodedata
import urllib.request
import urllib.error
from pathlib import Path
from math import log

try:
    import snowballstemmer
    _PT_STEMMER = snowballstemmer.stemmer("portuguese")
    HAS_STEMMER = True
except Exception:
    _PT_STEMMER = None
    HAS_STEMMER = False

# ====== CONFIG ======
# Vault resolution lives in the shared engine module (kb_config). Hook hot-path
# uses strict=False: an unresolved vault yields None and main() degrades (emits
# nothing) — it must NEVER raise into the prompt flow. The old hardcoded default
# was exactly the "best guess" the ponto-sensível rule forbids; removing it and
# refusing to guess is compliant.
try:
    from kb_config import resolve_vault
    VAULT = resolve_vault(strict=False)
except Exception:
    VAULT = None
HOME = Path(os.environ.get("HOME", os.path.expanduser("~")))
CACHE_DIR = HOME / ".claude" / "cache"
LOG_DIR = HOME / ".claude" / "logs"
MANIFEST = CACHE_DIR / "kb-manifest.json"
DAILY_COUNT_FILE = HOME / ".claude" / ".haiku-daily-count"

HAIKU_MODEL = os.environ.get("KB_HAIKU_MODEL", "claude-haiku-4-5-20251001")
# Direct API via OAuth ~2-3s sync; CLI overhead was 60-80s.
HAIKU_TIMEOUT = int(os.environ.get("KB_HAIKU_TIMEOUT", "15"))
HAIKU_DAILY_CAP = int(os.environ.get("KB_HAIKU_DAILY_CAP", "300"))
CREDENTIALS_FILE = HOME / ".claude" / ".credentials.json"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
# Haiku result cache — TTL and dir.
HAIKU_CACHE_TTL = int(os.environ.get("KB_HAIKU_CACHE_TTL", "300"))
HAIKU_CACHE_DIR = CACHE_DIR
# Adaptive skip: skip Haiku when BM25 already decided.
SKIP_HAIKU_TOP1 = float(os.environ.get("KB_SKIP_HAIKU_TOP1", "12.0"))
SKIP_HAIKU_GAP = float(os.environ.get("KB_SKIP_HAIKU_GAP", "5.0"))
BUDGET_BYTES = int(os.environ.get("KB_BUDGET_BYTES", "15000"))
TOP_BM25 = int(os.environ.get("KB_TOP_BM25", "40"))
TOP_FINAL = int(os.environ.get("KB_TOP_FINAL", "8"))
DEDUPE_TTL = int(os.environ.get("KB_DEDUPE_TTL", "60"))
# Haiku confidence tiers (when Haiku rerank ativo)
HIGH_THRESHOLD = float(os.environ.get("KB_HIGH_THRESHOLD", "0.75"))
MID_THRESHOLD = float(os.environ.get("KB_MID_THRESHOLD", "0.45"))
# BM25-fallback tiers (raised post-eval: 3.0→5.0 kill false positives)
BM25_HIGH_TIER = float(os.environ.get("KB_BM25_HIGH", "8.0"))
BM25_MID_TIER = float(os.environ.get("KB_BM25_MID", "5.0"))
# Scope weights: dense knowledge boost (workspace > project > ticket)
SCOPE_WEIGHT = {
    "workspace": float(os.environ.get("KB_SCOPE_WORKSPACE", "1.30")),
    "project": float(os.environ.get("KB_SCOPE_PROJECT", "1.20")),
    "ticket": float(os.environ.get("KB_SCOPE_TICKET", "1.00")),
    "index": float(os.environ.get("KB_SCOPE_INDEX", "1.05")),
}
# Status weights: down-weight uncertain/dead tickets so they don't pollute
# retrieval. `experimental` = branch that may never ship; `discarded` = dead
# (excluded). Reversible: finalize flips experimental->resolved on merge,
# restoring full weight. Learnings inherit the status of their _index.md.
STATUS_WEIGHT = {
    "experimental": float(os.environ.get("KB_STATUS_EXPERIMENTAL", "0.4")),
    "discarded": float(os.environ.get("KB_STATUS_DISCARDED", "0.0")),
}
# Manifest mtime walk skip TTL — scales for a large vault
MANIFEST_RECHECK_TTL = int(os.environ.get("KB_MANIFEST_RECHECK", "30"))
FAST_MODE = os.environ.get("KB_FAST_MODE", "0") == "1"
# Haiku rerank ON by default since 2026-05-19.
# ~13s sync latency, accepted by the user in exchange for semantic quality.
# Disable via KB_HAIKU_RERANK=0 or KB_FAST_MODE=1.
HAIKU_RERANK = os.environ.get("KB_HAIKU_RERANK", "1") == "1" and not FAST_MODE
# Embedding-backed primary retrieval (via kb-embed-daemon). Falls back to
# pure BM25 if daemon unreachable. Hard-restricted to kind=md — transcripts
# belong to kb-sync (capture), not interactive injection.
EMBED_RETRIEVAL = os.environ.get("KB_EMBED_RETRIEVAL", "1") == "1" and not FAST_MODE
EMBED_TOP_N = int(os.environ.get("KB_EMBED_TOP_N", "40"))
EMBED_ALPHA = float(os.environ.get("KB_EMBED_ALPHA", "0.7"))   # cosine weight
EMBED_BETA = float(os.environ.get("KB_EMBED_BETA", "0.3"))     # BM25-norm weight
EMBED_MIN_SCORE = float(os.environ.get("KB_EMBED_MIN_SCORE", "0.35"))
EMBED_DAEMON_TIMEOUT = float(os.environ.get("KB_EMBED_DAEMON_TIMEOUT", "2.0"))

STOPWORDS = {
    "de", "da", "do", "das", "dos", "o", "a", "os", "as", "um", "uma", "uns", "umas",
    "para", "por", "com", "sem", "em", "no", "na", "nos", "nas", "que", "se",
    "ser", "estar", "tem", "ter", "mais", "menos", "quando", "onde", "como", "qual",
    "esse", "essa", "isso", "este", "esta", "isto", "aquele", "aquela", "aquilo",
    "meu", "minha", "seu", "sua", "nosso", "nossa", "the", "and", "or", "of",
    "to", "in", "is", "at", "on", "for", "with", "from", "but", "not", "this",
    "that", "these", "those", "are", "was", "were", "be", "been", "have", "has",
}

# ====== UTILITIES ======


def log_budget(msg: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "kb-budget.log").open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}\n")
    except Exception:
        pass


def strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def tokenize(text: str) -> list:
    if not text:
        return []
    text = strip_accents(text.lower())
    text = text.replace("-", " ").replace("_", " ")
    raw = re.findall(r"[a-z0-9]+", text)
    out = []
    for t in raw:
        if len(t) < 3 or t in STOPWORDS:
            continue
        if HAS_STEMMER:
            try:
                stem = _PT_STEMMER.stemWord(t)
                out.append(stem if stem and len(stem) >= 3 else t)
            except Exception:
                out.append(t)
        else:
            out.append(t)
    return out


def parse_frontmatter(content: str) -> dict:
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end < 0:
        return {}
    block = content[3:end]
    fm = {}
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
            items = [v.strip().strip('"').strip("'") for v in inner.split(",") if v.strip()]
            fm[key] = items
            in_list = False
        else:
            fm[key] = value.strip('"').strip("'")
            in_list = False
    return fm


def classify_scope(rel_path: str):
    """KB layouts (relative to vault). project_name is None only for workspace.

       <ws>/Learnings/x.md                        -> workspace
       <ws>/<proj>/Learnings/x.md                 -> project
       <ws>/<proj>/<slug>/Learnings/x.md          -> ticket  (ungrouped branch)
       <ws>/<proj>/<type>/<slug>/Learnings/x.md   -> ticket  (type-grouped)
       <ws>/<proj>/<slug>/_index.md               -> index   (ungrouped)
       <ws>/<proj>/<type>/<slug>/_index.md        -> index   (type-grouped)
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
    """Folder that holds the _index.md governing this file, or None.
    Used so Learnings inherit their ticket's status (experimental/discarded)."""
    if rel_path.endswith("/_index.md"):
        return rel_path[: -len("/_index.md")]
    i = rel_path.find("/Learnings/")
    if i >= 0:
        return rel_path[:i]
    return None


# ====== MANIFEST ======


def manifest_needs_rebuild() -> bool:
    if not MANIFEST.exists():
        return True
    manifest_mtime = MANIFEST.stat().st_mtime
    recheck_sentinel = CACHE_DIR / "kb-manifest-recheck.ts"
    if recheck_sentinel.exists():
        try:
            age = time.time() - recheck_sentinel.stat().st_mtime
            if age < MANIFEST_RECHECK_TTL:
                return False
        except OSError:
            pass
    try:
        for md in VAULT.rglob("*.md"):
            try:
                if md.stat().st_mtime > manifest_mtime:
                    return True
            except OSError:
                continue
    except OSError:
        return False
    try:
        recheck_sentinel.touch()
    except OSError:
        pass
    return False


def build_manifest() -> dict:
    entries = []
    index_status = {}  # ticket_dir -> status (from _index.md frontmatter)
    if not VAULT.exists():
        return {"built_at": time.time(), "entries": entries}
    for md in VAULT.rglob("*.md"):
        try:
            rel = str(md.relative_to(VAULT)).replace("\\", "/")
        except ValueError:
            continue
        scope, proj = classify_scope(rel)
        if scope is None:
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm = parse_frontmatter(content)
        body = content
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end >= 0:
                body = content[end + 4:]
        wikilinks = re.findall(r"\[\[([^\]|#]+)(?:[|#][^\]]+)?\]\]", body)
        desc = fm.get("description", "") if isinstance(fm.get("description"), str) else ""
        if not desc:
            titulo = fm.get("title") or fm.get("título") or fm.get("titulo")
            if isinstance(titulo, str):
                desc = titulo
        if not desc:
            for line in body.splitlines():
                ls = line.strip()
                if ls and not ls.startswith("#") and not ls.startswith("---"):
                    desc = ls[:200]
                    break
        tags = fm.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list):
            tags = []
        modulo = fm.get("module") or fm.get("módulo") or fm.get("modulo") or ""
        if not isinstance(modulo, str):
            modulo = ""
        try:
            mtime = md.stat().st_mtime
        except OSError:
            mtime = 0
        own_status = fm.get("status")
        own_status = own_status.strip().lower() if isinstance(own_status, str) else ""
        tdir = ticket_dir_of(rel)
        if scope == "index" and own_status and tdir is not None:
            index_status[tdir] = own_status
        entries.append({
            "path": rel,
            "name": md.stem,
            "scope": scope,
            "projeto": proj or "",
            "tags": tags,
            "modulo": modulo,
            "desc": desc[:200],
            "wikilinks": wikilinks[:10],
            "mtime": mtime,
            "status": own_status,
            "tdir": tdir,
        })
    # Resolve status: Learnings without own status inherit their _index.md's.
    for e in entries:
        if not e["status"] and e["tdir"]:
            e["status"] = index_status.get(e["tdir"], "")
        e.pop("tdir", None)
    data = {"built_at": time.time(), "entries": entries}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    log_budget(f"manifest rebuilt: {len(entries)} entries")
    return data


def load_manifest() -> dict:
    if manifest_needs_rebuild():
        return build_manifest()
    try:
        with MANIFEST.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return build_manifest()


# ====== BM25 ======


def bm25_score(query_tokens: list, entries: list) -> list:
    if not entries or not query_tokens:
        return []
    docs = []
    for e in entries:
        parts = [
            e["name"].replace("-", " "),
            " ".join(e["tags"]),
            e["modulo"],
            e["desc"],
            e["projeto"],
        ]
        docs.append(tokenize(" ".join(p for p in parts if p)))
    k1, b = 1.5, 0.75
    N = len(docs)
    avgdl = max(1.0, sum(len(d) for d in docs) / N)
    df = {}
    for doc in docs:
        for t in set(doc):
            df[t] = df.get(t, 0) + 1
    scored = []
    for i, doc in enumerate(docs):
        if not doc:
            scored.append((0.0, i))
            continue
        tf = {}
        for t in doc:
            tf[t] = tf.get(t, 0) + 1
        dl = len(doc)
        score = 0.0
        for q in query_tokens:
            f = tf.get(q, 0)
            if f == 0:
                continue
            n_q = df.get(q, 1)
            idf = log((N - n_q + 0.5) / (n_q + 0.5) + 1)
            num = f * (k1 + 1)
            denom = f + k1 * (1 - b + b * dl / avgdl)
            score += idf * num / denom
        scope = entries[i].get("scope", "ticket")
        score *= SCOPE_WEIGHT.get(scope, 1.0)
        score *= STATUS_WEIGHT.get(entries[i].get("status", ""), 1.0)
        scored.append((score, i))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


# ====== HAIKU RERANK ======


def get_daily_count() -> int:
    today = time.strftime("%Y-%m-%d")
    if not DAILY_COUNT_FILE.exists():
        return 0
    try:
        lines = DAILY_COUNT_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) >= 2 and lines[0] == today:
            return int(lines[1])
    except Exception:
        return 0
    return 0


def incr_daily_count() -> None:
    today = time.strftime("%Y-%m-%d")
    cur = get_daily_count()
    try:
        DAILY_COUNT_FILE.write_text(f"{today}\n{cur + 1}\n", encoding="utf-8")
    except Exception:
        pass


def _load_oauth_token():
    try:
        with CREDENTIALS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("claudeAiOauth", {}).get("accessToken")
    except Exception as exc:
        log_budget(f"haiku creds load fail: {exc}")
        return None


def _haiku_cache_key(prompt: str, candidates: list, entries: list) -> str:
    """Reordering-invariant hash — only the prompt's stemmed tokens + candidate paths."""
    prompt_tokens = sorted(set(tokenize(prompt)))
    paths = sorted(entries[i]["path"] for _, i in candidates)
    payload = json.dumps({"p": prompt_tokens, "c": paths}, ensure_ascii=False)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def _haiku_cache_get(key: str):
    cache_file = HAIKU_CACHE_DIR / f"kb-haiku-{key}.json"
    if not cache_file.exists():
        return None
    try:
        if time.time() - cache_file.stat().st_mtime > HAIKU_CACHE_TTL:
            return None
        with cache_file.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _haiku_cache_put(key: str, value: dict) -> None:
    cache_file = HAIKU_CACHE_DIR / f"kb-haiku-{key}.json"
    try:
        with cache_file.open("w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False)
    except Exception:
        pass


def _haiku_cache_cleanup() -> None:
    """Delete Haiku caches older than 2x TTL."""
    cutoff = time.time() - HAIKU_CACHE_TTL * 2
    try:
        for f in HAIKU_CACHE_DIR.glob("kb-haiku-*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue
    except OSError:
        pass


def haiku_rerank(prompt: str, candidates: list, entries: list):
    if FAST_MODE:
        return None
    if not candidates:
        return None
    cache_key = _haiku_cache_key(prompt, candidates, entries)
    cached = _haiku_cache_get(cache_key)
    if cached is not None:
        log_budget(f"haiku cache hit {cache_key}")
        return cached
    if get_daily_count() >= HAIKU_DAILY_CAP:
        log_budget(f"haiku cap reached ({HAIKU_DAILY_CAP}) — fallback bm25")
        return None
    token = _load_oauth_token()
    if not token:
        log_budget("haiku: no OAuth token — fallback bm25")
        return None
    lines = []
    for idx, (score, i) in enumerate(candidates, 1):
        e = entries[i]
        tags = ",".join(e["tags"][:5]) if e["tags"] else ""
        lines.append(
            f"{idx}. [{e['scope']}] {e['path']}\n"
            f"   desc: {e['desc'][:140]}\n"
            f"   tags: {tags}\n"
            f"   mod: {e['modulo']}\n"
            f"   bm25: {score:.2f}"
        )
    cand_block = "\n".join(lines)
    user_clip = prompt[:1500]
    rerank_prompt = (
        f"User prompt:\n\"\"\"\n{user_clip}\n\"\"\"\n\n"
        f"Vault candidates (BM25 top {len(candidates)}):\n{cand_block}\n\n"
        "Task: rerank by semantic relevance to the prompt. "
        "Consider intent, modules, synonyms, and technical abbreviations. "
        "Items with scope 'workspace' and 'project' have higher density — prefer them on ties.\n\n"
        "Reply with a SINGLE JSON line:\n"
        "{\"items\": [{\"path\": \"<path>\", \"confidence\": 0.0-1.0, \"why\": \"<short phrase, same language as the candidate>\"}], \"tier\": \"high|mid|low\"}\n\n"
        f"At most {TOP_FINAL} items. Tier by top-1 confidence: "
        f">= {HIGH_THRESHOLD} high; {MID_THRESHOLD}-{HIGH_THRESHOLD} mid; "
        f"< {MID_THRESHOLD} low (items may be empty)."
    )
    body = json.dumps({
        "model": HAIKU_MODEL,
        "max_tokens": 800,
        "messages": [{"role": "user", "content": rerank_prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=body,
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=HAIKU_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        incr_daily_count()
        elapsed = time.time() - t0
        try:
            data = json.loads(raw)
        except Exception as exc:
            log_budget(f"haiku json parse fail after {elapsed:.2f}s: {exc}")
            return None
        content_blocks = data.get("content", [])
        text = ""
        for blk in content_blocks:
            if blk.get("type") == "text":
                text += blk.get("text", "")
        if not text:
            log_budget(f"haiku empty content after {elapsed:.2f}s")
            return None
        usage = data.get("usage", {})
        log_budget(
            f"haiku ok {elapsed:.2f}s in={usage.get('input_tokens', 0)} "
            f"out={usage.get('output_tokens', 0)}"
        )
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        parsed = json.loads(m.group())
        _haiku_cache_put(cache_key, parsed)
        return parsed
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")[:300] if exc.fp else ""
        if exc.code == 401:
            log_budget(
                f"haiku http 401 UNAUTHORIZED after {time.time()-t0:.2f}s — "
                f"OAuth token expirado. Rode /login no Claude Code para renovar. "
                f"Body: {body}"
            )
        else:
            log_budget(f"haiku http {exc.code} after {time.time()-t0:.2f}s: {body}")
        return None
    except (TimeoutError, urllib.error.URLError) as exc:
        log_budget(f"haiku network error after {time.time()-t0:.2f}s: {exc!r}")
        return None
    except Exception as exc:
        log_budget(f"haiku unexpected error: {exc!r}")
        return None


# ====== TICKET MATCH (additive Path A) ======


def find_ticket_folder(branch: str):
    """Locate the KB folder for a branch, both layouts:
       <vault>/<ws>/<proj>/<type>/<slug>/   (branch contained a "/")
       <vault>/<ws>/<proj>/<branch>/        (no "/")
    Match key is the branch-derived folder name (no numeric id required)."""
    if not branch:
        return None
    branch = branch.strip()
    if "/" in branch:
        tipo, slug = branch.split("/", 1)
        pattern = str(VAULT / "*" / "*" / tipo / slug)
    else:
        pattern = str(VAULT / "*" / "*" / branch)
    matches = [Path(p) for p in glob.glob(pattern)
               if Path(p).is_dir() and (Path(p) / "_index.md").exists()]
    if not matches:
        return None
    pwd_base = Path(os.getcwd()).name

    def project_of(p: Path):
        rel = p.relative_to(VAULT).parts
        return rel[1] if len(rel) >= 2 else ""

    filtered = [p for p in matches if project_of(p) == pwd_base]
    pick = filtered[0] if filtered else matches[0]
    try:
        return str(pick.relative_to(VAULT)).replace("\\", "/")
    except ValueError:
        return None


def emit_ticket_block(ticket_rel: str) -> list:
    out = [f"## Current ticket: {ticket_rel}"]
    index_path = VAULT / ticket_rel / "_index.md"
    if index_path.exists():
        try:
            content = index_path.read_text(encoding="utf-8", errors="ignore")
            fm = parse_frontmatter(content)
        except Exception:
            fm = {}
        keys = [
            "id", "type", "project", "module", "status", "tags",
            "apparent_problem", "actual_solution", "title",
        ]
        seen = set()
        for k in keys:
            if k in seen:
                continue
            v = fm.get(k)
            if v in (None, "", []):
                continue
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            label = k
            out.append(f"- {label}: {v}")
            seen.add(k)
    ticket_learn = VAULT / ticket_rel / "Learnings"
    if ticket_learn.exists():
        files = sorted(ticket_learn.glob("*.md"))
        if files:
            out.append("- Ticket learnings:")
            for f in files[:8]:
                out.append(f"  - [[{ticket_rel}/Learnings/{f.stem}]]")
    return out


# ====== EMIT ======


def resolve_wikilink(stem: str, entries: list, entry_by_name: dict):
    """Resolve [[wikilink]] to a full vault path via manifest name lookup.

    Accepts bare names (`foo`), with extension (`foo.md`), or paths
    (`a/b/foo`, `../feat/X/Learnings/foo`). Last path segment is used as
    the name key.
    """
    if not stem:
        return None
    bare = stem.split("/")[-1]
    if bare.endswith(".md"):
        bare = bare[:-3]
    paths = entry_by_name.get(bare)
    if not paths:
        return None
    return paths[0]


# ====== EMBEDDING RETRIEVAL (via kb-embed-daemon) ======


def _daemon_search_md(prompt: str, k: int):
    """Ask the embedding daemon for top-K md chunks. Hard-restrict kind=md.
    Returns list of dicts {path, score, ...} or None if daemon unreachable.
    Auto-spawn the daemon best-effort (this call falls back; next gets it)."""
    import socket as _sk
    lock = HOME / ".claude" / "state" / "kb-embed-daemon.lock"
    if not lock.exists():
        _maybe_spawn_daemon()
        return None
    try:
        info = json.loads(lock.read_text(encoding="utf-8"))
        port = info.get("port")
    except Exception:
        return None
    if not isinstance(port, int):
        return None
    payload = json.dumps({
        "op": "search", "query": prompt, "k": k,
        "filter": {"kind": ["md"], "min_score": EMBED_MIN_SCORE},
    }) + "\n"
    s = _sk.socket()
    s.settimeout(EMBED_DAEMON_TIMEOUT)
    try:
        s.connect(("127.0.0.1", port))
        s.sendall(payload.encode("utf-8"))
        chunks = []
        while True:
            buf = s.recv(65536)
            if not buf:
                break
            chunks.append(buf)
            if b"\n" in buf:
                break
        raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if not raw:
            return None
        resp = json.loads(raw.split("\n", 1)[0])
    except OSError:
        _maybe_spawn_daemon()
        return None
    except Exception:
        return None
    finally:
        s.close()
    if not resp.get("ok"):
        return None
    return resp.get("hits") or []


def _maybe_spawn_daemon():
    """Detached spawn of the embedding daemon if not running. Non-blocking —
    this hook falls back to BM25 on the current call; next call uses daemon."""
    here = Path(__file__).resolve().parent.parent / "scripts" / "kb-embed-daemon.py"
    if not here.exists():
        return
    import subprocess as _sp
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED|NEW_PG|NO_WINDOW
        _sp.Popen([sys.executable, str(here)],
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, stdin=_sp.DEVNULL,
                  close_fds=True, creationflags=creationflags)
        log_budget("kb-embed-daemon spawned (lazy)")
    except Exception as exc:
        log_budget(f"daemon spawn fail: {exc}")


def hybrid_candidates(prompt: str, q_tokens: list, entries: list,
                       bm25_top: list, top_n: int):
    """Combine cosine (daemon) + normalized BM25 over the same chunks.

    Returns [(score, idx)] in entries-list space — same shape as bm25_top so
    the rest of the pipeline (Haiku rerank, emit, GraphRAG) is unchanged.

    Falls back to bm25_top untouched if the daemon is unreachable.
    """
    hits = _daemon_search_md(prompt, k=top_n)
    if hits is None:
        return None  # signal caller to use bm25_top as-is

    path_to_idx = {e["path"]: i for i, e in enumerate(entries)}
    cos_by_idx = {}
    for h in hits:
        idx = path_to_idx.get(h.get("path"))
        if idx is not None:
            cos_by_idx[idx] = float(h.get("score", 0.0))

    bm25_by_idx = {i: s for s, i in bm25_top if s > 0}
    bm25_max = max(bm25_by_idx.values()) if bm25_by_idx else 0.0
    candidate_idxs = set(cos_by_idx) | set(bm25_by_idx)

    fused = []
    for idx in candidate_idxs:
        cos = cos_by_idx.get(idx, 0.0)
        bm25 = bm25_by_idx.get(idx, 0.0)
        bm25_n = (bm25 / bm25_max) if bm25_max > 0 else 0.0
        scope = entries[idx].get("scope", "ticket")
        weight = SCOPE_WEIGHT.get(scope, 1.0)
        weight *= STATUS_WEIGHT.get(entries[idx].get("status", ""), 1.0)
        score = (EMBED_ALPHA * cos + EMBED_BETA * bm25_n) * weight
        fused.append((score, idx))
    fused.sort(reverse=True)
    return fused[:top_n]


_LEARNING_RE = re.compile(r"[\\/]Learnings[\\/].*\.md$", re.IGNORECASE)
_INDEX_RE = re.compile(r"[\\/]_index\.md$", re.IGNORECASE)


def _is_trackable_learning(path: str) -> bool:
    if not path:
        return False
    if not _LEARNING_RE.search(path):
        return False
    if _INDEX_RE.search(path):
        return False
    return True


def emit_output(branch: str, ticket_match, reranked, bm25_top, entries, injected_paths=None) -> str:
    out = ["<vault-context>"]
    branch_disp = branch if branch else "no-branch"
    out.append(f"KB cross-ticket (branch={branch_disp}):")
    out.append("")
    entry_by_path = {e["path"]: e for e in entries}
    entry_by_name = {}
    for e in entries:
        entry_by_name.setdefault(e["name"], []).append(e["path"])
    used_rerank = False

    def _track(p: str) -> None:
        if injected_paths is None or not p:
            return
        if _is_trackable_learning(p) and p not in injected_paths:
            injected_paths.append(p)

    if reranked and isinstance(reranked, dict):
        items = reranked.get("items") or []
        tier = reranked.get("tier", "mid")
        if items:
            used_rerank = True
            out.append(f"## Top matches (tier={tier}, via Haiku rerank):")
            top1_conf = float(items[0].get("confidence", 0) or 0)
            for it in items[:TOP_FINAL]:
                p = it.get("path", "")
                conf = it.get("confidence", 0)
                why = it.get("why", "")
                try:
                    conf_f = float(conf)
                except Exception:
                    conf_f = 0.0
                out.append(f"- [[{p}]] (conf={conf_f:.2f}) — {why}")
                _track(p)

            if tier == "high" and items:
                top_path = items[0].get("path", "")
                top_entry = entry_by_path.get(top_path)
                if top_entry:
                    body_path = VAULT / top_path
                    if body_path.exists():
                        try:
                            content = body_path.read_text(encoding="utf-8", errors="ignore")
                            if content.startswith("---"):
                                end = content.find("\n---", 3)
                                if end >= 0:
                                    content = content[end + 4:]
                            content = content.strip()
                            excerpt = content[:1200]
                            out.append("")
                            out.append(f"### Body excerpt — {top_path}:")
                            out.append(excerpt)
                            if len(content) > 1200:
                                out.append("[...truncated]")
                        except Exception:
                            pass

            linked = []
            seen_targets = set()
            top_paths = {it.get("path", "") for it in items[:TOP_FINAL]}
            for it in items[:2]:
                e = entry_by_path.get(it.get("path", ""))
                if not e:
                    continue
                for wl in e.get("wikilinks", [])[:5]:
                    raw = wl.strip()
                    if not raw:
                        continue
                    resolved = resolve_wikilink(raw, entries, entry_by_name)
                    target = resolved or raw
                    if target in seen_targets or target in top_paths:
                        continue
                    seen_targets.add(target)
                    linked.append((raw, resolved))
            if linked:
                out.append("")
                out.append("## Related (GraphRAG 1-hop):")
                for raw, resolved in linked[:5]:
                    out.append(f"- [[{resolved or raw}]]")

    if not used_rerank:
        if bm25_top:
            top_score = bm25_top[0][0]
            # Hybrid scores live in ~[0,1.3]; BM25 raw in [0,30+]. Detect by range.
            is_hybrid = top_score <= 1.5
            if is_hybrid:
                hi, mid = 0.60, 0.45
                source_label = "hybrid embedding (cosine+BM25)"
                score_label = "score"
            else:
                hi, mid = BM25_HIGH_TIER, BM25_MID_TIER
                source_label = "BM25 lexical"
                score_label = "bm25"
            if top_score >= hi:
                tier_label = "high"
            elif top_score >= mid:
                tier_label = "mid"
            else:
                tier_label = "low"

            if tier_label == "low":
                out.append(f"## No strong match ({source_label}):")
                out.append(f"- top {score_label}: {top_score:.2f} (mid threshold: {mid})")
                out.append("- /kb-search if relevant to the technical context")
            else:
                out.append(f"## Top matches (tier={tier_label}, via {source_label}):")
                for score, i in bm25_top[:TOP_FINAL]:
                    e = entries[i]
                    out.append(f"- [[{e['path']}]] ({score_label}={score:.2f}) — {e['desc'][:120]}")
                    _track(e["path"])

                if tier_label == "high":
                    top_entry = entries[bm25_top[0][1]]
                    body_path = VAULT / top_entry["path"]
                    if body_path.exists():
                        try:
                            content = body_path.read_text(encoding="utf-8", errors="ignore")
                            if content.startswith("---"):
                                end = content.find("\n---", 3)
                                if end >= 0:
                                    content = content[end + 4:]
                            content = content.strip()
                            excerpt = content[:1200]
                            out.append("")
                            out.append(f"### Body excerpt — {top_entry['path']}:")
                            out.append(excerpt)
                            if len(content) > 1200:
                                out.append("[...truncated]")
                        except Exception:
                            pass

                linked = []
                seen_targets = set()
                top_paths = {entries[i]["path"] for _, i in bm25_top[:TOP_FINAL]}
                for score, i in bm25_top[:2]:
                    e = entries[i]
                    for wl in e.get("wikilinks", [])[:5]:
                        raw = wl.strip()
                        if not raw:
                            continue
                        resolved = resolve_wikilink(raw, entries, entry_by_name)
                        target = resolved or raw
                        if target in seen_targets or target in top_paths:
                            continue
                        seen_targets.add(target)
                        linked.append((raw, resolved))
                if linked:
                    out.append("")
                    out.append("## Related (GraphRAG 1-hop):")
                    for raw, resolved in linked[:5]:
                        out.append(f"- [[{resolved or raw}]]")
        elif not ticket_match:
            out.append("No lexical candidates. /kb-search <query> if relevant.")

    if ticket_match:
        out.append("")
        out.extend(emit_ticket_block(ticket_match))

    out.append("")
    vault_posix = str(VAULT).replace("\\", "/")
    out.append(f"Read body: Read tool, absolute path = \"{vault_posix}/\" + the cited path. "
               f"Deep search: /kb-search.")
    out.append("</vault-context>")
    text = "\n".join(out)
    if len(text.encode("utf-8")) > BUDGET_BYTES:
        log_budget(f"output truncated: {len(text)} > {BUDGET_BYTES}")
        encoded = text.encode("utf-8")[:BUDGET_BYTES]
        text = encoded.decode("utf-8", errors="ignore") + "\n... [TRUNCATED — budget]\n</vault-context>"
    return text


# ====== STATE BUMP (auto-knowledge tracking) ======


def _sanitize_session(session_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\-_]", "", session_id or "")


def infer_tier(reranked, candidates, entries) -> str:
    """Determine the single tier that emit_output is about to publish.

    Mirrors the tier rules used inside emit_output so the statusline can read
    the same signal without re-parsing the textual output. Possible values:
    "high" | "mid" | "low" | "none".
    """
    if reranked and isinstance(reranked, dict):
        items = reranked.get("items") or []
        if items:
            return reranked.get("tier", "mid")
    if not candidates:
        return "none"
    top_score = candidates[0][0]
    # Hybrid scores live in ~[0, 1.3]; raw BM25 in [0, 30+]. Detect by range.
    is_hybrid = top_score <= 1.5
    if is_hybrid:
        hi, mid = 0.60, 0.45
    else:
        hi, mid = BM25_HIGH_TIER, BM25_MID_TIER
    if top_score >= hi:
        return "high"
    if top_score >= mid:
        return "mid"
    return "low"


def _tier_state_path(session_id: str) -> Path | None:
    safe = _sanitize_session(session_id)
    if not safe:
        return None
    state_dir = HOME / ".claude" / "state"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return state_dir / f"kb-tier-{safe}.json"


def bump_tier_state(session_id: str, tier: str) -> None:
    """Update per-session tier counters used by the statusline.

    Schema: {session_id, last_tier, hits (tier>=mid), total, last_used}.
    """
    path = _tier_state_path(session_id)
    if path is None:
        return
    state = None
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                state = json.load(fh)
        except Exception:
            state = None
    if not isinstance(state, dict):
        state = {"session_id": session_id, "last_tier": "none",
                 "hits": 0, "total": 0, "last_used": ""}
    state["session_id"] = session_id
    state["last_tier"] = tier
    state["total"] = int(state.get("total", 0)) + 1
    if tier in ("high", "mid"):
        state["hits"] = int(state.get("hits", 0)) + 1
    state["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
    except OSError:
        pass


# ====== TOKEN ACCOUNTING (for /kb-stats) ======

_TIKTOKEN_ENC = None  # lazy init: tiktoken.Encoding | False (probed-missing) | None (unprobed)


def _count_tokens(text: str):
    """Approximate token count for the `<vault-context>` block.

    Tries tiktoken cl100k_base (within ~5-10% of Claude's tokenizer for mixed
    PT/EN/code). Falls back to len(utf8_bytes)//4 (~15-20% off) when tiktoken
    is not installed. Returns (count, exact_flag).

    Note: the injected block enters the model's input on every prompt — billed
    whether or not the model attends to it semantically. Body-read tool calls
    (mcp__obsidian-vault__read_file) are *separate* and on-demand.
    """
    global _TIKTOKEN_ENC
    if not isinstance(text, str) or not text:
        return (0, _TIKTOKEN_ENC not in (None, False))
    if _TIKTOKEN_ENC is None:
        try:
            import tiktoken  # type: ignore
            _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _TIKTOKEN_ENC = False
    if _TIKTOKEN_ENC and _TIKTOKEN_ENC is not False:
        try:
            return (len(_TIKTOKEN_ENC.encode(text)), True)
        except Exception:
            pass
    return (max(1, len(text.encode("utf-8")) // 4), False)


_SECTION_ORDER = ("header", "matches", "body_excerpt", "graphrag", "ticket", "footer")
_CITE_RE = re.compile(r"\[\[([^\]|#]+)")


def _cited_keys_from_output(output: str) -> list:
    """Citation keys ([[...]] basenames, no .md, lowercased) emitted this prompt.

    Persisted per session so the PostToolUse body-read tracker can tell a read
    of a *cited* learning (real KB consumption) from an unrelated vault read
    (maintenance). Basename match is robust across relative/absolute/aliased
    paths; learning slugs are descriptive and effectively unique in the vault.
    """
    keys = []
    for m in _CITE_RE.findall(output):
        raw = m.strip()
        if not raw:
            continue
        base = raw.replace("\\", "/").split("/")[-1].strip()
        if base.lower().endswith(".md"):
            base = base[:-3]
        base = base.strip().lower()
        if base and base not in keys:
            keys.append(base)
    return keys


def _split_sections(output: str) -> dict:
    """Partition the emitted `<vault-context>` into known sections.

    State-machine over lines — boundaries match the `## ...` and `### Body
    excerpt` headers produced by emit_output. Anything before the first match
    header is `header`; anything from `Read body:` / `</vault-context>` is
    `footer`. Used to attribute tokens per section for /kb-stats.
    """
    sections = {k: [] for k in _SECTION_ORDER}
    cur = "header"
    for line in output.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("## Top matches") or stripped.startswith("## No strong match"):
            cur = "matches"
        elif stripped.startswith("### Body excerpt"):
            cur = "body_excerpt"
        elif stripped.startswith("## Related (GraphRAG"):
            cur = "graphrag"
        elif stripped.startswith("## Current ticket:"):
            cur = "ticket"
        elif stripped.startswith("Read body:") or stripped.startswith("</vault-context>"):
            cur = "footer"
        sections[cur].append(line)
    return {k: "".join(v) for k, v in sections.items()}


def _token_state_path(session_id: str):
    safe = _sanitize_session(session_id)
    if not safe:
        return None
    state_dir = HOME / ".claude" / "state"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return state_dir / f"kb-tokens-{safe}.json"


def bump_token_state(session_id: str, output: str, tier: str) -> None:
    """Persist per-prompt and cumulative token counts of the injected block.

    Read by /kb-stats. Schema:
      {session_id, prompts, total, exact_tokens,
       by_tier: {high,mid,low,none -> int},
       by_section: {header,matches,body_excerpt,graphrag,ticket,footer -> int},
       first_at, last_at,
       last: {total, exact, tier, sections, at}}
    """
    path = _token_state_path(session_id)
    if path is None or not output:
        return
    sections = _split_sections(output)
    section_tokens = {k: _count_tokens(v)[0] for k, v in sections.items()}
    total, exact = _count_tokens(output)
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    last = {"total": total, "exact": exact, "tier": tier,
            "sections": section_tokens, "at": now}
    state = None
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                state = json.load(fh)
        except Exception:
            state = None
    if not isinstance(state, dict):
        state = {"session_id": session_id, "prompts": 0, "total": 0,
                 "by_tier": {}, "by_section": {}, "first_at": now}
    state["session_id"] = session_id
    state["prompts"] = int(state.get("prompts", 0)) + 1
    state["total"] = int(state.get("total", 0)) + total
    state["exact_tokens"] = exact
    bt = state.setdefault("by_tier", {})
    bt[tier] = int(bt.get(tier, 0)) + 1
    bs = state.setdefault("by_section", {})
    for k, v in section_tokens.items():
        bs[k] = int(bs.get(k, 0)) + v
    state["last_at"] = now
    state["last"] = last
    cited = set(state.get("cited_keys", []))
    for k in _cited_keys_from_output(output):
        cited.add(k)
    state["cited_keys"] = sorted(cited)
    try:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
    except OSError:
        pass


# ====== MAIN ======


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except Exception:
        return
    prompt = payload.get("prompt", "") or ""
    session_id = payload.get("session_id", "") or ""
    if not prompt.strip():
        return

    if VAULT is None:
        log_budget("vault unresolved (no KB_VAULT / no kb-workspaces 'vault') — degrade, no injection")
        return

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    phash = hashlib.md5(f"{session_id}::{prompt}".encode("utf-8")).hexdigest()[:16]
    dedupe_file = CACHE_DIR / f"kb-dedupe-{phash}.done"
    if dedupe_file.exists():
        try:
            age = time.time() - dedupe_file.stat().st_mtime
            if age < DEDUPE_TTL:
                return
        except OSError:
            pass

    try:
        for f in CACHE_DIR.glob("kb-dedupe-*.done"):
            try:
                if time.time() - f.stat().st_mtime > 600:
                    f.unlink()
            except OSError:
                continue
    except OSError:
        pass

    branch = os.environ.get("KB_BRANCH", "") or ""
    ticket_match = find_ticket_folder(branch)

    try:
        manifest = load_manifest()
    except Exception as exc:
        log_budget(f"manifest load fail: {exc}")
        return
    entries = manifest.get("entries", [])
    if not entries:
        if ticket_match:
            out_text = emit_output(branch, ticket_match, None, [], entries)
            bump_tier_state(session_id, "none")
            bump_token_state(session_id, out_text, "none")
            print(out_text)
        return

    q_tokens = tokenize(prompt)
    if not q_tokens:
        if ticket_match:
            out_text = emit_output(branch, ticket_match, None, [], entries)
            bump_tier_state(session_id, "none")
            bump_token_state(session_id, out_text, "none")
            print(out_text)
        return

    scored = bm25_score(q_tokens, entries)
    bm25_top = [(s, i) for s, i in scored[:TOP_BM25] if s > 0]

    # Hybrid retrieval: cosine via daemon + normalized BM25, fused with scope
    # weights. Falls back transparently to pure BM25 when the daemon is down.
    candidates = bm25_top
    via_embed = False
    if EMBED_RETRIEVAL:
        hybrid = hybrid_candidates(prompt, q_tokens, entries, bm25_top, EMBED_TOP_N)
        if hybrid:
            candidates = hybrid
            via_embed = True
            log_budget(f"hybrid: n={len(candidates)} top1={candidates[0][0]:.3f}")

    if not candidates and not ticket_match:
        return

    # Adaptive Haiku skip only applies in pure-BM25 mode (scores are raw BM25 there).
    # In hybrid mode every score is bounded ~[0, 1.3]; we always rerank.
    skip_haiku = False
    if not via_embed and candidates and HAIKU_RERANK:
        top1 = candidates[0][0]
        top2 = candidates[1][0] if len(candidates) >= 2 else 0.0
        if top1 >= SKIP_HAIKU_TOP1 and (top1 - top2) >= SKIP_HAIKU_GAP:
            skip_haiku = True
            log_budget(f"haiku skip: bm25 top1={top1:.2f} gap={top1-top2:.2f} (decidido)")

    reranked = (
        haiku_rerank(prompt, candidates, entries)
        if (candidates and HAIKU_RERANK and not skip_haiku)
        else None
    )

    _haiku_cache_cleanup()

    injected_paths = []
    output = emit_output(branch, ticket_match, reranked, candidates, entries, injected_paths)
    tier = infer_tier(reranked, candidates, entries)
    bump_tier_state(session_id, tier)
    bump_token_state(session_id, output, tier)
    try:
        dedupe_file.touch()
    except OSError:
        pass
    print(output)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log_budget(f"main crash: {exc}")
        sys.exit(0)
