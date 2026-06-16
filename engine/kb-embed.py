#!/usr/bin/env python3
"""Embedding-backed retrieval for kb-sync.

Local multilingual ONNX model (fastembed) — no API key, fully offline after first
download. Indexes vault markdown + select jsonl transcript turns. Persists vectors
in numpy + metadata in JSONL + manifest by mtime/sha so reindex is incremental.

Exposed for kb-sync:
  - reindex_vault(vault, store)
  - reindex_transcripts(open_branches, state_dir, projects_dir, store)
  - retrieve_top_k(query, k, scope=..., project=..., branch=..., kind=...)
  - read_md_body(vault, rel_path)

Graceful degradation: get_model() raises EmbeddingsUnavailable if fastembed/numpy
are missing — callers catch and fall back to the legacy (no-retrieve) path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable, Optional


# Canonical, host-agnostic transcript locator lives in kb_config (single source
# of truth). Guarded so the embedding engine still degrades gracefully if the
# sibling module is somehow unavailable.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import kb_config as _kb_config
except Exception:
    _kb_config = None

KB_HOME = Path(os.environ.get("KB_HOME") or (Path.home() / ".kb"))
CACHE_DIR = KB_HOME / "cache" / "kb-embed"
VECTORS_PATH = CACHE_DIR / "vectors.npy"
META_PATH = CACHE_DIR / "meta.jsonl"
MANIFEST_PATH = CACHE_DIR / "manifest.json"

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIM = 384

MAX_CHUNK_CHARS = 6000      # safety cap; model truncates internally at ~512 tokens
JSONL_CHUNK_CHARS = 1500    # per turn-pair chunk


class EmbeddingsUnavailable(RuntimeError):
    pass


_model = None
_np_mod = None


def _np():
    global _np_mod
    if _np_mod is not None:
        return _np_mod
    try:
        import numpy as np
        _np_mod = np
        return np
    except ImportError as e:
        raise EmbeddingsUnavailable(f"numpy not installed: {e}")


def get_model():
    """Lazy-load model. Raises EmbeddingsUnavailable if deps missing."""
    global _model
    if _model is not None:
        return _model
    try:
        from fastembed import TextEmbedding
    except ImportError as e:
        raise EmbeddingsUnavailable(f"fastembed not installed: {e}")
    _model = TextEmbedding(MODEL_NAME)
    return _model


def embed(texts: list[str]):
    """Return L2-normalized float32 (N, DIM) array. Empty input -> (0, DIM)."""
    np = _np()
    if not texts:
        return np.zeros((0, DIM), dtype=np.float32)
    m = get_model()
    vecs = np.array(list(m.embed(texts)), dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vecs / norms).astype(np.float32, copy=False)


def file_sha(path: Path) -> str:
    h = hashlib.sha1()
    try:
        h.update(path.read_bytes())
    except OSError:
        return ""
    return h.hexdigest()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _md_text(path: Path) -> tuple[str, str]:
    """Return (text_for_embedding, preview)."""
    raw = _read_text(path)
    body = raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end >= 0:
            body = raw[end + 4:]
    body = body.strip()
    preview = ""
    for line in body.splitlines():
        ls = line.strip()
        if ls and not ls.startswith("#"):
            preview = ls[:200]
            break
    if not preview:
        for line in body.splitlines():
            ls = line.strip()
            if ls:
                preview = ls[:200]
                break
    return raw[:MAX_CHUNK_CHARS], preview


def _classify_md(rel: str) -> tuple[Optional[str], Optional[str]]:
    """Mirror kb_retrieve.classify_scope locally so we don't import that hook."""
    parts = rel.replace("\\", "/").split("/")
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


