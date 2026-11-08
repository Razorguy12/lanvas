"""LANvas server. One asyncio loop owns all rooms, so every board operation is handled one at a time:
that single loop is the concurrency control. Each accepted op gets the next sequence number (`seq`),
which is the total order every client replays. Run:  python server.py [--port 47801] [--data data]
"""
import argparse
import asyncio
import glob
import hmac
import json
import os
import re
import secrets
import socket
import time

from common import PORT, apply_op, pack, proof, read_msg

COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080", "#9a6324", "#800000"]
KINDS = {"stroke", "line", "rect", "oval", "text", "note"}
NAME_RE = re.compile(r"^[\w .\-]{1,20}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
HEX_RE = re.compile(r"^[0-9a-f]{0,128}$")
HEARTBEAT, TIMEOUT = 5, 15            # ping every 5 s; drop a client silent for 15 s
MAX_OBJECTS, MAX_POINTS, MAX_CHAT, MAX_CHECKS, MAX_UNDO = 3000, 1500, 200, 10, 100
ROOMS, DATA = {}, "data"


class Room:
    """Everything about one whiteboard session. `objects` is the authoritative board state."""

    def __init__(self, code, host, salt="", key="", default_role="editor", seq=0,
                 chat=(), checks=(), users=None, objects=()):
        self.code, self.host, self.salt, self.key = code, host, salt, key
        self.default_role, self.seq = default_role, seq
        self.chat, self.checks, self.users = list(chat), list(checks), users or {}
        self.objects = {o["id"]: o for o in objects}
        self.seen = set(self.objects)             # every object id ever accepted (duplicate filter)
        self.conns, self.undo, self.redo = {}, {}, {}   # live connections; per-user undo/redo stacks
        self.dirty = True

    def dump(self):
        d = {k: getattr(self, k) for k in
             ("code", "host", "salt", "key", "default_role", "seq", "chat", "checks", "users")}
        d["objects"] = list(self.objects.values())
        return d

    def role(self, name):
        return "host" if name == self.host else self.users[name]["role"]


class Conn:
    def __init__(self, writer):
        self.w, self.user, self.room, self.last = writer, None, None, time.monotonic()


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
        return {"id": o["id"], "k": k, "c": o["c"], "w": int(o["w"]), "p": p,
                "s": str(o.get("s", ""))[:500], "u": user}
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
        conn.w.close()


def bcast(room, msg, skip=None):
    raw = pack(msg)
    for c in list(room.conns.values()):
        if c is not skip:
            write(c, raw)


def users_info(room):
    return [{"name": n, "role": room.role(n), "color": u["color"], "online": n in room.conns}
            for n, u in room.users.items()]


def snapshot(room):
    """Complete board + participant + chat state: sent on join, reconnect, resync and restore."""
    return {"seq": room.seq, "objects": list(room.objects.values()), "users": users_info(room),
            "chat": room.chat, "checks": [{"name": c["name"], "ts": c["ts"]} for c in room.checks]}


def say(room, user, text):
    """Append a chat line (user == "" means a system notice) and broadcast it."""
    m = {"u": user, "ts": int(time.time()), "text": text}
    room.chat.append(m)
    del room.chat[:-MAX_CHAT]
    room.dirty = True
    bcast(room, {"t": "chat", "m": m})


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


def undo_redo(room, me, redo):
    """Per-user undo: reverts only the caller's own last action, never other users' work.
    If someone else already removed/restored the target, that entry is skipped (no-op) and the
    next one is tried, so concurrent edits can never make undo corrupt the board."""
    src, dst = (room.redo, room.undo) if redo else (room.undo, room.redo)
    stack = src.setdefault(me, [])
    while stack:
        act, objs = stack.pop()
        do = act if redo else ("del" if act == "add" else "add")
        changed = False
        for o in objs:
            op = {"k": "add", "obj": o} if do == "add" else {"k": "del", "id": o["id"]}
            changed |= commit(room, op, me)
        if changed:
            dst.setdefault(me, []).append((act, objs))
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
        text = str(m.get("text", "")).strip()[:500]
        if text:
            say(room, me, text)
    elif t == "cur":
        bcast(room, {"t": "cur", "u": me, "x": m.get("x"), "y": m.get("y")}, skip=conn)
    elif t == "resync":
        send(conn, {"t": "snap", **snapshot(room)})
    elif t in ("op", "live", "undo", "redo") and not can_edit:
        deny("You have view-only access")
    elif t == "live":       # in-progress stroke preview: relayed, never stored or sequenced
        o = clean_obj(m.get("o") or {}, me)
        if o:
            bcast(room, {"t": "live", "u": me, "o": o, "app": bool(m.get("app"))}, skip=conn)
    elif t == "op":
        op = m.get("op") or {}
        if op.get("k") == "add":
            obj = clean_obj(op.get("obj") or {}, me)
            if not obj or len(room.objects) >= MAX_OBJECTS:
                return deny("Invalid object or board is full")
            if obj["id"] in room.seen:      # retransmitted duplicate: acknowledge, don't re-apply
                return send(conn, {"t": "ack", "id": obj["id"]})
            room.seen.add(obj["id"])
            commit(room, {"k": "add", "obj": obj}, me)
            push_undo(room, me, ("add", [obj]))
        elif op.get("k") == "del":
            old = room.objects.get(op.get("id"))
            if old and commit(room, {"k": "del", "id": old["id"]}, me):
                push_undo(room, me, ("del", [old]))
            else:
                send(conn, {"t": "ack", "id": "d:" + str(op.get("id"))})
    elif t in ("undo", "redo"):
        undo_redo(room, me, t == "redo")
    elif not host:
        deny("Only the host can do that")
    elif t == "clear":
        if room.objects:
            push_undo(room, me, ("del", list(room.objects.values())))
            commit(room, {"k": "clear"}, me)
    elif t == "role" and m.get("user") in room.users and m["user"] != room.host \
            and m.get("role") in ("editor", "viewer"):
        room.users[m["user"]]["role"] = m["role"]
        room.dirty = True
        bcast(room, {"t": "users", "users": users_info(room)})
    elif t == "kick" and m.get("user") in room.conns and m["user"] != room.host:
        target = room.conns[m["user"]]
        send(target, {"t": "error", "code": "KICKED", "text": "The host removed you", "fatal": True})
        target.w.close()
    elif t == "save":
        name = str(m.get("name") or time.strftime("%H:%M:%S"))[:40]
        room.checks.append({"name": name, "ts": int(time.time()), "objects": list(room.objects.values())})
        del room.checks[:-MAX_CHECKS]
        room.dirty = True
        bcast(room, {"t": "checks", "checks": snapshot(room)["checks"]})
    elif t == "restore" and isinstance(m.get("idx"), int) and 0 <= m["idx"] < len(room.checks):
        room.objects = {o["id"]: o for o in room.checks[m["idx"]]["objects"]}
        room.seen |= set(room.objects)
        room.seq += 1
        room.undo, room.redo, room.dirty = {}, {}, True
        bcast(room, {"t": "snap", **snapshot(room)})


# ---------- connections ----------

def new_code():
    while True:
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        if code not in ROOMS:
            return code


async def login(conn, reader):
    """Handshake: hello -> (challenge -> auth) -> welcome. Returns the Room, or None after an error."""
    async def fail(code, text):
        await asyncio.sleep(1)      # slows password guessing
        send(conn, {"t": "error", "code": code, "text": text})

    h = await asyncio.wait_for(read_msg(reader, 8192), 10)
    user = str(h.get("user", "")).strip()
    if h.get("t") != "hello" or not NAME_RE.match(user):
        return await fail("BAD_NAME", "Name must be 1-20 letters, digits, space . _ -")
    if h.get("create"):
        salt, key = str(h.get("salt", "")), str(h.get("key", ""))
        if not (HEX_RE.match(salt) and HEX_RE.match(key)) or bool(salt) != bool(key):
            return await fail("BAD_AUTH", "Malformed password data")
        code = new_code()
        room = ROOMS[code] = Room(code, user, salt, key,
                                  "viewer" if h.get("role") == "viewer" else "editor")
    else:
        room = ROOMS.get(str(h.get("room", "")).upper())
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
        old = room.conns.get(user)
        if old:                    # stale half-dead connection from the same user: replace it
            old.w.close()
    else:
        u = room.users[user] = {"role": room.default_role, "token": secrets.token_hex(16),
                                "color": COLORS[len(room.users) % len(COLORS)]}
    conn.user, conn.room = user, room
    room.conns[user] = conn
    room.dirty = True
    send(conn, {"t": "welcome", "room": room.code, "host": room.host,
                "you": {"name": user, "token": u["token"], "color": u["color"]}, **snapshot(room)})
    bcast(room, {"t": "users", "users": users_info(room)})
    say(room, "", f"{user} joined")
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
            AttributeError, TypeError):
        pass        # disconnect or malformed input: just drop this client
    finally:
        room = conn.room
        if room and room.conns.get(conn.user) is conn:
            del room.conns[conn.user]
            bcast(room, {"t": "gone", "u": conn.user})
            bcast(room, {"t": "users", "users": users_info(room)})
            say(room, "", f"{conn.user} left")      # room stays alive for reconnects
        writer.close()


