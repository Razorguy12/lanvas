"""UDP discovery, server side (Architecture.md section 4.4).

Runs in the same selector thread as the TCP server, so reading the room list needs no locks.
Only what a visitor may know is ever announced: names, never codes, members, board or chat.
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Callable
from typing import Any

from common import netutil
from common.protocol import (
    ANNOUNCE,
    ANNOUNCE_INTERVAL,
    DISCOVER,
    MAX_ANNOUNCE_BYTES,
    MAX_DISCOVER_BYTES,
    PROTOCOL_VERSION,
)

log = logging.getLogger(__name__)

MAX_REPLIES_PER_SECOND = 5
MAX_TRACKED_SOURCES = 4096


class Discovery:
    def __init__(
        self,
        port: int,
        server_id: str,
        server_name: str,
        tcp_port: int,
        rooms: Callable[[], list[dict[str, Any]]],
        clock: Callable[[], float],
        extra_targets: list[tuple[str, int]] | None = None,
        broadcast: bool = True,
    ) -> None:
        self.port = port
        self.server_id = server_id
        self.server_name = server_name
        self.tcp_port = tcp_port
        self.rooms = rooms
        self.clock = clock
        self.extra_targets = extra_targets or []
        self.broadcast = broadcast
        self.sock = netutil.open_discovery_socket(port, "127.0.0.1" if not broadcast else "")
        self.port = self.sock.getsockname()[1]
        self.next_announce = 0.0
        self._reply_window: dict[str, tuple[int, int]] = {}

    def pages(self) -> list[bytes]:
        """Announce payloads, each under the size cap, split across pages when rooms overflow."""
        addrs = [i.address for i in netutil.list_interfaces()]
        base = {
            "t": ANNOUNCE,
            "server_id": self.server_id,
            "server_name": self.server_name,
            "addrs": addrs,
            "tcp_port": self.tcp_port,
            "protocol_version": PROTOCOL_VERSION,
        }
        chunks: list[list[dict[str, Any]]] = [[]]
        for room in self.rooms():
            trial = {**base, "rooms": [*chunks[-1], room], "page": 999, "pages": 999}
            if len(json.dumps(trial).encode()) > MAX_ANNOUNCE_BYTES and chunks[-1]:
                chunks.append([])
            chunks[-1].append(room)
        return [
            json.dumps({**base, "rooms": rooms, "page": i, "pages": len(chunks)}).encode()
            for i, rooms in enumerate(chunks)
        ]

    def _targets(self) -> list[tuple[str, int]]:
        targets = [(b, self.port) for b in netutil.broadcast_targets()] if self.broadcast else []
        return targets + self.extra_targets

    def announce(self) -> None:
        self.next_announce = self.clock() + ANNOUNCE_INTERVAL
        for page in self.pages():
            for target in self._targets():
                try:
                    self.sock.sendto(page, target)
                except OSError:
                    pass  # one unreachable interface must not stop the others

    def request_announce(self) -> None:
        """Announce on the next tick after a room is created, renamed, hidden or deleted."""
        self.next_announce = 0.0

    def tick(self) -> None:
        if self.clock() >= self.next_announce:
            self.announce()

    def _reply_allowed(self, ip: str) -> bool:
        """At most 5 replies per second per source, so we cannot be used as an amplifier."""
        second = int(self.clock())
        if len(self._reply_window) > MAX_TRACKED_SOURCES:
            self._reply_window.clear()
        window, count = self._reply_window.get(ip, (second, 0))
        if window != second:
            window, count = second, 0
        if count >= MAX_REPLIES_PER_SECOND:
            return False
        self._reply_window[ip] = (window, count + 1)
        return True

    def on_readable(self) -> None:
        for _ in range(64):  # bounded so a flood cannot starve TCP clients in this one thread
            try:
                data, source = self.sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            self._handle(data, source)

    def _handle(self, data: bytes, source: tuple[str, int]) -> None:
        if len(data) > MAX_DISCOVER_BYTES:
            return
        try:
            msg = json.loads(data)
        except (ValueError, RecursionError):
            return
        if not isinstance(msg, dict) or msg.get("t") != DISCOVER:
            return
        for page in self.pages():
            if not self._reply_allowed(source[0]):
                return
            try:
                self.sock.sendto(page, source)
            except OSError:
                return

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def default_server_name() -> str:
    return socket.gethostname()[:64] or "LANvas server"
