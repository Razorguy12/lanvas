"""Protocol tests: start a real server process and talk to it with raw sockets.  Run:  python -m pytest -q"""
import os
import socket
import subprocess
import sys
import time

import pytest

from common import derive_key, pack, proof, recv_msg


@pytest.fixture()
def server(tmp_path):
    port = 40000 + os.getpid() % 2000
    cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "server.py"), "--port", str(port),
           "--data", str(tmp_path)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield port, cmd, tmp_path
    p.terminate()
    p.wait()


class Bot:
    def __init__(self, port, user, room=None, password="", create=False, role="editor", token=""):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=3)
        hello = {"t": "hello", "user": user, "token": token}
        if create:
            salt = os.urandom(16).hex() if password else ""
            hello.update(create=True, role=role, salt=salt, key=derive_key(password, salt) if password else "")
        else:
            hello["room"] = room
        self.s.sendall(pack(hello))
        m = recv_msg(self.s)
        if m["t"] == "challenge":
            self.s.sendall(pack({"t": "auth", "proof": proof(derive_key(password, m["salt"]), m["nonce"])}))
            m = recv_msg(self.s)
        self.first = m
        self.token = m.get("you", {}).get("token", "")

    def send(self, **m):
        self.s.sendall(pack(m))

    def until(self, pred):
        """Read messages until one matches (ignores pings, chat noise etc.)."""
        for _ in range(50):
            m = recv_msg(self.s)
            if pred(m):
                return m
        raise AssertionError("expected message never arrived")

    def op(self, t="op", **k):
        self.send(t=t, **k)


def stroke(i, pts=(1, 2, 3, 4)):
    return {"id": i, "k": "stroke", "c": "#112233", "w": 3, "p": list(pts), "s": ""}


def add(bot, i):
    bot.send(t="op", op={"k": "add", "obj": stroke(i)})


def is_op(kind):
    return lambda m: m["t"] == "op" and m["op"]["k"] == kind


def test_draw_is_ordered_and_broadcast(server):
    port, *_ = server
    a = Bot(port, "alice", create=True)
    code = a.first["room"]
    b = Bot(port, "bob", room=code)
    add(a, "s1")
    add(b, "s2")
    got = [b.until(is_op("add")) for _ in range(2)]
    assert [g["seq"] for g in got] == [1, 2]                      # one total order
    assert {g["op"]["obj"]["u"] for g in got} == {"alice", "bob"}  # authorship stamped by server
    seen_a = [a.until(is_op("add")) for _ in range(2)]
    assert [g["op"]["obj"]["id"] for g in seen_a] == [g["op"]["obj"]["id"] for g in got]


def test_new_joiner_gets_state_and_chat(server):
    port, *_ = server
    a = Bot(port, "alice", create=True)
    add(a, "s1")
    a.send(t="chat", text="hello")
    a.until(lambda m: m["t"] == "chat" and m["m"]["text"] == "hello")
    b = Bot(port, "bob", room=a.first["room"])
    w = b.first
    assert [o["id"] for o in w["objects"]] == ["s1"] and w["seq"] == 1
    assert any(c["text"] == "hello" and c["u"] == "alice" and c["ts"] for c in w["chat"])
    assert {u["name"] for u in w["users"]} == {"alice", "bob"}


def test_password_and_unknown_room(server):
    port, *_ = server
    a = Bot(port, "alice", create=True, password="secret")
    assert Bot(port, "bob", room=a.first["room"], password="secret").first["t"] == "welcome"
    assert Bot(port, "eve", room=a.first["room"], password="wrong").first["text"] == "Wrong password"
    assert Bot(port, "eve", room="NOPE00").first["code"] == "NO_ROOM"


def test_viewer_cannot_modify_and_only_host_clears(server):
    port, *_ = server
    a = Bot(port, "alice", create=True, role="viewer")      # newcomers join as viewers
    b = Bot(port, "bob", room=a.first["room"])
    add(b, "x")
    assert b.until(lambda m: m["t"] == "error")["code"] == "NOT_ALLOWED"
    a.send(t="role", user="bob", role="editor")
    b.until(lambda m: m["t"] == "users" and any(u["name"] == "bob" and u["role"] == "editor" for u in m["users"]))
    add(b, "x")
    b.until(is_op("add"))
    b.send(t="clear")
    assert b.until(lambda m: m["t"] == "error")["code"] == "NOT_ALLOWED"
    a.send(t="clear")
    assert b.until(is_op("clear"))["seq"] == 2


def test_duplicates_are_ignored_and_bad_input_rejected(server):
    port, *_ = server
    a = Bot(port, "alice", create=True)
    add(a, "d")
    add(a, "d")                                    # retransmitted duplicate
    a.send(t="op", op={"k": "add", "obj": {"id": "bad", "k": "stroke", "c": "red", "w": 3, "p": [1, 2]}})
    a.until(lambda m: m["t"] == "ack" and m["id"] == "d")
    assert a.until(lambda m: m["t"] == "error")["code"] == "NOT_ALLOWED"
    a.send(t="resync")
    assert a.until(lambda m: m["t"] == "snap")["seq"] == 1       # only one add was applied


def test_undo_redo_is_per_user(server):
    port, *_ = server
    a = Bot(port, "alice", create=True)
    b = Bot(port, "bob", room=a.first["room"])
    add(a, "a1")
    add(b, "b1")
    b.until(lambda m: m["t"] == "op" and m["seq"] == 2)
    a.send(t="undo")                                # undoes alice's stroke, not bob's (the latest)
    assert b.until(is_op("del"))["op"]["id"] == "a1"
    a.send(t="redo")
    assert b.until(is_op("add"))["op"]["obj"]["id"] == "a1"
    b.send(t="op", op={"k": "del", "id": "a1"})     # bob erases alice's stroke...
    b.until(is_op("del"))
    a.send(t="undo")                                # ...so alice's undo has nothing valid: skipped
    a.send(t="resync")
    snap = a.until(lambda m: m["t"] == "snap")
    assert [o["id"] for o in snap["objects"]] == ["b1"]


def test_save_restore_and_reconnect_with_token(server):
    port, *_ = server
    a = Bot(port, "alice", create=True)
    code = a.first["room"]
    add(a, "k1")
    a.send(t="save", name="v1")
    a.send(t="clear")
    a.until(is_op("clear"))
    a.send(t="restore", idx=0)
    assert [o["id"] for o in a.until(lambda m: m["t"] == "snap")["objects"]] == ["k1"]
    a.s.close()                                    # connection drops; room must survive
    time.sleep(0.2)
    a2 = Bot(port, "alice", room=code, token=a.token)
    assert a2.first["t"] == "welcome" and [o["id"] for o in a2.first["objects"]] == ["k1"]
    assert Bot(port, "alice", room=code).first["code"] == "NAME_TAKEN"      # no token: can't impersonate


def test_state_survives_server_restart(server):
    port, cmd, data = server
    a = Bot(port, "alice", create=True)
    code = a.first["room"]
    add(a, "p1")
    a.until(is_op("add"))
    time.sleep(6)                                   # autosave runs every 5 s
    assert any(f.endswith(".json") for f in os.listdir(data))
    p2 = subprocess.Popen(cmd[:3] + [str(port + 1)] + cmd[4:], stdout=subprocess.PIPE)
    try:
        for _ in range(50):
            try:
                b = Bot(port + 1, "alice", room=code, token=a.token)
                break
            except OSError:
                time.sleep(0.1)
        assert [o["id"] for o in b.first["objects"]] == ["p1"]
    finally:
        p2.terminate()
