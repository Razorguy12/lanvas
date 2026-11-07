"""UN-*, RC-01, RC-04, CT-03, SV-07, SV-08, SV-11..13, CH-*: client logic against a real server."""

from __future__ import annotations

import os
import random
import time

import pytest

from client import paths
from client.document import Document
from client.recovery import Journal, discard_crashed, find_crashed
from client.session import SAVE_SAVED, Session
from client.transport import STATE_LIVE, NetCore
from common.identity import Identity
from common.model import Board, apply_op
from common.protocol import MsgType
from common.snapshot import board_hash
from tests.harness import BotClient, add_member, make_room, start_server, wait_until

pytestmark = pytest.mark.integration
FULL_SOAK = os.environ.get("LANVAS_FULL_SOAK") == "1"
SOAK_SECONDS = 60.0 if FULL_SOAK else 3.0
SOAK_LATENCY = 0.3 if FULL_SOAK else 0.05


# ---- Undo logic against a tiny in-process sequencer: deterministic, and without sockets ----
class FakeServer:
    """Assigns seq and broadcasts, like the real server, so undo tests read as plain scenarios."""

    def __init__(self):
        self.board = Board()
        self.seq = 0
        self.clients: list[tuple[Document, str]] = []

    def client(self, name: str) -> Document:
        doc = Document(name)
        self.clients.append((doc, name))
        return doc

    def submit(self, op: dict, author: str) -> None:
        self.seq += 1
        logged = {**op, "seq": self.seq, "author_name": author}
        apply_op(self.board, logged)
        for doc, _ in self.clients:
            doc.receive(logged)

    def assert_converged(self) -> None:
        hashes = {doc.hash() for doc, _ in self.clients} | {board_hash(self.board)}
        assert len(hashes) == 1


@pytest.fixture
def world():
    server = FakeServer()
    return server, server.client("A"), server.client("B")


def _do(server, doc, name, op):
    server.submit(op, name)
    return op


def _send_all(server, doc, name, ops):
    for op in ops:
        server.submit(op, name)


def _make(server, a):
    op = a.create("rect", {"x": 0, "y": 0, "w": 10, "h": 10}, obj_id="r")
    server.submit(op, "A")


@pytest.mark.unit
def test_UN_01_undo_create_then_redo_same_id(world):
    server, a, b = world
    _make(server, a)
    result = a.undo()
    assert [op["kind"] for op in result.ops] == ["delete"]
    _send_all(server, a, "A", result.ops)
    assert b.view.get_live("r") is None
    result = a.redo()
    assert result.ops[0]["kind"] == "create" and result.ops[0]["id"] == "r"
    _send_all(server, a, "A", result.ops)
    assert b.view.get_live("r") is not None
    server.assert_converged()


@pytest.mark.unit
def test_UN_02_undo_move_restores_position(world):
    server, a, b = world
    _make(server, a)
    server.submit(a.update("r", {"x": 50, "y": 60}), "A")
    _send_all(server, a, "A", a.undo().ops)
    assert b.view.objects["r"]["props"]["x"] == 0 and b.view.objects["r"]["props"]["y"] == 0
    server.assert_converged()


@pytest.mark.unit
def test_UN_02_drag_coalesces_into_one_step(world):
    server, a, _ = world
    _make(server, a)
    for x in (1, 2, 3, 4):
        server.submit(a.update("r", {"x": x}, group="drag-1"), "A")
    assert len(a.undo_stack) == 2  # the create, plus one step for the whole drag
    _send_all(server, a, "A", a.undo().ops)
    assert a.view.objects["r"]["props"]["x"] == 0


@pytest.mark.unit
def test_UN_03_conflicting_property_skipped_others_undone(world):
    server, a, b = world
    _make(server, a)
    server.submit(a.update("r", {"x": 50, "y": 60}), "A")
    server.submit(b.update("r", {"x": 99}), "B")  # Sam moves x after us; y is still ours
    result = a.undo()
    assert result.skipped == 1 and result.skipped_by == {"B"}
    _send_all(server, a, "A", result.ops)
    props = b.view.objects["r"]["props"]
    assert props["x"] == 99 and props["y"] == 0
    server.assert_converged()


