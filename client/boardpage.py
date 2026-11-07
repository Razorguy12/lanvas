"""The board window: top bar, tool rail, panels, status bar, and the commands connecting them."""

from __future__ import annotations

import base64
import io
import logging
import os
import subprocess
import sys
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any

from PIL import Image

from client import dialogs, icons, theme
from client.boardlist import BoardEntry
from client.canvas.view import CanvasView
from client.export.lanvas import export_lanvas
from client.export.pdf import export_pdf
from client.export.png import export_raster, raster_size
from client.export.renderer import (
    SCOPE_BOARD,
    SCOPE_FRAME,
    SCOPE_SELECTION,
    ExportError,
    Scope,
)
from client.export.svg import export_svg
from client.panels.access import AccessDialog
from client.panels.chat import ChatPanel
from client.panels.comments import CommentsPanel
from client.panels.design import DesignPanel
from client.panels.inspector import InspectorPanel
from client.panels.layers import LayersPanel
from client.panels.people import PeoplePopover
from client.recovery import IoWorker
from client.session import (
    SAVE_EDITED,
    SAVE_FAILED,
    SAVE_IDLE,
    SAVE_LOCAL_ONLY,
    SAVE_SAVED,
    SAVE_SAVING,
)
from client.widgets import (
    Avatar,
    Button,
    ColorButton,
    Frame,
    Label,
    Signal,
    ToolButton,
    after_ms,
    ask_yes_no,
    call_in_ui,
    popup_menu,
)
from common import durable
from common.identity import short_fingerprint
from common.model import MAX_IMAGE_B64

log = logging.getLogger(__name__)

