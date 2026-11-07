"""The first screen: boards appear by themselves as devices find them (Design.md section 8.1)."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from client import icons, theme
from client.boardlist import BoardEntry
from client.widgets import Spinner, section_label

HINT_AFTER_MS = 10_000
HINT_TEXT = (
    "Make sure you're on the same Wi-Fi. Allow LANvas through your firewall (UDP 47800). "
    "Or use Join by address."
)


class LiveDot(QWidget):
    """A pulsing dot, proof that listening is active rather than the app being stuck."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(10, 10)
        self.alpha = 255
        self.step = -12
        timer = QTimer(self)
        timer.timeout.connect(self._tick)
        timer.start(60)

    def _tick(self) -> None:
        self.alpha += self.step
        if self.alpha <= 90 or self.alpha >= 255:
            self.step = -self.step
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(theme.current().success)
        color.setAlpha(max(90, min(255, self.alpha)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(self.rect().adjusted(1, 1, -1, -1))


class BoardRow(QFrame):
    join = pyqtSignal(object, str)

    def __init__(self, entry: BoardEntry) -> None:
        super().__init__()
        self.entry = entry
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 8, 8)
        row = QHBoxLayout()
        name = QLabel(entry.name if entry.compatible else f"{entry.name} (needs update)")
        name.setObjectName("title")
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(name, 2)
        host = entry.server_name + (f" · {entry.host_name}" if entry.host_name else "")
        if not entry.compatible:
            host = f"v{entry.protocol_version} server"
        where = QLabel(host)
        where.setObjectName("muted")
        where.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(where, 2)
        count = "—" if entry.member_count is None else _people(entry.member_count)
        people = QLabel(count)
        people.setObjectName("muted")
        row.addWidget(people)
        if entry.compatible:
            lock = QLabel()
            lock.setPixmap(icons.pixmap("lock", theme.current().text_muted, 28).scaled(14, 14))
            lock.setToolTip("Needs owner approval")
            row.addWidget(lock)
            approval = QLabel("approval")
            approval.setObjectName("muted")
            row.addWidget(approval)
            self.join_button = QPushButton("Join")
            self.join_button.setObjectName("primary")
            self.join_button.clicked.connect(self._clicked)
            row.addWidget(self.join_button)
        else:
            self.setEnabled(False)  # shown so that people can tell why it isn't joinable
        layout.addLayout(row)
        self.code = QLineEdit()
        self.code.setPlaceholderText("This room needs its code")
        self.code.setVisible(False)
        self.code.returnPressed.connect(self._clicked)
        layout.addWidget(self.code)

    def _clicked(self) -> None:
        if self.entry.require_code and not self.code.text().strip():
            self.code.setVisible(True)
            self.code.setFocus()
            return
        self.join.emit(self.entry, self.code.text().strip())

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802
        if self.entry.compatible:
            self._clicked()


def _people(count: int) -> str:
    return f"{count} {'person' if count == 1 else 'people'}"


class LauncherPage(QWidget):
    join_board = pyqtSignal(object, str)
    join_by_address = pyqtSignal()
    create_room = pyqtSignal()
    refresh = pyqtSignal()
    recover = pyqtSignal()
    name_changed = pyqtSignal(str)

    def __init__(self, username: str) -> None:
        super().__init__()
        self.setObjectName("page")
        self.rows: dict[tuple[str, str], BoardRow] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.banner = QWidget()
        self.banner.setObjectName("banner")
        banner_row = QHBoxLayout(self.banner)
        banner_row.setContentsMargins(16, 6, 16, 6)
        self.banner_text = QLabel("LANvas didn't close properly last time.")
        banner_row.addWidget(self.banner_text, 1)
        recover = QPushButton("Recover…")
        recover.clicked.connect(self.recover.emit)
        banner_row.addWidget(recover)
        self.banner.setVisible(False)
        outer.addWidget(self.banner)

        center = QHBoxLayout()
        outer.addLayout(center, 1)
        center.addStretch(1)
        column = QVBoxLayout()
        column.setContentsMargins(24, 24, 24, 24)
        column.setSpacing(12)
        center.addLayout(column, 6)
        center.addStretch(1)

        title = QLabel("LANvas")
        title.setObjectName("hero")
        column.addWidget(title)
        row = QHBoxLayout()
        label = QLabel("Your name")
        label.setObjectName("muted")
        row.addWidget(label)
        self.name = QLineEdit(username)
        self.name.setMaxLength(64)
        self.name.textChanged.connect(self.name_changed.emit)
        row.addWidget(self.name, 1)
        column.addLayout(row)

        heading = QHBoxLayout()
        heading.addWidget(section_label("Boards on this network"))
        heading.addStretch(1)
        heading.addWidget(LiveDot())
        live = QLabel("live")
        live.setObjectName("muted")
        heading.addWidget(live)
        refresh = QToolButton()
        refresh.setIcon(icons.icon("refresh", theme.current().text_primary))
        refresh.setToolTip("Search again now")
        refresh.clicked.connect(self.refresh.emit)
        heading.addWidget(refresh)
        column.addLayout(heading)

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName("page")
        self.list_layout = QVBoxLayout(body)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.addStretch(1)
        self.area.setWidget(body)
        column.addWidget(self.area, 1)

        self.empty = QWidget()
        empty_layout = QVBoxLayout(self.empty)
        looking = QHBoxLayout()
        looking.addStretch(1)
        looking.addWidget(Spinner())
        text = QLabel("Looking for boards on your network…")
        text.setObjectName("muted")
        looking.addWidget(text)
        looking.addStretch(1)
        empty_layout.addLayout(looking)
        self.hints = QLabel(HINT_TEXT)
        self.hints.setObjectName("muted")
        self.hints.setWordWrap(True)
        self.hints.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hints.setVisible(False)
        empty_layout.addWidget(self.hints)
        column.addWidget(self.empty)
        QTimer.singleShot(HINT_AFTER_MS, lambda: self.hints.setVisible(True))

        actions = QHBoxLayout()
        by_address = QPushButton("Join by address…")
        by_address.clicked.connect(self.join_by_address.emit)
        actions.addWidget(by_address)
        create = QPushButton("Create room…")
        create.clicked.connect(self.create_room.emit)
        actions.addWidget(create)
        actions.addStretch(1)
        column.addLayout(actions)

        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setStyleSheet(f"color:{theme.current().danger};")
        self.message.setVisible(False)
        column.addWidget(self.message)

    def show_message(self, text: str) -> None:
        self.message.setText(text)
        self.message.setVisible(bool(text))

    def set_recovery(self, text: str | None) -> None:
        self.banner.setVisible(text is not None)
        if text:
            self.banner_text.setText(text)

    def username(self) -> str:
        return self.name.text().strip() or "Anonymous"

    def set_boards(self, boards: list[BoardEntry]) -> None:
        """Diff-based: unchanged rows stay in place, so they never flicker or lose a typed code."""
        wanted = {(b.server_id, b.room_id): b for b in boards}
        for key in [k for k in self.rows if k not in wanted]:
            row = self.rows.pop(key)
            self.list_layout.removeWidget(row)
            row.deleteLater()
        for index, board in enumerate(boards):
            key = (board.server_id, board.room_id)
            row = self.rows.get(key)
            if row is None or row.entry != board:
                if row is not None:
                    self.list_layout.removeWidget(row)
                    row.deleteLater()
                row = BoardRow(board)
                row.join.connect(self.join_board.emit)
                self.rows[key] = row
            self.list_layout.insertWidget(index, row)
        self.empty.setVisible(not boards)
