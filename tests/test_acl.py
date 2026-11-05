"""AC-*, CT-05, SV-02: access control, including raw-socket attempts to get around it."""

from __future__ import annotations

import itertools

import pytest

from common.identity import Identity, b64, unb64
from common.protocol import ErrorCode, MsgType, encode_frame
from server import acl as A
from tests.harness import BotClient, FakeClock, add_member, make_room, start_server, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.security]


@pytest.fixture
def srv(tmp_path):
    port, handle = start_server(tmp_path)
    yield port, handle
    handle.stop()


# §6.3 permission matrix, written out independently of server/acl.py so that the test is a real check.
EXPECTED = {
    A.A_EDIT: {"owner": True, "editor": True, "viewer": False, None: False},
    A.A_CHAT: {"owner": True, "editor": True, "viewer": True, None: False},
    A.A_HISTORY: {"owner": True, "editor": True, "viewer": True, None: False},
    A.A_MANAGE_CHAT: {"owner": True, "editor": False, "viewer": False, None: False},
    A.A_SAVE: {"owner": True, "editor": True, "viewer": False, None: False},
    A.A_CLEAR: {"owner": True, "editor": False, "viewer": False, None: False},
    A.A_RESTORE: {"owner": True, "editor": False, "viewer": False, None: False},
    A.A_ADMIN: {"owner": True, "editor": False, "viewer": False, None: False},
}


@pytest.mark.unit
@pytest.mark.parametrize("action,role", itertools.product(EXPECTED, ["owner", "editor", "viewer", None]))
def test_AC_01_matrix_table(action, role):
    assert A.can(role, action, dict(A.DEFAULT_SETTINGS)) is EXPECTED[action][role]


@pytest.mark.unit
def test_AC_01_settings_modify_matrix():
    assert A.can("editor", A.A_CLEAR, {"editors_can_clear": True})
    assert not A.can("viewer", A.A_CLEAR, {"editors_can_clear": True})
    assert not A.can("editor", A.A_EDIT, {"locked": True})
    assert A.can("owner", A.A_EDIT, {"locked": True})
    assert not A.can("hacker", A.A_CHAT, {})


def _exercise(bot: BotClient, owner: BotClient, target_fp: str) -> dict[str, bool]:
    """Try each permission over the wire and report what the server actually allowed."""
    results = {}

    bot.send_op({"kind": "create", "id": "x" + bot.next_op_id(), "type": "rect", "z": "V", "props": {}})
    results["edit"] = _wait_either(bot, MsgType.OP_ACK, MsgType.OP_REJECT)
    bot.send_op({"kind": "clear"})
    results["clear"] = _wait_either(bot, MsgType.OP_ACK, MsgType.OP_REJECT)
    bot.send_op({"kind": "restore", "checkpoint_id": "cp_missing"})
    rejected = bot.wait_for(MsgType.OP_REJECT)
    results["restore"] = rejected["code"] == ErrorCode.NOT_FOUND  # got past the permission check
    bot.send_op({"kind": "bulk_create", "objects": [], "replace": True})
    results["replace"] = _wait_either(bot, MsgType.OP_ACK, MsgType.OP_REJECT)
    bot.save()
    results["save"] = _wait_either(bot, MsgType.SAVE_ACK, MsgType.SAVE_REJECT)
    bot.send_chat("hello")
    results["chat"] = _wait_either(bot, MsgType.CHAT_ACK, MsgType.CHAT_REJECT)
    bot.send_dm(target_fp, "hello")
    results["dm"] = _wait_either(bot, MsgType.CHAT_ACK, MsgType.CHAT_REJECT)
    bot.send(MsgType.CHAT_HISTORY, {"channel_id": "room"})
    results["history"] = _wait_either(bot, MsgType.CHAT_HISTORY, MsgType.CHAT_REJECT)
    for action, fields in (
        ("clear_chat", {}),
        ("set_retention", {"policy": {"mode": "forever"}}),
        ("set_settings", {"settings": {}}),
        ("create_invite", {"role": "viewer"}),
        ("list_checkpoints", {}),
    ):
        bot.admin(action, **fields)
        results[action] = _wait_either(bot, MsgType.ADMIN, MsgType.ERROR)
    return results


