"""Client crash recovery: the journal and session lock (Architecture.md section 12.4).

While a board is open, the journal holds the last confirmed board plus an append log of un-acked ops
and unsent chat. ``session-<pid>.lock`` exists for exactly as long as the client runs, so a lock
whose
process is gone proves a crash, and :func:`find_crashed` offers that session for recovery.

Disk writes happen on a dedicated worker thread, because the UI thread must never block on I/O.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.durable import (  # noqa: F401  (the "mirror" of server/persist.py named by the spec)
    AppendLog,
    atomic_write,
    atomic_write_json,
    read_json_with_bak,
    scan_log,
    truncate_log,
)
from common.model import Board
from common.snapshot import decode_snapshot, encode_snapshot

log = logging.getLogger(__name__)

JOURNAL_EVERY_SECONDS = 30.0
JOURNAL_EVERY_OPS = 200

E_OP_ADD = "op+"
E_OP_DONE = "op-"
E_CHAT_ADD = "chat+"
E_CHAT_DONE = "chat-"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # Never os.kill on Windows: there, signal 0 would actually terminate the process.
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class IoWorker:
    """A single background thread running file jobs in order, off the UI thread."""

    def __init__(self) -> None:
        self._jobs: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="lanvas-io", daemon=True)
        self._thread.start()

    def submit(self, job: Callable[[], None]) -> None:
        self._jobs.put(job)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                job()
            except OSError:
                log.exception("background write failed")

    def flush(self, timeout: float = 5.0) -> None:
        done = threading.Event()
        self.submit(done.set)
        done.wait(timeout)

    def stop(self) -> None:
        self._jobs.put(None)
        self._thread.join(5)


@dataclass
class CrashedSession:
    directory: Path
    room_id: str
    room_name: str
    host: str
    port: int
    seq: int
    board: dict[str, Any]
    ops: list[dict[str, Any]] = field(default_factory=list)
    chat: list[dict[str, Any]] = field(default_factory=list)


class Journal:
    """The open board's recovery files."""

    def __init__(self, root: Path, room_id: str, room_name: str, host: str, port: int) -> None:
        self.dir = root / room_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.dir / f"session-{os.getpid()}.lock"
        self.meta = {"room_id": room_id, "room_name": room_name, "host": host, "port": port}
        atomic_write_json(self.dir / "meta.json", self.meta)
        atomic_write_json(self.lock_path, {"pid": os.getpid()})
        self._log = AppendLog(self.dir / "pending.jsonl")
        self._entry_seq = max((s for s, _ in scan_log(self.dir / "pending.jsonl")[0]), default=0)
        self._since_snapshot = 0
        self._last_snapshot = 0.0
        self.worker = IoWorker()

    def _append(self, entry: dict[str, Any]) -> None:
        self._entry_seq += 1
        seq = self._entry_seq

        def job() -> None:
            self._log.append(seq, entry)
            self._log.sync()

        self.worker.submit(job)

    def op_added(self, op: dict[str, Any]) -> None:
        self._append({"e": E_OP_ADD, "op": op})

    def op_done(self, op_id: str) -> None:
        self._since_snapshot += 1
        self._append({"e": E_OP_DONE, "id": op_id})

    def chat_added(self, payload: dict[str, Any]) -> None:
        self._append({"e": E_CHAT_ADD, "msg": payload})

    def chat_done(self, msg_id: str) -> None:
        self._append({"e": E_CHAT_DONE, "id": msg_id})

    def snapshot_due(self, now: float) -> bool:
        return self._since_snapshot >= JOURNAL_EVERY_OPS or (
            now - self._last_snapshot >= JOURNAL_EVERY_SECONDS
        )

    def write_snapshot(self, board: Board, seq: int, now: float) -> None:
        """Persist the confirmed board. The board is copied here, on the caller's thread."""
        self._since_snapshot = 0
        self._last_snapshot = now
        raw = encode_snapshot(board, self.meta["room_id"], seq, "")
        self.worker.submit(lambda: atomic_write(self.dir / "board.json.gz", raw))

    def close(self, clean: bool = True) -> None:
        """A clean exit removes the journal; without this the next launch would see a crash."""
        self.worker.flush()
        self.worker.stop()
        self._log.close()
        if clean:
            shutil.rmtree(self.dir, ignore_errors=True)


def _read_pending(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries, good, bad = scan_log(path)
    if bad:
        truncate_log(path, good)
    ops: dict[str, dict[str, Any]] = {}
    chat: dict[str, dict[str, Any]] = {}
    for _, entry in entries:
        kind = entry.get("e")
        if kind == E_OP_ADD:
            ops[entry["op"]["op_id"]] = entry["op"]
        elif kind == E_OP_DONE:
            ops.pop(entry["id"], None)
        elif kind == E_CHAT_ADD:
            chat[entry["msg"]["msg_id"]] = entry["msg"]
        elif kind == E_CHAT_DONE:
            chat.pop(entry["id"], None)
    return list(ops.values()), list(chat.values())


def find_crashed(root: Path) -> list[CrashedSession]:
    """Sessions whose lock outlived its process. A live second client is correctly not a crash."""
    found: list[CrashedSession] = []
    if not root.exists():
        return found
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        locks = list(directory.glob("session-*.lock"))
        if not locks:
            continue
        alive = False
        for lock in locks:
            try:
                alive = alive or pid_alive(json.loads(lock.read_text())["pid"])
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if alive:
            continue
        meta, _ = read_json_with_bak(directory / "meta.json")
        if not isinstance(meta, dict):
            continue
        board: dict[str, Any] = {"objects": {}}
        seq = 0
        try:
            doc = decode_snapshot((directory / "board.json.gz").read_bytes())
            if doc is not None:
                board, seq = doc["board"], doc["seq"]
        except OSError:
            pass
        ops, chat = _read_pending(directory / "pending.jsonl")
        found.append(
            CrashedSession(
                directory,
                meta.get("room_id", directory.name),
                meta.get("room_name", ""),
                meta.get("host", ""),
                meta.get("port", 0),
                seq,
                board,
                ops,
                chat,
            )
        )
    return found


def discard_crashed(session: CrashedSession) -> None:
    shutil.rmtree(session.directory, ignore_errors=True)
