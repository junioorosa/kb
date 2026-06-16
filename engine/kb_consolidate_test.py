#!/usr/bin/env python3
"""Tests for kb-consolidate — the consolidation policy + plumbing.

The audit policy lives in the prompt text (nothing else enforces it), so the
contract tests pin its load-bearing clauses: non-destructive/branch-only,
merge-dups, contradiction resolved-by-landed, staleness keep-in-doubt, promotion
suggest-only, deterministic paths, consolidation-history trace, no MCP. Plumbing
tests cover scope gather, symbol extraction, freshness grep against a real git
repo, and a hermetic --dry-run that must create no branch and invoke no LLM.

Run: python engine/kb_consolidate_test.py
Synthetic vault + throwaway git repos only; no real vault, no LLM, no network.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("kb_consolidate", HERE / "kb-consolidate.py")
kc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kc)

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def flat(s: str) -> str:
    return " ".join(s.split())


def git(repo, *args):
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=str(repo),
                          capture_output=True, text=True)


def write(p: Path, body: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def learning(desc, tags="[a, b]", body_extra=""):
    return f"---\ndescription: {desc}\ntags: {tags}\nscope: ticket\n---\n\n{desc}\n{body_extra}\n"


def build_vault(root: Path) -> Path:
    vault = root / "vault"
    # project payments
    write(vault / "Pauta/payments/Learnings/idempotency-retry.md",
          learning("Retry externo precisa de chave de idempotencia", body_extra="Ver `PaymentRetryService`."))
    write(vault / "Pauta/payments/fix/233-timeout/_index.md",
          "---\ntitle: Timeout\nstatus: resolved\n---\n")
    write(vault / "Pauta/payments/fix/233-timeout/Learnings/socket-timeout.md",
          learning("Gateway sem socket timeout trava a fila", body_extra="`GatewayClient.setReadTimeout`"))
    # project ledger
    write(vault / "Pauta/ledger/Learnings/kafka-idempotency.md",
          learning("Rebalance do Kafka reentrega; consumidor idempotente por offset"))
    write(vault / "Pauta/ledger/feat/300-x/_index.md", "---\ntitle: X\nstatus: open\n---\n")
    write(vault / "Pauta/ledger/feat/300-x/Learnings/orphan-rows.md",
          learning("Flag em_processamento deixa linha orfa no crash"))
    # a non-project, workspace-scope learning (must not become a 'project')
    write(vault / "Pauta/Learnings/workspace-note.md", learning("Convencao geral do time"))
    return vault


# --- scope gather ------------------------------------------------------------

def test_gather():
    print("test_gather")
    with tempfile.TemporaryDirectory() as d:
        vault = build_vault(Path(d))
        projects = kc.gather_workspace(vault, "Pauta")
        names = [p["project"] for p in projects]
        check("two projects with learnings", names == ["ledger", "payments"], f"got {names}")
        pay = next(p for p in projects if p["project"] == "payments")
        check("ticket + project learnings gathered, _index excluded",
              "Pauta/payments/Learnings/idempotency-retry.md" in pay["learnings"]
              and "Pauta/payments/fix/233-timeout/Learnings/socket-timeout.md" in pay["learnings"]
              and all("_index.md" not in l for l in pay["learnings"]))
        check("index files tracked separately",
              "Pauta/payments/fix/233-timeout/_index.md" in pay["indexes"])
        check("workspace-scope note is not a project", "Learnings" not in names)


# --- symbols + freshness -----------------------------------------------------

def test_symbols():
    print("test_symbols")
    syms = kc.distinctive_symbols("uses `EmissaoNotaFiscalProdutoServiceImpl.realizaOperacoesAposEmissao` and ThreadPoolTaskScheduler")
    check("extracts backticked class", "EmissaoNotaFiscalProdutoServiceImpl" in syms)
    check("extracts dotted tail too", "realizaOperacoesAposEmissao" in syms)
    check("extracts CamelCase token", "ThreadPoolTaskScheduler" in syms)
    check("drops short noise", all(len(s) >= 6 for s in syms))


def test_freshness():
    print("test_freshness")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        vault = root / "vault"
        repo = root / "repos" / "payments"
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        write(repo / "Service.java", "class PaymentRetryService { void go(){} }\n")
        git(repo, "add", "-A"); git(repo, "commit", "-qm", "init")
        # learning cites one present symbol and one that no longer exists
        write(vault / "Pauta/payments/Learnings/x.md",
              learning("retry", body_extra="`PaymentRetryService` e `RemovedLegacyClass`"))
        hints = kc.freshness_hints(vault, repo, ["Pauta/payments/Learnings/x.md"])
        miss = hints.get("Pauta/payments/Learnings/x.md", [])
        check("absent symbol flagged", "RemovedLegacyClass" in miss)
        check("present symbol NOT flagged", "PaymentRetryService" not in miss)
        check("no repo -> no hints (graceful)", kc.freshness_hints(vault, None, ["Pauta/payments/Learnings/x.md"]) == {})


# --- prompt contracts --------------------------------------------------------

def test_map_prompt():
    print("test_map_prompt")
    p = kc.map_prompt(Path("/v"), "Pauta", "payments",
                      ["Pauta/payments/Learnings/a.md", "Pauta/payments/Learnings/b.md"],
                      ["Pauta/payments/fix/1/_index.md"],
                      {"Pauta/payments/Learnings/a.md": ["GoneClass"]})
    f = flat(p)
    check("non-destructive: throwaway branch, live vault untouched",
          "throwaway" in f and "live vault is untouched" in f)
    check("merge near-duplicates rule", "Merge near-duplicates" in p)
    check("contradiction resolved by what LANDED", "LANDED in production wins" in f)
    check("staleness keep-in-doubt", "In doubt, KEEP and flag" in f and "never delete on the probe alone" in f)
    check("promotion is suggest-only", "SUGGEST ONLY, do not apply" in f and "Promotion suggestions" in p)
    check("deterministic exact paths", "exact vault path" in f and "Never guess" in p)
    check("consolidation-history trace required", "## Consolidation history" in p)
    check("scoped to this project only", "Touch ONLY files under" in p and "payments/" in p)
    check("freshness hint injected", "GoneClass" in p and "STALENESS HINT" in f)
    check("no MCP", "do NOT use any MCP server" in p and "obsidian" not in p.lower())


def test_reduce_prompt():
    print("test_reduce_prompt")
    summaries = [
        {"project": "payments", "headers": [("Pauta/payments/Learnings/a.md", "idempotency on retry")]},
        {"project": "ledger", "headers": [("Pauta/ledger/Learnings/b.md", "idempotency by offset")]},
    ]
    p = kc.reduce_prompt(Path("/v"), "Pauta", summaries)
    f = flat(p)
    check("cross-project duplicates only", "cross-project duplicates ONLY" in f or "cross-project duplicates only" in f.lower())
    check("survivor at workspace scope", "WORKSPACE scope" in p and "Pauta/Learnings/" in p)
    check("catalog is descriptions, not bodies", "NOT the full bodies" in f and "idempotency on retry" in p)
    check("leave superficial matches alone", "LEAVE" in p and "do not over-merge" in f)
    check("deterministic + history trace", "exact vault paths" in f and "## Consolidation history" in p)
    check("no MCP", "do NOT use any MCP server" in p)


def test_headers():
    print("test_headers")
    with tempfile.TemporaryDirectory() as d:
        vault = build_vault(Path(d))
        hs = kc.project_headers(vault, ["Pauta/payments/Learnings/idempotency-retry.md"])
        check("header carries the description", hs and "idempotencia" in hs[0][1])


# --- hermetic dry-run (no branch, no LLM) ------------------------------------

def test_dry_run_subprocess():
    print("test_dry_run_subprocess")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        vault = build_vault(root)
        git(vault, "init", "-q"); git(vault, "add", "-A"); git(vault, "commit", "-qm", "seed")
        home = root / "home" / ".kb"
        home.mkdir(parents=True)
        (home / "config.json").write_text(json.dumps({
            "vault": str(vault),
            "workspaces": [{"name": "Pauta", "path": str(root / "repos")}],
        }), encoding="utf-8")
        env = dict(os.environ)
        env["KB_VAULT"] = str(vault)
        env["KB_HOME"] = str(home)
        env["HOME"] = str(root / "home")
        env["USERPROFILE"] = str(root / "home")
        r = subprocess.run([sys.executable, str(HERE / "kb-consolidate.py"),
                            "--workspace", "Pauta", "--dry-run"],
                           capture_output=True, text=True, env=env, timeout=60)
        check("dry-run exits 0", r.returncode == 0, r.stderr[:200])
        check("plan lists both projects", "payments:" in r.stdout and "ledger:" in r.stdout)
        check("prints a token estimate", "tok" in r.stdout and "estimated total" in r.stdout)
        check("declares no writes", "no branch created" in r.stdout.lower() and "no llm" in r.stdout.lower())
        branches = git(vault, "branch").stdout
        check("NO consolidation branch created by dry-run", "consolidation/" not in branches, branches)


def main():
    test_gather()
    test_symbols()
    test_freshness()
    test_map_prompt()
    test_reduce_prompt()
    test_headers()
    test_dry_run_subprocess()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