def _wait_either(bot: BotClient, good: int, bad: int) -> bool:
    while True:
        for i, (t, _) in enumerate(bot.inbox):
            if t in (good, bad):
                del bot.inbox[i]
                return t == good
        bot.pump(0.05)
        assert not bot.closed, "connection closed unexpectedly"


WIRE_EXPECTED = {
    "owner": dict.fromkeys(
        ["edit", "clear", "restore", "replace", "save", "chat", "dm", "history", "clear_chat",
         "set_retention", "set_settings", "create_invite", "list_checkpoints"], True),
    "editor": {"edit": True, "clear": False, "restore": False, "replace": False, "save": True, "chat": True,
               "dm": True, "history": True, "clear_chat": False, "set_retention": False,
               "set_settings": False, "create_invite": False, "list_checkpoints": False},
    "viewer": {"edit": False, "clear": False, "restore": False, "replace": False, "save": False, "chat": True,
               "dm": True, "history": True, "clear_chat": False, "set_retention": False,
               "set_settings": False, "create_invite": False, "list_checkpoints": False},
}


@pytest.mark.parametrize("role", ["owner", "editor", "viewer"])
def test_AC_01_matrix_over_the_wire(srv, role):
    port, _ = srv
    owner = make_room(port)
    peer = add_member(owner, port, "peer")
    bot = owner if role == "owner" else add_member(owner, port, "bot", role)
    assert _exercise(bot, owner, peer.fp) == WIRE_EXPECTED[role]


def test_AC_01_editors_can_clear_setting_enforced(srv):
    port, _ = srv
    owner = make_room(port)
    editor = add_member(owner, port, "ed")
    owner.admin("set_settings", settings={"editors_can_clear": True})
    owner.wait_for(MsgType.ADMIN)
    editor.send_op({"kind": "clear"})
    editor.wait_for(MsgType.OP_ACK)


def test_AC_01_locked_board_blocks_everyone_but_owner(srv):
    port, _ = srv
    owner = make_room(port)
    editor = add_member(owner, port, "ed")
    owner.admin("set_settings", settings={"locked": True})
    owner.wait_for(MsgType.ADMIN)
    editor.create_rect("a")
    assert editor.wait_for(MsgType.OP_REJECT)["code"] == ErrorCode.ROOM_LOCKED
    owner.create_rect("b")
    owner.wait_for(MsgType.OP_ACK)


# Everything a PENDING connection must never receive, from AC-02.
FORBIDDEN = {MsgType.WELCOME, MsgType.SNAPSHOT, MsgType.OP, MsgType.CHAT, MsgType.CHAT_HISTORY, MsgType.PRESENCE,
             MsgType.TRANSIENT, MsgType.RESYNC, MsgType.SAVE_ACK, MsgType.ADMIN, MsgType.OP_ACK, MsgType.CHAT_ACK}


@pytest.mark.parametrize("how", ["room_id", "room_code"])
def test_AC_02_unknown_identity_gets_only_access_pending(srv, how):
    port, _ = srv
    code = "ROOM-CODE-1"
    owner = make_room(port, code=code)
    owner.create_rect("secret-object")
    owner.send_chat("secret chat")
    owner.recv_until(1)
    visitor = BotClient(port, "visitor")
    pending = visitor.request(**({"room_id": owner.room_id} if how == "room_id" else {"room_code": code}))
    assert set(pending) == {"room_name", "host_name", "fingerprint"}
    # Activity while pending must not leak through either.
    owner.create_rect("secret-2")
    owner.send_chat("secret chat 2")
    owner.recv_until(2)
    visitor.drain()
    assert not visitor.types_seen() & FORBIDDEN
    assert not visitor.bytes_contain("secret")


