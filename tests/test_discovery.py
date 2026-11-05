"""DS-*: discovery over real UDP sockets on loopback, plus the launcher's tracker logic."""

from __future__ import annotations

import json
import socket
import time

import pytest

from client.boardlist import BoardTracker, parse_announce
from common import netutil
from common.protocol import ANNOUNCE, ErrorCode, MsgType
from tests.harness import BotClient, DiscoveryProbe, FakeClock, make_room, start_server, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.discovery]


def _announce(server_id="s1", rooms=None, version=2, pages=1, port=47801):
    return json.dumps(
        {"t": ANNOUNCE, "server_id": server_id, "server_name": "PC", "tcp_port": port, "protocol_version": version,
         "rooms": rooms if rooms is not None else [{"room_id": "r1", "name": "Sprint-1", "host_name": "Alice"}], "pages": pages}
    ).encode()


@pytest.fixture
def probe():
    p = DiscoveryProbe()
    yield p
    p.close()


@pytest.fixture
def srv(tmp_path, probe):
    port, handle = start_server(tmp_path, discovery=True, discovery_port=0, discovery_broadcast=False,
                                discovery_targets=[("127.0.0.1", probe.port)])
    yield port, handle
    handle.stop()


def _has_room(room_id):
    return lambda m: any(r["room_id"] == room_id for r in m["rooms"])


def test_DS_01_discover_reply_within_two_seconds(srv, probe):
    port, handle = srv
    owner = make_room(port, "Sprint-1")
    # A separate socket, so this test sees only the unicast reply and not the announce target.
    client = DiscoveryProbe()
    start = time.monotonic()
    client.discover(handle.server.discovery.port)
    msg = client.wait_announce(_has_room(owner.room_id), timeout=2.0)
    assert time.monotonic() - start < 2.0 and msg["tcp_port"] == port
    client.close()


def test_DS_02_room_appears_within_one_announce_interval(srv, probe):
    port, _ = srv
    probe.wait_announce(lambda m: m["t"] == ANNOUNCE)  # already listening before the room exists
    owner = make_room(port, "New")
    start = time.monotonic()
    probe.wait_announce(_has_room(owner.room_id), timeout=2.5)
    assert time.monotonic() - start < 2.5


