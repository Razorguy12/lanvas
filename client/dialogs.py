"""Modal dialogs from Design.md section 8. They only collect choices; callers do the work."""

from __future__ import annotations

import secrets
from typing import Any

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from client import icons, theme
from client.canvas.scene import BoardScene
from common.fileformat import LanvasFile, Trust
from common.identity import short_fingerprint
from common.model import Board
from common.protocol import TCP_PORT

MIN_PASSWORD_HINT = 10


class JoinByAddressDialog(QDialog):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Join by address")
        form = QFormLayout(self)
        self.host = QLineEdit()
        self.host.setPlaceholderText("192.168.1.10")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(TCP_PORT)
        self.code = QLineEdit()
        self.invite = QLineEdit()
        self.invite.setPlaceholderText("Optional")
        form.addRow("LAN address", self.host)
        form.addRow("Port", self.port)
        form.addRow("Room code", self.code)
        form.addRow("Invite code", self.invite)
        row = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        join = QPushButton("Join")
        join.setObjectName("primary")
        join.clicked.connect(self.accept)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(join)
        form.addRow(row)


class CreateRoomDialog(QDialog):
    def __init__(self, servers: list[Any], parent: Any = None, name: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Create room")
        self.setMinimumWidth(380)
        form = QFormLayout(self)
        self.name = QLineEdit(name)
        self.name.setPlaceholderText("Sprint-1")
        self.code = QLineEdit(secrets.token_hex(3).upper())
        self.server = QComboBox()
        for server in servers:
            label = f"{server.server_name} ({server.address})"
            self.server.addItem(label, (server.address, server.tcp_port))
        self.server.addItem("Another address…", None)
        self.host = QLineEdit("127.0.0.1")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(TCP_PORT)
        self.discoverable = QCheckBox("Discoverable on this network")
        self.discoverable.setChecked(True)
        self.require_code = QCheckBox("Require room code to request access")
        self.server.currentIndexChanged.connect(self._server_changed)
        form.addRow("Room name", self.name)
        form.addRow("Room code", self.code)
        form.addRow("Server", self.server)
        form.addRow("Address", self.host)
        form.addRow("Port", self.port)
        form.addRow(self.discoverable)
        form.addRow(self.require_code)
        row = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        create = QPushButton("Create room")
        create.setObjectName("primary")
        create.clicked.connect(self._try_accept)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(create)
        form.addRow(row)
        self._server_changed()

    def _server_changed(self) -> None:
        manual = self.server.currentData() is None
        self.host.setEnabled(manual)
        self.port.setEnabled(manual)

    def _try_accept(self) -> None:
        if self.name.text().strip():
            self.accept()
        else:
            self.name.setFocus()

    def target(self) -> tuple[str, int]:
        chosen = self.server.currentData()
        return chosen if chosen is not None else (self.host.text().strip(), self.port.value())


class AccessRequestDialog(QDialog):
    """The owner's decision. The fingerprint is there for the two people to compare out loud."""

    def __init__(self, request: dict[str, Any], room_name: str, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Access request")
        self.decision: tuple[str, str | None] | None = None
        layout = QVBoxLayout(self)
        title = QLabel(f"{request['name']} wants to join {room_name}")
        title.setObjectName("title")
        title.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(title)
        details = QLabel(
            f"Address: {request.get('ip', '')}\n"
            f"Fingerprint: {short_fingerprint(request['fingerprint'])}"
        )
        details.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(details)
        hint = QLabel("Ask them to read out their fingerprint. It should match this one.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        for label, decision, role, style in (
            ("Approve as Editor", "approve", "editor", "primary"),
            ("Approve as Viewer", "approve", "viewer", ""),
            ("Deny", "deny", None, "danger"),
            ("Block this device", "block", None, "danger"),
        ):
            button = QPushButton(label)
            if style:
                button.setObjectName(style)
            button.clicked.connect(lambda _=False, d=decision, r=role: self._choose(d, r))
            row.addWidget(button)
        layout.addLayout(row)

    def _choose(self, decision: str, role: str | None) -> None:
        self.decision = (decision, role)
        self.accept()


class RecoverDialog(QDialog):
    RECOVER, SAVE, DISCARD = 1, 2, 3

    def __init__(self, crashed: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Recover")
        self.choice = 0
        layout = QVBoxLayout(self)
        changes = len(crashed.ops)
        messages = len(crashed.chat)
        name = crashed.room_name or crashed.room_id
        text = QLabel(
            f"LANvas didn't close properly last time. {name} has {changes} unsaved "
            f"change{'s' if changes != 1 else ''} and {messages} unsent "
            f"message{'s' if messages != 1 else ''}."
        )
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(text)
        row = QHBoxLayout()
        discard = QPushButton("Discard")
        discard.clicked.connect(lambda: self._done(self.DISCARD))
        row.addWidget(discard)
        row.addStretch(1)
        save = QPushButton("Save recovered board as .lanvas…")
        save.clicked.connect(lambda: self._done(self.SAVE))
        row.addWidget(save)
        rejoin = QPushButton("Rejoin and send my changes")
        rejoin.setObjectName("primary")
        rejoin.setDefault(True)
        rejoin.clicked.connect(lambda: self._done(self.RECOVER))
        row.addWidget(rejoin)
        layout.addLayout(row)

    def _done(self, choice: int) -> None:
        self.choice = choice
        self.accept()


def trust_text(loaded: LanvasFile) -> tuple[str, str]:
    """(message, color token) for the header shown before anything else about a file."""
    t = theme.current()
    if loaded.trust is Trust.SIGNED:
        name = loaded.saved_by.get("name", "?")
        code = short_fingerprint(loaded.saved_by.get("fingerprint", ""))
        return f"✓ Signed by {name} ({code})", t.success
    return "⚠ This file was modified or is unsigned", t.warn


class OpenFileDialog(QDialog):
    """A read-only preview with its trust verdict on top, and the way to make a room from it."""

    VIEW, CREATE_ROOM = 1, 2

    def __init__(self, loaded: LanvasFile, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{loaded.room_name}.lanvas")
        self.resize(760, 560)
        self.choice = 0
        layout = QVBoxLayout(self)
        message, color = trust_text(loaded)
        banner = QLabel(message)
        banner.setTextFormat(Qt.TextFormat.PlainText)
        banner.setStyleSheet(f"color:{color}; font-weight:600;")
        layout.addWidget(banner)
        meta = QLabel(f"{loaded.room_name} · saved {loaded.saved_at} · version {loaded.seq}")
        meta.setTextFormat(Qt.TextFormat.PlainText)
        meta.setObjectName("muted")
        layout.addWidget(meta)
        self.scene = BoardScene(self)
        self.scene.sync(Board.from_json(loaded.board))
        view = QGraphicsView(self.scene)
        view.setRenderHints(QPainter.RenderHint.Antialiasing)
        view.setBackgroundBrush(_canvas_brush())
        self.view = view
        layout.addWidget(view, 1)
        if loaded.trust is not Trust.SIGNED:
            warn = QLabel(
                "Opened read-only. Only make a room from this if you trust where it came from."
            )
            warn.setObjectName("muted")
            layout.addWidget(warn)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Close")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        create = QPushButton("Create room from this file…")
        create.setObjectName("primary")
        create.clicked.connect(self._create)
        row.addWidget(create)
        layout.addLayout(row)

    def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt override)
        rect: QRectF = self.scene.content_rect()
        if not rect.isNull():
            self.view.fitInView(rect.adjusted(-30, -30, 30, 30), Qt.AspectRatioMode.KeepAspectRatio)

    def _create(self) -> None:
        self.choice = self.CREATE_ROOM
        self.accept()


def _canvas_brush() -> Any:
    from PyQt6.QtGui import QBrush, QColor

    return QBrush(QColor(theme.current().bg_canvas))


def ask_password(parent: Any, label: str = "Password") -> str | None:
    text, ok = QInputDialog.getText(parent, "Password", label, QLineEdit.EchoMode.Password)
    return text if ok else None


class PasswordFields(QWidget):
    """Password + confirm with a strength hint, used wherever a file can be protected."""

    def __init__(self) -> None:
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.first = QLineEdit()
        self.first.setEchoMode(QLineEdit.EchoMode.Password)
        self.second = QLineEdit()
        self.second.setEchoMode(QLineEdit.EchoMode.Password)
        self.hint = QLabel()
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        form.addRow("Password", self.first)
        form.addRow("Confirm", self.second)
        form.addRow(self.hint)
        self.first.textChanged.connect(self._update)
        self.second.textChanged.connect(self._update)
        self._update()

    def _update(self) -> None:
        strength = (
            "Longer is stronger." if len(self.first.text()) < MIN_PASSWORD_HINT else "Good length."
        )
        self.hint.setText(f"{strength} If you forget it, the file cannot be opened.")

    def valid(self) -> bool:
        return bool(self.first.text()) and self.first.text() == self.second.text()

    def password(self) -> str:
        return self.first.text()


class SaveCopyDialog(QDialog):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save a copy")
        layout = QVBoxLayout(self)
        self.include_chat = QCheckBox("Include room chat (private messages are never included)")
        layout.addWidget(self.include_chat)
        self.protect = QCheckBox("Protect with password")
        layout.addWidget(self.protect)
        self.fields = PasswordFields()
        self.fields.setVisible(False)
        self.protect.toggled.connect(self.fields.setVisible)
        layout.addWidget(self.fields)
        layout.addLayout(_ok_cancel(self, "Choose location…", self._try_accept))

    def _try_accept(self) -> None:
        if self.protect.isChecked() and not self.fields.valid():
            QMessageBox.warning(self, "Password", "The two passwords need to match.")
            return
        self.accept()

    def password(self) -> str | None:
        return self.fields.password() if self.protect.isChecked() else None


def _ok_cancel(dialog: QDialog, ok_text: str, on_ok: Any) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addStretch(1)
    cancel = QPushButton("Cancel")
    cancel.clicked.connect(dialog.reject)
    row.addWidget(cancel)
    ok = QPushButton(ok_text)
    ok.setObjectName("primary")
    ok.setDefault(True)
    ok.clicked.connect(on_ok)
    row.addWidget(ok)
    return row


FORMATS = ("PNG", "JPEG", "SVG", "PDF", ".lanvas")


class ExportDialog(QDialog):
    """Only the options relevant to the chosen format are enabled (Design.md section 8.4)."""

    def __init__(
        self,
        frames: list[tuple[str, str]],
        has_selection: bool,
        estimate: Any,
        allow_lanvas: bool = True,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export")
        self.setMinimumWidth(460)
        self.estimate = estimate
        form = QFormLayout(self)

        row = QHBoxLayout()
        self.format_group = QButtonGroup(self)
        for name in FORMATS:
            button = QRadioButton(name)
            self.format_group.addButton(button)
            row.addWidget(button)
            if name == "PNG":
                button.setChecked(True)
        form.addRow("Format", row)

        row = QHBoxLayout()
        self.scope_board = QRadioButton("Whole board")
        self.scope_board.setChecked(True)
        self.scope_selection = QRadioButton("Selection")
        self._has_selection = has_selection
        self.scope_frame = QRadioButton("Frame")
        self.scope_frame.setEnabled(bool(frames))
        self.frame_combo = QComboBox()
        for frame_id, name in frames:
            self.frame_combo.addItem(name, frame_id)
        for widget in (self.scope_board, self.scope_selection, self.scope_frame, self.frame_combo):
            row.addWidget(widget)
        form.addRow("What", row)

        row = QHBoxLayout()
        self.scale = QComboBox()
        for value in (1, 2, 4):
            self.scale.addItem(f"{value}x", value)
        self.transparent = QCheckBox("Transparent background")
        row.addWidget(self.scale)
        row.addWidget(self.transparent)
        form.addRow("PNG/JPEG", row)

        row = QHBoxLayout()
        self.page = QComboBox()
        self.page.addItem("Fit to content", "fit")
        self.page.addItem("A4 landscape", "A4")
        self.page.addItem("A3 landscape", "A3")
        self.each_frame = QCheckBox("Each frame as a page")
        self.each_frame.setEnabled(bool(frames))
        row.addWidget(self.page)
        row.addWidget(self.each_frame)
        form.addRow("PDF", row)

        self.include_chat = QCheckBox("Include room chat")
        self.protect = QCheckBox("Protect with password")
        row = QHBoxLayout()
        row.addWidget(self.include_chat)
        row.addWidget(self.protect)
        form.addRow(".lanvas", row)
        self.fields = PasswordFields()
        self.fields.setVisible(False)
        self.protect.toggled.connect(self._protect_toggled)
        form.addRow(self.fields)

        self.estimate_label = QLabel()
        self.estimate_label.setObjectName("muted")
        form.addRow(self.estimate_label)
        form.addRow(_ok_cancel(self, "Export…", self._try_accept))

        for signal in (
            self.format_group.buttonToggled,
            self.scope_board.toggled,
            self.scope_selection.toggled,
            self.scope_frame.toggled,
        ):
            signal.connect(self._refresh)
        self.scale.currentIndexChanged.connect(self._refresh)
        self.frame_combo.currentIndexChanged.connect(self._refresh)
        for button in self.format_group.buttons():
            button.setEnabled(button.text() != ".lanvas" or allow_lanvas)
        self._refresh()

    def _protect_toggled(self, on: bool) -> None:
        self.fields.setVisible(on)
        self.adjustSize()

    def format_name(self) -> str:
        return self.format_group.checkedButton().text()

    def _refresh(self, *_: Any) -> None:
        fmt = self.format_name()
        raster = fmt in ("PNG", "JPEG")
        self.scale.setEnabled(raster)
        self.transparent.setEnabled(fmt == "PNG")
        self.page.setEnabled(fmt == "PDF")
        self.each_frame.setEnabled(fmt == "PDF" and self.frame_combo.count() > 0)
        native = fmt == ".lanvas"
        self.include_chat.setEnabled(native)
        self.protect.setEnabled(native)
        for widget in (self.scope_board, self.scope_selection, self.scope_frame, self.frame_combo):
            widget.setEnabled(not native and self._scope_usable(widget))
        self.estimate_label.setText(self.estimate(self) if callable(self.estimate) else "")

    def _scope_usable(self, widget: QWidget) -> bool:
        if widget is self.scope_frame or widget is self.frame_combo:
            return self.frame_combo.count() > 0
        if widget is self.scope_selection:
            return self._has_selection
        return True

    def _try_accept(self) -> None:
        if self.protect.isChecked() and self.protect.isEnabled() and not self.fields.valid():
            QMessageBox.warning(self, "Password", "The two passwords need to match.")
            return
        self.accept()


class VersionHistoryDialog(QDialog):
    """Checkpoints from Ctrl+S. Restoring replaces the board for everyone, and can be undone."""

    def __init__(self, session: Any, checkpoints: list[dict[str, Any]], parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Version history")
        self.setMinimumSize(480, 360)
        self.session = session
        self.restored: str | None = None
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        for entry in reversed(checkpoints):
            author = entry.get("saved_by", {}).get("name", "?")
            label = entry.get("label") or "Saved"
            when = entry.get("time", "")[:19].replace("T", " ")
            digest = entry.get("board_hash", "")[7:15]
            item = QListWidgetItem(f"{when}  ·  {author}  ·  {label}  ·  {digest}")
            item.setData(Qt.ItemDataRole.UserRole, entry["checkpoint_id"])
            self.list.addItem(item)
        layout.addWidget(self.list)
        if not checkpoints:
            empty = QLabel("Nothing yet. Each Ctrl+S adds a version here.")
            empty.setObjectName("muted")
            layout.addWidget(empty)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        restore = QPushButton("Restore this version")
        restore.setObjectName("primary")
        restore.setEnabled(session.role == "owner")
        restore.clicked.connect(self._restore)
        row.addWidget(restore)
        layout.addLayout(row)

    def _restore(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        answer = QMessageBox.question(
            self,
            "Restore this version",
            "The board changes to this version for everyone. You can undo this with Ctrl+Z.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.restored = item.data(Qt.ItemDataRole.UserRole)
            self.accept()


class MemberPickerDialog(QDialog):
    def __init__(self, session: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New private message")
        self.chosen: str | None = None
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        names = [m.get("name") for m in session.members.values()]
        for member in session.members.values():
            if member["fp"] == session.fp:
                continue
            name = member.get("name", "?")
            # Fingerprints disambiguate only when needed: display names are allowed to collide.
            if names.count(name) > 1:
                name = f"{name} ({short_fingerprint(member['fp'])})"
            item = QListWidgetItem(name)
            item.setIcon(icons.icon("lock", theme.current().private))
            item.setData(Qt.ItemDataRole.UserRole, member["fp"])
            self.list.addItem(item)
        self.list.itemActivated.connect(self._choose)
        self.list.itemClicked.connect(self._choose)
        layout.addWidget(self.list)
        if self.list.count() == 0:
            none = QLabel("No other members in this room yet.")
            none.setObjectName("muted")
            layout.addWidget(none)

    def _choose(self, item: QListWidgetItem) -> None:
        self.chosen = item.data(Qt.ItemDataRole.UserRole)
        self.accept()
