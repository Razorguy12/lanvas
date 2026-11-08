# LANvas technical documentation

## 1. Architecture
Client-server over TCP/IP on a LAN; no internet needed. One server process (`server.py`) hosts any number of
rooms. Each room's authoritative state lives on the server: the ordered object list, participants, chat, saved
versions. Clients (`client.py`, PyQt6) hold a replica and paint it themselves (a custom QWidget with a zoom/pan
transform; the same renderer produces the PNG/JPG/PDF exports, so exports match the screen).

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
| C→S | `list` | (instead of `hello`) → S→C `rooms`: open, non-private rooms `{code, host, online, locked}` |
| C→S | `hello` | `user, room` (+ optional `want` role) or `create:true, salt, key, role, private, approval` (code is always server-assigned); optional session `token` |
| S→C | `waiting` | approval room (or an `admin` request): waiting at the door for the host/an admin |
| S→C / C→S | `req` / `grant{user,role}` `refuse{user}` `reqdone` | access requests to the host and admins and their answers; C→S `ask{role}` = in-room request |
| C→S→C | `present{on,v}` `view{v}` | presentation mode (admin+): `v = {z, cx, cy}` = presenter's zoom level and view centre |
| S→C | `challenge` | `salt, nonce` (only for password rooms) |
| C→S | `auth` | `proof` = HMAC-SHA256(key, nonce); the password is never sent |
| S→C→S | `recover` | reopening a closed room: server sends `ts, objects` of its last save; client answers `use:true/false` |
| S→C | `welcome` | `room, you{name,token,color}` + full snapshot (below) |
| C→S | `op` | `{k:"add",obj}`, `{k:"del",id}`, `{k:"set",id,attrs:{c,w,f,h}}` or `{k:"repl",rid,del:[ids],add:[objs]}`; obj = `{id,k,c,w,p,s,f,h}` (kind, color, width, points, text, fill, hidden); repl pieces carry `src` so they keep the original author |
| S→C | `op` | the same op plus `seq` (global order) and `by` (author); sent to everyone, sender included |
| C→S→C | `live` | in-progress stroke/shape preview; relayed to others, never stored or sequenced |
| C→S→C | `cur` | cursor position, relayed |
| C→S→C | `chat` | `text` (+ optional `to` = private message) → `{u, ts, text[, to]}`; private ones go only to sender and recipient |
| C→S | `undo` `redo` `clear` `save{name}` `restore{idx}` `role{user,role}` `kick{user}` `resync` | save: editor+; clear, restore, role, kick: admin+ |
| S→C | `snap` | full snapshot: `seq, objects, users, chat, checks` |
| S→C | `users` `gone` `checks` `ack` `error` `ping` | participants / user left / saved versions / duplicate acknowledged / error / heartbeat |

Object kinds: `stroke` (freehand), `line`, `rect`, `oval`, `text`, `note` (sticky note), `image` (`s` = base64
PNG, ≤ 700 KB, ≤ 40 per board). `welcome` also carries `boot` (changes on each server start) and `saved_at`.
S→C `bye` = the server is shutting down.

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
* **Fill** (`fill` op) sets an object's fill colour: rectangles, ovals, sticky notes, and pen strokes whose ends
  nearly meet (drawn as a filled polygon). Undo restores the previous fill.