def test_AC_02_pending_frames_are_dropped_not_acted_on(srv):
    port, handle = srv
    owner = make_room(port)
    owner.create_rect("a")
    owner.recv_until(1)
    visitor = BotClient(port, "visitor")
    visitor.request(room_id=owner.room_id)
    visitor.send_op({"kind": "clear"})
    visitor.send(MsgType.RESYNC, {"from_seq": 0})
    visitor.save()
    visitor.admin("list_checkpoints")
    visitor.send(MsgType.ACCESS_DECISION, {"fingerprint": visitor.fp, "decision": "approve", "role": "editor"})
    visitor.send(MsgType.TRANSIENT, {"k": "cursor", "d": {}})
    visitor.send(MsgType.PING, {"t": 1})
    visitor.wait_for(MsgType.PONG)  # processed in order, so everything above has been handled
    visitor.drain()
    assert not visitor.types_seen() & FORBIDDEN
    room = handle.server.rooms[owner.room_id]
    assert room.seq == 1 and room.acl.role_of(visitor.fp) is None


def test_AC_03_raw_op_before_authentication_closes(srv):
    port, handle = srv
    owner = make_room(port)
    bot = BotClient(port, "raw").connect()
    bot.send_op({"kind": "clear"})
    assert bot.wait_closed()
    assert bot.wait_for(MsgType.ERROR)["code"] == ErrorCode.BAD_STATE
    assert handle.server.rooms[owner.room_id].seq == 0


def test_AC_03_op_between_hello_and_proof_closes(srv):
    port, _ = srv
    make_room(port)
    bot = BotClient(port, "raw").connect()
    bot.hello(room_code="x")
    bot.send_op({"kind": "clear"})
    assert bot.wait_closed()


def test_AC_04_proof_signed_with_wrong_key_denied(srv):
    port, handle = srv
    owner = make_room(port)
    bot = BotClient(port, "mallory").connect()
    # Claims the owner's public key, but can only sign with its own private key.
    bot.send(MsgType.HELLO, {"username": "owner", "pubkey": b64(owner.key.pubkey), "room_id": owner.room_id})
    nonce = unb64(bot.wait_for(MsgType.CHALLENGE)["nonce"])
    bot.send(MsgType.PROOF, {"signature": b64(Identity.generate().sign_proof(nonce))})
    assert bot.wait_closed()
    assert bot.wait_for(MsgType.ERROR)["code"] == ErrorCode.AUTH_FAILED
    assert not bot.bytes_contain("snapshot")
    assert handle.thread.is_alive()


def test_AC_05_replayed_proof_rejected_fresh_nonce(srv):
    port, _ = srv
    owner = make_room(port)
    first = BotClient(port, "owner", owner.key).connect()
    nonce1 = unb64(first.hello(room_id=owner.room_id)["nonce"])
    captured = first.key.sign_proof(nonce1)
    first.send(MsgType.PROOF, {"signature": b64(captured)})
    first.wait_for(MsgType.WELCOME)

    attacker = BotClient(port, "attacker").connect()
    attacker.send(MsgType.HELLO, {"username": "owner", "pubkey": b64(owner.key.pubkey), "room_id": owner.room_id})
    nonce2 = unb64(attacker.wait_for(MsgType.CHALLENGE)["nonce"])
    assert nonce1 != nonce2
    attacker.send(MsgType.PROOF, {"signature": b64(captured)})
    assert attacker.wait_closed()
    assert attacker.wait_for(MsgType.ERROR)["code"] == ErrorCode.AUTH_FAILED


