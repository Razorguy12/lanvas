"""LANvas server. One asyncio loop owns all rooms, so every board operation is handled one at a time:
that single loop is the concurrency control. Each accepted op gets the next sequence number (`seq`),
which is the total order every client replays.
Run:  python server.py [--port 47801] [--data data] [--autosave 120] [--grace 30] [--log-level INFO]
"""
import argparse
import asyncio
import glob
import hmac
import json
import logging
import logging.handlers
import os
import re
import secrets
import signal
import socket
import time

from common import PORT, apply_op, pack, proof, read_msg

COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080", "#9a6324", "#800000"]
KINDS = {"stroke", "line", "rect", "oval", "text", "note", "image"}
B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
MAX_IMG, MAX_IMAGES = 700_000, 40     # base64 chars per image; images per room (keeps snapshots sane)
BOOT = secrets.token_hex(4)           # changes on every server start, so clients can tell it restarted
NAME_RE = re.compile(r"^[\w .\-]{1,20}$")
CODE_RE = re.compile(r"^[A-Z0-9]{4,8}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
HEX_RE = re.compile(r"^[0-9a-f]{0,128}$")
HEARTBEAT, TIMEOUT = 5, 15            # ping every 5 s; drop a client silent for 15 s
MAX_OBJECTS, MAX_POINTS, MAX_CHAT, MAX_CHECKS, MAX_UNDO, MAX_PIECES = 3000, 1500, 200, 10, 100, 300
ROOMS, DATA = {}, "data"
AUTOSAVE = 120        # seconds between autosaves of a changed room (the "save point")
GRACE = 30            # an empty room is closed after this long (clients reconnect well within it)
STARTUP_GRACE = 90    # after a server restart, give clients longer to come back before closing
log = logging.getLogger("lanvas")


class Room:
    """Everything about one whiteboard session. `objects` is the authoritative board state.
    A room is *open* while people use it. When it has been empty for GRACE seconds it is *closed*:
    saved to disk and kept dormant. Reopening its code later asks the first person whether to
    restore that last save or start fresh."""

    def __init__(self, code, host, salt="", key="", default_role="editor", seq=0, chat=(), checks=(),
                 users=None, objects=(), private=False, closed=False, saved_at=0, **_ignored):
        self.code, self.host, self.salt, self.key = code, host, salt, key
        self.default_role, self.seq, self.private, self.closed = default_role, seq, private, closed
        self.chat, self.checks, self.users = list(chat), list(checks), users or {}
        self.objects = {o["id"]: o for o in objects}
        self.seen = set(self.objects)             # every object/op id ever accepted (duplicate filter)
        self.conns, self.undo, self.redo = {}, {}, {}   # live connections; per-user undo/redo stacks
        self.saved_at, self.last_save = saved_at, time.monotonic()
        self.empty_since, self.dirty, self.lock = time.monotonic(), True, asyncio.Lock()

    def dump(self):
        d = {k: getattr(self, k) for k in ("code", "host", "salt", "key", "default_role", "seq", "chat",
                                           "checks", "users", "private", "closed", "saved_at")}
        d["objects"] = list(self.objects.values())
        return d

    def role(self, name):
        return "host" if name == self.host else self.users[name]["role"]

    def reset(self):
        """'Start fresh' on reopen: empty board and chat; keeps members, roles, password and versions."""
        self.objects, self.chat, self.undo, self.redo = {}, [], {}, {}
        self.seq += 1
        self.dirty = True


class Conn:
    def __init__(self, writer):
        self.w, self.user, self.room, self.last = writer, None, None, time.monotonic()
        peer = writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if peer else "?"


def clean_obj(o, user):
    """Validate an untrusted drawing object from a client; return a sanitised copy or None."""
    try:
        k, p = o["k"], o["p"]
        lo = 4 if k in ("line", "rect", "oval") else 2
        hi = MAX_POINTS * 2 if k == "stroke" else lo
        if not (k in KINDS and isinstance(o["id"], str) and 0 < len(o["id"]) <= 40
                and COLOR_RE.match(o["c"]) and 1 <= int(o["w"]) <= 40
                and isinstance(p, list) and lo <= len(p) <= hi and len(p) % 2 == 0
                and all(isinstance(v, (int, float)) and abs(v) < 1e5 for v in p)):
            return None
        f, text = o.get("f", ""), str(o.get("s", ""))
        if k == "image":
            if not (0 < len(text) <= MAX_IMG and B64_RE.match(text)):
                return None
        else:
            text = text[:500]
        return {"id": o["id"], "k": k, "c": o["c"], "w": int(o["w"]), "p": p, "s": text, "f": f if isinstance(f, str) and COLOR_RE.match(f) else "",
                "u": user}
    except (KeyError, TypeError, ValueError):
        return None


# ---------- sending ----------

def send(conn, msg):
    write(conn, pack(msg))


def write(conn, raw):
    if conn.w.is_closing():
        return
    conn.w.write(raw)
    if conn.w.transport.get_write_buffer_size() > 4_000_000:   # client not reading: cut it loose
        log.warning("%s (%s) not reading; disconnecting", conn.user, conn.peer)
        conn.w.close()


def bcast(room, msg, skip=None):
    raw = pack(msg)
    for c in list(room.conns.values()):
        if c is not skip:
            write(c, raw)


def users_info(room):
    return [{"name": n, "role": room.role(n), "color": u["color"], "online": n in room.conns}
            for n, u in room.users.items()]


def visible(c, user):
    """Private messages are visible only to their sender and recipient."""
    return not c.get("to") or user in (c["u"], c["to"])


def snapshot(room, user):
    """Complete board + participant + chat state: sent on join, reconnect, resync and restore."""
    return {"seq": room.seq, "objects": list(room.objects.values()), "users": users_info(room),
            "chat": [c for c in room.chat if visible(c, user)],
            "checks": [{"name": c["name"], "ts": c["ts"]} for c in room.checks]}


def bcast_snap(room):
    for c in list(room.conns.values()):
        send(c, {"t": "snap", **snapshot(room, c.user)})


def say(room, user, text, to=None):
    """Append a chat line (user == "" means a system notice) and deliver it. With `to`, it is a
    private message: stored (so it survives reconnects) but sent only to the two people involved."""
    m = {"u": user, "ts": int(time.time()), "text": text}
    if to:
        m["to"] = to
    room.chat.append(m)
    del room.chat[:-MAX_CHAT]
    room.dirty = True
    if not to:
        return bcast(room, {"t": "chat", "m": m})
    for name in {user, to}:
        if name in room.conns:
            send(room.conns[name], {"t": "chat", "m": m})


def room_list():
    """Rooms anyone may see in the lobby: open and not private."""
    return [{"code": r.code, "host": r.host, "online": len(r.conns), "locked": bool(r.key)}
            for r in ROOMS.values() if not r.private and not r.closed]


# ---------- board operations ----------

def commit(room, op, by):
    """Apply a validated op, stamp it with the next seq, and broadcast it to everyone (sender too)."""
    if not apply_op(room.objects, op):
        return False
    room.seq += 1
    room.dirty = True
    bcast(room, {"t": "op", "seq": room.seq, "op": op, "by": by})
    return True


def push_undo(room, me, entry):
    stack = room.undo.setdefault(me, [])
    stack.append(entry)
    del stack[:-MAX_UNDO]
    room.redo[me] = []          # a new action invalidates that user's redo history


def replay_ops(act, data, redo):
    """The ops that undo (or redo) one history entry."""
    if act in ("add", "del"):
        do = act if redo else ("del" if act == "add" else "add")
        return [{"k": "add", "obj": o} if do == "add" else {"k": "del", "id": o["id"]} for o in data]
    if act == "fill":
        return [{"k": "fill", "id": i, "f": new if redo else old} for i, old, new in data]
    olds, news = data                                    # "repl" (area eraser)
    gone, back = (olds, news) if redo else (news, olds)
    return [{"k": "repl", "del": [o["id"] for o in gone], "add": back}]


def undo_redo(room, me, redo):
    """Per-user undo: reverts only the caller's own last action, never other users' work.
    If someone else already removed/restored the target, that entry is skipped (no-op) and the
    next one is tried, so concurrent edits can never make undo corrupt the board."""
    src, dst = (room.redo, room.undo) if redo else (room.undo, room.redo)
    stack = src.setdefault(me, [])
    while stack:
        act, data = stack.pop()
        changed = False
        for op in replay_ops(act, data, redo):
            changed |= commit(room, op, me)
        if changed:
            dst.setdefault(me, []).append((act, data))
            return


def handle(room, conn, m):
    me, t = conn.user, m.get("t")
    host = me == room.host
    can_edit = room.role(me) in ("host", "editor")

    def deny(text):
        send(conn, {"t": "error", "code": "NOT_ALLOWED", "text": text})

    if t == "pong":
        return
    if t == "chat":
        text, to = str(m.get("text", "")).strip()[:500], m.get("to")
        if to and (to not in room.users or to == me):
            return deny("No such participant")
        if text:
            say(room, me, text, to)
    elif t == "cur":
        bcast(room, {"t": "cur", "u": me, "x": m.get("x"), "y": m.get("y")}, skip=conn)
    elif t == "resync":
        send(conn, {"t": "snap", **snapshot(room, me)})
    elif t in ("op", "live", "undo", "redo") and not can_edit:
        deny("You have view-only access")
    elif t == "live":       # in-progress stroke preview: relayed, never stored or sequenced
        o = clean_obj(m.get("o") or {}, me)
        if o:
            bcast(room, {"t": "live", "u": me, "o": o, "app": bool(m.get("app"))}, skip=conn)
    elif t == "op":
        op = m.get("op") or {}
        k = op.get("k")
        if k == "add":
            obj = clean_obj(op.get("obj") or {}, me)
            if not obj or len(room.objects) >= MAX_OBJECTS:
                return deny("Invalid object or board is full")
            if obj["k"] == "image" and sum(o["k"] == "image" for o in room.objects.values()) >= MAX_IMAGES:
                return deny(f"At most {MAX_IMAGES} images per board")
            if obj["id"] in room.seen:      # retransmitted duplicate: acknowledge, don't re-apply
                return send(conn, {"t": "ack", "id": obj["id"]})
            room.seen.add(obj["id"])
            commit(room, {"k": "add", "obj": obj}, me)
            push_undo(room, me, ("add", [obj]))
        elif k == "del":
            old = room.objects.get(op.get("id"))
            if old and commit(room, {"k": "del", "id": old["id"]}, me):
                push_undo(room, me, ("del", [old]))
            else:
                send(conn, {"t": "ack", "id": "d:" + str(op.get("id"))})
        elif k == "fill":
            old, f = room.objects.get(op.get("id")), op.get("f", "")
            if not (isinstance(f, str) and (f == "" or COLOR_RE.match(f))):
                return deny("Invalid fill colour")
            prev = old.get("f", "") if old else ""
            if old and commit(room, {"k": "fill", "id": old["id"], "f": f}, me):
                push_undo(room, me, ("fill", [(old["id"], prev, f)]))
            else:
                send(conn, {"t": "ack", "id": "f:" + str(op.get("id"))})
        elif k == "repl":           # area eraser: some objects replaced by their remaining pieces
            rid, dels, adds = str(op.get("rid", ""))[:40], op.get("del"), op.get("add")
            if not (rid and isinstance(dels, list) and isinstance(adds, list)
                    and len(dels) <= MAX_PIECES and len(adds) <= MAX_PIECES):
                return deny("Invalid erase")
            if "r:" + rid in room.seen:
                return send(conn, {"t": "ack", "id": "r:" + rid})
            adds = [clean_obj(o, me) if isinstance(o, dict) else None for o in adds]
            if None in adds or any(o["k"] == "image" for o in adds) or any(o["id"] in room.seen for o in adds) \
                    or len(room.objects) + len(adds) > MAX_OBJECTS + len(dels):
                return deny("Invalid erase")
            olds = [room.objects[i] for i in dels if isinstance(i, str) and i in room.objects]
            room.seen |= {"r:" + rid} | {o["id"] for o in adds}
            op = {"k": "repl", "rid": rid, "del": [o["id"] for o in olds], "add": adds}
            if commit(room, op, me):
                push_undo(room, me, ("repl", (olds, adds)))
            else:
                send(conn, {"t": "ack", "id": "r:" + rid})
    elif t in ("undo", "redo"):
        undo_redo(room, me, t == "redo")
    elif not host:
        deny("Only the host can do that")
    elif t == "clear":
        if room.objects:
            push_undo(room, me, ("del", list(room.objects.values())))
            commit(room, {"k": "clear"}, me)
            log.info("[%s] %s cleared the board", room.code, me)
    elif t == "role" and m.get("user") in room.users and m["user"] != room.host \
            and m.get("role") in ("editor", "viewer"):
        room.users[m["user"]]["role"] = m["role"]
        room.dirty = True
        bcast(room, {"t": "users", "users": users_info(room)})
        log.info("[%s] %s made %s %s", room.code, me, m["user"], m["role"])
    elif t == "kick" and m.get("user") in room.conns and m["user"] != room.host:
        target = room.conns[m["user"]]
        send(target, {"t": "error", "code": "KICKED", "text": "The host removed you", "fatal": True})
        target.w.close()
        log.info("[%s] %s kicked %s", room.code, me, m["user"])
    elif t == "save":
        name = str(m.get("name") or time.strftime("%H:%M:%S"))[:40]
        room.checks.append({"name": name, "ts": int(time.time()), "objects": list(room.objects.values())})
        del room.checks[:-MAX_CHECKS]
        room.dirty = True
        bcast(room, {"t": "checks", "checks": snapshot(room, me)["checks"]})
        log.info("[%s] %s saved version %r", room.code, me, name)
    elif t == "restore" and isinstance(m.get("idx"), int) and 0 <= m["idx"] < len(room.checks):
        room.objects = {o["id"]: o for o in room.checks[m["idx"]]["objects"]}
        room.seen |= set(room.objects)
        room.seq += 1
        room.undo, room.redo, room.dirty = {}, {}, True
        bcast_snap(room)
        log.info("[%s] %s restored version %r", room.code, me, room.checks[m["idx"]]["name"])


# ---------- connections ----------

def new_code():
    while True:
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        if code not in ROOMS:
            return code


async def reopen(conn, reader, room):
    """First person into a closed room decides: restore its last save, or start a fresh board.
    The lock makes anyone arriving meanwhile wait for that decision instead of being asked too."""
    async with room.lock:
        if not room.closed:
            return
        send(conn, {"t": "recover", "room": room.code, "ts": room.saved_at, "objects": len(room.objects)})
        try:
            a = await asyncio.wait_for(read_msg(reader, 8192), 120)
            use = not (a.get("t") == "recover" and a.get("use") is False)
        except asyncio.TimeoutError:
            use = True                      # no answer: keep the user's work
        if not use:
            room.reset()
        room.closed, room.empty_since = False, None
        save_room(room)
        log.info("[%s] reopened by %s (%s)", room.code, conn.user or "?",
                 "restored last save" if use else "started fresh")


async def login(conn, reader):
    """Handshake: hello -> (challenge -> auth) -> (recover) -> welcome. Returns the Room, or None.
    A first message of {"t": "list"} instead returns the lobby's public room list and ends."""
    async def fail(code, text):
        log.info("login failed from %s: %s", conn.peer, code)
        await asyncio.sleep(1)      # slows password guessing
        send(conn, {"t": "error", "code": code, "text": text})

    h = await asyncio.wait_for(read_msg(reader, 8192), 10)
    if h.get("t") == "list":
        return send(conn, {"t": "rooms", "rooms": room_list()})
    user = str(h.get("user", "")).strip()
    if h.get("t") != "hello" or not NAME_RE.match(user):
        return await fail("BAD_NAME", "Name must be 1-20 letters, digits, space . _ -")
    code = str(h.get("room", "")).strip().upper()
    if h.get("create") and not (code in ROOMS and ROOMS[code].closed):
        salt, key = str(h.get("salt", "")), str(h.get("key", ""))
        if not (HEX_RE.match(salt) and HEX_RE.match(key)) or bool(salt) != bool(key):
            return await fail("BAD_AUTH", "Malformed password data")
        if code and code in ROOMS:
            return await fail("ROOM_EXISTS", "A room with that code is already open; untick Create to join it")
        if code and not CODE_RE.match(code):
            return await fail("BAD_CODE", "Room code must be 4-8 letters/digits")
        code = code or new_code()
        room = ROOMS[code] = Room(code, user, salt, key, "viewer" if h.get("role") == "viewer" else "editor",
                                  private=bool(h.get("private")))
        room.empty_since = None
        save_room(room)
        log.info("[%s] created by %s from %s (%s%s)", code, user, conn.peer,
                 "private" if room.private else "public", ", password" if key else "")
    else:
        room = ROOMS.get(code)
        if not room:
            return await fail("NO_ROOM", "No room with that code")
        if room.key:
            nonce = secrets.token_hex(16)
            send(conn, {"t": "challenge", "salt": room.salt, "nonce": nonce})
            a = await asyncio.wait_for(read_msg(reader, 8192), 10)
            if a.get("t") != "auth" or not hmac.compare_digest(str(a.get("proof")), proof(room.key, nonce)):
                return await fail("AUTH", "Wrong password")
    token, u = str(h.get("token", "")), room.users.get(user)
    if u:
        same = bool(token) and hmac.compare_digest(token, u["token"])    # reconnect with session token
        if (user in room.conns or (user == room.host and not room.key)) and not same:
            return await fail("NAME_TAKEN", "That name is in use in this room")
    conn.user = user
    if room.closed:
        await reopen(conn, reader, room)
    if u:
        old = room.conns.get(user)
        if old:                    # stale half-dead connection from the same user: replace it
            old.w.close()
    else:
        u = room.users[user] = {"role": room.default_role, "token": secrets.token_hex(16),
                                "color": COLORS[len(room.users) % len(COLORS)]}
    conn.room = room
    room.conns[user] = conn
    room.dirty, room.empty_since = True, None
    send(conn, {"t": "welcome", "room": room.code, "host": room.host, "boot": BOOT, "saved_at": room.saved_at,
                "you": {"name": user, "token": u["token"], "color": u["color"]}, **snapshot(room, user)})
    bcast(room, {"t": "users", "users": users_info(room)})
    say(room, "", f"{user} joined")
    log.info("[%s] %s joined from %s (%d online)", room.code, user, conn.peer, len(room.conns))
    return room


async def serve(reader, writer):
    conn = Conn(writer)
    try:
        room = await login(conn, reader)
        while room:
            m = await read_msg(reader)
            conn.last = time.monotonic()
            handle(room, conn, m)
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, asyncio.TimeoutError,
            AttributeError, TypeError) as e:
        if conn.room is None:
            log.debug("connection from %s dropped before login: %r", conn.peer, e)
    except Exception:
        log.exception("unexpected error serving %s (%s)", conn.user, conn.peer)
    finally:
        room = conn.room
        if room and room.conns.get(conn.user) is conn:
            del room.conns[conn.user]
            bcast(room, {"t": "gone", "u": conn.user})
            bcast(room, {"t": "users", "users": users_info(room)})
            say(room, "", f"{conn.user} left")      # room stays alive for reconnects
            log.info("[%s] %s left (%d online)", room.code, conn.user, len(room.conns))
            if not room.conns:
                room.empty_since = time.monotonic()
        writer.close()


