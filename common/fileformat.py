"""Canonical JSON, board hash, snapshots, and the signed/encrypted ``.lanvas`` file format.

Shared by client and server so both produce byte-identical canonical JSON. That property is what
makes the board hash a meaningful convergence check (Architecture.md section 14.4).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import zlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from common.identity import Identity, b64, fingerprint_of, unb64, verify
from common.model import Board

FILE_FORMAT = "lanvas"
FILE_FORMAT_ENC = "lanvas-enc"
SNAPSHOT_FORMAT = "lanvas-snapshot"
FILE_VERSION = 2
HASH_PREFIX = "sha256:"

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
NONCE_BYTES = 12
MAX_FILE_INFLATED = 256 * 1024 * 1024


def canonical_json(obj: Any) -> bytes:
    """One byte sequence per logical value: sorted keys, no spaces, UTF-8, no NaN."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def board_hash(board: Board | dict[str, Any]) -> str:
    """The convergence check. The same function produces live, checkpoint and file hashes."""
    data = board.to_json() if isinstance(board, Board) else board
    return HASH_PREFIX + hashlib.sha256(canonical_json(data)).hexdigest()


class FileError(Exception):
    """A file could not be read at all. The message is safe to show to the user."""


class PasswordRequired(FileError):
    """The file is encrypted and no password was supplied."""


class WrongPassword(FileError):
    """Wrong password, or the ciphertext was tampered with. GCM cannot tell these apart."""


class Trust(Enum):
    SIGNED = "signed"
    MODIFIED = "modified"
    UNSIGNED = "unsigned"


@dataclass
class LanvasFile:
    room_name: str
    seq: int
    saved_at: str
    board: dict[str, Any]
    board_hash: str
    trust: Trust
    saved_by: dict[str, str] = field(default_factory=dict)
    room_chat: list[dict[str, Any]] | None = None


def _inflate_bounded(raw: bytes) -> bytes:
    # gzip framing, with a cap so a crafted file cannot exhaust memory when opened.
    inflater = zlib.decompressobj(wbits=31)
    try:
        out = inflater.decompress(raw, MAX_FILE_INFLATED)
    except zlib.error as exc:
        raise FileError("This file is damaged and could not be read.") from exc
    if inflater.unconsumed_tail or not inflater.eof:
        raise FileError("This file is damaged or too large to open.")
    return out


def build_document(
    board: Board | dict[str, Any],
    room_name: str,
    seq: int,
    saved_at: str,
    identity: Identity,
    name: str,
    room_chat: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    board_json = board.to_json() if isinstance(board, Board) else board
    doc: dict[str, Any] = {
        "format": FILE_FORMAT,
        "version": FILE_VERSION,
        "room_name": room_name,
        "seq": seq,
        "saved_at": saved_at,
        "saved_by": {
            "name": name,
            "fingerprint": identity.fingerprint,
            "pubkey": b64(identity.pubkey),
        },
        "board": board_json,
        "board_hash": board_hash(board_json),
    }
    if room_chat is not None:
        # Only the room channel can ever get here: this function has no parameter for DMs.
        doc["room_chat"] = room_chat
    doc["signature"] = b64(identity.sign(canonical_json(doc)))
    return doc


def _encrypt(payload: bytes, password: str) -> bytes:
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    key = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(password.encode())
    wrapper = {
        "format": FILE_FORMAT_ENC,
        "version": FILE_VERSION,
        "kdf": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": b64(salt),
        "nonce": b64(nonce),
        "ciphertext": b64(AESGCM(key).encrypt(nonce, payload, None)),
    }
    return canonical_json(wrapper)


def encode_lanvas(doc: dict[str, Any], password: str | None = None) -> bytes:
    # mtime=0 makes the gzip output reproducible for identical documents.
    payload = gzip.compress(canonical_json(doc), mtime=0)
    return _encrypt(payload, password) if password else payload


def _decrypt(raw: bytes, password: str | None) -> bytes:
    if password is None:
        raise PasswordRequired("This file is protected with a password.")
    try:
        wrap = json.loads(raw)
        if wrap["format"] != FILE_FORMAT_ENC:
            raise ValueError("not an encrypted LANvas file")
        salt, nonce, ciphertext = (unb64(wrap[k]) for k in ("salt", "nonce", "ciphertext"))
        n, r, p = int(wrap["n"]), int(wrap["r"]), int(wrap["p"])
    except (ValueError, KeyError, TypeError) as exc:
        raise FileError("This file is damaged and could not be read.") from exc
    if not salt or not nonce or not ciphertext:
        raise FileError("This file is damaged and could not be read.")
    if n != SCRYPT_N or r != SCRYPT_R or p != SCRYPT_P:
        # KDF cost comes from an untrusted file, so a huge n would be a memory bomb. Refuse it.
        raise FileError("This file uses unsupported protection settings.")
    key = Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode())
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except (InvalidTag, ValueError) as exc:
        raise WrongPassword("Wrong password, or the file was modified.") from exc


