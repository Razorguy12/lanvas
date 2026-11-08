"""The server: one thread, one ``selectors`` loop, every socket non-blocking.

Single-threaded on purpose. Each frame is handled to completion before the next, so assigning
``seq`` and ``chat_seq`` is a plain increment with no locks, and global ordering is trivially
correct.

Every incoming frame passes the same pipeline (Agents.md section 7):
size cap (decoder) -> state machine -> role -> schema -> rate limit.
"""

from __future__ import annotations

import logging
import random
import selectors
import socket
import time
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any

from common import chat as c
from common.identity import valid_fingerprint
from common.model import (
    KIND_BULK_CREATE,
    KIND_CLEAR,
    KIND_RESTORE,
    validate_op,
)
from common.protocol import (
    AWAY_AFTER,
    MAX_FRAME,
    MAX_FRAME_PREAUTH,
    MAX_SERVER_FRAME,
    RELEASE_AFTER,
    TCP_PORT,
    UDP_PORT,
    ErrorCode,
    FrameDecoder,
    MsgType,
    ProtocolError,
    encode_frame,
)
from common.snapshot import board_hash
from server import acl as A
from server.discovery import Discovery, default_server_name
from server.persist import atomic_write_json, iso_utc, read_json_with_bak
from server.rooms import MAX_ROOM_NAME, Room, new_room_code

log = logging.getLogger(__name__)

MAX_NAME = 64
MAX_TRANSIENT = 16 * 1024
TRANSIENT_DROP_OUTBOX = 256 * 1024
DEFAULT_MAX_OUTBOX = 32 * 1024 * 1024
HANDSHAKE_TIMEOUT = 10.0
PARTIAL_FRAME_TIMEOUT = 15.0
CLOSE_FLUSH_TIMEOUT = 2.0
GROUP_COMMIT_WINDOW = 0.1
SLOW_TICK = 1.0
MAX_ROOMS = 2000
INVITE_DEFAULT_TTL = 86400.0
INVITE_MAX_TTL = 30 * 86400.0

# Fields a client may set on an op. Whitelisting means "seq", "author" and "ts" can never be forged.
_OP_FIELDS = (
    "kind", "op_id", "base_seq", "id", "type", "parent", "z",
    "props", "patch", "objects", "replace", "checkpoint_id",
)  # fmt: skip


class State(Enum):
    CONNECTED = "connected"
    PENDING = "pending"
    ACTIVE = "active"
    CLOSING = "closing"


class Connection:
    """One client socket with its own decoder, outbox and position in the state machine."""

    def __init__(self, sock: socket.socket, addr: tuple[str, int], now: float, clock: Any) -> None:
        self.sock = sock
        self.ip = addr[0]
        self.decoder = FrameDecoder(MAX_FRAME_PREAUTH)
        self.outbox = bytearray()
        self.state = State.CONNECTED
        self.created = now
        self.last_recv = now
        self.partial_since: float | None = None
        self.close_deadline: float | None = None
        self.hello: dict[str, Any] = {}
        self.fp = ""
        self.name = ""
        self.client_id = f"{random.getrandbits(32):08x}"
        self.role: str | None = None
        self.room: Room | None = None
        self.away = False
        self.op_bucket = A.TokenBucket(500, 1000, clock)
        self.chat_bucket = A.TokenBucket(5, 10, clock)
        self.transient_bucket = A.TokenBucket(120, 240, clock)
        self.request_bucket = A.TokenBucket(20, 40, clock)
        self.write_registered = False


