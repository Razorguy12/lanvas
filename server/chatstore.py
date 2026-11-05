"""Room and DM channels: ordering, persistence, history and retention (Architecture.md 4.5).

Each channel has its own ``chat_seq`` and its own CRC-checked log. Messages are also kept in memory:
at lab scale that is a few megabytes at most, and it makes history paging trivially correct.

DM privacy depends on one rule, enforced here: :meth:`ChatStore.channel_for_reader` is the only way
to read a channel, and it refuses anyone who is not one of the channel's two fingerprints.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from common import chat as c
from server.persist import (
    AppendLog,
    RoomPaths,
    atomic_write_json,
    iso_utc,
    parse_iso,
    read_json_with_bak,
    rewrite_log,
    scan_log,
    truncate_log,
)

DAY = 86400.0


class Channel:
    def __init__(self, channel_id: str, path: Any) -> None:
        self.channel_id = channel_id
        self.path = path
        self.seq = 0
        self.messages: list[dict[str, Any]] = []
        self.by_msg_id: dict[str, int] = {}
        self.participants: tuple[str, str] | None = None
        self._log: AppendLog | None = None

    def log(self) -> AppendLog:
        if self._log is None:
            self._log = AppendLog(self.path)
        return self._log

    def sync(self) -> None:
        if self._log is not None:
            self._log.sync()

    def close(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    def load(self) -> str | None:
        """Load the log, truncating a torn tail. Returns a repair note if anything was fixed."""
        entries, good, bad = scan_log(self.path)
        note = None
        if bad:
            truncate_log(self.path, good)
            note = f"chat {self.channel_id}: truncated torn tail, kept {len(entries)} messages"
        for seq, data in entries:
            self.messages.append(data)
            self.by_msg_id[data["msg_id"]] = seq
            self.seq = max(self.seq, seq)
        if self.messages and self.channel_id != c.CHANNEL_ROOM:
            first = self.messages[0]
            self.participants = (first["from_fp"], first["to_fp"])
        return note

    def page(self, before: int | None, limit: int) -> list[dict[str, Any]]:
        candidates = self.messages
        if before is not None:
            candidates = [m for m in candidates if m["chat_seq"] < before]
        return candidates[-limit:]


class ChatStore:
    def __init__(self, paths: RoomPaths, clock: Callable[[], float]) -> None:
        self.paths = paths
        self.clock = clock
        self.channels: dict[str, Channel] = {}
        self.read: dict[str, dict[str, int]] = {}
        self.room = self._add(c.CHANNEL_ROOM, paths.room_chat)
        self.repairs: list[str] = []

    def _add(self, channel_id: str, path: Any) -> Channel:
        channel = Channel(channel_id, path)
        self.channels[channel_id] = channel
        return channel

    def load(self) -> list[str]:
        """Rebuild from disk. Returns repair notes for ``recovery.log``."""
        self.repairs = []
        if self.paths.dm_dir.exists():
            for path in sorted(self.paths.dm_dir.glob("*.jsonl")):
                self._add(c.DM_PREFIX + path.stem, path)
        for channel in self.channels.values():
            note = channel.load()
            if note:
                self.repairs.append(note)
        # High-water marks survive pruning. Otherwise clearing a channel and restarting would reuse
        # chat_seq values, and clients that dedupe by chat_seq would drop new messages as
        # duplicates.
        meta, _ = read_json_with_bak(self.paths.chat_meta)
        for channel_id, seq in (meta or {}).items():
            if channel_id in self.channels:
                self.channels[channel_id].seq = max(self.channels[channel_id].seq, seq)
        read, _ = read_json_with_bak(self.paths.read_json)
        self.read = read or {}
        return self.repairs

    def _save_meta(self) -> None:
        atomic_write_json(
            self.paths.chat_meta, {cid: ch.seq for cid, ch in self.channels.items()}, keep_bak=True
        )

    def _dm(self, fp_a: str, fp_b: str) -> Channel:
        channel_id = c.dm_channel_id(fp_a, fp_b)
        channel = self.channels.get(channel_id)
        if channel is None:
            channel = self._add(channel_id, self.paths.dm_file(channel_id))
            channel.participants = (fp_a, fp_b)
        return channel

    def duplicate_seq(self, channel_kind: str, sender: str, to: str | None, msg_id: str) -> Any:
        """If msg_id was already accepted, return (channel_id, chat_seq) for a repeat ack."""
        channel = self.room if channel_kind == c.CHANNEL_ROOM else self._dm_if_exists(sender, to)
        if channel is None:
            return None
        seq = channel.by_msg_id.get(msg_id)
        return (channel.channel_id, seq) if seq is not None else None

    def _dm_if_exists(self, fp_a: str, fp_b: str | None) -> Channel | None:
        return self.channels.get(c.dm_channel_id(fp_a, fp_b)) if fp_b else None

    def append(
        self,
        channel_kind: str,
        sender_fp: str,
        sender_name: str,
        to_fp: str | None,
        text: str,
        msg_id: str,
        reply_to: int | None,
    ) -> dict[str, Any]:
        """Assign chat_seq, stamp server time, write through the durable log."""
        channel = self.room if channel_kind == c.CHANNEL_ROOM else self._dm(sender_fp, to_fp or "")
        seq = channel.seq + 1
        message: dict[str, Any] = {
            "channel_id": channel.channel_id,
            "chat_seq": seq,
            "from_fp": sender_fp,
            "from_name": sender_name,
            "ts": iso_utc(self.clock()),
            "text": text,
            "msg_id": msg_id,
        }
        if to_fp:
            message["to_fp"] = to_fp
        if reply_to is not None:
            message["reply_to"] = reply_to
        # Disk first, then memory. If the write raises, no sequence number was consumed.
        channel.log().append(seq, message)
        channel.seq = seq
        channel.messages.append(message)
        channel.by_msg_id[msg_id] = seq
        return message

    def channel_for_reader(self, channel_id: Any, reader_fp: str) -> Channel | None:
        """The access check for chat history. A non-participant gets the same None as a bad id."""
        channel = self.channels.get(channel_id) if isinstance(channel_id, str) else None
        if channel is None:
            return None
        if channel.channel_id == c.CHANNEL_ROOM:
            return channel
        return channel if channel.participants and reader_fp in channel.participants else None

    def mark_read(self, fingerprint: str, channel_id: str, seq: int) -> None:
        positions = self.read.setdefault(fingerprint, {})
        if seq > positions.get(channel_id, 0):
            positions[channel_id] = seq
            atomic_write_json(self.paths.read_json, self.read, keep_bak=True)

    def unread(self, fingerprint: str, channel: Channel) -> int:
        position = self.read.get(fingerprint, {}).get(channel.channel_id, 0)
        return sum(
            1 for m in channel.messages if m["chat_seq"] > position and m["from_fp"] != fingerprint
        )

    def tail(self) -> list[dict[str, Any]]:
        return self.room.messages[-c.TAIL_SIZE :]

    def dm_index(
        self, fingerprint: str, members: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Per-peer summaries for WELCOME, built only from channels this fingerprint belongs to."""
        index = []
        for channel in self.channels.values():
            if not channel.participants or fingerprint not in channel.participants:
                continue
            if not channel.messages:
                continue
            a, b = channel.participants
            peer = b if a == fingerprint else a
            last = channel.messages[-1]
            index.append(
                {
                    "channel_id": channel.channel_id,
                    "peer_fp": peer,
                    "peer_name": members.get(peer, {}).get("name", "Former member"),
                    "last_preview": last["text"][: c.PREVIEW_CHARS],
                    "last_ts": last["ts"],
                    "last_seq": last["chat_seq"],
                    "unread": self.unread(fingerprint, channel),
                }
            )
        index.sort(key=lambda entry: entry["last_ts"], reverse=True)
        return index

    def clear_room(self) -> None:
        """Owner clears room chat. DM channels are deliberately untouched (CT-10)."""
        self.room.close()
        self.room.messages = []
        rewrite_log(self.room.path, [])
        self._save_meta()

    def prune(self, policy: dict[str, Any]) -> None:
        """Apply retention to every channel. Runs at startup and hourly."""
        mode = policy.get("mode")
        if mode == c.RETENTION_FOREVER:
            return
        self._save_meta()  # must precede the rewrite so a crash cannot lose the high-water mark
        for channel in self.channels.values():
            kept = self._apply_policy(channel.messages, mode, policy.get("value", 0))
            if len(kept) != len(channel.messages):
                channel.close()
                channel.messages = kept
                rewrite_log(channel.path, [(m["chat_seq"], m) for m in kept])

    def _apply_policy(
        self, messages: list[dict[str, Any]], mode: Any, value: int
    ) -> list[dict[str, Any]]:
        if mode == c.RETENTION_LAST_N:
            return messages[-value:]
        if mode == c.RETENTION_DAYS:
            cutoff = self.clock() - value * DAY
            return [m for m in messages if parse_iso(m["ts"]) >= cutoff]
        return messages

    def sync(self) -> None:
        for channel in self.channels.values():
            channel.sync()

    def close(self) -> None:
        for channel in self.channels.values():
            channel.close()
