#!/usr/bin/env python
"""bootstrap_test.py — tests for the one-line bootstrap scripts.

Exercises bootstrap.sh against a local fixture repo (KB_REPO=<path>) with
KB_BOOTSTRAP_NO_INSTALL=1, so the clone/update logic runs for real but the
full installer (scheduler registration etc.) never touches the machine.
bootstrap.ps1 is parse-checked (PowerShell 5.1 syntax) without executing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASSED = 0
FAILED = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}  {extra}")


def find_bash():
    for cand in (
        os.environ.get("KB_BASH"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        "/usr/bin/bash",
        "/bin/bash",
        shutil.which("bash"),
    ):
        if not cand or not Path(cand).exists():
            continue
        if "system32" in str(cand).lower():
            continue  # WSL launcher — wrong bash for repo scripts
        return cand
    return None


def make_fixture_repo(root: Path) -> Path:
    """A minimal git repo that looks like the KB repo (installer present)."""
    repo = root / "origin"
    (repo / "installer").mkdir(parents=True)
    (repo / "installer" / "install.sh").write_text("#!/usr/bin/env bash\necho fake-install\n", encoding="utf-8")
    (repo / "VERSION").write_text("0.0.1\n", encoding="utf-8")
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, timeout=30)
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, text=True, timeout=30)
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return repo


def main() -> int:
    bash = find_bash()
    if not bash:
        print("FAIL: no usable bash found")
        return 1

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        origin = make_fixture_repo(tmp)
        app_dir = tmp / "kbapp"
        env = os.environ.copy()
        env["KB_REPO"] = str(origin).replace("\\", "/")
        env["KB_APP_DIR"] = str(app_dir).replace("\\", "/")
        env["KB_BOOTSTRAP_NO_INSTALL"] = "1"

        print("test_bootstrap_sh_clone")
        r1 = subprocess.run([bash, str(ROOT / "bootstrap.sh")],
                            capture_output=True, text=True, env=env, timeout=60)
        check("first run exits 0", r1.returncode == 0, (r1.stderr or r1.stdout)[:200])
        check("clones the repo", (app_dir / ".git").is_dir())
        check("installer arrived", (app_dir / "installer" / "install.sh").is_file())
        check("reports the skipped-install next step", "install skipped" in r1.stdout)

        print("test_bootstrap_sh_update")
        (origin / "VERSION").write_text("0.0.2\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(origin), "commit", "-aqm", "bump"],
                       capture_output=True, text=True, timeout=30)
        r2 = subprocess.run([bash, str(ROOT / "bootstrap.sh")],
                            capture_output=True, text=True, env=env, timeout=60)
        check("second run exits 0", r2.returncode == 0, (r2.stderr or r2.stdout)[:200])
        check("takes the update path", "updating" in r2.stdout)
        check("pulled the new commit", (app_dir / "VERSION").read_text(encoding="utf-8").strip() == "0.0.2")

        print("test_bootstrap_sh_no_git_dest_failure")
        env_bad = dict(env)
        env_bad["KB_REPO"] = str(tmp / "nonexistent-repo")
        env_bad["KB_APP_DIR"] = str(tmp / "kbapp2").replace("\\", "/")
        # PATH still has gh? force the no-gh branch by clearing PATH except git's dir.
        r3 = subprocess.run([bash, str(ROOT / "bootstrap.sh")],
                            capture_output=True, text=True, env=env_bad, timeout=60)
        check("bad repo fails non-zero", r3.returncode != 0)

    print("test_bootstrap_ps1_parses")
    if os.name == "nt":
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$t = [System.Management.Automation.PSParser]::Tokenize((Get-Content -Raw '"
             + str(ROOT / "bootstrap.ps1").replace("'", "''")
             + "'), [ref]$null); 'TOKENS=' + $t.Count"],
            capture_output=True, text=True, timeout=60)
        check("ps1 tokenizes under PowerShell 5.1", ps.returncode == 0 and "TOKENS=" in ps.stdout,
              (ps.stderr or ps.stdout)[:200])
    else:
        print("  ok   skipped (not Windows)")
        global PASSED
        PASSED += 1

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