TOOLS = (
    ("select", "select", "Move / Select (V)", "V"),
    ("frame", "frame", "Frame (F)", "F"),
    ("rect", "rect", "Rectangle (R)", "R"),
    ("ellipse", "ellipse", "Ellipse (O)", "O"),
    ("line", "line", "Line (L)", "L"),
    ("arrow", "arrow", "Arrow (Shift+L)", "Shift+L"),
    ("pen", "pen", "Pen (P)", "P"),
    ("eraser", "eraser", "Eraser (E)", "E"),
    ("text", "text", "Text (T)", "T"),
    ("sticky", "sticky", "Sticky note (S)", "S"),
    ("comment", "comment", "Comment (C)", "C"),
)
ALIGN_KEYS = (
    ("Alt+A", "left"),
    ("Alt+D", "right"),
    ("Alt+W", "top"),
    ("Alt+S", "bottom"),
    ("Alt+H", "hcenter"),
    ("Alt+V", "vcenter"),
)
ROLE_LABEL = {"owner": "OWNER", "editor": "EDITOR", "viewer": "VIEWER"}
OP_REJECT_TEXT = {
    "NOT_ALLOWED": "You don't have permission to do that.",
    "ROOM_LOCKED": "The owner has locked this board.",
    "LIMIT_EXCEEDED": "That's too large to add to the board.",
    "NOT_FOUND": "That version no longer exists.",
}
MAX_IMAGE_SIDE = 1600
ESTIMATE_BYTES_PER_PIXEL = 0.5
EXPORT_TICK_MS = 0
TYPING_CLASSES = ("Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "TCombobox")
SPECIAL_KEYS = {
    "Del": "Delete", "Backspace": "BackSpace", "]": "bracketright", "[": "bracketleft",
    "\\": "backslash", "1": "1", "0": "0",
}


def key_sequences(keys: str) -> list[str]:
    """A Qt-style shortcut ("Ctrl+Shift+Z") as Tk event sequences."""
    *mods, key = keys.split("+")
    shift = "Shift" in mods
    tk_mods = [{"Ctrl": "Control", "Alt": "Alt"}[m] for m in mods if m != "Shift"]
    if key in SPECIAL_KEYS:
        name = SPECIAL_KEYS[key]
        if shift and key == "1":
            return ["<KeyPress-exclam>"]  # Shift+1 arrives as "!"
        prefix = "-".join(tk_mods + (["Shift"] if shift else []))
        return [f"<{prefix}-{name}>" if prefix else f"<KeyPress-{name}>"]
    letter = key.upper() if shift else key.lower()
    prefix = "-".join(tk_mods)
    return [f"<{prefix}-{letter}>" if prefix else f"<KeyPress-{letter}>"]


class Exporter:
    """Runs a chunked render on the UI thread between events, then writes the file off-thread."""

    def __init__(self, parent: tk.Misc, io_worker: IoWorker) -> None:
        self.parent_widget = parent
        self.io = io_worker
        self.finished = Signal()  # path, error
        self.steps: Any = None
        self.path = Path()
        self.total = 0
        self.progress: tk.Toplevel | None = None
        self.bar: ttk.Progressbar | None = None

    def start(self, steps: Any, path: Path, total: int) -> None:
        self.steps = steps
        self.path = path
        self.total = max(total, 1)
        if total >= 200:
            self.progress = tk.Toplevel(self.parent_widget)
            self.progress.title("Exporting…")
            self.progress.transient(self.parent_widget.winfo_toplevel())
            Label(self.progress, "Exporting…").pack(padx=16, pady=(12, 4))
            self.bar = ttk.Progressbar(self.progress, length=280, maximum=self.total)
            self.bar.pack(padx=16, pady=(0, 14))
        after_ms(self.parent_widget, EXPORT_TICK_MS, self._step)

    def _step(self) -> None:
        try:
            done = next(self.steps)
            if self.bar is not None:
                self.bar.configure(value=min(done, self.total))
            after_ms(self.parent_widget, EXPORT_TICK_MS, self._step)
        except StopIteration as stop:
            self._close_progress()
            self.write(stop.value, self.path)
        except ExportError as exc:
            self._close_progress()
            self.finished.emit(str(self.path), str(exc))

    def _close_progress(self) -> None:
        if self.progress is not None:
            self.progress.destroy()
            self.progress = None
            self.bar = None

    def write(self, data: bytes, path: Path) -> None:
        def job() -> None:
            try:
                durable.atomic_write(path, data)
                call_in_ui(lambda: self.finished.emit(str(path), ""))
            except OSError as exc:
                error = str(exc)
                call_in_ui(lambda: self.finished.emit(str(path), error))

        self.io.submit(job)


class BoardPage(Frame):
    def __init__(self, master: tk.Misc, session: Any, get_core: Callable[[], Any],
                 discovery: Any) -> None:
        super().__init__(master, "page")
        self.leave_requested = Signal()
        self.switch_requested = Signal()
        self.open_file_requested = Signal()
        self.session = session
        self.get_core = get_core
        self.discovery = discovery
        self.boards: list[BoardEntry] = []
        self.access_dialog: AccessDialog | None = None
        self._request_toasts: dict[str, Any] = {}
        self._popover: PeoplePopover | None = None
        self._bound: list[str] = []
        self.io = IoWorker()
        self.exporter = Exporter(self, self.io)
        self.exporter.finished.connect(self._export_finished)
        self.tool_buttons: dict[str, ToolButton] = {}
        self._build()
        self._bind_shortcuts()
        self.canvas.comment_requested.connect(self._open_comment)
        self.canvas.tool_changed.connect(self._tool_changed)
        self.canvas.zoom_changed.connect(lambda f: self.zoom_button.setText(f"{f * 100:.0f}%"))
        self.canvas.selection_changed.connect(self._status_refresh)
        self.rtt: float | None = None
        self.conn_state = "connecting"
        self._clock: str | None = after_ms(self, 1000, self._second)
        self.apply_role()

    # ------------------------------------------------------------------ construction
    def _build(self) -> None:
        t = theme.current()
        self.topbar = self._build_topbar()
        self.topbar.pack(fill="x")
        tk.Frame(self, height=1, background=t.border).pack(fill="x")
        self.reconnect_banner = Label(self, "Reconnecting… your changes are saved locally",
                                      "banner", anchor="center", padding=(12, 6))
        self.reconnect_banner.pack(fill="x")
        self.reconnect_banner.setVisible(False)
        self.statusbar = self._build_statusbar()
        self.statusbar.pack(side="bottom", fill="x")
        tk.Frame(self, height=1, background=t.border).pack(side="bottom", fill="x")
        self.middle = ttk.PanedWindow(self, orient="vertical")
        self.middle.pack(fill="both", expand=True)
        holder = Frame(self.middle, "page")
        self.middle.add(holder, weight=1)
        self.rail = self._build_rail(holder)
        self.rail.pack(side="left", fill="y")
        self.rail_border = tk.Frame(holder, width=1, background=t.border)
        self.rail_border.pack(side="left", fill="y")
        self.split = ttk.PanedWindow(holder, orient="horizontal")
        self.split.pack(side="left", fill="both", expand=True)
        self.canvas = CanvasView(self.session, self.split)
        self.layers = LayersPanel(self.split, self.canvas)
        self.right = self._build_right(self.split)
        self.split.add(self.layers, weight=0)
        self.split.add(self.canvas, weight=1)
        self.split.add(self.right, weight=0)
        self._pane_widths = [240, 280]  # layers, right panel; the canvas takes the rest
        self.split.bind("<Configure>", self._split_resized, add="+")
        self.split.bind("<ButtonRelease-1>", self._sash_moved, add="+")
        self.inspector = InspectorPanel(self.middle, self.get_core, self.discovery)
        self._inspector_shown = False
        self._ui_shown = True

    def _split_resized(self, event: Any = None) -> None:
        """Side panels keep their width as the window resizes (Qt's stretch factors)."""
        if not self._ui_shown:
            return
        width = self.split.winfo_width()
        left, right = self._pane_widths
        try:
            self.split.sashpos(0, left)
            self.split.sashpos(1, max(left + 200, width - right))
        except tk.TclError:
            pass

    def _sash_moved(self, _event: Any = None) -> None:
        if self._ui_shown:
            try:
                width = self.split.winfo_width()
                self._pane_widths = [self.split.sashpos(0), width - self.split.sashpos(1)]
            except tk.TclError:
                pass

    def _build_topbar(self) -> Frame:
        t = theme.current()
        bar = Frame(self, "bar", padding=(8, 4))
        menu_icon = icons.photo("menu", t.text_primary, master=bar)
        self.menu_button = Button(bar, "Room", "room", image=menu_icon)
        self.menu_button.clicked.connect(self._open_room_menu)
        self.menu_button.pack(side="left")
        share_icon = icons.photo("share", t.text_primary, master=bar)
        self.share_button = Button(bar, "Share", image=share_icon)
        self.share_button.clicked.connect(self.open_access)
        self.share_button.pack(side="left", padx=4)
        undo = ToolButton(bar, "undo", tooltip="Undo (Ctrl+Z). Only ever undoes your own changes.")
        undo.clicked.connect(self.session.undo)
        undo.pack(side="left")
        redo = ToolButton(bar, "redo", tooltip="Redo (Ctrl+Shift+Z)")
        redo.clicked.connect(self.session.redo)
        redo.pack(side="left")
        inspector = ToolButton(bar, "activity", tooltip="Packet Inspector (Ctrl+Shift+I)")
        inspector.clicked.connect(self.toggle_inspector)
        inspector.pack(side="right")
        self.zoom_button = Button(bar, "100%")
        self.zoom_button.setToolTip("Ctrl+scroll to zoom. Ctrl+0 for 100%, Shift+1 to fit.")
        self.zoom_button.clicked.connect(lambda: self.canvas.zoom_to(1.0))
        self.zoom_button.pack(side="right", padx=4)
        self.avatars = Frame(bar, "bar")
        self.avatars.pack(side="right", padx=4)
        return bar

    def _build_rail(self, master: tk.Misc) -> Frame:
        rail = Frame(master, "bar", padding=(4, 8), width=40)
        for name, icon_name, tip, _ in TOOLS:
            button = ToolButton(rail, icon_name, checkable=True, tooltip=tip)
            button.clicked.connect(lambda n=name: self._tool_clicked(n))
            button.pack(pady=1)
            self.tool_buttons[name] = button
        self.tool_buttons["select"].setChecked(True)
        self.image_button = ToolButton(rail, "image", tooltip="Insert image…")
        self.image_button.clicked.connect(self.insert_image)
        self.image_button.pack(pady=1)
        hand = ToolButton(rail, "hand", checkable=True, tooltip="Hand (hold Space)")
        hand.clicked.connect(lambda: self._tool_clicked("hand"))
        hand.pack(pady=1)
        self.tool_buttons["hand"] = hand
        tk.Frame(rail, height=8, background=theme.current().bg_panel).pack()
        self.stroke_swatch = ColorButton(rail, self.canvas_stroke_default(), allow_none=False)
        self.stroke_swatch.setToolTip("Stroke and pen color")
        self.stroke_swatch.color_chosen.connect(self._stroke_color)
        self.stroke_swatch.pack(pady=1)
        self.fill_swatch = ColorButton(rail, "#D9D9D9")
        self.fill_swatch.setToolTip("Fill color for new shapes")
        self.fill_swatch.color_chosen.connect(lambda c: setattr(self.canvas, "fill_color", c))
        self.fill_swatch.pack(pady=1)
        return rail

    @staticmethod
    def canvas_stroke_default() -> str:
        return "#1E1E1E"

    def _tool_clicked(self, name: str) -> None:
        self.canvas.set_tool(name)
        self._tool_changed(self.canvas.tool.name)  # a refused tool must not stay checked

    def _stroke_color(self, color: str | None) -> None:
        if color:
            self.canvas.stroke_color = color

    def _build_right(self, master: tk.Misc) -> ttk.Notebook:
        self.tabs = ttk.Notebook(master)
        self.design = DesignPanel(self.tabs, self.canvas, self.export_selection)
        self.comments = CommentsPanel(self.tabs, self.canvas)
        self.chat = ChatPanel(self.tabs, self.session)
        self.chat.unread_changed.connect(self._update_chat_badge)
        self.chat.new_private.connect(self.new_private_message)
        self.tabs.add(self.design, text="Design")
        self.tabs.add(self.comments, text="Comments")
        self.tabs.add(self.chat, text="Chat")
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())
        return self.tabs

    def _build_statusbar(self) -> Frame:
        t = theme.current()
        bar = Frame(self, "bar", padding=(8, 2))
        self.role_chip = tk.Label(bar, text="", foreground="#FFFFFF", padx=6, pady=1,
                                  font=theme.font(11, 600))
        self.role_chip.pack(side="left", padx=(0, 12))
        self.dot = Label(bar, "●", "panel")
        self.dot.pack(side="left")
        self.conn_label = Label(bar, "", "panel")
        self.conn_label.pack(side="left", padx=(4, 12))
        self.seq_label = Label(bar, "", "panelmuted")
        self.seq_label.pack(side="left", padx=(0, 12))
        self.objects_label = Label(bar, "", "panelmuted")
        self.objects_label.pack(side="left", padx=(0, 12))
        self.hash_label = tk.Entry(bar, relief="flat", borderwidth=0, width=26,
                                   readonlybackground=t.bg_panel, foreground=t.text_muted,
                                   font=theme.font(12), highlightthickness=0, state="readonly")
        self._hash_shown = False
        self.save_chip = Button(bar, "", "flat")
        self.save_chip.clicked.connect(self._save_chip_clicked)
        self.save_chip.pack(side="right")
        return bar

    def _bind_shortcuts(self) -> None:
        top = self.winfo_toplevel()

        def bind(keys: str, action: Callable[[], None]) -> None:
            for sequence in key_sequences(keys):
                top.bind(sequence, lambda e, k=keys, a=action: self._when_not_typing(k, a))
                self._bound.append(sequence)

        for name, _, _, key in TOOLS:
            bind(key, lambda n=name: self._tool_clicked(n))
        bind("Ctrl+Z", self.session.undo)
        bind("Ctrl+Shift+Z", self.session.redo)
        bind("Ctrl+S", self.save)
        bind("Ctrl+Shift+S", self.save_copy)
        bind("Ctrl+O", self.open_file_requested.emit)
        bind("Ctrl+Shift+E", self.export)
        bind("Ctrl+Shift+C", self.focus_chat)
        bind("Ctrl+Shift+M", self.new_private_message)
        bind("Ctrl+\\", self.toggle_ui)
        bind("Ctrl+Shift+I", self.toggle_inspector)
        bind("Ctrl+Shift+H", self.toggle_hash)
        bind("Ctrl+D", self.canvas.duplicate_selected)
        bind("Ctrl+G", self.canvas.group_selected)
        bind("Del", self.canvas.delete_selected)
        bind("Backspace", self.canvas.delete_selected)
        bind("Ctrl+]", lambda: self.canvas.change_z(True))
        bind("Ctrl+[", lambda: self.canvas.change_z(False))
        bind("Ctrl+0", lambda: self.canvas.zoom_to(1.0))
        bind("Shift+1", self.canvas.zoom_fit)
        for key, mode in ALIGN_KEYS:
            bind(key, lambda m=mode: self.canvas.align(m))

    def _when_not_typing(self, keys: str, action: Callable[[], None]) -> str | None:
        """Single-letter shortcuts must not fire while the person is typing into a field; nor
        do the editing keys a text field handles itself (undo, redo)."""
        if not self.winfo_ismapped():
            return None
        focus = self.focus_get()
        typing = focus is not None and focus.winfo_class() in TYPING_CLASSES
        if typing and (("Ctrl" not in keys and "Alt" not in keys) or keys.endswith("Z")):
            return None
        action()
        return "break"

    # ------------------------------------------------------------------ session events
    def on_event(self, name: str, **kw: Any) -> None:
        handler = getattr(self, f"_ev_{name}", None)
        if handler is not None:
            handler(**kw)

    def _ev_welcome(self, first: bool, changed: set[str]) -> None:
        self.canvas.sync(None)
        self.layers.refresh()
        self.design.refresh()
        self.comments.refresh()
        self.chat.refresh(scroll_bottom=True)
        self.apply_role()
        self._refresh_people()
        self._status_refresh()
        for request in self.session.pending_requests.values():
            self._toast_request(request)
        if first and self.session.doc.view.live():
            self.after_idle(self.canvas.zoom_fit)

    def _ev_board(self, changed: set[str]) -> None:
        self.canvas.sync(changed)
        self.layers.schedule_refresh()
        self.design.refresh()
        self.comments.refresh()
        self._status_refresh()

    def _ev_transient(self, msg: dict[str, Any]) -> None:
        self.canvas.on_transient(msg)

    def _ev_state(self, state: str) -> None:
        self.conn_state = state
        self.reconnect_banner.setVisible(
            state in ("reconnecting", "connecting") and self.session.welcomed
        )
        self._status_refresh()

    def _ev_presence(self, msg: dict[str, Any]) -> None:
        event = msg.get("event")
        name = msg.get("name", "")
        fp = msg.get("fp", "")
        if fp != self.session.fp:
            if event == "join":
                self.toast(f"{name} joined")
            elif event == "leave":
                self.toast(f"{name} left")
                self.canvas.forget_presence(fp)
            elif event == "away":
                self.toast(f"{name} is away")
        if event == "room":
            self.apply_role()
        if event == "member_added":
            # Possibly decided on the owner's other device, so this window must catch up too.
            self._update_share_badge()
            self._dismiss_request_toast(fp)
            if self.access_dialog is not None:
                self.access_dialog._fill_requests()
        self._refresh_people()
        self.chat.refresh()

    def _ev_role_changed(self, role: str) -> None:
        self.toast(f"You are now {'an' if role in ('owner', 'editor') else 'a'} {role}")
        self.apply_role()
        self._refresh_people()

    def _ev_chat(
        self, channel_id: str, message: dict[str, Any] | None = None, history: bool = False
    ) -> None:
        self.chat.refresh()
        if message is None or message["from_fp"] == self.session.fp:
            return
        chat_open = self.tabs.select() == str(self.chat) and self.isVisible()
        if message.get("to_fp") and (not chat_open or channel_id != self.session.chat.active):
            sender = message.get("from_name", "")
            preview = message["text"][:40]
            self.toast(
                f"Private · {sender}: {preview}",
                "Reply",
                lambda: self.open_private(message["from_fp"]),
            )

    def _ev_access_request(self, request: dict[str, Any]) -> None:
        self._toast_request(request)
        self._update_share_badge()
        if self.access_dialog is not None:
            self.access_dialog._fill_requests()

    def _ev_requests_changed(self) -> None:
        self._update_share_badge()
        for fingerprint in list(self._request_toasts):
            if fingerprint not in self.session.pending_requests:
                self._dismiss_request_toast(fingerprint)

    def _ev_op_rejected(self, code: str) -> None:
        self.toast(OP_REJECT_TEXT.get(code, f"That change was refused ({code})."))

    def _ev_undo_skipped(self, count: int, by: list[str]) -> None:
        who = f": edited by {', '.join(by)}" if by else ""
        self.toast(f"{count} change{'s' if count != 1 else ''} not undone{who}")

    def _ev_toast(self, text: str) -> None:
        self.toast(text)

    def _ev_save_state(self) -> None:
        self._status_refresh()

    def _ev_hash_mismatch(self) -> None:
        self.toast("Your view was out of date and has been refreshed")

    def _ev_recovered(self, ops: int, chat: int) -> None:
        parts = []
        if ops:
            parts.append(f"{ops} change{'s' if ops != 1 else ''}")
        if chat:
            parts.append(f"{chat} message{'s' if chat != 1 else ''}")
        if parts:
            self.toast(" and ".join(parts) + " recovered")

    def _ev_admin(self, msg: dict[str, Any]) -> None:
        action = msg.get("action")
        if action == "create_invite" and self.access_dialog is not None:
            self.access_dialog.show_invite(msg.get("token", ""))
        elif action == "list_checkpoints":
            self._show_history(msg.get("checkpoints", []))

    def _ev_error(self, code: str, msg: dict[str, Any]) -> None:
        if not msg.get("fatal") and code in ("NOT_ALLOWED", "NOT_FOUND", "BAD_SCHEMA"):
            self.toast("That action was refused by the server.")

    # ------------------------------------------------------------------ toast relay
    def toast(self, text: str, action: str | None = None, on_action: Any = None) -> None:
        window = self.winfo_toplevel()
        if hasattr(window, "show_toast"):
            window.show_toast(text, action, on_action)

    def _toast_request(self, request: dict[str, Any]) -> None:
        window = self.winfo_toplevel()
        if hasattr(window, "show_toast"):
            # Stays until decided: a request must not silently vanish after a few seconds.
            self._request_toasts[request["fingerprint"]] = window.show_toast(
                f"{request['name']} wants to join {self.session.room.get('name', '')}",
                "Review",
                lambda: self.review_request(request),
                duration=0,
            )
        self._update_share_badge()

    def _dismiss_request_toast(self, fingerprint: str) -> None:
        toast = self._request_toasts.pop(fingerprint, None)
        if toast is not None:
            try:
                toast.close_toast()
            except tk.TclError:
                pass  # the stack cap already removed this toast

    def review_request(self, request: dict[str, Any]) -> None:
        dialog = dialogs.AccessRequestDialog(request, self.session.room.get("name", ""), self)
        if dialog.exec() and dialog.decision is not None:
            decision, role = dialog.decision
            self.session.decide_access(request["fingerprint"], decision, role)

    # ------------------------------------------------------------------ roles and status
    def apply_role(self) -> None:
        role = self.session.role
        can_edit = self.canvas.can_edit()
        for name, _, tip, _ in TOOLS:
            if name == "select":
                continue
            button = self.tool_buttons[name]
            button.setEnabled(can_edit)
            if can_edit:
                button.setToolTip(tip)
            elif self.session.settings.get("locked") and role != "viewer":
                button.setToolTip("The owner has locked this board.")
            else:
                button.setToolTip("Viewers can't edit. Ask the owner.")
        self.image_button.setEnabled(can_edit)
        self.share_button.setVisible(role == "owner")
        self.menu_button.setText(self.session.room.get("name", "Room"))
        if not can_edit and self.canvas.tool.edits:
            self.canvas.set_tool("select")
        self.layers.schedule_refresh()
        self.design.refresh()
        self._status_refresh()

    def _status_refresh(self) -> None:
        t = theme.current()
        session = self.session
        role = session.role or ""
        color = theme.ROLE_COLORS.get(role, t.text_muted)
        self.role_chip.configure(text=ROLE_LABEL.get(role, ""), background=color)
        states = {
            "live": (t.success, "Connected"),
            "pending": (t.warn, "Waiting"),
            "connecting": (t.warn, "Connecting"),
            "reconnecting": (t.warn, "Reconnecting"),
            "closed": (t.danger, "Offline"),
        }
        dot_color, text = states.get(self.conn_state, (t.danger, "Offline"))
        if self.rtt is not None and self.conn_state == "live":
            text += f" · {self.rtt * 1000:.0f} ms"
        self.dot.configure(foreground=dot_color)
        self.dot.setToolTip(text)
        self.conn_label.setText(text)
        self.seq_label.setText(f"seq {session.doc.seq}")
        count = sum(1 for o in session.doc.view.live() if o["type"] != "comment")
        self.objects_label.setText(f"{count} objects")
        if self._hash_shown:
            self.hash_label.configure(state="normal")
            self.hash_label.delete(0, "end")
            self.hash_label.insert(0, session.doc.hash()[:23])
            self.hash_label.configure(state="readonly")
        self._refresh_save_chip()

    def _refresh_save_chip(self) -> None:
        t = theme.current()
        state = self.session.save_state
        info = self.session.save_info
        if state == SAVE_SAVING:
            text, color = "Saving…", t.text_muted
        elif state == SAVE_SAVED:
            when = time.strftime("%H:%M", time.localtime(info.get("time", time.time())))
            text, color = f"✓ Saved · {when}", t.success
        elif state == SAVE_LOCAL_ONLY:
            text, color = "Saved locally · server checkpoint pending", t.warn
        elif state == SAVE_FAILED:
            text, color = f"Save failed: {info.get('error', 'unknown')}", t.danger
        elif state == SAVE_EDITED:
            text, color = "Edited since last save", t.text_muted
        else:
            text, color = "", t.text_muted
        self.save_chip.setText(text)
        self.save_chip.setVisible(state != SAVE_IDLE)
        style = f"save{color[1:]}.flat.TButton"
        ttk.Style(self).configure(style, foreground=color)
        self.save_chip.configure(style=style)
        tips = []
        if "seq" in info:
            tips.append(f"Checkpoint at seq {info['seq']}")
        if "board_hash" in info:
            tips.append(f"Board hash {info['board_hash'][:23]}")
        if "local_path" in info:
            tips.append(f"Local copy: {info['local_path']}")
        self.save_chip.setToolTip("\n".join(tips))

    def _save_chip_clicked(self) -> None:
        if self.session.save_state == SAVE_FAILED:
            self.save()

    def _refresh_people(self) -> None:
        for child in self.avatars.winfo_children():
            child.destroy()
        online = [m for m in self.session.members.values() if m.get("online")]
        online.sort(key=lambda m: m["fp"] != self.session.fp)
        for member in online[:6]:
            avatar = Avatar(self.avatars, member.get("name", "?"), member["fp"],
                            member.get("away", False))
            avatar.clicked.connect(self.open_people)
            avatar.pack(side="left")
        if not online:
            people = ToolButton(self.avatars, "users")
            people.clicked.connect(self.open_people)
            people.pack(side="left")
        extra = len(online) - 6
        if extra > 0:
            more = ToolButton(self.avatars, text=f"+{extra}")
            more.clicked.connect(self.open_people)
            more.pack(side="left")

    def open_people(self) -> None:
        if self._popover is not None and self._popover.winfo_exists():
            self._popover.close()
        popover = PeoplePopover(self, self.session, self.open_private)
        popover.update_idletasks()
        x = self.topbar.winfo_rootx() + self.topbar.winfo_width() - popover.winfo_reqwidth() - 8
        y = self.topbar.winfo_rooty() + self.topbar.winfo_height() + 4
        popover.geometry(f"+{max(0, x)}+{y}")
        popover.focus_force()
        self._popover = popover  # kept alive for as long as it is on screen

    def _update_share_badge(self) -> None:
        count = len(self.session.pending_requests)
        self.share_button.setText(f"Share  •{count}" if count else "Share")

    def _update_chat_badge(self, count: int) -> None:
        self.tabs.tab(self.chat, text=f"Chat  •{count}" if count else "Chat")

    def chat_tab_text(self) -> str:
        return str(self.tabs.tab(self.chat, "text"))

    def _tab_changed(self) -> None:
        if self.tabs.select() == str(self.chat):
            self.chat.open_channel(self.session.chat.active)

    def _tool_changed(self, name: str) -> None:
        for tool, button in self.tool_buttons.items():
            button.setChecked(tool == name)

    def _second(self) -> None:
        self.session.tick()
        self._status_refresh()
        self._clock = after_ms(self, 1000, self._second)

    # ------------------------------------------------------------------ panel commands
    def toggle_ui(self) -> None:
        self._ui_shown = not self._ui_shown
        if self._ui_shown:
            self.split.insert(0, self.layers, weight=0)
            self.split.add(self.right, weight=0)
            self.rail.pack(side="left", fill="y", before=self.split)
            self.rail_border.pack(side="left", fill="y", before=self.split)
            self.after_idle(self._split_resized)
        else:
            self.split.forget(self.layers)
            self.split.forget(self.right)
            self.rail.pack_forget()
            self.rail_border.pack_forget()

    def toggle_inspector(self) -> None:
        self._inspector_shown = not self._inspector_shown
        if self._inspector_shown:
            self.middle.add(self.inspector, weight=0)
            self.update_idletasks()
            self.middle.sashpos(0, max(100, self.middle.winfo_height() - 300))
            self.inspector.start()
        else:
            self.middle.forget(self.inspector)
            self.inspector.stop()

    def toggle_hash(self) -> None:
        self._hash_shown = not self._hash_shown
        if self._hash_shown:
            self.hash_label.pack(side="left", after=self.objects_label)
        else:
            self.hash_label.pack_forget()
        self._status_refresh()

    def focus_chat(self) -> None:
        self.tabs.select(self.chat)
        self.chat.focus_input()

    def new_private_message(self) -> None:
        picker = dialogs.MemberPickerDialog(self.session, self)
        if picker.exec() and picker.chosen:
            self.open_private(picker.chosen)

    def open_private(self, fingerprint: str) -> None:
        self.tabs.select(self.chat)
        self.chat.open_private(fingerprint)

    def open_access(self) -> None:
        self.access_dialog = AccessDialog(self.session, self)
        self.access_dialog.exec()
        self.access_dialog = None
        self._update_share_badge()

    def _open_comment(self, obj_id: str) -> None:
        self.tabs.select(self.comments)
        self.comments.open_comment(obj_id)

    def _open_room_menu(self) -> None:
        others = [b for b in self.boards if b.room_id != self.session.room_id and b.compatible]
        owner = self.session.role == "owner"
        can_clear = owner or (
            self.session.role == "editor" and self.session.settings.get("editors_can_clear")
        )
        popup_menu(self.menu_button, [
            ("Save\tCtrl+S", self.save),
            ("Save a copy…\tCtrl+Shift+S", self.save_copy),
            ("Open .lanvas…\tCtrl+O", self.open_file_requested.emit),
            ("Export…\tCtrl+Shift+E", self.export),
            ("Version history", self.version_history, owner),
            ("Clear canvas…", self.clear_canvas, bool(can_clear)),
            ("Switch board", [(b.name, lambda b=b: self.switch_requested.emit(b, ""))
                              for b in others]),
            None,
            ("Leave room", self.leave_requested.emit),
        ])

    def clear_canvas(self) -> None:
        if ask_yes_no(self, "Clear canvas",
                      "Remove everything from the board for everyone? You can undo this."):
            self.session.clear()

    def version_history(self) -> None:
        self.session.send_admin("list_checkpoints")

    def _show_history(self, checkpoints: list[dict[str, Any]]) -> None:
        dialog = dialogs.VersionHistoryDialog(self.session, checkpoints, self)
        if dialog.exec() and dialog.restored:
            self.session.restore(dialog.restored)

    def insert_image(self) -> None:
        """Images travel inside ops, so they get downscaled to fit the per-object size limit."""
        if not self.canvas.can_edit():
            return
        path = filedialog.askopenfilename(
            parent=self, title="Insert image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.webp")],
        )
        if path:
            self.insert_image_file(Path(path))

    def insert_image_file(self, path: Path) -> None:
        try:
            with Image.open(path) as opened:
                image = opened.copy()
        except (OSError, ValueError):
            self.toast("That file could not be read as an image.")
            return
        data = encode_image(image)
        if data is None:
            self.toast("That image is too large to add to the board.")
            return
        center = self.canvas.view_center_scene()
        side = max(image.width, image.height, 1)
        scale = min(1.0, 600 / side)
        width, height = image.width * scale, image.height * scale
        op = self.session.create(
            "image",
            {
                "x": center.x - width / 2,
                "y": center.y - height / 2,
                "w": width,
                "h": height,
                "image_data": data,
                "name": path.name[:200],
            },
        )
        if op:
            self.canvas.set_selection({op["id"]})

    # ------------------------------------------------------------------ save and export
    def save(self) -> None:
        self.session.save()

    def _default_name(self, suffix: str) -> str:
        safe = "".join(
            c for c in self.session.room.get("name", "board") if c.isalnum() or c in " -_"
        )
        return f"{safe.strip() or 'board'}{suffix}"

    def save_copy(self) -> None:
        if not self.session.can_export():
            self.toast("Viewers can't save copies in this room.")
            return
        options = dialogs.SaveCopyDialog(self)
        if not options.exec():
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Save a copy", initialfile=self._default_name(".lanvas"),
            defaultextension=".lanvas", filetypes=[("LANvas board", "*.lanvas")],
        )
        if path:
            self._write_lanvas(path, options.include_chat.isChecked(), options.password())

    def _write_lanvas(self, path: str, include_chat: bool, password: str | None) -> None:
        document = self.session.export_document(include_chat)
        target = Path(path)

        def job() -> None:
            try:
                durable.atomic_write(target, export_lanvas(document, password))
                call_in_ui(lambda: self.exporter.finished.emit(str(target), ""))
            except OSError as exc:
                error = str(exc)
                call_in_ui(lambda: self.exporter.finished.emit(str(target), error))

        self.io.submit(job)

    def export_selection(self) -> None:
        self.export(default_selection=True)

    def export(self, default_selection: bool = False) -> None:
        if not self.session.can_export():
            self.toast("Viewers can't export in this room.")
            return
        scene = self.canvas.board_scene
        frames = [
            (obj_id, item.obj["props"].get("name") or "Frame")
            for obj_id, item in scene.items_by_id.items()
            if item.obj["type"] == "frame"
        ]

        def estimate(dialog: dialogs.ExportDialog) -> str:
            fmt = dialog.format_name()
            if fmt not in ("PNG", "JPEG"):
                return "Vector output; size depends on content." if fmt != ".lanvas" else ""
            width, height = raster_size(scene, self._scope_from(dialog), dialog.scale.currentData())
            megabytes = width * height * ESTIMATE_BYTES_PER_PIXEL / 1e6
            return f"Estimated size: {width} × {height} px · ~{megabytes:.1f} MB"

        dialog = dialogs.ExportDialog(frames, bool(self.canvas.selection), estimate, parent=self)
        if default_selection and self.canvas.selection:
            dialog.scope_selection.setChecked(True)
        if dialog.exec():
            self._run_export(dialog)

    def _scope_from(self, dialog: dialogs.ExportDialog) -> Scope:
        if dialog.scope_selection.isChecked():
            return Scope(SCOPE_SELECTION, selection=frozenset(self.canvas.selection))
        if dialog.scope_frame.isChecked():
            return Scope(SCOPE_FRAME, frame_id=dialog.frame_combo.currentData())
        return Scope(SCOPE_BOARD)

    def _run_export(self, dialog: dialogs.ExportDialog) -> None:
        fmt = dialog.format_name()
        filters = {
            "PNG": ("PNG image", ".png"),
            "JPEG": ("JPEG image", ".jpg"),
            "SVG": ("SVG image", ".svg"),
            "PDF": ("PDF document", ".pdf"),
            ".lanvas": ("LANvas board", ".lanvas"),
        }
        label, suffix = filters[fmt]
        # Read every option now: the dialog window is gone once the file chooser returns.
        include_chat = dialog.include_chat.isChecked()
        password = dialog.fields.password() if dialog.protect.isChecked() else None
        scope = self._scope_from(dialog)
        scale = dialog.scale.currentData()
        transparent = dialog.transparent.isChecked()
        page = dialog.page.currentData()
        each_frame = dialog.each_frame.isChecked()
        path = filedialog.asksaveasfilename(
            parent=self, title="Export", initialfile=self._default_name(suffix),
            defaultextension=suffix, filetypes=[(label, f"*{suffix}")],
        )
        if not path:
            return
        if fmt == ".lanvas":
            self._write_lanvas(path, include_chat, password)
            return
        scene = self.canvas.board_scene
        title = self.session.room.get("name", "LANvas")
        if fmt in ("PNG", "JPEG"):
            steps = export_raster(
                scene, scope, fmt, scale, transparent,
                "#FFFFFF" if transparent else theme.current().bg_canvas,
            )
        elif fmt == "SVG":
            steps = export_svg(scene, scope, title)
        else:
            steps = export_pdf(scene, scope, page, each_frame, title)
        self.exporter.start(steps, Path(path), len(scene.items_by_id))

    def _export_finished(self, path: str, error: str) -> None:
        name = Path(path).name
        if error:
            self.toast(f"Could not export {name}: {error}")
        else:
            self.toast(f"Exported {name}", "Show in folder", lambda: _show_in_folder(path))

    def shutdown(self) -> None:
        top = self.winfo_toplevel()
        for sequence in self._bound:
            top.unbind(sequence)
        self._bound = []
        self.inspector.stop()
        self.io.flush()
        self.io.stop()

    def on_rtt(self, rtt: float) -> None:
        self.rtt = rtt

    def fingerprint_short(self) -> str:
        return short_fingerprint(self.session.fp)


def encode_image(image: Image.Image) -> str | None:
    """PNG keeps transparency; JPEG is the fallback for photos that would otherwise be too big."""
    if max(image.width, image.height) > MAX_IMAGE_SIDE:
        image = image.copy()
        image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
    for fmt, quality in (("PNG", None), ("JPEG", 85), ("JPEG", 60)):
        buffer = io.BytesIO()
        if fmt == "JPEG":
            image.convert("RGB").save(buffer, "JPEG", quality=quality)
        else:
            image.save(buffer, "PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode()
        if len(encoded) <= MAX_IMAGE_B64:
            return encoded
    return None


def _show_in_folder(path: str) -> None:
    folder = str(Path(path).parent)
    if sys.platform == "win32":
        os.startfile(folder)  # type: ignore[attr-defined]  # noqa: S606
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, folder])  # noqa: S603
        except OSError:
            log.warning("could not open %s", folder)
