"""Server view of the durable writer, plus the on-disk layout (Architecture.md section 12.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from common.durable import (  # noqa: F401  (re-exported for server modules)
    AppendLog,
    atomic_write,
    atomic_write_json,
    read_json_with_bak,
    rewrite_log,
    scan_log,
    truncate_log,
)

KEEP_SNAPSHOTS = 10


def iso_utc(timestamp: float) -> str:
    """Server timestamps are UTC ISO-8601 on the wire; clients format them in local time."""
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="milliseconds")


def parse_iso(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


class RoomPaths:
    """Every path inside one room's data directory, so no other module builds them by hand."""

    def __init__(self, data_dir: Path, room_id: str) -> None:
        self.root = data_dir / "rooms" / room_id
        self.room_json = self.root / "room.json"
        self.acl_json = self.root / "acl.json"
        self.ops = self.root / "ops.jsonl"
        self.chat_dir = self.root / "chat"
        self.dm_dir = self.chat_dir / "dm"
        self.room_chat = self.chat_dir / "room.jsonl"
        self.read_json = self.chat_dir / "read.json"
        self.chat_meta = self.chat_dir / "meta.json"
        self.snapshots = self.root / "snapshots"
        self.checkpoints = self.root / "checkpoints"
        self.checkpoint_index = self.checkpoints / "index.json"
        self.recovery_log = self.root / "recovery.log"

    def dm_file(self, channel_id: str) -> Path:
        # channel_id was validated as "dm:" + 16 hex digits, so this cannot escape the directory.
        return self.dm_dir / (channel_id.split(":", 1)[1] + ".jsonl")

    def snapshot_file(self, seq: int) -> Path:
        return self.snapshots / f"{seq:012d}.json.gz"

    def checkpoint_file(self, seq: int) -> Path:
        return self.checkpoints / f"{seq:012d}.json.gz"