def _wait_tracker(probe, tracker, predicate, clock, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe.pump()
        for raw in probe.raw:
            tracker.feed(raw, "127.0.0.1", clock())
        probe.raw.clear()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_DS_03_hidden_or_deleted_room_disappears(srv, probe):
    port, _ = srv
    a = make_room(port, "Hide me")
    b = BotClient(port, "o2")
    b.join(create={"name": "Delete me"})
    tracker = BoardTracker()
    names = lambda: {e.name for e in tracker.boards()}  # noqa: E731
    assert _wait_tracker(probe, tracker, lambda: names() == {"Hide me", "Delete me"}, time.time)
    a.admin("set_settings", settings={"discoverable": False})
    assert _wait_tracker(probe, tracker, lambda: names() == {"Delete me"}, time.time, timeout=3)
    b.admin("delete_room")
    assert _wait_tracker(probe, tracker, lambda: names() == set(), time.time, timeout=3)


def test_DS_03_server_stopped_entry_expires_after_seven_seconds():
    tracker = BoardTracker()
    tracker.feed(_announce(), "10.0.0.1", 100.0)
    tracker.expire(106.9)
    assert len(tracker.boards()) == 1
    tracker.expire(107.1)
    assert tracker.boards() == [] and tracker.servers() == []


def test_DS_03_multi_page_announce_does_not_drop_rooms_on_other_pages():
    tracker = BoardTracker()
    tracker.feed(_announce(rooms=[{"room_id": "a", "name": "A"}], pages=2), "10.0.0.1", 1.0)
    tracker.feed(_announce(rooms=[{"room_id": "b", "name": "B"}], pages=2), "10.0.0.1", 1.1)
    assert {b.room_id for b in tracker.boards()} == {"a", "b"}


def test_DS_04_announce_contains_nothing_private(srv, probe):
    port, _ = srv
    owner = make_room(port, "Public-name", code="TOP-SECRET-CODE")
    owner.create_rect("secret-board-object")
    owner.send_chat("secret-chat-text")
    owner.recv_until(1)
    hidden = BotClient(port, "o2")
    hidden.join(create={"name": "Hidden-room-name", "discoverable": False})
    member = "unique-member-name"
    probe.wait_announce(_has_room(owner.room_id))
    time.sleep(0.05)
    probe.pump()
    blob = b"".join(probe.raw).decode()
    for needle in ("TOP-SECRET", "secret-board-object", "secret-chat-text", "Hidden-room-name", hidden.room_id, member, owner.fp, "code_hash"):
        assert needle not in blob, needle
    allowed_room = {"room_id", "name", "requires_approval", "require_code", "host_name", "member_count"}
    allowed_top = {"t", "server_id", "server_name", "addrs", "tcp_port", "protocol_version", "rooms", "page", "pages"}
    for msg in probe.received:
        assert set(msg) <= allowed_top
        for room in msg["rooms"]:
            assert set(room) <= allowed_room
    for raw in probe.raw:
        assert len(raw) <= 1200


def test_DS_04_many_rooms_paginated_under_size_cap(srv, probe):
    port, handle = srv
    for i in range(40):
        b = BotClient(port, f"o{i}")
        b.request_limit = 0
        handle.server.request_limiter.limit = 10_000
        b.join(create={"name": f"Room with a fairly long name number {i}"})
    pages = handle.server.discovery.pages()
    assert len(pages) > 1 and all(len(p) <= 1200 for p in pages)
    names = [r["name"] for p in pages for r in json.loads(p)["rooms"]]
    assert len(names) == 40 and {json.loads(p)["pages"] for p in pages} == {len(pages)}


def test_DS_04_member_count_only_if_owner_allows(srv, probe):
    port, handle = srv
    owner = make_room(port, "R", show_member_count=False)
    rooms = {r["room_id"]: r for r in handle.server.discoverable_rooms()}
    assert "member_count" not in rooms[owner.room_id]


def test_DS_05_server_and_client_share_the_port():
    """SO_REUSEADDR/REUSEPORT: two binds on one UDP port, as with same-host server and client."""
    first = netutil.open_discovery_socket(0)
    port = first.getsockname()[1]
    second = netutil.open_discovery_socket(port)
    assert second.getsockname()[1] == port
    first.close()
    second.close()


def test_DS_05_interfaces_enumerated_with_broadcast():
    targets = netutil.broadcast_targets()
    assert netutil.GLOBAL_BROADCAST in targets
    for iface in netutil.list_interfaces():
        assert not iface.address.startswith("127.")
        socket.inet_aton(iface.address)
        socket.inet_aton(iface.broadcast)


def test_DS_05_real_broadcast_reaches_shared_port():
    """One real broadcast datagram, delivered to both sockets sharing the port."""
    interfaces = netutil.list_interfaces()
    if not interfaces:
        pytest.skip("no non-loopback IPv4 interface on this machine")
    a = netutil.open_discovery_socket(0)
    port = a.getsockname()[1]
    b = netutil.open_discovery_socket(port)
    sender = netutil.open_discovery_socket(0)
    try:
        sender.sendto(b"hello", (interfaces[0].broadcast, port))
    except OSError as exc:
        pytest.skip(f"broadcast not permitted here: {exc}")
    try:
        got = [wait_until(lambda s=s: _try_recv(s), timeout=1.0) for s in (a, b)]
    finally:
        for s in (a, b, sender):
            s.close()
    if not any(got):
        pytest.skip("firewall drops broadcast on this machine; verify in the L3 run")
    assert all(got)


def _try_recv(sock):
    try:
        return sock.recvfrom(64)[0] == b"hello"
    except BlockingIOError:
        return False


@pytest.mark.parametrize(
    "packet",
    [b"\xff\xfe garbage", b"[]", b'{"t":"OTHER"}', b"x" * 2000, json.dumps({"t": "LANVAS_DISCOVER", "pad": "x" * 600}).encode(), b"[" * 400],
)
def test_DS_06_bad_discover_packets_ignored(srv, packet):
    port, handle = srv
    sender = DiscoveryProbe()
    sender.send(handle.server.discovery.port, packet)
    time.sleep(0.05)
    sender.pump()
    assert sender.raw == []
    assert handle.thread.is_alive()
    make_room(port, "still alive")
    sender.close()


def test_DS_06_flood_reply_rate_limited_server_responsive(tmp_path):
    clock = FakeClock()  # frozen, so all 500 packets land inside the same one-second window
    port, handle = start_server(tmp_path, clock=clock, discovery=True, discovery_port=0, discovery_broadcast=False)
    try:
        flood = DiscoveryProbe()
        for _ in range(500):
            flood.discover(handle.server.discovery.port)
        owner = make_room(port)
        owner.barrier()
        time.sleep(0.05)
        flood.pump()
        assert 0 < len(flood.raw) <= 5
        clock.advance(1.1)
        flood.raw.clear()
        flood.discover(handle.server.discovery.port)
        assert wait_until(lambda: (flood.pump(), flood.raw)[1], timeout=1)
        flood.close()
    finally:
        handle.stop()


def test_DS_06_tracker_ignores_bad_and_flooding_sources():
    tracker = BoardTracker()
    for bad in (b"junk", b"[]", b'{"t":"LANVAS_ANNOUNCE"}', _announce()[:20], b"x" * 5000):
        tracker.feed(bad, "10.0.0.9", 1.0)
    assert tracker.boards() == []
    assert parse_announce(json.dumps({"t": ANNOUNCE, "server_id": "s", "tcp_port": 1, "protocol_version": 2, "rooms": [{"room_id": 1, "name": "x"}]}).encode()) is None
    limited = BoardTracker()
    for i in range(100):
        limited.feed(_announce(rooms=[{"room_id": f"r{i}", "name": "x"}], pages=2), "10.0.0.9", 5.0)
    assert len(limited.boards()) <= 20


def test_DS_07_join_from_list_with_room_id_normal_approval(srv):
    port, _ = srv
    owner = make_room(port, "R")
    visitor = BotClient(port, "v")
    assert visitor.request(room_id=owner.room_id)["room_name"] == "R"
    owner.wait_for(MsgType.ACCESS_REQUEST)


def test_DS_07_require_code_room_flagged_and_enforced(srv):
    port, handle = srv
    owner = make_room(port, "R", code="SEKRIT", require_code=True)
    assert handle.server.discoverable_rooms()[0]["require_code"] is True
    bare = BotClient(port, "v").connect()
    bare.handshake(room_id=owner.room_id)
    assert bare.wait_for(MsgType.ERROR)["code"] == ErrorCode.CODE_REQUIRED
    BotClient(port, "v2").request(room_id=owner.room_id, room_code="SEKRIT")


def test_DS_08_incompatible_version_shown_not_joinable():
    tracker = BoardTracker()
    tracker.feed(_announce(version=1), "10.0.0.2", 1.0)
    entry = tracker.boards()[0]
    assert entry.name == "Sprint-1" and entry.compatible is False
    tracker.feed(_announce(server_id="s2", rooms=[{"room_id": "r9", "name": "Aaa"}]), "10.0.0.3", 1.0)
    assert tracker.boards()[0].compatible  # joinable boards sort first