def _is_encrypted(raw: bytes) -> bool:
    # Plain files are gzip (magic 1F 8B); only the encrypted wrapper is bare JSON. The wrapper's own
    # "format" field is checked after parsing, because canonical key order puts the long ciphertext
    # first, so a prefix search for the marker would not find it.
    return raw[:1] == b"{"


def _assess_trust(doc: dict[str, Any]) -> tuple[Trust, str]:
    """Check the hash first, then the signature, and report rather than raise.

    Tampered files may still be opened (read-only until confirmed), so this returns a verdict.
    """
    computed = board_hash(doc.get("board", {}))
    if doc.get("board_hash") != computed:
        return Trust.MODIFIED, computed
    signature = unb64(doc.get("signature"))
    saved_by = doc.get("saved_by") if isinstance(doc.get("saved_by"), dict) else {}
    pubkey = unb64(saved_by.get("pubkey"))
    if signature is None or pubkey is None:
        return Trust.UNSIGNED, computed
    # The claimed fingerprint must match the key, or anyone could sign as "Alice".
    if saved_by.get("fingerprint") != fingerprint_of(pubkey):
        return Trust.MODIFIED, computed
    unsigned = {k: v for k, v in doc.items() if k != "signature"}
    if not verify(pubkey, signature, canonical_json(unsigned)):
        return Trust.MODIFIED, computed
    return Trust.SIGNED, computed


def decode_lanvas(raw: bytes, password: str | None = None) -> LanvasFile:
    if _is_encrypted(raw):
        raw = _decrypt(raw, password)
    payload = _inflate_bounded(raw)
    try:
        doc = json.loads(payload.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise FileError("This file is damaged and could not be read.") from exc
    if not isinstance(doc, dict) or doc.get("format") != FILE_FORMAT:
        raise FileError("This is not a LANvas file.")
    if doc.get("version") != FILE_VERSION:
        raise FileError(f"Unsupported LANvas file version: {doc.get('version')}.")
    board = doc.get("board")
    if not isinstance(board, dict) or not isinstance(board.get("objects", {}), dict):
        raise FileError("This file is damaged and could not be read.")
    trust, computed = _assess_trust(doc)
    saved_by = doc.get("saved_by") if isinstance(doc.get("saved_by"), dict) else {}
    chat = doc.get("room_chat")
    return LanvasFile(
        room_name=str(doc.get("room_name", "Untitled")),
        seq=int(doc.get("seq", 0)) if isinstance(doc.get("seq", 0), int) else 0,
        saved_at=str(doc.get("saved_at", "")),
        board=board,
        board_hash=computed,
        trust=trust,
        saved_by={k: str(v) for k, v in saved_by.items()},
        room_chat=chat if isinstance(chat, list) else None,
    )


def encode_snapshot(board: Board, room_id: str, seq: int, created_at: str) -> bytes:
    board_json = board.to_json()
    return gzip.compress(
        canonical_json(
            {
                "format": SNAPSHOT_FORMAT,
                "version": FILE_VERSION,
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
    """Return a verified snapshot, or None. Unverified board data is never used (section 12.1)."""
    try:
        doc = json.loads(_inflate_bounded(raw).decode("utf-8"))
    except (FileError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or doc.get("format") != SNAPSHOT_FORMAT:
        return None
    if not isinstance(doc.get("board"), dict) or not isinstance(doc.get("seq"), int):
        return None
    if doc.get("board_hash") != board_hash(doc["board"]):
        return None
    return doc
