"""The op log: append-only history, automatic snapshots, and Ctrl+S checkpoints.

Board state is a pure function of the log, so ``ops.jsonl`` is the real source of truth. Snapshots
only speed up startup and the replay of very old clients.
"""

from __future__ import annotations

from typing import Any

from common.fileformat import board_hash, decode_snapshot, encode_snapshot
from common.model import Board
from server.persist import (
    KEEP_SNAPSHOTS,
    AppendLog,
    RoomPaths,
    atomic_write,
    atomic_write_json,
    iso_utc,
    read_json_with_bak,
)

SNAPSHOT_EVERY_OPS = 200
SNAPSHOT_EVERY_SECONDS = 120.0


class OpLog:
    def __init__(self, paths: RoomPaths) -> None:
        self.paths = paths
        self._log: AppendLog | None = None
        self.last_snapshot_seq = 0
        self.last_snapshot_time = 0.0

    def log(self) -> AppendLog:
        if self._log is None:
            self._log = AppendLog(self.paths.ops)
        return self._log

    def append(self, seq: int, op: dict[str, Any]) -> None:
        self.log().append(seq, op)

    def sync(self) -> None:
        if self._log is not None:
            self._log.sync()

    @property
    def dirty(self) -> bool:
        return self._log is not None and self._log.dirty

    def close(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    def snapshot_due(self, seq: int, now: float) -> bool:
        if seq == self.last_snapshot_seq:
            return False
        if seq - self.last_snapshot_seq >= SNAPSHOT_EVERY_OPS:
            return True
        return now - self.last_snapshot_time >= SNAPSHOT_EVERY_SECONDS

    def write_snapshot(self, board: Board, room_id: str, seq: int, now: float) -> None:
        self.sync()  # a snapshot must never be ahead of the durable log it summarizes
        raw = encode_snapshot(board, room_id, seq, iso_utc(now))
        atomic_write(self.paths.snapshot_file(seq), raw)
        self.last_snapshot_seq = seq
        self.last_snapshot_time = now
        files = sorted(self.paths.snapshots.glob("*.json.gz"))
        for old in files[:-KEEP_SNAPSHOTS]:
            old.unlink(missing_ok=True)

    # --- checkpoints ---
    def read_index(self) -> list[dict[str, Any]]:
        index, _ = read_json_with_bak(self.paths.checkpoint_index)
        return index if isinstance(index, list) else []

    def write_checkpoint(
        self, board: Board, room_id: str, seq: int, now: float, saved_by: dict[str, str], label: Any
    ) -> dict[str, Any]:
        self.sync()
        when = iso_utc(now)
        atomic_write(self.paths.checkpoint_file(seq), encode_snapshot(board, room_id, seq, when))
        entry = {
            "checkpoint_id": f"cp_{seq}",
            "seq": seq,
            "board_hash": board_hash(board),
            "saved_by": saved_by,
            "label": label if isinstance(label, str) else "",
            "time": when,
        }
        # Saving twice with no edits yields one entry, not a list of identical versions.
        index = [e for e in self.read_index() if e["checkpoint_id"] != entry["checkpoint_id"]]
        index.append(entry)
        atomic_write_json(self.paths.checkpoint_index, index, keep_bak=True)
        return entry

    def load_checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        """Return the verified checkpoint document, or None if unknown or corrupted."""
        for entry in self.read_index():
            if entry["checkpoint_id"] == checkpoint_id:
                try:
                    raw = self.paths.checkpoint_file(entry["seq"]).read_bytes()
                except OSError:
                    return None
                return decode_snapshot(raw)
        return None

    def delete_checkpoint(self, checkpoint_id: str) -> bool:
        index = self.read_index()
        remaining = [e for e in index if e["checkpoint_id"] != checkpoint_id]
        if len(remaining) == len(index):
            return False
        atomic_write_json(self.paths.checkpoint_index, remaining, keep_bak=True)
        for entry in index:
            if entry["checkpoint_id"] == checkpoint_id:
                self.paths.checkpoint_file(entry["seq"]).unlink(missing_ok=True)
        return True