class VectorStore:
    """numpy + JSONL + JSON manifest. Append-only with periodic compaction on drop."""

    def __init__(self):
        np = _np()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.np = np
        self.vectors = self._load_vectors()
        self.meta = self._load_meta()
        self.manifest = self._load_manifest()
        # consistency: if vectors and meta disagree, rebuild from scratch
        if self.vectors.shape[0] != len(self.meta):
            self.vectors = np.zeros((0, DIM), dtype=np.float32)
            self.meta = []
            self.manifest = {}

    def _load_vectors(self):
        if VECTORS_PATH.exists():
            try:
                v = self.np.load(str(VECTORS_PATH))
                if v.ndim == 2 and v.shape[1] == DIM:
                    return v.astype(self.np.float32, copy=False)
            except Exception:
                pass
        return self.np.zeros((0, DIM), dtype=self.np.float32)

    def _load_meta(self) -> list[dict]:
        if not META_PATH.exists():
            return []
        out = []
        for line in META_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def _load_manifest(self) -> dict:
        if MANIFEST_PATH.exists():
            try:
                return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def save(self):
        self.np.save(str(VECTORS_PATH), self.vectors)
        with META_PATH.open("w", encoding="utf-8") as f:
            for m in self.meta:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        MANIFEST_PATH.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def _append(self, vec, meta: dict) -> int:
        self.vectors = self.np.vstack([self.vectors, vec.reshape(1, -1)])
        new_id = len(self.meta)
        meta = {**meta, "id": new_id}
        self.meta.append(meta)
        return new_id

    def _drop(self, ids: list[int]):
        if not ids:
            return
        keep = self.np.ones(len(self.meta), dtype=bool)
        for i in ids:
            if 0 <= i < len(self.meta):
                keep[i] = False
        self.vectors = self.vectors[keep]
        id_map = {}
        new_meta = []
        for old_idx, m in enumerate(self.meta):
            if not keep[old_idx]:
                continue
            new_idx = len(new_meta)
            id_map[old_idx] = new_idx
            new_meta.append({**m, "id": new_idx})
        self.meta = new_meta
        for path, info in self.manifest.items():
            old = info.get("chunk_ids", [])
            info["chunk_ids"] = [id_map[c] for c in old if c in id_map]

    def search(self, query_vec, k: int, filter_fn=None):
        if len(self.meta) == 0:
            return []
        sims = self.vectors @ query_vec
        if filter_fn is not None:
            mask = self.np.array([0.0 if filter_fn(m) else -1e9 for m in self.meta],
                                 dtype=self.np.float32)
            sims = sims + mask
        order = self.np.argsort(-sims)
        out = []
        for i in order[:max(k * 2, k)]:
            i = int(i)
            score = float(sims[i])
            if score < -1e6:
                break
            out.append({**self.meta[i], "score": score})
            if len(out) >= k:
                break
        return out


def reindex_vault(vault: Path, store: VectorStore, verbose: bool = False) -> dict:
    """Incremental embed of all KB .md files in the vault."""
    added = updated = removed = skipped = 0
    seen = set()
    candidates = []

    if not vault.exists():
        return {"added": 0, "updated": 0, "removed": 0, "skipped": 0, "total": 0}

    for md in vault.rglob("*.md"):
        if ".obsidian" in md.parts:
            continue
        try:
            rel = str(md.relative_to(vault)).replace("\\", "/")
        except ValueError:
            continue
        scope, project = _classify_md(rel)
        if scope is None:
            continue
        candidates.append((md, rel, scope, project))
        seen.add(rel)

    to_embed = []
    for md, rel, scope, project in candidates:
        try:
            mtime = md.stat().st_mtime
        except OSError:
            continue
        info = store.manifest.get(rel)
        if info and info.get("mtime") == mtime:
            skipped += 1
            continue
        sha = file_sha(md)
        if info and info.get("sha") == sha:
            info["mtime"] = mtime
            skipped += 1
            continue
        full_text, preview = _md_text(md)
        if not full_text.strip():
            continue
        if info:
            store._drop(info.get("chunk_ids", []))
            updated += 1
        else:
            added += 1
        to_embed.append((md, rel, scope, project, sha, mtime, full_text, preview))

    # drop vault files that vanished
    for rel, info in list(store.manifest.items()):
        if info.get("kind", "md") == "md" and rel not in seen:
            store._drop(info.get("chunk_ids", []))
            del store.manifest[rel]
            removed += 1

    if to_embed:
        texts = [t[6] for t in to_embed]
        vecs = embed(texts)
        for (md, rel, scope, project, sha, mtime, full_text, preview), v in zip(to_embed, vecs):
            nid = store._append(v, {
                "path": rel,
                "kind": "md",
                "scope": scope,
                "project": project or "",
                "name": md.stem,
                "preview": preview,
                "text": full_text[:MAX_CHUNK_CHARS],
                "sha": sha,
                "mtime": mtime,
            })
            store.manifest[rel] = {
                "sha": sha, "mtime": mtime, "kind": "md", "chunk_ids": [nid],
            }

    if verbose:
        print(f"  [embed] vault: +{added} ~{updated} -{removed} skip={skipped}")

    return {"added": added, "updated": updated, "removed": removed,
            "skipped": skipped, "total": len(store.meta)}


