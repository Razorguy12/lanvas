"""Client networking without UI code: socket thread, handshake, heartbeat, reconnect, inspector,
chaos.

``client/net.py`` wraps this and hands its events to the Tk thread. Keeping it UI-free lets the
same code run in headless tests against a real server.

Threading: one background thread owns the socket. :meth:`NetCore.send` may be called from any
thread;
it queues the frame and wakes the loop through a socketpair, so the UI thread never touches a
socket.
"""

from __future__ import annotations

import heapq
import json
import logging
import random
import selectors
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from common.identity import Identity, b64, unb64
from common.protocol import (
    MAX_SERVER_FRAME,
    PING_INTERVAL,
    PROTOCOL_VERSION,
    ErrorCode,
    FrameDecoder,
    MsgType,
    ProtocolError,
    encode_frame,
)

log = logging.getLogger(__name__)

STATE_CONNECTING = "connecting"
STATE_LIVE = "live"
STATE_RECONNECTING = "reconnecting"
STATE_PENDING = "pending"
STATE_CLOSED = "closed"

BACKOFF_START = 0.5
BACKOFF_MAX = 8.0
DEAD_AFTER = 15.0
CONNECT_TIMEOUT = 5.0
TRANSIENT_DROP_QUEUE = 64
RING_SIZE = 5000
PAYLOAD_PREVIEW = 8192

# After these, reconnecting cannot help: the server has made a decision about this identity.
FATAL_CODES = frozenset(
    {
        ErrorCode.ACCESS_DENIED,
        ErrorCode.KICKED,
        ErrorCode.REVOKED,
        ErrorCode.ROOM_NOT_FOUND,
        ErrorCode.CODE_REQUIRED,
        ErrorCode.AUTH_FAILED,
        ErrorCode.BAD_VERSION,
        ErrorCode.RATE_LIMITED,
    }
)
_REORDERABLE = (MsgType.OP, MsgType.CHAT)


def scrub_private(msg: dict[str, Any], my_fp: str) -> dict[str, Any]:
    """DM text is shown only to its two participants (Architecture.md section 11)."""
    if "to_fp" not in msg and not str(msg.get("channel_id", "")).startswith("dm:"):
        return msg
    if my_fp in (msg.get("from_fp"), msg.get("to_fp")):
        return msg
    text = msg.get("text", "")
    return {**msg, "text": f"«private, {len(text)} chars»"}


@dataclass
class FrameRecord:
    time: float
    direction: str  # "in" or "out"
    type_name: str
    seq: int | None
    size: int
    rtt: float | None
    preview: str


class Inspector:
    """Ring buffer behind the Packet Inspector. Bounded, so a long session cannot grow memory."""

    def __init__(self, my_fp: str) -> None:
        self.my_fp = my_fp
        self.records: deque[FrameRecord] = deque(maxlen=RING_SIZE)
        self.total = 0
        self.rtt: float | None = None
        self.lock = threading.Lock()

    def record(self, direction: str, msg_type: int, msg: dict[str, Any], size: int) -> None:
        seq = msg.get("seq", msg.get("chat_seq"))
        try:
            name = MsgType(msg_type).name
        except ValueError:
            name = str(msg_type)
        preview = json.dumps(scrub_private(msg, self.my_fp), ensure_ascii=False)[:PAYLOAD_PREVIEW]
        rec = FrameRecord(time.time(), direction, name, seq, size, self.rtt, preview)
        with self.lock:
            self.records.append(rec)
            self.total += 1

    def snapshot(self) -> list[FrameRecord]:
        with self.lock:
            return list(self.records)


@dataclass
class Chaos:
    """The network simulator. Debug only; it exists to show ordering and recovery holding up."""

    latency: float = 0.0  # seconds added to delivery
    duplicate: float = 0.0  # probability of duplicating a frame
    reorder_next: int = 0  # hold this many sequenced frames, then deliver them reversed
    drop_requested: bool = False
    rng: random.Random = field(default_factory=random.Random)

    @property
    def active(self) -> bool:
        return bool(self.latency or self.duplicate or self.reorder_next)


