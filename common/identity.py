"""Simple persistent client IDs used to associate members with ACL entries.

This is a proof-of-concept identity only. The server accepts the ID sent in HELLO, so it does not
prove who owns that ID and must not be treated as secure authentication.
"""

from __future__ import annotations

import random
from pathlib import Path

from common.durable import atomic_write

ID_HEX = 32
FINGERPRINT_LENGTH = ID_HEX + ID_HEX // 4 - 1


def _format_id(value: str) -> str:
    value = value.upper()
    return "-".join(value[i : i + 4] for i in range(0, ID_HEX, 4))


def valid_fingerprint(value: object) -> bool:
    if not isinstance(value, str) or len(value) != FINGERPRINT_LENGTH:
        return False
    groups = value.split("-")
    return len(groups) == 8 and all(
        len(group) == 4 and all(char in "0123456789ABCDEF" for char in group)
        for group in groups
    )


def short_fingerprint(fingerprint: str) -> str:
    """Short form shown in the UI for comparing member IDs."""
    return fingerprint[:9]


class Identity:
    """A stable random ID saved locally; it contains no keys or cryptographic proof."""

    def __init__(self, fingerprint: str) -> None:
        if not valid_fingerprint(fingerprint):
            raise ValueError("invalid client ID")
        self.fingerprint = fingerprint

    @classmethod
    def generate(cls) -> Identity:
        value = f"{random.getrandbits(ID_HEX * 4):0{ID_HEX}X}"
        return cls(_format_id(value))

    @classmethod
    def load_or_create(cls, path: Path) -> Identity:
        """Load the saved ID, or replace an old key file with a new plain-text ID."""
        if path.exists():
            try:
                return cls(path.read_text(encoding="ascii").strip())
            except (OSError, UnicodeError, ValueError):
                pass
        identity = cls.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, identity.fingerprint.encode("ascii"))
        return identity
