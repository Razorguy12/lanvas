"""The board window: top bar, tool rail, panels, status bar, and the commands connecting them."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QBuffer, QIODevice, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QImage, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

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
from client.widgets import Avatar, ColorButton
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


class Exporter(QObject):
    """Runs a chunked render on the UI thread between events, then writes the file off-thread."""

    finished = pyqtSignal(str, str)  # path, error

    def __init__(self, parent: QWidget, io: IoWorker) -> None:
        super().__init__(parent)
        self.parent_widget = parent
        self.io = io
        self.timer = QTimer(self)
        self.timer.setInterval(EXPORT_TICK_MS)
        self.timer.timeout.connect(self._step)
        self.steps: Any = None
        self.path = Path()
        self.total = 0
        self.progress: QProgressDialog | None = None

    def start(self, steps: Any, path: Path, total: int) -> None:
        self.steps = steps
        self.path = path
        self.total = max(total, 1)
        if total >= 200:
            self.progress = QProgressDialog("Exporting…", "", 0, self.total, self.parent_widget)
            self.progress.setCancelButton(None)
            self.progress.setWindowModality(Qt.WindowModality.NonModal)
            self.progress.show()
        self.timer.start()

    def _step(self) -> None:
        try:
            done = next(self.steps)
            if self.progress is not None:
                self.progress.setValue(min(done, self.total))
        except StopIteration as stop:
            self.timer.stop()
            self._close_progress()
            self.write(stop.value, self.path)
        except ExportError as exc:
            self.timer.stop()
            self._close_progress()
            self.finished.emit(str(self.path), str(exc))

    def _close_progress(self) -> None:
        if self.progress is not None:
            self.progress.close()
            self.progress = None

    def write(self, data: bytes, path: Path) -> None:
        def job() -> None:
            try:
                durable.atomic_write(path, data)
                self.finished.emit(str(path), "")
            except OSError as exc:
                self.finished.emit(str(path), str(exc))

        self.io.submit(job)


class BoardPage(QWidget):
    leave_requested = pyqtSignal()
    switch_requested = pyqtSignal(object, str)
    open_file_requested = pyqtSignal()

    def __init__(self, session: Any, get_core: Callable[[], Any], discovery: Any) -> None:
        super().__init__()
        self.session = session
        self.get_core = get_core
        self.discovery = discovery
        self.boards: list[BoardEntry] = []
        self.access_dialog: AccessDialog | None = None
        self._request_toasts: dict[str, Any] = {}
        self.setObjectName("page")
        self.io = IoWorker()
        self.exporter = Exporter(self, self.io)
        self.exporter.finished.connect(self._export_finished, Qt.ConnectionType.QueuedConnection)
        self.canvas = CanvasView(session, self)
        self.canvas.comment_requested.connect(self._open_comment)
        self.tool_buttons: dict[str, QToolButton] = {}
        self._build()
        self._bind_shortcuts()
        self.canvas.tool_changed.connect(self._tool_changed)
        self.canvas.zoom_changed.connect(lambda f: self.zoom_button.setText(f"{f * 100:.0f}%"))
        self.canvas.selection_changed.connect(self._status_refresh)
        self.clock = QTimer(self)
        self.clock.setInterval(1000)
        self.clock.timeout.connect(self._second)
        self.clock.start()
        self.rtt: float | None = None
        self.conn_state = "connecting"
        self.apply_role()

    # ------------------------------------------------------------------ construction
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.topbar = self._build_topbar()
        root.addWidget(self.topbar)
        self.reconnect_banner = QLabel("Reconnecting… your changes are saved locally")
        self.reconnect_banner.setObjectName("banner")
        self.reconnect_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reconnect_banner.setVisible(False)
        root.addWidget(self.reconnect_banner)
        self.middle = QSplitter(Qt.Orientation.Vertical)
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.rail = self._build_rail()
        self.layers = LayersPanel(self.canvas)
        self.layers.setObjectName("leftpanel")
        self.layers.setMinimumWidth(200)
        self.right = self._build_right()
        self.right.setMinimumWidth(200)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.rail)
        self.split.addWidget(self.layers)
        self.split.addWidget(self.canvas)
        self.split.addWidget(self.right)
        self.split.setSizes([240, 900, 280])
        self.split.setStretchFactor(1, 1)
        self.split.setCollapsible(1, False)
        body.addWidget(self.split, 1)
        holder = QWidget()
        holder.setLayout(body)
        self.middle.addWidget(holder)
        self.inspector = InspectorPanel(self.get_core, self.discovery)
        self.inspector.setVisible(False)
        self.middle.addWidget(self.inspector)
        self.middle.setStretchFactor(0, 1)
        root.addWidget(self.middle, 1)
        self.statusbar = self._build_statusbar()
        root.addWidget(self.statusbar)

    def _build_topbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("topbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 4, 8, 4)
        self.menu_button = QPushButton("Room")
        self.menu_button.setObjectName("title")
        self.menu_button.setIcon(icons.icon("menu", theme.current().text_primary))
        self.menu_button.clicked.connect(self._open_room_menu)
        row.addWidget(self.menu_button)
        self.share_button = QPushButton("Share")
        self.share_button.setIcon(icons.icon("share", theme.current().text_primary))
        self.share_button.clicked.connect(self.open_access)
        row.addWidget(self.share_button)
        undo = QToolButton()
        undo.setIcon(icons.icon("undo", theme.current().text_primary))
        undo.setToolTip("Undo (Ctrl+Z). Only ever undoes your own changes.")
        undo.clicked.connect(self.session.undo)
        row.addWidget(undo)
        redo = QToolButton()
        redo.setIcon(icons.icon("redo", theme.current().text_primary))
        redo.setToolTip("Redo (Ctrl+Shift+Z)")
        redo.clicked.connect(self.session.redo)
        row.addWidget(redo)
        row.addStretch(1)
        self.avatars = QHBoxLayout()
        self.avatars.setSpacing(-4)
        row.addLayout(self.avatars)
        self.zoom_button = QPushButton("100%")
        self.zoom_button.setToolTip("Ctrl+scroll to zoom. Ctrl+0 for 100%, Shift+1 to fit.")
        self.zoom_button.clicked.connect(lambda: self.canvas.zoom_to(1.0))
        row.addWidget(self.zoom_button)
        inspector = QToolButton()
        inspector.setIcon(icons.icon("activity", theme.current().text_primary))
        inspector.setToolTip("Packet Inspector (Ctrl+Shift+I)")
        inspector.clicked.connect(self.toggle_inspector)
        row.addWidget(inspector)
        return bar

    def _build_rail(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("toolrail")
        rail.setFixedWidth(40)
        column = QVBoxLayout(rail)
        column.setContentsMargins(4, 8, 4, 8)
        column.setSpacing(2)
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        color = theme.current().text_primary
        for name, icon_name, tip, _ in TOOLS:
            button = QToolButton()
            button.setIcon(icons.icon(icon_name, color))
            button.setToolTip(tip)
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, n=name: self.canvas.set_tool(n))
            self.tool_group.addButton(button)
            self.tool_buttons[name] = button
            column.addWidget(button)
        self.tool_buttons["select"].setChecked(True)
        self.image_button = QToolButton()
        self.image_button.setIcon(icons.icon("image", color))
        self.image_button.setToolTip("Insert image…")
        self.image_button.clicked.connect(self.insert_image)
        column.addWidget(self.image_button)
        hand = QToolButton()
        hand.setIcon(icons.icon("hand", color))
        hand.setCheckable(True)
        hand.setToolTip("Hand (hold Space)")
        hand.clicked.connect(lambda: self.canvas.set_tool("hand"))
        self.tool_group.addButton(hand)
        self.tool_buttons["hand"] = hand
        column.addWidget(hand)
        column.addSpacing(8)
        self.stroke_swatch = ColorButton(self.canvas.stroke_color, allow_none=False)
        self.stroke_swatch.setToolTip("Stroke and pen color")
        self.stroke_swatch.color_chosen.connect(self._stroke_color)
        column.addWidget(self.stroke_swatch)
        self.fill_swatch = ColorButton(self.canvas.fill_color)
        self.fill_swatch.setToolTip("Fill color for new shapes")
        self.fill_swatch.color_chosen.connect(lambda c: setattr(self.canvas, "fill_color", c))
        column.addWidget(self.fill_swatch)
        column.addStretch(1)
        return rail

    def _stroke_color(self, color: str | None) -> None:
        if color:
            self.canvas.stroke_color = color

    def _build_right(self) -> QWidget:
        self.tabs = QTabWidget()
        self.tabs.setObjectName("rightpanel")
        self.design = DesignPanel(self.canvas, self.export_selection)
        self.comments = CommentsPanel(self.canvas)
        self.chat = ChatPanel(self.session)
        self.chat.unread_changed.connect(self._update_chat_badge)
        self.chat.new_private.connect(self.new_private_message)
        self.tabs.addTab(self.design, "Design")
        self.tabs.addTab(self.comments, "Comments")
        self.tabs.addTab(self.chat, "Chat")
        self.tabs.currentChanged.connect(self._tab_changed)
        return self.tabs

    def _build_statusbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("statusbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 2, 8, 2)
        row.setSpacing(12)
        self.role_chip = QLabel()
        row.addWidget(self.role_chip)
        self.dot = QLabel("●")
        row.addWidget(self.dot)
        self.conn_label = QLabel()
        row.addWidget(self.conn_label)
        self.seq_label = QLabel()
        self.seq_label.setObjectName("muted")
        row.addWidget(self.seq_label)
        self.objects_label = QLabel()
        self.objects_label.setObjectName("muted")
        row.addWidget(self.objects_label)
        self.hash_label = QLabel()
        self.hash_label.setObjectName("muted")
        self.hash_label.setVisible(False)
        self.hash_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.hash_label)
        row.addStretch(1)
        self.save_chip = QPushButton()
        self.save_chip.setFlat(True)
        self.save_chip.clicked.connect(self._save_chip_clicked)
        row.addWidget(self.save_chip)
        return bar

    def _bind_shortcuts(self) -> None:
        def bind(keys: str, action: Callable[[], None]) -> None:
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(lambda: self._when_not_typing(keys, action))

        for name, _, _, key in TOOLS:
            bind(key, lambda n=name: self.canvas.set_tool(n))
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

    def _when_not_typing(self, keys: str, action: Callable[[], None]) -> None:
        """Single-letter shortcuts must not fire while the person is typing into a field."""
        focus = QGuiApplication.focusObject()
        typing = focus is not None and focus.metaObject().className() in (
            "QLineEdit",
            "QPlainTextEdit",
            "QTextEdit",
            "QDoubleSpinBox",
            "QSpinBox",
        )
        if typing and "Ctrl" not in keys and "Alt" not in keys:
            return
        action()

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
            QTimer.singleShot(0, self.canvas.zoom_fit)

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
        chat_open = self.tabs.currentWidget() is self.chat and self.isVisible()
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
        window = self.window()
        if hasattr(window, "show_toast"):
            window.show_toast(text, action, on_action)

    def _toast_request(self, request: dict[str, Any]) -> None:
        window = self.window()
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
            except RuntimeError:
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
        name = self.session.room.get("name", "Room")
        self.menu_button.setText(name)
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
        self.role_chip.setText(ROLE_LABEL.get(role, ""))
        self.role_chip.setStyleSheet(
            f"color:#FFFFFF;background:{color};border-radius:4px;padding:1px 6px;"
            "font-size:11px;font-weight:600;"
        )
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
        self.dot.setStyleSheet(f"color:{dot_color};")
        self.dot.setToolTip(text)
        self.conn_label.setText(text)
        self.seq_label.setText(f"seq {session.doc.seq}")
        count = sum(1 for o in session.doc.view.live() if o["type"] != "comment")
        self.objects_label.setText(f"{count} objects")
        if self.hash_label.isVisible():
            self.hash_label.setText(session.doc.hash()[:23])
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
        self.save_chip.setStyleSheet(f"color:{color};border:none;background:transparent;")
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
        while self.avatars.count():
            widget = self.avatars.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        online = [m for m in self.session.members.values() if m.get("online")]
        online.sort(key=lambda m: m["fp"] != self.session.fp)
        for member in online[:6]:
            avatar = Avatar(member.get("name", "?"), member["fp"], member.get("away", False))
            avatar.clicked.connect(self.open_people)
            self.avatars.addWidget(avatar)
        if not online:
            people = QToolButton()
            people.setIcon(icons.icon("users", theme.current().text_primary))
            people.clicked.connect(self.open_people)
            self.avatars.addWidget(people)
        extra = len(online) - 6
        if extra > 0:
            more = QToolButton()
            more.setText(f"+{extra}")
            more.clicked.connect(self.open_people)
            self.avatars.addWidget(more)

    def open_people(self) -> None:
        popover = PeoplePopover(self.session, self.open_private)
        popover.adjustSize()
        anchor = self.topbar.mapToGlobal(self.topbar.rect().bottomRight())
        popover.move(anchor.x() - popover.width() - 8, anchor.y() + 4)
        popover.show()
        self._popover = popover  # kept alive for as long as it is on screen

    def _update_share_badge(self) -> None:
        count = len(self.session.pending_requests)
        self.share_button.setText(f"Share  •{count}" if count else "Share")

    def _update_chat_badge(self, count: int) -> None:
        index = self.tabs.indexOf(self.chat)
        self.tabs.setTabText(index, f"Chat  •{count}" if count else "Chat")

    def _tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.chat:
            self.chat.open_channel(self.session.chat.active)

    def _tool_changed(self, name: str) -> None:
        button = self.tool_buttons.get(name)
        if button is not None:
            button.setChecked(True)

    def _second(self) -> None:
        self.session.tick()
        self._status_refresh()

    # ------------------------------------------------------------------ panel commands
    def toggle_ui(self) -> None:
        visible = not self.layers.isVisible()
        for widget in (self.layers, self.right, self.rail):
            widget.setVisible(visible)

    def toggle_inspector(self) -> None:
        self.inspector.setVisible(not self.inspector.isVisible())
        if self.inspector.isVisible():
            self.middle.setSizes([self.height() - 300, 300])

    def toggle_hash(self) -> None:
        self.hash_label.setVisible(not self.hash_label.isVisible())
        self._status_refresh()

    def focus_chat(self) -> None:
        self.tabs.setCurrentWidget(self.chat)
        self.chat.focus_input()

    def new_private_message(self) -> None:
        picker = dialogs.MemberPickerDialog(self.session, self)
        if picker.exec() and picker.chosen:
            self.open_private(picker.chosen)

    def open_private(self, fingerprint: str) -> None:
        self.tabs.setCurrentWidget(self.chat)
        self.chat.open_private(fingerprint)

    def open_access(self) -> None:
        self.access_dialog = AccessDialog(self.session, self)
        self.access_dialog.exec()
        self.access_dialog = None
        self._update_share_badge()

    def _open_comment(self, obj_id: str) -> None:
        self.tabs.setCurrentWidget(self.comments)
        self.comments.open_comment(obj_id)

    def _open_room_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("Save\tCtrl+S", self.save)
        menu.addAction("Save a copy…\tCtrl+Shift+S", self.save_copy)
        menu.addAction("Open .lanvas…\tCtrl+O", self.open_file_requested.emit)
        menu.addAction("Export…\tCtrl+Shift+E", self.export)
        history = menu.addAction("Version history", self.version_history)
        if self.session.role != "owner":
            history.setEnabled(False)
            history.setToolTip("Only the owner can browse versions.")
        clear = menu.addAction("Clear canvas…", self.clear_canvas)
        clear.setEnabled(
            self.session.role == "owner"
            or (self.session.role == "editor" and self.session.settings.get("editors_can_clear"))
        )
        switch = menu.addMenu("Switch board")
        others = [b for b in self.boards if b.room_id != self.session.room_id and b.compatible]
        if not others:
            switch.setEnabled(False)
        for board in others:
            switch.addAction(board.name, lambda b=board: self.switch_requested.emit(b, ""))
        menu.addSeparator()
        menu.addAction("Leave room", self.leave_requested.emit)
        menu.exec(self.menu_button.mapToGlobal(self.menu_button.rect().bottomLeft()))

    def clear_canvas(self) -> None:
        answer = QMessageBox.question(
            self,
            "Clear canvas",
            "Remove everything from the board for everyone? You can undo this.",
        )
        if answer == QMessageBox.StandardButton.Yes:
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
        path, _ = QFileDialog.getOpenFileName(
            self, "Insert image", "", "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp)"
        )
        if not path:
            return
        image = QImage(path)
        if image.isNull():
            self.toast("That file could not be read as an image.")
            return
        data = encode_image(image)
        if data is None:
            self.toast("That image is too large to add to the board.")
            return
        center = self.canvas.mapToScene(self.canvas.viewport().rect().center())
        side = max(image.width(), image.height(), 1)
        scale = min(1.0, 600 / side)
        width, height = image.width() * scale, image.height() * scale
        op = self.session.create(
            "image",
            {
                "x": center.x() - width / 2,
                "y": center.y() - height / 2,
                "w": width,
                "h": height,
                "image_data": data,
                "name": Path(path).name[:200],
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
        path, _ = QFileDialog.getSaveFileName(
            self, "Save a copy", self._default_name(".lanvas"), "LANvas board (*.lanvas)"
        )
        if path:
            self._write_lanvas(path, options.include_chat.isChecked(), options.password())

    def _write_lanvas(self, path: str, include_chat: bool, password: str | None) -> None:
        document = self.session.export_document(include_chat)
        target = Path(path)

        def job() -> None:
            try:
                durable.atomic_write(target, export_lanvas(document, password))
                self.exporter.finished.emit(str(target), "")
            except OSError as exc:
                self.exporter.finished.emit(str(target), str(exc))

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
            "PNG": ("PNG image (*.png)", ".png"),
            "JPEG": ("JPEG image (*.jpg)", ".jpg"),
            "SVG": ("SVG image (*.svg)", ".svg"),
            "PDF": ("PDF document (*.pdf)", ".pdf"),
            ".lanvas": ("LANvas board (*.lanvas)", ".lanvas"),
        }
        file_filter, suffix = filters[fmt]
        path, _ = QFileDialog.getSaveFileName(
            self, "Export", self._default_name(suffix), file_filter
        )
        if not path:
            return
        if fmt == ".lanvas":
            password = dialog.fields.password() if dialog.protect.isChecked() else None
            self._write_lanvas(path, dialog.include_chat.isChecked(), password)
            return
        scene = self.canvas.board_scene
        scope = self._scope_from(dialog)
        title = self.session.room.get("name", "LANvas")
        if fmt in ("PNG", "JPEG"):
            steps = export_raster(
                scene,
                scope,
                fmt,
                dialog.scale.currentData(),
                dialog.transparent.isChecked(),
                "#FFFFFF" if dialog.transparent.isChecked() else theme.current().bg_canvas,
            )
        elif fmt == "SVG":
            steps = export_svg(scene, scope, title)
        else:
            steps = export_pdf(
                scene, scope, dialog.page.currentData(), dialog.each_frame.isChecked(), title
            )
        self.exporter.start(steps, Path(path), len(scene.items_by_id))

    def _export_finished(self, path: str, error: str) -> None:
        name = Path(path).name
        if error:
            self.toast(f"Could not export {name}: {error}")
        else:
            self.toast(f"Exported {name}", "Show in folder", lambda: _show_in_folder(path))

    def shutdown(self) -> None:
        self.clock.stop()
        self.inspector.timer.stop()
        self.io.flush()
        self.io.stop()

    def on_rtt(self, rtt: float) -> None:
        self.rtt = rtt

    def fingerprint_short(self) -> str:
        return short_fingerprint(self.session.fp)


def encode_image(image: QImage) -> str | None:
    """PNG keeps transparency; JPEG is the fallback for photos that would otherwise be too big."""
    import base64

    if max(image.width(), image.height()) > MAX_IMAGE_SIDE:
        image = image.scaled(
            MAX_IMAGE_SIDE,
            MAX_IMAGE_SIDE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    for fmt, quality in (("PNG", -1), ("JPEG", 85), ("JPEG", 60)):
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, fmt, quality)
        encoded = base64.b64encode(bytes(buffer.data())).decode()
        if len(encoded) <= MAX_IMAGE_B64:
            return encoded
    return None


def _show_in_folder(path: str) -> None:
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
