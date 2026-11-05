"""L0 for .lanvas and snapshots; SV-09, SV-10, EX-05 (file part), chat exclusion rules."""

from __future__ import annotations

import gzip
import json

import pytest

from common import fileformat as ff
from common.identity import Identity
from common.model import Board, apply_op

pytestmark = [pytest.mark.unit, pytest.mark.persist]


def _board() -> Board:
    board = Board()
    apply_op(board, {"kind": "create", "id": "a", "type": "text", "z": "V", "props": {"text": "héllo"}})
    return board


def _doc(identity=None, **kwargs):
    identity = identity or Identity.generate()
    return ff.build_document(_board(), "Sprint-1", 7, "2026-01-01T00:00:00+00:00", identity, "Alice", **kwargs)


def test_canonical_json_is_order_independent():
    assert ff.canonical_json({"b": 1, "a": 2}) == ff.canonical_json({"a": 2, "b": 1})
    assert ff.canonical_json({"é": 1}) == '{"é":1}'.encode()
    with pytest.raises(ValueError):
        ff.canonical_json({"x": float("inf")})


def test_EX_05_lanvas_round_trip_signed():
    identity = Identity.generate()
    loaded = ff.decode_lanvas(ff.encode_lanvas(_doc(identity)))
    assert loaded.trust is ff.Trust.SIGNED
    assert loaded.board_hash == ff.board_hash(_board())
    assert loaded.saved_by["fingerprint"] == identity.fingerprint
    assert loaded.room_name == "Sprint-1" and loaded.seq == 7


def test_encoding_is_reproducible():
    doc = _doc()
    assert ff.encode_lanvas(doc) == ff.encode_lanvas(doc)


def _reencode(doc) -> bytes:
    return gzip.compress(json.dumps(doc).encode())


def test_SV_09_board_edit_detected():
    doc = _doc()
    doc["board"]["objects"]["a"]["props"]["text"] = "evil"
    assert ff.decode_lanvas(_reencode(doc)).trust is ff.Trust.MODIFIED


def test_SV_09_hash_recomputed_but_signature_breaks():
    doc = _doc()
    doc["board"]["objects"]["a"]["props"]["text"] = "evil"
    doc["board_hash"] = ff.board_hash(doc["board"])
    assert ff.decode_lanvas(_reencode(doc)).trust is ff.Trust.MODIFIED


def test_SV_09_other_field_edit_breaks_signature():
    doc = _doc()
    doc["room_name"] = "Other"
    assert ff.decode_lanvas(_reencode(doc)).trust is ff.Trust.MODIFIED


def test_SV_09_resigned_with_wrong_fingerprint_detected():
    # An attacker re-signs with their own key but keeps claiming Alice's fingerprint.
    alice, mallory = Identity.generate(), Identity.generate()
    doc = _doc(alice)
    doc["saved_by"]["pubkey"] = ff.b64(mallory.pubkey)
    unsigned = {k: v for k, v in doc.items() if k != "signature"}
    doc["signature"] = ff.b64(mallory.sign(ff.canonical_json(unsigned)))
    assert ff.decode_lanvas(_reencode(doc)).trust is ff.Trust.MODIFIED


def test_SV_09_unsigned_reported():
    doc = _doc()
    del doc["signature"]
    assert ff.decode_lanvas(_reencode(doc)).trust is ff.Trust.UNSIGNED


def test_SV_09_every_byte_flip_is_detected_or_refused():
    """No flip may change the document while still reporting SIGNED.

    A few bits carry no content at all (gzip header mtime/OS bytes and the padding bits after the
    final deflate block). Flipping those yields the identical signed document, so SIGNED is correct.
    """
    doc = _doc()
    raw = bytearray(ff.encode_lanvas(doc))
    for i in range(len(raw)):
        flipped = bytearray(raw)
        flipped[i] ^= 0x40
        try:
            loaded = ff.decode_lanvas(bytes(flipped))
        except ff.FileError:
            continue  # refused cleanly
        if loaded.trust is ff.Trust.SIGNED:
            assert loaded.board == doc["board"], f"flip at byte {i} changed content unnoticed"
            assert loaded.room_name == doc["room_name"] and loaded.seq == doc["seq"]


def test_SV_10_encrypted_round_trip_and_wrong_password():
    raw = ff.encode_lanvas(_doc(), password="correct horse")
    assert b"Sprint-1" not in raw
    assert ff.decode_lanvas(raw, "correct horse").trust is ff.Trust.SIGNED
    with pytest.raises(ff.PasswordRequired):
        ff.decode_lanvas(raw)
    with pytest.raises(ff.WrongPassword):
        ff.decode_lanvas(raw, "wrong")


def test_SV_10_tampered_ciphertext_fails_cleanly():
    raw = ff.encode_lanvas(_doc(), password="pw")
    wrap = json.loads(raw)
    cipher = bytearray(ff.unb64(wrap["ciphertext"]))
    cipher[10] ^= 1
    wrap["ciphertext"] = ff.b64(bytes(cipher))
    with pytest.raises(ff.WrongPassword):
        ff.decode_lanvas(json.dumps(wrap).encode(), "pw")


def test_SV_10_hostile_kdf_cost_refused():
    wrap = json.loads(ff.encode_lanvas(_doc(), password="pw"))
    wrap["n"] = 2**30
    with pytest.raises(ff.FileError):
        ff.decode_lanvas(json.dumps(wrap).encode(), "pw")


@pytest.mark.parametrize("raw", [b"", b"garbage", gzip.compress(b"not json"), gzip.compress(b'{"format":"other"}'), gzip.compress(b'{"format":"lanvas","version":99}')])
def test_not_a_lanvas_file(raw):
    with pytest.raises(ff.FileError):
        ff.decode_lanvas(raw)


def test_room_chat_is_optional_and_dms_have_no_path_in():
    chat = [{"text": "hi"}]
    assert ff.decode_lanvas(ff.encode_lanvas(_doc(room_chat=chat))).room_chat == chat
    assert ff.decode_lanvas(ff.encode_lanvas(_doc())).room_chat is None
    with pytest.raises(TypeError):
        _doc(dm_chat=chat)


def test_snapshot_verifies_and_rejects_tampering():
    board = _board()
    raw = ff.encode_snapshot(board, "r_1", 9, "t")
    assert ff.decode_snapshot(raw)["seq"] == 9
    doc = json.loads(gzip.decompress(raw))
    doc["board"]["objects"]["a"]["props"]["text"] = "evil"
    assert ff.decode_snapshot(gzip.compress(json.dumps(doc).encode())) is None
    assert ff.decode_snapshot(raw[:20]) is None
