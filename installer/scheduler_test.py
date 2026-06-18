#!/usr/bin/env python3
"""Tests for scheduler — multi-time artifacts, normalization, status parsers.

Everything here is pure (artifact builders + parsers + dry-run register), so it
runs identically on any OS without touching Task Scheduler / launchd / cron.
Run: python installer/scheduler_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scheduler  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


KB = Path("C:/Users/example/.kb") if sys.platform == "win32" else Path("/home/example/.kb")


def main() -> int:
    print("test_normalize_times")
    check("single string accepted", scheduler.normalize_times("01:00") == ["01:00"])
    check("list sorted + deduped", scheduler.normalize_times(["22:30", "06:00", "22:30"]) == ["06:00", "22:30"])
    for bad in ("1:00", "24:00", "aa:bb", "", [], ["01:00", "nope"]):
        try:
            scheduler.normalize_times(bad)
            check(f"rejects {bad!r}", False)
        except ValueError:
            check(f"rejects {bad!r}", True)

    print("test_windows_xml")
    xml = scheduler._windows_task_xml(KB, "python", ["01:00", "13:30"])
    check("one CalendarTrigger per time", xml.count("<CalendarTrigger>") == 2)
    check("both times present", "T01:00:00" in xml and "T13:30:00" in xml)
    check("daily interval", xml.count("<DaysInterval>1</DaysInterval>") == 2)
    check("runs the engine sync script", "kb-sync.py" in xml)
    check("redirect is xml-escaped", "&gt;&gt;" in xml and ">>" not in
          xml.split("<Arguments>")[1].split("</Arguments>")[0])
    check("interactive token (no stored password)", "<LogonType>InteractiveToken</LogonType>" in xml)

    print("test_macos_plist")
    pl = scheduler._macos_plist(KB, "python3", ["01:00", "13:30"])
    check("interval is an array", "<array>" in pl.split("StartCalendarInterval")[1])
    check("two interval dicts", pl.count("<key>Hour</key>") == 2)
    check("minutes carried", "<key>Minute</key><integer>30</integer>" in pl)

    print("test_linux_cron_block")
    block = scheduler._linux_cron_block(KB, "python3", ["01:00", "13:30"])
    lines = [l for l in block.splitlines() if not l.startswith("#")]
    check("one cron line per time", len(lines) == 2)
    check("minute hour order", lines[0].startswith("0 1 ") and lines[1].startswith("30 13 "))
    check("managed markers wrap the block",
          block.splitlines()[0] == scheduler._CRON_BEGIN and block.splitlines()[-1] == scheduler._CRON_END)

    print("test_path_prefix_injection")
    # Linux: prefix is prepended inline, scoped to the command, and the line
    # still begins with "minute hour " so the time parsers keep working.
    blk = scheduler._linux_cron_block(KB, "python3", ["01:00"], path_prefix="/home/u/.local/bin")
    cron_line = [l for l in blk.splitlines() if not l.startswith("#")][0]
    check("linux prefix prepended inline", 'PATH="/home/u/.local/bin:$PATH"' in cron_line)
    check("linux line still starts minute hour", cron_line.startswith("0 1 "))
    check("linux times still parse with prefix", scheduler.parse_times_from_cron(blk) == ["01:00"])
    check("linux: no prefix -> no PATH=", "PATH=" not in scheduler._linux_cron_block(KB, "python3", ["01:00"]))
    # macOS: prefix becomes an EnvironmentVariables/PATH entry; the interval
    # dicts (Hour/Minute) are untouched so the plist parser still round-trips.
    plp = scheduler._macos_plist(KB, "python3", ["01:00", "13:30"], path_prefix="/home/u/.local/bin")
    check("macos EnvironmentVariables added", "<key>EnvironmentVariables</key>" in plp)
    check("macos PATH carries the prefix", "/home/u/.local/bin:/usr/local/bin:/usr/bin:/bin" in plp)
    check("macos intervals untouched (2 Hours)", plp.count("<key>Hour</key>") == 2)
    check("macos times still parse with prefix", scheduler.parse_times_from_plist(plp) == ["01:00", "13:30"])
    check("macos: no prefix -> no EnvironmentVariables",
          "<key>EnvironmentVariables</key>" not in scheduler._macos_plist(KB, "python3", ["01:00"]))

    print("test_register_dry_run")
    rep = scheduler.register(KB, time_hhmm=["13:30", "01:00"], dry_run=True)
    check("reports normalized times", rep.get("times") == ["01:00", "13:30"])
    check("legacy single-time field kept", rep.get("time") == "01:00")
    check("dry-run never registers", rep.get("registered") is False)
    check("artifact or command included", bool(rep.get("artifact") or rep.get("command")))
    rep = scheduler.register(KB, time_hhmm="01:00", dry_run=True)
    check("single string still works", rep.get("times") == ["01:00"])
    rep = scheduler.register(KB, time_hhmm="25:99", dry_run=True)
    check("bad time is a loud error", rep.get("registered") is False and "error" in rep)

    print("test_status_parsers")
    xml = scheduler._windows_task_xml(KB, "python", ["01:00", "13:30"])
    check("xml parser round-trips", scheduler.parse_times_from_task_xml(xml) == ["01:00", "13:30"])
    pl = scheduler._macos_plist(KB, "python3", ["09:05", "23:55"])
    check("plist parser round-trips", scheduler.parse_times_from_plist(pl) == ["09:05", "23:55"])
    tab = "MAILTO=x\n" + scheduler._linux_cron_block(KB, "python3", ["09:05", "23:55"]) + "\n5 4 * * * other\n"
    check("cron parser reads only the managed block",
          scheduler.parse_times_from_cron(tab) == ["09:05", "23:55"])

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
