"""Comments panel: pins on the canvas, threads, and a resolve toggle."""

from __future__ import annotations

import tkinter as tk
from typing import Any

from client.widgets import (
    Button,
    CheckBox,
    Frame,
    Label,
    LineEdit,
    ListWidget,
    TextView,
    section_label,
)


class CommentsPanel(Frame):
    def __init__(self, master: tk.Misc, canvas: Any) -> None:
        super().__init__(master, "panel", padding=12)
        self.canvas = canvas
        self.current: str | None = None
        section_label(self, "Comments").pack(anchor="w")
        self.show_resolved = CheckBox(self, "Show resolved", panel=True)
        self.show_resolved.toggled.connect(lambda *_: self.refresh())
        self.show_resolved.pack(anchor="w", pady=(6, 4))
        self.list = ListWidget(self, height=7)
        self.list.itemClicked.connect(self._clicked)
        self.list.pack(fill="x")
        self.hint = Label(self, "Press C, then click the canvas to pin a comment.", "panelmuted",
                          wraplength=240, justify="left")
        self.hint.pack(anchor="w", pady=4)
        self.thread = TextView(self)
        self.thread.pack(fill="both", expand=True, pady=(4, 4))
        row = Frame(self, "panel")
        row.pack(fill="x")
        self.reply = LineEdit(row, placeholder="Reply…")
        self.reply.returnPressed.connect(self._send_reply)
        self.reply.pack(side="left", fill="x", expand=True)
        self.resolve = Button(row, "Resolve")
        self.resolve.clicked.connect(self._toggle_resolved)
        self.resolve.pack(side="left", padx=(6, 0))
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
            self.list.addItem(f"{mark}{props.get('author', '?')}: {first_line}", comment["id"],
                              selected=comment["id"] == self.current)
        self.hint.setVisible(self.list.count() == 0)
        self._render_thread()

    def _clicked(self, obj_id: str | None) -> None:
        self.current = obj_id
        comment = self.canvas.doc().view.get_live(self.current) if obj_id else None
        if comment is not None:
            self.canvas.centerOn(comment["props"].get("x", 0), comment["props"].get("y", 0))
        self._render_thread()

    def _render_thread(self) -> None:
        comment = self.canvas.doc().view.get_live(self.current) if self.current else None
        can_edit = self.canvas.can_edit()
        self.reply.setEnabled(comment is not None and can_edit)
        self.resolve.setEnabled(comment is not None and can_edit)
        view = self.thread
        view.begin()
        if comment is not None:
            props = comment["props"]
            self.resolve.setText("Reopen" if props.get("resolved") else "Resolve")
            # Comment text comes from other users, so it is inserted as plain text, never markup.
            view.write(props.get("author", "?") + "\n", "bold")
            view.write(props.get("text", "") + "\n\n")
            for reply in props.get("replies", []):
                view.write(reply.get("author", "?"), "bold")
                view.write(" " + reply.get("ts", "") + "\n", "muted")
                view.write(reply.get("text", "") + "\n\n")
        view.end()

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