@pytest.mark.unit
def test_UN_03_undo_update_on_object_deleted_by_other_skips(world):
    server, a, b = world
    _make(server, a)
    server.submit(a.update("r", {"x": 50}), "A")
    server.submit(b.delete("r"), "B")
    result = a.undo()
    assert result.ops == [] and result.skipped == 1
    assert a.view.get_live("r") is None  # undo must not resurrect someone else's delete


@pytest.mark.unit
def test_UN_04_undo_never_touches_other_users_ops(world):
    server, a, b = world
    _make(server, a)
    server.submit(b.create("rect", {"x": 1}, obj_id="b-rect"), "B")
    server.submit(b.update("b-rect", {"x": 2}), "B")
    _send_all(server, a, "A", a.undo().ops)  # undoes A's own create only
    assert b.view.get_live("r") is None and b.view.objects["b-rect"]["props"]["x"] == 2
    assert a.undo().ops == []  # nothing else of A's left to undo
    assert a.view.get_live("b-rect") is not None
    server.assert_converged()


@pytest.mark.unit
def test_UN_05_undo_clear_restores_objects_for_issuer(world):
    server, a, b = world
    _make(server, a)
    server.submit(b.create("rect", {"x": 1}, obj_id="other"), "B")
    before = a.hash()
    server.submit(a.clear(), "A")
    assert not b.view.live()
    _send_all(server, a, "A", a.undo().ops)
    assert {o["id"] for o in b.view.live()} == {"r", "other"}
    assert a.hash() == before
    _send_all(server, a, "A", a.redo().ops)
    assert not b.view.live()
    server.assert_converged()


@pytest.mark.unit
def test_rejected_op_rolled_back_and_removed_from_undo(world):
    _, a, _ = world
    op = a.create("rect", {"x": 0}, obj_id="r")
    assert a.view.get_live("r") is not None
    assert a.on_reject(op["op_id"]) == {"r"}
    assert a.view.get_live("r") is None and not a.can_undo()


@pytest.mark.unit
def test_optimistic_pending_reapplied_over_remote_update(world):
    """Local x stays on screen while pending, and server order decides once it resolves."""
    server, a, b = world
    _make(server, a)
    mine = a.update("r", {"x": 5})  # not sent yet
    server.submit(b.update("r", {"x": 7, "y": 9}), "B")
    assert a.view.objects["r"]["props"]["x"] == 5 and a.view.objects["r"]["props"]["y"] == 9
    server.submit(mine, "A")
    assert a.view.objects["r"]["props"]["x"] == 5
    server.assert_converged()


@pytest.mark.unit
def test_CH_02_out_of_order_ops_buffer_and_gap_detected(seed):
    ops = []
    source = Board()
    for i in range(1, 51):
        op = {"kind": "create", "id": f"o{i}", "type": "rect", "z": "V", "props": {"x": i}, "seq": i}
        apply_op(source, op)
        ops.append(op)
    shuffled = list(ops)
    random.Random(seed).shuffle(shuffled)
    doc = Document("x")
    gap_seen = False
    for op in shuffled:
        _, gap = doc.receive(op)
        gap_seen = gap_seen or gap
        doc.receive(op)  # every frame duplicated as well
    assert gap_seen and doc.seq == 50 and doc.hash() == board_hash(source)


