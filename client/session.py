"""One open room: glues transport frames to the document, chat model, journal and saving.

Plain logic with no Qt. The UI (or a test) calls ``handle_frame`` from a single thread and reacts to
``emit`` events. Every board and chat mutation happens on that one thread.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from client.chatmodel import ChatModel, Outgoing
from client.document import Document, UndoResult
from client.recovery import CrashedSession, Journal
from common.identity import Identity
from common.protocol import ErrorCode, MsgType

log = logging.getLogger(__name__)

RETRY_INTERVAL = 1.0

SAVE_IDLE = "idle"
SAVE_SAVING = "saving"
SAVE_SAVED = "saved"
SAVE_PENDING = "pending"
SAVE_FAILED = "failed"
SAVE_EDITED = "edited"


class Session:
    def __init__(
        self,
        identity: Identity,
        username: str,
        send: Callable[[int, dict[str, Any]], None],
        emit: Callable[..., None] = lambda *a, **k: None,
        clock: Callable[[], float] = time.time,
        journal_root: Path | None = None,
        host: str = "",
        port: int = 0,
    ) -> None:
        self.identity = identity
        self.fp = identity.fingerprint
        self.username = username
        self.send = send
        self.emit = emit
        self.clock = clock
        self.journal_root = journal_root
        self.host = host
        self.port = port
        self.doc = Document()
        self.doc.my_fp = self.fp
        self.chat = ChatModel(self.fp)
        self.journal: Journal | None = None
        self.role: str | None = None
        self.room: dict[str, Any] = {}
        self.room_id: str | None = None
        self.room_code: str | None = None
        self.settings: dict[str, Any] = {}
        self.members: dict[str, dict[str, Any]] = {}
        self.pending_requests: dict[str, dict[str, Any]] = {}
        self.muted = False
        self.live = False
        self.welcomed = False
        self.save_state = SAVE_IDLE
        self.save_info: dict[str, Any] = {}
        self.save_pending = False
        self._save_label: str | None = None
        self._hash_check: dict[str, Any] | None = None
        self._last_retry = 0.0
        self._journaled: set[str] = set()
        self._chat_journaled: set[str] = set()
        self._resync_sent_at = -1.0
        self.recovered_count: tuple[int, int] | None = None

    # ------------------------------------------------------------------ handshake inputs
    def hello_fields(self, base: dict[str, Any]) -> dict[str, Any]:
        """Fields for every HELLO. After the first WELCOME they always name the room by id."""
        if self.room_id:
            fields = {"room_id": self.room_id}
            if base.get("room_code"):
                fields["room_code"] = base["room_code"]
            if self.welcomed:
                fields["last_seq"] = self.doc.seq
            return fields
        return dict(base)

    def adopt_crashed(self, crashed: CrashedSession) -> None:
        """Load a crashed session, so its ops go out with their original ids and dedupe cleanly."""
        self.room_id = crashed.room_id
        self.doc.load_snapshot(crashed.board, crashed.seq)
        self.welcomed = crashed.seq > 0
        self.doc.pending = list(crashed.ops)
        self.doc._rebuild_view()
        for op in crashed.ops:
            self._journaled.add(op["op_id"])
            prefix, _, count = op["op_id"].rpartition(":")
            if prefix == self.doc.client_id and count.isdigit():
                self.doc.counter = max(self.doc.counter, int(count))
        for payload in crashed.chat:
            channel_id = self._channel_for_payload(payload)
            out = Outgoing(payload["msg_id"], channel_id, payload, payload.get("text", ""))
            self.chat.outbox[out.msg_id] = out
            self._chat_journaled.add(out.msg_id)
        self.recovered_count = (len(crashed.ops), len(crashed.chat))

    def _channel_for_payload(self, payload: dict[str, Any]) -> str:
        if payload.get("channel") == "dm":
            return self.chat.ensure_dm(payload["to"]).channel_id
        return "room"

    # ------------------------------------------------------------------ transport events
    def on_state(self, state: str) -> None:
        was_live = self.live
        self.live = state == "live"
        if was_live and not self.live:
            self.emit("disconnected")
        self.emit("state", state=state)

    def handle_frame(self, msg_type: int, msg: dict[str, Any]) -> None:
        handler = {
            MsgType.WELCOME: self._on_welcome,
            MsgType.OP: self._on_op,
            MsgType.OP_ACK: self._on_op_ack,
            MsgType.OP_REJECT: self._on_op_reject,
            MsgType.RESYNC: self._on_resync,
            MsgType.SNAPSHOT: self._on_snapshot,
            MsgType.CHAT: self._on_chat,
            MsgType.CHAT_ACK: self._on_chat_ack,
            MsgType.CHAT_REJECT: self._on_chat_reject,
            MsgType.CHAT_HISTORY: self._on_chat_history,
            MsgType.PRESENCE: self._on_presence,
            MsgType.TRANSIENT: self._on_transient,
            MsgType.ACCESS_PENDING: self._on_access_pending,
            MsgType.ACCESS_REQUEST: self._on_access_request,
            MsgType.SAVE_ACK: self._on_save_ack,
            MsgType.SAVE_REJECT: self._on_save_reject,
            MsgType.ADMIN: lambda m: self.emit("admin", msg=m),
            MsgType.ERROR: self._on_error,
        }.get(msg_type)
        if handler is not None:
            handler(msg)

    def _on_welcome(self, msg: dict[str, Any]) -> None:
        first = not self.welcomed
        self.role = msg["role"]
        self.room = msg["room"]
        self.room_id = msg["room"]["id"]
        self.settings = msg.get("settings", {})
        self.muted = msg.get("muted", False)
        self.room_code = msg.get("room_code", self.room_code)
        self.members = {m["fp"]: m for m in msg.get("members", [])}
        for request in msg.get("pending_requests", []):
            self.pending_requests[request["fingerprint"]] = request
        if self.doc.client_id == "local":
            # Stable across reconnects for this session. Reusing it would be harmless: op ids also
            # carry a counter, and the server's dedupe table would simply answer with old acks.
            self.doc.client_id = msg["client_id"]
            self.chat.client_id = msg["client_id"]
        changed: set[str] = set()
        if "snapshot" in msg:
            changed = self.doc.load_snapshot(msg["snapshot"], msg["last_seq"])
        for op in msg.get("delta", []):
            got, _ = self.doc.receive(op)
            changed |= got
        self.chat.load_welcome(msg)
        self.welcomed = True
        self._open_journal()
        self._resend_outstanding()
        if self.journal is not None:
            self.journal.write_snapshot(self.doc.confirmed, self.doc.seq, self.clock())
        self.emit("welcome", first=first, changed=changed)
        if self.recovered_count is not None:
            self.emit("recovered", ops=self.recovered_count[0], chat=self.recovered_count[1])
            self.recovered_count = None
        if self.save_pending:
            self._send_save()

    def _open_journal(self) -> None:
        if self.journal is None and self.journal_root is not None and self.room_id:
            self.journal = Journal(
                self.journal_root, self.room_id, self.room.get("name", ""), self.host, self.port
            )
            for op in self.doc.pending:
                self.journal.op_added(op)
            for out in self.chat.outbox.values():
                self.journal.chat_added(out.payload)

    def _resend_outstanding(self) -> None:
        """Un-acked work goes out again; the server's op_id and msg_id dedupe makes that safe."""
        for op in self.doc.pending:
            self.send(MsgType.OP, op)
        for out in self.chat.outbox.values():
            if out.state != "failed":
                self.send(MsgType.CHAT, out.payload)

    def _on_op(self, op: dict[str, Any]) -> None:
        changed, gap = self.doc.receive(op)
        if self.journal is not None and op.get("op_id") in self._journaled:
            self._journaled.discard(op["op_id"])
            self.journal.op_done(op["op_id"])
        if gap:
            self._request_resync()
        if changed:
            self.emit("board", changed=changed)
            self._note_edit()
        self._check_save_hash()

    def _request_resync(self) -> None:
        now = self.clock()
        if now - self._resync_sent_at < 0.5:
            return  # a gap reported for every buffered op must not become a flood of requests
        self._resync_sent_at = now
        self.send(MsgType.RESYNC, {"from_seq": self.doc.seq})

    def _on_op_ack(self, msg: dict[str, Any]) -> None:
        changed = self.doc.on_ack(msg.get("op_id", ""), msg.get("seq", 0))
        if changed:
            op_id = msg.get("op_id", "")
            if self.journal is not None and op_id in self._journaled:
                self._journaled.discard(op_id)
                self.journal.op_done(op_id)
            self.emit("board", changed=changed)

    def _on_op_reject(self, msg: dict[str, Any]) -> None:
        op_id = msg.get("op_id", "")
        code = msg.get("code", "")
        if code == ErrorCode.RATE_LIMITED:
            return  # stays pending; tick() re-sends it shortly
        changed = self.doc.on_reject(op_id)
        if self.journal is not None and op_id in self._journaled:
            self._journaled.discard(op_id)
            self.journal.op_done(op_id)
        self.emit("board", changed=changed)
        self.emit("op_rejected", code=code)

    def _on_resync(self, msg: dict[str, Any]) -> None:
        changed: set[str] = set()
        for op in msg.get("ops", []):
            got, _ = self.doc.receive(op)
            changed |= got
        if changed:
            self.emit("board", changed=changed)
        self._check_save_hash()

    def _on_snapshot(self, msg: dict[str, Any]) -> None:
        changed = self.doc.load_snapshot(msg["board"], msg["seq"])
        self.emit("board", changed=changed)
        self._check_save_hash()

    def _on_transient(self, msg: dict[str, Any]) -> None:
        self.emit("transient", msg=msg)

    def _on_access_pending(self, msg: dict[str, Any]) -> None:
        self.emit("access_pending", msg=msg)

    def _on_access_request(self, msg: dict[str, Any]) -> None:
        self.pending_requests[msg["fingerprint"]] = msg
        self.emit("access_request", request=msg)

    def _on_error(self, msg: dict[str, Any]) -> None:
        self.emit("error", code=msg.get("code", ""), msg=msg)

    # ------------------------------------------------------------------ presence
    def _on_presence(self, msg: dict[str, Any]) -> None:
        event = msg.get("event")
        fp = msg.get("fp")
        if event == "room":
            self.room = msg.get("room", self.room)
            self.settings = msg.get("settings", self.settings)
        elif event == "chat_cleared":
            room = self.chat.room
            room.messages, room.seqs, room.unread = [], [], 0
            room.has_more = False
            self.emit("chat", channel_id="room")
        elif event == "member_removed":
            self.members.pop(fp, None)
        elif fp:
            member = self.members.setdefault(
                fp, {"fp": fp, "name": msg.get("name", ""), "role": msg.get("role"), "muted": False}
            )
            if msg.get("name"):
                member["name"] = msg["name"]
            member["role"] = msg.get("role", member.get("role"))
            member["muted"] = msg.get("muted", member.get("muted", False))
            if event in ("join", "back"):
                member["online"], member["away"] = True, False
            elif event == "away":
                member["away"] = True
            elif event == "leave":
                member["online"], member["away"] = False, False
            elif event == "member_added":
                self.pending_requests.pop(fp, None)
            if fp == self.fp:
                self.muted = member["muted"]
                if member["role"] and member["role"] != self.role:
                    self.role = member["role"]
                    self.emit("role_changed", role=self.role)
        self.emit("presence", msg=msg)

    # ------------------------------------------------------------------ editing
    def _submit(self, op: dict[str, Any] | None) -> dict[str, Any] | None:
        if op is None:
            return None
        if self.journal is not None:
            self._journaled.add(op["op_id"])
            self.journal.op_added(op)
        self.send(MsgType.OP, op)
        self._note_edit()
        return op

    def _note_edit(self) -> None:
        if self.save_state in (SAVE_SAVED, SAVE_PENDING, SAVE_FAILED, SAVE_IDLE):
            self.save_state = SAVE_EDITED
            self.emit("save_state")

    def create(self, obj_type: str, props: dict[str, Any], **kw: Any) -> dict[str, Any] | None:
        op = self._submit(self.doc.create(obj_type, props, **kw))
        if op:
            self.emit("board", changed={op["id"]})
        return op

    def update(self, obj_id: str, patch: dict[str, Any], group: str | None = None) -> None:
        if self._submit(self.doc.update(obj_id, patch, group)):
            self.emit("board", changed={obj_id})

    def delete(self, obj_id: str, group: str | None = None) -> None:
        if self._submit(self.doc.delete(obj_id, group)):
            self.emit("board", changed={obj_id})

    def clear(self) -> None:
        before = set(self.doc.view.objects)
        self._submit(self.doc.clear())
        self.emit("board", changed=before)

    def restore(self, checkpoint_id: str) -> None:
        self._submit(self.doc.restore(checkpoint_id))

    def replace_board(self, objects: list[dict[str, Any]]) -> None:
        before = set(self.doc.view.objects)
        self._submit(self.doc.replace_board(objects))
        self.emit("board", changed=before | {o["id"] for o in objects})

    def _after_undo(self, result: UndoResult) -> None:
        changed: set[str] = set()
        for op in result.ops:
            self._submit(op)
            if op.get("id"):
                changed.add(op["id"])
            else:
                changed |= set(self.doc.view.objects)
        self.emit("board", changed=changed)
        if result.skipped:
            self.emit("undo_skipped", count=result.skipped, by=sorted(result.skipped_by))

    def undo(self) -> None:
        self._after_undo(self.doc.undo())

    def redo(self) -> None:
        self._after_undo(self.doc.redo())

    def send_transient(self, kind: str, data: dict[str, Any]) -> None:
        self.send(MsgType.TRANSIENT, {"k": kind, "d": data})

    # ------------------------------------------------------------------ chat
    def send_chat(self, channel_id: str, text: str) -> Outgoing:
        out = self.chat.compose(channel_id, text)
        if self.journal is not None:
            self._chat_journaled.add(out.msg_id)
            self.journal.chat_added(out.payload)
        self.send(MsgType.CHAT, out.payload)
        self.emit("chat", channel_id=channel_id)
        return out

    def retry_chat(self, msg_id: str) -> None:
        out = self.chat.retry(msg_id)
        if out is not None:
            self.send(MsgType.CHAT, out.payload)
            self.emit("chat", channel_id=out.channel_id)

    def _chat_settled(self, msg_id: str) -> None:
        if self.journal is not None and msg_id in self._chat_journaled:
            self._chat_journaled.discard(msg_id)
            self.journal.chat_done(msg_id)

    def _on_chat(self, msg: dict[str, Any]) -> None:
        self._chat_settled(msg.get("msg_id", ""))
        channel_before = msg["channel_id"]
        new = self.chat.receive(msg)
        channel = self.chat.channels[channel_before]
        request = self.chat.gap_request(channel)
        if request is not None:
            self.send(MsgType.CHAT_HISTORY, request)
        if new:
            self.emit("chat", channel_id=channel_before, message=msg)
            if channel_before == self.chat.active:
                read = self.chat.mark_active(channel_before)
                if read:
                    self.send(MsgType.CHAT_READ, read)

    def _on_chat_ack(self, msg: dict[str, Any]) -> None:
        if msg.get("duplicate"):
            self._chat_settled(msg.get("msg_id", ""))
            self.chat.on_ack(msg)
            self.emit("chat", channel_id=msg.get("channel_id", "room"))

    def _on_chat_reject(self, msg: dict[str, Any]) -> None:
        code = msg.get("code", "")
        if code == ErrorCode.RATE_LIMITED:
            return  # the message stays "sending"; tick() retries it
        out = self.chat.on_reject(msg.get("msg_id"), code)
        if out is not None:
            self._chat_settled(out.msg_id)
            self.emit("chat", channel_id=out.channel_id)

    def _on_chat_history(self, msg: dict[str, Any]) -> None:
        channel = self.chat.merge_history(msg)
        if channel is not None:
            self.emit("chat", channel_id=channel.channel_id, history=True)

    def load_older(self, channel_id: str) -> None:
        channel = self.chat.channels.get(channel_id)
        request = self.chat.history_request(channel) if channel else None
        if request:
            self.send(MsgType.CHAT_HISTORY, request)

    def open_channel(self, channel_id: str) -> None:
        read = self.chat.mark_active(channel_id)
        if read:
            self.send(MsgType.CHAT_READ, read)
        channel = self.chat.channels[channel_id]
        if channel.is_private and not channel.seqs:
            self.load_older(channel_id)  # a DM only has a summary until it is first opened
        self.emit("chat", channel_id=channel_id)

    # ------------------------------------------------------------------ owner actions
    def send_admin(self, action: str, **fields: Any) -> None:
        self.send(MsgType.ADMIN, {"action": action, **fields})

    def decide_access(self, fingerprint: str, decision: str, role: str | None = None) -> None:
        payload: dict[str, Any] = {"fingerprint": fingerprint, "decision": decision}
        if role:
            payload["role"] = role
        self.send(MsgType.ACCESS_DECISION, payload)
        self.pending_requests.pop(fingerprint, None)
        self.emit("requests_changed")

    # ------------------------------------------------------------------ saving
    def can_export(self) -> bool:
        return self.role in ("owner", "editor") or bool(self.settings.get("viewers_can_export"))

    def save(self, label: str | None = None) -> None:
        """Ctrl+S requests a durable server checkpoint when the current role allows it."""
        can_checkpoint = self.role in ("owner", "editor")
        if not can_checkpoint:
            self.emit("toast", text="Only editors can save a server checkpoint.")
            return
        self.save_state = SAVE_SAVING
        self.emit("save_state")
        self._save_label = label
        if self.live:
            self._send_save()
        else:
            self.save_pending = True  # goes out from _on_welcome after reconnecting
            self.save_state = SAVE_PENDING
            self.emit("save_state")

    def _send_save(self) -> None:
        self.save_pending = False
        self.send(MsgType.SAVE, {"label": self._save_label} if self._save_label else {})

    def _on_save_ack(self, msg: dict[str, Any]) -> None:
        self.save_state = SAVE_SAVED
        self.save_info |= {
            "time": self.clock(),
            "seq": msg["seq"],
            "board_hash": msg["board_hash"],
            "checkpoint_id": msg["checkpoint_id"],
        }
        self._hash_check = msg
        self._check_save_hash()
        self.emit("save_state")

    def _check_save_hash(self) -> None:
        """Compare with the server once this client has reached the checkpoint's seq."""
        check = self._hash_check
        if check is None or self.doc.seq < check["seq"]:
            return
        self._hash_check = None
        if self.doc.seq == check["seq"] and self.doc.hash() != check["board_hash"]:
            log.warning("board hash mismatch at seq %s", check["seq"])
            self.send(MsgType.RESYNC, {"from_seq": -1})  # any invalid position forces a snapshot
            self.emit("hash_mismatch")

    def _on_save_reject(self, msg: dict[str, Any]) -> None:
        self.save_state = SAVE_FAILED
        self.save_info["error"] = msg.get("code", "")
        self.emit("save_state")

    # ------------------------------------------------------------------ periodic work
    def tick(self) -> None:
        """Called about once a second by the UI: journal snapshots and rate-limited retries."""
        now = self.clock()
        if self.journal is not None and self.journal.snapshot_due(now):
            self.journal.write_snapshot(self.doc.confirmed, self.doc.seq, now)
        if self.live and now - self._last_retry >= RETRY_INTERVAL:
            self._last_retry = now
            # A frame can only have been refused by the rate limiter if it has been outstanding a
            # while, so only messages older than the interval are re-sent. Dedupe covers the rest.
            for out in self.chat.outbox.values():
                if out.state == "sending":
                    self.send(MsgType.CHAT, out.payload)

    def close(self, clean: bool = True) -> None:
        if self.journal is not None:
            self.journal.close(clean=clean)
            self.journal = None
