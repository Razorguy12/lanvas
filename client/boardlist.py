"""The launcher's board list as plain logic: expiry, compatibility and diffing (no Qt, no
sockets)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from common.protocol import ANNOUNCE, BOARD_EXPIRY, MAX_ANNOUNCE_BYTES, PROTOCOL_VERSION

MAX_PER_SECOND_PER_SOURCE = 20


@dataclass(frozen=True)
class BoardEntry:
    """One row in the launcher. Frozen, so comparing two lists finds what actually changed."""

    server_id: str
    room_id: str
    name: str
    server_name: str
    host_name: str
    address: str
    tcp_port: int
    member_count: int | None
    require_code: bool
    compatible: bool
    protocol_version: int


@dataclass(frozen=True)
class ServerEntry:
    server_id: str
    server_name: str
    address: str
    tcp_port: int
    compatible: bool


def parse_announce(data: bytes) -> dict[str, Any] | None:
    """Validate an untrusted datagram. Anyone on the LAN can send anything to this port."""
    if len(data) > MAX_ANNOUNCE_BYTES:
        return None
    try:
        msg = json.loads(data)
    except (ValueError, RecursionError):
        return None
    if not isinstance(msg, dict) or msg.get("t") != ANNOUNCE:
        return None
    if not isinstance(msg.get("server_id"), str) or type(msg.get("tcp_port")) is not int:
        return None
    if type(msg.get("protocol_version")) is not int or not isinstance(msg.get("rooms"), list):
        return None
    for room in msg["rooms"]:
        if not isinstance(room, dict) or not isinstance(room.get("room_id"), str):
            return None
        if not isinstance(room.get("name"), str):
            return None
    return msg


class BoardTracker:
    """Board list keyed by (server_id, room_id); entries expire after 7 s without an announce."""

    def __init__(self, expiry: float = BOARD_EXPIRY) -> None:
        self.expiry = expiry
        self._boards: dict[tuple[str, str], tuple[BoardEntry, float]] = {}
        self._servers: dict[str, tuple[ServerEntry, float]] = {}
        self._rate: dict[str, tuple[int, int]] = {}
        self.log: list[dict[str, Any]] = []

    def _allowed(self, source_ip: str, now: float) -> bool:
        second = int(now)
        window, count = self._rate.get(source_ip, (second, 0))
        if window != second:
            window, count = second, 0
        self._rate[source_ip] = (window, count + 1)
        return count < MAX_PER_SECOND_PER_SOURCE

    def feed(self, data: bytes, source_ip: str, now: float) -> None:
        if not self._allowed(source_ip, now):
            return
        msg = parse_announce(data)
        if msg is None:
            return
        compatible = msg["protocol_version"] == PROTOCOL_VERSION
        server_id = msg["server_id"]
        server_name = str(msg.get("server_name", ""))[:64]
        self._servers[server_id] = (
            ServerEntry(server_id, server_name, source_ip, msg["tcp_port"], compatible),
            now,
        )
        self.log.append({"time": now, "source": source_ip, "rooms": len(msg["rooms"]), "msg": msg})
        del self.log[:-200]
        seen = set()
        for room in msg["rooms"]:
            count = room.get("member_count")
            entry = BoardEntry(
                server_id=server_id,
                room_id=room["room_id"],
                name=room["name"][:80],
                server_name=server_name,
                host_name=str(room.get("host_name", ""))[:64],
                # The address the packet came from is the one known to be reachable from here.
                address=source_ip,
                tcp_port=msg["tcp_port"],
                member_count=count if type(count) is int else None,
                require_code=room.get("require_code") is True,
                compatible=compatible,
                protocol_version=msg["protocol_version"],
            )
            self._boards[(server_id, room["room_id"])] = (entry, now)
            seen.add(room["room_id"])
        # A single-page announce is the complete list, so a room missing from it is gone at once.
        if msg.get("pages", 1) == 1:
            for key in [k for k in self._boards if k[0] == server_id and k[1] not in seen]:
                del self._boards[key]

    def expire(self, now: float) -> None:
        for key, (_, seen) in list(self._boards.items()):
            if now - seen > self.expiry:
                del self._boards[key]
        for key, (_, seen) in list(self._servers.items()):
            if now - seen > self.expiry:
                del self._servers[key]

    def boards(self) -> list[BoardEntry]:
        rows = [entry for entry, _ in self._boards.values()]
        return sorted(rows, key=lambda e: (not e.compatible, e.name.lower(), e.room_id))

    def servers(self) -> list[ServerEntry]:
        rows = [entry for entry, _ in self._servers.values()]
        return sorted(rows, key=lambda e: e.server_name.lower())