# ---------- persistence and housekeeping ----------

def save_room(room):
    """Atomic, durable write: a crash or power cut leaves either the old file or the new one."""
    path = os.path.join(DATA, room.code + ".json")
    room.saved_at = int(time.time())
    with open(path + ".tmp", "w") as f:
        json.dump(room.dump(), f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(path + ".tmp", path)
    room.dirty, room.last_save = False, time.monotonic()


async def housekeeping():
    ping = pack({"t": "ping"})
    while True:
        await asyncio.sleep(min(HEARTBEAT, AUTOSAVE, GRACE))
        now = time.monotonic()
        for room in list(ROOMS.values()):
            try:
                for c in list(room.conns.values()):
                    if now - c.last > TIMEOUT:
                        log.info("[%s] %s timed out", room.code, c.user)
                        c.w.close()                  # silent client: treat as disconnected
                    else:
                        write(c, ping)
                if room.closed:
                    continue
                if not room.conns and room.empty_since and now - room.empty_since > GRACE:
                    room.closed = True
                    save_room(room)
                    log.info("[%s] closed (empty); saved %d objects for recovery", room.code, len(room.objects))
                elif room.dirty and now - room.last_save >= AUTOSAVE:
                    save_room(room)                  # periodic autosave = the room's save point
                    log.info("[%s] autosaved (%d objects)", room.code, len(room.objects))
            except Exception:
                log.exception("[%s] housekeeping failed", room.code)


def load_rooms():
    """Server recovery: bring back every saved room. Rooms that were open when the server stopped
    (even if it crashed) are restored open at their last save point, so clients just reconnect."""
    for f in sorted(glob.glob(os.path.join(DATA, "*.json"))):
        try:
            with open(f) as fh:
                room = Room(**json.load(fh))
        except (OSError, ValueError, TypeError, KeyError) as e:
            log.error("could not load %s: %r (file left untouched)", f, e)
            continue
        ROOMS[room.code] = room
        room.dirty = False
        if not room.closed:
            room.empty_since = time.monotonic() + STARTUP_GRACE - GRACE
        log.info("[%s] recovered %s: %d objects, saved %s", room.code, "closed" if room.closed else "OPEN",
                 len(room.objects), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(room.saved_at)))


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))        # sends nothing; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


