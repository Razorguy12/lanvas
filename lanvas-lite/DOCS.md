# LANvas technical documentation

## 1. Architecture
Client-server over TCP/IP on a LAN; no internet needed. One server process (`server.py`) hosts any number of
rooms. Each room's authoritative state lives on the server: the ordered object list, participants, chat, saved
versions. Clients (`client.py`) hold a replica and render it.

```
 client A ─┐                       ┌─ room ABC123: objects, seq, users, chat, versions
 client B ─┼── TCP 47801 ── server ┤
 client C ─┘   (asyncio, 1 loop)   └─ room XYZ789: ...        data/<code>.json (autosave)
```
**Why TCP:** whiteboard operations must arrive, in order, exactly once; TCP provides that. UDP would force us to
rebuild reliability and ordering ourselves. **Framing:** TCP is a byte stream, so every message is
`[4-byte big-endian length][UTF-8 JSON]`; the receiver reads exactly 4 bytes, then exactly that many. Frames are
capped (8 KB before login, 1 MB from clients afterward) so a bad peer can't exhaust memory.

## 2. Application-level messages (JSON, field `t` = type)
| Direction | Type | Fields / meaning |
|---|---|---|
| C→S | `hello` | `user, room` or `create:true, salt, key, role`; optional session `token` |
| S→C | `challenge` | `salt, nonce` (only for password rooms) |
| C→S | `auth` | `proof` = HMAC-SHA256(key, nonce); the password is never sent |
| S→C | `welcome` | `room, you{name,token,color}` + full snapshot (below) |
| C→S | `op` | `{k:"add",obj}` or `{k:"del",id}`; obj = `{id,k,c,w,p,s}` (kind, color, width, points, text) |
| S→C | `op` | the same op plus `seq` (global order) and `by` (author); sent to everyone, sender included |
| C→S→C | `live` | in-progress stroke/shape preview; relayed to others, never stored or sequenced |
| C→S→C | `cur` | cursor position, relayed |
| C→S→C | `chat` | `text` → `{u, ts, text}` (server timestamps and orders it) |
| C→S | `undo` `redo` `clear` `save{name}` `restore{idx}` `role{user,role}` `kick{user}` `resync` | host-only: clear, save, restore, role, kick |
| S→C | `snap` | full snapshot: `seq, objects, users, chat, checks` |
| S→C | `users` `gone` `checks` `ack` `error` `ping` | participants / user left / saved versions / duplicate acknowledged / error / heartbeat |

Object kinds: `stroke` (freehand), `line`, `rect`, `oval`, `text`, `note` (sticky note).

## 3. Synchronization, ordering and concurrency
* The server runs one asyncio loop, so ops are processed one at a time: no locks, no torn state.
* Every accepted op gets the next `seq` and is broadcast; **`seq` is the total order**. Clients apply ops with the
  same reducer (`common.apply_op`) used by the server, so replicas converge.
* All ops are **idempotent** (add of an existing id, delete of a missing id = no-op), so duplicates are harmless.
* Clients draw their own ops immediately (optimistic) and keep them in a `pending` map until the server echoes
  them; the echo is then a no-op. Result: zero perceived latency and still one authoritative order.
* **Delayed / duplicated:** an op with `seq <= last` is ignored. **Missing:** an op with `seq > last+1` means a gap,
  so the client sends `resync` and replaces its state with a snapshot.
* **Concurrent drawing:** two users drawing at once just produce two `add` ops, ordered by arrival at the server.
  Concurrent erase of the same object: the second delete is a no-op.
* **Undo/redo (per user):** the server keeps a stack per user. Undo reverts only that user's own last action
  (add → delete, erase → restore, clear → restore all) and never touches other users' work. If someone else already
  changed the target (e.g. erased it), that entry is skipped and the next one is tried. A new action clears that
  user's redo stack. Undo/redo are issued as normal ops, so they are ordered and broadcast like any other.
* **Traffic:** a stroke preview sends only newly added points, throttled to ~25/s; cursors ~20/s; finished strokes
  are sent once as a single op; the pen drops points closer than 3 px.

## 4. Sessions, identity and access control
* The host creates a room (server returns a 6-character code). Others join with server address + code.
* Optional room password: stored on the server only as PBKDF2(password, salt); login uses challenge-response, so
  neither the password nor a reusable proof crosses the network at join time. (The derived key is sent once, when
  the host creates the room.) Failed logins are delayed 1 s.
* Roles: **host** (clear, save, restore, change roles, kick), **editor** (draw/erase/undo), **viewer** (watch + chat).
  The host picks the default role for newcomers and can change anyone's role live (right-click a participant).
  The server checks the role on every message, so a modified client cannot bypass it. All object data is validated
  (kind, color format, size limits, point count, bounds).
* Each user gets a color; their cursor shows it, and their default pen color matches it. Names are unique per room.

## 5. Connection management and recovery
* Server pings every 5 s; a client silent for 15 s is dropped. Clients treat 15 s without server traffic as dead.
* A dropped client keeps its place in the room (as "offline"); the room stays alive for everyone else.
* The client reconnects automatically (1, 2, 4, 8 s backoff), proving identity with its saved session token, and
  receives a fresh snapshot (the latest state). Anything drawn while offline stays on screen, is retransmitted after
  reconnect, and duplicates are filtered by object id.
* **Persistence:** rooms autosave to `data/<code>.json` (atomic write-then-rename) every 5 s when changed, and are
  reloaded when the server starts. **Save** (host) stores a named version (last 10 kept); **Restore** (host) replaces
  the board with it for everyone. Export SVG writes the current board to a file.

## 6. User guide
1. **Server:** on one machine run `lanvas-server` (or `python server.py`). Note the address it prints. Allow TCP
   47801 through the firewall. Keep the machine on the same LAN as the clients.
2. **Host:** run `lanvas-client`; enter the server address, your name, optionally a password; tick *Create a new
   room*, choose whether newcomers are editors or viewers; **Connect**. Tell others the room code (status bar).
3. **Join:** run `lanvas-client`; enter the server address, room code, name (and password); **Connect**.
4. **Draw:** pick Pen, Line, Rect, Oval, Text or Note; choose color and width; drag on the canvas. **Eraser** removes
   whole objects it touches. **Undo/Redo** (Ctrl+Z / Ctrl+Y) affect your own actions only.
5. **Collaborate:** coloured cursors show who is where; the Participants list shows who is online; chat is on the
   right (messages show sender and time).
6. **Host tools:** Clear, Save (named version), Restore (pick a version), right-click a participant to switch
   viewer/editor or kick. **Export SVG** is available to everyone. **Leave** returns to the login screen.

## 7. Requirement coverage and known omissions
All mandatory requirements (drawing, erasing, color, real-time sync, shared canvas, clear, save/restore,
session create/join, participants, join/leave notices, user identification, snapshot for newcomers, chat with
history, ordering/duplicate/missing handling, reconnection, state recovery, undo/redo) are implemented.
Extras: shapes (line/rect/oval), text, sticky notes, role-based permissions, password-protected rooms, version
history, SVG export, live stroke previews, cursors, offline drawing that syncs on reconnect, persistence across
server restarts. **Not implemented:** image insertion, presentation mode, mind maps/flowchart templates, moving or
resizing existing objects, zoom/pan, UDP auto-discovery of servers, traffic encryption (plain TCP on the LAN).
