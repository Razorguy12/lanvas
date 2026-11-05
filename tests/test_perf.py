"""PF-*: performance targets. These measure this machine over loopback, not a real LAN."""

from __future__ import annotations

import statistics
import threading
import time

import pytest

from client.canvas.view import CanvasView
from client.session import Session
from common.identity import Identity
from common.model import Board, apply_op
from common.protocol import MsgType
from tests.harness import add_member, make_room, start_server

pytestmark = pytest.mark.perf


def _latencies(bots, sender, count=200, pause=0.002):
    samples = []
    for i in range(count):
        start = time.perf_counter()
        sender.create_rect(f"p{i}", x=i)
        seq = sender.seq + 1
        for bot in bots:
            bot.recv_until(seq)
        samples.append(time.perf_counter() - start)
        time.sleep(pause)
    return samples


def _assert_targets(samples):
    median = statistics.median(samples)
    p95 = statistics.quantiles(samples, n=20)[18]
    print(f"\nmedian {median * 1000:.2f} ms, p95 {p95 * 1000:.2f} ms")
    assert median < 0.050 and p95 < 0.150


def test_PF_01_op_broadcast_latency_five_clients(tmp_path):
    port, handle = start_server(tmp_path)
    try:
        owner = make_room(port)
        bots = [owner] + [add_member(owner, port, f"b{i}") for i in range(4)]
        for bot in bots:
            bot.drain(0.005)
        _assert_targets(_latencies(bots, owner))
    finally:
        handle.stop()


def test_PF_06_batched_fsync_latency_under_load(tmp_path):
    """Five clients drawing at once, with durable logging on: PF-01's targets must still hold."""
    port, handle = start_server(tmp_path)
    try:
        owner = make_room(port)
        bots = [owner] + [add_member(owner, port, f"b{i}") for i in range(4)]
        for bot in bots:
            bot.drain(0.005)
        stop = threading.Event()

        def background(bot):
            n = 0
            while not stop.is_set():
                bot.send_op({"kind": "create", "id": f"{bot.name}-{n}", "type": "rect", "z": "V", "props": {"x": n}})
                n += 1
                time.sleep(0.01)  # one stroke commit every 10 ms is far busier than real drawing

        threads = [threading.Thread(target=background, args=(b,)) for b in bots[1:]]
        for t in threads:
            t.start()
        samples = []
        try:
            for i in range(150):
                start = time.perf_counter()
                op_id = owner.next_op_id()
                owner.send_op({"kind": "create", "id": f"probe{i}", "type": "rect", "z": "V", "props": {}}, op_id)
                owner.wait_for(MsgType.OP, lambda m, o=op_id: m.get("op_id") == o)
                samples.append(time.perf_counter() - start)
                owner.inbox.clear()
                time.sleep(0.005)
        finally:
            stop.set()
            for t in threads:
                t.join()
        _assert_targets(samples)
    finally:
        handle.stop()


def _big_board(count=1000):
    board = Board()
    for i in range(count):
        apply_op(
            board,
            {"kind": "create", "id": f"o{i:04d}", "type": "rect", "z": f"V{i:04d}".replace("0", "1"),
             "props": {"x": i % 50 * 40, "y": i // 50 * 40, "w": 30, "h": 30, "fill": "#0D99FF", "stroke": "#000000", "stroke_w": 1}},
        )
    return board


def _session():
    return Session(Identity.generate(), "me", send=lambda *a: None)


def test_PF_03_load_1000_object_snapshot_under_one_second(qapp):
    board = _big_board()
    session = _session()
    canvas = CanvasView(session)
    start = time.perf_counter()
    session.doc.load_snapshot(board.to_json(), 1000)
    canvas.sync(None)
    elapsed = time.perf_counter() - start
    print(f"\nloaded in {elapsed * 1000:.0f} ms")
    assert elapsed < 1.0 and len(canvas.board_scene.items_by_id) == 1000
    session.close()


def test_PF_04_pan_zoom_frame_rate_software_baseline(qapp):
    """Software rendering offscreen: a floor. A real display and GPU still need checking by eye."""
    session = _session()
    canvas = CanvasView(session)
    session.doc.load_snapshot(_big_board().to_json(), 1000)
    canvas.sync(None)
    canvas.resize(1280, 800)
    canvas.show()
    canvas.zoom_fit()
    frames = 60
    start = time.perf_counter()
    for i in range(frames):
        canvas.zoom_to(0.3 + (i % 10) * 0.02)
        canvas.translate(2, 1)
        canvas.grab()
    fps = frames / (time.perf_counter() - start)
    print(f"\n{fps:.0f} fps")
    assert fps >= 30
    canvas.close()
    session.close()


def test_PF_05_server_idle_cpu_with_five_clients(tmp_path):
    port, handle = start_server(tmp_path, discovery=True, discovery_port=0, discovery_broadcast=False)
    try:
        owner = make_room(port)
        for i in range(4):
            add_member(owner, port, f"b{i}")
        time.sleep(0.1)
        cpu_start, wall_start = time.process_time(), time.perf_counter()
        while time.perf_counter() - wall_start < 3.0:
            time.sleep(0.1)  # this process runs almost nothing else, so its CPU time is the server's
        share = (time.process_time() - cpu_start) / (time.perf_counter() - wall_start)
        print(f"\nserver CPU {share * 100:.2f}%")
        assert share < 0.05
    finally:
        handle.stop()
