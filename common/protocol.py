"""Wire protocol: frame layout, message types, error codes, and stream reassembly.

TCP is a byte stream, so one ``recv`` may hold half a frame or twenty frames. Everything that
reads the socket goes through :class:`FrameDecoder`, which is a pure state machine with no socket
in it. That is what lets the framing fuzz tests (FR-*) split input at every byte boundary.

Frame layout (Architecture.md section 4.1), 9 byte header followed by the payload::

    magic "LV" (2) | version u8 | type u8 | flags u8 | length u32 big-endian | payload
"""

from __future__ import annotations

import json
import struct
import zlib
from enum import IntEnum
from typing import Any

PROTOCOL_VERSION = 3

MAGIC = b"LV"
HEADER = struct.Struct(">2sBBBI")
HEADER_SIZE = HEADER.size

FLAG_COMPRESSED = 0x01
COMPRESS_THRESHOLD = 1024

MAX_FRAME = 8 * 1024 * 1024
MAX_FRAME_PREAUTH = 64 * 1024
# A small compressed frame can expand enormously, so inflation is capped separately.
MAX_INFLATED = 64 * 1024 * 1024
# Client-to-server frames stay under MAX_FRAME. Only the server may send a frame this large, because
# a full board snapshot with embedded images can legitimately exceed 8 MB even after compression.
MAX_SERVER_FRAME = MAX_INFLATED
MAX_JSON_DEPTH = 32

TCP_PORT = 47801
UDP_PORT = 47800

ANNOUNCE = "LANVAS_ANNOUNCE"
DISCOVER = "LANVAS_DISCOVER"
MAX_ANNOUNCE_BYTES = 1200
MAX_DISCOVER_BYTES = 512
ANNOUNCE_INTERVAL = 2.0
BOARD_EXPIRY = 7.0

PING_INTERVAL = 5.0
AWAY_AFTER = 15.0
RELEASE_AFTER = 60.0


class MsgType(IntEnum):
    """Every frame type on the wire (Architecture.md section 4.2)."""

    HELLO = 1
    # 2 and 3 were challenge/proof messages in the removed signed-identity protocol.
    ACCESS_PENDING = 4
    ACCESS_REQUEST = 5
    ACCESS_DECISION = 6
    WELCOME = 7
    OP = 8
    OP_ACK = 9
    OP_REJECT = 10
    TRANSIENT = 11
    CHAT = 12
    CHAT_ACK = 13
    CHAT_REJECT = 14
    CHAT_HISTORY = 15
    CHAT_READ = 16
    PRESENCE = 17
    RESYNC = 18
    SNAPSHOT = 19
    SAVE = 20
    SAVE_ACK = 21
    SAVE_REJECT = 22
    ADMIN = 23
    PING = 24
    PONG = 25
    ERROR = 26


class ErrorCode:
    """Stable error codes, so clients branch on a constant rather than on free text."""

    BAD_FRAME = "BAD_FRAME"
    BAD_VERSION = "BAD_VERSION"
    FRAME_TOO_LARGE = "FRAME_TOO_LARGE"
    BAD_JSON = "BAD_JSON"
    BAD_STATE = "BAD_STATE"
    BAD_SCHEMA = "BAD_SCHEMA"
    ACCESS_DENIED = "ACCESS_DENIED"
    ROOM_NOT_FOUND = "ROOM_NOT_FOUND"
    CODE_REQUIRED = "CODE_REQUIRED"
    NOT_ALLOWED = "NOT_ALLOWED"
    ROOM_LOCKED = "ROOM_LOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    TOO_MANY_CONNECTIONS = "TOO_MANY_CONNECTIONS"
    TIMEOUT = "TIMEOUT"
    KICKED = "KICKED"
    REVOKED = "REVOKED"
    MUTED = "MUTED"
    UNKNOWN_RECIPIENT = "UNKNOWN_RECIPIENT"
    UNKNOWN_CHANNEL = "UNKNOWN_CHANNEL"
    ID_EXISTS = "ID_EXISTS"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    UNKNOWN_KIND = "UNKNOWN_KIND"
    NOT_FOUND = "NOT_FOUND"
    SAVE_FAILED = "SAVE_FAILED"
    SLOW_CLIENT = "SLOW_CLIENT"


