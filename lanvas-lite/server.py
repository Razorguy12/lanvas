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

from common import ATTRS, PORT, apply_op, pack, proof, read_msg

COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080", "#9a6324", "#800000"]
KINDS = {"stroke", "line", "rect", "oval", "text", "note", "image", "poly", "button", "group"}
POINTS = {"line": (4, 4), "rect": (4, 4), "oval": (4, 4), "button": (4, 4), "group": (4, 4), "text": (2, 2),
          "note": (2, 2), "image": (2, 4), "poly": (6, 400)}       # (min, max) numbers in p; stroke: see below
PAGE_RE = re.compile(r"^[\w-]{1,20}$")
MAX_ITEMS, MAX_PAGES = 300, 30
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
ROLES = ("viewer", "editor", "admin")          # plus "host" (the room's owner)
APPROVERS = ("host", "admin")
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
                 users=None, objects=(), private=False, closed=False, saved_at=0, approval=False, pages=None,
                 bans=None, **_ignored):
        self.code, self.host, self.salt, self.key = code, host, salt, key
        self.default_role, self.seq, self.private, self.closed = default_role, seq, private, closed
        self.approval, self.waiting = approval, {}   # waiting: name -> (future, wanted role) at the door
        self.pages = pages or [{"id": "main", "name": "Page 1"}]       # whiteboards inside this room
        self.bans = bans or {"users": [], "ips": []}
        self.presenter, self.view = None, None
        self.chat, self.checks, self.users = list(chat), list(checks), users or {}
        self.objects = {o["id"]: o for o in objects}
        self.seen = set(self.objects)             # every object/op id ever accepted (duplicate filter)
        self.conns, self.undo, self.redo = {}, {}, {}   # live connections; per-user undo/redo stacks
        self.saved_at, self.last_save = saved_at, time.monotonic()
        self.empty_since, self.dirty, self.lock = time.monotonic(), True, asyncio.Lock()

    def dump(self):
        d = {k: getattr(self, k) for k in ("code", "host", "salt", "key", "default_role", "seq", "chat",
                                           "checks", "users", "private", "closed", "saved_at", "approval", "pages", "bans")}
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
        self.ip = peer[0] if peer else "?"
        self.peer = f"{self.ip}:{peer[1]}" if peer else "?"


