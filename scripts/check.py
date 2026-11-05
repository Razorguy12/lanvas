#!/usr/bin/env python3
"""Run before every push: ruff, then the fast L0/L1/L2 set. Same command on Windows and Linux."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(args: list[str]) -> int:
    print("$", " ".join(args), flush=True)
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(ROOT)}
    return subprocess.run(args, cwd=ROOT, env=env).returncode


def main() -> int:
    py = sys.executable
    failed = run([py, "-m", "ruff", "check", "."])
    failed |= run([py, "-m", "pytest", "-m", "not perf and not lan and not chaos", "-q"])
    print("check.py:", "FAILED" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
