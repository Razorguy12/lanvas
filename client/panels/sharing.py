"""Room members and sharing/access controls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from client.widgets import Avatar, section_label
from common import netutil
from common.identity import short_fingerprint

TOGGLES = (
    ("discoverable", "Discoverable on this network"),
    ("require_code", "Require room code to request access"),
    ("show_member_count", "Show member count in discovery"),
    ("locked", "Lock board (read-only for all but the owner)"),
    ("editors_can_clear", "Editors can clear canvas"),
    ("viewers_can_export", "Viewers can export"),
)
EXPIRY = (("1 hour", 3600), ("1 day", 86400), ("7 days", 604800))


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


class AccessDialog(QDialog):
    def __init__(self, session: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Share and access")
        self.setMinimumWidth(460)
        root = QVBoxLayout(self)
        root.addWidget(section_label("Room"))
        root.addWidget(QLabel(f"<b>{_plain(session.room.get('name', ''))}</b>"))
        code = session.room_code
        address = ", ".join(i.address for i in netutil.list_interfaces())
        if session.host and session.host not in ("127.0.0.1", "localhost"):
            address = session.host
        root.addLayout(
            _copy_row("Room code", code or "(shown only when the room was created)", bool(code))
        )
        root.addLayout(_copy_row("LAN address", f"{address}  (port {session.port})", True))
        root.addWidget(section_label("Requests"))
        self.requests = QVBoxLayout()
        root.addLayout(self.requests)
        self._fill_requests()
        root.addWidget(section_label("Settings"))
        for key, label in TOGGLES:
            box = QCheckBox(label)
            box.setChecked(bool(session.settings.get(key)))
            box.toggled.connect(
                lambda v, k=key: session.send_admin("set_settings", settings={k: v})
            )
            root.addWidget(box)
        root.addWidget(section_label("Invite"))
        invite = QHBoxLayout()
        self.invite_role = QComboBox()
        self.invite_role.addItems(["editor", "viewer"])
        invite.addWidget(self.invite_role)
        self.invite_expiry = QComboBox()
        for label, seconds in EXPIRY:
            self.invite_expiry.addItem(label, seconds)
        invite.addWidget(self.invite_expiry)
        make = QPushButton("Create single-use invite")
        make.clicked.connect(self._create_invite)
        invite.addWidget(make)
        root.addLayout(invite)
        self.invite_out = QLineEdit()
        self.invite_out.setReadOnly(True)
        self.invite_out.setPlaceholderText("The invite code appears here")
        root.addWidget(self.invite_out)
        root.addWidget(section_label("Members"))
        root.addWidget(self._member_table())
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        root.addWidget(close)

    def _fill_requests(self) -> None:
        while self.requests.count():
            child = self.requests.takeAt(0).widget()
            if child is not None:
                child.deleteLater()
        pending = list(self.session.pending_requests.values())
        if not pending:
            none = QLabel("No pending requests.")
            none.setObjectName("muted")
            self.requests.addWidget(none)
        for request in pending:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            fp = request["fingerprint"]
            layout.addWidget(QLabel(f"{request['name']} · {short_fingerprint(fp)}"), 1)
            for label, decision, role in (
                ("Editor", "approve", "editor"),
                ("Viewer", "approve", "viewer"),
                ("Deny", "deny", None),
            ):
                button = QPushButton(label)
                button.clicked.connect(
                    lambda _=False, d=decision, r=role, f=fp: self._decide(f, d, r)
                )
                layout.addWidget(button)
            self.requests.addWidget(row)

    def _decide(self, fingerprint: str, decision: str, role: str | None) -> None:
        self.session.decide_access(fingerprint, decision, role)
        self._fill_requests()

    def _create_invite(self) -> None:
        self.session.send_admin(
            "create_invite",
            role=self.invite_role.currentText(),
            ttl=self.invite_expiry.currentData(),
        )

    def show_invite(self, token: str) -> None:
        self.invite_out.setText(token)

    def _member_table(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        for member in self.session.members.values():
            if member.get("role") == "owner":
                continue
            row = QHBoxLayout()
            fp = member["fp"]
            row.addWidget(QLabel(f"{member.get('name', '?')} · {short_fingerprint(fp)}"), 1)
            role = QComboBox()
            role.addItems(["editor", "viewer"])
            role.setCurrentText(member.get("role", "viewer"))
            role.activated.connect(
                lambda _=0, f=fp, w=role: self.session.send_admin(
                    "set_role", fp=f, role=w.currentText()
                )
            )
            row.addWidget(role)
            mute = QPushButton("Unmute" if member.get("muted") else "Mute")
            mute.clicked.connect(
                lambda _=False, f=fp, m=member: self.session.send_admin(
                    "mute", fp=f, muted=not m.get("muted")
                )
            )
            row.addWidget(mute)
            revoke = QPushButton("Revoke")
            revoke.setObjectName("danger")
            revoke.clicked.connect(lambda _=False, f=fp: self._remove("revoke", f))
            row.addWidget(revoke)
            kick = QPushButton("Kick")
            kick.setObjectName("danger")
            kick.clicked.connect(lambda _=False, f=fp: self._remove("kick", f))
            row.addWidget(kick)
            layout.addLayout(row)
        if layout.count() == 0:
            none = QLabel("Only you so far.")
            none.setObjectName("muted")
            layout.addWidget(none)
        return box

    def _remove(self, action: str, fingerprint: str) -> None:
        answer = QMessageBox.question(
            self, action.capitalize(), f"{action.capitalize()} this member?"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.session.send_admin(action, fp=fingerprint)
            self.accept()


def _plain(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;")


def _copy_row(label: str, value: str, can_copy: bool) -> QHBoxLayout:
    row = QHBoxLayout()
    caption = QLabel(label)
    caption.setObjectName("muted")
    row.addWidget(caption)
    field = QLineEdit(value)
    field.setReadOnly(True)
    row.addWidget(field, 1)
    if can_copy:
        button = QPushButton("Copy")
        button.clicked.connect(lambda: QApplication.clipboard().setText(value))
        row.addWidget(button)
    return row