class NetCore:
    """One logical connection to a room, surviving any number of TCP reconnects."""

    def __init__(
        self,
        host: str,
        port: int,
        identity: Identity,
        username: str,
        on_event: Callable[[str, Any], None],
        hello_fields: Callable[[], dict[str, Any]],
    ) -> None:
        self.host = host
        self.port = port
        self.identity = identity
        self.username = username
        self.on_event = on_event
        self.hello_fields = hello_fields
        self.inspector = Inspector(identity.fingerprint)
        self.chaos = Chaos()
        self.state = STATE_CONNECTING
        self._queue: deque[tuple[int, bytes, dict[str, Any]]] = deque()
        self._lock = threading.Lock()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lanvas-net", daemon=True)
        self._delayed: list[tuple[float, int, int, dict[str, Any]]] = []
        self._delay_counter = 0
        self._held: list[tuple[int, dict[str, Any]]] = []
        self._ping_sent: dict[float, float] = {}

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(3)

    def request_drop(self) -> None:
        self.chaos.drop_requested = True
        self._wake()

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass  # buffer full means a wake-up is already pending

    def send(self, msg_type: int, payload: dict[str, Any]) -> None:
        """Queue a frame. While offline it is dropped; the session layer re-sends after WELCOME."""
        if self.state != STATE_LIVE and msg_type != MsgType.PING:
            return
        with self._lock:
            # Cursors are the first thing to go when the link is backed up (Architecture.md 9).
            if msg_type == MsgType.TRANSIENT and len(self._queue) > TRANSIENT_DROP_QUEUE:
                return
            self._queue.append((msg_type, encode_frame(msg_type, payload), payload))
        self._wake()

    # ------------------------------------------------------------------ thread body
    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.on_event("state", state)

    def _run(self) -> None:
        backoff = BACKOFF_START
        first = True
        while not self._stop.is_set():
            self._set_state(STATE_CONNECTING if first else STATE_RECONNECTING)
            outcome = self._session()
            if outcome == "fatal" or self._stop.is_set():
                break
            if outcome == "was_live":
                backoff = BACKOFF_START  # the link worked, so retry promptly
            first = False
            self._stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
        self._set_state(STATE_CLOSED)

    def _session(self) -> str:
        """Run one TCP connection. Returns "retry", "was_live", or "fatal"."""
        try:
            sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        except OSError as exc:
            log.info("connect failed: %s", exc)
            return "retry"
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setblocking(False)
        was_live = False
        selector = selectors.DefaultSelector()
        outbox = bytearray()
        decoder = FrameDecoder(MAX_SERVER_FRAME)
        with self._lock:
            self._queue.clear()  # anything queued belonged to the previous connection
        self._held.clear()
        try:
            selector.register(sock, selectors.EVENT_READ)
            selector.register(self._wake_r, selectors.EVENT_READ)
            self._drain_wake()
            hello = {
                "username": self.username,
                "pubkey": b64(self.identity.pubkey),
                "protocol_version": PROTOCOL_VERSION,
                **self.hello_fields(),
            }
            outbox += encode_frame(MsgType.HELLO, hello)
            last_recv = time.monotonic()
            next_ping = last_recv + PING_INTERVAL
            while not self._stop.is_set():
                self._pull_queue(outbox)
                self._set_write_interest(selector, sock, bool(outbox))
                events = selector.select(self._select_timeout(next_ping))
                for key, mask in events:
                    if key.fileobj is self._wake_r:
                        self._drain_wake()
                        continue
                    if mask & selectors.EVENT_WRITE and outbox:
                        sent = sock.send(outbox[:262144])
                        del outbox[:sent]
                    if mask & selectors.EVENT_READ:
                        data = sock.recv(1 << 18)
                        if not data:
                            return "was_live" if was_live else "retry"
                        last_recv = time.monotonic()
                        for msg_type, msg in decoder.feed(data):
                            size = len(data)
                            self.inspector.record("in", msg_type, msg, size)
                            verdict = self._on_frame(msg_type, msg, outbox)
                            if verdict == "live":
                                was_live = True
                            elif verdict == "fatal":
                                return "fatal"
                self._release_delayed()
                now = time.monotonic()
                if self.chaos.drop_requested:
                    self.chaos.drop_requested = False
                    return "was_live" if was_live else "retry"
                if now - last_recv > DEAD_AFTER:
                    log.info("no traffic for %ss, reconnecting", DEAD_AFTER)
                    return "was_live" if was_live else "retry"
                if now >= next_ping:
                    next_ping = now + PING_INTERVAL
                    outbox += encode_frame(MsgType.PING, {"t": now})
            return "fatal"
        except (BlockingIOError, InterruptedError):
            return "retry"
        except (OSError, ProtocolError) as exc:
            log.info("connection lost: %s", exc)
            return "was_live" if was_live else "retry"
        finally:
            selector.close()
            try:
                sock.close()
            except OSError:
                pass
            if self.state == STATE_LIVE:
                self.state = STATE_RECONNECTING
                self.on_event("state", STATE_RECONNECTING)

    def _select_timeout(self, next_ping: float) -> float:
        timeout = max(0.0, next_ping - time.monotonic())
        if self._delayed:
            timeout = min(timeout, max(0.0, self._delayed[0][0] - time.monotonic()))
        return min(timeout, 0.25)

    def _drain_wake(self) -> None:
        try:
            while self._wake_r.recv(4096):
                pass
        except (BlockingIOError, InterruptedError, OSError):
            pass

    def _set_write_interest(self, selector: Any, sock: socket.socket, want: bool) -> None:
        mask = selectors.EVENT_READ | (selectors.EVENT_WRITE if want else 0)
        selector.modify(sock, mask)

    def _pull_queue(self, outbox: bytearray) -> None:
        with self._lock:
            while self._queue:
                msg_type, frame, payload = self._queue.popleft()
                self.inspector.record("out", msg_type, payload, len(frame))
                outbox += frame
                # Duplicating outgoing frames exercises the server's op_id and msg_id dedupe.
                if (
                    msg_type in (MsgType.OP, MsgType.CHAT)
                    and self.chaos.duplicate
                    and self.chaos.rng.random() < self.chaos.duplicate
                ):
                    outbox += frame

    def _on_frame(self, msg_type: int, msg: dict[str, Any], outbox: bytearray) -> str:
        if msg_type == MsgType.CHALLENGE:
            nonce = unb64(msg.get("nonce"))
            if nonce is None:
                return "fatal"
            proof = {"signature": b64(self.identity.sign_proof(nonce))}
            outbox += encode_frame(MsgType.PROOF, proof)
            return ""
        if msg_type == MsgType.PONG:
            sent = msg.get("t")
            if isinstance(sent, float):
                self.inspector.rtt = time.monotonic() - sent
                self.on_event("rtt", self.inspector.rtt)
            return ""
        if msg_type == MsgType.ACCESS_PENDING:
            self._set_state(STATE_PENDING)
        elif msg_type == MsgType.WELCOME:
            self._set_state(STATE_LIVE)
            self._deliver(msg_type, msg, chaos=False)
            return "live"
        elif msg_type == MsgType.ERROR and msg.get("code") in FATAL_CODES:
            self._deliver(msg_type, msg, chaos=False)
            return "fatal"
        self._deliver(msg_type, msg)
        return ""

    def _deliver(self, msg_type: int, msg: dict[str, Any], chaos: bool = True) -> None:
        if not chaos or not self.chaos.active:
            self.on_event("frame", (msg_type, msg))
            return
        copies = 1
        if self.chaos.duplicate and self.chaos.rng.random() < self.chaos.duplicate:
            copies = 2
        for _ in range(copies):
            if msg_type in _REORDERABLE and self.chaos.reorder_next > 0:
                self._held.append((msg_type, msg))
                self.chaos.reorder_next -= 1
                if self.chaos.reorder_next == 0:
                    for held_type, held in reversed(self._held):
                        self._schedule(held_type, held)
                    self._held.clear()
            else:
                self._schedule(msg_type, msg)

    def _schedule(self, msg_type: int, msg: dict[str, Any]) -> None:
        if not self.chaos.latency:
            self.on_event("frame", (msg_type, msg))
            return
        self._delay_counter += 1
        due = time.monotonic() + self.chaos.latency
        # The counter breaks ties, so two frames due at once never compare their dict payloads.
        heapq.heappush(self._delayed, (due, self._delay_counter, msg_type, msg))

    def _release_delayed(self) -> None:
        now = time.monotonic()
        while self._delayed and self._delayed[0][0] <= now:
            _, _, msg_type, msg = heapq.heappop(self._delayed)
            self.on_event("frame", (msg_type, msg))
