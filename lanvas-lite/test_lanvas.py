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
           "--data", str(tmp_path), "--autosave", "1", "--grace", "1", "--log-level", "WARNING"]
    p = start(cmd, port)
    yield port, cmd, tmp_path
    p.terminate()
    p.wait()


def start(cmd, port):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return p


def rooms(port):
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    s.sendall(pack({"t": "list"}))
    return {r["code"]: r for r in recv_msg(s)["rooms"]}


class Bot:
    def __init__(self, port, user, room=None, password="", create=False, role="editor", token="",
                 private=False, recover=True):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=3)
        hello = {"t": "hello", "user": user, "token": token, "room": room or ""}
        if create:
            salt = os.urandom(16).hex() if password else ""
            hello.update(create=True, role=role, salt=salt, key=derive_key(password, salt) if password else "",
                         private=private)
        self.s.sendall(pack(hello))
        m = recv_msg(self.s)
        if m["t"] == "challenge":
            self.s.sendall(pack({"t": "auth", "proof": proof(derive_key(password, m["salt"]), m["nonce"])}))
            m = recv_msg(self.s)
        self.asked = None
        if m["t"] == "recover":
            self.asked = m
            self.s.sendall(pack({"t": "recover", "use": recover}))
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
    time.sleep(2.5)                                 # autosave (1 s in tests)
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


def ids(objs):
    return [o["id"] for o in objs]


def test_fill_and_undo(server):
    port, *_ = server
    a = Bot(port, "alice", create=True)
    a.send(t="op", op={"k": "add", "obj": {"id": "r1", "k": "rect", "c": "#000000", "w": 2, "p": [0, 0, 50, 50]}})
    a.send(t="op", op={"k": "fill", "id": "r1", "f": "#ff0000"})
    assert a.until(is_op("fill"))["op"]["f"] == "#ff0000"
    a.send(t="op", op={"k": "fill", "id": "r1", "f": "red"})
    assert a.until(lambda m: m["t"] == "error")["code"] == "NOT_ALLOWED"
    a.send(t="undo")
    assert a.until(is_op("fill"))["op"]["f"] == ""


def test_area_erase_is_one_undoable_op(server):
    port, *_ = server
    a = Bot(port, "alice", create=True)
    b = Bot(port, "bob", room=a.first["room"])
    add(a, "s1")
    piece = stroke("p1", (1, 2, 2, 3))
    a.send(t="op", op={"k": "repl", "rid": "g1", "del": ["s1"], "add": [piece]})
    got = b.until(is_op("repl"))["op"]
    assert got["del"] == ["s1"] and ids(got["add"]) == ["p1"]
    a.send(t="op", op={"k": "repl", "rid": "g1", "del": ["s1"], "add": [piece]})   # duplicate
    a.until(lambda m: m["t"] == "ack" and m["id"] == "r:g1")
    a.send(t="undo")
    back = b.until(is_op("repl"))["op"]
    assert back["del"] == ["p1"] and ids(back["add"]) == ["s1"]


def test_erase_pieces_geometry():
    from common import erase_pieces
    line = {"k": "line", "w": 2, "p": [0, 0, 100, 0]}
    parts = erase_pieces(line, 50, 0, 5)
    assert len(parts) == 2 and max(parts[0][0::2]) < 45 and min(parts[1][0::2]) > 55
    assert erase_pieces(line, 50, 40, 5) is None                     # not touched
    assert erase_pieces({"k": "text", "w": 2, "p": [0, 0]}, 0, 0, 5) is None
    assert len(erase_pieces({"k": "rect", "w": 1, "p": [0, 0, 100, 100]}, 50, 0, 5)) == 1


def test_room_list_hides_private_rooms(server):
    port, *_ = server
    a, b = Bot(port, "alice", create=True), Bot(port, "bob", create=True, private=True)
    pub, priv = a.first["room"], b.first["room"]
    listed = rooms(port)
    assert pub in listed and priv not in listed and listed[pub]["online"] == 1
    assert Bot(port, "carol", room=priv).first["t"] == "welcome"      # still joinable by code