# ---- Sessions on real sockets ----
class Client:
    """A Session driven by NetCore on its thread: headless, but the same code path as the UI."""

    def __init__(self, port, name, key=None, journal=None, **hello):
        self.identity = key or Identity.generate()
        self.events = []
        self.frames = []
        self.hello = hello
        self.session = Session(
            self.identity, name, send=self._send, emit=self._emit, journal_root=journal, host="127.0.0.1", port=port
        )
        self.net = NetCore("127.0.0.1", port, self.identity, name, self._on_net, lambda: self.session.hello_fields(self.hello))

    def _send(self, msg_type, payload):
        self.net.send(msg_type, payload)

    def _emit(self, name, **kw):
        self.events.append((name, kw))

    def _on_net(self, kind, data):
        self.frames.append((kind, data))

    def start(self):
        self.net.start()
        return self

    def pump(self):
        """Run queued network events on this thread, which plays the part of the UI thread."""
        while self.frames:
            kind, data = self.frames.pop(0)
            if kind == "state":
                self.session.on_state(data)
            elif kind == "frame":
                self.session.handle_frame(*data)

    def wait(self, predicate, timeout=10.0):
        def check():
            self.pump()
            return predicate()

        assert wait_until(check, timeout), f"timed out; events={[e for e, _ in self.events[-10:]]}"

    def wait_live(self):
        self.wait(lambda: self.session.live and self.session.welcomed)
        return self

    def stop(self, clean=True):
        self.net.stop()
        self.session.close(clean=clean)

    def event_names(self):
        return [name for name, _ in self.events]


@pytest.fixture
def srv(tmp_path):
    port, handle = start_server(tmp_path / "server")
    yield port, handle
    handle.stop()


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LANVAS_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _converged(*clients):
    for client in clients:
        client.pump()
    return len({c.session.doc.hash() for c in clients}) == 1 and len({c.session.doc.seq for c in clients}) == 1


def test_RC_01_kill_tcp_mid_stroke_resumes_no_dup_no_loss(srv):
    port, handle = srv
    owner = make_room(port)
    a = Client(port, "a", room_id=owner.room_id).start()
    a.wait(lambda: any(k == "access_pending" for k, _ in a.events))
    owner.wait_for(MsgType.ACCESS_REQUEST)
    owner.send(MsgType.ACCESS_DECISION, {"fingerprint": a.identity.fingerprint, "decision": "approve", "role": "editor"})
    a.wait_live()
    session = a.session
    for i in range(20):
        session.create("rect", {"x": i}, obj_id=f"before{i}")
    a.net.request_drop()  # the connection dies with ops possibly in flight
    for i in range(20):
        session.create("rect", {"x": i}, obj_id=f"during{i}")
    a.wait(lambda: not session.doc.pending, timeout=15)
    room = handle.server.rooms[owner.room_id]
    assert room.seq == 40  # no duplicates, nothing lost
    assert session.doc.hash() == board_hash(room.board)
    owner.recv_until(40)
    assert owner.board_hash() == session.doc.hash()
    a.stop()


def test_RC_04_unacked_ops_and_chat_resent_deduped(srv):
    port, handle = srv
    owner = make_room(port)
    room = handle.server.rooms[owner.room_id]
    key = Identity.generate()
    room.acl.add_member(key.fingerprint, "a", "editor")
    a = Client(port, "a", key, room_id=owner.room_id).start().wait_live()
    session = a.session
    session.create("rect", {"x": 1}, obj_id="one")
    session.send_chat("room", "hello")
    a.wait(lambda: not session.doc.pending and not session.chat.outbox)
    # Offline edits: the transport drops frames, so they stay in the outboxes until reconnect.
    a.net.request_drop()
    a.wait(lambda: not session.live)
    op = session.create("rect", {"x": 2}, obj_id="two")
    out = session.send_chat("room", "offline hi")
    assert op and out.state == "sending"
    a.wait(lambda: session.live and not session.doc.pending and not session.chat.outbox, timeout=15)
    # Now re-deliver the already-accepted ids, as a client unsure of what got through would.
    session.send(MsgType.OP, op)
    session.send(MsgType.CHAT, out.payload)
    a.wait(lambda: True)
    time.sleep(0.05)
    a.pump()
    assert room.seq == 2
    assert [m["text"] for m in room.chat.room.messages] == ["hello", "offline hi"]
    a.stop()


