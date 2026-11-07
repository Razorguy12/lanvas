# Architecture.md

> How LANvas works. Any change to the protocol or data model **must** update this file, bump `PROTOCOL_VERSION`, and add or adjust tests.

> **Implementation notes (spec gaps filled while building, no wire change from a released version).** `HELLO` also carries `create`, `invite` and `last_seq` (§4.2). `ADMIN` has an S→C reply (§4.2). A sixth op kind, `bulk_create`, gives "bulk create" its wire form (§5.2). Server-to-client frames may reach 64 MB (§4.1). Announces carry `require_code` per room (§4.4). Crash-safety code lives once in `common/durable.py` (§12.1).

> **Spec revision 2** adds: private (direct) messages and retained chat history (§4.5), live board discovery (§4.4), secure save with Ctrl+S and file recovery (§12), and export to PNG/JPEG/SVG/PDF (§12.5). `PROTOCOL_VERSION = 2`.

---

## 1. Technology choices

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Required/recommended; fast to build |
| GUI | **PyQt6** (`QGraphicsScene` / `QGraphicsView`) | Scene graph gives selection, zoom, pan, transforms, z-order |
| Transport | **TCP** via raw `socket` | Reliable, ordered per connection |
| Discovery | **UDP** broadcast via raw `socket` | Connectionless, ideal for "who is out there?" |
| Server I/O | `socket` + `selectors` (single thread) | Low-level, no races, trivial ordering. **No asyncio streams, no socketserver, no websockets** |
| Crypto | `cryptography` (Ed25519, AES-256-GCM, scrypt, optional TLS cert gen); stdlib `hmac`, `hashlib`, `ssl` | Identity, signed/encrypted save files, transport protection |
| Export | Qt built-ins: `QImage` (PNG/JPEG), `QSvgGenerator` (SVG), `QPdfWriter` (PDF) | No extra dependency, works offline |
| Packaging | PyInstaller (build per OS) | Standalone executables |
| Tests | `pytest` | Protocol, ordering, ACL, chaos, persistence |

## 2. System overview

```
┌────────────────────────── Client (PyQt6) ──────────────────────────┐
│ UI thread: Canvas(QGraphicsScene) Toolbar Layers Props Chat Users  │
│    ▲ signals/slots                                    │ commands   │
│ DocumentModel (objects, local undo stack, pending ops)│            │
│ ChatModel (channels, history pages, unread, outbox)   │            │
│    ▲                                                  ▼            │
│ NetWorker thread: socket, framing, heartbeat, reconnect, outbox    │
│ DiscoveryWorker thread: UDP listen + DISCOVER, board list          │
│ RecoveryJournal: local snapshot + un-acked ops/chat (disk, worker) │
└──────────────────────────────┬─────────────────────────────────────┘
                 TCP (framed)  │   UDP broadcast (discovery only)
┌──────────────────────────────┴─────────────────────────────────────┐
│ Server (selectors event loop, single thread)                       │
│  Acceptor → Connection(state machine) → Dispatcher                 │
│  AuthManager + ACL │ RoomManager │ OpLog + seq │ Presence          │
│  ChatStore (room + DM channels) │ Discovery(UDP announce/respond)  │
│  Persistence: durable writer (atomic files, checksums, fsync)      │
└────────────────────────────────────────────────────────────────────┘
```

The server is the **single authority** for: membership, permissions, operation order, chat order and retention, and canonical board state. Clients are untrusted.

## 3. Repository layout

```
lanvas/
├─ common/
│   ├─ protocol.py      # framing, message types, encode/decode, constants
│   ├─ model.py         # object schema, property validation, apply_op()
│   ├─ fractional.py    # fractional indexing for z-order
│   ├─ identity.py      # Ed25519 keys, fingerprints, signatures
│   ├─ chat.py          # chat message schema, channel ids, validation
│   ├─ snapshot.py      # canonical JSON, board hash, verified recovery snapshots
│   ├─ durable.py       # the durable writer, shared by server and client (§12.1)
│   ├─ netutil.py       # IPv4 interfaces, broadcast addresses, shared discovery socket
│   └─ simplify.py      # Ramer-Douglas-Peucker stroke simplification (§9)
├─ server/
│   ├─ main.py          # CLI entry (+ optional status window)
│   ├─ loop.py          # selectors loop, Connection class
│   ├─ rooms.py         # Room, membership, presence
│   ├─ acl.py           # roles, requests, invites, rate limits
│   ├─ oplog.py         # append-only log, snapshots, checkpoints, startup recovery
│   ├─ chatstore.py     # room chat + DM channels, history, retention
│   ├─ discovery.py     # UDP announce/respond
│   └─ persist.py       # room paths and durable writer helpers
├─ client/
│   ├─ main.py
│   ├─ transport.py     # NetCore: socket thread, handshake, reconnect, inspector, chaos (no Qt)
│   ├─ net.py           # NetWorker: re-emits NetCore events as queued Qt signals
│   ├─ session.py       # one open room: document, chat, journal, saving (no Qt)
│   ├─ boardlist.py     # board tracking, expiry, validation of announces (no Qt)
│   ├─ discovery.py     # DiscoveryWorker (QThread), live board list
│   ├─ document.py      # client-side model, optimistic apply, undo/redo
│   ├─ chatmodel.py     # channels, history paging, unread counts, outbox
│   ├─ recovery.py      # local journal, crash detection, recovery dialog data
│   ├─ export/          # formats.py (PNG/JPEG/SVG/PDF), renderer.py
│   ├─ canvas/          # view, items, tools (select, pen, shape, text…)
│   ├─ panels/          # layers, properties, chat, sharing, inspector
│   └─ theme.py         # tokens from Design.md
├─ tests/
├─ docs/  (this file, Design.md, ProductContext.md, Agents.md)
└─ build/ (pyinstaller specs)
```

