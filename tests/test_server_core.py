"""Phase 1 gate: OR-*, RC-02/03/05/06/07, SV-01/03..06, SE-01..04 against a real server."""

from __future__ import annotations

import random
import socket
import struct
import threading

import pytest

from common import durable
from common import protocol as p
from common.model import MAX_TEXT
from common.protocol import ErrorCode, MsgType
from common.snapshot import board_hash, decode_snapshot
from server.loop import Server, State
from tests.harness import (
    BotClient,
    FakeClock,
    FaultyFS,
    SimulatedCrash,
    add_member,
    make_room,
    start_server,
    wait_until,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def srv(tmp_path):
    port, handle = start_server(tmp_path)
    yield port, handle
    handle.stop()


def _random_op(rng: random.Random, bot: BotClient, ids: list[str]) -> dict:
    roll = rng.random()
    if roll < 0.4 or not ids:
        obj_id = f"{bot.name}-{len(ids)}-{rng.randrange(1 << 30)}"
        ids.append(obj_id)
        return {"kind": "create", "id": obj_id, "type": "rect", "z": "V", "props": {"x": rng.randrange(1000), "y": 0, "w": 5, "h": 5}}
    target = rng.choice(ids)
    if roll < 0.85:
        return {"kind": "update", "id": target, "patch": {"x": rng.randrange(1000), "fill": f"#{rng.randrange(1 << 24):06x}"}}
    return {"kind": "delete", "id": target}


def test_OR_01_ten_bots_converge(srv, seed):
    port, handle = srv
    owner = make_room(port)
    bots = [owner] + [add_member(owner, port, f"b{i}") for i in range(9)]
    for bot in bots:
        bot.drain(0.005)
    shared: list[str] = []  # all bots target the same ids, so conflicts really happen
    errors: list[BaseException] = []

    def work(bot: BotClient, bot_seed: int) -> None:
        try:
            rng = random.Random(bot_seed)
            for _ in range(200):
                bot.send_op(_random_op(rng, bot, shared))
                bot.pump(0)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(b, seed + i)) for i, b in enumerate(bots)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    room = handle.server.rooms[owner.room_id]
    for bot in bots:
        bot.barrier()
    total = room.seq
    assert total == 2000, "an accepted op was lost or an op was wrongly rejected"
    for bot in bots:
        bot.recv_until(total, timeout=20)
    hashes = {bot.board_hash() for bot in bots}
    assert hashes == {board_hash(room.board)}, f"divergence, seed {seed}"


def test_OR_02_seq_strictly_increasing_no_gaps(srv):
    port, _ = srv
    owner = make_room(port)
    other = add_member(owner, port, "b")
    for i in range(50):
        owner.create_rect(f"o{i}")
    other.recv_until(50)
    assert other.seen_seqs == list(range(1, 51))


def test_OR_03_duplicate_op_id_one_seq_same_ack(srv):
    port, handle = srv
    owner = make_room(port)
    op = {"kind": "create", "id": "a", "type": "rect", "z": "V", "props": {}}
    owner.send_op(op, op_id="c:1")
    first = owner.wait_for(MsgType.OP_ACK)
    owner.send_op(op, op_id="c:1")
    second = owner.wait_for(MsgType.OP_ACK)
    assert first["seq"] == second["seq"] and second["duplicate"]
    assert handle.server.rooms[owner.room_id].seq == 1


def test_OR_04_update_after_delete_no_resurrection(srv):
    port, handle = srv
    owner = make_room(port)
    owner.create_rect("a")
    owner.send_op({"kind": "delete", "id": "a"})
    owner.send_op({"kind": "update", "id": "a", "patch": {"x": 9}})
    owner.recv_until(3)
    assert owner.board.get_live("a") is None
    assert owner.board_hash() == board_hash(handle.server.rooms[owner.room_id].board)


def test_OR_05_higher_seq_wins_on_all_clients(srv):
    port, _ = srv
    owner = make_room(port)
    other = add_member(owner, port, "b")
    owner.create_rect("a")
    other.recv_until(1)
    owner.send_op({"kind": "update", "id": "a", "patch": {"x": 1}})
    owner.recv_until(2)
    other.send_op({"kind": "update", "id": "a", "patch": {"x": 2}})
    other.recv_until(3)
    owner.recv_until(3)
    assert owner.board.objects["a"]["props"]["x"] == 2 == other.board.objects["a"]["props"]["x"]