@pytest.mark.chat
def test_CT_03_offline_dm_arrives_with_unread_badge(srv):
    port, handle = srv
    owner = make_room(port)
    room = handle.server.rooms[owner.room_id]
    key = Identity.generate()
    room.acl.add_member(key.fingerprint, "a", "editor")
    owner.send_dm(key.fingerprint, "while you were out")
    owner.wait_for(MsgType.CHAT_ACK)
    a = Client(port, "a", key, room_id=owner.room_id).start().wait_live()
    channel = a.session.chat.ensure_dm(owner.fp)
    assert channel.unread == 1 and channel.preview == "while you were out"
    a.session.open_channel(channel.channel_id)
    a.wait(lambda: channel.seqs == [1])
    assert channel.unread == 0
    assert a.session.chat.total_unread() == 0
    a.stop()
    # The read position followed the user to the server.
    assert wait_until(lambda: room.chat.read.get(key.fingerprint, {}).get(channel.channel_id) == 1)


@pytest.mark.persist
def test_SV_07_killed_client_recovers_once(srv, tmp_path):
    port, handle = srv
    journal_root = paths.journal_dir()
    owner = make_room(port)
    room = handle.server.rooms[owner.room_id]
    key = Identity.generate()
    room.acl.add_member(key.fingerprint, "a", "editor")
    a = Client(port, "a", key, journal=journal_root, room_id=owner.room_id).start().wait_live()
    session = a.session
    session.create("rect", {"x": 1}, obj_id="confirmed")
    a.wait(lambda: not session.doc.pending)
    # The link goes down, the user keeps working, then the process dies before anything is resent.
    a.net.chaos.drop_requested = True
    a.wait(lambda: not session.live)
    a.net._stop.set()
    a.net._wake()
    session.create("rect", {"x": 2}, obj_id="unsent")
    session.send_chat("room", "unsent message")
    session.journal.worker.flush()
    # Simulate death: the worker stops, the lock stays, and it names a process that is gone.
    a.net.stop()
    session.journal.worker.stop()
    lock = session.journal.lock_path
    lock.write_text('{"pid": 999999999}')

    crashed = find_crashed(journal_root)
    assert len(crashed) == 1
    state = crashed[0]
    assert [op["id"] for op in state.ops] == ["unsent"]
    assert [m["text"] for m in state.chat] == ["unsent message"]
    assert state.room_id == owner.room_id

    again = Client(port, "a", key, journal=None)
    again.session.adopt_crashed(state)
    again.start().wait_live()
    again.wait(lambda: not again.session.doc.pending and not again.session.chat.outbox, timeout=15)
    assert ("recovered", {"ops": 1, "chat": 1}) in again.events
    assert room.seq == 2
    assert [m["text"] for m in room.chat.room.messages] == ["unsent message"]
    # A second recovery of the same journal must not double anything: ids are the originals.
    third = Client(port, "a", key, journal=None)
    third.session.adopt_crashed(state)
    third.start().wait_live()
    third.wait(lambda: not third.session.doc.pending and not third.session.chat.outbox, timeout=15)
    assert room.seq == 2 and len(room.chat.room.messages) == 1
    discard_crashed(state)
    assert find_crashed(journal_root) == []
    again.stop()
    third.stop()


@pytest.mark.persist
def test_SV_07_clean_exit_and_live_session_are_not_crashes(tmp_path):
    root = tmp_path / "j"
    journal = Journal(root, "r_1", "Room", "h", 1)
    assert find_crashed(root) == []  # this very process is alive
    journal.close(clean=True)
    assert find_crashed(root) == []