Rules: `common/` and `server/` import **no Qt**. `client/` never imports `server/`. `common/snapshot.py` is shared so that the server and client produce byte-identical canonical JSON and hashes.

## 4. Protocol

### 4.1 Framing (TCP is a byte stream)

Every message is one frame:

```
 0        2     3     4          5        8
 ├─ magic ─┤ ver ┤type┤  flags   ├─ length (u32, big-endian) ─┤ payload ...
   "LV"      u8    u8     u8
```

- Header is 9 bytes: `magic(2) ver(1) type(1) flags(1) length(4)`.
- `flags` bit0 = payload zlib-compressed (used when payload > 1 KB).
- Payload is UTF-8 JSON (human-debuggable, fine for a LAN).
- `length` is capped at 8 MB for client-to-server frames and 64 KB before authentication. Oversize or bad magic closes the connection. Server-to-client frames may reach 64 MB, because a full snapshot with embedded images can exceed 8 MB after compression; inflation is capped at 64 MB in both directions.
- Receiver uses a per-connection buffer and a `recv_exact`-style state machine (non-blocking on the server).

### 4.2 Message types

| Type | Dir | Purpose | Reliability |
|---|---|---|---|
| `HELLO` | C→S | version, username, identity pubkey, and `room_id` (from discovery) **or** `room_code` **or** `create{name, code, discoverable, require_code, show_member_count}`; optional `invite` token and `last_seq` | n/a |
| `CHALLENGE` | S→C | random nonce for identity proof | n/a |
| `PROOF` | C→S | Ed25519 signature of nonce | n/a |
| `ACCESS_PENDING` | S→C | not authorized, request recorded | n/a |
| `ACCESS_REQUEST` | S→owner | who is asking (name, IP, fingerprint) | logged |
| `ACCESS_DECISION` | owner→S | approve(role) / deny | logged |
| `WELCOME` | S→C | role, client_id, `last_seq`, snapshot or delta, `chat_tail`, `dm_index`, `last_checkpoint` | n/a |
| `OP` | both | board mutation (§5) | sequenced |
| `OP_ACK` / `OP_REJECT` | S→C | seq assigned, or error code | n/a |
| `TRANSIENT` | both | cursor, selection, live stroke preview | **not logged**, droppable |
| `CHAT` | both | room or direct message (§4.5) | sequenced per channel, persisted |
| `CHAT_ACK` / `CHAT_REJECT` | S→C | `chat_seq` assigned, or error code | n/a |
| `CHAT_HISTORY` | both | request an older page of a channel / the page in reply | n/a |
| `CHAT_READ` | C→S | mark a channel read up to `chat_seq` (unread sync across devices) | n/a |
| `PRESENCE` | S→C | join, leave, away, role change | n/a |
| `RESYNC` / `SNAPSHOT` | both | gap recovery | n/a |
| `SAVE` | C→S | Ctrl+S: request a durable server checkpoint (§12.3) | n/a |
| `SAVE_ACK` / `SAVE_REJECT` | S→C | checkpoint `seq`, `board_hash`, `checkpoint_id`, time; or error code | n/a |
| `ADMIN` | both | C→S: `set_role`, `kick`, `revoke`, `mute`, `set_settings` (includes lock), `rename`, `clear_chat`, `set_retention`, `create_invite`, `list_checkpoints`, `delete_checkpoint`, `delete_room`. S→C: `{action, ok, ...}` reply, e.g. the invite token or checkpoint list. A refusal is a non-fatal `ERROR` | logged |
| `PING` / `PONG` | both | liveness plus RTT | n/a |
| `ERROR` | S→C | code and message | n/a |

### 4.3 Why TCP, and why UDP for discovery only

Drawing state and chat need **reliable, ordered** delivery; reimplementing that over UDP adds complexity with no benefit on a LAN. Cursor positions would tolerate loss, but a single TCP connection keeps auth and ordering simple; we mitigate head-of-line blocking by throttling transients. UDP is used only for discovery, which is naturally connectionless and must work before the client knows any address.

### 4.4 Discovery: live list of available boards (UDP)

Goal: as soon as a device is on the network and the launcher is open, it **shows the boards it can join**, without typing an address.

**Server side (`server/discovery.py`)**
- Binds UDP `47800` with `SO_REUSEADDR` (and `SO_REUSEPORT` where available) and `SO_BROADCAST`.
- **Announces** `LANVAS_ANNOUNCE` every **2 s** to the broadcast address of every IPv4 interface (enumerated, not only `255.255.255.255`), and once immediately on start and whenever a discoverable room is created, renamed, hidden, or deleted.
- **Responds** to `LANVAS_DISCOVER` with a unicast `LANVAS_ANNOUNCE` to the sender.
- Announce payload (JSON, kept under 1200 bytes; paginated with `page`/`pages` if a server hosts many rooms):

```
{ "t": "LANVAS_ANNOUNCE", "server_id": "<random per install>", "server_name": "Lab-PC-3",
  "addrs": ["192.168.1.10"], "tcp_port": 47801, "protocol_version": 2,
  "rooms": [ { "room_id": "r_8f2c…", "name": "Sprint-1", "requires_approval": true,
               "require_code": false, "host_name": "Alice", "member_count": 3 } ] }
               # member_count only if the owner allows. require_code tells the launcher to ask first.
```

A server with no rooms still announces (`"rooms": []`), so the Create room dialog can list it. Announces also go to `127.255.255.255`, so a machine hosting its own server sees it with no network at all.

- **Never advertised:** room codes, member names, board content, chat, hidden rooms.
- Hidden rooms are joinable only by IP + room code.

