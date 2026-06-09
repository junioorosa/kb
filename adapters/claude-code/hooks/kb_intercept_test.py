#!/usr/bin/env python
"""kb_intercept_test.py — behavioural tests for the kb-mark / kb-stats
UserPromptSubmit intercepts.

Drives each intercept as a subprocess with a controlled HOME/KB_VAULT, feeds a
JSON payload on stdin, and asserts the emitted decision + side effects (sidecar
writes, _index status patch) and that the user-facing reason still carries the
key facts. Assertions are on semantics/substrings, not exact wording, so a
message-polish pass stays green as long as behaviour is preserved.

Run:  python kb_intercept_test.py   (exit 0 all green, 1 on any failure)
Isolated: never touches the real ~/.claude state or the real vault.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
ENGINE = HOOKS.parent.parent.parent / "engine"   # repo/engine holds kb_config.py
MARK = HOOKS / "kb-mark-intercept.py"
STATS = HOOKS / "kb-stats-intercept.py"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(script: Path, payload: dict, home: Path, vault: Path | None = None,
        disabled: bool = False) -> tuple[int, dict | None, str]:
    env = dict(os.environ)
    env["USERPROFILE"] = str(home)   # Windows expanduser("~")
    env["HOME"] = str(home)          # POSIX expanduser("~")
    env["PYTHONPATH"] = str(ENGINE) + os.pathsep + env.get("PYTHONPATH", "")
    if vault:
        env["KB_VAULT"] = str(vault)
    if disabled:
        env["KB_HOOKS_DISABLED"] = "1"
    else:
        env.pop("KB_HOOKS_DISABLED", None)
    p = subprocess.run([sys.executable, str(script)], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", env=env)
    out = p.stdout.strip()
    parsed = None
    if out:
        try:
            parsed = json.loads(out)
        except Exception:
            parsed = None
    return p.returncode, parsed, out


def sidecar(home: Path, sid: str) -> dict:
    f = home / ".claude" / "state" / f"kb-session-branch-{sid}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def write_state(home: Path, name: str, data: dict) -> None:
    d = home / ".claude" / "state"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(data), encoding="utf-8")


def make_folder(vault: Path, rel: str, status: str = "open") -> Path:
    """Create <vault>/<rel>/_index.md with minimal frontmatter."""
    d = vault / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "_index.md").write_text(
        f"---\nproject: p\nbranch: x\nstatus: {status}\n---\n# t\n", encoding="utf-8")
    return d


def fresh_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="kbihome_"))


# ----------------------------- kb-mark -----------------------------

def test_mark_new():
    home = fresh_home()
    rc, j, _ = run(MARK, {"prompt": "/kb-mark feat/login", "session_id": "s1"}, home)
    sc = sidecar(home, "s1")
    check("mark: blocks", j is not None and j.get("decision") == "block", str(j))
    check("mark: reason names branch", j and "feat/login" in j.get("reason", ""), str(j))
    check("mark: sidecar branch written", sc.get("branch") == "feat/login", str(sc))
    check("mark: manual_override flag", sc.get("manual_override") is True, str(sc))


def test_mark_no_branch():
    home = fresh_home()
    rc, j, _ = run(MARK, {"prompt": "/kb-mark", "session_id": "s1"}, home)
    check("mark: empty arg blocks with guidance", j and j.get("decision") == "block"
          and "kb-mark" in j.get("reason", "").lower(), str(j))
    check("mark: no sidecar written on empty arg", sidecar(home, "s1") == {}, str(sidecar(home, "s1")))


def test_mark_remove():
    home = fresh_home()
    run(MARK, {"prompt": "/kb-mark feat/x", "session_id": "s1"}, home)
    rc, j, _ = run(MARK, {"prompt": "/kb-mark --remove", "session_id": "s1"}, home)
    check("mark: remove blocks", j and j.get("decision") == "block", str(j))
    check("mark: remove mentions prior branch", j and "feat/x" in j.get("reason", ""), str(j))
    check("mark: sidecar deleted after remove", sidecar(home, "s1") == {}, "still present")


def test_mark_remove_nothing():
    home = fresh_home()
    rc, j, _ = run(MARK, {"prompt": "/kb-mark --remove", "session_id": "s1"}, home)
    check("mark: remove-nothing blocks (no crash)", j and j.get("decision") == "block", str(j))


def test_mark_done():
    home = fresh_home()
    run(MARK, {"prompt": "/kb-mark fix/bug", "session_id": "s1"}, home)
    rc, j, _ = run(MARK, {"prompt": "/kb-mark --done", "session_id": "s1"}, home)
    sc = sidecar(home, "s1")
    check("mark: done blocks", j and j.get("decision") == "block", str(j))
    check("mark: done sets manual_done", sc.get("manual_done") is True, str(sc))
    check("mark: done names branch", j and "fix/bug" in j.get("reason", ""), str(j))


def test_mark_experimental_patches_index():
    home = fresh_home()
    vault = Path(tempfile.mkdtemp(prefix="kbivault_"))
    make_folder(vault, "ws/proj/feat/exp", status="open")
    rc, j, _ = run(MARK, {"prompt": "/kb-mark --experimental feat/exp", "session_id": "s1"},
                   home, vault=vault)
    sc = sidecar(home, "s1")
    idx = (vault / "ws/proj/feat/exp/_index.md").read_text(encoding="utf-8")
    check("mark: experimental blocks", j and j.get("decision") == "block", str(j))
    check("mark: experimental sidecar flag", sc.get("mark_experimental") is True, str(sc))
    check("mark: experimental patches _index status", "status: experimental" in idx, idx)


def test_mark_existing_folder_warns():
    home = fresh_home()
    vault = Path(tempfile.mkdtemp(prefix="kbivault_"))
    make_folder(vault, "ws/proj/feat/dup")
    rc, j, _ = run(MARK, {"prompt": "/kb-mark feat/dup", "session_id": "s1"}, home, vault=vault)
    r = j.get("reason", "") if j else ""
    check("mark: existing-folder still marks", sidecar(home, "s1").get("branch") == "feat/dup", "")
    check("mark: existing-folder warns about update", j and j.get("decision") == "block"
          and ("update" in r.lower() or "exist" in r.lower()), r)


def test_mark_non_match_passthrough():
    home = fresh_home()
    rc, j, out = run(MARK, {"prompt": "how do I mark a branch?", "session_id": "s1"}, home)
    check("mark: non-/kb-mark prompt passes through (no output)", out == "", out)


def test_mark_disabled():
    home = fresh_home()
    rc, j, out = run(MARK, {"prompt": "/kb-mark feat/x", "session_id": "s1"}, home, disabled=True)
    check("mark: disabled -> no-op passthrough", out == "" and sidecar(home, "s1") == {}, out)


def test_mark_no_session():
    home = fresh_home()
    rc, j, _ = run(MARK, {"prompt": "/kb-mark feat/x"}, home)
    check("mark: missing session_id blocks with error", j and j.get("decision") == "block"
          and "session" in j.get("reason", "").lower(), str(j))


# ----------------------------- kb-stats -----------------------------

def test_stats_no_data():
    home = fresh_home()
    rc, j, _ = run(STATS, {"prompt": "/kb-stats", "session_id": "s1"}, home)
    check("stats: no-data blocks with guidance", j and j.get("decision") == "block"
          and "kb" in j.get("reason", "").lower(), str(j))


def test_stats_renders():
    home = fresh_home()
    write_state(home, "kb-tokens-s1.json", {
        "session_id": "s1", "total": 1200, "prompts": 4, "exact_tokens": True,
        "by_tier": {"high": 1, "mid": 2, "low": 1}, "by_section": {"matches": 800, "footer": 400},
        "last": {"total": 300, "tier": "mid", "sections": {"matches": 200, "footer": 100}},
    })
    write_state(home, "kb-tier-s1.json", {"total": 4, "hits": 3})
    write_state(home, "kb-bodyread-s1.json", {"cited_reads": 2, "cited_read_tokens": 500,
                                              "vault_reads": 3, "vault_read_tokens": 700})
    rc, j, _ = run(STATS, {"prompt": "/kb-stats", "session_id": "s1"}, home)
    r = j.get("reason", "") if j else ""
    check("stats: blocks", j and j.get("decision") == "block", str(j))
    check("stats: shows cumulative total", "1,200" in r or "1200" in r, r[:200])
    check("stats: shows tier hit-rate", "%" in r, r[:200])
    check("stats: shows body-reads section", "body-read" in r.lower() or "consumed" in r.lower(), r[:200])


def test_stats_non_match_passthrough():
    home = fresh_home()
    rc, j, out = run(STATS, {"prompt": "show me kb stats please", "session_id": "s1"}, home)
    check("stats: non-/kb-stats prompt passes through", out == "", out)


def test_stats_disabled():
    home = fresh_home()
    write_state(home, "kb-tokens-s1.json", {"session_id": "s1", "total": 10, "prompts": 1})
    rc, j, out = run(STATS, {"prompt": "/kb-stats", "session_id": "s1"}, home, disabled=True)
    check("stats: disabled -> passthrough", out == "", out)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    print(f"Running {len(TESTS)} intercept test(s)\n")
    for t in TESTS:
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"exception: {e!r}")
    print()
    if FAILS:
        print(f"{len(FAILS)} failed: {', '.join(FAILS)}")
        sys.exit(1)
    print("all green")
    sys.exit(0)


if __name__ == "__main__":
    main()
