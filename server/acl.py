"""Access control: roles, the permission matrix, requests, invites and rate limiters.

This module is the single answer to "may this identity do that?". The loop calls :func:`can` on
every frame, because the UI's greyed-out buttons protect nothing against a raw socket client.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.persist import atomic_write_json

ROLE_OWNER = "owner"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"
MEMBER_ROLES = (ROLE_OWNER, ROLE_EDITOR, ROLE_VIEWER)
ASSIGNABLE_ROLES = (ROLE_EDITOR, ROLE_VIEWER)

# Actions, one per row of the permission matrix in Architecture.md section 6.3.
A_EDIT = "edit"
A_CHAT = "chat"
A_HISTORY = "history"
A_MANAGE_CHAT = "manage_chat"
A_SAVE = "save"
A_CLEAR = "clear"
A_RESTORE = "restore"
A_ADMIN = "admin"

_MATRIX: dict[str, frozenset[str]] = {
    ROLE_OWNER: frozenset(
        {A_EDIT, A_CHAT, A_HISTORY, A_MANAGE_CHAT, A_SAVE, A_CLEAR, A_RESTORE, A_ADMIN}
    ),
    ROLE_EDITOR: frozenset({A_EDIT, A_CHAT, A_HISTORY, A_SAVE}),
    ROLE_VIEWER: frozenset({A_CHAT, A_HISTORY}),
}

STATUS_PENDING = "pending"
STATUS_DENIED = "denied"
STATUS_BLOCKED = "blocked"

DENY_COOLDOWN = 60.0

DEFAULT_SETTINGS: dict[str, Any] = {
    "editors_can_clear": False,
    "viewers_can_export": False,
    "require_code": False,
    "show_member_count": True,
    "discoverable": True,
    "locked": False,
}
BOOL_SETTINGS = (
    "editors_can_clear",
    "viewers_can_export",
    "require_code",
    "show_member_count",
    "discoverable",
    "locked",
)


def can(role: str | None, action: str, settings: dict[str, Any]) -> bool:
    """Deny by default: a role that is unknown or None has no rights at all."""
    if role not in _MATRIX:
        return False
    if action == A_CLEAR and role == ROLE_EDITOR and settings.get("editors_can_clear"):
        return True
    if action == A_EDIT and settings.get("locked") and role != ROLE_OWNER:
        return False
    return action in _MATRIX[role]


class Acl:
    """Persisted room ACL. Every mutation is saved atomically before the caller acts on it."""

    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        self.path = path
        self.clock = clock
        self.owner = ""
        self.members: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.invites: dict[str, dict[str, Any]] = {}
        self.settings: dict[str, Any] = dict(DEFAULT_SETTINGS)

    def load(self, data: dict[str, Any]) -> None:
        self.owner = data["owner"]
        self.members = data.get("members", {})
        self.requests = data.get("requests", {})
        self.invites = data.get("invites", {})
        saved_settings = data.get("settings", {})
        self.settings = {
            **DEFAULT_SETTINGS,
            **{key: value for key, value in saved_settings.items() if key in DEFAULT_SETTINGS},
        }

    def save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "owner": self.owner,
                "members": self.members,
                "requests": self.requests,
                "invites": self.invites,
                "settings": self.settings,
            },
            keep_bak=True,
        )

    def set_owner(self, fingerprint: str, name: str) -> None:
        self.owner = fingerprint
        self.add_member(fingerprint, name, ROLE_OWNER)

    def add_member(self, fingerprint: str, name: str, role: str) -> None:
        self.members[fingerprint] = {
            "role": role,
            "name": name,
            "added_at": self.clock(),
            "muted": False,
        }
        self.requests.pop(fingerprint, None)
        self.save()

    def role_of(self, fingerprint: str) -> str | None:
        member = self.members.get(fingerprint)
        return member["role"] if member else None

    def is_muted(self, fingerprint: str) -> bool:
        member = self.members.get(fingerprint)
        return bool(member and member.get("muted"))

    def remember_name(self, fingerprint: str, name: str) -> None:
        member = self.members.get(fingerprint)
        if member and member["name"] != name:
            member["name"] = name
            self.save()

    # --- requests ---
    def record_request(self, fingerprint: str, name: str, ip: str) -> dict[str, Any]:
        request = {"name": name, "ip": ip, "time": self.clock(), "status": STATUS_PENDING}
        self.requests[fingerprint] = request
        self.save()
        return request

    def request_status(self, fingerprint: str) -> str | None:
        request = self.requests.get(fingerprint)
        return request["status"] if request else None

    def cooldown_left(self, fingerprint: str) -> float:
        """Seconds until a denied identity may ask again; infinite when blocked."""
        request = self.requests.get(fingerprint)
        if not request:
            return 0.0
        if request["status"] == STATUS_BLOCKED:
            return float("inf")
        if request["status"] != STATUS_DENIED:
            return 0.0
        return max(0.0, request["time"] + DENY_COOLDOWN - self.clock())

    def deny(self, fingerprint: str, block: bool = False) -> None:
        request = self.requests.setdefault(
            fingerprint, {"name": "", "ip": "", "time": self.clock(), "status": STATUS_DENIED}
        )
        request["status"] = STATUS_BLOCKED if block else STATUS_DENIED
        request["time"] = self.clock()
        self.save()

    def pending_requests(self) -> list[dict[str, Any]]:
        return [
            {"fingerprint": fp, "name": r["name"], "ip": r["ip"], "time": r["time"]}
            for fp, r in self.requests.items()
            if r["status"] == STATUS_PENDING
        ]

    # --- invites ---
    def create_invite(self, role: str, ttl: float, single_use: bool = True) -> str:
        token = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=12))
        self.invites[token] = {
            "role": role,
            "expires": self.clock() + ttl,
            "single_use": single_use,
        }
        self.save()
        return token

    def consume_invite(self, token: str) -> str | None:
        """Return the invite's role if valid. A single-use invite is burned before it is honored."""
        invite = self.invites.get(token)
        if invite is None:
            return None
        if invite["expires"] < self.clock():
            del self.invites[token]
            self.save()
            return None
        if invite["single_use"]:
            del self.invites[token]
            self.save()
        return invite["role"]


class TokenBucket:
    """Per-connection rate limiter: smooth rate plus a small burst allowance."""

    def __init__(self, rate: float, burst: float, clock: Callable[[], float]) -> None:
        self.rate = rate
        self.burst = burst
        self.clock = clock
        self.tokens = burst
        self.last = clock()

    def allow(self, cost: float = 1.0) -> bool:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


class IpRequestLimiter:
    """Per-IP cap on *new access requests*, with a cool-down once the cap is hit (AC-10)."""

    def __init__(
        self,
        clock: Callable[[], float],
        limit: int = 10,
        window: float = 60.0,
        cooldown: float = 60,
    ) -> None:
        self.clock = clock
        self.limit = limit
        self.window = window
        self.cooldown = cooldown
        self._hits: dict[str, deque[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def allow(self, ip: str) -> bool:
        now = self.clock()
        if self._blocked_until.get(ip, 0.0) > now:
            return False
        hits = self._hits.setdefault(ip, deque())
        while hits and hits[0] < now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            self._blocked_until[ip] = now + self.cooldown
            hits.clear()
            return False
        hits.append(now)
        return True
