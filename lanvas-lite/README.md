# LANvas (lite)

LAN-only, multi-user collaborative whiteboard. Python standard library only (tkinter + asyncio + sockets):
nothing to `pip install` to run it, so it works with the internet disabled.

| File | Role | Lines |
|---|---|---|
| `common.py` | message framing, password proof, board-op reducer (shared) | ~75 |
| `server.py` | asyncio TCP server: rooms, auth, ordering, undo, persistence | ~360 |
| `client.py` | Tk GUI + network thread with auto-reconnect | ~540 |
| `test_lanvas.py` | protocol tests against a real server process | ~190 |
| `build.py` | PyInstaller build of the two executables | 6 |

## Run (Python 3.9+; Linux needs `sudo apt install python3-tk`)
```
python server.py            # prints the LAN address, e.g. 192.168.1.20:47801
python client.py            # on every participant's machine
```
Host: tick **Create a new room**, choose your name and an optional password; the room code appears in the
status bar. Others: enter the server address, the room code, their name and the password, then **Connect**.
Server data (saved boards) lives in `./data` (`--data` to change). Open **TCP 47801** in the server's firewall.

## Standalone executables
`pip install pyinstaller && python build.py` creates `dist/lanvas-server` and `dist/lanvas-client`
(build on each OS you need; PyInstaller can't cross-compile).

## Tests
`pip install pytest && python -m pytest -q`  (about 10 s; starts its own server on a spare port)

See `DOCS.md` for architecture, message format, synchronization/recovery design and the user guide.