def clean_obj(o, user, room=None, nested=False):
    """Validate an untrusted drawing object from a client; return a sanitised copy or None.
    Merged layers are kind "group" with their parts in `items` (groups are flattened, never nested)."""
    try:
        k, p = o["k"], o["p"]
        lo, hi = POINTS.get(k, (2, MAX_POINTS * 2))
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
        out = {"id": o["id"], "k": k, "c": o["c"], "w": int(o["w"]), "p": p, "s": text,
               "f": f if isinstance(f, str) and COLOR_RE.match(f) else "", "u": user}
        for flag in ("h", "lk", "cl"):
            if o.get(flag) is True:
                out[flag] = True
        if "a" in o:
            if not (isinstance(o["a"], int) and 10 <= o["a"] <= 100):
                return None
            out["a"] = o["a"]
        pages = {pg["id"] for pg in room.pages} if room else {"main"}
        if not nested:
            out["pg"] = o.get("pg", "main")
            if out["pg"] not in pages:
                return None
        if o.get("ln"):
            if o["ln"] not in pages:
                return None
            out["ln"] = o["ln"]
        if k == "group":
            if nested or not isinstance(o.get("items"), list) or not 1 <= len(o["items"]) <= MAX_ITEMS:
                return None
            items = []
            for it in o["items"]:
                parts = it.get("items", []) if isinstance(it, dict) and it.get("k") == "group" else [it]
                for part in parts:
                    c = clean_obj(part, part.get("u", user) if isinstance(part, dict) else user, room, True)
                    if not c:
                        return None
                    items.append(c)
            if len(items) > MAX_ITEMS or sum(len(c["p"]) for c in items) > 40000:
                return None
            out["items"] = items
        return out
    except (KeyError, TypeError, ValueError, AttributeError):
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
            "present": {"by": room.presenter, "view": room.view} if room.presenter else None, "pages": room.pages,
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
    return [{"code": r.code, "host": r.host, "online": len(r.conns), "locked": bool(r.key), "approval": r.approval}
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
    if act == "set":
        return [{"k": "set", "id": i, "attrs": new if redo else old} for i, old, new in data]
    olds, news = data                                    # "repl" (area eraser, clip)
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


def clean_attrs(room, old, a):
    """Validate a design/move change by validating the object it would produce. None if invalid."""
    if not isinstance(a, dict) or not a or set(a) - set(ATTRS) or (old["k"] == "image" and "s" in a):
        return None
    for flag in ("h", "lk", "cl"):
        if flag in a and not isinstance(a[flag], bool):
            return None
    if "items" in a and old["k"] != "group":
        return None
    new = clean_obj({**old, **a}, old.get("u", ""), room)
    if not new or any(new.get(k, ATTRS[k]) != v for k, v in a.items() if k != "items"):
        return None                     # anything cleaned away means the request was invalid
    return {k: new.get(k, ATTRS[k]) for k in a}


def locked(o, attrs=None):
    """A locked layer can't be changed, except for unlocking it (or hiding / showing it)."""
    return o.get("lk") and not (attrs is not None and set(attrs) <= {"lk", "h"})


def admins_info(room):
    """Bans and each member's last address: only for the host and admins."""
    return {"t": "bans", "bans": room.bans, "ips": {n: u.get("ip", "?") for n, u in room.users.items()}}


def kick(room, name, text):
    c = room.conns.get(name)
    if c:
        send(c, {"t": "error", "code": "KICKED", "text": text, "fatal": True})
        c.w.close()


def may_change_role(room, actor, target, new):
    """Host: anyone but themself. Admin: may promote/demote editors and viewers (up to admin),
    but may not touch other admins or the host."""
    if target == room.host or target not in room.users or new not in ROLES:
        return False
    if actor == room.host:
        return True
    return room.role(actor) == "admin" and room.role(target) != "admin"


def set_role(room, user, role, by):
    room.users[user]["role"] = role
    if role == "admin" and user in room.conns:
        send(room.conns[user], admins_info(room))
    room.dirty = True
    bcast(room, {"t": "users", "users": users_info(room)})
    log.info("[%s] %s made %s %s", room.code, by, user, role)


def approvers(room):
    return [c for n, c in room.conns.items() if room.role(n) in APPROVERS]


def handle(room, conn, m):
    me, t = conn.user, m.get("t")
    role = room.role(me)
    host, admin, can_edit = me == room.host, role in APPROVERS, role in ("host", "admin", "editor")

    def deny(text):
        send(conn, {"t": "error", "code": "NOT_ALLOWED", "text": text})

    def mine(o):
        """Editors may only erase/change their own work; admins and the host may change anyone's."""
        return admin or o.get("u") == me

    if t == "pong":
        return
    if t == "chat":
        text, to = str(m.get("text", "")).strip()[:500], m.get("to")
        if to and (to not in room.users or to == me):
            return deny("No such participant")
        if text:
            say(room, me, text, to)
    elif t == "cur":
        bcast(room, {"t": "cur", "u": me, "x": m.get("x"), "y": m.get("y"), "pg": str(m.get("pg", "main"))[:20]},
              skip=conn)
    elif t == "resync":
        send(conn, {"t": "snap", **snapshot(room, me)})
    elif t == "ask":            # in-room request for a different role
        want = m.get("role")
        if want not in ROLES or host:
            return deny("Invalid request")
        for c in approvers(room):
            send(c, {"t": "req", "user": me, "role": want, "inroom": True})
        if not approvers(room):
            deny("No host or admin is online to approve")
        log.info("[%s] %s asked for %s", room.code, me, want)
    elif t == "view":           # presenter's viewport, relayed to followers
        if room.presenter == me and isinstance(m.get("v"), dict):
            room.view = {k: m["v"].get(k) for k in ("z", "cx", "cy", "pg")}
            bcast(room, {"t": "view", "v": room.view}, skip=conn)
    elif t in ("op", "live", "undo", "redo", "save") and not can_edit:
        deny("You have view-only access")
    elif t == "live":       # in-progress stroke preview: relayed, never stored or sequenced
        o = clean_obj(m.get("o") or {}, me, room)
        if o:
            bcast(room, {"t": "live", "u": me, "o": o, "app": bool(m.get("app"))}, skip=conn)
    elif t == "op":
        op = m.get("op") or {}
        k = op.get("k")
        if k == "add":
            obj = clean_obj(op.get("obj") or {}, me, room)
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
            if old and (not mine(old) or locked(old)):
                send(conn, {"t": "ack", "id": "d:" + old["id"]})
                return deny("That layer is locked" if mine(old) else "You can only erase your own work")
            if old and commit(room, {"k": "del", "id": old["id"]}, me):
                push_undo(room, me, ("del", [old]))
            else:
                send(conn, {"t": "ack", "id": "d:" + str(op.get("id"))})
        elif k in ("fill", "set"):
            old = room.objects.get(op.get("id"))
            if not old:
                return send(conn, {"t": "ack", "id": k[0] + ":" + str(op.get("id"))})
            attrs = clean_attrs(room, old, {"f": op.get("f", "")} if k == "fill" else op.get("attrs"))
            if attrs is None:
                return deny("Invalid change")
            if not mine(old) or locked(old, attrs):
                send(conn, {"t": "ack", "id": k[0] + ":" + old["id"]})
                return deny("That layer is locked" if mine(old) else "You can only change your own work")
            prev = {a: old.get(a, ATTRS[a]) for a in attrs} if old else {}
            if old and commit(room, {"k": "set", "id": old["id"], "attrs": attrs}, me):
                push_undo(room, me, ("set", [(old["id"], prev, attrs)]))
            else:
                send(conn, {"t": "ack", "id": k[0] + ":" + str(op.get("id"))})
        elif k == "repl":           # area eraser / clip: objects replaced by their remaining pieces
            rid, dels, adds = str(op.get("rid", ""))[:40], op.get("del"), op.get("add")
            if not (rid and isinstance(dels, list) and isinstance(adds, list)
                    and len(dels) <= MAX_PIECES and len(adds) <= MAX_PIECES):
                return deny("Invalid erase")
            if "r:" + rid in room.seen:
                return send(conn, {"t": "ack", "id": "r:" + rid})
            olds = [room.objects[i] for i in dels if isinstance(i, str) and i in room.objects]
            if not all(mine(o) and not locked(o) for o in olds):
                send(conn, {"t": "ack", "id": "r:" + rid})
                return deny("You can only erase your own work")
            authors = {o["id"]: o.get("u", me) for o in olds}   # pieces keep the original author
            adds = [clean_obj(o, authors.get(o.get("src"), me), room) if isinstance(o, dict) else None for o in adds]
            if None in adds or any(o["k"] == "image" for o in adds) or any(o["id"] in room.seen for o in adds) \
                    or len(room.objects) + len(adds) > MAX_OBJECTS + len(dels):
                return deny("Invalid erase")
            room.seen |= {"r:" + rid} | {o["id"] for o in adds}
            op = {"k": "repl", "rid": rid, "del": [o["id"] for o in olds], "add": adds}
            if commit(room, op, me):
                push_undo(room, me, ("repl", (olds, adds)))
            else:
                send(conn, {"t": "ack", "id": "r:" + rid})
    elif t in ("undo", "redo"):
        undo_redo(room, me, t == "redo")
    elif t == "save":           # editors and above
        name = str(m.get("name") or time.strftime("%H:%M:%S"))[:40]
        room.checks.append({"name": name, "ts": int(time.time()), "objects": list(room.objects.values())})
        del room.checks[:-MAX_CHECKS]
        room.dirty = True
        bcast(room, {"t": "checks", "checks": snapshot(room, me)["checks"]})
        log.info("[%s] %s saved version %r", room.code, me, name)
    elif t in ("page_add", "page_rename"):     # editors and above manage whiteboards (pages)
        name = str(m.get("name") or "").strip()[:30] or f"Page {len(room.pages) + 1}"
        if t == "page_add":
            if len(room.pages) >= MAX_PAGES:
                return deny("Too many pages")
            room.pages.append({"id": secrets.token_hex(4), "name": name})
        else:
            pg = next((pg for pg in room.pages if pg["id"] == m.get("id")), None)
            if not pg:
                return deny("No such page")
            pg["name"] = name
        room.dirty = True
        bcast(room, {"t": "pages", "pages": room.pages})
    elif not admin:
        deny("Only the host or an admin can do that")
    elif t == "page_del":
        pid = m.get("id")
        if len(room.pages) < 2 or pid not in {pg["id"] for pg in room.pages}:
            return deny("Can't delete the last page")
        gone = [o for o in room.objects.values() if o.get("pg", "main") == pid]
        if gone:
            push_undo(room, me, ("del", gone))
            commit(room, {"k": "clear", "pg": pid}, me)
        room.pages = [pg for pg in room.pages if pg["id"] != pid]
        for o in list(room.objects.values()):        # links to the deleted page now lead nowhere
            if o.get("ln") == pid:
                commit(room, {"k": "set", "id": o["id"], "attrs": {"ln": ""}}, me)
        room.dirty = True
        bcast(room, {"t": "pages", "pages": room.pages})
        log.info("[%s] %s deleted a page", room.code, me)
    elif t == "clear":
        pg = m.get("pg") if m.get("pg") in {p["id"] for p in room.pages} else None
        gone = [o for o in room.objects.values() if pg is None or o.get("pg", "main") == pg]
        if gone:
            push_undo(room, me, ("del", gone))
            commit(room, {"k": "clear", "pg": pg}, me)
            log.info("[%s] %s cleared %s", room.code, me, "a page" if pg else "the board")
    elif t == "role":
        if not may_change_role(room, me, m.get("user"), m.get("role")):
            return deny("You can't change that person's role")
        set_role(room, m["user"], m["role"], me)
    elif t in ("grant", "refuse"):      # answer to an access request (at the door or in the room)
        user, want = m.get("user"), m.get("role")
        if user in room.waiting:
            fut, _ = room.waiting[user]
            if t == "grant" and (want not in ROLES or (want == "admin" and not host and role != "admin")):
                return deny("Invalid role")
            if not fut.done():
                fut.set_result((want, me) if t == "grant" else (None, me))
        elif user in room.users and t == "grant":
            if not may_change_role(room, me, user, want):
                return deny("You can't give that role")
            set_role(room, user, want, me)
        elif user in room.conns:
            send(room.conns[user], {"t": "error", "code": "REFUSED", "text": f"{me} declined your request"})
        for c in approvers(room):
            send(c, {"t": "reqdone", "user": user})
    elif t in ("kick", "ban"):
        name = m.get("user")
        if name not in room.users or name == room.host or (not host and room.role(name) == "admin"):
            return deny("You can't remove that person")
        if t == "ban":
            if m.get("ip"):
                ip = room.users[name].get("ip")
                host_ip = room.users[room.host].get("ip")
                if not ip or ip == host_ip:
                    return deny("Can't ban that address (it is the owner's)")
                if ip not in room.bans["ips"]:
                    room.bans["ips"].append(ip)
                for n, c in list(room.conns.items()):
                    if c.ip == ip and n != room.host:
                        kick(room, n, f"{me} banned your address from this room")
            elif name not in room.bans["users"]:
                room.bans["users"].append(name)
            room.dirty = True
            for c in approvers(room):
                send(c, admins_info(room))
        kick(room, name, f"{me} {'banned' if t == 'ban' else 'removed'} you")
        log.info("[%s] %s %s %s%s", room.code, me, "banned" if t == "ban" else "kicked", name,
                 " (by IP)" if m.get("ip") else "")
    elif t == "unban":
        for key in ("users", "ips"):
            if m.get(key[:-1]) in room.bans[key]:
                room.bans[key].remove(m[key[:-1]])
        room.dirty = True
        send(conn, admins_info(room))
        log.info("[%s] %s lifted a ban", room.code, me)
    elif t == "restore" and isinstance(m.get("idx"), int) and 0 <= m["idx"] < len(room.checks):
        room.objects = {o["id"]: o for o in room.checks[m["idx"]]["objects"]}
        room.seen |= set(room.objects)
        room.seq += 1
        room.undo, room.redo, room.dirty = {}, {}, True
        bcast_snap(room)
        log.info("[%s] %s restored version %r", room.code, me, room.checks[m["idx"]]["name"])
    elif t == "present":        # presentation mode: everyone follows the presenter's view
        if m.get("on"):
            room.presenter, room.view = me, m.get("v") if isinstance(m.get("v"), dict) else None
        elif room.presenter in (me, None) or host:
            room.presenter, room.view = None, None
        bcast(room, {"t": "present", "by": room.presenter, "view": room.view})
        log.info("[%s] presentation %s by %s", room.code, "started" if room.presenter else "stopped", me)


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
    if h.get("create"):                 # the server always assigns the code
        salt, key = str(h.get("salt", "")), str(h.get("key", ""))
        if not (HEX_RE.match(salt) and HEX_RE.match(key)) or bool(salt) != bool(key):
            return await fail("BAD_AUTH", "Malformed password data")
        code = new_code()
        room = ROOMS[code] = Room(code, user, salt, key, "viewer" if h.get("role") == "viewer" else "editor",
                                  private=bool(h.get("private")), approval=bool(h.get("approval")))
        room.empty_since = None
        save_room(room)
        log.info("[%s] created by %s from %s (%s%s%s)", code, user, conn.peer,
                 "private" if room.private else "public", ", password" if key else "",
                 ", approval required" if room.approval else "")
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
    if conn.ip in room.bans["ips"] or user in room.bans["users"]:
        return await fail("BANNED", "You are banned from this room")
    token, u = str(h.get("token", "")), room.users.get(user)
    if u:
        same = bool(token) and hmac.compare_digest(token, u["token"])    # reconnect with session token
        if (user in room.conns or (user == room.host and not room.key)) and not same:
            return await fail("NAME_TAKEN", "That name is in use in this room")
    elif user in room.waiting:
        return await fail("NAME_TAKEN", "Someone with that name is already waiting to enter")
    conn.user = user
    want = h.get("want") if h.get("want") in ROLES else None
    if not u and user != room.host and (room.approval or want == "admin"):
        # Knock at the door: a host or admin must let a newcomer in (and decides the role).
        if room.closed or not approvers(room):
            return await fail("NO_APPROVER", "No host or admin is online to let you in")
        want = want or room.default_role
        fut = asyncio.get_running_loop().create_future()
        room.waiting[user] = (fut, want)
        send(conn, {"t": "waiting", "text": f"Waiting for the host or an admin to let you in as {want}..."})
        for c in approvers(room):
            send(c, {"t": "req", "user": user, "role": want, "inroom": False})
        log.info("[%s] %s (%s) asks to enter as %s", room.code, user, conn.peer, want)
        try:
            granted, by = await asyncio.wait_for(fut, 300)
        except asyncio.TimeoutError:
            granted, by = None, "nobody"
        finally:
            room.waiting.pop(user, None)
            for c in approvers(room):
                send(c, {"t": "reqdone", "user": user})
        if not granted:
            log.info("[%s] %s was not let in (by %s)", room.code, user, by)
            send(conn, {"t": "error", "code": "DENIED", "text": "Your request to enter was declined"})
            return None
        log.info("[%s] %s let %s in as %s", room.code, by, user, granted)
        want = granted
    elif not u and want and want != room.default_role and want != "admin":
        want = None                     # open room: the host's default applies; ask for more once inside
    if room.closed:
        await reopen(conn, reader, room)
    if u:
        old = room.conns.get(user)
        if old:                    # stale half-dead connection from the same user: replace it
            old.w.close()
    else:
        u = room.users[user] = {"role": want or room.default_role, "token": secrets.token_hex(16),
                                "color": COLORS[len(room.users) % len(COLORS)]}
    conn.room = room
    room.conns[user] = conn
    u["ip"] = conn.ip
    room.dirty, room.empty_since = True, None
    send(conn, {"t": "welcome", "room": room.code, "host": room.host, "boot": BOOT, "saved_at": room.saved_at,
                "you": {"name": user, "token": u["token"], "color": u["color"]}, **snapshot(room, user)})
    bcast(room, {"t": "users", "users": users_info(room)})
    if room.role(user) in APPROVERS:
        send(conn, admins_info(room))
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
            if room.presenter == conn.user:
                room.presenter, room.view = None, None
                bcast(room, {"t": "present", "by": None, "view": None})
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