def test_OR_06_reorder_via_z_patch_syncs(srv):
    port, _ = srv
    owner = make_room(port)
    owner.create_rect("a")
    owner.send_op({"kind": "update", "id": "a", "patch": {"z": "Vz"}})
    owner.recv_until(2)
    assert owner.board.objects["a"]["z"] == "Vz"


def test_RC_02_reconnect_inside_window_gets_delta_only(srv):
    port, _ = srv
    owner = make_room(port)
    bob = add_member(owner, port, "bob")
    owner.create_rect("a")
    bob.recv_until(1)
    bob.close()
    owner.create_rect("b")
    again = BotClient(port, "bob", bob.key)
    again.board, again.seq = bob.board, bob.seq
    welcome = again.join(room_id=owner.room_id, last_seq=1)
    assert "snapshot" not in welcome and [o["seq"] for o in welcome["delta"]] == [2]
    owner.recv_until(2)
    assert again.board_hash() == owner.board_hash()


def test_RC_03_reconnect_beyond_window_gets_snapshot(srv):
    port, handle = srv
    owner = make_room(port)
    bob = add_member(owner, port, "bob")
    bob.close()
    room = handle.server.rooms[owner.room_id]
    owner.create_rect("a")
    owner.recv_until(1)
    room.recent_ops.clear()  # the replay window has moved past this client
    owner.create_rect("b")
    owner.recv_until(2)
    again = BotClient(port, "bob", bob.key)
    welcome = again.join(room_id=owner.room_id, last_seq=0)
    assert "snapshot" in welcome and again.board_hash() == board_hash(room.board)


def test_RC_03_future_seq_gets_snapshot(srv):
    # A client ahead of the server (server data was lost) must not be told it is up to date.
    port, _ = srv
    owner = make_room(port)
    bob = add_member(owner, port, "bob")
    bob.close()
    again = BotClient(port, "bob", bob.key)
    assert "snapshot" in again.join(room_id=owner.room_id, last_seq=999)


def test_RC_03_explicit_resync(srv):
    port, handle = srv
    owner = make_room(port)
    owner.create_rect("a")
    owner.recv_until(1)
    owner.send(MsgType.RESYNC, {"from_seq": 0})
    assert [o["seq"] for o in owner.wait_for(MsgType.RESYNC)["ops"]] == [1]
    handle.server.rooms[owner.room_id].recent_ops.clear()
    owner.create_rect("b")
    owner.recv_until(2)
    owner.send(MsgType.RESYNC, {"from_seq": 0})
    assert owner.wait_for(MsgType.SNAPSHOT)["board_hash"] == owner.board_hash()


def test_RC_05_server_restart_hash_equal(srv):
    port, handle = srv
    owner = make_room(port)
    for i in range(450):  # crosses the automatic snapshot threshold, so snapshot plus tail is used
        owner.create_rect(f"o{i}", x=i)
    owner.recv_until(450)
    room_id, before = owner.room_id, owner.board_hash()
    assert wait_until(lambda: list(handle.server.rooms[room_id].paths.snapshots.glob("*")))
    handle.kill()
    handle.restart()
    room = handle.server.rooms[room_id]
    assert board_hash(room.board) == before and room.seq == 450
    back = BotClient(port, "owner", owner.key)
    back.join(room_id=room_id)
    assert back.board_hash() == before


def test_RC_05_op_ids_survive_restart(srv):
    port, handle = srv
    owner = make_room(port)
    op = {"kind": "create", "id": "a", "type": "rect", "z": "V", "props": {}}
    owner.send_op(op, op_id="c:1")
    owner.recv_until(1)
    room_id = owner.room_id
    handle.kill()
    handle.restart()
    back = BotClient(port, "owner", owner.key)
    back.join(room_id=room_id)
    back.send_op(op, op_id="c:1")
    assert back.wait_for(MsgType.OP_ACK)["seq"] == 1
    assert handle.server.rooms[room_id].seq == 1


def test_RC_06_heartbeat_away_then_release_keeps_membership(tmp_path):
    clock = FakeClock()
    port, handle = start_server(tmp_path, clock=clock)
    try:
        owner = make_room(port)
        bob = add_member(owner, port, "bob")
        owner.drain()
        owner.inbox.clear()
        clock.advance(16)
        owner.send(MsgType.PING, {"t": 1})  # owner stays live; only bob is silent
        event = owner.wait_for(MsgType.PRESENCE, lambda m: m["event"] == "away")
        assert event["fp"] == bob.fp
        clock.advance(46)
        owner.send(MsgType.PING, {"t": 2})
        assert owner.wait_for(MsgType.PRESENCE, lambda m: m["event"] == "leave")["fp"] == bob.fp
        assert bob.wait_closed()
        room = handle.server.rooms[owner.room_id]
        assert bob.fp in room.acl.members
        back = BotClient(port, "bob", bob.key)
        assert back.join(room_id=owner.room_id)["role"] == "editor"
    finally:
        handle.stop()