def reindex_transcripts(open_branches: set[str], state_dir: Path,
                         projects_dir: Path, store: VectorStore,
                         verbose: bool = False) -> dict:
    """Chunk + embed jsonl transcripts for sessions tied to currently-open branches.

    Bounds the scope so we don't index the full history of every session ever."""
    added = updated = removed = skipped = 0
    seen_keys = set()

    sessions = []
    if state_dir.exists():
        for sc in state_dir.glob("kb-session-branch-*.json"):
            try:
                d = json.loads(sc.read_text(encoding="utf-8"))
            except Exception:
                continue
            br = d.get("branch", "")
            if not br or br not in open_branches:
                continue
            sid = d.get("session_id", "")
            cwd = d.get("cwd", "")
            if not sid:
                continue
            # Locate by session_id (deterministic key), not by reconstructing the
            # path from cwd — a mark from a subdirectory encodes to a directory
            # that never existed. cwd-encoding stays only as a fallback.
            jsonl = None
            if _kb_config is not None:
                jsonl = _kb_config.find_session_transcript(sid, projects_dir)
            if (jsonl is None or not jsonl.exists()) and cwd:
                cand = projects_dir / re.sub(r"[:/\\_]", "-", cwd) / f"{sid}.jsonl"
                jsonl = cand if cand.exists() else None
            if not jsonl or not jsonl.exists():
                continue
            sessions.append((sid, cwd, br, jsonl))

    for sid, cwd, branch, jsonl in sessions:
        key = f"transcript:{sid}"
        seen_keys.add(key)
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        info = store.manifest.get(key)
        if info and info.get("mtime") == mtime:
            skipped += 1
            continue
        # naive: drop and rechunk
        if info:
            store._drop(info.get("chunk_ids", []))
            updated += 1
        else:
            added += 1
        chunks = list(_chunk_jsonl(jsonl))
        if not chunks:
            store.manifest[key] = {"mtime": mtime, "kind": "transcript",
                                    "branch": branch, "session_id": sid,
                                    "chunk_ids": []}
            continue
        texts = [c["text"] for c in chunks]
        vecs = embed(texts)
        new_ids = []
        for c, v in zip(chunks, vecs):
            nid = store._append(v, {
                "path": f"{key}:turn{c['idx']}",
                "kind": "transcript",
                "scope": "ticket",
                "project": Path(cwd).name,
                "branch": branch,
                "session_id": sid,
                "turn_idx": c["idx"],
                "preview": c["text"][:200],
                "text": c["text"],
                "mtime": mtime,
            })
            new_ids.append(nid)
        store.manifest[key] = {"mtime": mtime, "kind": "transcript",
                                "branch": branch, "session_id": sid,
                                "chunk_ids": new_ids}

    # drop transcripts whose branch is no longer open (resolved/dead)
    for key, info in list(store.manifest.items()):
        if info.get("kind") == "transcript" and key not in seen_keys:
            store._drop(info.get("chunk_ids", []))
            del store.manifest[key]
            removed += 1

    if verbose:
        print(f"  [embed] transcripts: +{added} ~{updated} -{removed} skip={skipped}")

    return {"added": added, "updated": updated, "removed": removed,
            "skipped": skipped, "sessions": len(sessions)}


