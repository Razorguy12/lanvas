"""Canonical board hashing and verified server/client recovery snapshots."""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from typing import Any

from common.model import Board

SNAPSHOT_FORMAT = "lanvas-snapshot"
SNAPSHOT_VERSION = 2
HASH_PREFIX = "sha256:"
MAX_SNAPSHOT_INFLATED = 256 * 1024 * 1024


def canonical_json(obj: Any) -> bytes:
    """One byte sequence per logical value: sorted keys, no spaces, UTF-8, no NaN."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def board_hash(board: Board | dict[str, Any]) -> str:
    """Hash canonical board data for convergence and checkpoint verification."""
    data = board.to_json() if isinstance(board, Board) else board
    return HASH_PREFIX + hashlib.sha256(canonical_json(data)).hexdigest()


def _inflate_bounded(raw: bytes) -> bytes:
    inflater = zlib.decompressobj(wbits=31)
    try:
        out = inflater.decompress(raw, MAX_SNAPSHOT_INFLATED)
    except zlib.error:
        return b""
    if inflater.unconsumed_tail or not inflater.eof:
        return b""
    return out


def encode_snapshot(board: Board, room_id: str, seq: int, created_at: str) -> bytes:
    board_json = board.to_json()
    return gzip.compress(
        canonical_json(
            {
                "format": SNAPSHOT_FORMAT,
                "version": SNAPSHOT_VERSION,
                "room_id": room_id,
                "seq": seq,
                "created_at": created_at,
                "board": board_json,
                "board_hash": board_hash(board_json),
            }
        ),
        mtime=0,
    )


def decode_snapshot(raw: bytes) -> dict[str, Any] | None:
    """Return a verified snapshot, or None. Unverified board data is never used."""
    try:
        doc = json.loads(_inflate_bounded(raw).decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or doc.get("format") != SNAPSHOT_FORMAT:
        return None
    if doc.get("version") != SNAPSHOT_VERSION:
        return None
    if not isinstance(doc.get("board"), dict) or not isinstance(doc.get("seq"), int):
        return None
    if doc.get("board_hash") != board_hash(doc["board"]):
        return None
    return doc