**Client side (`client/discovery.py`, `DiscoveryWorker` thread)**
- Opens when the launcher opens (and stays alive in the background while a board is open, for the **Switch board** menu).
- Binds UDP `47800` (shared with a local server via `SO_REUSEADDR`) to receive announces passively, so a device that **just joined the network** sees boards within one announce interval.
- Also sends `LANVAS_DISCOVER` immediately on start, on **Refresh**, and when its set of network interfaces changes (polled every 5 s), so results appear without waiting.
- Maintains a board list keyed by `(server_id, room_id)`; entries **expire after 7 s** without an announce (about 3 missed intervals). The UI is updated by signal only when the list changes.
- Drops packets that are oversized, malformed, from an incompatible `protocol_version` (shown greyed as "needs update"), or that arrive faster than a per-source rate limit.

**Joining from the list:** the client sends `HELLO{room_id}`. Because the room code is a *locator*, not a credential (§8), a discovered room does not need the code; owner approval or an invite is still required. A room may set `require_code = true`, in which case the launcher asks for the code before sending `HELLO`.

**Abuse controls:** replies are unicast and never larger than one announce; at most 5 replies/s per source IP; the server ignores discover packets over 512 bytes.

### 4.5 Chat: room chat, private messages, and retention

**Channels**
- `room`: one per room, every `ACTIVE` member receives it.
- `dm:<id>`: one per pair of members, `id = sha256(sorted([fp_a, fp_b]))[:16]`. Delivered **only** to the connections of those two fingerprints. Addressing is by **fingerprint**, never username (usernames can collide).

**CHAT payload (C→S)**
```
CHAT { msg_id: "clientId:counter", channel: "room" | "dm", to: "<fingerprint>" (dm only),
       text: "≤ 2000 chars", reply_to: <chat_seq>? }
```

**Server algorithm (same single thread as ops)**
1. Connection must be `ACTIVE`; sender not muted (mute applies to room and DMs); for DMs the recipient must be a current member of the room (online or offline). Otherwise `CHAT_REJECT{code}`.
2. Dedupe by `msg_id` (re-send `CHAT_ACK`).
3. Validate length, control characters stripped, rate limit (token bucket, e.g. 5 msgs/s burst 10).
4. Assign `chat_seq = ++channel.seq`, stamp server UTC time, append to the channel's `.jsonl` file (durable writer, §12.1).
5. Deliver `CHAT{channel_id, chat_seq, from_fp, from_name, ts, text}`: room → all `ACTIVE` members; DM → sender's and recipient's live connections only. Offline DM recipients get it from history on next connect.

**Retention (chat is kept, not ephemeral)**
- Room chat and DMs are **persisted on the server** and survive client reconnects and server restarts.
- Default retention: **kept for the lifetime of the room**. The owner can set a policy: keep forever / last N messages / last N days. Pruning runs at startup and every hour; it never removes messages newer than the newest checkpoint export that included chat.
- The owner can **clear room chat** (`ADMIN clear_chat`, logged). Owners cannot read or clear other members' DMs through the UI.
- On `WELCOME`, the client receives `chat_tail` (last 50 room messages) and `dm_index` (for each DM channel: peer fingerprint and name, last message preview, unread count). Older messages are fetched with `CHAT_HISTORY{channel_id, before_chat_seq, limit ≤ 100}`.
- Unread positions are stored server-side per member (`CHAT_READ`) so they follow the user across reconnects.
- Revoked or kicked members stop receiving chat and DMs immediately; their earlier messages stay in history under their name.
- Un-acked outgoing chat is kept in the client outbox and recovery journal and re-sent after reconnect (deduped by `msg_id`).

**Honest privacy note (for the report):** DMs are hidden from other members, but they are stored in plaintext on the **server machine's disk**, and travel in plaintext on the LAN unless TLS is enabled (§8). They are not end-to-end encrypted.

## 5. Data model, ordering and concurrency

### 5.1 Object model (Figma-like)

```
Board
 └─ objects: { id → Object }           # flat map, tree via parent
Object { id, type, parent, z (fractional index), props{...}, deleted }
 type ∈ frame | rect | ellipse | line | arrow | path | text | sticky | image | group
 props: x, y, w, h, rotation, fill, stroke, stroke_w, opacity, locked,
        points[] (path), text, font_size, … (validated per type)
```

A freehand **stroke is a `path` object**; the **eraser deletes path objects** (object-level erase). This avoids pixel-merge conflicts.

### 5.2 Operations

```
OP { op_id: "clientId:counter", base_seq, kind, ... }
 kind = create {id,type,parent,z,props}
      | update {id,patch:{prop:value,…}}
      | delete {id}
      | clear  {}                       # permission required
      | restore {checkpoint_id}         # owner only: board becomes that checkpoint (§12.3)
      | bulk_create {objects[], replace?}  # undo of clear/restore; replace=true is owner only
```

`restore` is expanded by the server before logging: the op carries the checkpoint's complete object list (tombstones included) and replaces the board in a **single seq**. It is atomic for every client, replay never depends on the checkpoint file still existing, and the board's hash equals the checkpoint's hash exactly (SV-11). Undoing a `restore` is `bulk_create{replace:true}` with the previous objects, which returns the previous hash exactly. Undoing a `clear` is `bulk_create` of the objects it removed. Creating a room from a file uses the same `replace` form.

Fields an op may set are whitelisted by the server. `seq`, `author`, and `author_name` are always the server's own (SE-04).

### 5.3 Server algorithm (single thread, so no locks)

1. Authenticate connection → role. Reject if role cannot perform `kind`.
2. **Dedupe**: if `op_id` already seen → re-send `OP_ACK` (idempotent).
3. **Validate** schema, property types and ranges, object limits (max objects, max points, max text length).
4. Assign `seq = ++room.seq`; append to `ops.jsonl` through the durable writer; update canonical state.
5. Broadcast `OP{seq,...}` to all authorized members (including the sender as ack).

### 5.4 Conflict resolution: per-property last-writer-wins by `seq`