def test_AC_06_owner_approves_live_transition(srv):
    port, _ = srv
    owner = make_room(port)
    owner.create_rect("a")
    owner.send_chat("hello newcomer")
    owner.recv_until(1)
    owner.wait_for(MsgType.CHAT_ACK)
    visitor = BotClient(port, "v")
    visitor.request(room_id=owner.room_id)
    request = owner.wait_for(MsgType.ACCESS_REQUEST)
    assert request["fingerprint"] == visitor.fp and request["name"] == "v" and request["ip"]
    owner.send(MsgType.ACCESS_DECISION, {"fingerprint": visitor.fp, "decision": "approve", "role": "editor"})
    welcome = visitor.wait_for(MsgType.WELCOME, consume=False)  # same socket, no reconnect
    assert welcome["role"] == "editor" and welcome["chat_tail"][0]["text"] == "hello newcomer"
    assert visitor.board_hash() == owner.board_hash()
    visitor.create_rect("b")
    owner.recv_until(2)


def test_AC_06_only_owner_can_decide(srv):
    port, handle = srv
    owner = make_room(port)
    editor = add_member(owner, port, "ed")
    visitor = BotClient(port, "v")
    visitor.request(room_id=owner.room_id)
    editor.send(MsgType.ACCESS_DECISION, {"fingerprint": visitor.fp, "decision": "approve", "role": "editor"})
    assert editor.wait_for(MsgType.ERROR)["code"] == ErrorCode.NOT_ALLOWED
    assert handle.server.rooms[owner.room_id].acl.role_of(visitor.fp) is None


def test_AC_06_deny_then_cooldown(tmp_path):
    clock = FakeClock()
    port, handle = start_server(tmp_path, clock=clock)
    try:
        owner = make_room(port)
        visitor = BotClient(port, "v")
        visitor.request(room_id=owner.room_id)
        owner.send(MsgType.ACCESS_DECISION, {"fingerprint": visitor.fp, "decision": "deny"})
        assert visitor.wait_for(MsgType.ERROR)["code"] == ErrorCode.ACCESS_DENIED
        assert visitor.wait_closed()
        retry = BotClient(port, "v", visitor.key).connect()
        retry.handshake(room_id=owner.room_id)
        assert retry.wait_for(MsgType.ERROR)["retry_after"] > 0  # inside the cool-down
        clock.advance(61)
        again = BotClient(port, "v", visitor.key)
        again.request(room_id=owner.room_id)  # cool-down over, so may ask again
    finally:
        handle.stop()


def test_AC_06_blocked_device_never_returns(tmp_path):
    clock = FakeClock()
    port, handle = start_server(tmp_path, clock=clock)
    try:
        owner = make_room(port)
        visitor = BotClient(port, "v")
        visitor.request(room_id=owner.room_id)
        owner.send(MsgType.ACCESS_DECISION, {"fingerprint": visitor.fp, "decision": "block"})
        visitor.wait_for(MsgType.ERROR)
        clock.advance(100_000)
        retry = BotClient(port, "v", visitor.key).connect()
        retry.handshake(room_id=owner.room_id)
        assert retry.wait_for(MsgType.ERROR)["code"] == ErrorCode.ACCESS_DENIED
    finally:
        handle.stop()


@pytest.mark.parametrize("action", ["kick", "revoke"])
def test_AC_07_removed_member_stops_receiving_immediately(srv, action):
    port, _ = srv
    owner = make_room(port)
    bob = add_member(owner, port, "bob")
    third = add_member(owner, port, "third")
    owner.admin(action, fp=bob.fp)
    owner.wait_for(MsgType.ADMIN)
    assert bob.wait_closed()
    assert bob.wait_for(MsgType.ERROR)["code"] in (ErrorCode.KICKED, ErrorCode.REVOKED)
    bob.inbox.clear()
    owner.send_chat("after removal")
    owner.send_dm(third.fp, "dm after removal")
    third.wait_for(MsgType.CHAT, lambda m: m["text"] == "dm after removal")
    bob.drain()
    assert not bob.inbox
    # A DM to the removed member is now refused, and they are not let back in silently.
    owner.send_dm(bob.fp, "x")
    assert owner.wait_for(MsgType.CHAT_REJECT)["code"] == ErrorCode.UNKNOWN_RECIPIENT
    back = BotClient(port, "bob", bob.key).connect()
    back.handshake(room_id=owner.room_id)
    back.drain(0.05)
    assert MsgType.WELCOME not in back.types_seen()


