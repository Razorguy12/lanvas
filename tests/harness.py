"""Test harness (Architecture.md section 14.2). Bots share common/protocol.py but own their sockets."""

from __future__ import annotations

import json
import select
import socket
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from common import durable
from common.identity import Identity
from common.model import Board, apply_op
from common.protocol import MAX_SERVER_FRAME, FrameDecoder, MsgType, encode_frame
from common.snapshot import board_hash


class SimulatedCrash(BaseException):
    """BaseException so no `except Exception` in product code can accidentally swallow a crash."""


class FaultyFS:
    """Injects a crash at the named durable-writer step, after ``skip`` earlier hits of that step."""

    def __init__(self, step: str, skip: int = 0) -> None:
        self.step = step
        self.skip = skip

    def __enter__(self) -> FaultyFS:
        def hook(name: str) -> None:
            if name == self.step:
                if self.skip == 0:
                    durable.set_fault_hook(None)
                    raise SimulatedCrash(name)
                self.skip -= 1

        durable.set_fault_hook(hook)
        return self

    def __exit__(self, *exc: object) -> None:
        durable.set_fault_hook(None)


class FakeClock:
    """Injected into the server so timeout behavior can be tested without sleeping."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def wait_until(predicate: Callable[[], Any], timeout: float = 5.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


class BotClient:
    """A headless client over a raw TCP socket."""

    def __init__(
        self,
        port: int,
        name: str = "bot",
        key: Identity | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        self.host = host
        self.port = port
        self.name = name
        self.key = key or Identity.generate()
        self.fp = self.key.fingerprint
        self.client_id = uuid.uuid4().hex[:8]
        self.counter = 0
        self.sock: socket.socket | None = None
        self.decoder = FrameDecoder(MAX_SERVER_FRAME)
        self.inbox: list[tuple[int, dict[str, Any]]] = []
        self.closed = False
        self.board = Board()
        self.seq = 0
        self.role: str | None = None
        self.welcome: dict[str, Any] | None = None
        self.seen_seqs: list[int] = []

    # ---- transport ----
    def connect(self) -> BotClient:
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.decoder = FrameDecoder(MAX_SERVER_FRAME)
        self.closed = False
        return self

    def send(self, msg_type: int, payload: dict[str, Any] | None = None) -> None:
        assert self.sock is not None
        self.sock.sendall(encode_frame(msg_type, payload))

    def send_raw(self, data: bytes) -> None:
        assert self.sock is not None
        self.sock.sendall(data)

    def pump(self, timeout: float = 0.0) -> bool:
        """Read whatever is available. Returns True if any bytes arrived."""
        if self.sock is None or self.closed:
            return False
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return False
        try:
            data = self.sock.recv(1 << 20)
        except OSError:
            data = b""
        if not data:
            self.closed = True
            return False
        for frame in self.decoder.feed(data):
            self.inbox.append(frame)
            self._observe(*frame)
        return True

    def _observe(self, msg_type: int, msg: dict[str, Any]) -> None:
        if msg_type == MsgType.WELCOME:
            self.welcome = msg
            self.role = msg["role"]
            if "snapshot" in msg:
                self.board = Board.from_json(msg["snapshot"])
                self.seq = msg["last_seq"]
            for op in msg.get("delta", []):
                self._apply(op)
        elif msg_type == MsgType.OP:
            self._apply(msg)
        elif msg_type == MsgType.SNAPSHOT:
            self.board = Board.from_json(msg["board"])
            self.seq = msg["seq"]
        elif msg_type == MsgType.RESYNC:
            for op in msg.get("ops", []):
                self._apply(op)

    def _apply(self, op: dict[str, Any]) -> None:
        if op["seq"] <= self.seq:
            return
        apply_op(self.board, op)
        self.seq = op["seq"]
        self.seen_seqs.append(op["seq"])

    def wait_for(
        self,
        msg_type: int,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        timeout: float = 5.0,
        consume: bool = True,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            for i, (t, m) in enumerate(self.inbox):
                if t == msg_type and (predicate is None or predicate(m)):
                    if consume:
                        del self.inbox[i]
                    return m
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                seen = [MsgType(t).name for t, _ in self.inbox]
                raise AssertionError(f"{self.name}: no {MsgType(msg_type).name}; have {seen}")
            self.pump(min(remaining, 0.05))
            if self.closed:
                for t, m in self.inbox:
                    if t == msg_type and (predicate is None or predicate(m)):
                        return m
                seen = [MsgType(t).name for t, _ in self.inbox]
                raise AssertionError(f"{self.name}: closed, no {MsgType(msg_type).name}; have {seen}")

    def wait_closed(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while not self.closed and time.monotonic() < deadline:
            self.pump(0.02)
        return self.closed

    def drain(self, quiet: float = 0.03) -> list[tuple[int, dict[str, Any]]]:
        """Read until the socket goes quiet, and return everything queued so far."""
        while self.pump(quiet):
            pass
        return list(self.inbox)

    def types_seen(self) -> set[int]:
        return {t for t, _ in self.inbox}

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.closed = True

    # ---- protocol helpers ----
    def hello(self, **fields: Any) -> None:
        payload = {"username": self.name, "client_id": self.fp, **fields}
        self.send(MsgType.HELLO, payload)

    def handshake(self, **fields: Any) -> None:
        self.hello(**fields)

    def join(self, **fields: Any) -> dict[str, Any]:
        """Connect and expect WELCOME: for owners, existing members, and invite holders."""
        self.connect()
        self.handshake(**fields)
        return self.wait_for(MsgType.WELCOME, consume=False)

    def request(self, **fields: Any) -> dict[str, Any]:
        """Connect as an unknown identity and expect ACCESS_PENDING."""
        self.connect()
        self.handshake(**fields)
        return self.wait_for(MsgType.ACCESS_PENDING)

    def next_op_id(self) -> str:
        self.counter += 1
        return f"{self.client_id}:{self.counter}"

    def send_op(self, op: dict[str, Any], op_id: str | None = None) -> str:
        op_id = op_id or self.next_op_id()
        self.send(MsgType.OP, {"op_id": op_id, "base_seq": self.seq, **op})
        return op_id

    def create_rect(self, obj_id: str | None = None, **props: Any) -> str:
        obj_id = obj_id or uuid.uuid4().hex[:12]
        base = {"x": 0, "y": 0, "w": 10, "h": 10}
        self.send_op({"kind": "create", "id": obj_id, "type": "rect", "z": "V", "props": base | props})
        return obj_id

    def send_chat(self, text: str, msg_id: str | None = None, **extra: Any) -> str:
        msg_id = msg_id or self.next_op_id()
        self.send(MsgType.CHAT, {"msg_id": msg_id, "channel": "room", "text": text, **extra})
        return msg_id

    def send_dm(self, to_fp: str, text: str, msg_id: str | None = None) -> str:
        msg_id = msg_id or self.next_op_id()
        self.send(MsgType.CHAT, {"msg_id": msg_id, "channel": "dm", "to": to_fp, "text": text})
        return msg_id

    def save(self, label: str | None = None) -> None:
        self.send(MsgType.SAVE, {"label": label} if label else {})

    def admin(self, action: str, **fields: Any) -> None:
        self.send(MsgType.ADMIN, {"action": action, **fields})

    def barrier(self) -> None:
        """Wait until the server has processed everything this bot sent.

        Frames on one connection are handled in order, so the PONG proves all earlier ops were
        sequenced. This replaces guessing how long the server needs.
        """
        self.send(MsgType.PING, {"t": "barrier"})
        self.wait_for(MsgType.PONG, lambda m: m.get("t") == "barrier", timeout=30)

    def recv_until(self, seq: int, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while self.seq < seq:
            if time.monotonic() > deadline or self.closed:
                raise AssertionError(f"{self.name}: at seq {self.seq}, wanted {seq}")
            self.pump(0.05)

    def board_hash(self) -> str:
        return board_hash(self.board)

    @property
    def room_id(self) -> str:
        assert self.welcome is not None
        return self.welcome["room"]["id"]

    def bytes_contain(self, needle: str) -> bool:
        return any(needle in json.dumps(m, ensure_ascii=False) for _, m in self.inbox)


class DiscoveryProbe:
    """Sends and receives discovery datagrams on a test port."""

    def __init__(self, listen: bool = True) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.setblocking(False)
        self.received: list[dict[str, Any]] = []
        self.raw: list[bytes] = []

    def send(self, port: int, data: bytes) -> None:
        self.sock.sendto(data, ("127.0.0.1", port))

    def discover(self, port: int) -> None:
        self.send(port, json.dumps({"t": "LANVAS_DISCOVER", "protocol_version": 2}).encode())

    def pump(self) -> None:
        while True:
            try:
                data, _ = self.sock.recvfrom(65535)
            except BlockingIOError:
                return
            self.raw.append(data)
            self.received.append(json.loads(data))

    def wait_announce(self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 3.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            for msg in self.received:
                if predicate(msg):
                    return msg
            time.sleep(0.01)
        raise AssertionError(f"no matching announce in {self.received[-3:]}")

    def close(self) -> None:
        self.sock.close()


class ServerHandle:
    """Runs a real Server on a thread. kill() mimics a crash: no flush, no clean shutdown."""

    def __init__(self, data_dir: Path, clock: Callable[[], float] | None, **config: Any) -> None:
        self.data_dir = data_dir
        self.clock = clock
        self.config = config
        self.port = 0
        self.server: Any = None
        self.thread: threading.Thread | None = None
        self._boot()

    def _boot(self) -> None:
        from server.loop import Server

        kwargs = dict(self.config)
        if self.clock is not None:
            kwargs["clock"] = self.clock
        self.server = Server(self.data_dir, host="127.0.0.1", port=self.port, **kwargs)
        self.server.start()
        self.port = self.server.port
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()

    def kill(self) -> None:
        """Stop serving and drop every socket, without any shutdown flush."""
        self.server.request_stop(crash=True)
        assert self.thread is not None
        self.thread.join(5)

    def stop(self) -> None:
        self.server.request_stop()
        assert self.thread is not None
        self.thread.join(5)

    def restart(self) -> None:
        """Bring the server back on the same port. Running the integrity scan is part of start()."""
        self._boot()


def start_server(tmpdir: Path, clock: Callable[[], float] | None = None, **config: Any) -> tuple[int, ServerHandle]:
    config.setdefault("discovery", False)
    handle = ServerHandle(Path(tmpdir), clock, **config)
    return handle.port, handle


def add_member(owner: BotClient, port: int, name: str, role: str = "editor", key: Identity | None = None) -> BotClient:
    """Bring a new bot in through the real approval flow, so tests exercise the same path as users."""
    bot = BotClient(port, name, key)
    bot.request(room_id=owner.room_id)
    owner.wait_for(MsgType.ACCESS_REQUEST, lambda m: m["fingerprint"] == bot.fp)
    owner.send(MsgType.ACCESS_DECISION, {"fingerprint": bot.fp, "decision": "approve", "role": role})
    bot.wait_for(MsgType.WELCOME, consume=False)
    return bot


def make_room(port: int, name: str = "Room", **create: Any) -> BotClient:
    owner = BotClient(port, "owner")
    owner.join(create={"name": name, **create})
    return owner
