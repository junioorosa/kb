#!/usr/bin/env python3
"""kb_retrieve_eval_test.py — retrieval relevance harness (the ruler).

This is NOT a unit test of one function — it measures end-to-end retrieval
QUALITY: given a query, does the right learning land in the top-k? It exists so
any change to the retrieval mechanism OR to vault content (notably a future
consolidation pass that rewrites learnings) can be checked for regressions
before it ships — the same "measure before/after" discipline that retired the
LLM reranker.

How it works:
  * Builds a SYNTHETIC vault in a temp dir (workspace "Acme", projects
    "acme-payments" and "ledger") — generic engineering learnings, zero real
    identifiers, so the whole rig can live in the public repo and run anywhere.
  * Drives the REAL engine (kb_retrieve.py) as a subprocess against that vault,
    exactly as the Claude Code hook does: a prompt JSON on stdin, the
    <vault-context> block on stdout.
  * Scores hit@1 / hit@3 / hit@5 against a hand-curated answer key (EVAL_SET).

Determinism: runs the BM25-only path (KB_FAST_MODE=1) — no embedding daemon, no
fastembed, no network. That is the reproducible floor that gates CI; the hybrid
embedding path only adds recall on top of it. The PT stemmer may or may not be
installed, so the gate asserts an AGGREGATE hit-rate floor, not per-query
exactness — minor stemming variance can't flake the build.

Run: python engine/kb_retrieve_eval_test.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "kb_retrieve.py"

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


# ===========================================================================
# Synthetic vault. Each entry: rel_path -> {fm: {...frontmatter...}, body: str}.
# The BM25 document is name + tags + module + description + project, so the
# `description` carries the searchable substance of each learning.
# ===========================================================================

def L(description, tags, module="", scope="ticket"):
    return {"fm": {"description": description, "tags": tags, "module": module,
                   "scope": scope}, "body": description}


def IDX(title, status):
    return {"fm": {"title": title, "status": status}, "body": title}


FIXTURES = {
    # --- workspace scope -----------------------------------------------------
    "Acme/Learnings/idempotency-key-on-retry.md": L(
        "Retry de chamada externa precisa de chave de idempotencia senao duplica pedido no provider",
        ["idempotency", "retry", "http"], scope="workspace"),
    "Acme/Learnings/db-connection-pool-exhaustion.md": L(
        "Pool de conexoes esgota quando uma transacao longa segura a conexao; usar timeout de pool",
        ["database", "pool", "transaction"], scope="workspace"),
    "Acme/Learnings/jpa-criteria-partial-string-filter.md": L(
        "Filtro de substring parcial com JPA Criteria usa like com curinga dos dois lados do termo",
        ["jpa", "criteria", "filter"], scope="workspace"),

    # --- project acme-payments ----------------------------------------------
    "Acme/acme-payments/Learnings/webhook-signature-verification.md": L(
        "Verificar assinatura HMAC do webhook antes de processar e rejeitar timestamp velho replay",
        ["webhook", "hmac", "security"], scope="project"),
    "Acme/acme-payments/fix/233-timeout-gateway/_index.md": IDX("Timeout no gateway", "resolved"),
    "Acme/acme-payments/fix/233-timeout-gateway/Learnings/payment-gateway-socket-timeout.md": L(
        "Gateway de pagamento sem socket timeout trava a fila de cobranca; usar 15s connect 60s read",
        ["timeout", "gateway", "payment"]),
    "Acme/acme-payments/feat/250-installments/_index.md": IDX("Parcelamento", "resolved"),
    "Acme/acme-payments/feat/250-installments/Learnings/installment-rounding-last-parcel.md": L(
        "Arredondamento de parcela joga o residuo dos centavos na ultima parcela em vez de distribuir",
        ["installments", "rounding"]),
    "Acme/acme-payments/feat/214-pix-refund/_index.md": IDX("Estorno PIX", "open"),
    "Acme/acme-payments/feat/214-pix-refund/Learnings/pix-refund-partial-amount.md": L(
        "Estorno parcial de PIX exige valor menor ou igual ao original senao o banco central rejeita",
        ["pix", "refund", "estorno"]),

    # --- project ledger ------------------------------------------------------
    "Acme/ledger/Learnings/scheduler-single-thread-default.md": L(
        "TaskScheduler default do Spring tem apenas uma thread; usar pool dedicado para os Scheduled",
        ["scheduler", "spring", "threadpool"], scope="project"),
    "Acme/ledger/Learnings/kafka-consumer-duplicate-on-rebalance.md": L(
        "Rebalance do Kafka reentrega mensagem; o consumidor precisa ser idempotente por offset",
        ["kafka", "consumer", "rebalance"], scope="project"),
    "Acme/ledger/Learnings/postgres-camelcase-alias-quotes.md": L(
        "Alias camelCase no Postgres exige aspas duplas senao o nome vira lowercase e quebra o mapper",
        ["postgres", "sql", "alias"], scope="project"),
    "Acme/ledger/fix/301-orphan-rows/_index.md": IDX("Linhas orfas", "resolved"),
    "Acme/ledger/fix/301-orphan-rows/Learnings/processing-flag-orphan-on-crash.md": L(
        "Flag em_processamento setada antes da chamada externa deixa linha orfa se a instancia cai",
        ["orphan", "startup", "queue"]),
    "Acme/ledger/feat/325-iso8601-parse/_index.md": IDX("Parse de data", "resolved"),
    "Acme/ledger/feat/325-iso8601-parse/Learnings/iso8601-literal-t-separator.md": L(
        "Data ISO 8601 tem o T literal como separador e nao um espaco; o parser quebra com formato errado",
        ["date", "iso8601", "parsing"]),

    # --- status-weight fixtures (NOT in the hit-rate set) --------------------
    # A normal/experimental matched pair: near-identical text, only status
    # differs, so a query hits both and the down-weight must order them.
    "Acme/ledger/feat/318-rate-limit/_index.md": IDX("Rate limit", "resolved"),
    "Acme/ledger/feat/318-rate-limit/Learnings/backoff-progressive-ratelimit.md": L(
        "Backoff progressivo quando o provider externo devolve quatrocentos e vinte nove rate limit excedido",
        ["backoff", "ratelimit"]),
    "Acme/ledger/feat/340-exp-backoff/_index.md": IDX("Backoff experimental", "experimental"),
    "Acme/ledger/feat/340-exp-backoff/Learnings/backoff-experimental-variant.md": L(
        "Backoff progressivo quando o provider externo devolve quatrocentos e vinte nove rate limit excedido",
        ["backoff", "ratelimit"]),
    # A discarded approach: weight 0.0 -> must never be cited.
    "Acme/acme-payments/feat/199-discarded/_index.md": IDX("Abordagem descartada", "discarded"),
    "Acme/acme-payments/feat/199-discarded/Learnings/discarded-double-write-sync.md": L(
        "Sincronizacao por double write simultaneo entre Oracle e Postgres no commit da transacao distribuida",
        ["sync", "oracle", "distributed"]),
}


# (query, expected path fragment) — one intended target each, chosen for strong
# lexical overlap so BM25 alone ranks it at or near the top regardless of stem.
EVAL_SET = [
    ("retry de chamada externa duplica pedido sem chave de idempotencia", "idempotency-key-on-retry"),
    ("pool de conexoes esgota com transacao longa segurando conexao", "db-connection-pool-exhaustion"),
    ("filtro de substring parcial com jpa criteria usando like curinga", "jpa-criteria-partial-string-filter"),
    ("verificar assinatura hmac do webhook e rejeitar replay velho", "webhook-signature-verification"),
    ("gateway de pagamento sem socket timeout trava a fila de cobranca", "payment-gateway-socket-timeout"),
    ("arredondamento de parcela joga residuo na ultima parcela", "installment-rounding-last-parcel"),
    ("estorno parcial de pix valor menor ou igual ao original", "pix-refund-partial-amount"),
    ("taskscheduler default do spring uma thread usar pool dedicado", "scheduler-single-thread-default"),
    ("rebalance do kafka reentrega mensagem consumidor idempotente offset", "kafka-consumer-duplicate-on-rebalance"),
    ("alias camelcase no postgres exige aspas duplas senao lowercase", "postgres-camelcase-alias-quotes"),
    ("flag em processamento deixa linha orfa quando instancia cai", "processing-flag-orphan-on-crash"),
    ("data iso 8601 separador t literal parser quebra formato", "iso8601-literal-t-separator"),
]


def build_vault(root: Path) -> None:
    for rel, spec in FIXTURES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        fm = spec["fm"]
        lines = ["---"]
        for k, v in fm.items():
            if isinstance(v, list):
                lines.append(f"{k}: [{', '.join(v)}]")
            else:
                lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("")
        lines.append(spec["body"])
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_retrieve(prompt: str, vault: Path, kb_home: Path, session: str):
    """Drive kb_retrieve.py exactly like the hook: prompt JSON on stdin,
    <vault-context> on stdout. BM25-only (KB_FAST_MODE=1) for determinism."""
    env = dict(os.environ)
    env["KB_VAULT"] = str(vault)
    env["KB_HOME"] = str(kb_home)
    env["KB_FAST_MODE"] = "1"      # BM25-only: skip the embedding daemon
    env["KB_BRANCH"] = ""
    env["HOME"] = str(kb_home.parent)
    env["USERPROFILE"] = str(kb_home.parent)
    payload = json.dumps({"prompt": prompt, "session_id": session})
    p = subprocess.run([sys.executable, str(SCRIPT)], input=payload,
                       capture_output=True, text=True, env=env, timeout=60,
                       encoding="utf-8", errors="replace")
    return p.stdout or ""


_CITE = re.compile(r"^\s*-\s*\[\[([^\]]+)\]\]", re.M)


def cited_paths(output: str):
    return [m.group(1).strip() for m in _CITE.finditer(output)][:10]


def position_of(fragment: str, paths):
    for i, p in enumerate(paths):
        if fragment.lower() in p.lower():
            return i + 1  # 1-based
    return None


def main() -> int:
    if not SCRIPT.exists():
        print(f"FAIL: {SCRIPT} not found")
        return 1

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        vault = root / "vault"
        kb_home = root / "home" / ".kb"
        kb_home.mkdir(parents=True, exist_ok=True)
        build_vault(vault)

        # --- relevance gate --------------------------------------------------
        print("test_hit_rate (BM25-only, synthetic vault)")
        h1 = h3 = h5 = 0
        n = len(EVAL_SET)
        misses = []
        for i, (query, expected) in enumerate(EVAL_SET):
            out = run_retrieve(query, vault, kb_home, f"eval-{i}")
            pos = position_of(expected, cited_paths(out))
            if pos is not None:
                h5 += 1
                if pos <= 3:
                    h3 += 1
                if pos == 1:
                    h1 += 1
            else:
                misses.append((query, expected, cited_paths(out)[:3]))

        print(f"  hit@1 {h1}/{n}  hit@3 {h3}/{n}  hit@5 {h5}/{n}")
        for q, exp, got in misses:
            print(f"    MISS  '{q[:50]}' expected {exp} | got {[Path(g).name for g in got]}")

        # Floor, not exactness: tolerant of stemmer presence/absence.
        check("hit@3 >= 11/12", h3 >= 11, f"got {h3}/{n}")
        check("hit@1 >= 9/12", h1 >= 9, f"got {h1}/{n}")
        check("hit@5 == 12/12 (every target retrievable)", h5 == n, f"got {h5}/{n}")

        # --- status weighting ------------------------------------------------
        print("test_status_weighting")
        out = run_retrieve(
            "backoff progressivo quando o provider devolve quatrocentos e vinte nove rate limit",
            vault, kb_home, "eval-status")
        paths = cited_paths(out)
        normal_pos = position_of("backoff-progressive-ratelimit", paths)
        exp_pos = position_of("backoff-experimental-variant", paths)
        check("normal learning is retrieved", normal_pos is not None)
        check("experimental sibling down-weighted below the normal one",
              normal_pos is not None and (exp_pos is None or normal_pos < exp_pos),
              f"normal@{normal_pos} exp@{exp_pos}")

        out = run_retrieve(
            "sincronizacao por double write simultaneo entre oracle e postgres distribuida",
            vault, kb_home, "eval-discarded")
        paths = cited_paths(out)
        check("discarded learning (weight 0) is never cited",
              position_of("discarded-double-write-sync", paths) is None,
              f"got {[Path(p).name for p in paths[:3]]}")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