def test_private_messages(server):
    port, *_ = server
    a = Bot(port, "alice", create=True)
    code = a.first["room"]
    b, c = Bot(port, "bob", room=code), Bot(port, "carol", room=code)
    a.send(t="chat", text="psst", to="bob")
    assert b.until(lambda m: m["t"] == "chat" and m["m"]["text"] == "psst")["m"]["to"] == "bob"
    a.send(t="chat", text="all")
    c.until(lambda m: m["t"] == "chat" and m["m"]["text"] == "all")
    c.send(t="resync")
    assert all(x["text"] != "psst" for x in c.until(lambda m: m["t"] == "snap")["chat"])
    b.send(t="resync")
    assert any(x["text"] == "psst" for x in b.until(lambda m: m["t"] == "snap")["chat"])


def test_custom_code_and_closed_room_recovery(server):
    port, *_ = server
    a = Bot(port, "alice", create=True, room="MYROOM")
    assert a.first["room"] == "MYROOM"
    assert Bot(port, "x", create=True, room="MYROOM").first["code"] == "ROOM_EXISTS"
    add(a, "k1")
    a.until(is_op("add"))
    a.s.close()
    time.sleep(7)                                    # grace (1 s) passes on a 1 s housekeeping tick
    assert "MYROOM" not in rooms(port)               # closed rooms are not listed
    b = Bot(port, "bob", room="MYROOM")              # reopen: asked, chooses the saved version
    assert b.asked and b.asked["objects"] == 1 and ids(b.first["objects"]) == ["k1"]
    b.s.close()
    time.sleep(7)
    c = Bot(port, "alice", room="MYROOM", create=True, token=a.token, recover=False)   # start fresh
    assert c.asked and c.first["objects"] == [] and c.first["host"] == "alice"


def test_server_crash_recovers_open_rooms(server):
    port, cmd, _ = server
    cmd = cmd[:3] + [str(port + 2)] + cmd[4:]        # our own server instance, so we can crash it
    p = start(cmd, port + 2)
    a = Bot(port + 2, "alice", create=True)
    code = a.first["room"]
    add(a, "c1")
    a.until(is_op("add"))
    time.sleep(2.5)                                  # let an autosave happen
    p.kill()                                         # hard crash: no shutdown save
    p.wait()
    p = start(cmd, port + 2)
    try:
        assert code in rooms(port + 2)               # restored OPEN, not waiting for a reopen prompt
        b = Bot(port + 2, "alice", room=code, token=a.token)
        assert b.asked is None and ids(b.first["objects"]) == ["c1"]
    finally:
        p.terminate()
        p.wait()


PNG_1PX = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


def test_image_objects(server):
    port, *_ = server
    a = Bot(port, "alice", create=True)
    img = {"id": "i1", "k": "image", "c": "#000000", "w": 1, "p": [10, 10], "s": PNG_1PX}
    a.send(t="op", op={"k": "add", "obj": img})
    assert a.until(is_op("add"))["op"]["obj"]["s"] == PNG_1PX          # not truncated like text
    a.send(t="op", op={"k": "add", "obj": {**img, "id": "i2", "s": "not base64!"}})
    assert a.until(lambda m: m["t"] == "error")["code"] == "NOT_ALLOWED"


def test_clients_told_when_server_stops_and_restarts(server):
    port, cmd, _ = server
    cmd = cmd[:3] + [str(port + 3)] + cmd[4:]
    p = start(cmd, port + 3)
    a = Bot(port + 3, "alice", create=True)
    code, boot = a.first["room"], a.first["boot"]
    add(a, "z1")
    a.until(is_op("add"))
    p.terminate()                                    # clean stop: saves latest state and says bye
    assert a.until(lambda m: m["t"] == "bye")
    p.wait()
    p = start(cmd, port + 3)
    try:
        b = Bot(port + 3, "alice", room=code, token=a.token)
        assert b.first["boot"] != boot and ids(b.first["objects"]) == ["z1"] and b.first["saved_at"]
    finally:
        p.terminate()
        p.wait()
