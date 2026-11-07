"""User identity: an Ed25519 keypair whose public-key fingerprint is the stable user ID.

Usernames are display-only and may collide, so the ACL, DM routing and file signatures all key on
the fingerprint. The server always derives the fingerprint from the public key it verified a
signature against; a client-supplied fingerprint is never believed.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from common import durable

# The proof signs this prefix plus the nonce, not the bare nonce. Otherwise a rogue server could
# Domain separation prevents a proof signature from being reused for another signed payload.
PROOF_DOMAIN = b"LANVAS-PROOF-v2:"
NONCE_BYTES = 32
FINGERPRINT_HEX = 32


def fingerprint_of(pubkey: bytes) -> str:
    """128-bit SHA-256 prefix grouped for reading aloud, e.g. ``7F3A-91C2-...``."""
    digest = hashlib.sha256(pubkey).hexdigest().upper()[:FINGERPRINT_HEX]
    return "-".join(digest[i : i + 4] for i in range(0, FINGERPRINT_HEX, 4))


def short_fingerprint(fingerprint: str) -> str:
    """The short code the UI shows for verbal verification, e.g. ``7F3A-91C2``."""
    return fingerprint[:9]


def valid_fingerprint(value: object) -> bool:
    if not isinstance(value, str) or len(value) != FINGERPRINT_HEX + FINGERPRINT_HEX // 4 - 1:
        return False
    return all(c in "0123456789ABCDEF-" for c in value)


def verify(pubkey: bytes, signature: bytes, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(pubkey).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def unb64(text: object) -> bytes | None:
    """Decode strict base64 from untrusted input; returns None instead of raising."""
    if not isinstance(text, str):
        return None
    try:
        return base64.b64decode(text, validate=True)
    except ValueError:
        return None


class Identity:
    """A local keypair. The private half never leaves this object or the key file."""

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key
        self.pubkey = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.fingerprint = fingerprint_of(self.pubkey)

    @classmethod
    def generate(cls) -> Identity:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path: Path) -> Identity:
        """Load the key from the user's config dir, creating it on first launch (0600)."""
        if path.exists():
            raw = path.read_bytes()
            return cls(Ed25519PrivateKey.from_private_bytes(raw))
        identity = cls.generate()
        raw = identity._key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        durable.atomic_write(path, raw, mode=0o600 if os.name != "nt" else None)
        return identity

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)

    def sign_proof(self, nonce: bytes) -> bytes:
        return self.sign(PROOF_DOMAIN + nonce)


def verify_proof(pubkey: bytes, signature: bytes, nonce: bytes) -> bool:
    return verify(pubkey, signature, PROOF_DOMAIN + nonce)
