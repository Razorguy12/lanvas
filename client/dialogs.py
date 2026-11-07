"""Modal dialogs from Design.md section 8. They only collect choices; callers do the work."""

from __future__ import annotations

import secrets
import tkinter as tk
from typing import Any

from client import icons, theme
from client.canvas.paint import render_scene
from client.canvas.scene import BoardScene
from client.geom import Affine
from client.widgets import (
    Button,
    CheckBox,
    ComboBox,
    Dialog,
    Frame,
    Label,
    LineEdit,
    ListWidget,
    RadioGroup,
    SpinBox,
    ask_text,
    ask_yes_no,
    warn,
)
from common.fileformat import LanvasFile, Trust
from common.identity import short_fingerprint
from common.model import Board
from common.protocol import TCP_PORT

MIN_PASSWORD_HINT = 10


def _form(dialog: Dialog, padding: int = 16) -> Frame:
    body = Frame(dialog, padding=padding)
    body.pack(fill="both", expand=True)
    body.columnconfigure(1, weight=1)
    return body


def _row(form: Frame, label: str, widget: Any, row: int) -> None:
    Label(form, label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
    widget.grid(row=row, column=1, sticky="ew", pady=3)


def _buttons(form: Frame, row: int, *buttons: tuple[str, Any, str]) -> list[Button]:
    """Right-aligned buttons, in the order given (Cancel first, primary last)."""
    holder = Frame(form)
    holder.grid(row=row, column=0, columnspan=4, sticky="e", pady=(12, 0))
    made = []
    for text, action, style in buttons:
        button = Button(holder, text, style)
        button.clicked.connect(action)
        button.pack(side="left", padx=(6, 0))
        made.append(button)
    return made


class JoinByAddressDialog(Dialog):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent, "Join by address")
        form = _form(self)
        self.host = LineEdit(form, placeholder="192.168.1.10", width=24)
        self.port = SpinBox(form, 1, 65535, TCP_PORT)
        self.code = LineEdit(form)
        self.invite = LineEdit(form, placeholder="Optional")
        for i, (label, widget) in enumerate((("LAN address", self.host), ("Port", self.port),
                                             ("Room code", self.code),
                                             ("Invite code", self.invite))):
            _row(form, label, widget, i)
        _buttons(form, 4, ("Cancel", self.reject, ""), ("Join", self.accept, "primary"))
        for field in (self.host, self.code, self.invite):
            field.returnPressed.connect(self.accept)
        self.after(10, self.host.setFocus)


class CreateRoomDialog(Dialog):
    def __init__(self, servers: list[Any], parent: Any = None, name: str = "") -> None:
        super().__init__(parent, "Create room")
        self.minsize(380, 0)
        form = _form(self)
        self.name = LineEdit(form, name, placeholder="Sprint-1", width=28)
        self.code = LineEdit(form, secrets.token_hex(3).upper())
        self.server = ComboBox(form, width=26)
        for server in servers:
            self.server.addItem(f"{server.server_name} ({server.address})",
                                (server.address, server.tcp_port))
        self.server.addItem("Another address…", None)
        self.host = LineEdit(form, "127.0.0.1")
        self.port = SpinBox(form, 1, 65535, TCP_PORT)
        self.discoverable = CheckBox(form, "Discoverable on this network", checked=True)
        self.require_code = CheckBox(form, "Require room code to request access")
        self.server.currentIndexChanged.connect(lambda *_: self._server_changed())
        for i, (label, widget) in enumerate((("Room name", self.name), ("Room code", self.code),
                                             ("Server", self.server), ("Address", self.host),
                                             ("Port", self.port))):
            _row(form, label, widget, i)
        self.discoverable.grid(row=5, column=0, columnspan=2, sticky="w", pady=3)
        self.require_code.grid(row=6, column=0, columnspan=2, sticky="w", pady=3)
        _buttons(form, 7, ("Cancel", self.reject, ""),
                 ("Create room", self._try_accept, "primary"))
        self.name.returnPressed.connect(self._try_accept)
        self._server_changed()
        self.after(10, self.name.setFocus)

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


