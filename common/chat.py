"""Chat schema, channel ids and validation shared by client and server (Architecture.md 4.5)."""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any

from common.identity import valid_fingerprint
from common.protocol import ErrorCode

CHANNEL_ROOM = "room"
CHANNEL_DM = "dm"
DM_PREFIX = "dm:"

MAX_TEXT = 2000
MAX_MSG_ID = 96
TAIL_SIZE = 50
MAX_HISTORY_PAGE = 100
PREVIEW_CHARS = 60

RETENTION_FOREVER = "forever"
RETENTION_LAST_N = "last_n"
RETENTION_DAYS = "days"


def dm_channel_id(fp_a: str, fp_b: str) -> str:
    """Stable id for a pair of members.

    Hashing the *sorted* pair gives both sides the same id without either being "first", and keeps
    fingerprints out of file names. The id is also safe to use as a file name, unlike client input.
    """
    joined = "|".join(sorted((fp_a, fp_b)))
    return DM_PREFIX + hashlib.sha256(joined.encode()).hexdigest()[:16]


def is_dm_channel(channel_id: object) -> bool:
    if not isinstance(channel_id, str) or not channel_id.startswith(DM_PREFIX):
        return False
    tail = channel_id[len(DM_PREFIX) :]
    return len(tail) == 16 and all(c in "0123456789abcdef" for c in tail)


def clean_text(text: str) -> str:
    """Strip control characters but keep newlines and tabs, which are legitimate in messages."""
    kept = [c for c in text if c in "\n\t" or unicodedata.category(c) != "Cc"]
    return "".join(kept).replace("\r\n", "\n").strip()


def validate_outgoing(msg: dict[str, Any]) -> tuple[str | None, str]:
    """Validate a client's CHAT frame. Returns (error_code or None, cleaned text)."""
    msg_id = msg.get("msg_id")
    if not isinstance(msg_id, str) or not 0 < len(msg_id) <= MAX_MSG_ID:
        return ErrorCode.BAD_SCHEMA, ""
    channel = msg.get("channel")
    if channel not in (CHANNEL_ROOM, CHANNEL_DM):
        return ErrorCode.UNKNOWN_CHANNEL, ""
    if channel == CHANNEL_DM and not valid_fingerprint(msg.get("to")):
        return ErrorCode.UNKNOWN_RECIPIENT, ""
    text = msg.get("text")
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        return ErrorCode.BAD_SCHEMA, ""
    cleaned = clean_text(text)
    if not cleaned:
        return ErrorCode.BAD_SCHEMA, ""
    reply_to = msg.get("reply_to")
    if reply_to is not None and (type(reply_to) is not int or reply_to < 0):
        return ErrorCode.BAD_SCHEMA, ""
    return None, cleaned


def validate_retention(policy: object) -> bool:
    if not isinstance(policy, dict):
        return False
    mode = policy.get("mode")
    if mode == RETENTION_FOREVER:
        return True
    value = policy.get("value")
    if mode in (RETENTION_LAST_N, RETENTION_DAYS):
        return type(value) is int and 0 < value <= 10_000_000
    return False
