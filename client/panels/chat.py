"""Chat panel: room and private channels, retained history, and per-message send state.

All user text is HTML-escaped before it reaches the view. A chat message is untrusted input, and it
must never be able to inject markup or a link into another member's window.
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta
from typing import Any

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from client import icons, theme
from client.chatmodel import FAILED
from common.chat import MAX_TEXT
from common.identity import short_fingerprint

VISIBLE_MESSAGES = 500
SCROLL_TOP_ZONE = 8


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


class ChatPanel(QWidget):
    unread_changed = pyqtSignal(int)
    message_person = pyqtSignal(str)
    new_private = pyqtSignal()

    def __init__(self, session: Any) -> None:
        super().__init__()
        self.session = session
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(12, 8, 8, 4)
        title = QLabel("Chat")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        self.owner_menu = QToolButton()
        self.owner_menu.setText("…")
        self.owner_menu.setToolTip("Room chat settings")
        self.owner_menu.clicked.connect(self._open_owner_menu)
        header.addWidget(self.owner_menu)
        new_button = QToolButton()
        new_button.setIcon(icons.icon("new", theme.current().text_primary))
        new_button.setToolTip("New private message (Ctrl+Shift+M)")
        new_button.clicked.connect(self.new_private.emit)
        header.addWidget(new_button)
        root.addLayout(header)

        self.channels = QListWidget()
        self.channels.setMaximumHeight(110)
        self.channels.itemClicked.connect(self._channel_clicked)
        root.addWidget(self.channels)

        self.strip_row = QWidget()
        self.strip_row.setObjectName("privatestrip")
        self.strip_row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        strip_layout = QHBoxLayout(self.strip_row)
        strip_layout.setContentsMargins(8, 4, 8, 4)
        strip_layout.setSpacing(6)
        self.strip_icon = QLabel()
        strip_layout.addWidget(self.strip_icon)
        self.strip = QLabel()
        self.strip.setWordWrap(True)
        self.strip.setTextFormat(Qt.TextFormat.PlainText)
        self.strip.setStyleSheet(
            f"color:{theme.current().on_private};font-size:11px;background:transparent;"
        )
        strip_layout.addWidget(self.strip, 1)
        root.addWidget(self.strip_row)

        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.setOpenExternalLinks(False)
        self.view.anchorClicked.connect(self._anchor)
        self.view.verticalScrollBar().valueChanged.connect(self._scrolled)
        root.addWidget(self.view, 1)

        self.notice = QLabel()
        self.notice.setObjectName("muted")
        self.notice.setWordWrap(True)
        self.notice.setContentsMargins(12, 2, 12, 2)
        root.addWidget(self.notice)

        row = QHBoxLayout()
        row.setContentsMargins(8, 4, 8, 8)
        self.input = QLineEdit()
        self.input.setMaxLength(MAX_TEXT)
        self.input.returnPressed.connect(self._send)
        row.addWidget(self.input, 1)
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self._send)
        row.addWidget(self.send_button)
        root.addLayout(row)
        self.visible_to_user = True
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

    def _channel_clicked(self, item: QListWidgetItem) -> None:
        self.open_channel(item.data(Qt.ItemDataRole.UserRole))

    def _send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.session.send_chat(self.session.chat.active, text)
        self.input.clear()
        self.refresh(scroll_bottom=True)

    def _scrolled(self, value: int) -> None:
        if value <= SCROLL_TOP_ZONE:
            self.session.load_older(self.session.chat.active)

    def _anchor(self, url: QUrl) -> None:
        target = url.toString()
        if target.startswith("retry:"):
            self.session.retry_chat(target[len("retry:") :])
        elif target.startswith("person:"):
            fingerprint = target[len("person:") :]
            if fingerprint != self.session.fp and fingerprint in self.session.members:
                menu = QMenu(self)
                menu.addAction("Message privately", lambda: self.open_private(fingerprint))
                menu.exec(self.cursor().pos())

    def _open_owner_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("Clear room chat…", self._clear_chat)
        retention = menu.addMenu("Chat retention")
        retention.addAction("Keep forever", lambda: self._retention({"mode": "forever"}))
        retention.addAction("Last N messages…", lambda: self._ask_retention("last_n", "messages"))
        retention.addAction("Last N days…", lambda: self._ask_retention("days", "days"))
        menu.exec(self.owner_menu.mapToGlobal(self.owner_menu.rect().bottomLeft()))

    def _clear_chat(self) -> None:
        from PyQt6.QtWidgets import QMessageBox

        answer = QMessageBox.question(
            self,
            "Clear room chat",
            "Remove all room messages for everyone? Private messages are not affected.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.session.send_admin("clear_chat")

    def _ask_retention(self, mode: str, unit: str) -> None:
        from PyQt6.QtWidgets import QInputDialog

        value, ok = QInputDialog.getInt(
            self, "Chat retention", f"Keep the last how many {unit}?", 500, 1, 1000000
        )
        if ok:
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
            self.strip_icon.setPixmap(
                icons.pixmap("lock", theme.current().on_private, 28).scaled(14, 14)
            )
            self.strip_row.show()
        else:
            self.strip_row.hide()
        self._render_notice(channel, name)
        self.input.setPlaceholderText(f"Message {name}…")
        muted = self.session.muted
        self.input.setEnabled(not muted)
        self.send_button.setEnabled(not muted)
        bar = self.view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        old_max, old_value = bar.maximum(), bar.value()
        self.view.setHtml(self._render_messages(channel))
        if scroll_bottom or at_bottom:
            bar.setValue(bar.maximum())
        else:
            # Older history went in above: keep the same messages under the reader's eyes.
            bar.setValue(old_value + (bar.maximum() - old_max))
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
        self.channels.blockSignals(True)
        self.channels.clear()
        for channel in chat.sorted_channels():
            prefix = "" if channel.is_private else "# "
            suffix = "" if channel.is_private else " (room)"
            badge = f"   •{channel.unread}" if channel.unread else ""
            item = QListWidgetItem(prefix + self._channel_name(channel) + suffix + badge)
            item.setData(Qt.ItemDataRole.UserRole, channel.channel_id)
            if channel.is_private:
                # A drawn icon: an emoji would depend on fonts that are not bundled with the app.
                item.setIcon(icons.icon("lock", theme.current().private))
            self.channels.addItem(item)
            item.setSelected(channel.channel_id == chat.active)
        self.channels.blockSignals(False)

    def _render_notice(self, channel: Any, name: str) -> None:
        text = ""
        if self.session.muted:
            text = "The owner has muted you in this room."
        elif channel.is_private and not self.session.members.get(channel.peer_fp, {}).get("online"):
            text = f"{name} is offline; they'll see this when they reconnect."
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def _render_messages(self, channel: Any) -> str:
        t = theme.current()
        parts: list[str] = [
            f"<style>body{{color:{t.text_primary};}} a{{text-decoration:none;}}"
            f" .m{{color:{t.text_muted};font-size:11px;}}</style>"
        ]
        messages = channel.messages[-VISIBLE_MESSAGES:]
        if channel.has_more or len(channel.messages) > VISIBLE_MESSAGES:
            parts.append("<p align='center' class='m'>— Load older messages —</p>")
        elif messages:
            parts.append("<p align='center' class='m'>Beginning of conversation</p>")
        last_day = None
        last_sender = None
        for message in messages:
            stamp = _parse_ts(message.get("ts", ""))
            if stamp is not None and stamp.date() != last_day:
                last_day = stamp.date()
                last_sender = None
                parts.append(f"<p align='center' class='m'>——— {_day_label(stamp)} ———</p>")
            when = stamp.strftime("%H:%M") if stamp else ""
            parts.append(self._message_html(message, when, message["from_fp"] != last_sender))
            last_sender = message["from_fp"]
        for out in self.session.chat.outgoing_for(channel.channel_id):
            parts.append(self._outgoing_html(out))
        return "".join(parts)

    def _message_html(self, message: dict[str, Any], when: str, show_name: bool) -> str:
        text = html.escape(message["text"]).replace("\n", "<br>")
        fingerprint = message["from_fp"]
        t = theme.current()
        if fingerprint == self.session.fp:
            return (
                f"<p align='right' style='margin:2px 0'>{text} "
                f"<span class='m'>{when} You</span></p>"
            )
        name = html.escape(message.get("from_name", ""))
        color = theme.user_color(fingerprint)
        header = ""
        if show_name:
            header = (
                f"<a href='person:{html.escape(fingerprint, quote=True)}' "
                f"style='color:{color};font-weight:600'>{name}</a> "
                f"<span class='m' style='color:{t.text_muted}'>{when}</span><br>"
            )
        return f"<p style='margin:4px 0 2px 0'>{header}{text}</p>"

    def _outgoing_html(self, out: Any) -> str:
        t = theme.current()
        text = html.escape(out.text).replace("\n", "<br>")
        if out.state == FAILED:
            link = html.escape(out.msg_id, quote=True)
            return (
                f"<p align='right' style='margin:2px 0'>{text} "
                f"<a href='retry:{link}' style='color:{t.danger}'>Retry</a></p>"
            )
        return (
            f"<p align='right' style='margin:2px 0;color:{t.text_muted}'>{text} <i>sending</i></p>"
        )
