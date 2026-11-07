#!/usr/bin/env python3
"""Phase gate runner (Agents.md section 11): run every test ID for phases 0..N and report.

Each catalogue ID must map to at least one passing test named ``test_<ID>_...``. IDs that automation
cannot prove on one machine (Windows runs, physical LAN, packaged builds on each OS) are listed as
PENDING. A gate is reported GREEN only when nothing is FAIL, MISSING, or PENDING: this script will
not call a gate green on the strength of the part that happened to be testable.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def ids(prefix: str, lo: int, hi: int) -> list[str]:
    return [f"{prefix}-{n:02d}" for n in range(lo, hi + 1)]


PHASES: dict[int, list[str]] = {
    0: ids("FR", 1, 6) + ["SV-03", "XP-06"],
    1: ids("OR", 1, 6)
    + ["RC-02", "RC-03", "RC-05", "RC-06", "RC-07", "CT-01", "CT-02", "CT-04"]
    + ids("CT", 6, 10)
    + ["CT-12", "SV-01"]
    + ids("SV", 3, 6)
    + ids("SE", 1, 4),
    2: ids("AC", 1, 12) + ["CT-05", "CT-11", "SV-02"],
    3: ids("DS", 1, 8) + ["RC-01", "XP-03", "XP-04"],
    4: ids("UN", 1, 4) + ["RC-04", "CT-03", "SV-07", "EX-01"],
    5: ["UN-05"]
    + ["SV-11"]
    + ids("EX", 2, 4)
    + ids("EX", 6, 8)
    + ids("CH", 1, 3)
    + ids("PF", 1, 6)
    + ids("XP", 1, 3),
}

# Automated tests here can only speak for the OS they ran on. These need people and hardware.
MANUAL: dict[str, str] = {
    "XP-01": "L2 suite on Windows and a second Linux distro",
    "XP-02": "server and clients on different OSes (L3)",
    "XP-03": "built executables launched on every other demo OS",
    "XP-04": "firewall rules verified on Windows and Linux",
    "XP-05": "HiDPI and Wayland/X11 by eye",
    "XP-06": "also needs a run on Windows/NTFS (Linux covered by SV-03)",
    "PF-04": "frame rate on real GPU and display",
}
GATE_MANUAL: dict[int, list[str]] = {
    0: ["check.py on Windows"],
    3: ["L3 LAN run (first)"],
    4: ["Design.md 13.1 Tier 1 rows", "L3 LAN run on 3+ machines (Tier 1 exit)"],
    5: ["full Design.md 13.1", "L3 LAN run (Tier 2 exit)"],
}


def run_tests(xml_path: Path) -> tuple[dict[str, list[str]], int]:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(ROOT)}
    cmd = [sys.executable, "-m", "pytest", "-q", f"--junitxml={xml_path}", "-p", "no:cacheprovider"]
    code = subprocess.run(cmd, cwd=ROOT, env=env).returncode
    results: dict[str, list[str]] = {}
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        name = case.get("name", "")
        status = "pass"
        if case.find("failure") is not None or case.find("error") is not None:
            status = "fail"
        elif case.find("skipped") is not None:
            status = "skip"
        # A test can cover several IDs: test_SV_03_XP_06_atomic_write...
        for prefix, num in re.findall(r"(?:^test_|_)([A-Z]{2})_(\d{2})(?=_|\[|$)", name):
            results.setdefault(f"{prefix}-{num}", []).append(status)
    return results, code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", type=int, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        results, _ = run_tests(Path(tmp) / "report.xml")

    print(f"\n# Gate report: phases 0..{args.phase}")
    blocking = 0
    for phase in range(args.phase + 1):
        print(f"\n## Phase {phase}")
        for test_id in PHASES[phase]:
            statuses = results.get(test_id, [])
            if "fail" in statuses:
                verdict = "FAIL"
                blocking += 1
            elif test_id in MANUAL:
                done = f"{statuses.count('pass')} automated pass; " if statuses else ""
                verdict = f"PENDING ({done}needs: {MANUAL[test_id]})"
                blocking += 1
            elif statuses.count("pass"):
                verdict = f"PASS ({statuses.count('pass')})"
            elif statuses:
                verdict = "SKIPPED"
                blocking += 1
            else:
                verdict = "MISSING"
                blocking += 1
            print(f"- {test_id}: {verdict}")
        for item in GATE_MANUAL.get(phase, []):
            print(f"- manual: PENDING ({item})")
            blocking += 1
    print(f"\nGate {'GREEN' if blocking == 0 else f'NOT GREEN: {blocking} item(s) outstanding'}")
    return 0 if blocking == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