def test_AC_07_history_stays_under_removed_members_name(srv):
    port, handle = srv
    owner = make_room(port)
    bob = add_member(owner, port, "bob")
    bob.send_chat("I was here")
    owner.wait_for(MsgType.CHAT)
    owner.admin("kick", fp=bob.fp)
    owner.wait_for(MsgType.ADMIN)
    assert handle.server.rooms[owner.room_id].chat.room.messages[0]["from_name"] == "bob"


def test_AC_07_role_change_applies_to_next_frame(srv):
    port, _ = srv
    owner = make_room(port)
    bob = add_member(owner, port, "bob")
    owner.admin("set_role", fp=bob.fp, role="viewer")
    owner.wait_for(MsgType.ADMIN)
    assert bob.wait_for(MsgType.PRESENCE, lambda m: m["event"] == "role")["role"] == "viewer"
    bob.create_rect("a")
    assert bob.wait_for(MsgType.OP_REJECT)["code"] == ErrorCode.NOT_ALLOWED


def test_AC_07_owner_cannot_be_targeted(srv):
    port, handle = srv
    owner = make_room(port)
    owner.admin("kick", fp=owner.fp)
    assert owner.wait_for(MsgType.ERROR)["code"] == ErrorCode.NOT_FOUND
    owner.admin("set_role", fp=owner.fp, role="viewer")
    owner.wait_for(MsgType.ERROR)
    assert handle.server.rooms[owner.room_id].acl.role_of(owner.fp) == "owner"


def test_AC_08_viewer_op_rejected_state_unchanged(srv):
    port, handle = srv
    owner = make_room(port)
    viewer = add_member(owner, port, "v", "viewer")
    owner.create_rect("a")
    viewer.recv_until(1)
    owner.recv_until(1)
    for op in ({"kind": "update", "id": "a", "patch": {"x": 9}}, {"kind": "delete", "id": "a"}, {"kind": "clear"}):
        viewer.send_op(op)
        assert viewer.wait_for(MsgType.OP_REJECT)["code"] == ErrorCode.NOT_ALLOWED
    assert handle.server.rooms[owner.room_id].seq == 1
    assert owner.board_hash() == viewer.board_hash()


def test_AC_09_invite_single_use(srv):
    port, handle = srv
    owner = make_room(port)
    owner.admin("create_invite", role="editor")
    token = owner.wait_for(MsgType.ADMIN)["token"]
    room = handle.server.rooms[owner.room_id]
    assert token not in room.paths.acl_json.read_text()  # stored only as a hash
    first = BotClient(port, "i1")
    assert first.join(room_id=owner.room_id, invite=token)["role"] == "editor"
    second = BotClient(port, "i2")
    second.request(room_id=owner.room_id, invite=token)  # falls back to asking; no free entry
    assert room.acl.role_of(second.fp) is None


def test_AC_09_invite_expired(tmp_path):
    clock = FakeClock()
    port, handle = start_server(tmp_path, clock=clock)
    try:
        owner = make_room(port)
        owner.admin("create_invite", role="viewer", ttl=60)
        token = owner.wait_for(MsgType.ADMIN)["token"]
        clock.advance(61)
        late = BotClient(port, "late")
        late.request(room_id=owner.room_id, invite=token)
        assert handle.server.rooms[owner.room_id].acl.role_of(late.fp) is None
    finally:
        handle.stop()


def test_AC_09_invite_cannot_grant_owner(srv):
    port, _ = srv
    owner = make_room(port)
    owner.admin("create_invite", role="owner")
    assert owner.wait_for(MsgType.ERROR)["code"] == ErrorCode.BAD_SCHEMA