def test_RC_07_slow_client_dropped_others_unaffected(tmp_path):
    port, handle = start_server(tmp_path, max_outbox=512 * 1024)
    try:
        owner = make_room(port)
        slow = add_member(owner, port, "slow")
        slow.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        fast = add_member(owner, port, "fast")
        big = "x" * (MAX_TEXT // 2)
        rng = random.Random(1)
        n = 0
        room = handle.server.rooms[owner.room_id]
        target = {c for c in room.connections if c.fp == slow.fp}
        while n < 3000 and any(c in handle.server.conns for c in target):
            # Random text defeats zlib, so the data really does pile up in the outbox.
            text = "".join(rng.choices("abcdefghijklmnop", k=len(big)))
            owner.send_op({"kind": "create", "id": f"o{n}", "type": "text", "z": "V", "props": {"text": text}})
            n += 1
            owner.pump(0)
            fast.pump(0)
        assert not any(c in handle.server.conns for c in target), "slow client never dropped"
        owner.barrier()
        fast.recv_until(room.seq, timeout=30)
        assert fast.board_hash() == board_hash(room.board)
    finally:
        handle.stop()


def test_SV_01_save_ack_matches_client_and_disk(srv):
    port, handle = srv
    owner = make_room(port)
    editor = add_member(owner, port, "ed")
    editor.create_rect("a")
    editor.recv_until(1)
    editor.save("first")
    ack = editor.wait_for(MsgType.SAVE_ACK)
    assert ack["seq"] == 1 and ack["board_hash"] == editor.board_hash() and ack["label"] == "first"
    room = handle.server.rooms[owner.room_id]
    doc = decode_snapshot(room.paths.checkpoint_file(1).read_bytes())
    assert doc["board_hash"] == ack["board_hash"]
    assert room.oplog.read_index()[-1]["checkpoint_id"] == ack["checkpoint_id"]


def test_SV_01_saving_twice_without_edits_is_one_entry(srv):
    port, handle = srv
    owner = make_room(port)
    owner.create_rect("a")
    owner.recv_until(1)
    owner.save()
    owner.wait_for(MsgType.SAVE_ACK)
    owner.save()
    owner.wait_for(MsgType.SAVE_ACK)
    assert len(handle.server.rooms[owner.room_id].oplog.read_index()) == 1


def _populated(port: int) -> BotClient:
    owner = make_room(port)
    for i in range(5):
        owner.create_rect(f"o{i}")
    owner.send_chat("hello")
    owner.recv_until(5)
    owner.wait_for(MsgType.CHAT_ACK)
    return owner


@pytest.mark.persist
@pytest.mark.parametrize("step", [durable.STEP_TMP_WRITTEN, durable.STEP_TMP_SYNCED, durable.STEP_BAK_DONE, durable.STEP_REPLACED])
def test_SV_03_crash_during_checkpoint_restart_clean(srv, tmp_path, step):
    port, handle = srv
    owner = _populated(port)
    room_id, before = owner.room_id, owner.board_hash()
    room = handle.server.rooms[room_id]
    with pytest.raises(SimulatedCrash), FaultyFS(step):
        room.oplog.write_checkpoint(room.board, room_id, room.seq, 0.0, {"fp": "x", "name": "x"}, "")
    handle.kill()
    handle.restart()
    assert board_hash(handle.server.rooms[room_id].board) == before
    # Whatever survived must be a whole, verifying file or not exist at all.
    for path in handle.server.rooms[room_id].paths.checkpoints.glob("*.json.gz"):
        assert decode_snapshot(path.read_bytes()) is not None


@pytest.mark.persist
def test_SV_04_torn_last_line_ops_and_chat(srv):
    port, handle = srv
    owner = _populated(port)
    room_id, before = owner.room_id, owner.board_hash()
    room = handle.server.rooms[room_id]
    paths = room.paths
    handle.kill()
    with open(paths.ops, "ab") as f:
        f.write(b'{"seq":6,"data":{"kind":"crea')
    with open(paths.room_chat, "ab") as f:
        f.write(b'{"seq":2,"da')
    handle.restart()
    room = handle.server.rooms[room_id]
    assert room.seq == 5 and board_hash(room.board) == before
    assert len(room.chat.room.messages) == 1
    assert b"ops.jsonl" in paths.recovery_log.read_bytes()
    assert b"chat room" in paths.recovery_log.read_bytes()
    assert durable.scan_log(paths.ops)[2] is False  # the tail really was cut off


@pytest.mark.persist
def test_SV_05_corrupt_newest_snapshot_falls_back(srv):
    port, handle = srv
    owner = make_room(port)
    room = handle.server.rooms[owner.room_id]
    room_id = owner.room_id
    for i in range(3):
        owner.create_rect(f"o{i}")
    owner.save()
    owner.wait_for(MsgType.SAVE_ACK)
    for i in range(3, 6):
        owner.create_rect(f"o{i}")
    owner.recv_until(6)
    owner.save()
    owner.wait_for(MsgType.SAVE_ACK)
    before = owner.board_hash()
    newest = room.paths.checkpoint_file(6)
    handle.kill()
    newest.write_bytes(newest.read_bytes()[:30] + b"garbage")
    handle.restart()
    room = handle.server.rooms[room_id]
    assert board_hash(room.board) == before and room.seq == 6
    assert b"failed verification" in room.paths.recovery_log.read_bytes()


@pytest.mark.persist
def test_SV_05_all_snapshots_corrupt_replays_full_log(srv):
    port, handle = srv
    owner = _populated(port)
    owner.save()
    owner.wait_for(MsgType.SAVE_ACK)
    room_id, before = owner.room_id, owner.board_hash()
    paths = handle.server.rooms[room_id].paths
    handle.kill()
    for path in paths.checkpoints.glob("*.json.gz"):
        path.write_bytes(b"junk")
    handle.restart()
    assert board_hash(handle.server.rooms[room_id].board) == before


@pytest.mark.persist
def test_SV_06_corrupt_acl_uses_bak(srv):
    port, handle = srv
    owner = make_room(port)
    bob = add_member(owner, port, "bob", "viewer")
    # One more change, so .bak holds a version that already includes bob.
    owner.admin("mute", fp=bob.fp, muted=True)
    owner.wait_for(MsgType.ADMIN)
    room_id = owner.room_id
    paths = handle.server.rooms[room_id].paths
    handle.kill()
    paths.acl_json.write_text("{corrupt")
    handle.restart()
    room = handle.server.rooms[room_id]
    assert room.acl.role_of(bob.fp) == "viewer" and room.acl.owner == owner.fp
    assert b"acl.json" in paths.recovery_log.read_bytes()


@pytest.mark.security
def test_SE_01_garbage_never_kills_server(srv, seed):
    port, handle = srv
    rng = random.Random(seed)
    for i in range(10_000):
        if i % 50 == 0:
            sock = socket.create_connection(("127.0.0.1", port))
        kind = rng.random()
        try:
            if kind < 0.4:
                sock.sendall(rng.randbytes(rng.randrange(1, 64)))
            elif kind < 0.7:
                body = rng.randbytes(rng.randrange(0, 32))
                sock.sendall(struct.pack(">2sBBBI", b"LV", 2, rng.randrange(256), rng.randrange(2), len(body)) + body)
            else:
                sock.sendall(p.encode_frame(rng.randrange(40), {"x": rng.randrange(9)}))
        except OSError:
            sock = socket.create_connection(("127.0.0.1", port))
        if i % 50 == 49:
            sock.close()
    sock.close()
    assert handle.thread.is_alive()
    owner = make_room(port)
    owner.create_rect("a")
    owner.recv_until(1)
    assert wait_until(lambda: len(handle.server.conns) <= 2, timeout=10)


@pytest.mark.security
def test_SE_01_authenticated_garbage_payloads(srv):
    """After HELLO admission, every handler must survive wrong-typed fields."""
    port, handle = srv
    owner = make_room(port)
    junk_values = [None, 1, "x", [], {}, True, -1, 1.5, {"a": []}, ["x"]]
    fields = ["kind", "op_id", "id", "patch", "objects", "channel", "to", "text", "msg_id", "action", "fp",
              "channel_id", "chat_seq", "before_chat_seq", "limit", "from_seq", "label", "settings", "policy",
              "fingerprint", "decision", "role", "checkpoint_id", "k", "d", "type", "z", "props", "ttl"]
    for msg_type in (MsgType.OP, MsgType.CHAT, MsgType.CHAT_HISTORY, MsgType.CHAT_READ, MsgType.RESYNC,
                     MsgType.SAVE, MsgType.ADMIN, MsgType.ACCESS_DECISION, MsgType.TRANSIENT):
        for field in fields:
            for value in junk_values:
                if owner.closed:
                    owner = BotClient(port, "owner", owner.key)
                    owner.join(room_id=next(iter(handle.server.rooms)))
                try:
                    owner.send(msg_type, {field: value})
                    owner.pump(0)
                except OSError:
                    owner.closed = True
    assert handle.thread.is_alive()


@pytest.mark.security
def test_SE_02_slow_loris_closed(tmp_path):
    clock = FakeClock()
    port, handle = start_server(tmp_path, clock=clock)
    try:
        owner = make_room(port)
        owner.send_raw(p.encode_frame(MsgType.PING, {"t": 1})[:5])
        wait_until(lambda: any(c.partial_since for c in handle.server.conns))
        clock.advance(16)
        assert owner.wait_closed()
    finally:
        handle.stop()


@pytest.mark.security
def test_SE_02_silent_unauthenticated_connection_closed(tmp_path):
    clock = FakeClock()
    port, handle = start_server(tmp_path, clock=clock)
    try:
        bot = BotClient(port, "idle").connect()
        wait_until(lambda: handle.server.conns)
        clock.advance(11)
        assert bot.wait_closed()
        assert bot.wait_for(MsgType.ERROR)["code"] == ErrorCode.TIMEOUT
    finally:
        handle.stop()


@pytest.mark.security
def test_SE_03_connection_cap_per_ip(tmp_path):
    port, handle = start_server(tmp_path, max_conns_per_ip=5)
    try:
        bots = [BotClient(port, f"b{i}").connect() for i in range(8)]
        assert wait_until(lambda: sum(1 for c in handle.server.conns if c.state is not State.CLOSING) <= 5)
        refused = [b for b in bots if b.wait_closed(0.3)]
        assert len(refused) == 3
        assert refused[0].wait_for(MsgType.ERROR)["code"] == ErrorCode.TOO_MANY_CONNECTIONS
    finally:
        handle.stop()


@pytest.mark.security
@pytest.mark.parametrize(
    "props, code",
    [
        ({"points": [0.5] * 50_000}, ErrorCode.LIMIT_EXCEEDED),
        ({"name": "n" * 5000}, ErrorCode.LIMIT_EXCEEDED),
        ({"x": "NaN"}, ErrorCode.BAD_SCHEMA),
    ],
)
def test_SE_04_oversized_op_rejected_with_code(srv, props, code):
    port, handle = srv
    owner = make_room(port)
    owner.send_op({"kind": "create", "id": "a", "type": "path", "z": "V", "props": props})
    assert owner.wait_for(MsgType.OP_REJECT)["code"] == code
    assert handle.server.rooms[owner.room_id].seq == 0


@pytest.mark.security
def test_SE_04_op_fields_cannot_be_forged(srv):
    port, handle = srv
    owner = make_room(port)
    bob = add_member(owner, port, "bob")
    bob.send_op({"kind": "create", "id": "a", "type": "rect", "z": "V", "props": {}, "seq": 999, "author": owner.fp})
    logged = bob.wait_for(MsgType.OP)
    assert logged["seq"] == 1 and logged["author"] == bob.fp


@pytest.mark.component
def test_FR_04_bad_magic_closed_with_error_socketpair(tmp_path):
    server = Server(tmp_path, discovery=False, port=0)
    server.start()
    a, b = socket.socketpair()
    try:
        server.adopt(a, ("local", 0))
        b.sendall(b"XXXX")
        decoder = p.FrameDecoder()
        frames = []
        b.settimeout(2)
        while True:
            server.poll(0.01)
            try:
                data = b.recv(4096)
            except TimeoutError:
                break
            if not data:
                break
            frames += decoder.feed(data)
        assert frames[0][0] == MsgType.ERROR and frames[0][1]["code"] == ErrorCode.BAD_FRAME
        assert not server.conns
    finally:
        b.close()
        server.request_stop()
        server._shutdown()


@pytest.mark.component
def test_FR_06_invalid_json_and_oversize_preauth_closed_server_alive(srv):
    port, handle = srv
    for raw in (
        p.HEADER.pack(p.MAGIC, 2, MsgType.HELLO, 0, 5) + b"{bad}",
        p.HEADER.pack(p.MAGIC, 2, MsgType.HELLO, 0, p.MAX_FRAME_PREAUTH + 1),
    ):
        bot = BotClient(port).connect()
        bot.send_raw(raw)
        assert bot.wait_closed()
    assert handle.thread.is_alive()
