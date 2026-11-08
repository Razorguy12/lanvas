"""Build standalone executables into dist/. PyInstaller cannot cross-compile, so run this once on
each OS you need (needs `pip install pyinstaller` on the build machine only):  python build.py"""
import PyInstaller.__main__ as pyi

for name in ("server", "client"):
    pyi.run(["--onefile", "--noconfirm", "--name", f"lanvas-{name}", f"{name}.py"])
