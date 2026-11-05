"""Chat state: channels, ordered messages, unread counts, history paging and the outbox.

Messages are keyed by ``chat_seq``, so duplicates vanish, out-of-order delivery sorts itself out,
and
history pages merge with live traffic without special cases.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any

from common.chat import CHANNEL_DM, CHANNEL_ROOM, MAX_HISTORY_PAGE, dm_channel_id

SENDING = "sending"
FAILED = "failed"


@dataclass
class ChatChannel:
    channel_id: str
    peer_fp: str | None = None
    peer_name: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    seqs: list[int] = field(default_factory=list)
    unread: int = 0
    read_upto: int = 0
    has_more: bool = True
    loading: bool = False
    last_ts: str = ""
    preview: str = ""
    remote_last_seq: int = 0

    def insert(self, message: dict[str, Any]) -> bool:
        seq = message["chat_seq"]
        i = bisect.bisect_left(self.seqs, seq)
        if i < len(self.seqs) and self.seqs[i] == seq:
            return False
        self.seqs.insert(i, seq)
        self.messages.insert(i, message)
        if message["ts"] >= self.last_ts:
            self.last_ts = message["ts"]
            self.preview = message["text"][:60]
        return True

    @property
    def is_private(self) -> bool:
        return self.peer_fp is not None

    def first_gap(self) -> int | None:
        """The seq just above the newest hole in what is held, or None if contiguous."""
        for i in range(len(self.seqs) - 1, 0, -1):
            if self.seqs[i] != self.seqs[i - 1] + 1:
                return self.seqs[i]
        return None


@dataclass
class Outgoing:
    msg_id: str
    channel_id: str
    payload: dict[str, Any]
    text: str
    state: str = SENDING
    ts: float = 0.0
    error: str = ""


class ChatModel:
    def __init__(self, my_fp: str = "") -> None:
        self.my_fp = my_fp
        self.channels: dict[str, ChatChannel] = {CHANNEL_ROOM: ChatChannel(CHANNEL_ROOM)}
        self.outbox: dict[str, Outgoing] = {}
        self.active = CHANNEL_ROOM
        self.counter = 0
        self.client_id = "local"

    def next_msg_id(self) -> str:
        self.counter += 1
        return f"{self.client_id}:m{self.counter}"

    @property
    def room(self) -> ChatChannel:
        return self.channels[CHANNEL_ROOM]

    def total_unread(self) -> int:
        return sum(ch.unread for ch in self.channels.values())

    def sorted_channels(self) -> list[ChatChannel]:
        """The room first, then private conversations by most recent message."""
        dms = [ch for ch in self.channels.values() if ch.is_private]
        dms.sort(key=lambda ch: ch.last_ts, reverse=True)
        return [self.room, *dms]

    def load_welcome(self, welcome: dict[str, Any]) -> None:
        """Merge retained history. On reconnect this leaves local outbox and scrollback intact."""
        room = self.room
        room.read_upto = max(room.read_upto, welcome.get("room_read", 0))
        for message in welcome.get("chat_tail", []):
            room.insert(message)
        latest = welcome.get("room_chat_seq", 0)
        room.remote_last_seq = latest
        room.unread = sum(
            1
            for m in room.messages
            if m["chat_seq"] > room.read_upto and m["from_fp"] != self.my_fp
        )
        room.has_more = bool(room.seqs) and room.seqs[0] > 1
        for entry in welcome.get("dm_index", []):
            channel = self.ensure_dm(entry["peer_fp"], entry.get("peer_name", ""))
            channel.unread = entry.get("unread", 0)
            channel.last_ts = max(channel.last_ts, entry.get("last_ts", ""))
            channel.preview = entry.get("last_preview", "")
            channel.remote_last_seq = entry.get("last_seq", 0)

    def ensure_dm(self, peer_fp: str, peer_name: str = "") -> ChatChannel:
        channel_id = dm_channel_id(self.my_fp, peer_fp)
        channel = self.channels.get(channel_id)
        if channel is None:
            channel = ChatChannel(channel_id, peer_fp=peer_fp, peer_name=peer_name)
            self.channels[channel_id] = channel
        elif peer_name:
            channel.peer_name = peer_name
        return channel

    def receive(self, message: dict[str, Any]) -> bool:
        """Handle a live CHAT. Returns True if it was new, which is when the UI should react."""
        channel_id = message["channel_id"]
        channel = self.channels.get(channel_id)
        if channel is None:
            peer = message["to_fp"] if message["from_fp"] == self.my_fp else message["from_fp"]
            name = "" if message["from_fp"] == self.my_fp else message.get("from_name", "")
            channel = self.ensure_dm(peer, name)
        self.outbox.pop(message.get("msg_id", ""), None)
        if not channel.insert(message):
            return False
        channel.remote_last_seq = max(channel.remote_last_seq, message["chat_seq"])
        if message["from_fp"] != self.my_fp and channel_id != self.active:
            channel.unread += 1
        return True

    def compose(self, channel_id: str, text: str) -> Outgoing:
        channel = self.channels[channel_id]
        msg_id = self.next_msg_id()
        payload: dict[str, Any] = {"msg_id": msg_id, "text": text}
        if channel.is_private:
            payload |= {"channel": CHANNEL_DM, "to": channel.peer_fp}
        else:
            payload["channel"] = CHANNEL_ROOM
        out = Outgoing(msg_id, channel_id, payload, text)
        self.outbox[msg_id] = out
        return out

    def on_ack(self, ack: dict[str, Any]) -> None:
        # A duplicate ack (the message arrived before a disconnect) resolves the entry here.
        # A first-time ack is followed by the CHAT broadcast, which also clears it.
        if ack.get("duplicate"):
            self.outbox.pop(ack.get("msg_id", ""), None)

    def on_reject(self, msg_id: Any, code: str) -> Outgoing | None:
        out = self.outbox.get(msg_id) if isinstance(msg_id, str) else None
        if out is not None:
            out.state = FAILED
            out.error = code
        return out

    def retry(self, msg_id: str) -> Outgoing | None:
        out = self.outbox.get(msg_id)
        if out is not None:
            out.state = SENDING
            out.error = ""
        return out

    def outgoing_for(self, channel_id: str) -> list[Outgoing]:
        return [o for o in self.outbox.values() if o.channel_id == channel_id]

    def merge_history(self, page: dict[str, Any]) -> ChatChannel | None:
        channel = self.channels.get(page.get("channel_id"))
        if channel is None:
            return None
        for message in page.get("messages", []):
            channel.insert(message)
        channel.has_more = bool(page.get("has_more"))
        channel.loading = False
        return channel

    def history_request(self, channel: ChatChannel) -> dict[str, Any] | None:
        """The next older page, or None if already loading or at the beginning."""
        if channel.loading or not channel.has_more:
            return None
        channel.loading = True
        request: dict[str, Any] = {"channel_id": channel.channel_id, "limit": MAX_HISTORY_PAGE}
        if channel.seqs:
            request["before_chat_seq"] = channel.seqs[0]
        return request

    def gap_request(self, channel: ChatChannel) -> dict[str, Any] | None:
        """Ask for messages inside a hole, which appears when chat frames were lost mid-stream."""
        gap = channel.first_gap()
        if gap is None:
            return None
        return {"channel_id": channel.channel_id, "before_chat_seq": gap, "limit": MAX_HISTORY_PAGE}

    def mark_active(self, channel_id: str) -> dict[str, Any] | None:
        """Switch channel and clear its unread count; returns the CHAT_READ to send, if any."""
        self.active = channel_id
        channel = self.channels[channel_id]
        channel.unread = 0
        latest = channel.seqs[-1] if channel.seqs else channel.remote_last_seq
        if latest > channel.read_upto:
            channel.read_upto = latest
            return {"channel_id": channel_id, "chat_seq": latest}
        return None
