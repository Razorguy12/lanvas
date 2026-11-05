"""L0 for the board model, fractional index and identity; includes OR-04, OR-05, OR-06, SE-04."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from common import fractional as fr
from common.fileformat import board_hash
from common.identity import (
    Identity,
    fingerprint_of,
    short_fingerprint,
    valid_fingerprint,
    verify,
    verify_proof,
)
from common.model import MAX_OBJECTS, MAX_POINTS, MAX_TEXT, Board, apply_op, validate_op
from common.protocol import ErrorCode

pytestmark = pytest.mark.unit


def rect(obj_id: str = "a", **props):
    return {"kind": "create", "id": obj_id, "type": "rect", "z": "V", "props": {"x": 0, "y": 0, "w": 10, "h": 10, **props}}


def test_create_update_delete():
    board = Board()
    apply_op(board, rect())
    apply_op(board, {"kind": "update", "id": "a", "patch": {"x": 5}})
    assert board.objects["a"]["props"]["x"] == 5
    apply_op(board, {"kind": "delete", "id": "a"})
    assert board.objects["a"]["deleted"] and not board.live()


def test_OR_04_update_on_deleted_object_ignored():
    board = Board()
    apply_op(board, rect())
    apply_op(board, {"kind": "delete", "id": "a"})
    before = board_hash(board)
    op = {"kind": "update", "id": "a", "patch": {"x": 99}}
    assert validate_op(board, op) is None  # accepted so outcome does not depend on arrival order
    apply_op(board, op)
    assert board_hash(board) == before and board.get_live("a") is None


def test_OR_05_last_writer_wins_is_per_property():
    board = Board()
    apply_op(board, rect())
    apply_op(board, {"kind": "update", "id": "a", "patch": {"x": 1, "fill": "#111111"}})
    apply_op(board, {"kind": "update", "id": "a", "patch": {"x": 2}})
    assert board.objects["a"]["props"]["x"] == 2
    assert board.objects["a"]["props"]["fill"] == "#111111"  # untouched register survives


def test_create_over_tombstone_revives_same_id():
    board = Board()
    apply_op(board, rect())
    apply_op(board, {"kind": "delete", "id": "a"})
    op = rect(x=7)
    assert validate_op(board, op) is None
    apply_op(board, op)
    assert board.get_live("a")["props"]["x"] == 7


def test_create_over_live_id_rejected():
    board = Board()
    apply_op(board, rect())
    assert validate_op(board, rect()) == ErrorCode.ID_EXISTS


def test_clear_then_bulk_create_restores_hash():
    board = Board()
    apply_op(board, rect("a"))
    apply_op(board, rect("b"))
    before = board_hash(board)
    removed = [o for o in board.live()]
    snapshot = [dict(o) | {"deleted": False} for o in removed]
    apply_op(board, {"kind": "clear"})
    assert not board.live()
    apply_op(board, {"kind": "bulk_create", "objects": snapshot})
    assert board_hash(board) == before


def test_replay_is_deterministic(seed):
    rng = random.Random(seed)
    ops = []
    for i in range(500):
        target = f"o{rng.randrange(40)}"
        ops.append(
            rng.choice(
                [
                    rect(target, x=i),
                    {"kind": "update", "id": target, "patch": {"x": i, "fill": "#0d99ff"}},
                    {"kind": "delete", "id": target},
                ]
            )
        )
    first, second = Board(), Board()
    for op in ops:
        apply_op(first, op)
        apply_op(second, op)
    assert board_hash(first) == board_hash(second)


@pytest.mark.parametrize(
    "op, code",
    [
        ({"kind": "explode"}, ErrorCode.UNKNOWN_KIND),
        ({"kind": "create", "id": "x", "type": "rect", "z": "V", "props": {"evil": 1}}, ErrorCode.BAD_SCHEMA),
        ({"kind": "create", "id": "x", "type": "virus", "z": "V"}, ErrorCode.BAD_SCHEMA),
        ({"kind": "create", "id": "x", "type": "rect", "z": "bad key!"}, ErrorCode.BAD_SCHEMA),
        ({"kind": "create", "id": "x", "type": "rect", "z": "V", "props": {"x": True}}, ErrorCode.BAD_SCHEMA),
        ({"kind": "create", "id": "x", "type": "rect", "z": "V", "props": {"opacity": 2}}, ErrorCode.BAD_SCHEMA),
        ({"kind": "create", "id": "x", "type": "rect", "z": "V", "props": {"x": 1e300}}, ErrorCode.BAD_SCHEMA),
        ({"kind": "create", "id": "x" * 500, "type": "rect", "z": "V"}, ErrorCode.BAD_SCHEMA),
        ({"kind": "update", "id": "a"}, ErrorCode.BAD_SCHEMA),
        ({"kind": "delete"}, ErrorCode.BAD_SCHEMA),
        ({"kind": "bulk_create", "objects": "nope"}, ErrorCode.BAD_SCHEMA),
        ({"kind": "create", "id": "x", "type": "text", "z": "V", "props": {"text": "t" * (MAX_TEXT + 1)}}, ErrorCode.LIMIT_EXCEEDED),
        ({"kind": "create", "id": "x", "type": "path", "z": "V", "props": {"points": [0.0] * (MAX_POINTS * 2 + 2)}}, ErrorCode.LIMIT_EXCEEDED),
    ],
)
def test_SE_04_validate_rejects(op, code):
    assert validate_op(Board(), op) == code


def test_SE_04_object_limit():
    board = Board(objects={str(i): {"deleted": False} for i in range(MAX_OBJECTS)})
    assert validate_op(board, rect("new")) == ErrorCode.LIMIT_EXCEEDED


def test_OR_06_many_inserts_in_same_gap_no_collisions():
    lo, hi = "A", "B"
    seen = set()
    for _ in range(1000):
        key = fr.between(lo, hi)
        assert lo < key < hi and key not in seen
        seen.add(key)
        hi = key  # always insert into the same shrinking gap
    assert fr.needs_rebalance(sorted(seen))
    fresh = fr.spaced_keys(len(seen))
    assert fresh == sorted(set(fresh)) and len(fresh) == len(seen)
    assert all(fr.valid_key(k) and len(k) <= fr.REBALANCE_LEN for k in fresh)


def test_fractional_open_ends_and_validity(seed):
    rng = random.Random(seed)
    keys = [fr.between(None, None)]
    for _ in range(500):
        i = rng.randrange(len(keys) + 1)
        lo = keys[i - 1] if i else None
        hi = keys[i] if i < len(keys) else None
        key = fr.between(lo, hi)
        assert fr.valid_key(key)
        keys.insert(i, key)
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    with pytest.raises(ValueError):
        fr.between("B", "A")


def test_identity_sign_verify_and_fingerprint():
    a, b = Identity.generate(), Identity.generate()
    assert a.fingerprint != b.fingerprint and valid_fingerprint(a.fingerprint)
    assert fingerprint_of(a.pubkey) == a.fingerprint
    assert len(short_fingerprint(a.fingerprint)) == 9
    assert verify(a.pubkey, a.sign(b"m"), b"m")
    assert not verify(b.pubkey, a.sign(b"m"), b"m")
    assert not verify(a.pubkey, a.sign(b"m"), b"n")
    assert not verify(b"short", b"junk", b"m")


def test_identity_proof_is_domain_separated():
    # A plain signature over the nonce must not pass as a proof, and vice versa.
    a = Identity.generate()
    nonce = b"n" * 32
    assert verify_proof(a.pubkey, a.sign_proof(nonce), nonce)
    assert not verify_proof(a.pubkey, a.sign(nonce), nonce)


def test_identity_persists_with_private_permissions(tmp_path: Path):
    path = tmp_path / "cfg" / "identity.key"
    first = Identity.load_or_create(path)
    assert Identity.load_or_create(path).fingerprint == first.fingerprint
    if __import__("os").name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