def _chunk_jsonl(jsonl: Path) -> Iterable[dict]:
    """Walk Claude Code jsonl, yield {idx, text} for each user+assistant turn pair."""
    buf_user, buf_asst = [], []
    idx = 0
    try:
        with jsonl.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                role = rec.get("type") or rec.get("role")
                if role == "user":
                    if buf_user or buf_asst:
                        text = _pair_to_text(buf_user, buf_asst)
                        if text:
                            yield {"idx": idx, "text": text}
                            idx += 1
                        buf_user, buf_asst = [], []
                    t = _extract_text(rec.get("message") or rec)
                    if t:
                        buf_user.append(t)
                elif role == "assistant":
                    t = _extract_text(rec.get("message") or rec)
                    if t:
                        buf_asst.append(t)
    except OSError:
        return
    if buf_user or buf_asst:
        text = _pair_to_text(buf_user, buf_asst)
        if text:
            yield {"idx": idx, "text": text}


def _pair_to_text(buf_user, buf_asst) -> str:
    parts = []
    if buf_user:
        parts.append("USER:\n" + "\n".join(buf_user))
    if buf_asst:
        parts.append("ASSISTANT:\n" + "\n".join(buf_asst))
    out = "\n".join(parts).strip()
    return out[:JSONL_CHUNK_CHARS]


def _extract_text(rec) -> str:
    if isinstance(rec, str):
        return rec
    if not isinstance(rec, dict):
        return ""
    content = rec.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if not isinstance(blk, dict):
                continue
            t = blk.get("type")
            if t == "text":
                txt = blk.get("text", "")
                if txt:
                    parts.append(txt)
            elif t == "tool_use":
                parts.append(f"[tool_use:{blk.get('name','?')}]")
            elif t == "tool_result":
                out = blk.get("content")
                if isinstance(out, str):
                    parts.append("[tool_result]: " + out[:400])
                elif isinstance(out, list):
                    for sub in out:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            parts.append("[tool_result]: " + (sub.get("text", "")[:400]))
        return "\n".join(parts)
    return ""


def retrieve_top_k(query: str, k: int = 8,
                   scope: Optional[set] = None,
                   project: Optional[str] = None,
                   branch: Optional[str] = None,
                   kind: Optional[set] = None,
                   store: Optional[VectorStore] = None) -> list[dict]:
    """Embed query, return top-K matching chunks with metadata + cosine score.

    Filters narrow the candidate pool before ranking. None = no filter for that dim.
    """
    if store is None:
        store = VectorStore()
    if len(store.meta) == 0:
        return []
    qv = embed([query])[0]

    def filt(m):
        if scope and m.get("scope") not in scope:
            return False
        if project and m.get("project") and m["project"] != project:
            return False
        if branch and m.get("branch") and m["branch"] != branch:
            return False
        if kind and m.get("kind") not in kind:
            return False
        return True

    return store.search(qv, k, filter_fn=filt)


def read_md_body(vault: Path, rel_path: str, max_chars: int = 3000) -> str:
    """Inline read of a vault .md (frontmatter included, capped)."""
    p = vault / rel_path
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    return text[:max_chars]


# ---------- Daemon client (for hooks needing low-latency queries) ----------

DAEMON_HOST = "127.0.0.1"
DAEMON_LOCK = KB_HOME / "state" / "kb-embed-daemon.lock"


def _daemon_port() -> int | None:
    """Read the lockfile and return port if the daemon answers a ping quickly."""
    if not DAEMON_LOCK.exists():
        return None
    try:
        info = json.loads(DAEMON_LOCK.read_text(encoding="utf-8"))
    except Exception:
        return None
    port = info.get("port")
    if not isinstance(port, int):
        return None
    import socket as _sk
    s = _sk.socket()
    s.settimeout(0.4)
    try:
        s.connect((DAEMON_HOST, port))
        s.sendall(b'{"op":"ping"}\n')
        s.recv(64)
        return port
    except OSError:
        return None
    finally:
        s.close()


def daemon_request(payload: dict, timeout: float = 10.0) -> dict | None:
    """One-shot JSON request to the daemon. Returns None if daemon unreachable."""
    port = _daemon_port()
    if port is None:
        return None
    import socket as _sk
    s = _sk.socket()
    s.settimeout(timeout)
    try:
        s.connect((DAEMON_HOST, port))
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
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
        return json.loads(raw.split("\n", 1)[0])
    except OSError:
        return None
    except Exception:
        return None
    finally:
        s.close()


