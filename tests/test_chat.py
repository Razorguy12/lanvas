"""CT-*: room chat and private messages."""

from __future__ import annotations

import pytest

from common.chat import dm_channel_id
from common.protocol import ErrorCode, MsgType
from tests.harness import BotClient, add_member, make_room, start_server

pytestmark = [pytest.mark.integration, pytest.mark.chat]


@pytest.fixture
def srv(tmp_path):
    port, handle = start_server(tmp_path)
    yield port, handle
    handle.stop()


@pytest.fixture
def trio(srv):
    port, handle = srv
    a = make_room(port)
    b = add_member(owner=a, port=port, name="b")
    c = add_member(owner=a, port=port, name="c", role="viewer")
    return a, b, c, handle


def test_CT_01_room_message_from_each_role(trio):
    a, b, c, _ = trio
    for sender in (a, b, c):
        sender.send_chat(f"from {sender.name}")
        for receiver in (a, b, c):
            msg = receiver.wait_for(MsgType.CHAT, lambda m, name=sender.name: m["text"] == f"from {name}")
            assert msg["from_fp"] == sender.fp and msg["from_name"] == sender.name
            assert msg["ts"].endswith("+00:00")


def test_CT_01_sender_name_and_fp_cannot_be_forged(trio):
    a, b, _, _ = trio
    b.send_chat("hi", from_fp=a.fp, from_name="owner", ts="2000")
    msg = a.wait_for(MsgType.CHAT)
    assert msg["from_fp"] == b.fp and msg["from_name"] == "b" and not msg["ts"].startswith("2000")


def test_CT_01_control_characters_stripped_and_limits(trio):
    a, _, _, _ = trio
    a.send_chat("he\x00ll\x07o\nworld")
    assert a.wait_for(MsgType.CHAT)["text"] == "hello\nworld"
    a.send_chat("x" * 2001)
    assert a.wait_for(MsgType.CHAT_REJECT)["code"] == ErrorCode.BAD_SCHEMA
    a.send_chat("   ")
    assert a.wait_for(MsgType.CHAT_REJECT)["code"] == ErrorCode.BAD_SCHEMA


def test_CT_02_dm_not_delivered_to_third_member(trio):
    a, b, c, _ = trio
    c.drain()
    c.inbox.clear()
    secret = "the-secret-dm-text"
    b.send_dm(a.fp, secret)
    assert a.wait_for(MsgType.CHAT)["text"] == secret
    assert b.wait_for(MsgType.CHAT)["text"] == secret
    # A marker sent afterwards proves c's stream has passed the point where a leak would appear.
    a.send_chat("marker")
    c.wait_for(MsgType.CHAT, lambda m: m["text"] == "marker", consume=False)
    assert not c.bytes_contain(secret)


def test_CT_03_dm_to_offline_member_appears_in_index(trio):
    a, b, _, _ = trio
    b.close()
    a.send_dm(b.fp, "see you later")
    a.wait_for(MsgType.CHAT_ACK)
    back = BotClient(a.port, "b", b.key)
    entry = back.join(room_id=a.room_id)["dm_index"][0]
    assert entry["peer_fp"] == a.fp and entry["unread"] == 1
    assert entry["last_preview"] == "see you later"
    assert entry["channel_id"] == dm_channel_id(a.fp, b.fp)


def test_CT_04_same_msg_id_one_chat_seq(trio):
    a, b, _, handle = trio
    a.send_chat("once", msg_id="m:1")
    first = a.wait_for(MsgType.CHAT_ACK)
    a.send_chat("once", msg_id="m:1")
    second = a.wait_for(MsgType.CHAT_ACK)
    assert first["chat_seq"] == second["chat_seq"] and second["duplicate"]
    assert len(handle.server.rooms[a.room_id].chat.room.messages) == 1
    # And the same for a DM, whose dedupe table is per channel.
    a.send_dm(b.fp, "x", msg_id="m:2")
    one = a.wait_for(MsgType.CHAT_ACK)
    a.send_dm(b.fp, "x", msg_id="m:2")
    assert a.wait_for(MsgType.CHAT_ACK)["chat_seq"] == one["chat_seq"]


def test_CT_06_dm_to_non_member_rejected(trio):
    a, _, _, _ = trio
    stranger = BotClient(a.port, "stranger")
    a.send_dm(stranger.fp, "hi")
    assert a.wait_for(MsgType.CHAT_REJECT)["code"] == ErrorCode.UNKNOWN_RECIPIENT


def test_CT_06_dm_to_self_rejected(trio):
    a, _, _, _ = trio
    a.send_dm(a.fp, "hi")
    assert a.wait_for(MsgType.CHAT_REJECT)["code"] == ErrorCode.UNKNOWN_RECIPIENT


def test_CT_07_history_for_someone_elses_dm_rejected(trio):
    a, b, c, _ = trio
    a.send_dm(b.fp, "private history")
    a.wait_for(MsgType.CHAT_ACK)
    channel_id = dm_channel_id(a.fp, b.fp)
    c.send(MsgType.CHAT_HISTORY, {"channel_id": channel_id})
    rejected = c.wait_for(MsgType.CHAT_REJECT)
    assert rejected["code"] == ErrorCode.NOT_ALLOWED and "messages" not in rejected
    assert not c.bytes_contain("private history")
    b.send(MsgType.CHAT_HISTORY, {"channel_id": channel_id})
    assert b.wait_for(MsgType.CHAT_HISTORY)["messages"][0]["text"] == "private history"