* **Area eraser** ("Eraser") rubs out only the part under its circle (size follows the width slider): lines,
  strokes and rect/oval outlines are cut into pieces (`common.erase_pieces`). The client shows the result at once
  and sends the whole drag as one `repl` op (remove these ids, add these pieces), so it is one undo step and
  duplicate-safe (`rid`). Text and notes are not cut; use **Erase object** for those (old whole-object eraser).
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
* Roles: **host/owner** and **admin** (clear, restore, change roles, kick, present, erase or change anyone's work),
  **editor** (draw, save versions, erase/change **only their own** work), **viewer** (watch + chat). Admins can
  promote/demote editors and viewers (up to admin) but can't touch other admins or the owner; only the owner can.
* **Entering:** the room code is always assigned by the server. In an open room newcomers enter with the role the
  host chose (editor or viewer) and can be promoted later (right-click a participant), or ask with **Request role**.
  In an **approval** room (host ticks *Approval needed to enter*) a newcomer asks for viewer/editor/admin and waits
  until the host or an admin accepts (as asked or as another role) or refuses. Asking for admin always needs
  approval, even in open rooms. Returning members (session token) walk straight in.
  The server checks the role on every message, so a modified client cannot bypass it. All object data is validated
  (kind, color format, size limits, point count, bounds).
* Each user gets a color; their cursor shows it, and their default pen color matches it. Names are unique per room.

## 5. Connection management and recovery
* Server pings every 5 s; a client silent for 15 s is dropped. Clients treat 15 s without server traffic as dead.
* A dropped client keeps its place in the room (as "offline"); the room stays alive for everyone else.
* The client reconnects automatically (1, 2, 4, 8 s backoff), proving identity with its saved session token, and
  receives a fresh snapshot (the latest state). Anything drawn while offline stays on screen, is retransmitted after
  reconnect, and duplicates are filtered by object id.
* **Autosave:** a changed room is saved to `data/<code>.json` every 2 minutes (`--autosave`), atomically and
  fsync'd (write temp file, then rename), so a crash or power cut leaves the old or new save, never half of one.
* **Room close / room recovery:** a room empty for 30 s (`--grace`) is *closed*: saved and hidden from the lobby.
  Entering its code again (join, or Create with that code) asks the first person in: **restore the last saved
  version** or **start with an empty board** (members, roles, password and named versions are kept either way).
  Others arriving meanwhile wait for that answer. No answer in 2 min = restore.
* **Server disconnect notice:** a clean stop (Ctrl+C / SIGTERM) sends every client `bye`; a crash closes the
  sockets. Either way clients immediately show a red "Server disconnected — trying to reconnect" banner and a chat
  notice. When the server is back the client reconnects by itself; if the server restarted (`boot` changed) it
  says "the board was restored to its last saved version (time)".
* **Server recovery:** on start the server reloads every saved room. Rooms that were open when it stopped — even by
  a crash or `kill -9` — come back open at their last save point; clients reconnect automatically and get the
  snapshot (they get 90 s before the room is considered closed). Ctrl+C / SIGTERM save every room before exit.
* **Server logs:** `data/server.log` (rotated at 1 MB, 5 kept) and stderr: start-up, recovered rooms, logins and
  failures (with client address), joins/leaves/timeouts, room create/close/reopen, autosaves, host actions,
  errors. Chat content is never logged. `--log-level DEBUG|INFO|WARNING|ERROR`. **Save** (host) stores a named version (last 10 kept); **Restore** (host) replaces
  the board with it for everyone. Export SVG writes the current board to a file.

## 6. User guide
1. **Server:** on one machine run `lanvas-server` (or `python server.py`). Note the address it prints. Allow TCP
   47801 through the firewall. Keep the machine on the same LAN as the clients.
2. **Host:** run `lanvas-client`; enter the server address, your name, optionally a password and a room code of
   your own (blank = random); tick *Create a new room*, choose whether newcomers are editors or viewers, and tick
   *Private* to keep it out of the room list; **Connect**. Tell others the room code (status bar).
3. **Join:** the login screen lists open public rooms (Refresh; double-click to join), or type the room code.
   Entering the code of a closed room reopens it and asks whether to restore its last autosave.
4. **Draw:** pick Pen, Line, Rect, Oval, Text or Note; choose color and width; drag on the canvas. **Eraser** rubs
   out just the area under it; **Erase object** removes whole objects; **Fill** colours the inside of the shape you
   click. **Undo/Redo** (Ctrl+Z / Ctrl+Y) affect your own actions only.
5. **Collaborate:** coloured cursors show who is where; the Participants list shows who is online; chat is on the
   right (messages show sender and time).
   **Image** tool: click where it should go and pick a file. PNG, JPEG, GIF, BMP and WebP all work.
   Large images are shrunk to 800 px. **Zoom:** the − / 100% / + buttons, Ctrl+wheel (zooms at the pointer) or
   Ctrl + / − / 0. Scroll with the wheel, Shift+wheel or the scrollbars, or pan by dragging with the middle button. Pick a name in **To:** (or right-click a participant → *Message
   privately*) to send a private message only they can see. **Dark mode** toggles the theme (remembered).
6. **Host/admin tools:** Clear, Restore (pick a version), right-click a participant to change role or kick, answer
   access requests (a window pops up). **Save** (named version) is for editors and above. **Export** → SVG, PNG, JPG
   or PDF , available to everyone; invisible objects are left out.
7. **Design page:** the **Design** tab, or the ⌖ select tool (V) then click an object. Lists every object (even
   invisible ones); for the selected one you can make it invisible/visible, change stroke colour, width and fill,
   **clip** it (drag a rectangle; only the part inside is kept) or delete it. Undo works for all of these.
8. **Presentation mode:** a host/admin clicks **Present**: everyone goes full screen and follows the presenter's
   zoom and scrolling. Esc stops presenting (presenter) or leaves (followers; **Follow presentation** rejoins). **Leave** returns to the login screen.

### Interface
Figma/Canva-style layout: a top bar (room code chip, connection state, undo/redo, zoom, participant avatars,
Export, Present, ⋯ menu), a floating tool rail on the left (tools, colour swatches, width), the board page on a
dotted workspace in the middle, and a right panel with **People / Chat / Design** tabs. Rounded white cards with
soft shadows, hover/pressed states, tooltips that show each shortcut, toasts for feedback, light and dark themes.
Right-click an object for quick actions; right-click a person to message, change role or kick.

### Keyboard shortcuts (press `?` or F1 in the app for this list)
| Keys | Action | Keys | Action |
|---|---|---|---|
| P E X F L R O T N I V | Pen, Eraser, Erase object, Fill, Line, Rect, Oval, Text, Note, Image, Select/design | 1–8, C | Palette colour, custom colour |
| [ / ] | Thinner / thicker | Ctrl+Z, Ctrl+Y / Ctrl+Shift+Z | Undo, redo |
| Del, H | Delete / hide selected object | Ctrl+S, Ctrl+Shift+S | Save version, restore version |
| Ctrl+Shift+Backspace | Clear board (admin) | Ctrl+= / Ctrl+- / Ctrl+0 / Ctrl+1 | Zoom in / out / 100% / fit |
| Space+drag, middle-drag, wheel | Pan (Ctrl+wheel zooms) | F5, Esc | Present, cancel / leave presentation |
| Ctrl+D | Dark mode | Alt+1/2/3 | People / Chat / Design tab |
| / | Focus chat | Ctrl+E | Export |
| Ctrl+Shift+C | Copy room code | Ctrl+R, Ctrl+Q | Request role, leave room |

## 7. Requirement coverage and known omissions
All mandatory requirements (drawing, erasing, color, real-time sync, shared canvas, clear, save/restore,
session create/join, participants, join/leave notices, user identification, snapshot for newcomers, chat with
history, ordering/duplicate/missing handling, reconnection, state recovery, undo/redo) are implemented.
Extras: shapes (line/rect/oval), text, sticky notes, role-based permissions, password-protected rooms, version
history, SVG export, live stroke previews, cursors, offline drawing that syncs on reconnect, persistence across
server restarts, design page (hide / recolour / resize / clip objects), presentation mode, role requests and
approval rooms, PNG/JPG/PDF export, fill, area eraser, public room list, private messages, autosave with room/server recovery,
dark mode, server logs, image upload, zoom/pan. **Not implemented:** image insertion, presentation mode, mind maps/flowchart templates, moving or
resizing existing objects, UDP auto-discovery of servers, traffic encryption (plain TCP on the LAN).