async def main(host, port):
    load_rooms()
    server = await asyncio.start_server(serve, host, port)
    task, stop = asyncio.create_task(housekeeping()), asyncio.Event()
    try:            # SIGTERM (e.g. systemd stop, `kill`) shuts down cleanly like Ctrl+C
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    except (NotImplementedError, AttributeError):
        pass
    n_open = sum(not r.closed for r in ROOMS.values())
    print(f"LANvas server listening on {lan_ip()}:{port}  ({len(ROOMS)} saved room(s), {n_open} restored open)",
          flush=True)
    log.info("listening on %s:%d; autosave every %ss; data in %s", host, port, AUTOSAVE, os.path.abspath(DATA))
    await stop.wait()
    log.info("shutdown requested; saving rooms and notifying clients")
    task.cancel()
    server.close()
    for room in ROOMS.values():
        save_room(room)
        bcast(room, {"t": "bye", "text": "the server was shut down"})     # clients learn at once, not after 15 s
    await asyncio.sleep(0.3)                    # let those frames flush


def setup_logging(level):
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(os.path.join(DATA, "server.log"), maxBytes=1_000_000,
                                              backupCount=5, encoding="utf-8")
    sh = logging.StreamHandler()        # stderr
    for h in (fh, sh):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(level)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--data", default="data")
    ap.add_argument("--autosave", type=float, default=AUTOSAVE, help="seconds between autosaves (default 120)")
    ap.add_argument("--grace", type=float, default=GRACE, help="seconds an empty room stays open (default 30)")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    a = ap.parse_args()
    DATA, AUTOSAVE, GRACE = a.data, a.autosave, a.grace
    STARTUP_GRACE = max(GRACE * 3, 90 if GRACE >= 30 else 0)
    os.makedirs(DATA, exist_ok=True)
    setup_logging(a.log_level)
    try:
        asyncio.run(main(a.host, a.port))
    except KeyboardInterrupt:
        pass
    finally:
        for r in ROOMS.values():
            try:
                save_room(r)
            except OSError:
                log.exception("[%s] final save failed", r.code)
        log.info("server stopped; %d room(s) saved", len(ROOMS))
