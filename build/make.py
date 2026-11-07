"""Build standalone executables: ``python -m build.make``. PyInstaller cannot cross-compile, so run
this on each OS the demo uses. The output in ``dist/`` needs no Python on the target machine."""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "client" / "assets" / "fonts"
SEP = ";" if platform.system() == "Windows" else ":"

# Pulled in only by dynamic import, so PyInstaller cannot see them by itself.
CLIENT_HIDDEN = ("PIL.ImageTk", "PIL._tkinter_finder")
# Trimmed because these parts are never used by either program.
EXCLUDES = ("PyQt5", "PyQt6", "PySide6", "numpy")


def build(name: str, entry: str, gui: bool) -> Path:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        name,
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build" / "work"),
        "--specpath",
        str(ROOT / "build" / "work"),
        "--paths",
        str(ROOT),
        "--collect-submodules",
        "cryptography",
    ]
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    if gui:
        command += ["--windowed", "--add-data", f"{FONTS}{SEP}client/assets/fonts"]
        for module in CLIENT_HIDDEN:
            command += ["--hidden-import", module]
    else:
        # The server never ships a UI toolkit. The rule that server/ imports no UI code is
        # enforced here too.
        command += ["--exclude-module", "tkinter", "--exclude-module", "PIL"]
    command.append(str(ROOT / entry))
    subprocess.run(command, cwd=ROOT, check=True)
    return ROOT / "dist" / name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--os", default=platform.system(), help="for the build log only")
    parser.add_argument("--only", choices=("server", "client"), help="build just one of them")
    args = parser.parse_args()
    print(f"Building for {args.os} on {platform.platform()}")
    if args.only != "client":
        print("Built", build("lanvas-server", "build/entry_server.py", gui=False))
    if args.only != "server":
        print("Built", build("lanvas-client", "build/entry_client.py", gui=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