def ensure_daemon(spawn: bool = True) -> bool:
    """Return True iff the daemon is reachable. Optionally spawn it (detached)
    when not alive. Spawning is best-effort and non-blocking — first caller
    pays the load cost via fallback; subsequent callers find the daemon up."""
    if _daemon_port() is not None:
        return True
    if not spawn:
        return False
    import subprocess as _sp
    here = Path(__file__).resolve().parent
    daemon = here / "kb-embed-daemon.py"
    if not daemon.exists():
        return False
    try:
        creationflags = 0
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW = 0x08000000
            creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        _sp.Popen(
            [sys.executable, str(daemon)],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, stdin=_sp.DEVNULL,
            close_fds=True, creationflags=creationflags,
        )
    except Exception:
        return False
    return False  # not yet reachable this call; next call gets it


def daemon_search(query: str, k: int = 10, scope=None, project=None,
                   branch=None, kind=None, min_score: float = 0.0,
                   timeout: float = 10.0) -> list | None:
    """Convenience wrapper. Returns hits list, or None if daemon unreachable."""
    flt = {}
    if scope: flt["scope"] = list(scope)
    if project: flt["project"] = project
    if branch: flt["branch"] = branch
    if kind: flt["kind"] = list(kind)
    if min_score: flt["min_score"] = min_score
    resp = daemon_request({"op": "search", "query": query, "k": k, "filter": flt},
                           timeout=timeout)
    if not resp or not resp.get("ok"):
        return None
    return resp.get("hits") or []


# ---------- CLI for manual reindex + smoke tests ----------

def _cli():
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_idx = sub.add_parser("reindex", help="reindex vault (and optionally transcripts)")
    p_idx.add_argument("--vault", required=True)
    p_idx.add_argument("--state-dir", default=str(KB_HOME / "state"))
    p_idx.add_argument("--projects-dir", default=str(Path.home() / ".claude" / "projects"))
    p_idx.add_argument("--branches", help="comma-separated open branches for transcript indexing")
    p_idx.add_argument("--no-transcripts", action="store_true")

    p_q = sub.add_parser("query", help="run a top-K query")
    p_q.add_argument("query")
    p_q.add_argument("-k", type=int, default=5)
    p_q.add_argument("--scope")
    p_q.add_argument("--project")
    p_q.add_argument("--kind")

    p_st = sub.add_parser("stats", help="store stats")

    args = ap.parse_args()

    if args.cmd == "reindex":
        store = VectorStore()
        r1 = reindex_vault(Path(args.vault), store, verbose=True)
        print(f"  vault: {r1}")
        if not args.no_transcripts and args.branches:
            br = {b.strip() for b in args.branches.split(",") if b.strip()}
            r2 = reindex_transcripts(br, Path(args.state_dir),
                                      Path(args.projects_dir), store, verbose=True)
            print(f"  transcripts: {r2}")
        store.save()
        print(f"  saved {len(store.meta)} chunks in {CACHE_DIR}")
    elif args.cmd == "query":
        scope = {args.scope} if args.scope else None
        kind = {args.kind} if args.kind else None
        res = retrieve_top_k(args.query, k=args.k, scope=scope,
                              project=args.project, kind=kind)
        for r in res:
            print(f"  [{r['score']:.3f}] {r.get('kind','?'):10s} {r.get('scope','?'):9s} {r['path']}")
            if r.get("preview"):
                print(f"            {r['preview'][:120]}")
    elif args.cmd == "stats":
        store = VectorStore()
        kinds = {}
        scopes = {}
        for m in store.meta:
            kinds[m.get("kind", "?")] = kinds.get(m.get("kind", "?"), 0) + 1
            scopes[m.get("scope", "?")] = scopes.get(m.get("scope", "?"), 0) + 1
        print(f"  total chunks: {len(store.meta)}")
        print(f"  vectors:      {store.vectors.shape}")
        print(f"  by kind:      {kinds}")
        print(f"  by scope:     {scopes}")
        print(f"  cache dir:    {CACHE_DIR}")


if __name__ == "__main__":
    _cli()
