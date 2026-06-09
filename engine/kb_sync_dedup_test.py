#!/usr/bin/env python
"""kb_sync_dedup_test.py — unit tests for kb-sync.dedup_scan's pure logic.

Fakes kb_embed (no fastembed/numpy needed), so it runs anywhere. Exercises:
  - chunk -> file aggregation (max score across a file's chunks)
  - self-exclusion and _index.md-neighbour exclusion
  - threshold filtering (recall-biased 0.80 default)
  - pair de-duplication (A<->B counted once, not twice)
  - a_new / b_new flags + twin (both new) vs review (one new) labelling + sort
  - max_pairs cap + descending sort
  - fail-open on EmbeddingsUnavailable / arbitrary error

Run:  python kb_sync_dedup_test.py   (exit 0 all green, 1 on any failure)
"""
import importlib.util as ilu
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_kb_sync():
    spec = ilu.spec_from_file_location("kbs_dedup", HERE / "kb-sync.py")
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeEmbed:
    """Minimal stand-in for the kb_embed module. retrieve_top_k dispatches on the
    query string, which we arrange to equal the source rel via read_md_body."""
    class EmbeddingsUnavailable(RuntimeError):
        pass

    def __init__(self, hits_by_rel, raise_on_retrieve=None):
        self._hits = hits_by_rel
        self._raise = raise_on_retrieve

    class VectorStore:
        pass

    def reindex_vault(self, vault, store, verbose=False):
        return {}

    def read_md_body(self, vault, rel, max_chars=2000):
        # body IS the rel (non-empty) so retrieve_top_k can dispatch on it
        return "" if rel.endswith("__empty__.md") else rel

    def retrieve_top_k(self, query, k=6, kind=None, store=None, **kw):
        if self._raise:
            raise self._raise
        return list(self._hits.get(query, []))


class FakeReport:
    def __init__(self, touched_paths):
        self._touched = touched_paths

    def changed_files(self, vault):
        return list(self._touched)


VAULT = Path("/v")


def P(rel):
    return VAULT / rel


def run(mod, touched_rels, hits_by_rel, raise_on_retrieve=None, **kw):
    mod.kb_embed = FakeEmbed(hits_by_rel, raise_on_retrieve)
    report = FakeReport([P(r) for r in touched_rels])
    return mod.dedup_scan(report, VAULT, **kw)


FAILS = []


def check(name, cond, detail=""):
    mark = "\033[32mPASS\033[0m" if cond else "\033[31mFAIL\033[0m"
    print(f"  {mark}  {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    mod = load_kb_sync()
    A = "ws/proj/Learnings/a.md"
    B = "ws/proj/Learnings/b.md"
    C = "ws/proj/Learnings/c.md"
    IDX = "ws/proj/_index.md"

    # 1. chunk->file max aggregation: two chunks of B, keep the max (0.91)
    out = run(mod, [A], {A: [{"path": B, "score": 0.71}, {"path": B, "score": 0.91}]})
    check("aggregate max across chunks", len(out) == 1 and out[0]["score"] == 0.91, str(out))

    # 2. self-exclusion: a hit on the source file itself is dropped
    out = run(mod, [A], {A: [{"path": A, "score": 0.99}]})
    check("self excluded", out == [], str(out))

    # 3. _index neighbour excluded (legit overlap with its own learnings)
    out = run(mod, [A], {A: [{"path": IDX, "score": 0.95}]})
    check("_index neighbour excluded", out == [], str(out))

    # 4a. below threshold dropped
    out = run(mod, [A], {A: [{"path": B, "score": 0.79}]})
    check("below threshold dropped", out == [], str(out))
    # 4b. at threshold kept
    out = run(mod, [A], {A: [{"path": B, "score": 0.80}]})
    check("at threshold kept", len(out) == 1, str(out))

    # 5. pair de-dup: A->B and B->A both touched => one pair
    out = run(mod, [A, B], {A: [{"path": B, "score": 0.88}],
                            B: [{"path": A, "score": 0.88}]})
    check("pair counted once", len(out) == 1, str(out))

    # 6. a_new/b_new: A touched, B not -> a_new True, b_new False (sorted a<b => a.md is 'a')
    out = run(mod, [A], {A: [{"path": B, "score": 0.88}]})
    d = out[0]
    check("new-side flags", d["a"] == A and d["a_new"] and not d["b_new"], str(d))

    # 7. cap + sort desc
    hits = {A: [{"path": f"ws/proj/Learnings/n{i}.md", "score": 0.80 + i * 0.001}
                for i in range(10)]}
    out = run(mod, [A], hits, max_pairs=3)
    desc = all(out[i]["score"] >= out[i + 1]["score"] for i in range(len(out) - 1))
    check("max_pairs cap", len(out) == 3, str(len(out)))
    check("sorted desc", desc, str([d["score"] for d in out]))

    # 8. fail-open on EmbeddingsUnavailable
    mod.kb_embed = FakeEmbed({}, raise_on_retrieve=FakeEmbed.EmbeddingsUnavailable("down"))
    out = mod.dedup_scan(FakeReport([P(A)]), VAULT)
    check("fail-open on EmbeddingsUnavailable", out == [], str(out))

    # 9. fail-open on arbitrary error
    mod.kb_embed = FakeEmbed({}, raise_on_retrieve=ValueError("boom"))
    out = mod.dedup_scan(FakeReport([P(A)]), VAULT)
    check("fail-open on generic error", out == [], str(out))

    # 10. no touched learnings -> [] (ignores _index-only changes)
    out = run(mod, [IDX], {})
    check("no learnings touched -> empty", out == [], str(out))

    # 11. empty body skipped (no crash)
    EMPTY = "ws/proj/Learnings/__empty__.md"
    out = run(mod, [EMPTY], {EMPTY: [{"path": B, "score": 0.99}]})
    check("empty body skipped", out == [], str(out))

    # 12. kind label: both sides touched -> twin; one side touched -> review
    out = run(mod, [A, B], {A: [{"path": B, "score": 0.85}]})  # B also touched
    check("both-new -> twin", out and out[0]["kind"] == "twin", str(out))
    out = run(mod, [A], {A: [{"path": B, "score": 0.85}]})      # B not touched
    check("one-new -> review", out and out[0]["kind"] == "review", str(out))

    # 13. twins sort before a higher-scoring review pair
    out = run(mod, [A, B], {
        A: [{"path": B, "score": 0.81}],                        # twin (both new), lower score
        B: [{"path": C, "score": 0.95}],                        # review (C not new), higher score
    })
    check("twin sorts before higher review",
          len(out) == 2 and out[0]["kind"] == "twin" and out[1]["kind"] == "review",
          str([(d["kind"], d["score"]) for d in out]))

    print()
    if FAILS:
        print(f"\033[31m{len(FAILS)} failed\033[0m: {', '.join(FAILS)}")
        sys.exit(1)
    print("\033[32mall green\033[0m")
    sys.exit(0)


if __name__ == "__main__":
    main()