@pytest.mark.persist
def test_SV_07_torn_journal_tail_tolerated(tmp_path):
    root = tmp_path / "j"
    journal = Journal(root, "r_1", "Room", "h", 1)
    journal.op_added({"op_id": "a:1", "kind": "delete", "id": "x"})
    journal.op_added({"op_id": "a:2", "kind": "delete", "id": "y"})
    journal.op_done("a:1")
    journal.worker.flush()
    journal.worker.stop()
    journal.lock_path.write_text('{"pid": 999999999}')
    with open(journal.dir / "pending.jsonl", "ab") as f:
        f.write(b'{"seq":9,"data":{"e":"op')
    state = find_crashed(root)[0]
    assert [op["op_id"] for op in state.ops] == ["a:2"]


@pytest.mark.persist
def test_SV_01_hash_mismatch_triggers_refresh(srv):
    port, handle = srv
    owner = make_room(port)
    room = handle.server.rooms[owner.room_id]
    a = Client(port, "owner", owner.key, room_id=owner.room_id).start().wait_live()
    a.session.create("rect", {"x": 1}, obj_id="r")
    a.wait(lambda: not a.session.doc.pending)
    # Force this client's copy out of step with the truth, as a bug would.
    a.session.doc.confirmed.objects["r"]["props"]["x"] = 999
    a.session.save()
    a.wait(lambda: "hash_mismatch" in a.event_names())
    a.wait(lambda: a.session.doc.hash() == board_hash(room.board))
    a.stop()


@pytest.mark.persist
def test_SV_11_restore_checkpoint_then_undo(srv):
    port, handle = srv
    owner = make_room(port)
    room = handle.server.rooms[owner.room_id]
    a = Client(port, "owner", owner.key, room_id=owner.room_id).start().wait_live()
    watcher = add_member(owner, port, "w", "viewer")
    session = a.session
    session.create("rect", {"x": 1}, obj_id="r1")
    session.update("r1", {"x": 2})
    a.wait(lambda: not session.doc.pending)
    session.save()
    a.wait(lambda: session.save_state == SAVE_SAVED)
    checkpoint = session.save_info["checkpoint_id"]
    checkpoint_hash = session.save_info["board_hash"]
    session.create("rect", {"x": 5}, obj_id="r2")
    session.delete("r1")
    a.wait(lambda: not session.doc.pending)
    edited_hash = session.doc.hash()
    assert edited_hash != checkpoint_hash

    session.restore(checkpoint)
    a.wait(lambda: not session.doc.pending and session.doc.hash() == checkpoint_hash)
    assert board_hash(room.board) == checkpoint_hash
    watcher.recv_until(room.seq)
    assert watcher.board_hash() == checkpoint_hash

    session.undo()
    a.wait(lambda: not session.doc.pending)
    assert session.doc.hash() == edited_hash == board_hash(room.board)
    watcher.recv_until(room.seq)
    assert watcher.board_hash() == edited_hash
    a.stop()


@pytest.mark.persist
def test_SV_11_restore_is_owner_only_and_needs_valid_checkpoint(srv):
    port, _ = srv
    owner = make_room(port)
    editor = add_member(owner, port, "e")
    editor.send_op({"kind": "restore", "checkpoint_id": "cp_1"})
    assert editor.wait_for(MsgType.OP_REJECT)["code"] == "NOT_ALLOWED"
    owner.send_op({"kind": "restore", "checkpoint_id": "cp_404"})
    assert owner.wait_for(MsgType.OP_REJECT)["code"] == "NOT_FOUND"