# ---------- persistence and housekeeping ----------

def save_room(room):
    path = os.path.join(DATA, room.code + ".json")
    with open(path + ".tmp", "w") as f:
        json.dump(room.dump(), f)
    os.replace(path + ".tmp", path)      # atomic: a crash never leaves a half-written file
    room.dirty = False


async def housekeeping():
    ping = pack({"t": "ping"})
    while True:
        await asyncio.sleep(HEARTBEAT)
        now = time.monotonic()
        for room in list(ROOMS.values()):
            for c in list(room.conns.values()):
                if now - c.last > TIMEOUT:
                    c.w.close()                  # silent client: treat as disconnected
                else:
                    write(c, ping)
            if room.dirty:
                save_room(room)                  # autosave


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))        # sends nothing; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


async def main(host, port):
    for f in glob.glob(os.path.join(DATA, "*.json")):
        with open(f) as fh:
            room = Room(**json.load(fh))
        ROOMS[room.code] = room
    server = await asyncio.start_server(serve, host, port)
    asyncio.create_task(housekeeping())
    print(f"LANvas server listening on {lan_ip()}:{port}  ({len(ROOMS)} saved room(s) loaded)", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--data", default="data")
    a = ap.parse_args()
    DATA = a.data
    os.makedirs(DATA, exist_ok=True)
    try:
        asyncio.run(main(a.host, a.port))
    except KeyboardInterrupt:
        pass
    finally:
        for r in ROOMS.values():
            save_room(r)
