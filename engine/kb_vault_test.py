#!/usr/bin/env python3
"""Tests for kb_vault against a SYNTHETIC temp vault (fake slugs — never the real
vault / company data). Covers the date-inheritance rule, overview aggregation,
filtering, the path-guard, and that the markdown renderer escapes injected HTML.

Run: python engine/kb_vault_test.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_vault as kv  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(("  ok   " if cond else "  FAIL ") + name)


def w(p: Path, body: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def build_vault(root: Path) -> Path:
    v = root / "vault"
    # workspace-scope learning (no governing ticket -> dateless)
    w(v / "Acme/Learnings/ws-rule.md",
      "---\nname: ws-rule\ndescription: workspace wide rule\ntags:\n  - global\n---\nbody\n")
    # project-scope learning (no governing ticket -> dateless)
    w(v / "Acme/gateway/Learnings/proj-rule.md",
      "---\nname: proj-rule\ndescription: gateway project rule\ntags:\n  - gateway\n---\nbody\n")
    # ticket (type-grouped), resolved 2026-05-10
    w(v / "Acme/gateway/fix/login/_index.md",
      "---\nproject: gateway\ntype: fix\nslug: login\ntitle: Login fix\nstatus: resolved\n"
      "opened: 2026-05-01\nresolved: 2026-05-10\nlast_update: 2026-05-10\nbranch: fix/login\n---\nticket body\n")
    w(v / "Acme/gateway/fix/login/Learnings/jwt-expiry.md",
      "---\nname: jwt-expiry\ndescription: use <= not < for expiry\nticket_origin: login\n"
      "tags:\n  - auth\n  - jwt\nscope: ticket\n---\n"
      "# How\nText with **bold**, `code`, a [link](https://example.com).\n\n"
      "- one\n- two\n\n```\nraw <not> escaped-as-tag\n```\n<script>alert(1)</script>\n")
    # ticket (ungrouped), open, last_update 2026-04-05
    w(v / "Acme/gateway/checkout/_index.md",
      "---\nproject: gateway\nslug: checkout\ntitle: Checkout\nstatus: open\n"
      "opened: 2026-04-02\nlast_update: 2026-04-05\nbranch: checkout\n---\nbody\n")
    w(v / "Acme/gateway/checkout/Learnings/idempotent-pay.md",
      "---\nname: idempotent-pay\ndescription: dedupe payment by key\ntags:\n  - checkout\nscope: ticket\n---\nbody\n")
    return v


def test_all():
    print("test_kb_vault")
    with tempfile.TemporaryDirectory() as d:
        v = build_vault(Path(d))

        # --- overview ---
        ov = kv.overview(v)
        check("4 learnings total", ov["totals"]["learnings"] == 4)
        check("2 tickets total", ov["totals"]["tickets"] == 2)
        check("status: 1 resolved + 1 open",
              ov["tickets_by_status"].get("resolved") == 1 and ov["tickets_by_status"].get("open") == 1)
        check("by_project gateway has 3 learnings",
              any(p["project"] == "gateway" and p["learnings"] == 3 for p in ov["by_project"]))
        months = {g["month"]: g for g in ov["growth"]}
        check("growth uses ticket dates (2026-05 learning+resolved)",
              months.get("2026-05", {}).get("learnings") == 1 and months["2026-05"].get("tickets_resolved") == 1)
        check("growth uses last_update when no resolved (2026-04 learning)",
              months.get("2026-04", {}).get("learnings") == 1)
        check("dateless learnings excluded from growth (2 dated of 4)",
              sum(g["learnings"] for g in ov["growth"]) == 2)
        check("tags aggregated", dict(ov["top_tags"]).get("auth") == 1)

        # --- list + filters ---
        alll = kv.list_learnings(v)
        check("list returns 4", len(alll) == 4)
        check("sorted newest-ticket-date first", alll[0]["name"] == "jwt-expiry")
        check("filter project=gateway -> 3", len(kv.list_learnings(v, project="gateway")) == 3)
        check("filter scope=ticket -> 2", len(kv.list_learnings(v, scope="ticket")) == 2)
        check("filter tag=checkout -> 1", len(kv.list_learnings(v, tag="checkout")) == 1)
        check("query q='expiry' -> 1", len(kv.list_learnings(v, q="expiry")) == 1)
        jwt = next(r for r in alll if r["name"] == "jwt-expiry")
        check("learning inherits ticket date", jwt["date"] == "2026-05-10")

        # --- read_item + render safety ---
        item = kv.read_item(v, "Acme/gateway/fix/login/Learnings/jwt-expiry.md")
        check("read_item scope=ticket", item["scope"] == "ticket")
        check("renders heading", "<h1>" in item["html"])
        check("renders bold + code + list", "<strong>" in item["html"] and "<code>" in item["html"] and "<li>" in item["html"])
        check("fenced code block rendered", "<pre><code>" in item["html"])
        check("injected HTML is escaped (no raw <script>)", "<script>" not in item["html"] and "&lt;script&gt;" in item["html"])

        # --- path guard ---
        try:
            kv.read_item(v, "../../etc/passwd")
            check("path guard blocks traversal", False)
        except ValueError:
            check("path guard blocks traversal", True)
        try:
            kv.read_item(v, "Acme/gateway/fix/login/_index.md")  # inside vault, ok
            check("read_item allows in-vault _index", True)
        except ValueError:
            check("read_item allows in-vault _index", False)


def main():
    test_all()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
