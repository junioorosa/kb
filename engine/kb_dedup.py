#!/usr/bin/env python3
"""kb_dedup.py — auto-resolve twin learnings (the dedup axis).

verify-by-ablation (kb_verify) asks, per note, "would a model know this without it?".
That is ORTHOGONAL to "is this note the same as another note?" — a GOLD twin (a
domain fact stated in two files) passes ablation on both sides and the duplication
survives. Catching that needs a PAIRWISE comparison, which only embedding similarity
gives. This module is that axis.

It reuses the calibrated detection of `dedup_scan` (cosine over learning bodies via
kb_embed) for RECALL, then gates each candidate pair through an LLM for PRECISION and
resolves it:

  cosine >= threshold  ->  LLM: same core insight?
      yes  -> MERGE into ONE note, folding EVERY unique fact from both (non-lossy),
              delete the redundant file, repoint its inbound [[links]] to the keeper.
      no   -> KEEP_BOTH (same area, different fact) — no change.

Why auto (no human review, unlike consolidate): a confirmed exact/near twin is not a
judgement call — "twin is twin". The human gate stays for the genuinely subjective
consolidate work (contradiction resolution, scope promotion, staleness deletion).

Safety — the vault is the sensitive point:
  * NON-LOSSY merge. The original `dedup_scan` refused to merge because "the unique
    delta on the duplicate side can be the thing worth keeping". We honor that: the
    merge prompt MUST preserve every unique fact from both sides; we never blind-delete
    a file that carries content the keeper lacks.
  * CONSERVATIVE. The LLM defaults to KEEP_BOTH on any doubt; an unparseable or failed
    call is KEEP_BOTH (never a destructive guess). Cosine only nominates; the LLM (and
    its bias to keep) decides.
  * DETERMINISTIC keeper. The survivor is chosen by inbound-link count then body length
    — not "best guess" — so a re-run is stable and link repointing is minimized.
  * AUDITABLE. The survivor gets a `## Dedup history` trace naming both sources.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from datetime import date
from pathlib import Path


DEFAULT_MAX_TURNS = 10
THRESHOLD = 0.80            # cross-folder cosine bar (mirrors dedup_scan review bar)
SAME_DIR_THRESHOLD = 0.72   # same Learnings/ dir — likelier a real twin (mirrors dedup_scan)
DEFAULT_DEDUP_CAP = 20      # pairs resolved per run (batch sweep)


def _load_kb_embed():
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("kb_embed", here / "kb-embed.py")
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


kb_embed = _load_kb_embed()


def claude_run(prompt: str, max_turns: int = DEFAULT_MAX_TURNS, timeout: int = 300):
    cmd = ["claude", "--print", "--max-turns", str(max_turns), "--dangerously-skip-permissions"]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except Exception as e:  # noqa: BLE001 - any failure is non-fatal -> KEEP_BOTH
        return 1, "", f"{type(e).__name__}: {e}"


# --- helpers -----------------------------------------------------------------

def is_learning(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    return "/Learnings/" in rel and rel.endswith(".md") and not rel.endswith("/_index.md")


def learnings_dir(rel: str) -> str | None:
    rel = rel.replace("\\", "/")
    i = rel.rfind("/Learnings/")
    return rel[:i] + "/Learnings" if i != -1 else None


def _read(vault: Path, rel: str) -> str:
    try:
        return (vault / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def inbound_link_count(vault: Path, slug: str) -> int:
    """How many OTHER files reference `[[slug]]` (basename, ignoring path/alias). Used to
    pick the keeper so we repoint as few links as possible."""
    n = 0
    pat = re.compile(r"\[\[(?:[^\]\|]*/)?" + re.escape(slug) + r"(?:\||\]\])")
    for md in vault.rglob("*.md"):
        if ".obsidian" in md.parts or ".git" in md.parts:
            continue
        if md.stem == slug:
            continue
        try:
            if pat.search(md.read_text(encoding="utf-8", errors="ignore")):
                n += 1
        except OSError:
            continue
    return n


def pick_keeper(vault: Path, a_rel: str, b_rel: str) -> tuple[str, str]:
    """(keeper_rel, dropped_rel): more inbound links wins; tie -> longer body; tie -> path order."""
    a_slug, b_slug = Path(a_rel).stem, Path(b_rel).stem
    a_in, b_in = inbound_link_count(vault, a_slug), inbound_link_count(vault, b_slug)
    if a_in != b_in:
        return (a_rel, b_rel) if a_in > b_in else (b_rel, a_rel)
    a_len, b_len = len(_read(vault, a_rel)), len(_read(vault, b_rel))
    if a_len != b_len:
        return (a_rel, b_rel) if a_len > b_len else (b_rel, a_rel)
    return tuple(sorted([a_rel, b_rel]))  # deterministic


# --- detection (recall): nominate candidate twin pairs via cosine -------------

def find_twin_pairs(vault: Path, candidate_rels: list[str], store=None,
                    threshold: float = THRESHOLD,
                    same_dir_threshold: float = SAME_DIR_THRESHOLD) -> list[dict]:
    """For each candidate learning, its near neighbours (cosine >= bar) — each pair is a
    twin SUSPECT the LLM then confirms or rejects. Reuses kb_embed exactly like
    dedup_scan. Returns [{a, b, score}] sorted by score desc. Empty if embeddings down."""
    if kb_embed is None:
        return []
    cands = [r.replace("\\", "/") for r in candidate_rels if is_learning(r)]
    if not cands:
        return []
    try:
        if store is None:
            store = kb_embed.VectorStore()
            kb_embed.reindex_vault(vault, store, verbose=False)
        seen, out = set(), []
        for rel in cands:
            body = kb_embed.read_md_body(vault, rel, max_chars=2000)
            if not body.strip():
                continue
            best: dict[str, float] = {}
            for h in kb_embed.retrieve_top_k(body, k=6, kind={"md"}, store=store):
                hp = (h.get("path") or "").replace("\\", "/")
                if not hp or hp == rel or hp.endswith("_index.md") or not is_learning(hp):
                    continue
                best[hp] = max(best.get(hp, 0.0), float(h.get("score", 0.0)))
            for hp, s in best.items():
                same_dir = learnings_dir(rel) is not None and learnings_dir(rel) == learnings_dir(hp)
                if s < (same_dir_threshold if same_dir else threshold):
                    continue
                pair = tuple(sorted([rel, hp]))
                if pair in seen:
                    continue
                seen.add(pair)
                out.append({"a": pair[0], "b": pair[1], "score": round(s, 3)})
        out.sort(key=lambda d: -d["score"])
        return out
    except getattr(kb_embed, "EmbeddingsUnavailable", Exception):
        return []
    except Exception:
        return []


# --- resolution (precision): LLM confirms twin and folds non-lossy ------------

_RESOLVE = """Two knowledge notes from the same project may be duplicates. Decide and, if so, merge them NON-LOSSILY.

