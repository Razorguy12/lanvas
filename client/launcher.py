"""The first screen: boards appear by themselves as devices find them (Design.md section 8.1)."""

from __future__ import annotations

import tkinter as tk
from typing import Any

from client import icons, theme
from client.boardlist import BoardEntry
from client.widgets import (
    Button,
    Frame,
    Label,
    LineEdit,
    ScrollFrame,
    Signal,
    Spinner,
    ToolButton,
    after_ms,
    blend,
    section_label,
)

HINT_AFTER_MS = 10_000
HINT_TEXT = (
    "Make sure you're on the same Wi-Fi. Allow LANvas through your firewall (UDP 47800). "
    "Or use Join by address."
)


class LiveDot(tk.Canvas):
    """A pulsing dot, proof that listening is active rather than the app being stuck."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, width=10, height=10, highlightthickness=0,
                         background=theme.current().bg_app)
        self.alpha = 255
        self.step = -12
        self.dot = self.create_oval(1, 1, 9, 9, outline="")
        self._tick()

    def _tick(self) -> None:
        self.alpha += self.step
        if self.alpha <= 90 or self.alpha >= 255:
            self.step = -self.step
        t = theme.current()
        a = max(90, min(255, self.alpha)) / 255
        self.itemconfigure(self.dot, fill=blend(t.success, t.bg_app, a))
        after_ms(self, 60, self._tick)


class BoardRow(Frame):
    def __init__(self, entry: BoardEntry, master: tk.Misc | None = None) -> None:
        super().__init__(master if master is not None else tk._default_root, "card",
                         padding=(12, 8, 8, 8))
        self.entry = entry
        self.join = Signal()
        t = theme.current()
        row = tk.Frame(self, background=t.bg_panel)
        row.pack(fill="x")
        name = Label(row, entry.name if entry.compatible else f"{entry.name} (needs update)",
                     "title")
        name.pack(side="left", fill="x", expand=True)
        host = entry.server_name + (f" · {entry.host_name}" if entry.host_name else "")
        if not entry.compatible:
            host = f"v{entry.protocol_version} server"
        where = Label(row, host, "panelmuted")
        where.pack(side="left", fill="x", expand=True)
        count = "—" if entry.member_count is None else _people(entry.member_count)
        Label(row, count, "panelmuted").pack(side="left", padx=6)
        self.widgets: list[Any] = [row, name, where]
        if entry.compatible:
            lock = Label(row, "", "panel", image=icons.photo("lock", t.text_muted, 14, row))
            lock.setToolTip("Needs owner approval")
            lock.pack(side="left")
            Label(row, "approval", "panelmuted").pack(side="left", padx=(2, 8))
            self.join_button = Button(row, "Join", "primary")
            self.join_button.clicked.connect(self._clicked)
            self.join_button.pack(side="left")
        else:
            name.configure(foreground=t.text_muted)
            self.state(["disabled"])  # shown so that people can tell why it isn't joinable
        self.code = LineEdit(self, placeholder="This room needs its code")
        self.code.returnPressed.connect(self._clicked)
        self.code.pack(fill="x", pady=(6, 0))
        self.code.setVisible(False)
        for widget in (self, row, name, where):
            widget.bind("<Double-Button-1>", self._double, add="+")

    def _clicked(self) -> None:
        if self.entry.require_code and not self.code.text().strip():
            self.code.setVisible(True)
            self.code.setFocus()
            return
        self.join.emit(self.entry, self.code.text().strip())

    def _double(self, _event: Any = None) -> None:
        if self.entry.compatible:
            self._clicked()


def _people(count: int) -> str:
    return f"{count} {'person' if count == 1 else 'people'}"


class LauncherPage(Frame):
    def __init__(self, master: tk.Misc, username: str) -> None:
        super().__init__(master, "page")
        self.join_board = Signal()
        self.join_by_address = Signal()
        self.create_room = Signal()
        self.open_file = Signal()
        self.refresh = Signal()
        self.recover = Signal()
        self.name_changed = Signal()
        self.rows: dict[tuple[str, str], BoardRow] = {}
        t = theme.current()

        self.banner = Frame(self, "banner", padding=(16, 6))
        self.banner.pack(fill="x")
        self.banner_text = Label(self.banner, "LANvas didn't close properly last time.", "banner")
        self.banner_text.pack(side="left", fill="x", expand=True)
        recover = Button(self.banner, "Recover…", "banner")
        recover.clicked.connect(self.recover.emit)
        recover.pack(side="right")
        self.banner.setVisible(False)

        center = Frame(self, "page")
        center.pack(fill="both", expand=True)
        center.columnconfigure(0, weight=1)
        center.columnconfigure(1, weight=6)
        center.columnconfigure(2, weight=1)
        center.rowconfigure(0, weight=1)
        column = Frame(center, "page", padding=24)
        column.grid(row=0, column=1, sticky="nsew")
        column.columnconfigure(0, weight=1)

        Label(column, "LANvas", "hero").grid(row=0, column=0, sticky="w", pady=(0, 12))
        row = Frame(column, "page")
        row.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        Label(row, "Your name", "muted").pack(side="left", padx=(0, 8))
        self.name = LineEdit(row, username, max_length=64)
        self.name.textChanged.connect(self.name_changed.emit)
        self.name.pack(side="left", fill="x", expand=True)

        heading = Frame(column, "page")
        heading.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        section_label(heading, "Boards on this network", on_panel=False).pack(side="left")
        refresh = ToolButton(heading, "refresh", tooltip="Search again now")
        refresh.configure(style="Page.Tool.TCheckbutton")
        refresh.clicked.connect(self.refresh.emit)
        refresh.pack(side="right")
        Label(heading, "live", "muted").pack(side="right", padx=(4, 8))
        LiveDot(heading).pack(side="right")

        self.area = ScrollFrame(column)
        self.area.grid(row=3, column=0, sticky="nsew")
        column.rowconfigure(3, weight=1)
        self.list_body = self.area.body

        self.empty = Frame(column, "page", padding=(0, 12))
        self.empty.grid(row=4, column=0, sticky="ew")
        looking = Frame(self.empty, "page")
        looking.pack()
        Spinner(looking, background=t.bg_app).pack(side="left", padx=(0, 8))
        Label(looking, "Looking for boards on your network…", "muted").pack(side="left")
        self.hints = Label(self.empty, HINT_TEXT, "muted", justify="center", wraplength=520)
        self.hints.pack(pady=(8, 0))
        self.hints.setVisible(False)
        after_ms(self, HINT_AFTER_MS, lambda: self.hints.setVisible(True))

        actions = Frame(column, "page")
        actions.grid(row=5, column=0, sticky="w", pady=(12, 0))
        for text, signal in (("Join by address…", self.join_by_address),
                             ("Create room…", self.create_room),
                             ("Open .lanvas…", self.open_file)):
            button = Button(actions, text)
            button.clicked.connect(signal.emit)
            button.pack(side="left", padx=(0, 6))

        self.message = Label(column, "", "danger", wraplength=640, justify="left")
        self.message.grid(row=6, column=0, sticky="w", pady=(8, 0))
        self.message.setVisible(False)

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
            self.rows.pop(key).destroy()
        for board in boards:
            key = (board.server_id, board.room_id)
            row = self.rows.get(key)
            if row is None or row.entry != board:
                if row is not None:
                    row.destroy()
                row = BoardRow(board, self.list_body)
                row.join.connect(self.join_board.emit)
                self.rows[key] = row
        for board in boards:
            row = self.rows[(board.server_id, board.room_id)]
            row.pack_forget()
            row.pack(fill="x", pady=(0, 6))
        self.empty.setVisible(not boards)