- Two users moving the same object: each property is a register; higher `seq` wins. Everyone converges because the visible state is a pure function of the ordered log.
- `update` on a deleted object is ignored. `delete` wins over concurrent `update`.
- Z-order uses **fractional indexing** (insert between neighbors without renumbering).
- **Optimistic UI:** the client applies its own op immediately, then reconciles to the authoritative `seq` order when the broadcast returns (re-applying still-pending local ops on top).

### 5.5 Undo / redo (multi-user, well-defined)

- Undo/redo is **per user**: it affects only *your own* actions.
- The client records `(forward, inverse)` for each action. Undo sends the inverse as a **new op** (so history stays append-only and is broadcast normally).
- **Conflict-aware:** an inverse patch applies per property **only if** the current value still equals what your op wrote. Properties changed by others since are skipped, and the UI shows a small toast ("1 change not undone: edited by Sam").
- Undoing a create sends a delete; undoing a delete sends a create with the original id and props.
- `clear` and `restore` are undoable by their issuer via a bulk create of the removed objects.

### 5.6 Reliability: delayed, duplicated, missing

| Problem | Handling |
|---|---|
| Duplicate op / chat | `op_id` / `msg_id` dedupe on server; `seq` / `chat_seq` dedupe on client |
| Out-of-order delivery (via chaos shim / reconnect) | Client buffers by `seq` (and per-channel `chat_seq`), applies in order |
| Gap detected (`seq` jumps) | Client sends `RESYNC{from_seq}`; server replays, or sends `SNAPSHOT` if gap is large |
| Chat gap | Client requests `CHAT_HISTORY` for the missing range |
| Lost connection | See §7 |
| Client or server crash | See §12 (durable writes, recovery) |

## 6. Identity, access control and sessions

### 6.1 Identity

