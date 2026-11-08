# LANvas (lite)

LAN-only, multi-user collaborative whiteboard. The server is Python standard library only (asyncio + sockets);
the client uses **PyQt6** (`pip install PyQt6`, once). No internet is needed at run time.

| File | Role | Lines |
|---|---|---|
| `common.py` | message framing, password proof, board-op reducer, eraser/clip geometry | ~180 |
| `server.py` | asyncio TCP server: rooms, roles, auth, ordering, undo, persistence, logs | ~690 |
| `client.py` | PyQt6 GUI (custom-painted board) + network thread with auto-reconnect | ~1980 |
| `test_lanvas.py` | protocol tests against a real server process | ~190 |
| `build.py` | PyInstaller build of the two executables | 6 |

## Run (Python 3.9+)
```
python server.py            # prints the LAN address, e.g. 192.168.1.20:47801
pip install PyQt6          # client machines only
python client.py            # on every participant's machine
```
Host: pick **Create a room**, choose your name and an optional password; the room code appears in the
status bar. Others: enter the server address, the room code, their name and the password, then **Connect**.
Server data (saved boards) and `server.log` live in `./data` (`--data` to change). Open **TCP 47801** in the server's firewall.

## Standalone executables
`pip install pyinstaller && python build.py` creates `dist/lanvas-server` and `dist/lanvas-client`
(build on each OS you need; PyInstaller can't cross-compile).

## Tests
`pip install pytest && python -m pytest -q`  (about 40 s; starts its own server on a spare port)

See `DOCS.md` for architecture, message format, synchronization/recovery design and the user guide.
