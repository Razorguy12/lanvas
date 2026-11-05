# LANvas

A Figma-style, offline, LAN-only collaborative whiteboard, built on raw TCP and UDP sockets with PyQt6.
The room creator decides who can view or edit. Boards on the network appear by themselves, and a crash costs nothing.

- [`ProductContext.md`](ProductContext.md): what and why
- [`Architecture.md`](Architecture.md): protocol, data model, persistence, security
- [`Design.md`](Design.md): UI behavior and tokens
- [`Agents.md`](Agents.md): process, phases, conventions
- [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md): how to use it
- [`docs/test-log.md`](docs/test-log.md): what has been tested, and what has not

## Run from source

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m server.main                                  # start a server (runs the integrity scan first)
python -m client.main                                  # start a client
python -m client.main --profile second                 # a second identity on the same machine
```

The server keeps its data in `./data` (`--data` to change). Python 3.11 or newer.

## Build standalone executables

PyInstaller cannot cross-compile, so run this on each OS the demo uses:

```bash
python -m build.make                                   # writes dist/lanvas-server and dist/lanvas-client
```

Check a build with no Python environment involved (against a running server):

```bash
QT_QPA_PLATFORM=offscreen dist/lanvas-client/lanvas-client --smoke 127.0.0.1 47801 /tmp/out
```

## Firewall (XP-04)

LANvas needs **TCP 47801** (app) and **UDP 47800** (discovery), inbound on the server and on every client.

| OS | Commands |
|---|---|
| Fedora / RHEL (`firewalld`) | `sudo firewall-cmd --permanent --add-port=47801/tcp --add-port=47800/udp && sudo firewall-cmd --reload` |
| Ubuntu / Debian (`ufw`) | `sudo ufw allow 47801/tcp && sudo ufw allow 47800/udp` |
| Windows (admin PowerShell) | `New-NetFirewallRule -DisplayName "LANvas TCP" -Direction Inbound -Protocol TCP -LocalPort 47801 -Action Allow` and `New-NetFirewallRule -DisplayName "LANvas UDP" -Direction Inbound -Protocol UDP -LocalPort 47800 -Action Allow` |

If discovery is blocked or the network isolates clients from each other, **Join by address** still works: it needs only TCP 47801.

On Linux, Qt 6.5+ on X11 may need `libxcb-cursor0`. On Wayland, `QT_QPA_PLATFORM=xcb` is a fallback.

## Tests

```bash
python scripts/check.py                                # lint plus the fast set; run before every push
pytest                                                 # everything
LANVAS_FULL_SOAK=1 pytest -m chaos                     # the catalogue's full 60 s chaos soak
python scripts/gate.py --phase 5                       # gate report by test ID, for docs/test-log.md
```

## Bundled assets

Inter 4.1 by The Inter Project Authors, under the SIL Open Font License (`client/assets/fonts/LICENSE.txt`).
