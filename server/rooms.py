"""A room: canonical board, sequence counter, ACL, chat, and its live connections."""

from __future__ import annotations

import secrets
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from common.model import Board, apply_op
from server import recovery
from server.acl import Acl, hash_secret
from server.chatstore import ChatStore
from server.oplog import OpLog
from server.persist import RoomPaths, atomic_write_json

REPLAY_WINDOW = 5000
MAX_ROOM_NAME = 80
MAX_DEDUPE = 200_000


def new_room_id() -> str:
    return "r_" + secrets.token_hex(8)


def new_room_code() -> str:
    return secrets.token_hex(3).upper()


class Room:
    """All state for one board. Only the single server thread ever touches this, so no locks."""

    def __init__(self, data_dir: Path, room_id: str, clock: Callable[[], float]) -> None:
        self.room_id = room_id
        self.clock = clock
        self.paths = RoomPaths(data_dir, room_id)
        self.name = ""
        self.code_hash = ""
        self.host_name = ""
        self.board = Board()
        self.seq = 0
        self.acl = Acl(self.paths.acl_json, clock)
        self.chat = ChatStore(self.paths, clock)
        self.oplog = OpLog(self.paths)
        self.recent_ops: deque[dict[str, Any]] = deque(maxlen=REPLAY_WINDOW)
        self.seen_op_ids: dict[str, int] = {}
        self.connections: set[Any] = set()

    # --- lifecycle ---
    @classmethod
    def create(
        cls,
        data_dir: Path,
        clock: Callable[[], float],
        name: str,
        code: str,
        owner_fp: str,
        owner_name: str,
        discoverable: bool = True,
        room_id: str | None = None,
    ) -> Room:
        room = cls(data_dir, room_id or new_room_id(), clock)
        room.name = name
        room.code_hash = hash_secret(code)
        room.host_name = owner_name
        room.acl.settings["discoverable"] = discoverable
        room.save_meta()
        room.acl.set_owner(owner_fp, owner_name)
        return room

    @classmethod
    def load(cls, data_dir: Path, room_id: str, clock: Callable[[], float]) -> Room | None:
        """Rebuild a room from disk, repairing what it can. None if the metadata is
        unrecoverable."""
        room = cls(data_dir, room_id, clock)
        repairs: list[str] = []
        meta = recovery.load_json_file(room.paths.room_json, repairs)
        acl = recovery.load_json_file(room.paths.acl_json, repairs)
        if not isinstance(meta, dict) or not isinstance(acl, dict) or "owner" not in acl:
            recovery.log.error("room %s: metadata unrecoverable, not loaded", room_id)
            return None
        room.name = meta.get("name", room_id)
        room.code_hash = meta.get("code_hash", "")
        room.host_name = meta.get("host_name", "")
        room.acl.load(acl)
        got = recovery.recover_board(room.paths, room_id, repairs)
        room.board = got.board
        room.seq = got.seq
        room.seen_op_ids = got.seen_op_ids
        room.recent_ops.extend(got.recent_ops)
        room.oplog.last_snapshot_seq = got.snapshot_seq
        room.oplog.last_snapshot_time = clock()
        repairs.extend(room.chat.load())
        room.chat.prune(room.acl.settings["chat_retention"])
        recovery.write_recovery_log(room.paths, room_id, room.seq, room.board, repairs, clock())
        return room

    def save_meta(self) -> None:
        atomic_write_json(
            self.paths.room_json,
            {
                "id": self.room_id,
                "name": self.name,
                "code_hash": self.code_hash,
                "host_name": self.host_name,
            },
            keep_bak=True,
        )

    def code_matches(self, code: Any) -> bool:
        return isinstance(code, str) and hash_secret(code) == self.code_hash

    # --- ops ---
    def apply_and_log(self, op: dict[str, Any]) -> dict[str, Any]:
        """Assign the next seq and commit: log first, so a crash cannot apply an unlogged op."""
        seq = self.seq + 1
        logged = {**op, "seq": seq}
        self.oplog.append(seq, logged)
        self.seq = seq
        apply_op(self.board, logged)
        self.recent_ops.append(logged)
        if "op_id" in logged:
            self.seen_op_ids[logged["op_id"]] = seq
            if len(self.seen_op_ids) > MAX_DEDUPE:
                # dict preserves insertion order, so this evicts the oldest ids.
                self.seen_op_ids.pop(next(iter(self.seen_op_ids)))
        return logged

    def ops_since(self, last_seq: int) -> list[dict[str, Any]] | None:
        """Ops after ``last_seq``, or None if that point has left the replay window."""
        if last_seq == self.seq:
            return []
        if last_seq > self.seq or not self.recent_ops:
            return None
        if self.recent_ops[0]["seq"] > last_seq + 1:
            return None
        return [op for op in self.recent_ops if op["seq"] > last_seq]

    def maybe_snapshot(self, now: float) -> None:
        if self.oplog.snapshot_due(self.seq, now):
            self.oplog.write_snapshot(self.board, self.room_id, self.seq, now)

    def sync(self) -> None:
        self.oplog.sync()
        self.chat.sync()

    def close(self) -> None:
        self.oplog.close()
        self.chat.close()