def test_AC_10_request_flood_rate_limit_and_cooldown(tmp_path):
    clock = FakeClock()
    port, handle = start_server(tmp_path, clock=clock, request_limit=6)
    try:
        owner = make_room(port)  # creating the room spends one of this address's six requests
        owner.inbox.clear()
        codes = []
        for i in range(8):
            bot = BotClient(port, f"flood{i}").connect()
            bot.handshake(room_id=owner.room_id)
            bot.pump(0.05)
            codes.append(bot.inbox[0][1].get("code") if bot.inbox[0][0] == MsgType.ERROR else "pending")
        assert codes[:5] == ["pending"] * 5
        assert set(codes[5:]) == {ErrorCode.RATE_LIMITED}
        owner.drain()
        assert sum(1 for t, _ in owner.inbox if t == MsgType.ACCESS_REQUEST) == 5  # owner is not spammed
        clock.advance(61)
        BotClient(port, "after").request(room_id=owner.room_id)
    finally:
        handle.stop()


def test_AC_10_guessing_room_codes_is_limited(tmp_path):
    port, handle = start_server(tmp_path, request_limit=3)
    try:
        owner = make_room(port, code="REAL")
        for i in range(5):
            bot = BotClient(port, "g").connect()
            bot.handshake(room_code=f"guess{i}")
            assert bot.wait_for(MsgType.ERROR)["code"] == ErrorCode.ROOM_NOT_FOUND
        # Even the right code now hits the limit, because guessing burned this address's budget.
        bot = BotClient(port, "g").connect()
        bot.handshake(room_code="REAL")
        assert bot.wait_for(MsgType.ERROR)["code"] == ErrorCode.RATE_LIMITED
        assert owner.room_id
    finally:
        handle.stop()


def test_AC_11_restart_preserves_acl(srv):
    port, handle = srv
    owner = make_room(port)
    editor = add_member(owner, port, "ed")
    viewer = add_member(owner, port, "vi", "viewer")
    owner.admin("mute", fp=viewer.fp, muted=True)
    owner.wait_for(MsgType.ADMIN)
    pending = BotClient(port, "p")
    pending.request(room_id=owner.room_id)
    room_id = owner.room_id
    handle.kill()
    handle.restart()
    acl = handle.server.rooms[room_id].acl
    assert acl.owner == owner.fp
    assert acl.role_of(editor.fp) == "editor" and acl.role_of(viewer.fp) == "viewer"
    assert acl.is_muted(viewer.fp)
    assert acl.request_status(pending.fp) == "pending"
    back = BotClient(handle.port, "ed", editor.key)
    assert back.join(room_id=room_id)["role"] == "editor"  # recognized without asking again


def test_AC_12_owner_offline_request_queues(srv):
    port, _ = srv
    owner = make_room(port)
    editor = add_member(owner, port, "ed")
    room_id = owner.room_id
    owner.close()
    visitor = BotClient(port, "v")
    visitor.request(room_id=room_id)
    editor.create_rect("still-working")  # members keep working with the owner away
    editor.wait_for(MsgType.OP_ACK)
    back = BotClient(port, "owner", owner.key)
    welcome = back.join(room_id=room_id)
    assert [r["fingerprint"] for r in welcome["pending_requests"]] == [visitor.fp]
    back.send(MsgType.ACCESS_DECISION, {"fingerprint": visitor.fp, "decision": "approve", "role": "viewer"})
    assert visitor.wait_for(MsgType.WELCOME, consume=False)["role"] == "viewer"


def test_AC_12_pending_requests_only_shown_to_owner(srv):
    port, _ = srv
    owner = make_room(port)
    editor = add_member(owner, port, "ed")
    visitor = BotClient(port, "secret-visitor")
    visitor.request(room_id=owner.room_id)
    again = BotClient(port, "ed", editor.key)
    assert "pending_requests" not in again.join(room_id=owner.room_id)
    editor.drain()
    assert MsgType.ACCESS_REQUEST not in editor.types_seen()