On first launch the client generates an **Ed25519 keypair** (stored in the user's config dir). The **fingerprint** (short hash of the public key) is the stable user ID. Usernames are display-only and may collide; the fingerprint is what the ACL, DM addressing, and save-file signatures track.

### 6.2 Connection state machine (server)

```
CONNECTED → (HELLO) → CHALLENGED → (PROOF ok) → AUTHENTICATED
   ├─ identity in ACL as member ─────────────► ACTIVE(role)
   ├─ identity pending ──────────────────────► PENDING   (only ACCESS_PENDING / PING allowed)
   ├─ identity denied/revoked ───────────────► DENIED    (close, cool-down)
   └─ unknown identity ──► create request ───► PENDING, notify owner
```

In `PENDING`/`DENIED`, the server **never** sends snapshots, ops, chat (room or DM), chat history, checkpoints, or participant lists, and drops any other frame type. This is enforced server-side regardless of the UI.

### 6.3 Room ACL (persisted in `acl.json`)

```
owner: <fingerprint>
members: { fingerprint → {role, name, added_at, muted} }
requests: { fingerprint → {name, ip, time, status} }
invites: { token_hash → {role, expires, single_use} }
settings: { editors_can_clear, viewers_can_export, require_code, chat_retention, show_member_count }
```

Permission matrix:

| Action | owner | editor | viewer | pending |
|---|:-:|:-:|:-:|:-:|
| Receive board, room chat, presence | ✔ | ✔ | ✔ | ✘ |
| create / update / delete objects | ✔ | ✔ | ✘ | ✘ |
| Send room chat | ✔ | ✔ | ✔ (owner can mute) | ✘ |
| Send / receive private messages (DMs) | ✔ | ✔ | ✔ (owner can mute) | ✘ |
| Read chat history (room + own DMs only) | ✔ | ✔ | ✔ | ✘ |
| Clear room chat, set chat retention | ✔ | ✘ | ✘ | ✘ |
| `SAVE` (server checkpoint, Ctrl+S) | ✔ | ✔ | ✘ | ✘ |
| Export (client-side) | ✔ | ✔ | owner-configurable | ✘ |
| Clear board | ✔ | owner-configurable | ✘ | ✘ |
| Restore a checkpoint | ✔ | ✘ | ✘ | ✘ |
| Approve/deny, set roles, kick, mute, lock board | ✔ | ✘ | ✘ | ✘ |

Role changes and revocation take effect immediately: the server updates the live connection and sends `PRESENCE`.

Export permission for viewers is enforced by the client UI only: a viewer already receives the full board, so the server cannot stop a modified client from rendering it. The setting is a courtesy control, and the report says so.

### 6.4 Owner offline

Requests queue in the ACL; owner gets them on next connect. Approved members keep working and chatting. A room may be configured so editors continue even when the owner is away.

## 7. Connection management and recovery

- `PING`/`PONG` every 5 s; no traffic for 15 s → mark **away**; 60 s → release the slot (ACL membership is *not* lost).
- Client reconnects with exponential backoff, presents identity, sends `last_seq` and the last `chat_seq` per open channel.
- Server replies with the ops after `last_seq`, or a full snapshot if too old, plus missed chat. Client keeps un-acked local ops and chat and re-sends them (server dedupes by `op_id` / `msg_id`).
- Server restart: run the integrity scan (§12.4), then rebuild state from the newest valid snapshot plus the `ops.jsonl` tail.
- Automatic snapshots every 200 ops or 2 minutes; manual checkpoints on Ctrl+S (§12.3).

## 8. Security

- **Authentication:** Ed25519 challenge-response; no passwords on the wire.
- **Authorization:** enforced per frame on the server (see matrix). Clients are never trusted.
- **Room code** is a locator, not a credential. Even with the code, access needs owner approval (or a valid invite).
- **Invites:** random single-use token, stored hashed, with expiry.
- **DM routing:** the server delivers a DM only to the two fingerprints of the channel; history requests are checked against the requester's fingerprint.
- **Save files:** written atomically, checksummed, signed with the saver's identity key, optionally encrypted with a password (§12.5). Files in the user data dir use `0600` where supported.
- **Abuse controls:** frame-size caps, per-connection token-bucket rate limit (ops and chat), per-IP request limits and cool-down, max objects and points, JSON depth limit, discovery reply rate limit.
- **Transport:** Tier 3 target is TLS via `ssl` wrapping the raw socket with a self-signed cert generated on first server run, plus trust-on-first-use pinning in the client. If TLS slips, the access model above still holds, but traffic (including DMs) is plaintext on the LAN. Document this honestly.
- **Threat notes for the report:** same-LAN sniffing (mitigated by TLS), rogue client sending malformed frames (validation), impersonation (identity keys), brute-force requests (rate limits), replay of `PROOF` (fresh nonce per connection), DM snooping by other members (server-side routing), tampered save files (hash + signature), discovery amplification (unicast, rate-limited replies).

## 9. Performance

- Live pen strokes: client sends `TRANSIENT stroke_preview` batches at about 30 Hz; commits a single `create path` on mouse-up with points simplified by **Ramer–Douglas–Peucker**.
- Drag and resize: send `update` at most every 33 ms with coalescing (only the latest patch per object).
- Cursors/selections: throttled to about 20 Hz, dropped if the outbox is backed up.
- zlib for payloads > 1 KB; snapshots always compressed.
- Server write path uses a per-connection outbox with `EVENT_WRITE` registered only when non-empty; slow clients are disconnected when the outbox exceeds a cap.
- Disk: `fsync` is batched (group commit within a ≤ 100 ms window) so per-op latency stays low; `SAVE` forces an immediate `fsync`.
- Export rendering of large boards is tiled and reports progress; file writing happens off the UI thread.
- Stats (bytes/s, msgs/s, RTT, seq lag) are surfaced in the Packet Inspector.

## 10. Threading (client)

- **UI thread:** all Qt objects, including the scene render used by export. Never blocks on the network or on large disk writes.
- **NetWorker thread:** owns the TCP socket; receives frames, emits Qt signals (`op_received`, `chat_received`, `presence`, `save_ack`, …); sends from a thread-safe queue.
- **DiscoveryWorker thread:** owns the UDP socket; emits `boards_changed(list)`.
- **IO worker (`QThreadPool`):** recovery journal writes and encoding/writing export files.
- Qt widgets are never touched from worker threads.

## 11. Packet Inspector and Chaos Simulator (client-side shim)

A `Transport` wrapper sits between framing and the socket:

- **Inspector:** records `(time, dir, type, seq, size, rtt)` into a ring buffer for the UI. DM text is shown as `«private, 42 chars»` unless the inspecting user is a participant of that DM.
- **Chaos:** optionally adds delay, duplicates frames, reorders *application delivery*, or force-closes the connection. Used for tests and the demo.

## 12. Persistence, secure save, recovery and export

### 12.1 Durable writer (server and client)

All persistent files go through one helper, `common/durable.py`. `server/persist.py` and `client/recovery.py` re-export it, because two copies of crash-safety code would drift. Three rules:

1. **Whole-file writes are atomic:** write `<name>.tmp` → `flush` → `os.fsync` → `os.replace(tmp, name)` → `fsync` the directory (POSIX). A crash leaves either the old file or the new file, never a half file.
2. **Append-only logs are self-checking:** each line of `ops.jsonl` and chat logs is `{"seq":…, "data":…, "crc":<crc32 of data>}`. A torn or corrupt **last** line is detected and truncated on startup; corruption in the middle stops replay at the last good line and falls back as in §12.4.
3. **Every snapshot and checkpoint carries a SHA-256** of its canonical board JSON, verified before use.

Line endings are normalized to `\n`; all paths use `pathlib`.

### 12.2 Persistence layout

```
data/rooms/<room_id>/
   room.json                 # name, code hash, settings
   acl.json
   ops.jsonl                 # append-only, one op per line: seq, data, crc
   chat/
      room.jsonl             # room channel, chat_seq-ordered
      dm/<channel_id>.jsonl  # one file per pair of members
      read.json              # unread positions per member
   snapshots/<seq>.json.gz   # automatic, keep the newest 10
   checkpoints/<seq>.json.gz # Ctrl+S checkpoints, kept until the owner deletes them
   checkpoints/index.json    # checkpoint_id, seq, board_hash, saved_by, label, time
   recovery.log              # what the integrity scan found and fixed
```

Snapshot / checkpoint file:
```
{ "format": "lanvas-snapshot", "version": 2, "room_id": "…", "seq": 1234,
  "created_at": "UTC ISO-8601", "board": { …canonical… }, "board_hash": "sha256:…" }
```

### 12.3 Ctrl+S: secure save

Pressing **Ctrl+S** (or the Save button) requests a verified server checkpoint:

1. **Server checkpoint** (owner/editor): the client sends `SAVE{label?}`. The server flushes and `fsync`s `ops.jsonl` and chat logs, writes a checksummed checkpoint at the current `seq` atomically, adds it to `checkpoints/index.json`, and replies `SAVE_ACK{checkpoint_id, seq, board_hash, time}`. The client compares `board_hash` with its own; a mismatch triggers `RESYNC` and a warning toast. Viewers get `SAVE_REJECT{NOT_ALLOWED}` and the UI explains it.


The owner can browse checkpoints (Version history) and `restore` one (§5.2).

### 12.4 Recovery

**Server startup integrity scan (`server/oplog.py`)**
1. For each room, load `acl.json` and `room.json`; on parse failure, use `<name>.bak` (the previous atomic version is kept as `.bak`).
2. Pick the newest snapshot or checkpoint whose SHA-256 verifies; skip any that fail and note them in `recovery.log`.
3. Replay `ops.jsonl` lines with `seq` > snapshot seq while CRCs verify; truncate a torn tail.
4. Recompute `board_hash` and log `room, seq, hash, repairs`. The server prints a one-line summary per room.
5. Chat logs get the same CRC check and tail truncation.

**Client crash recovery (`client/recovery.py`)**
- While a board is open, the client keeps a **recovery journal** in its user data dir: the last confirmed board (rewritten atomically every 30 s or 200 ops) plus an append log of un-acked local ops and unsent chat.
- A `session.lock` file (with PID) is created on open and removed on clean exit.
- On next launch, a stale lock means the client crashed. The **Recover** dialog offers: *Rejoin and send my unsaved changes* (re-sent with original `op_id`s, so the server dedupes anything that already arrived) or *Discard*.

### 12.5 Export

All exports are written atomically through the durable writer. Scope can be: whole board, current selection, or one frame.

| Format | How | Notes |
|---|---|---|
| **PNG** | `QGraphicsScene.render` into a `QImage` | 1x / 2x / 4x scale, transparent or canvas background |
| **JPEG** | same, quality 90 | no transparency; background filled |
| **SVG** | `QSvgGenerator` | vector; text kept as text; images embedded as base64 |
| **PDF** | `QPdfWriter` | vector; page = fit to content, or A4/A3 landscape; option **each frame as one page** |

## 13. Testing strategy

| Test | What it proves |
|---|---|
| Framing fuzz (split/merged/partial frames) | Stream reassembly |
| Ordering with N simulated clients | All reach identical board hash |
| Dedupe and gap/resync | Reliability rules |
| ACL matrix | Every role/action pair, including raw-socket bypass attempts |
| Chat routing and retention | DMs reach only their two members; history survives restarts |
| Discovery | New devices see boards quickly; nothing private is advertised |
| Undo conflict cases | §5.5 behavior |
| Reconnect and server restart | Recovery |
| Crash and corruption injection | Durable writer and integrity scan |
| Export round trips | Every supported export opens and renders the board |
| Chaos soak | Convergence under delay/duplicates |

**Convergence check:** after any test, every client computes `sha256(canonical_json(board))`; all hashes must match the server's.

---

## 14. Test procedure (full)

Section 13 is the summary; this is the procedure the team follows. Test IDs are referenced from `ProductContext.md` (acceptance) and `Agents.md` (workflow, phase gates).

### 14.1 Test layers

| Layer | Scope | Needs | Runs |
|---|---|---|---|
| **L0 Unit** | Pure functions: framing, `apply_op`, fractional index, RDP, identity, ACL rules, chat validation, snapshot hashing, CRC/atomic writer | nothing | every save / push |
| **L1 Component** | Server loop and connection state machine over `socket.socketpair()` and a fake clock | nothing | every push |
| **L2 Integration** | Real server on `127.0.0.1:0` (ephemeral port) plus headless **bot clients** over raw TCP and UDP; no Qt | one machine | every push |
| **L3 LAN system** | Real GUI clients on **3+ physical machines**, mixed OS, internet disabled | a router/hotspot | at every phase gate that includes a GUI, plus once per working session from Phase 3 on |
| **L4 Acceptance** | The demo script and requirement checklist (`ProductContext.md` §12, §16) | whole team | Phase 7 gate and demo day |

### 14.2 Harness (`tests/harness.py`)

- `start_server(tmpdir)` returns `(port, handle)`; uses a temp data dir, so tests never touch real rooms. `handle.kill()` simulates a crash (no clean shutdown); `handle.restart()` runs the integrity scan.
- `BotClient(port, name, key)` implements HELLO/PROOF, `send_op()`, `send_chat()`, `send_dm(fp)`, `save()`, `recv_until(seq)`, `board_hash()`. Bots use the **same `common/protocol.py`** but their own socket code, so framing bugs are not hidden.
- `DiscoveryProbe(port)` sends/receives UDP on a test port.
- `FaultyFS` wraps the durable writer to inject a crash after any write step (after `tmp` write, before `replace`, mid-append).
- `FakeClock` injected into the server for heartbeat, retention, and timeout tests (no real sleeping).
- Randomized tests print their **seed** on failure and accept `--seed` to reproduce.
- UI and export tests use `QT_QPA_PLATFORM=offscreen`; nothing in L0-L2 server tests may need a display.
- pytest markers: `unit`, `integration`, `security`, `chaos`, `perf`, `lan`, `chat`, `discovery`, `persist`, `export`.

### 14.3 Test case catalogue

**Framing (FR)**

| ID | Case | Pass criteria |
|---|---|---|
| FR-01 | Frame split at every byte boundary | Reassembled identically |
| FR-02 | Many frames in one `recv` | All decoded, in order |
| FR-03 | Length above cap (8 MB; 64 KB pre-auth) | Connection closed, no memory growth |
| FR-04 | Bad magic or version | Closed with `ERROR` |
| FR-05 | Compressed flag round trip (>1 KB) | Payload equal |
| FR-06 | Invalid JSON, wrong types, JSON depth bomb | Rejected, server alive |

**Ordering and concurrency (OR)**

| ID | Case | Pass criteria |
|---|---|---|
| OR-01 | 10 bots x 200 random create/update/delete, concurrent | All client hashes == server hash |
| OR-02 | Inspect assigned `seq` | Strictly increasing, no gaps |
| OR-03 | Same `op_id` sent twice | One `seq`; second returns same ack |
| OR-04 | Update on a deleted object | Ignored, no resurrection |
| OR-05 | Two users set the same property | Higher `seq` wins on all clients |
| OR-06 | 1000 z-order inserts in the same gap | No collisions; rebalance works |

**Undo / redo (UN)**

| ID | Case | Pass criteria |
|---|---|---|
| UN-01 | Undo create / redo create | Object removed / restored with same id |
| UN-02 | Undo move (no conflict) | Original position restored |
| UN-03 | Undo move after another user edited that property | Property skipped, toast raised, others still undone |
| UN-04 | User A undo never touches user B's ops | Verified |
| UN-05 | Undo `clear` / undo `restore` | Objects restored by the issuer |

**Access control and security (AC / SE)**

| ID | Case | Pass criteria |
|---|---|---|
| AC-01 | Role x message-type matrix (generated table) | Every cell matches the §6.3 permission matrix |
| AC-02 | Unknown identity connects with correct room code or room_id | Only `ACCESS_PENDING`; **zero** SNAPSHOT/OP/CHAT/CHAT_HISTORY/PRESENCE bytes |
| AC-03 | Raw `OP` before authentication | Connection closed |
| AC-04 | `PROOF` signed with the wrong key | Denied |
| AC-05 | Replay an old `PROOF` on a new connection | Rejected (fresh nonce) |
| AC-06 | Owner approves a pending connection | Live transition to `ACTIVE`, receives snapshot and chat tail |
| AC-07 | Owner revokes or kicks a live member | Connection downgraded/closed immediately; no further chat or DMs delivered |
| AC-08 | Viewer sends `OP` | `OP_REJECT`; state unchanged on all clients |
| AC-09 | Invite reused / expired | Second use fails; expired fails |
| AC-10 | Access-request flood from one IP | Rate limit plus cool-down |
| AC-11 | Server restart | ACL, roles, pending requests, mute flags preserved |
| AC-12 | Owner offline | Request queues; owner sees it on next login; members keep working |
| SE-01 | 10,000 random-byte/garbled frames | Server never crashes; memory bounded |
| SE-02 | Slow-loris (partial frame stalls) | Timeout and close |
| SE-03 | Too many connections from one IP | Cap enforced |
| SE-04 | Oversized arrays/strings/objects in an op | `OP_REJECT` with code |

**Chat and private messages (CT)**

| ID | Case | Pass criteria |
|---|---|---|
| CT-01 | Room message from each role | Delivered to all `ACTIVE` members with server timestamp and correct sender |
| CT-02 | DM from A to B with a third bot C listening on a raw socket | A and B receive it; C receives **zero** bytes of it |
| CT-03 | DM to an offline member | Stored; appears in `dm_index` with unread count on next connect |
| CT-04 | Same `msg_id` sent twice | One `chat_seq`; second returns same ack |
| CT-05 | Pending/denied identity sends CHAT or CHAT_HISTORY, or DMs a member | Dropped; nothing delivered or returned |
| CT-06 | DM to a fingerprint that is not a room member | `CHAT_REJECT{UNKNOWN_RECIPIENT}` |
| CT-07 | `CHAT_HISTORY` for a DM channel the requester is not part of | Rejected, no data |
| CT-08 | Server restart | Room chat and DMs, order and timestamps, and unread positions preserved |
| CT-09 | History paging over 500 messages | Pages are contiguous, no gaps or duplicates |
| CT-10 | Retention "last N" / "N days" (fake clock); owner clears room chat | Only expected messages remain; DMs untouched by room clear |
| CT-11 | Muted member sends room chat / DM | Both rejected |
| CT-12 | Chat flood | Rate limit applies; server responsive |

**Discovery (DS)**

| ID | Case | Pass criteria |
|---|---|---|
| DS-01 | Client starts after server; no user action | Board list populated within 2 s (DISCOVER reply) |
| DS-02 | Client already listening; server starts or a room is created | Board appears within one announce interval (≤ 2 s + jitter) |
| DS-03 | Server stopped / room hidden or deleted | Entry disappears within 7 s |
| DS-04 | Inspect announce bytes | No room codes, member names, board or chat content; hidden rooms absent |
| DS-05 | Multiple interfaces (Wi-Fi + VM bridge) and server and client on the same host | Rooms found on the right interface; port sharing works |
| DS-06 | Malformed, oversized, or flood DISCOVER packets | Ignored; reply rate limited; server responsive |
| DS-07 | Join from the list with `room_id` (no code) | Normal approval flow; `require_code` rooms ask for the code |
| DS-08 | Incompatible `protocol_version` in announce | Shown as not joinable, no crash |

**Save, persistence and recovery (SV)**

| ID | Case | Pass criteria |
|---|---|---|
| SV-01 | Editor sends `SAVE` | `SAVE_ACK` with seq and hash equal to the client's; checkpoint on disk verifies |
| SV-02 | Viewer sends `SAVE` | `SAVE_REJECT{NOT_ALLOWED}` |
| SV-03 | Crash injected at each durable-writer step (`FaultyFS`) | After restart, every file is either the old or the new version; never partial |
| SV-04 | Torn last line in `ops.jsonl` and in a chat log | Truncated, logged in `recovery.log`; hash equals last durable state |
| SV-05 | Newest snapshot corrupted (bad SHA-256) | Falls back to the previous one + replay; hash equals pre-crash |
| SV-06 | `acl.json` corrupted | `.bak` used; ACL intact |
| SV-07 | Client killed with un-acked ops and unsent chat | Recovery dialog on next launch; "Rejoin and send" restores them with no duplicates |
| SV-11 | Owner restores a checkpoint, then undoes | Board equals checkpoint hash on all clients; undo returns previous hash |

**Export (EX)**

| ID | Case | Pass criteria |
|---|---|---|
| EX-01 | PNG 1x/2x/4x, transparent and filled | Pixel size matches bounds x scale; alpha as chosen |
| EX-02 | JPEG | Opens; background filled |
| EX-03 | SVG | Valid XML; opens in a browser offline; text selectable |
| EX-04 | PDF fit-to-content and "each frame as page" | Opens; page count equals frame count; vector (not one big image) |
| EX-06 | Scope: selection / single frame | Only those objects in the output |
| EX-07 | Export with internet disabled, bundled fonts only | Text renders with Inter, no network access |
| EX-08 | Export a 1000-object board | Completes with progress shown; UI responsive (no freeze > 100 ms) |

**Recovery (RC)**

| ID | Case | Pass criteria |
|---|---|---|
| RC-01 | Kill TCP mid-stroke, reconnect | Resumes; no duplicate or lost op |
| RC-02 | Reconnect with `last_seq` inside the replay window | Delta only |
| RC-03 | Reconnect beyond the window | Full snapshot, hash matches |
| RC-04 | Un-acked local ops and chat after reconnect | Re-sent, deduped by `op_id` / `msg_id` |
| RC-05 | Server restart (snapshot + log tail) | Hash equals pre-restart |
| RC-06 | Heartbeat: silent 15 s / 60 s (fake clock) | Marked away / slot released; membership kept |
| RC-07 | One slow client with a full outbox | That client dropped; others unaffected |

**Chaos (CH)**

| ID | Case | Pass criteria |
|---|---|---|
| CH-01 | 300 ms delay + 20% duplicates, 5 clients, 60 s, with chat traffic | All hashes equal; chat order identical on all clients |
| CH-02 | Reorder application delivery | Client buffers by `seq`; converges |
| CH-03 | Random disconnects every 5-15 s | Converges; no crash |

**Performance (PF)**

| ID | Case | Target |
|---|---|---|
| PF-01 | Op broadcast latency, 5 clients on LAN | median < 50 ms, p95 < 150 ms |
| PF-02 | 300-point pen stroke after RDP | at least 60% fewer points; commit op < 4 KB |
| PF-03 | Load a 1000-object snapshot | < 1 s |
| PF-04 | Pan/zoom with 1000 objects | at least 30 fps |
| PF-05 | Server CPU with 5 idle clients (incl. discovery announces) | < 5% |
| PF-06 | Batched fsync under 5 drawing clients | PF-01 still met |

**Cross-platform (XP)**

| ID | Case | Pass criteria |
|---|---|---|
| XP-01 | L2 suite on Windows and on at least two Linux distros | Green |
| XP-02 | Server on OS A, clients on OS B and C (L3) | Full demo script passes |
| XP-03 | Built executables on each OS (not just `python -m`) | Launch, discover, join, draw, save, export |
| XP-04 | Firewall prompt/rule handled (Windows Defender, `ufw`/`firewalld`) for TCP 47801 and UDP 47800 | Documented fix works; discovery works both ways |
| XP-05 | HiDPI (100/150/200%) and Wayland/X11 | UI usable |
| XP-06 | Atomic replace and `fsync` on Windows (NTFS) and Linux (ext4) | SV-03 green on both |

### 14.4 Convergence check (used everywhere)

`board_hash = sha256(canonical_json(sorted objects with props, z, deleted flags))`. After any L2/L3/chaos test, every client computes it and all must equal the server's. The same function produces checkpoint and snapshot hashes.

### 14.5 When tests run

| Moment | What |
|---|---|
| Before every push | `python scripts/check.py` (ruff + L0 + L1 + L2 fast set) |
| Before merge to `main` | Full L0-L2 including `security`, `chat`, `persist`, and `chaos` |
| **Every phase gate** (`Agents.md` §11) | That phase's gate IDs **plus every earlier phase's gate IDs** (full regression); L3 run if the phase has GUI work |
| Phase 3 onward, each working session | L3 LAN test (14.6) with 3+ machines, mixed OS |
| Phase 7 gate (release) | First-build-of-record and XP-03 on every OS, full perf, acceptance (`ProductContext.md` §16), fresh-machine unzip test offline |
| Demo day | Hardware, router, firewall, and demo script dry run |

### 14.6 L3 LAN test script (about 20 minutes)

1. Disconnect all machines from the internet; join the same router/hotspot.
2. Open the client launcher on machines C and D **before** starting the server.
3. Machine A (any OS): start the server; note the printed IP and integrity-scan summary.
4. Machine B: create a room as owner. C and D should see it appear in their launcher **without refreshing** (DS-02).
5. C, D: click the board; request access; one is approved as editor, one as viewer; verify AC-02/AC-08 by hand.
6. B, C draw and move simultaneously for 2 minutes; room chat; C sends a DM to B and D confirms it never sees it (CT-02); undo/redo on both.
7. Unplug C's network for 20 seconds while drawing and chatting, then reconnect (RC-01, RC-04).
8. Enable chaos mode on D for 1 minute (CH-01).
9. Compare the board-hash readout (status bar, `Ctrl+Shift+H`) on all machines: **all must match**.
10. B presses Ctrl+S; check the `SAVE_ACK` hash in the inspector equals every client's hash (SV-01).
11. Kill the server process (no clean shutdown); restart; hashes, room chat and DMs must match (RC-05, CT-08).
12. Force-quit C's client; relaunch; use the Recover dialog (SV-07).
13. Export PNG, SVG, and PDF on one machine and open each file on another OS (EX-01..EX-04).
14. Record any failure with the bug template (14.8).

### 14.7 Exit criteria

Phase gates are defined in `Agents.md` §11. In terms of tiers:

- **Tier 1 done (end of Phase 4):** L0-L2 green; FR-*, OR-01..OR-05, AC-01..AC-12, SE-*, CT-01..CT-12, DS-01..DS-08, SV-01..SV-07, RC-01..RC-07, UN-01..UN-04, EX-01 pass; one clean L3 run on 3+ machines.
- **Tier 2 done (end of Phase 5):** above plus UN-05, OR-06, SV-11, EX-02..EX-04, EX-06..EX-08, CH-01..CH-03, PF-01..PF-06, XP-01..XP-03.
- **Release candidate (Phase 7):** all tests green on all demo OSes; XP-04..XP-06; fresh-machine unzip test passes offline.

### 14.8 Bug handling

| Severity | Meaning | Response |
|---|---|---|
| S1 | Data divergence, data loss, auth bypass, DM leak, crash | Stop-the-line; fix and add a regression test before any other work |
| S2 | Feature broken, no workaround | Fix before the current phase gate can pass |
| S3 | Wrong but with workaround | Schedule within the current or next phase |
| S4 | Cosmetic | Backlog |

Bug template: title, severity, OS/distro, build/commit, phase, steps, expected, actual, logs/pcap, seed. **Every fixed bug gets a regression test.**
