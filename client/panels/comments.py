"""Comments panel: pins on the canvas, threads, and a resolve toggle."""

from __future__ import annotations

import html
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from client import theme
from client.widgets import section_label


class CommentsPanel(QWidget):
    def __init__(self, canvas: Any) -> None:
        super().__init__()
        self.canvas = canvas
        self.current: str | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.addWidget(section_label("Comments"))
        self.show_resolved = QCheckBox("Show resolved")
        self.show_resolved.toggled.connect(self.refresh)
        root.addWidget(self.show_resolved)
        self.list = QListWidget()
        self.list.setMaximumHeight(180)
        self.list.itemClicked.connect(self._clicked)
        root.addWidget(self.list)
        self.hint = QLabel("Press C, then click the canvas to pin a comment.")
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)
        self.thread = QTextBrowser()
        root.addWidget(self.thread, 1)
        row = QHBoxLayout()
        self.reply = QLineEdit()
        self.reply.setPlaceholderText("Reply…")
        self.reply.returnPressed.connect(self._send_reply)
        row.addWidget(self.reply, 1)
        self.resolve = QPushButton("Resolve")
        self.resolve.clicked.connect(self._toggle_resolved)
        row.addWidget(self.resolve)
        root.addLayout(row)
        self.refresh()

    def _comments(self) -> list[dict[str, Any]]:
        return [o for o in self.canvas.doc().view.live() if o["type"] == "comment"]

    def open_comment(self, obj_id: str) -> None:
        self.current = obj_id
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        for comment in self._comments():
            props = comment["props"]
            if props.get("resolved") and not self.show_resolved.isChecked():
                continue
            mark = "✓ " if props.get("resolved") else ""
            first_line = props.get("text", "").split("\n")[0][:40]
            item = QListWidgetItem(f"{mark}{props.get('author', '?')}: {first_line}")
            item.setData(Qt.ItemDataRole.UserRole, comment["id"])
            self.list.addItem(item)
            item.setSelected(comment["id"] == self.current)
        self.hint.setVisible(self.list.count() == 0)
        self._render_thread()

    def _clicked(self, item: QListWidgetItem) -> None:
        self.current = item.data(Qt.ItemDataRole.UserRole)
        comment = self.canvas.doc().view.get_live(self.current)
        if comment is not None:
            self.canvas.centerOn(comment["props"].get("x", 0), comment["props"].get("y", 0))
        self._render_thread()

    def _render_thread(self) -> None:
        comment = self.canvas.doc().view.get_live(self.current) if self.current else None
        can_edit = self.canvas.can_edit()
        self.reply.setEnabled(comment is not None and can_edit)
        self.resolve.setEnabled(comment is not None and can_edit)
        if comment is None:
            self.thread.setHtml("")
            return
        props = comment["props"]
        self.resolve.setText("Reopen" if props.get("resolved") else "Resolve")
        muted = theme.current().text_muted
        # Comment text comes from other users, so it is escaped before being rendered as HTML.
        parts = [
            f"<p><b>{html.escape(props.get('author', '?'))}</b><br>"
            f"{html.escape(props.get('text', '')).replace(chr(10), '<br>')}</p>"
        ]
        for reply in props.get("replies", []):
            parts.append(
                f"<p><b>{html.escape(reply.get('author', '?'))}</b> "
                f"<span style='color:{muted}'>{html.escape(reply.get('ts', ''))}</span><br>"
                f"{html.escape(reply.get('text', '')).replace(chr(10), '<br>')}</p>"
            )
        self.thread.setHtml("".join(parts))

    def _send_reply(self) -> None:
        text = self.reply.text().strip()
        comment = self.canvas.doc().view.get_live(self.current) if self.current else None
        if not text or comment is None:
            return
        replies = list(comment["props"].get("replies", []))
        replies.append({"author": self.canvas.session.username, "text": text[:20000], "ts": ""})
        self.canvas.session.update(comment["id"], {"replies": replies})
        self.reply.clear()
        self.refresh()

    def _toggle_resolved(self) -> None:
        comment = self.canvas.doc().view.get_live(self.current) if self.current else None
        if comment is not None:
            resolved = not comment["props"].get("resolved", False)
            self.canvas.session.update(comment["id"], {"resolved": resolved})
            self.refresh()
