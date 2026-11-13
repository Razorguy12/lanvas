"""Build standalone executables into dist/. PyInstaller cannot cross-compile, so run this once on
each OS you need (needs `pip install pyinstaller PyQt6` on the build machine only):  python build.py
Linux -> dist/lanvas-server, dist/lanvas-client      Windows -> dist/lanvas-server.exe, dist/lanvas-client.exe"""
import PyInstaller.__main__ as pyi

pyi.run(["--onefile", "--noconfirm", "--clean", "--name", "lanvas-server", "server.py"])
# --windowed: the GUI client opens without a console window on Windows
pyi.run(["--onefile", "--noconfirm", "--clean", "--windowed", "--name", "lanvas-client", "client.py"])