@pytest.mark.chaos
def test_CH_01_delay_duplicates_five_clients_converge(srv, seed):
    port, handle = srv
    owner = make_room(port)
    room = handle.server.rooms[owner.room_id]
    clients = []
    for i in range(5):
        key = Identity.generate()
        room.acl.add_member(key.fingerprint, f"c{i}", "editor")
        client = Client(port, f"c{i}", key, room_id=owner.room_id).start().wait_live()
        client.net.chaos.latency = SOAK_LATENCY
        client.net.chaos.duplicate = 0.2
        client.net.chaos.rng = random.Random(seed + i)
        clients.append(client)
    rng = random.Random(seed)
    ids = []
    deadline = time.monotonic() + SOAK_SECONDS
    n = 0
    while time.monotonic() < deadline:
        client = rng.choice(clients)
        client.pump()
        if rng.random() < 0.3 or not ids:
            obj_id = f"o{n}"
            n += 1
            ids.append(obj_id)
            client.session.create("rect", {"x": 0}, obj_id=obj_id)
        else:
            client.session.update(rng.choice(ids), {"x": rng.randrange(1000)})
        if n % 7 == 0:
            client.session.send_chat("room", f"m{n}")
            n += 1
        time.sleep(0.002)
    for client in clients:
        client.net.chaos.latency = 0
        client.net.chaos.duplicate = 0
    assert wait_until(lambda: _converged(*clients) and all(not c.session.doc.pending for c in clients), 30), f"seed {seed}"
    assert wait_until(lambda: (_converged(*clients), all(not c.session.chat.outbox for c in clients))[1], 30)
    assert clients[0].session.doc.hash() == board_hash(room.board)
    orders = {tuple(c.session.chat.room.seqs) for c in clients}
    for client in clients:
        client.pump()
    orders = {tuple(c.session.chat.room.seqs) for c in clients}
    assert len(orders) == 1
    texts = {tuple(m["text"] for m in c.session.chat.room.messages) for c in clients}
    assert len(texts) == 1, "chat order differs"
    for client in clients:
        client.stop()


@pytest.mark.chaos
def test_CH_02_reorder_on_the_wire_converges(srv):
    port, handle = srv
    owner = make_room(port)
    room = handle.server.rooms[owner.room_id]
    a = Client(port, "owner", owner.key, room_id=owner.room_id).start().wait_live()
    key = Identity.generate()
    room.acl.add_member(key.fingerprint, "b", "editor")
    b = Client(port, "b", key, room_id=owner.room_id).start().wait_live()
    b.net.chaos.reorder_next = 5
    for i in range(20):
        a.session.create("rect", {"x": i}, obj_id=f"o{i}")
    assert wait_until(lambda: _converged(a, b) and b.session.doc.seq == 20, 10)
    assert b.session.doc.hash() == board_hash(room.board)
    a.stop()
    b.stop()


@pytest.mark.chaos
def test_CH_03_random_disconnects_converge(srv, seed):
    """Disconnects every few hundred ms: far more frequent than the catalogue's 5-15 s."""
    port, handle = srv
    owner = make_room(port)
    room = handle.server.rooms[owner.room_id]
    clients = []
    for i in range(3):
        key = Identity.generate()
        room.acl.add_member(key.fingerprint, f"c{i}", "editor")
        clients.append(Client(port, f"c{i}", key, room_id=owner.room_id).start().wait_live())
    rng = random.Random(seed)
    for n in range(150):
        client = rng.choice(clients)
        client.pump()
        client.session.create("rect", {"x": n}, obj_id=f"o{n}")
        if n % 30 == 15:
            rng.choice(clients).net.request_drop()
        time.sleep(0.005)
    assert wait_until(lambda: _converged(*clients) and all(c.session.live and not c.session.doc.pending for c in clients), 40), f"seed {seed}"
    assert room.seq == 150, "ops lost or duplicated across reconnects"
    assert clients[0].session.doc.hash() == board_hash(room.board)
    for client in clients:
        client.stop()


def test_bot_and_session_agree(srv):
    port, handle = srv
    owner = make_room(port)
    a = Client(port, "owner", owner.key, room_id=owner.room_id).start().wait_live()
    a.session.create("text", {"text": "é", "x": 1})
    a.wait(lambda: not a.session.doc.pending)
    owner.recv_until(1)
    assert owner.board_hash() == a.session.doc.hash() == board_hash(handle.server.rooms[owner.room_id].board)
    assert BotClient(port, "x").key
    assert a.net.state == STATE_LIVE
    a.stop()
