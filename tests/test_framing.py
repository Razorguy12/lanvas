"""FR-01..FR-06: TCP stream reassembly and hostile framing."""

from __future__ import annotations

import json
import random
import struct
import zlib

import pytest

from common import protocol as p

pytestmark = pytest.mark.unit


def test_FR_01_split_at_every_byte_boundary():
    frames = [p.encode_frame(p.MsgType.OP, {"n": i, "s": "é" * i}) for i in range(5)]
    blob = b"".join(frames)
    for cut in range(len(blob) + 1):
        decoder = p.FrameDecoder()
        out = decoder.feed(blob[:cut]) + decoder.feed(blob[cut:])
        assert [m["n"] for _, m in out] == list(range(5))
        assert not decoder.has_partial


def test_FR_01_one_byte_at_a_time():
    blob = p.encode_frame(p.MsgType.CHAT, {"text": "hello"})
    decoder = p.FrameDecoder()
    out = []
    for i in range(len(blob)):
        out += decoder.feed(blob[i : i + 1])
    assert out == [(p.MsgType.CHAT, {"text": "hello"})]


def test_FR_02_many_frames_in_one_recv():
    blob = b"".join(p.encode_frame(p.MsgType.PING, {"i": i}) for i in range(1000))
    out = p.FrameDecoder().feed(blob)
    assert [m["i"] for _, m in out] == list(range(1000))


@pytest.mark.parametrize("cap", [p.MAX_FRAME, p.MAX_FRAME_PREAUTH])
def test_FR_03_length_above_cap_refused_before_buffering(cap):
    header = p.HEADER.pack(p.MAGIC, p.PROTOCOL_VERSION, p.MsgType.OP, 0, cap + 1)
    decoder = p.FrameDecoder(cap)
    with pytest.raises(p.ProtocolError) as err:
        decoder.feed(header)
    assert err.value.code == p.ErrorCode.FRAME_TOO_LARGE
    assert decoder.buffered <= p.HEADER_SIZE  # refused with no payload buffered


def test_FR_03_inflation_bomb_refused():
    body = zlib.compress(b" " * (p.MAX_INFLATED + 1024))
    frame = p.HEADER.pack(p.MAGIC, p.PROTOCOL_VERSION, p.MsgType.OP, p.FLAG_COMPRESSED, len(body))
    with pytest.raises(p.ProtocolError):
        p.FrameDecoder().feed(frame + body)


def test_FR_04_bad_magic_fails_on_first_byte():
    with pytest.raises(p.ProtocolError) as err:
        p.FrameDecoder().feed(b"X")
    assert err.value.code == p.ErrorCode.BAD_FRAME


def test_FR_04_bad_version():
    frame = bytearray(p.encode_frame(p.MsgType.PING))
    frame[2] = 99
    with pytest.raises(p.ProtocolError) as err:
        p.FrameDecoder().feed(bytes(frame))
    assert err.value.code == p.ErrorCode.BAD_VERSION


def test_FR_05_compression_round_trip():
    payload = {"text": "abc" * 2000}
    frame = p.encode_frame(p.MsgType.CHAT, payload)
    assert frame[4] & p.FLAG_COMPRESSED
    assert len(frame) < 2000
    assert p.FrameDecoder().feed(frame) == [(p.MsgType.CHAT, payload)]


def test_FR_05_small_payload_not_compressed():
    assert not p.encode_frame(p.MsgType.CHAT, {"t": "x"})[4] & p.FLAG_COMPRESSED


def _raw(body: bytes) -> bytes:
    return p.HEADER.pack(p.MAGIC, p.PROTOCOL_VERSION, p.MsgType.OP, 0, len(body)) + body


@pytest.mark.parametrize(
    "body",
    [b"{not json", b"[1,2,3]", b'"str"', b"null", b"\xff\xfe", b'{"a":NaN}', b'{"a":Infinity}'],
)
def test_FR_06_invalid_json_or_wrong_type(body):
    with pytest.raises(p.ProtocolError) as err:
        p.FrameDecoder().feed(_raw(body))
    assert err.value.code == p.ErrorCode.BAD_JSON


def test_FR_06_depth_bomb_rejected_without_recursion():
    depth = 1_000_000
    with pytest.raises(p.ProtocolError):
        p.FrameDecoder().feed(_raw(b'{"a":' + b"[" * depth + b"]" * depth + b"}"))


def test_FR_06_brackets_inside_strings_are_not_depth():
    payload = {"t": "[" * 500 + "\\\"" + "{" * 500}
    assert p.FrameDecoder().feed(p.encode_frame(p.MsgType.CHAT, payload))[0][1] == payload


def test_FR_06_random_bytes_never_escape_as_other_exceptions(seed):
    rng = random.Random(seed)
    for _ in range(2000):
        decoder = p.FrameDecoder()
        try:
            decoder.feed(rng.randbytes(rng.randrange(1, 64)))
        except p.ProtocolError:
            pass
        # Valid header with garbage body must also fail only as ProtocolError.
        junk = rng.randbytes(rng.randrange(0, 64))
        try:
            p.FrameDecoder().feed(struct.pack(">2sBBBI", b"LV", 2, 8, rng.choice((0, 1)), len(junk)) + junk)
        except p.ProtocolError:
            pass


def test_encode_refuses_non_finite():
    with pytest.raises(ValueError):
        p.encode_frame(p.MsgType.OP, {"x": float("nan")})
    assert json.loads(p.encode_payload({"é": 1}).decode()) == {"é": 1}
