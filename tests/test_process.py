"""Real OS processes, real kill -9: the crash the demo performs, not a simulated one."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from common.protocol import MsgType
from tests.harness import BotClient, add_member, make_room, wait_until

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parent.parent
KILL = signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(data: Path, port: int) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-m", "server.main", "--host", "127.0.0.1", "--port", str(port), "--data", str(data), "--no-discovery"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        stderr=subprocess.PIPE,
        text=True,
    )

    def accepting() -> bool:
        import socket

        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            return False

    assert wait_until(accepting, 10), "server did not start"
    return process


@pytest.mark.persist
def test_RC_05_CT_08_real_sigkill_preserves_board_chat_and_dms(tmp_path):
    port = _free_port()
    process = _start(tmp_path, port)
    try:
        owner = make_room(port, "Sprint-1")
        bob = add_member(owner, port, "Bob")
        for i in range(30):
            owner.create_rect(f"o{i}", x=i)
        owner.send_chat("room message")
        owner.send_dm(bob.fp, "private message")
        owner.wait_for(MsgType.CHAT, lambda m: m["text"] == "private message")
        owner.recv_until(30)
        room_id, before = owner.room_id, owner.board_hash()
        process.send_signal(KILL)  # no flush, no handlers, nothing gets to run
        process.wait(10)
        process = _start(tmp_path, port)
        back = BotClient(port, "Bob", bob.key)
        welcome = back.join(room_id=room_id)
        assert back.board_hash() == before
        assert [m["text"] for m in welcome["chat_tail"]] == ["room message"]
        assert welcome["dm_index"][0]["last_preview"] == "private message"
        assert welcome["dm_index"][0]["unread"] == 1
    finally:
        process.terminate()
        process.communicate(timeout=10)


def test_server_prints_addresses_and_integrity_summary(tmp_path):
    """Operators rely on this output, so its contents are part of the behavior."""
    port = _free_port()
    process = _start(tmp_path, port)
    owner = make_room(port, "R")
    owner.create_rect("a")
    owner.recv_until(1)
    process.send_signal(KILL)
    process.wait(10)
    process = _start(tmp_path, port)
    process.terminate()
    _, err = process.communicate(timeout=10)
    assert "integrity scan" in err and "listening on TCP" in err
    assert f"room {owner.room_id}: seq=1 hash=sha256:" in err
    assert "code" not in err.lower().replace("error code", "")


@pytest.mark.skipif(not (ROOT / "dist" / "lanvas-client").exists(), reason="built executables are not present")
def test_XP_03_built_executables_full_flow(tmp_path):
    """The packaged binaries, with no Python environment: launch, discover, join, draw, save, export."""
    suffix = ".exe" if os.name == "nt" else ""
    server_exe = ROOT / "dist" / "lanvas-server" / f"lanvas-server{suffix}"
    client_exe = ROOT / "dist" / "lanvas-client" / f"lanvas-client{suffix}"
    tcp, udp = _free_port(), _free_port()
    # Stripping PYTHONPATH and virtualenv variables proves the bundle needs nothing from outside.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME")}
    server = subprocess.Popen(
        [str(server_exe), "--port", str(tcp), "--udp-port", str(udp), "--data", str(tmp_path / "data")],
        cwd=tmp_path, env=env, stderr=subprocess.PIPE, text=True,
    )
    try:
        result = subprocess.run(
            [str(client_exe), "--smoke", "127.0.0.1", str(tcp), str(tmp_path / "out"), "--udp-port", str(udp)],
            cwd=tmp_path,
            env={**env, "QT_QPA_PLATFORM": "offscreen", "LANVAS_HOME": str(tmp_path / "home")},
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SMOKE OK" in result.stdout
        for name in ("s.png", "s.jpg", "s.svg", "s.pdf"):
            assert (tmp_path / "out" / name).stat().st_size > 0
    finally:
        server.terminate()
        server.communicate(timeout=10)
