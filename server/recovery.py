"""Startup integrity scan (Architecture.md section 12.4).

For each room: load metadata with ``.bak`` fallback, pick the newest snapshot or checkpoint whose
SHA-256 verifies, replay the log's CRC-valid tail, truncate torn writes, and record what happened.
Corruption is reported and repaired; it is never a reason to refuse to start.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from common.durable import read_json_with_bak, scan_log, truncate_log
from common.model import Board, apply_op
from common.snapshot import board_hash, decode_snapshot
from server.persist import RoomPaths, iso_utc

log = logging.getLogger("lanvas.recovery")
summary_log = logging.getLogger("lanvas.summary")


@dataclass
class RecoveredBoard:
    board: Board
    seq: int
    repairs: list[str] = field(default_factory=list)
    recent_ops: list[dict[str, Any]] = field(default_factory=list)
    seen_op_ids: dict[str, int] = field(default_factory=dict)
    snapshot_seq: int = 0


def load_json_file(path: Any, repairs: list[str]) -> Any | None:
    value, used_backup = read_json_with_bak(path)
    if used_backup:
        repairs.append(f"{path.name}: unreadable, restored from {path.name}.bak")
    return value


def _best_snapshot(paths: RoomPaths, repairs: list[str]) -> dict[str, Any] | None:
    """Newest verified board across automatic snapshots and Ctrl+S checkpoints."""
    candidates = []
    for directory in (paths.snapshots, paths.checkpoints):
        if directory.exists():
            candidates.extend(directory.glob("*.json.gz"))
    candidates.sort(key=lambda p: int(p.name.split(".")[0]), reverse=True)
    for path in candidates:
        try:
            doc = decode_snapshot(path.read_bytes())
        except OSError:
            doc = None
        if doc is not None:
            return doc
        repairs.append(f"snapshot {path.parent.name}/{path.name}: failed verification, skipped")
    return None


def recover_board(paths: RoomPaths, room_id: str, repairs: list[str]) -> RecoveredBoard:
    entries, good, bad = scan_log(paths.ops)
    if bad:
        truncate_log(paths.ops, good)
        repairs.append(f"ops.jsonl: truncated at byte {good}, kept {len(entries)} ops")

    snapshot = _best_snapshot(paths, repairs)
    board = Board.from_json(snapshot["board"]) if snapshot else Board()
    start_seq = snapshot["seq"] if snapshot else 0
    # A snapshot past the log tail means the log lost lines the snapshot already covers. The
    # snapshot is verified and newer, so it stands; there is simply nothing left to replay.
    seq = start_seq
    recovered = RecoveredBoard(board=board, seq=seq, snapshot_seq=start_seq)
    for op_seq, op in entries:
        # Every op id is remembered, including those before the snapshot, or a client re-sending
        # after the restart would get its op applied a second time.
        if "op_id" in op:
            recovered.seen_op_ids[op["op_id"]] = op_seq
        recovered.recent_ops.append(op)
        if op_seq > start_seq:
            apply_op(board, op)
            seq = op_seq
    recovered.seq = seq
    recovered.repairs = repairs
    return recovered


def write_recovery_log(
    paths: RoomPaths, room_id: str, seq: int, board: Board, repairs: list[str], now: float
) -> None:
    """Append to recovery.log and print the one-line per-room summary the operator sees."""
    digest = board_hash(board)
    status = f"{len(repairs)} repairs" if repairs else "clean"
    summary_log.info("room %s: seq=%d hash=%s %s", room_id, seq, digest[:19], status)
    if not repairs:
        return
    for repair in repairs:
        log.warning("room %s: %s", room_id, repair)
    paths.root.mkdir(parents=True, exist_ok=True)
    # recovery.log is advisory history, not state, so it is appended to rather than replaced.
    with open(paths.recovery_log, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{iso_utc(now)} seq={seq} hash={digest}\n")
        for repair in repairs:
            handle.write(f"  {repair}\n")
