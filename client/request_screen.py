"""The "Request access" screen. Deliberately renders no board, chat or user list of any kind."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from client import theme
from client.widgets import Spinner
from common.identity import short_fingerprint

STATE_WAITING = "waiting"
STATE_APPROVED = "approved"
STATE_DENIED = "denied"


class RequestAccessPage(QWidget):
    cancel = pyqtSignal()
    retry = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.remaining = 0
        root = QVBoxLayout(self)
        root.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(420)
        column = QVBoxLayout(card)
        column.setContentsMargins(24, 24, 24, 24)
        column.setSpacing(10)
        self.room = QLabel()
        self.room.setObjectName("hero")
        self.room.setTextFormat(Qt.TextFormat.PlainText)
        self.room.setWordWrap(True)
        column.addWidget(self.room)
        self.owner = QLabel()
        self.owner.setObjectName("muted")
        self.owner.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(self.owner)
        state_row = QHBoxLayout()
        self.spinner = Spinner()
        state_row.addWidget(self.spinner)
        self.state = QLabel()
        self.state.setWordWrap(True)
        state_row.addWidget(self.state, 1)
        column.addLayout(state_row)
        finger_label = QLabel("Your fingerprint, for the owner to check with you:")
        finger_label.setObjectName("muted")
        finger_label.setWordWrap(True)
        column.addWidget(finger_label)
        self.fingerprint = QLabel()
        self.fingerprint.setObjectName("title")
        self.fingerprint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.fingerprint)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.retry_button = QPushButton("Ask again")
        self.retry_button.clicked.connect(self.retry.emit)
        self.retry_button.setVisible(False)
        buttons.addWidget(self.retry_button)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.cancel.emit)
        buttons.addWidget(cancel)
        column.addLayout(buttons)
        row.addWidget(card)
        row.addStretch(1)
        root.addLayout(row)
        root.addStretch(1)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)

    def paintEvent(self, event: Any) -> None:  # noqa: N802 (Qt override)
        """A soft abstract backdrop. It is a placeholder drawn from nothing, not real board data."""
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, QColor(theme.current().bg_canvas))
        gradient.setColorAt(1, QColor(theme.current().bg_app))
        painter.fillRect(self.rect(), gradient)

    def set_info(self, room: str, owner: str, fingerprint: str) -> None:
        self.room.setText(room)
        self.owner.setText(f"Owner: {owner}" if owner else "")
        self.fingerprint.setText(short_fingerprint(fingerprint))
        self.show_state(STATE_WAITING)

    def show_connecting(self, target: str) -> None:
        self.room.setText("Connecting…")
        self.owner.setText(target)
        self.fingerprint.setText("")
        self._state_text("Reaching the server…", theme.current().text_muted)
        self.spinner.setVisible(True)

    def show_state(self, state: str, retry_after: float | None = None) -> None:
        t = theme.current()
        self.timer.stop()
        self.retry_button.setVisible(False)
        self.spinner.setVisible(state == STATE_WAITING)
        if state == STATE_WAITING:
            self._state_text("Request sent, waiting for approval…", t.warn)
        elif state == STATE_APPROVED:
            self._state_text("Approved", t.success)
        else:
            if retry_after is None:
                self._state_text("The owner has blocked this device.", t.danger)
                return
            self.remaining = int(retry_after) + 1
            self.timer.start()
            self._tick()

    def _state_text(self, text: str, color: str) -> None:
        self.state.setText(text)
        self.state.setStyleSheet(f"color:{color}; font-weight:600;")

    def _tick(self) -> None:
        t = theme.current()
        self.remaining -= 1
        if self.remaining > 0:
            self._state_text(f"Denied. You can ask again in {self.remaining} s.", t.danger)
        else:
            self.timer.stop()
            self._state_text("Denied. You can ask again now.", t.danger)
            self.retry_button.setVisible(True)