class ProtocolError(Exception):
    """The peer broke the framing rules. Stream position is lost, so the connection must close."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def json_depth_ok(raw: bytes, limit: int = MAX_JSON_DEPTH) -> bool:
    """Reject nesting bombs by scanning bytes *before* parsing.

    ``json.loads`` recurses, so a deep enough payload could exhaust the interpreter stack. Scanning
    first means an attacker's input never reaches the recursive code path at all. Multi-byte UTF-8
    sequences never contain the ASCII bytes tested here, so a byte scan is safe.
    """
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { [
            depth += 1
            if depth > limit:
                return False
        elif byte in (0x7D, 0x5D):  # } ]
            depth -= 1
    return True


def _reject_constant(name: str) -> None:
    # NaN and Infinity are not JSON and would poison canonical state checks, so they are refused.
    raise ValueError(f"non-finite number: {name}")


def encode_payload(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def decode_payload(raw: bytes) -> dict[str, Any]:
    if not json_depth_ok(raw):
        raise ProtocolError(ErrorCode.BAD_JSON, "JSON nested too deeply")
    try:
        obj = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise ProtocolError(ErrorCode.BAD_JSON, str(exc)) from exc
    if not isinstance(obj, dict):
        raise ProtocolError(ErrorCode.BAD_JSON, "payload must be a JSON object")
    return obj


def encode_frame(
    msg_type: int, payload: dict[str, Any] | None = None, limit: int = MAX_FRAME
) -> bytes:
    """Serialize one frame, compressing payloads over 1 KB (Architecture.md section 9)."""
    body = encode_payload(payload if payload is not None else {})
    flags = 0
    if len(body) > COMPRESS_THRESHOLD:
        compressed = zlib.compress(body, 6)
        if len(compressed) < len(body):
            body = compressed
            flags = FLAG_COMPRESSED
    if len(body) > limit:
        raise ProtocolError(ErrorCode.FRAME_TOO_LARGE, f"frame of {len(body)} bytes")
    return HEADER.pack(MAGIC, PROTOCOL_VERSION, int(msg_type), flags, len(body)) + body


def _inflate(body: bytes) -> bytes:
    # decompressobj with max_length bounds memory; plain zlib.decompress would inflate everything.
    inflater = zlib.decompressobj()
    try:
        out = inflater.decompress(body, MAX_INFLATED)
    except zlib.error as exc:
        raise ProtocolError(ErrorCode.BAD_FRAME, f"bad compressed data: {exc}") from exc
    if inflater.unconsumed_tail or not inflater.eof:
        raise ProtocolError(ErrorCode.FRAME_TOO_LARGE, "inflated payload too large or truncated")
    return out


class FrameDecoder:
    """Incremental decoder. Feed it arbitrary chunks; it returns whole frames in order.

    Memory is bounded: the header is validated as soon as it is complete, so an oversized length
    is refused before any of that payload has to be buffered (FR-03).
    """

    def __init__(self, max_frame: int = MAX_FRAME) -> None:
        self.max_frame = max_frame
        self._buf = bytearray()

    @property
    def has_partial(self) -> bool:
        """True while part of a frame is buffered; the server uses it to time out slow-loris."""
        return bool(self._buf)

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> list[tuple[int, dict[str, Any]]]:
        self._buf.extend(data)
        frames: list[tuple[int, dict[str, Any]]] = []
        while True:
            frame = self._next_frame()
            if frame is None:
                return frames
            frames.append(frame)

    def _check_prefix(self) -> None:
        # Validate magic and version byte by byte, so garbage fails on its first bad byte instead
        # of waiting for nine bytes that may never arrive.
        head = bytes(self._buf[:2])
        if head and head != MAGIC[: len(head)]:
            raise ProtocolError(ErrorCode.BAD_FRAME, "bad magic")
        if len(self._buf) >= 3 and self._buf[2] != PROTOCOL_VERSION:
            raise ProtocolError(ErrorCode.BAD_VERSION, f"unsupported version {self._buf[2]}")

    def _next_frame(self) -> tuple[int, dict[str, Any]] | None:
        self._check_prefix()
        if len(self._buf) < HEADER_SIZE:
            return None
        _, _, msg_type, flags, length = HEADER.unpack_from(self._buf)
        if length > self.max_frame:
            raise ProtocolError(ErrorCode.FRAME_TOO_LARGE, f"frame length {length}")
        if flags & ~FLAG_COMPRESSED:
            raise ProtocolError(ErrorCode.BAD_FRAME, "unknown flags")
        if len(self._buf) < HEADER_SIZE + length:
            return None
        body = bytes(self._buf[HEADER_SIZE : HEADER_SIZE + length])
        del self._buf[: HEADER_SIZE + length]
        if flags & FLAG_COMPRESSED:
            body = _inflate(body)
        return msg_type, decode_payload(body)
