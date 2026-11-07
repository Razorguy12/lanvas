"""Chat panel: room and private channels, retained history, and per-message send state.

User text is inserted into the view as plain text, never interpreted as markup. A chat message is
untrusted input, and it must never be able to inject formatting or a link into another member's
window.
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime, timedelta
from typing import Any

from client import icons, theme
from client.chatmodel import FAILED
from client.widgets import (
    Button,
    Frame,
    Label,
    LineEdit,
    ListWidget,
    Signal,
    TextView,
    ToolButton,
    ask_int,
    ask_yes_no,
    popup_menu,
)
from common.chat import MAX_TEXT
from common.identity import short_fingerprint

VISIBLE_MESSAGES = 500
SCROLL_TOP_ZONE = 0.01


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts).astimezone()  # server sends UTC, shown in local time
    except (ValueError, TypeError):
        return None


def _day_label(day: datetime) -> str:
    today = datetime.now().astimezone().date()
    if day.date() == today:
        return "Today"
    if day.date() == today - timedelta(days=1):
        return "Yesterday"
    return day.strftime("%d %b %Y")


class ChatPanel(Frame):
    def __init__(self, master: tk.Misc, session: Any) -> None:
        super().__init__(master, "panel")
        self.session = session
        self.unread_changed = Signal()
        self.message_person = Signal()
        self.new_private = Signal()
        t = theme.current()

        header = Frame(self, "panel", padding=(12, 8, 8, 4))
        header.pack(fill="x")
        Label(header, "Chat", "title").pack(side="left")
        new_button = ToolButton(header, "new", tooltip="New private message (Ctrl+Shift+M)")
        new_button.clicked.connect(self.new_private.emit)
        new_button.pack(side="right")
        self.owner_menu = ToolButton(header, text="…", tooltip="Room chat settings")
        self.owner_menu.clicked.connect(self._open_owner_menu)
        self.owner_menu.pack(side="right")

        self.channels = ListWidget(self, height=4)
        self.channels.itemClicked.connect(self._channel_clicked)
        self.channels.pack(fill="x")

        self.strip_row = Frame(self, "private", padding=(8, 4))
        self.strip_row.pack(fill="x")
        self.strip_icon = Label(self.strip_row, "", "private",
                                image=icons.photo("lock", t.on_private, 14, self))
        self.strip_icon.pack(side="left", padx=(0, 6))
        self.strip = Label(self.strip_row, "", "private", wraplength=260, justify="left")
        self.strip.pack(side="left", fill="x", expand=True)

        view_holder = Frame(self, "panel")
        view_holder.pack(fill="both", expand=True)
        self.view = TextView(view_holder)
        scroll = tk.Scrollbar(view_holder, orient="vertical", command=self.view.yview)
        self.view.configure(yscrollcommand=lambda lo, hi: (scroll.set(lo, hi),
                                                           self._scrolled(float(lo))))
        self.view.pack(side="left", fill="both", expand=True)
        self._scrollbar = scroll

        self.notice = Label(self, "", "panelmuted", wraplength=280, justify="left",
                            padding=(12, 2))
        self.notice.pack(fill="x")

        row = Frame(self, "panel", padding=(8, 4, 8, 8))
        row.pack(fill="x")
        self.input = LineEdit(row, max_length=MAX_TEXT)
        self.input.returnPressed.connect(self._send)
        self.input.pack(side="left", fill="x", expand=True)
        self.send_button = Button(row, "Send", "primary")
        self.send_button.clicked.connect(self._send)
        self.send_button.pack(side="left", padx=(6, 0))
        self.visible_to_user = True
        self._rendering = False
        self.refresh()

    # ------------------------------------------------------------------ actions
    def open_channel(self, channel_id: str) -> None:
        self.session.open_channel(channel_id)
        self.refresh(scroll_bottom=True)

    def open_private(self, fingerprint: str) -> None:
        name = self.session.members.get(fingerprint, {}).get("name", "")
        channel = self.session.chat.ensure_dm(fingerprint, name)
        self.open_channel(channel.channel_id)
        self.input.setFocus()

    def focus_input(self) -> None:
        self.input.setFocus()

    def _channel_clicked(self, channel_id: str | None) -> None:
        if channel_id:
            self.open_channel(channel_id)

    def _send(self) -> None:
        text = self.input.text().strip()
        if not text or not self.input.isEnabled():
            return
        self.session.send_chat(self.session.chat.active, text)
        self.input.clear()
        self.refresh(scroll_bottom=True)

    def _scrolled(self, top: float) -> None:
        if not self._rendering and top <= SCROLL_TOP_ZONE and self.view.yview()[1] < 1.0:
            self.session.load_older(self.session.chat.active)

    def _person_menu(self, fingerprint: str) -> None:
        if fingerprint != self.session.fp and fingerprint in self.session.members:
            x, y = self.winfo_pointerxy()
            popup_menu(self, [("Message privately", lambda: self.open_private(fingerprint))], x, y)

    def _open_owner_menu(self) -> None:
        popup_menu(self.owner_menu, [
            ("Clear room chat…", self._clear_chat),
            ("Chat retention", [
                ("Keep forever", lambda: self._retention({"mode": "forever"})),
                ("Last N messages…", lambda: self._ask_retention("last_n", "messages")),
                ("Last N days…", lambda: self._ask_retention("days", "days")),
            ]),
        ])

    def _clear_chat(self) -> None:
        if ask_yes_no(self, "Clear room chat",
                      "Remove all room messages for everyone? Private messages are not affected."):
            self.session.send_admin("clear_chat")

    def _ask_retention(self, mode: str, unit: str) -> None:
        value = ask_int(self, "Chat retention", f"Keep the last how many {unit}?", 500, 1, 1000000)
        if value is not None:
            self._retention({"mode": mode, "value": value})

    def _retention(self, policy: dict[str, Any]) -> None:
        self.session.send_admin("set_retention", policy=policy)

    # ------------------------------------------------------------------ rendering
    def refresh(self, scroll_bottom: bool = False) -> None:
        chat = self.session.chat
        self._render_channel_list()
        channel = chat.channels.get(chat.active) or chat.room
        self.owner_menu.setVisible(self.session.role == "owner" and not channel.is_private)
        name = self._channel_name(channel)
        if channel.is_private:
            self.strip.setText(f"Private · only you and {name} can read")
            self.strip_row.setVisible(True)
        else:
            self.strip_row.setVisible(False)
        self._render_notice(channel, name)
        self.input.setPlaceholderText(f"Message {name}…")
        muted = self.session.muted
        self.input.setEnabled(not muted)
        self.send_button.setEnabled(not muted)
        top, bottom = self.view.yview()
        at_bottom = bottom >= 0.999
        old_lines = int(self.view.index("end-1c").split(".")[0])
        self._rendering = True
        self._render_messages(channel)
        if scroll_bottom or at_bottom:
            self.view.see("end")
        else:
            # Older history went in above: keep the same messages under the reader's eyes.
            new_lines = int(self.view.index("end-1c").split(".")[0])
            first_visible = int(self.view.index("@0,0").split(".")[0])
            self.view.yview(f"{max(1, first_visible + new_lines - old_lines)}.0")
        self.after_idle(lambda: setattr(self, "_rendering", False))
        self.unread_changed.emit(chat.total_unread())

    def _channel_name(self, channel: Any) -> str:
        if not channel.is_private:
            return self.session.room.get("name", "room")
        member = self.session.members.get(channel.peer_fp, {})
        name = member.get("name") or channel.peer_name or "Former member"
        collisions = [m for m in self.session.members.values() if m.get("name") == name]
        if len(collisions) > 1:
            return f"{name} ({short_fingerprint(channel.peer_fp)})"
        return name

    def _render_channel_list(self) -> None:
        chat = self.session.chat
        self.channels.clear()
        lock = icons.photo("lock", theme.current().private, master=self)
        for channel in chat.sorted_channels():
            prefix = "" if channel.is_private else "# "
            suffix = "" if channel.is_private else " (room)"
            badge = f"   •{channel.unread}" if channel.unread else ""
            # A drawn icon: an emoji would depend on fonts that are not bundled with the app.
            self.channels.addItem(prefix + self._channel_name(channel) + suffix + badge,
                                  channel.channel_id,
                                  icon=lock if channel.is_private else None,
                                  selected=channel.channel_id == chat.active)

    def _render_notice(self, channel: Any, name: str) -> None:
        text = ""
        if self.session.muted:
            text = "The owner has muted you in this room."
        elif channel.is_private and not self.session.members.get(channel.peer_fp, {}).get("online"):
            text = f"{name} is offline; they'll see this when they reconnect."
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def _render_messages(self, channel: Any) -> None:
        view = self.view
        view.begin()
        messages = channel.messages[-VISIBLE_MESSAGES:]
        if channel.has_more or len(channel.messages) > VISIBLE_MESSAGES:
            view.write("— Load older messages —\n", "muted", "center")
        elif messages:
            view.write("Beginning of conversation\n", "muted", "center")
        last_day = None
        last_sender = None
        for message in messages:
            stamp = _parse_ts(message.get("ts", ""))
            if stamp is not None and stamp.date() != last_day:
                last_day = stamp.date()
                last_sender = None
                view.write(f"——— {_day_label(stamp)} ———\n", "muted", "center")
            when = stamp.strftime("%H:%M") if stamp else ""
            self._message(message, when, message["from_fp"] != last_sender)
            last_sender = message["from_fp"]
        for out in self.session.chat.outgoing_for(channel.channel_id):
            self._outgoing(out)
        view.end()

    def _message(self, message: dict[str, Any], when: str, show_name: bool) -> None:
        view = self.view
        text = message["text"]
        fingerprint = message["from_fp"]
        if fingerprint == self.session.fp:
            view.write(text + " ", "right")
            view.write(f"{when} You\n", "right", "muted")
            return
        if show_name:
            view.write("\n")
            view.link(message.get("from_name", ""), lambda f=fingerprint: self._person_menu(f),
                      theme.user_color(fingerprint), "bold")
            view.write(f" {when}\n", "muted")
        view.write(text + "\n")

    def _outgoing(self, out: Any) -> None:
        view = self.view
        if out.state == FAILED:
            view.write(out.text + " ", "right")
            view.link("Retry", lambda m=out.msg_id: self.session.retry_chat(m),
                      theme.current().danger, "right")
            view.write("\n", "right")
            return
        view.write(out.text + " ", "right", "muted")
        view.write("sending\n", "right", "muted", "italic")