class AccessRequestDialog(Dialog):
    """The owner's decision. The fingerprint is there for the two people to compare out loud."""

    def __init__(self, request: dict[str, Any], room_name: str, parent: Any = None) -> None:
        super().__init__(parent, "Access request")
        self.decision: tuple[str, str | None] | None = None
        body = _form(self)
        Label(body, f"{request['name']} wants to join {room_name}", "pagetitle").grid(
            row=0, column=0, columnspan=4, sticky="w")
        Label(body, f"Address: {request.get('ip', '')}\n"
                    f"Fingerprint: {short_fingerprint(request['fingerprint'])}",
              justify="left").grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        Label(body, "Ask them to read out their fingerprint. It should match this one.",
              "muted", wraplength=460, justify="left").grid(row=2, column=0, columnspan=4,
                                                            sticky="w", pady=(6, 0))
        _buttons(body, 3, *(
            (label, lambda d=decision, r=role: self._choose(d, r), style)
            for label, decision, role, style in (
                ("Approve as Editor", "approve", "editor", "primary"),
                ("Approve as Viewer", "approve", "viewer", ""),
                ("Deny", "deny", None, "danger"),
                ("Block this device", "block", None, "danger"),
            )
        ))

    def _choose(self, decision: str, role: str | None) -> None:
        self.decision = (decision, role)
        self.accept()


class RecoverDialog(Dialog):
    RECOVER, SAVE, DISCARD = 1, 2, 3

    def __init__(self, crashed: Any, parent: Any = None) -> None:
        super().__init__(parent, "Recover")
        self.choice = 0
        body = _form(self)
        changes = len(crashed.ops)
        messages = len(crashed.chat)
        name = crashed.room_name or crashed.room_id
        self.text = Label(
            body,
            f"LANvas didn't close properly last time. {name} has {changes} unsaved "
            f"change{'s' if changes != 1 else ''} and {messages} unsent "
            f"message{'s' if messages != 1 else ''}.",
            wraplength=520, justify="left",
        )
        self.text.grid(row=0, column=0, columnspan=4, sticky="w")
        row = Frame(body)
        row.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        discard = Button(row, "Discard")
        discard.clicked.connect(lambda: self._done(self.DISCARD))
        discard.pack(side="left")
        rejoin = Button(row, "Rejoin and send my changes", "primary")
        rejoin.clicked.connect(lambda: self._done(self.RECOVER))
        rejoin.pack(side="right")
        save = Button(row, "Save recovered board as .lanvas…")
        save.clicked.connect(lambda: self._done(self.SAVE))
        save.pack(side="right", padx=6)
        self.bind("<Return>", lambda e: self._done(self.RECOVER))

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


class OpenFileDialog(Dialog):
    """A read-only preview with its trust verdict on top, and the way to make a room from it."""

    VIEW, CREATE_ROOM = 1, 2
    PREVIEW = (720, 420)

    def __init__(self, loaded: LanvasFile, parent: Any = None) -> None:
        super().__init__(parent, f"{loaded.room_name}.lanvas")
        self.choice = 0
        body = _form(self)
        message, color = trust_text(loaded)
        banner = Label(body, message)
        banner.set_color(color, bold=True)
        banner.grid(row=0, column=0, sticky="w")
        Label(body, f"{loaded.room_name} · saved {loaded.saved_at} · version {loaded.seq}",
              "muted").grid(row=1, column=0, sticky="w", pady=(2, 8))
        self.scene = BoardScene(self)
        self.scene.sync(Board.from_json(loaded.board))
        self.view = tk.Canvas(body, width=self.PREVIEW[0], height=self.PREVIEW[1],
                              highlightthickness=0, background=theme.current().bg_canvas)
        self.view.grid(row=2, column=0, sticky="nsew")
        body.rowconfigure(2, weight=1)
        self._photo = None
        self._paint_preview()
        row = 3
        if loaded.trust is not Trust.SIGNED:
            Label(body, "Opened read-only. Only make a room from this if you trust where it "
                        "came from.", "muted").grid(row=3, column=0, sticky="w", pady=(8, 0))
            row = 4
        _buttons(body, row, ("Close", self.reject, ""),
                 ("Create room from this file…", self._create, "primary"))

    def _paint_preview(self) -> None:
        """Fit the content in view (QGraphicsView.fitInView), painted by the export renderer."""
        from PIL import ImageTk

        width, height = self.PREVIEW
        rect = self.scene.content_rect()
        base = Affine()
        if not rect.is_null():
            rect = rect.adjusted(-30, -30, 30, 30)
            scale = min(width / rect.w, height / rect.h)
            base = Affine(a=scale, d=scale,
                          e=width / 2 - rect.center().x * scale,
                          f=height / 2 - rect.center().y * scale)
        image = render_scene(self.scene.ordered(), (width, height), base,
                             theme.current().bg_canvas)
        self._photo = ImageTk.PhotoImage(image, master=self)
        self.view.create_image(0, 0, image=self._photo, anchor="nw")

    def _create(self) -> None:
        self.choice = self.CREATE_ROOM
        self.accept()


