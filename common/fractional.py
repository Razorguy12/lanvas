"""Fractional indexing for z-order (Architecture.md section 5.4).

Each object's ``z`` is a string, and objects sort by it. Inserting between two neighbours produces
a new key without renumbering anyone, so a reorder is a single-property ``update`` op. That matters
for concurrency: renumbering would turn one drag into hundreds of ops that conflict with everyone.

Keys are base-62 fractions: "V" means 0.V. Keys never end in the lowest digit, which guarantees
there is always room to insert before any key.
"""

from __future__ import annotations

DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(DIGITS)
_VALUE = {c: i for i, c in enumerate(DIGITS)}

MAX_KEY_LEN = 128
# Repeated inserts in one gap make keys grow; past this the client should rebalance.
REBALANCE_LEN = 64


def valid_key(key: object) -> bool:
    return (
        isinstance(key, str)
        and 0 < len(key) <= MAX_KEY_LEN
        and all(c in _VALUE for c in key)
        and key[-1] != DIGITS[0]
    )


def between(lo: str | None, hi: str | None) -> str:
    """Return a key strictly between ``lo`` and ``hi``; ``None`` means an open end."""
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError(f"keys out of order: {lo!r} >= {hi!r}")
    lo_s = lo or ""
    hi_s = hi
    out: list[str] = []
    i = 0
    while True:
        lo_d = _VALUE[lo_s[i]] if i < len(lo_s) else 0
        # Once the prefix has diverged from hi, hi no longer limits us, so treat it as open.
        hi_d = _VALUE[hi_s[i]] if hi_s is not None and i < len(hi_s) else BASE
        if hi_d - lo_d > 1:
            out.append(DIGITS[(lo_d + hi_d) // 2])
            return "".join(out)
        out.append(DIGITS[lo_d])
        if hi_d - lo_d == 1:
            # The digits are adjacent: stay on lo's branch, where only lo bounds us from below.
            hi_s = None
        i += 1


def spaced_keys(count: int) -> list[str]:
    """Evenly spaced short keys for ``count`` objects, used to rebalance a long-keyed layer list."""
    if count <= 0:
        return []
    width = 1
    while BASE**width <= count + 1:
        width += 1
    step = BASE**width // (count + 1)
    keys: list[str] = []
    for n in range(1, count + 1):
        value = n * step
        chars: list[str] = []
        for _ in range(width):
            value, digit = divmod(value, BASE)
            chars.append(DIGITS[digit])
        key = "".join(reversed(chars)).rstrip(DIGITS[0])
        keys.append(key)
    return keys


def needs_rebalance(keys: list[str]) -> bool:
    return any(len(k) > REBALANCE_LEN for k in keys)