@pytest.mark.parametrize("bad", ["dm:../../etc", "../room", "", "nope", None, 5])
def test_CT_07_bad_channel_ids_rejected(trio, bad):
    a, _, _, _ = trio
    a.send(MsgType.CHAT_HISTORY, {"channel_id": bad})
    assert a.wait_for(MsgType.CHAT_REJECT)["code"] == ErrorCode.NOT_ALLOWED


def test_CT_08_restart_preserves_chat_dms_order_and_unread(trio):
    a, b, _, handle = trio
    for i in range(5):
        a.send_chat(f"r{i}")
        a.wait_for(MsgType.CHAT_ACK)
    a.send_dm(b.fp, "dm1")
    last = a.wait_for(MsgType.CHAT_ACK)
    a.send_dm(b.fp, "dm2")
    a.wait_for(MsgType.CHAT_ACK)
    b.send(MsgType.CHAT_READ, {"channel_id": last["channel_id"], "chat_seq": last["chat_seq"]})
    b.send(MsgType.PING, {"t": 1})
    b.wait_for(MsgType.PONG)  # PONG arrives after CHAT_READ has been processed
    room_id = a.room_id
    tail_before = [(m["chat_seq"], m["ts"], m["text"]) for m in a.welcome["chat_tail"]]
    assert tail_before == []
    handle.kill()
    handle.restart()
    back = BotClient(handle.port, "b", b.key)
    welcome = back.join(room_id=room_id)
    assert [m["text"] for m in welcome["chat_tail"]] == [f"r{i}" for i in range(5)]
    assert [m["chat_seq"] for m in welcome["chat_tail"]] == [1, 2, 3, 4, 5]
    assert welcome["dm_index"][0]["unread"] == 1  # dm2 only: dm1 was marked read before the crash
    a2 = BotClient(handle.port, "owner", a.key)
    a2.join(room_id=room_id)
    a2.send_chat("r5")
    assert a2.wait_for(MsgType.CHAT)["chat_seq"] == 6


def test_CT_09_history_paging_contiguous(trio):
    a, _, _, handle = trio
    room = handle.server.rooms[a.room_id]
    for i in range(500):  # written directly: the rate limit is not what this test is about
        room.chat.append("room", a.fp, "owner", None, f"m{i}", f"x:{i}", None)
    seen: list[int] = []
    before = None
    while True:
        a.send(MsgType.CHAT_HISTORY, {"channel_id": "room", "before_chat_seq": before, "limit": 100})
        page = a.wait_for(MsgType.CHAT_HISTORY)
        seqs = [m["chat_seq"] for m in page["messages"]]
        assert seqs == sorted(seqs) and len(seqs) <= 100
        seen = seqs + seen
        if not page["has_more"]:
            break
        before = seqs[0]
    assert seen == list(range(1, 501))


def test_CT_10_clear_room_leaves_dms_alone(trio):
    a, b, _, handle = trio
    a.send_chat("room text")
    a.wait_for(MsgType.CHAT_ACK)
    a.send_dm(b.fp, "dm text")
    a.wait_for(MsgType.CHAT_ACK)
    a.admin("clear_chat")
    a.wait_for(MsgType.ADMIN)
    room = handle.server.rooms[a.room_id]
    assert room.chat.room.messages == []
    assert room.chat.channels[dm_channel_id(a.fp, b.fp)].messages[0]["text"] == "dm text"
    a.send_chat("after clear")  # the log must still be writable after being rewritten
    assert a.wait_for(MsgType.CHAT, lambda m: m["text"] == "after clear")["chat_seq"] == 2


def test_CT_11_muted_member_rejected_room_and_dm(trio):
    a, b, _, _ = trio
    a.admin("mute", fp=b.fp, muted=True)
    a.wait_for(MsgType.ADMIN)
    b.send_chat("hi")
    assert b.wait_for(MsgType.CHAT_REJECT)["code"] == ErrorCode.MUTED
    b.send_dm(a.fp, "hi")
    assert b.wait_for(MsgType.CHAT_REJECT)["code"] == ErrorCode.MUTED
    a.admin("mute", fp=b.fp, muted=False)
    a.wait_for(MsgType.ADMIN)
    b.send_chat("back")
    assert a.wait_for(MsgType.CHAT)["text"] == "back"


def test_CT_12_chat_flood_rate_limited_server_responsive(trio):
    a, b, _, handle = trio
    for i in range(200):
        a.send_chat(f"f{i}")
    assert a.wait_for(MsgType.CHAT_REJECT)["code"] == ErrorCode.RATE_LIMITED
    assert len(handle.server.rooms[a.room_id].chat.room.messages) < 50
    b.send(MsgType.PING, {"t": 1})
    b.wait_for(MsgType.PONG, timeout=2)
