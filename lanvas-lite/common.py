"""Shared by client and server: message framing, password proofs, and the board-operation reducer.

Wire format: every message is a 4-byte big-endian length followed by that many bytes of UTF-8 JSON.
TCP is a byte stream (one recv may hold half a message or three), so the length prefix is what
lets both sides find message boundaries.
"""
import hashlib
import hmac
import json
import math
import struct

PORT = 47801
MAX_FRAME = 64 * 1024 * 1024   # server -> client (a full snapshot can be big)
MAX_CLIENT_FRAME = 1024 * 1024  # client -> server, after login


def pack(msg):
    """Serialise one message into a length-prefixed frame."""
    body = json.dumps(msg, separators=(",", ":")).encode()
    return struct.pack(">I", len(body)) + body


def recv_msg(sock, limit=MAX_FRAME):
    """Blocking read of one frame from a socket (client side)."""
    def exact(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed")
            buf += chunk
        return buf
    (n,) = struct.unpack(">I", exact(4))
    if n > limit:
        raise ValueError("frame too large")
    return json.loads(exact(n))


async def read_msg(reader, limit=MAX_CLIENT_FRAME):
    """Read one frame from an asyncio StreamReader (server side)."""
    (n,) = struct.unpack(">I", await reader.readexactly(4))
    if n > limit:
        raise ValueError("frame too large")
    return json.loads(await reader.readexactly(n))


def derive_key(password, salt_hex):
    """Slow salted hash of the room password. The server stores only this, never the password."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 50_000).hex()


def proof(key_hex, nonce):
    """Challenge-response: proves knowledge of the key without sending it (nonce stops replays)."""
    return hmac.new(bytes.fromhex(key_hex), nonce.encode(), "sha256").hexdigest()


ATTRS = {"c": "#000000", "w": 1, "f": "", "h": False}     # editable on the design page, with defaults


def apply_op(objects, op):
    """Apply one board operation to `objects` (id -> object; dict order is the drawing order).

    Used identically by server and clients. Every op is idempotent, so a duplicated or retransmitted
    op changes nothing. Returns True only if the board actually changed.
        {"k": "add", "obj": {...}}   {"k": "del", "id": "..."}   {"k": "clear"}
        {"k": "fill", "id": "...", "f": "#rrggbb" or ""}          (set an object's fill colour)
        {"k": "repl", "del": [ids], "add": [objs]}                (area eraser / clip: replace by pieces)
        {"k": "set", "id": "...", "attrs": {"c", "w", "f", "h"}}  (design page: colour, width, fill, hidden)
    """
    k = op.get("k")
    if k == "add":
        if op["obj"]["id"] in objects:
            return False
        objects[op["obj"]["id"]] = op["obj"]
        return True
    if k == "del":
        return objects.pop(op.get("id"), None) is not None
    if k == "clear":
        had = bool(objects)
        objects.clear()
        return had
    if k == "fill":
        o = objects.get(op.get("id"))
        if not o or o.get("f", "") == op.get("f", ""):
            return False
        objects[o["id"]] = {**o, "f": op.get("f", "")}      # new dict: never mutate copies held by undo
        return True
    if k == "set":
        o, attrs = objects.get(op.get("id")), {a: v for a, v in (op.get("attrs") or {}).items() if a in ATTRS}
        if not o or all(o.get(a, ATTRS[a]) == v for a, v in attrs.items()):
            return False
        objects[o["id"]] = {**o, **attrs}
        return True
    if k == "repl":
        changed = False
        for i in op.get("del", []):
            changed |= objects.pop(i, None) is not None
        for o in op.get("add", []):
            if o["id"] not in objects:
                objects[o["id"]] = o
                changed = True
        return changed
    return False


# ---------- area ("regular") eraser geometry ----------

def outline(o):
    """An erasable object as a polyline [x0,y0,x1,y1,...], or None (text and notes are not erasable)."""
    k, p = o["k"], o["p"]
    if k in ("stroke", "line"):
        return list(p)
    if k == "rect":
        x0, y0, x1, y1 = p
        return [x0, y0, x1, y0, x1, y1, x0, y1, x0, y0]
    if k == "oval":
        cx, cy, rx, ry = (p[0] + p[2]) / 2, (p[1] + p[3]) / 2, abs(p[2] - p[0]) / 2, abs(p[3] - p[1]) / 2
        n = 64
        return [v for i in range(n + 1) for v in (cx + rx * math.cos(2 * math.pi * i / n),
                                                  cy + ry * math.sin(2 * math.pi * i / n))]
    return None


def _dense(pts, step):
    """Polyline -> list of (x, y) with no gap longer than `step`."""
    xs, ys = pts[0::2], pts[1::2]
    dense = [(xs[0], ys[0])]
    for i in range(1, len(xs)):
        (ax, ay), bx, by = dense[-1], xs[i], ys[i]
        n = max(1, int(math.hypot(bx - ax, by - ay) / step))
        dense += [(ax + (bx - ax) * j / n, ay + (by - ay) * j / n) for j in range(1, n + 1)]
    return dense


def _runs(dense, keep, max_pts):
    """Split the kept points into continuous polylines (closed outlines rejoin across their start)."""
    pieces, run = [], []
    for (px, py), k in zip(dense, keep):
        if k:
            run.append((round(px, 1), round(py, 1)))
        if (not k or len(run) >= max_pts) and run:
            if len(run) >= 2:
                pieces.append([v for q in run for v in q])
            run = run[-1:] if k else []          # a split at max_pts keeps continuity
    if len(run) >= 2:
        pieces.append([v for q in run for v in q])
    loop = dense[0] == dense[-1] and keep[0] and keep[-1]
    if loop and len(pieces) >= 2 and len(pieces[0]) + len(pieces[-1]) <= 2 * max_pts:
        pieces[0] = pieces.pop() + pieces[0][2:]
    return pieces


def erase_pieces(o, x, y, r, max_pts=1400):
    """Rub out the part of `o` within radius r of (x, y). Returns None if `o` is untouched, otherwise the
    list of remaining polylines (possibly empty = fully erased). Segments are densified first so the
    eraser cuts a clean gap even through long straight lines."""
    pts = outline(o)
    if pts is None:
        return None
    rr = r + o["w"] / 2
    xs, ys = pts[0::2], pts[1::2]
    if x < min(xs) - rr or x > max(xs) + rr or y < min(ys) - rr or y > max(ys) + rr:
        return None
    dense = _dense(pts, max(2.0, rr / 2))
    keep = [math.hypot(px - x, py - y) > rr for px, py in dense]
    return None if all(keep) else _runs(dense, keep, max_pts)


def clip_pieces(o, x0, y0, x1, y1, max_pts=1400):
    """Clip `o` to a rectangle: keep only the parts inside it. None = not clippable (text/note/image)."""
    pts = outline(o)
    if pts is None:
        return None
    x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
    dense = _dense(pts, 2.0)
    return _runs(dense, [x0 <= px <= x1 and y0 <= py <= y1 for px, py in dense], max_pts)
