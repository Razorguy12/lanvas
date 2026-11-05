"""Native save: signed, optionally encrypted. A thin wrapper so the UI has one export call per
format."""

from __future__ import annotations

from typing import Any

from common.fileformat import encode_lanvas


def export_lanvas(document: dict[str, Any], password: str | None = None) -> bytes:
    # scrypt is deliberately slow, so this always runs on the IO worker when a password is set.
    return encode_lanvas(document, password)