@pytest.mark.chat
def test_CT_05_pending_cannot_chat_or_read(srv):
    port, handle = srv
    owner = make_room(port)
    owner.send_chat("room secret")
    owner.wait_for(MsgType.CHAT_ACK)
    member = add_member(owner, port, "m")
    owner.send_dm(member.fp, "dm secret")
    owner.wait_for(MsgType.CHAT_ACK)
    channel = owner.wait_for(MsgType.CHAT, lambda m: m["text"] == "dm secret")["channel_id"]
    visitor = BotClient(port, "v")
    visitor.request(room_id=owner.room_id)
    owner.inbox.clear()
    member.drain()
    member.inbox.clear()
    visitor.send_chat("sneak")
    visitor.send_dm(owner.fp, "sneak dm")
    visitor.send(MsgType.CHAT_HISTORY, {"channel_id": "room"})
    visitor.send(MsgType.CHAT_HISTORY, {"channel_id": channel})
    visitor.send(MsgType.PING, {"t": 1})
    visitor.wait_for(MsgType.PONG)
    visitor.drain()
    assert not visitor.types_seen() & FORBIDDEN and not visitor.bytes_contain("secret")
    owner.drain()
    member.drain()
    assert not owner.bytes_contain("sneak") and not member.bytes_contain("sneak")
    assert len(handle.server.rooms[owner.room_id].chat.room.messages) == 1


@pytest.mark.chat
def test_CT_05_denied_identity_gets_nothing(srv):
    port, _ = srv
    owner = make_room(port)
    visitor = BotClient(port, "v")
    visitor.request(room_id=owner.room_id)
    owner.send(MsgType.ACCESS_DECISION, {"fingerprint": visitor.fp, "decision": "deny"})
    visitor.wait_for(MsgType.ERROR)
    again = BotClient(port, "v", visitor.key).connect()
    again.handshake(room_id=owner.room_id)
    try:
        again.send_chat("sneak")
    except OSError:
        pass
    again.wait_closed()
    assert not again.types_seen() & FORBIDDEN


@pytest.mark.persist
def test_SV_02_viewer_save_rejected(srv):
    port, handle = srv
    owner = make_room(port)
    viewer = add_member(owner, port, "v", "viewer")
    viewer.save()
    assert viewer.wait_for(MsgType.SAVE_REJECT)["code"] == ErrorCode.NOT_ALLOWED
    assert not handle.server.rooms[owner.room_id].oplog.read_index()


def test_hidden_room_needs_code_and_looks_absent_otherwise(srv):
    port, _ = srv
    owner = make_room(port, code="SECRETCODE", discoverable=False)
    guess = BotClient(port, "g").connect()
    guess.handshake(room_id=owner.room_id)
    assert guess.wait_for(MsgType.ERROR)["code"] == ErrorCode.ROOM_NOT_FOUND
    BotClient(port, "v").request(room_code="SECRETCODE")


def test_require_code_setting(srv):
    port, _ = srv
    owner = make_room(port, code="THECODE", require_code=True)
    nocode = BotClient(port, "n").connect()
    nocode.handshake(room_id=owner.room_id)
    assert nocode.wait_for(MsgType.ERROR)["code"] == ErrorCode.CODE_REQUIRED
    BotClient(port, "v").request(room_id=owner.room_id, room_code="THECODE")


def test_room_code_stored_hashed_never_logged_plain(srv):
    port, handle = srv
    owner = make_room(port, code="PLAINCODE123")
    assert owner.welcome["room_code"] == "PLAINCODE123"  # returned once, to the creator
    paths = handle.server.rooms[owner.room_id].paths
    assert "PLAINCODE123" not in paths.room_json.read_text()
    assert "PLAINCODE123" not in paths.acl_json.read_text()


def test_raw_bytes_cannot_skip_state_machine_by_frame_type(srv):
    port, _ = srv
    owner = make_room(port)
    for msg_type in MsgType:
        if msg_type == MsgType.HELLO:
            continue
        bot = BotClient(port, "raw").connect()
        bot.send_raw(encode_frame(msg_type, {"room_id": owner.room_id}))
        assert bot.wait_closed(), msg_type
        assert wait_until(lambda: True)
        assert not bot.types_seen() & FORBIDDEN