KEEPER note — path `<<KPATH>>`:
<<KBODY>>

OTHER note — path `<<DPATH>>`:
<<DBODY>>

Do they state the SAME core insight (a twin / near-duplicate), or are they DISTINCT (same area, different fact)?
- If DISTINCT or you are unsure -> output exactly: DECISION: KEEP_BOTH
- If SAME core insight -> output the MERGED note for the KEEPER, folding in EVERY unique fact, nuance, code symbol, gotcha and wikilink from BOTH (lossless — do not drop anything the OTHER had). Keep the keeper's frontmatter (merge tags). Write the prose in the same language as the notes.

Output format EXACTLY:
DECISION: MERGE
---BODY---
<full merged markdown of the keeper note, including its --- frontmatter --- block>
---END---

(or just `DECISION: KEEP_BOTH` with nothing else). Be conservative: when in doubt, KEEP_BOTH."""


def _fill(tmpl: str, **kw) -> str:
    out = tmpl
    for k, v in kw.items():
        out = out.replace("<<" + k + ">>", v)
    return out


def _parse_resolution(stdout: str) -> dict | None:
    """-> {'decision': 'KEEP_BOTH'} or {'decision': 'MERGE', 'body': '<md>'}; None if
    unparseable (caller treats None as KEEP_BOTH)."""
    if re.search(r"(?mi)^\s*DECISION:\s*KEEP_BOTH\b", stdout):
        return {"decision": "KEEP_BOTH"}
    if re.search(r"(?mi)^\s*DECISION:\s*MERGE\b", stdout):
        m = re.search(r"---BODY---\s*\n(.*?)\n---END---", stdout, re.S)
        if m and m.group(1).strip():
            return {"decision": "MERGE", "body": m.group(1).strip() + "\n"}
        return None  # MERGE claimed but no usable body -> conservative KEEP_BOTH
    return None


def repoint_links(vault: Path, old_slug: str, new_slug: str) -> int:
    """Rewrite every `[[...old_slug]]` / `[[...old_slug|alias]]` to point at new_slug,
    preserving any alias. Returns files changed."""
    changed = 0
    # [[ <optional path/> old_slug <optional |alias> ]] — slug anchored at start or after "/"
    pat = re.compile(r"\[\[(?:[^\]\|]*/)?" + re.escape(old_slug) + r"(\|[^\]]*)?\]\]")
    for md in vault.rglob("*.md"):
        if ".obsidian" in md.parts or ".git" in md.parts:
            continue
        try:
            txt = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        new = pat.sub(lambda m: f"[[{new_slug}{m.group(1) or ''}]]", txt)
        if new != txt:
            try:
                md.write_text(new, encoding="utf-8")
                changed += 1
            except OSError:
                pass
    return changed


def _add_dedup_trace(body: str, keeper_rel: str, dropped_rel: str) -> str:
    line = (f"- {date.today().isoformat()} merged `{Path(dropped_rel).stem}` into this note "
            f"(kb_dedup, twin). Sources: `{keeper_rel}` + `{dropped_rel}`.")
    if "## Dedup history" in body:
        return body.rstrip() + "\n" + line + "\n"
    return body.rstrip() + "\n\n## Dedup history\n" + line + "\n"


def resolve_pair(vault: Path, a_rel: str, b_rel: str, max_turns: int = DEFAULT_MAX_TURNS,
                 dry_run: bool = False) -> dict:
    """Confirm + resolve one suspect pair. Returns {action, keeper, dropped, reason}.
    action in {'merged','keep_both','error','dry'}. Conservative: any failure -> keep_both."""
    keeper, dropped = pick_keeper(vault, a_rel, b_rel)
    kbody, dbody = _read(vault, keeper), _read(vault, dropped)
    base = {"keeper": keeper, "dropped": dropped, "reason": ""}
    if not kbody.strip() or not dbody.strip():
        return {**base, "action": "keep_both", "reason": "a side is empty/unreadable"}
    if dry_run:
        return {**base, "action": "dry", "reason": "would ask LLM to confirm twin + merge"}

    rc, out, err = claude_run(
        _fill(_RESOLVE, KPATH=keeper, KBODY=kbody, DPATH=dropped, DBODY=dbody), max_turns)
    if rc != 0:
        return {**base, "action": "error", "reason": f"resolve call failed: {err[:120]}"}
    parsed = _parse_resolution(out)
    if parsed is None or parsed["decision"] == "KEEP_BOTH":
        return {**base, "action": "keep_both", "reason": "LLM: distinct or unsure (conservative)"}

    # MERGE: write keeper with folded body, delete dropped, repoint its inbound links.
    merged = _add_dedup_trace(parsed["body"], keeper, dropped)
    try:
        (vault / keeper).write_text(merged, encoding="utf-8")
    except OSError as e:
        return {**base, "action": "error", "reason": f"keeper write failed: {e}"}
    repointed = repoint_links(vault, Path(dropped).stem, Path(keeper).stem)
    try:
        (vault / dropped).unlink()
    except OSError as e:
        return {**base, "action": "error", "reason": f"dropped unlink failed: {e}"}
    return {**base, "action": "merged", "reason": f"folded; repointed {repointed} link-file(s)"}


def dedup_paths(vault: Path, candidate_rels: list[str], store=None, cap: int | None = None,
                dry_run: bool = False, log=print) -> dict:
    """Detect (cosine) + resolve (LLM) twins among candidate learnings. A file already
    merged/deleted this run is skipped if it resurfaces in a later pair. Returns
    {pairs, merged:[...], kept, errors, deferred}."""
    pairs = find_twin_pairs(vault, candidate_rels, store=store)
    deferred = []
    if cap is not None and len(pairs) > cap:
        deferred = pairs[cap:]
        pairs = pairs[:cap]
    gone: set = set()
    merged, kept, errors = [], 0, 0
    for p in pairs:
        if p["a"] in gone or p["b"] in gone:
            continue
        r = resolve_pair(vault, p["a"], p["b"], dry_run=dry_run)
        if r["action"] == "merged":
            log(f"  [dedup] merged {r['dropped']} -> {r['keeper']} (cos={p['score']}; {r['reason']})")
            gone.add(r["dropped"])
            merged.append(r)
        elif r["action"] == "dry":
            log(f"  [dedup] WOULD CHECK {p['a']} ~ {p['b']} (cos={p['score']})")
        elif r["action"] == "error":
            errors += 1
            log(f"  [dedup] kept (check failed, conservative): {p['a']} ~ {p['b']} — {r['reason']}")
        else:
            kept += 1
    if deferred:
        log(f"  [dedup] deferred {len(deferred)} pair(s) over cap={cap}")
    return {"pairs": len(pairs), "merged": merged, "kept": kept, "errors": errors, "deferred": deferred}


# --- batch sweep CLI ---------------------------------------------------------

def _gather_workspace_learnings(vault: Path, workspace: str) -> list[str]:
    out = []
    root = vault / workspace
    if not root.is_dir():
        return out
    for md in root.rglob("*.md"):
        if ".obsidian" in md.parts:
            continue
        rel = str(md.relative_to(vault)).replace("\\", "/")
        if is_learning(rel):
            out.append(rel)
    return sorted(out)


def main() -> int:
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import kb_config
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: kb_config not importable: {e}", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(description="auto-resolve twin learnings (embedding + LLM, non-lossy merge).")
    ap.add_argument("--workspace", help="workspace to sweep (default: the only one)")
    ap.add_argument("--project", help="limit to one project (folder name)")
    ap.add_argument("--cap", type=int, default=DEFAULT_DEDUP_CAP, help=f"max pairs this run (default {DEFAULT_DEDUP_CAP})")
    ap.add_argument("--dry-run", action="store_true", help="detect + report suspects; never merge/delete")
    args = ap.parse_args()
    try:
        vault = Path(kb_config.resolve_vault(strict=True))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: vault unresolved ({e}).", file=sys.stderr)
        return 2
    cfg = kb_config.load_config()
    workspaces = cfg.get("workspaces", []) or []
    ws = args.workspace or (workspaces[0].get("name") if len(workspaces) == 1 else None)
    if not ws:
        print(f"ERROR: multiple workspaces; pass --workspace (one of {[w.get('name') for w in workspaces]})", file=sys.stderr)
        return 2
    rels = _gather_workspace_learnings(vault, ws)
    if args.project:
        rels = [r for r in rels if r.split("/")[1:2] == [args.project] or (len(r.split("/")) > 1 and r.split("/")[1] == args.project)]
    if not rels:
        print(f"No learnings under '{ws}'.")
        return 0
    print(f"dedup: {len(rels)} learning(s) under '{ws}'" + (f"/{args.project}" if args.project else "")
          + (", DRY-RUN" if args.dry_run else ""))
    res = dedup_paths(vault, rels, cap=args.cap, dry_run=args.dry_run)
    print(f"\nsuspect-pairs={res['pairs']} merged={len(res['merged'])} kept={res['kept']} "
          f"errors={res['errors']} deferred={len(res['deferred'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