class Server:
    def __init__(
        self,
        data_dir: Path,
        host: str = "0.0.0.0",
        port: int = TCP_PORT,
        clock: Callable[[], float] = time.time,
        discovery: bool = True,
        discovery_port: int = UDP_PORT,
        discovery_targets: list[tuple[str, int]] | None = None,
        discovery_broadcast: bool = True,
        max_outbox: int = DEFAULT_MAX_OUTBOX,
        max_conns_per_ip: int = 64,
        max_conns: int = 1024,
        request_limit: int = 10,
        request_window: float = 60.0,
        request_cooldown: float = 60.0,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.host = host
        self.port = port
        self.clock = clock
        self.max_outbox = max_outbox
        self.max_conns_per_ip = max_conns_per_ip
        self.max_conns = max_conns
        self.selector = selectors.DefaultSelector()
        self.listener: socket.socket | None = None
        self.conns: set[Connection] = set()
        self.rooms: dict[str, Room] = {}
        self.ip_counts: dict[str, int] = {}
        self.request_limiter = A.IpRequestLimiter(
            clock, request_limit, request_window, request_cooldown
        )
        self.want_discovery = discovery
        self.discovery_port = discovery_port
        self.discovery_targets = discovery_targets
        self.discovery_broadcast = discovery_broadcast
        self.discovery: Discovery | None = None
        self._stop = False
        self._crash = False
        self._closed = False
        self._last_commit = 0.0
        self._last_slow = 0.0
        self.server_id = ""

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Run the integrity scan first; only a repaired server starts accepting connections."""
        self._load_rooms()
        self.server_id = self._load_server_id()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((self.host, self.port))
        self.listener.listen(128)
        self.listener.setblocking(False)
        self.port = self.listener.getsockname()[1]
        self.selector.register(self.listener, selectors.EVENT_READ, self._on_accept)
        if self.want_discovery:
            self.discovery = Discovery(
                self.discovery_port,
                self.server_id,
                default_server_name(),
                self.port,
                self.discoverable_rooms,
                self.clock,
                self.discovery_targets,
                self.discovery_broadcast,
            )
            self.selector.register(self.discovery.sock, selectors.EVENT_READ, self._on_discovery)

    def _load_server_id(self) -> str:
        path = self.data_dir / "server.json"
        meta, _ = read_json_with_bak(path)
        if isinstance(meta, dict) and isinstance(meta.get("server_id"), str):
            return meta["server_id"]
        server_id = f"{random.getrandbits(64):016x}"
        atomic_write_json(path, {"server_id": server_id}, keep_bak=True)
        return server_id

    def _load_rooms(self) -> None:
        root = self.data_dir / "rooms"
        if not root.exists():
            return
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            room = Room.load(self.data_dir, directory.name, self.clock)
            if room is not None:
                self.rooms[room.room_id] = room

    def run(self) -> None:
        try:
            while not self._stop:
                self.poll(0.05)
        finally:
            self._shutdown()

    def request_stop(self, crash: bool = False) -> None:
        """Safe from another thread: only sets flags that the loop reads on its next iteration."""
        self._crash = crash
        self._stop = True

    def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._crash:
            for room in self.rooms.values():
                room.sync()
                room.maybe_snapshot(self.clock())
        for conn in list(self.conns):
            try:
                conn.sock.close()
            except OSError:
                pass
        self.conns.clear()
        if self.listener is not None:
            self.listener.close()
        if self.discovery is not None:
            self.discovery.close()
        for room in self.rooms.values():
            room.close()
        self.selector.close()

    def poll(self, timeout: float = 0.0) -> None:
        for key, events in self.selector.select(timeout):
            callback = key.data
            if isinstance(callback, Connection):
                self._on_conn_event(callback, events)
            else:
                callback()
        self._tick()

    # ------------------------------------------------------------------ timers
    def _tick(self) -> None:
        now = self.clock()
        # Real time, not self.clock: with a frozen fake clock the commit must still happen.
        mono = time.monotonic()
        if mono - self._last_commit >= GROUP_COMMIT_WINDOW:
            self._last_commit = mono
            for room in self.rooms.values():
                room.sync()
        if self.discovery is not None:
            self.discovery.tick()
        if now - self._last_slow >= SLOW_TICK or now < self._last_slow:
            self._last_slow = now
            self._check_timeouts(now)
            for room in self.rooms.values():
                room.maybe_snapshot(now)

    def _check_timeouts(self, now: float) -> None:
        for conn in list(self.conns):
            if conn.state is State.CLOSING:
                if conn.close_deadline is not None and now >= conn.close_deadline:
                    self._drop(conn)
                continue
            if conn.state is State.CONNECTED:
                if now - conn.created > HANDSHAKE_TIMEOUT:
                    self._fail(conn, ErrorCode.TIMEOUT, "handshake timed out")
                    continue
            if conn.partial_since is not None and now - conn.partial_since > PARTIAL_FRAME_TIMEOUT:
                self._fail(conn, ErrorCode.TIMEOUT, "partial frame timed out")  # slow-loris
                continue
            silent = now - conn.last_recv
            if silent > RELEASE_AFTER:
                # The slot is released, but ACL membership is untouched: they can simply return.
                self._fail(conn, ErrorCode.TIMEOUT, "no traffic")
            elif silent > AWAY_AFTER and not conn.away and conn.state is State.ACTIVE:
                conn.away = True
                self._presence(conn.room, "away", conn)

    # ------------------------------------------------------------------ socket events
    def _on_discovery(self) -> None:
        if self.discovery is not None:
            self.discovery.on_readable()

    def _on_accept(self) -> None:
        assert self.listener is not None
        for _ in range(64):
            try:
                sock, addr = self.listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            self.adopt(sock, addr)

    def adopt(self, sock: socket.socket, addr: tuple[str, int]) -> Connection:
        """Take ownership of a connected socket. Tests use this directly with a socketpair."""
        sock.setblocking(False)
        if sock.family == socket.AF_INET:
            # Small frames must not wait for Nagle: this is what keeps op latency in milliseconds.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn = Connection(sock, addr, self.clock(), self.clock)
        self.conns.add(conn)
        self.ip_counts[conn.ip] = self.ip_counts.get(conn.ip, 0) + 1
        self.selector.register(sock, selectors.EVENT_READ, conn)
        if self.ip_counts[conn.ip] > self.max_conns_per_ip or len(self.conns) > self.max_conns:
            self._fail(conn, ErrorCode.TOO_MANY_CONNECTIONS, "connection limit reached")
        return conn

    def _on_conn_event(self, conn: Connection, events: int) -> None:
        if events & selectors.EVENT_WRITE:
            self._flush(conn)
        if events & selectors.EVENT_READ and conn in self.conns:
            self._read(conn)

    def _read(self, conn: Connection) -> None:
        try:
            data = conn.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop(conn)
            return
        if not data:
            self._drop(conn)
            return
        if conn.state is State.CLOSING:
            return  # we are done listening; bytes are discarded while the ERROR flushes
        conn.last_recv = self.clock()
        if conn.away and conn.state is State.ACTIVE:
            conn.away = False
            self._presence(conn.room, "back", conn)
        try:
            for msg_type, payload in conn.decoder.feed(data):
                if conn.state is State.CLOSING or conn not in self.conns:
                    return
                self._dispatch(conn, msg_type, payload)
        except ProtocolError as exc:
            self._fail(conn, exc.code, str(exc))
            return
        if conn in self.conns:
            conn.partial_since = (
                (conn.partial_since or self.clock()) if conn.decoder.has_partial else None
            )

    def _send_bytes(self, conn: Connection, frame: bytes) -> None:
        if conn.state is State.CLOSING and conn.close_deadline is None:
            return
        conn.outbox += frame
        if len(conn.outbox) > self.max_outbox:
            # One stalled client must not grow memory without bound or slow anyone else (RC-07).
            log.warning("dropping slow client %s", conn.ip)
            self._drop(conn)
            return
        self._flush(conn)

    def _send(self, conn: Connection, msg_type: int, payload: dict[str, Any] | None = None) -> None:
        self._send_bytes(conn, encode_frame(msg_type, payload, MAX_SERVER_FRAME))

    def _flush(self, conn: Connection) -> None:
        if conn not in self.conns:
            return
        try:
            while conn.outbox:
                sent = conn.sock.send(conn.outbox[:262144])
                del conn.outbox[:sent]
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            self._drop(conn)
            return
        # EVENT_WRITE is registered only while there is backlog; otherwise every select would
        # return immediately (a writable socket is nearly always writable) and spin the CPU.
        want_write = bool(conn.outbox)
        if want_write != conn.write_registered:
            mask = selectors.EVENT_READ | (selectors.EVENT_WRITE if want_write else 0)
            self.selector.modify(conn.sock, mask, conn)
            conn.write_registered = want_write
        if not conn.outbox and conn.state is State.CLOSING:
            self._drop(conn)

    def _fail(self, conn: Connection, code: str, message: str = "") -> None:
        """Send a final ERROR and close once it has flushed."""
        if conn not in self.conns or conn.state is State.CLOSING:
            return
        was_active = conn.state is State.ACTIVE
        room = conn.room
        self._send(conn, MsgType.ERROR, {"code": code, "message": message, "fatal": True})
        if conn not in self.conns:
            return
        self._detach(conn, room, was_active)
        conn.state = State.CLOSING
        conn.close_deadline = self.clock() + CLOSE_FLUSH_TIMEOUT
        self._flush(conn)

    def _detach(self, conn: Connection, room: Room | None, was_active: bool) -> None:
        conn.role = None
        if room is None:
            return
        room.connections.discard(conn)
        if was_active:
            self._presence(room, "leave", conn)
        conn.room = None

    def _drop(self, conn: Connection) -> None:
        if conn not in self.conns:
            return
        self.conns.discard(conn)
        self._detach(conn, conn.room, conn.state is State.ACTIVE)
        self.ip_counts[conn.ip] = self.ip_counts.get(conn.ip, 1) - 1
        if self.ip_counts[conn.ip] <= 0:
            del self.ip_counts[conn.ip]
        try:
            self.selector.unregister(conn.sock)
        except (KeyError, ValueError):
            pass
        try:
            conn.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ dispatch
    def _dispatch(self, conn: Connection, msg_type: int, msg: dict[str, Any]) -> None:
        try:
            if conn.state is State.CONNECTED:
                if msg_type != MsgType.HELLO:
                    self._fail(conn, ErrorCode.BAD_STATE, "HELLO expected")  # AC-03
                    return
                self._on_hello(conn, msg)
            elif conn.state is State.PENDING:
                # Deny by default: nothing but keep-alive is answered, and no error is sent that
                # could confirm or describe anything about the room's contents.
                if msg_type == MsgType.PING:
                    self._send(conn, MsgType.PONG, {"t": msg.get("t")})
            elif conn.state is State.ACTIVE:
                handler = self._active_handlers().get(msg_type)
                if handler is None:
                    self._send(conn, MsgType.ERROR, {"code": ErrorCode.BAD_STATE, "fatal": False})
                else:
                    handler(conn, msg)
        except OSError:
            # A failing disk must not take the whole server down with it.
            log.exception("I/O error handling frame %s", msg_type)
            self._send(conn, MsgType.ERROR, {"code": ErrorCode.SAVE_FAILED, "fatal": False})
        except (KeyError, TypeError, ValueError, AttributeError, IndexError, ProtocolError) as exc:
            # Schema validation should have caught this. Hostile input ends here either way: it
            # costs that one connection, and never the server.
            log.warning("malformed frame %s from %s: %r", msg_type, conn.ip, exc)
            self._fail(conn, ErrorCode.BAD_SCHEMA, "malformed frame")

    def _active_handlers(self) -> dict[int, Callable[[Connection, dict[str, Any]], None]]:
        return {
            MsgType.OP: self._on_op,
            MsgType.TRANSIENT: self._on_transient,
            MsgType.CHAT: self._on_chat,
            MsgType.CHAT_HISTORY: self._on_chat_history,
            MsgType.CHAT_READ: self._on_chat_read,
            MsgType.RESYNC: self._on_resync,
            MsgType.SAVE: self._on_save,
            MsgType.ADMIN: self._on_admin,
            MsgType.ACCESS_DECISION: self._on_access_decision,
            MsgType.PING: lambda conn, msg: self._send(conn, MsgType.PONG, {"t": msg.get("t")}),
            MsgType.PONG: lambda conn, msg: None,
        }

    # ------------------------------------------------------------------ handshake
    def _on_hello(self, conn: Connection, msg: dict[str, Any]) -> None:
        fingerprint = msg.get("client_id")
        name = msg.get("username")
        if not valid_fingerprint(fingerprint) or not isinstance(name, str):
            self._fail(conn, ErrorCode.BAD_SCHEMA, "bad HELLO")
            return
        conn.fp = fingerprint
        conn.name = _clean_name(name)
        conn.hello = msg
        self._admit(conn)

    def _find_room(self, hello: dict[str, Any]) -> tuple[Room | None, bool]:
        """Locate the target room. Returns (room, code_matched)."""
        room_id = hello.get("room_id")
        code = hello.get("room_code")
        if isinstance(room_id, str) and room_id in self.rooms:
            room = self.rooms[room_id]
            return room, room.code_matches(code)
        if isinstance(code, str):
            for room in self.rooms.values():
                if room.code_matches(code):
                    return room, True
        return None, False

    def _admit(self, conn: Connection) -> None:
        """Apply the room access decision after reading the client-supplied ID."""
        hello = conn.hello
        create = hello.get("create")
        if create is not None:
            self._create_room(conn, create)
            return
        room, code_ok = self._find_room(hello)
        if room is None:
            # Counted, so that room codes cannot be guessed at full speed.
            self.request_limiter.allow(conn.ip)
            self._fail(conn, ErrorCode.ROOM_NOT_FOUND, "no such room")
            return
        role = room.acl.role_of(conn.fp)
        if role is not None:
            room.acl.remember_name(conn.fp, conn.name)
            self._activate(conn, room, role)
            return
        left = room.acl.cooldown_left(conn.fp)
        if left > 0:
            retry = None if left == float("inf") else left
            self._send(conn, MsgType.ERROR, {"code": ErrorCode.ACCESS_DENIED, "retry_after": retry})
            self._close_after_flush(conn)
            return
        invite = hello.get("invite")
        if isinstance(invite, str):
            invited_role = room.acl.consume_invite(invite)
            if invited_role is not None:
                room.acl.add_member(conn.fp, conn.name, invited_role)
                self._activate(conn, room, invited_role)
                self._presence(room, "member_added", conn)
                return
        settings = room.acl.settings
        if not code_ok and not settings["discoverable"]:
            # Answer exactly as for a missing room, so a hidden room's existence is not revealed.
            self.request_limiter.allow(conn.ip)
            self._fail(conn, ErrorCode.ROOM_NOT_FOUND, "no such room")
            return
        if not code_ok and settings["require_code"]:
            self.request_limiter.allow(conn.ip)
            self._fail(conn, ErrorCode.CODE_REQUIRED, "this room needs its code")
            return
        self._hold_pending(conn, room)

    def _hold_pending(self, conn: Connection, room: Room) -> None:
        already = room.acl.request_status(conn.fp) == A.STATUS_PENDING
        if not already and not self.request_limiter.allow(conn.ip):
            self._fail(conn, ErrorCode.RATE_LIMITED, "too many requests")  # AC-10
            return
        if not already:
            request = room.acl.record_request(conn.fp, conn.name, conn.ip)
            for owner in self._owner_conns(room):
                self._send(owner, MsgType.ACCESS_REQUEST, _request_view(conn.fp, request))
        conn.state = State.PENDING
        conn.room = room
        room.connections.add(conn)
        # Only what discovery already told them: never members, board, or chat.
        self._send(
            conn,
            MsgType.ACCESS_PENDING,
            {"room_name": room.name, "host_name": room.host_name, "fingerprint": conn.fp},
        )

    def _close_after_flush(self, conn: Connection) -> None:
        conn.state = State.CLOSING
        conn.close_deadline = self.clock() + CLOSE_FLUSH_TIMEOUT
        self._flush(conn)

    def _create_room(self, conn: Connection, create: Any) -> None:
        if not isinstance(create, dict) or not isinstance(create.get("name"), str):
            self._fail(conn, ErrorCode.BAD_SCHEMA, "bad create")
            return
        name = _clean_name(create["name"], MAX_ROOM_NAME)
        code = create.get("code")
        if not isinstance(code, str) or not 0 < len(code) <= 64:
            code = new_room_code()
        if len(self.rooms) >= MAX_ROOMS or not self.request_limiter.allow(conn.ip):
            self._fail(conn, ErrorCode.RATE_LIMITED, "cannot create more rooms now")
            return
        room = Room.create(
            self.data_dir,
            self.clock,
            name,
            code,
            conn.fp,
            conn.name,
            discoverable=create.get("discoverable", True) is not False,
        )
        for key in ("require_code", "show_member_count"):
            if isinstance(create.get(key), bool):
                room.acl.settings[key] = create[key]
        room.acl.save()
        self.rooms[room.room_id] = room
        self._activate(conn, room, A.ROLE_OWNER, extra={"room_code": code})
        self._announce_soon()

    def _activate(
        self, conn: Connection, room: Room, role: str, extra: dict[str, Any] | None = None
    ) -> None:
        first_for_identity = not any(
            other.fp == conn.fp and other.state is State.ACTIVE for other in room.connections
        )
        conn.state = State.ACTIVE
        conn.role = role
        conn.room = room
        conn.decoder.max_frame = MAX_FRAME
        room.connections.add(conn)
        self._send(conn, MsgType.WELCOME, self._welcome(conn, room, role) | (extra or {}))
        if first_for_identity:
            self._presence(room, "join", conn)

    def _welcome(self, conn: Connection, room: Room, role: str) -> dict[str, Any]:
        settings = room.acl.settings
        payload: dict[str, Any] = {
            "role": role,
            "client_id": conn.client_id,
            "fingerprint": conn.fp,
            "last_seq": room.seq,
            "room": _room_view(room),
            "members": self._member_list(room),
            "chat_tail": room.chat.tail(),
            "room_chat_seq": room.chat.room.seq,
            "room_read": room.chat.read.get(conn.fp, {}).get(c.CHANNEL_ROOM, 0),
            "dm_index": room.chat.dm_index(conn.fp, room.acl.members),
            "muted": room.acl.is_muted(conn.fp),
            "settings": settings,
        }
        index = room.oplog.read_index()
        payload["last_checkpoint"] = index[-1] if index else None
        if role == A.ROLE_OWNER:
            payload["pending_requests"] = room.acl.pending_requests()  # AC-12
        last_seq = conn.hello.get("last_seq")
        delta = room.ops_since(last_seq) if type(last_seq) is int and last_seq >= 0 else None
        if delta is None:
            payload["snapshot"] = room.board.to_json()
        else:
            payload["delta"] = delta
        return payload

    def _member_list(self, room: Room) -> list[dict[str, Any]]:
        online: dict[str, bool] = {}
        for other in room.connections:
            if other.state is State.ACTIVE:
                online[other.fp] = online.get(other.fp, True) and other.away
        return [
            {
                "fp": fp,
                "name": member["name"],
                "role": member["role"],
                "muted": member.get("muted", False),
                "online": fp in online,
                "away": online.get(fp, False),
            }
            for fp, member in room.acl.members.items()
        ]

    # ------------------------------------------------------------------ fan-out helpers
    def _active(self, room: Room | None) -> list[Connection]:
        if room is None:
            return []
        return [conn for conn in room.connections if conn.state is State.ACTIVE]

    def _owner_conns(self, room: Room) -> list[Connection]:
        return [conn for conn in self._active(room) if conn.role == A.ROLE_OWNER]

    def _broadcast(self, room: Room | None, msg_type: int, payload: dict[str, Any]) -> None:
        """Send to ACTIVE members only. PENDING connections sit in the same set, so this matters."""
        frame = encode_frame(msg_type, payload, MAX_SERVER_FRAME)
        for conn in self._active(room):
            self._send_bytes(conn, frame)

    def _presence(self, room: Room | None, event: str, conn: Connection) -> None:
        if room is None:
            return
        if event == "leave" and any(
            other is not conn and other.fp == conn.fp and other.state is State.ACTIVE
            for other in room.connections
        ):
            return  # the person is still here on another connection
        member = room.acl.members.get(conn.fp, {})
        self._broadcast(
            room,
            MsgType.PRESENCE,
            {
                "event": event,
                "fp": conn.fp,
                "name": member.get("name", conn.name),
                "role": member.get("role"),
                "muted": member.get("muted", False),
                "client_id": conn.client_id,
            },
        )

    def _member_event(self, room: Room, event: str, fp: str) -> None:
        member = room.acl.members.get(fp, {})
        self._broadcast(
            room,
            MsgType.PRESENCE,
            {
                "event": event,
                "fp": fp,
                "name": member.get("name", ""),
                "role": member.get("role"),
                "muted": member.get("muted", False),
            },
        )

    def _announce_soon(self) -> None:
        if self.discovery is not None:
            self.discovery.request_announce()

    # ------------------------------------------------------------------ ops
    def _on_op(self, conn: Connection, msg: dict[str, Any]) -> None:
        room = conn.room
        assert room is not None
        op_id = msg.get("op_id")
        if not isinstance(op_id, str) or not 0 < len(op_id) <= 96:
            self._reject_op(conn, op_id, ErrorCode.BAD_SCHEMA)
            return
        denied = self._op_permission(conn, room, msg)
        if denied:
            self._reject_op(conn, op_id, denied)
            return
        seen = room.seen_op_ids.get(op_id)
        if seen is not None:
            self._send(conn, MsgType.OP_ACK, {"op_id": op_id, "seq": seen, "duplicate": True})
            return
        if not conn.op_bucket.allow():
            self._reject_op(conn, op_id, ErrorCode.RATE_LIMITED)
            return
        error = validate_op(room.board, msg)
        if error:
            self._reject_op(conn, op_id, error)
            return
        op = {key: msg[key] for key in _OP_FIELDS if key in msg}
        if op["kind"] == KIND_RESTORE:
            checkpoint = room.oplog.load_checkpoint(op["checkpoint_id"])
            if checkpoint is None:
                self._reject_op(conn, op_id, ErrorCode.NOT_FOUND)
                return
            # Expanded before logging, so replay never depends on the checkpoint file still
            # existing.
            op["objects"] = list(checkpoint["board"].get("objects", {}).values())
        op["author"] = conn.fp
        op["author_name"] = room.acl.members[conn.fp]["name"]
        logged = room.apply_and_log(op)
        self._send(conn, MsgType.OP_ACK, {"op_id": op_id, "seq": logged["seq"]})
        self._broadcast(room, MsgType.OP, logged)

    def _op_permission(self, conn: Connection, room: Room, msg: dict[str, Any]) -> str | None:
        kind = msg.get("kind")
        settings = room.acl.settings
        if conn.role != A.ROLE_OWNER and settings.get("locked"):
            return ErrorCode.ROOM_LOCKED
        if kind == KIND_CLEAR:
            allowed = A.can(conn.role, A.A_CLEAR, settings)
        elif kind == KIND_RESTORE or (kind == KIND_BULK_CREATE and msg.get("replace")):
            allowed = A.can(conn.role, A.A_RESTORE, settings)
        else:
            allowed = A.can(conn.role, A.A_EDIT, settings)
        return None if allowed else ErrorCode.NOT_ALLOWED

    def _reject_op(self, conn: Connection, op_id: Any, code: str) -> None:
        log.warning("op rejected: %s from %s", code, conn.fp[:9])
        self._send(conn, MsgType.OP_REJECT, {"op_id": op_id, "code": code})

    def _on_transient(self, conn: Connection, msg: dict[str, Any]) -> None:
        """Cursors and live previews are droppable, so overload sheds them first."""
        if not conn.transient_bucket.allow():
            return
        forwarded = {
            "from": conn.fp,
            "client_id": conn.client_id,
            "name": conn.name,
            "k": msg.get("k"),
            "d": msg.get("d"),
        }
        frame = encode_frame(MsgType.TRANSIENT, forwarded)
        if len(frame) > MAX_TRANSIENT:
            return
        for other in self._active(conn.room):
            if other is not conn and len(other.outbox) < TRANSIENT_DROP_OUTBOX:
                self._send_bytes(other, frame)

    def _on_resync(self, conn: Connection, msg: dict[str, Any]) -> None:
        room = conn.room
        assert room is not None
        from_seq = msg.get("from_seq")
        ops = room.ops_since(from_seq) if type(from_seq) is int and from_seq >= 0 else None
        if ops is None:
            self._send(
                conn,
                MsgType.SNAPSHOT,
                {
                    "seq": room.seq,
                    "board": room.board.to_json(),
                    "board_hash": board_hash(room.board),
                },
            )
        else:
            self._send(conn, MsgType.RESYNC, {"ops": ops, "seq": room.seq})

    # ------------------------------------------------------------------ chat
    def _on_chat(self, conn: Connection, msg: dict[str, Any]) -> None:
        room = conn.room
        assert room is not None
        msg_id = msg.get("msg_id")

        def reject(code: str) -> None:
            log.warning("chat rejected: %s from %s", code, conn.fp[:9])
            self._send(conn, MsgType.CHAT_REJECT, {"msg_id": msg_id, "code": code})

        if room.acl.is_muted(conn.fp):
            reject(ErrorCode.MUTED)
            return
        error, text = c.validate_outgoing(msg)
        if error:
            reject(error)
            return
        channel_kind = msg["channel"]
        to_fp = msg.get("to") if channel_kind == c.CHANNEL_DM else None
        if to_fp is not None and (to_fp not in room.acl.members or to_fp == conn.fp):
            reject(ErrorCode.UNKNOWN_RECIPIENT)  # CT-06
            return
        dup = room.chat.duplicate_seq(channel_kind, conn.fp, to_fp, msg_id)
        if dup is not None:
            self._send(
                conn,
                MsgType.CHAT_ACK,
                {"msg_id": msg_id, "channel_id": dup[0], "chat_seq": dup[1], "duplicate": True},
            )
            return
        if not conn.chat_bucket.allow():
            reject(ErrorCode.RATE_LIMITED)
            return
        message = room.chat.append(
            channel_kind,
            conn.fp,
            room.acl.members[conn.fp]["name"],
            to_fp,
            text,
            msg_id,
            msg.get("reply_to"),
        )
        self._send(
            conn,
            MsgType.CHAT_ACK,
            {
                "msg_id": msg_id,
                "channel_id": message["channel_id"],
                "chat_seq": message["chat_seq"],
                "ts": message["ts"],
            },
        )
        if to_fp is None:
            self._broadcast(room, MsgType.CHAT, message)
        else:
            # Routed strictly by the two fingerprints. Nothing else in the room is ever consulted.
            frame = encode_frame(MsgType.CHAT, message)
            for other in self._active(room):
                if other.fp in (conn.fp, to_fp):
                    self._send_bytes(other, frame)

    def _on_chat_history(self, conn: Connection, msg: dict[str, Any]) -> None:
        room = conn.room
        assert room is not None
        channel = room.chat.channel_for_reader(msg.get("channel_id"), conn.fp)
        if channel is None or not conn.request_bucket.allow():
            self._send(
                conn,
                MsgType.CHAT_REJECT,
                {"channel_id": msg.get("channel_id"), "code": ErrorCode.NOT_ALLOWED},
            )  # CT-07
            return
        before = msg.get("before_chat_seq")
        before = before if type(before) is int else None
        limit = msg.get("limit")
        limit = limit if type(limit) is int and 0 < limit <= c.MAX_HISTORY_PAGE else 50
        page = channel.page(before, limit)
        oldest = page[0]["chat_seq"] if page else None
        has_more = bool(oldest and channel.messages and channel.messages[0]["chat_seq"] < oldest)
        self._send(
            conn,
            MsgType.CHAT_HISTORY,
            {"channel_id": channel.channel_id, "messages": page, "has_more": has_more},
        )

    def _on_chat_read(self, conn: Connection, msg: dict[str, Any]) -> None:
        room = conn.room
        assert room is not None
        channel = room.chat.channel_for_reader(msg.get("channel_id"), conn.fp)
        seq = msg.get("chat_seq")
        if channel is None or type(seq) is not int or not conn.request_bucket.allow():
            return
        room.chat.mark_read(conn.fp, channel.channel_id, min(seq, channel.seq))

    # ------------------------------------------------------------------ save
    def _on_save(self, conn: Connection, msg: dict[str, Any]) -> None:
        room = conn.room
        assert room is not None
        if not A.can(conn.role, A.A_SAVE, room.acl.settings):
            self._send(conn, MsgType.SAVE_REJECT, {"code": ErrorCode.NOT_ALLOWED})
            return
        if not conn.request_bucket.allow():
            self._send(conn, MsgType.SAVE_REJECT, {"code": ErrorCode.RATE_LIMITED})
            return
        try:
            room.sync()  # SAVE forces the fsync that group commit would otherwise delay
            entry = room.oplog.write_checkpoint(
                room.board,
                room.room_id,
                room.seq,
                self.clock(),
                {"fp": conn.fp, "name": room.acl.members[conn.fp]["name"]},
                msg.get("label"),
            )
        except OSError:
            log.exception("checkpoint failed for room %s", room.room_id)
            self._send(conn, MsgType.SAVE_REJECT, {"code": ErrorCode.SAVE_FAILED})
            return
        self._send(conn, MsgType.SAVE_ACK, entry)

    # ------------------------------------------------------------------ owner actions
    def _on_access_decision(self, conn: Connection, msg: dict[str, Any]) -> None:
        room = conn.room
        assert room is not None
        if not A.can(conn.role, A.A_ADMIN, room.acl.settings):
            self._send(conn, MsgType.ERROR, {"code": ErrorCode.NOT_ALLOWED, "fatal": False})
            return
        target = msg.get("fingerprint")
        decision = msg.get("decision")
        status = room.acl.request_status(target) if isinstance(target, str) else None
        if status is None:
            self._send(conn, MsgType.ERROR, {"code": ErrorCode.NOT_FOUND, "fatal": False})
            return
        waiting = [
            other for other in room.connections
            if other.fp == target and other.state is State.PENDING
        ]  # fmt: skip
        if decision == "approve":
            role = msg.get("role")
            if role not in A.ASSIGNABLE_ROLES:
                self._send(conn, MsgType.ERROR, {"code": ErrorCode.BAD_SCHEMA, "fatal": False})
                return
            name = room.acl.requests[target]["name"]
            room.acl.add_member(target, name, role)
            log.info("room %s: approved %s as %s", room.room_id, target[:9], role)
            self._member_event(room, "member_added", target)
            for other in waiting:
                self._activate(other, room, role)  # live transition, with no reconnect (AC-06)
        elif decision in ("deny", "block"):
            room.acl.deny(target, block=decision == "block")
            log.info("room %s: %s %s", room.room_id, decision, target[:9])
            for other in waiting:
                self._send(
                    other,
                    MsgType.ERROR,
                    {"code": ErrorCode.ACCESS_DENIED, "retry_after": A.DENY_COOLDOWN},
                )
                self._detach(other, room, False)
                self._close_after_flush(other)
        else:
            self._send(conn, MsgType.ERROR, {"code": ErrorCode.BAD_SCHEMA, "fatal": False})

    def _on_admin(self, conn: Connection, msg: dict[str, Any]) -> None:
        room = conn.room
        assert room is not None
        action = msg.get("action")
        handler = {
            "set_role": self._admin_set_role,
            "kick": self._admin_kick,
            "revoke": self._admin_kick,
            "mute": self._admin_mute,
            "set_settings": self._admin_settings,
            "rename": self._admin_rename,
            "clear_chat": self._admin_clear_chat,
            "create_invite": self._admin_invite,
            "list_checkpoints": self._admin_list_checkpoints,
            "delete_checkpoint": self._admin_delete_checkpoint,
            "delete_room": self._admin_delete_room,
        }.get(action)
        if not A.can(conn.role, A.A_ADMIN, room.acl.settings):
            self._send(
                conn,
                MsgType.ERROR,
                {"code": ErrorCode.NOT_ALLOWED, "action": action, "fatal": False},
            )
            return
        if handler is None:
            self._send(conn, MsgType.ERROR, {"code": ErrorCode.BAD_SCHEMA, "fatal": False})
            return
        log.info("room %s: admin %s by %s", room.room_id, action, conn.fp[:9])
        handler(conn, room, msg)

    def _admin_ok(self, conn: Connection, action: str, **extra: Any) -> None:
        self._send(conn, MsgType.ADMIN, {"action": action, "ok": True, **extra})

    def _admin_fail(self, conn: Connection, action: str, code: str) -> None:
        self._send(conn, MsgType.ERROR, {"code": code, "action": action, "fatal": False})

    def _target(self, conn: Connection, room: Room, msg: dict[str, Any]) -> str | None:
        """A member who may be acted on. The owner can never demote or remove themselves."""
        target = msg.get("fp")
        if (
            not isinstance(target, str)
            or target not in room.acl.members
            or target == room.acl.owner
        ):
            self._admin_fail(conn, str(msg.get("action")), ErrorCode.NOT_FOUND)
            return None
        return target

    def _admin_set_role(self, conn: Connection, room: Room, msg: dict[str, Any]) -> None:
        target = self._target(conn, room, msg)
        role = msg.get("role")
        if target is None:
            return
        if role not in A.ASSIGNABLE_ROLES:
            self._admin_fail(conn, "set_role", ErrorCode.BAD_SCHEMA)
            return
        room.acl.members[target]["role"] = role
        room.acl.save()
        for other in self._active(room):
            if other.fp == target:
                other.role = role  # takes effect on the very next frame
        self._member_event(room, "role", target)
        self._admin_ok(conn, "set_role")

    def _admin_kick(self, conn: Connection, room: Room, msg: dict[str, Any]) -> None:
        """Kick removes membership; revoke additionally starts the denied cool-down."""
        target = self._target(conn, room, msg)
        if target is None:
            return
        action = msg["action"]
        name = room.acl.members[target]["name"]
        del room.acl.members[target]
        if action == "revoke":
            room.acl.requests[target] = {
                "name": name, "ip": "", "time": self.clock(), "status": A.STATUS_DENIED,
            }  # fmt: skip
        room.acl.save()
        code = ErrorCode.KICKED if action == "kick" else ErrorCode.REVOKED
        for other in self._active(room):
            if other.fp == target:
                self._fail(other, code, "removed by the owner")
        self._broadcast(
            room, MsgType.PRESENCE, {"event": "member_removed", "fp": target, "name": name}
        )
        self._admin_ok(conn, action)
        self._announce_soon()

    def _admin_mute(self, conn: Connection, room: Room, msg: dict[str, Any]) -> None:
        target = self._target(conn, room, msg)
        if target is None:
            return
        room.acl.members[target]["muted"] = msg.get("muted", True) is not False
        room.acl.save()
        self._member_event(room, "mute", target)
        self._admin_ok(conn, "mute")

    def _admin_settings(self, conn: Connection, room: Room, msg: dict[str, Any]) -> None:
        changes = msg.get("settings")
        if not isinstance(changes, dict):
            self._admin_fail(conn, "set_settings", ErrorCode.BAD_SCHEMA)
            return
        for key, value in changes.items():
            if key not in A.BOOL_SETTINGS or not isinstance(value, bool):
                self._admin_fail(conn, "set_settings", ErrorCode.BAD_SCHEMA)
                return
        room.acl.settings.update(changes)
        room.acl.save()
        self._broadcast(
            room,
            MsgType.PRESENCE,
            {"event": "room", "room": _room_view(room), "settings": room.acl.settings},
        )
        self._admin_ok(conn, "set_settings")
        self._announce_soon()

    def _admin_rename(self, conn: Connection, room: Room, msg: dict[str, Any]) -> None:
        if not isinstance(msg.get("name"), str) or not msg["name"].strip():
            self._admin_fail(conn, "rename", ErrorCode.BAD_SCHEMA)
            return
        room.name = _clean_name(msg["name"], MAX_ROOM_NAME)
        room.save_meta()
        self._broadcast(
            room,
            MsgType.PRESENCE,
            {"event": "room", "room": _room_view(room), "settings": room.acl.settings},
        )
        self._admin_ok(conn, "rename")
        self._announce_soon()

    def _admin_clear_chat(self, conn: Connection, room: Room, msg: dict[str, Any]) -> None:
        room.chat.clear_room()
        self._broadcast(room, MsgType.PRESENCE, {"event": "chat_cleared"})
        self._admin_ok(conn, "clear_chat")

    def _admin_invite(self, conn: Connection, room: Room, msg: dict[str, Any]) -> None:
        role = msg.get("role")
        if role not in A.ASSIGNABLE_ROLES:
            self._admin_fail(conn, "create_invite", ErrorCode.BAD_SCHEMA)
            return
        ttl = msg.get("ttl", INVITE_DEFAULT_TTL)
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not 0 < ttl <= INVITE_MAX_TTL
        ):
            self._admin_fail(conn, "create_invite", ErrorCode.BAD_SCHEMA)
            return
        token = room.acl.create_invite(role, float(ttl), msg.get("single_use", True) is not False)
        # This proof-of-concept keeps invite tokens in plain text in the room ACL file.
        self._admin_ok(
            conn, "create_invite", token=token, role=role, expires=iso_utc(self.clock() + ttl)
        )

    def _admin_list_checkpoints(self, conn: Connection, room: Room, msg: dict[str, Any]) -> None:
        self._admin_ok(conn, "list_checkpoints", checkpoints=room.oplog.read_index())

    def _admin_delete_checkpoint(self, conn: Connection, room: Room, msg: dict[str, Any]) -> None:
        checkpoint_id = msg.get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or not room.oplog.delete_checkpoint(checkpoint_id):
            self._admin_fail(conn, "delete_checkpoint", ErrorCode.NOT_FOUND)
            return
        self._admin_ok(conn, "delete_checkpoint", checkpoint_id=checkpoint_id)

    def _admin_delete_room(self, conn: Connection, room: Room, msg: dict[str, Any]) -> None:
        """Hide the room and disconnect everyone. Files stay on disk, moved aside to be
        recoverable."""
        for other in list(room.connections):
            self._fail(other, ErrorCode.ROOM_NOT_FOUND, "the room was deleted")
        room.close()
        del self.rooms[room.room_id]
        trash = self.data_dir / "deleted_rooms"
        trash.mkdir(parents=True, exist_ok=True)
        room.paths.root.replace(trash / room.room_id)
        self._announce_soon()

    # ------------------------------------------------------------------ discovery data
    def discoverable_rooms(self) -> list[dict[str, Any]]:
        """Only the fields listed in Architecture.md section 4.4, built explicitly field by
        field."""
        rooms = []
        for room in self.rooms.values():
            settings = room.acl.settings
            if not settings["discoverable"]:
                continue
            entry: dict[str, Any] = {
                "room_id": room.room_id,
                "name": room.name,
                "requires_approval": True,
                "require_code": settings["require_code"],
                "host_name": room.host_name,
            }
            if settings["show_member_count"]:
                entry["member_count"] = len(
                    {other.fp for other in room.connections if other.state is State.ACTIVE}
                )
            rooms.append(entry)
        return rooms


def _clean_name(name: str, limit: int = MAX_NAME) -> str:
    cleaned = c.clean_text(name).replace("\n", " ")[:limit]
    return cleaned or "Anonymous"


def _room_view(room: Room) -> dict[str, Any]:
    return {
        "id": room.room_id,
        "name": room.name,
        "host_name": room.host_name,
        "owner": room.acl.owner,
    }


def _request_view(fp: str, request: dict[str, Any]) -> dict[str, Any]:
    return {
        "fingerprint": fp,
        "name": request["name"],
        "ip": request["ip"],
        "time": request["time"],
    }
