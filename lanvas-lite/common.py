"""Shared by client and server: message framing, password proofs, and the board-operation reducer.

Wire format: every message is a 4-byte big-endian length followed by that many bytes of UTF-8 JSON.
TCP is a byte stream (one recv may hold half a message or three), so the length prefix is what
lets both sides find message boundaries.
"""
import hashlib
import hmac
import json
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


def apply_op(objects, op):
    """Apply one board operation to `objects` (id -> object; dict order is the drawing order).

    Used identically by server and clients. Every op is idempotent, so a duplicated or retransmitted
    op changes nothing. Returns True only if the board actually changed.
        {"k": "add", "obj": {...}}   {"k": "del", "id": "..."}   {"k": "clear"}
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
    return False
