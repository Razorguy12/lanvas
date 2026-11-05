"""The People popover: who is here, their role and state, plus owner controls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from client.widgets import Avatar, section_label
from common.identity import short_fingerprint


def state_text(member: dict[str, Any]) -> str:
    if member.get("away"):
        return "away"
    return "online" if member.get("online") else "offline"


class PeoplePopover(QFrame):
    def __init__(self, session: Any, on_message: Callable[[str], None]) -> None:
        super().__init__(None, Qt.WindowType.Popup)
        self.session = session
        self.on_message = on_message
        self.setObjectName("card")
        self.setMinimumWidth(360)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.addWidget(section_label("People in this room"))
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setMaximumHeight(360)
        body = QWidget()
        self.rows = QVBoxLayout(body)
        self.rows.setContentsMargins(0, 0, 0, 0)
        area.setWidget(body)
        root.addWidget(area)
        members = sorted(
            session.members.values(),
            key=lambda m: (
                not m.get("online"),
                m.get("role") != "owner",
                m.get("name", "").lower(),
            ),
        )
        for member in members:
            self.rows.addWidget(self._row(member))
        self.rows.addStretch(1)

    def _row(self, member: dict[str, Any]) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)
        fingerprint = member["fp"]
        layout.addWidget(Avatar(member.get("name", "?"), fingerprint, member.get("away", False)))
        you = " (you)" if fingerprint == self.session.fp else ""
        muted = " · muted" if member.get("muted") else ""
        name = member.get("name", "?")
        label = QLabel(f"{name}{you}\n{member.get('role', '')} · {state_text(member)}{muted}")
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setToolTip(short_fingerprint(fingerprint))
        layout.addWidget(label, 1)
        if fingerprint != self.session.fp:
            message = QPushButton("Message")
            message.clicked.connect(lambda: (self.close(), self.on_message(fingerprint)))
            layout.addWidget(message)
        if self.session.role == "owner" and member.get("role") != "owner":
            self._owner_controls(layout, member)
        return row

    def _owner_controls(self, layout: QHBoxLayout, member: dict[str, Any]) -> None:
        fingerprint = member["fp"]
        role = QComboBox()
        role.addItems(["editor", "viewer"])
        role.setCurrentText(member.get("role", "viewer"))
        role.activated.connect(
            lambda: self.session.send_admin("set_role", fp=fingerprint, role=role.currentText())
        )
        layout.addWidget(role)
        mute = QPushButton("Unmute" if member.get("muted") else "Mute")
        mute.clicked.connect(
            lambda: (
                self.session.send_admin("mute", fp=fingerprint, muted=not member.get("muted")),
                self.close(),
            )
        )
        layout.addWidget(mute)
        kick = QPushButton("Kick")
        kick.setObjectName("danger")
        kick.clicked.connect(lambda: self._kick(fingerprint, member.get("name", "this person")))
        layout.addWidget(kick)

    def _kick(self, fingerprint: str, name: str) -> None:
        self.close()
        answer = QMessageBox.question(
            None, "Kick", f"Remove {name} from this room? They would have to ask to join again."
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.session.send_admin("kick", fp=fingerprint)