def ask_password(parent: Any, label: str = "Password") -> str | None:
    return ask_text(parent, "Password", label, password=True)


class PasswordFields(Frame):
    """Password + confirm with a strength hint, used wherever a file can be protected."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.columnconfigure(1, weight=1)
        self.first = LineEdit(self, password=True)
        self.second = LineEdit(self, password=True)
        self.hint = Label(self, "", "muted", wraplength=380, justify="left")
        _row(self, "Password", self.first, 0)
        _row(self, "Confirm", self.second, 1)
        self.hint.grid(row=2, column=0, columnspan=2, sticky="w")
        self.first.textChanged.connect(lambda *_: self._update())
        self.second.textChanged.connect(lambda *_: self._update())
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


class SaveCopyDialog(Dialog):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent, "Save a copy")
        body = _form(self)
        self.include_chat = CheckBox(body, "Include room chat (private messages are never "
                                           "included)")
        self.include_chat.grid(row=0, column=0, columnspan=2, sticky="w")
        self.protect = CheckBox(body, "Protect with password")
        self.protect.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.fields = PasswordFields(body)
        self.fields.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.fields.setVisible(False)
        self.protect.toggled.connect(self.fields.setVisible)
        _buttons(body, 3, ("Cancel", self.reject, ""),
                 ("Choose location…", self._try_accept, "primary"))

    def _try_accept(self) -> None:
        if self.protect.isChecked() and not self.fields.valid():
            warn(self, "Password", "The two passwords need to match.")
            return
        self.accept()

    def password(self) -> str | None:
        return self.fields.password() if self.protect.isChecked() else None


FORMATS = ("PNG", "JPEG", "SVG", "PDF", ".lanvas")


class ExportDialog(Dialog):
    """Only the options relevant to the chosen format are enabled (Design.md section 8.4)."""

    def __init__(
        self,
        frames: list[tuple[str, str]],
        has_selection: bool,
        estimate: Any,
        allow_lanvas: bool = True,
        parent: Any = None,
    ) -> None:
        super().__init__(parent, "Export")
        self.minsize(460, 0)
        self.estimate = estimate
        form = _form(self)

        row = Frame(form)
        self.format_group = RadioGroup(self)
        for name in FORMATS:
            button = self.format_group.add(row, name)
            button.pack(side="left", padx=(0, 8))
            if name == "PNG":
                button.setChecked(True)
            if name == ".lanvas" and not allow_lanvas:
                button.setEnabled(False)
        _row(form, "Format", row, 0)

        row = Frame(form)
        self.scope_group = RadioGroup(self)
        self.scope_board = self.scope_group.add(row, "Whole board")
        self.scope_board.setChecked(True)
        self.scope_selection = self.scope_group.add(row, "Selection")
        self._has_selection = has_selection
        self.scope_frame = self.scope_group.add(row, "Frame")
        self.frame_combo = ComboBox(row, width=12)
        for frame_id, name in frames:
            self.frame_combo.addItem(name, frame_id)
        for widget in (self.scope_board, self.scope_selection, self.scope_frame,
                       self.frame_combo):
            widget.pack(side="left", padx=(0, 8))
        _row(form, "What", row, 1)

        row = Frame(form)
        self.scale = ComboBox(row, width=6)
        for value in (1, 2, 4):
            self.scale.addItem(f"{value}x", value)
        self.transparent = CheckBox(row, "Transparent background")
        self.scale.pack(side="left", padx=(0, 8))
        self.transparent.pack(side="left")
        _row(form, "PNG/JPEG", row, 2)

        row = Frame(form)
        self.page = ComboBox(row, width=14)
        self.page.addItem("Fit to content", "fit")
        self.page.addItem("A4 landscape", "A4")
        self.page.addItem("A3 landscape", "A3")
        self.each_frame = CheckBox(row, "Each frame as a page")
        self.page.pack(side="left", padx=(0, 8))
        self.each_frame.pack(side="left")
        _row(form, "PDF", row, 3)

        row = Frame(form)
        self.include_chat = CheckBox(row, "Include room chat")
        self.protect = CheckBox(row, "Protect with password")
        self.include_chat.pack(side="left", padx=(0, 8))
        self.protect.pack(side="left")
        _row(form, ".lanvas", row, 4)
        self.fields = PasswordFields(form)
        self.fields.grid(row=5, column=0, columnspan=2, sticky="ew")
        self.fields.setVisible(False)
        self.protect.toggled.connect(self.fields.setVisible)

        self.estimate_label = Label(form, "", "muted")
        self.estimate_label.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        _buttons(form, 7, ("Cancel", self.reject, ""), ("Export…", self._try_accept, "primary"))

        self.format_group.toggled.connect(self._refresh)
        self.scope_group.toggled.connect(self._refresh)
        self.scale.currentIndexChanged.connect(self._refresh)
        self.frame_combo.currentIndexChanged.connect(self._refresh)
        self._refresh()

    def format_name(self) -> str:
        checked = self.format_group.checked()
        return checked.text() if checked else "PNG"

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
        for widget in (self.scope_board, self.scope_selection, self.scope_frame,
                       self.frame_combo):
            widget.setEnabled(not native and self._scope_usable(widget))
        self.estimate_label.setText(self.estimate(self) if callable(self.estimate) else "")

    def _scope_usable(self, widget: Any) -> bool:
        if widget is self.scope_frame or widget is self.frame_combo:
            return self.frame_combo.count() > 0
        if widget is self.scope_selection:
            return self._has_selection
        return True

    def _try_accept(self) -> None:
        if self.protect.isChecked() and self.protect.isEnabled() and not self.fields.valid():
            warn(self, "Password", "The two passwords need to match.")
            return
        self.accept()


class VersionHistoryDialog(Dialog):
    """Checkpoints from Ctrl+S. Restoring replaces the board for everyone, and can be undone."""

    def __init__(self, session: Any, checkpoints: list[dict[str, Any]], parent: Any = None) -> None:
        super().__init__(parent, "Version history")
        self.minsize(480, 360)
        self.session = session
        self.restored: str | None = None
        body = _form(self)
        body.columnconfigure(0, weight=1)
        self.list = ListWidget(body, height=12)
        for entry in reversed(checkpoints):
            author = entry.get("saved_by", {}).get("name", "?")
            label = entry.get("label") or "Saved"
            when = entry.get("time", "")[:19].replace("T", " ")
            digest = entry.get("board_hash", "")[7:15]
            self.list.addItem(f"{when}  ·  {author}  ·  {label}  ·  {digest}",
                              entry["checkpoint_id"])
        self.list.grid(row=0, column=0, columnspan=2, sticky="nsew")
        body.rowconfigure(0, weight=1)
        if not checkpoints:
            Label(body, "Nothing yet. Each Ctrl+S adds a version here.", "muted").grid(
                row=1, column=0, sticky="w", pady=(6, 0))
        _, restore = _buttons(body, 2, ("Close", self.reject, ""),
                              ("Restore this version", self._restore, "primary"))
        restore.setEnabled(session.role == "owner")

    def _restore(self) -> None:
        chosen = self.list.currentData()
        if chosen is None:
            return
        if ask_yes_no(self, "Restore this version",
                      "The board changes to this version for everyone. You can undo this with "
                      "Ctrl+Z."):
            self.restored = chosen
            self.accept()


class MemberPickerDialog(Dialog):
    def __init__(self, session: Any, parent: Any = None) -> None:
        super().__init__(parent, "New private message")
        self.chosen: str | None = None
        body = _form(self)
        body.columnconfigure(0, weight=1)
        self.list = ListWidget(body, height=8)
        names = [m.get("name") for m in session.members.values()]
        lock = icons.photo("lock", theme.current().private, master=self)
        for member in session.members.values():
            if member["fp"] == session.fp:
                continue
            name = member.get("name", "?")
            # Fingerprints disambiguate only when needed: display names are allowed to collide.
            if names.count(name) > 1:
                name = f"{name} ({short_fingerprint(member['fp'])})"
            self.list.addItem(name, member["fp"], icon=lock)
        self.list.itemActivated.connect(self._choose)
        self.list.itemClicked.connect(self._choose)
        self.list.grid(row=0, column=0, sticky="nsew")
        if self.list.count() == 0:
            Label(body, "No other members in this room yet.", "muted").grid(
                row=1, column=0, sticky="w", pady=(6, 0))

    def _choose(self, fingerprint: str | None) -> None:
        if fingerprint:
            self.chosen = fingerprint
            self.accept()
