"""Share / Access panel for the owner (Design.md section 7.3)."""

from __future__ import annotations

import tkinter as tk
from typing import Any

from client.widgets import (
    Button,
    CheckBox,
    ComboBox,
    Dialog,
    Frame,
    Label,
    LineEdit,
    ask_yes_no,
    section_label,
)
from common import netutil
from common.identity import short_fingerprint

TOGGLES = (
    ("discoverable", "Discoverable on this network"),
    ("require_code", "Require room code to request access"),
    ("show_member_count", "Show member count in discovery"),
    ("locked", "Lock board (read-only for all but the owner)"),
    ("editors_can_clear", "Editors can clear canvas"),
    ("viewers_can_export", "Viewers can export and save copies"),
)
EXPIRY = (("1 hour", 3600), ("1 day", 86400), ("7 days", 604800))


class AccessDialog(Dialog):
    def __init__(self, session: Any, parent: Any = None) -> None:
        super().__init__(parent, "Share and access")
        self.session = session
        self.minsize(460, 0)
        root = Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        section_label(root, "Room", on_panel=False).pack(anchor="w")
        Label(root, session.room.get("name", ""), "pagetitle").pack(anchor="w", pady=(2, 6))
        code = session.room_code
        address = ", ".join(i.address for i in netutil.list_interfaces())
        if session.host and session.host not in ("127.0.0.1", "localhost"):
            address = session.host
        _copy_row(root, "Room code", code or "(shown only when the room was created)", bool(code))
        _copy_row(root, "LAN address", f"{address}  (port {session.port})", True)
        section_label(root, "Requests", on_panel=False).pack(anchor="w", pady=(10, 2))
        self.requests = Frame(root)
        self.requests.pack(fill="x")
        self._fill_requests()
        section_label(root, "Settings", on_panel=False).pack(anchor="w", pady=(10, 2))
        for key, label in TOGGLES:
            box = CheckBox(root, label, checked=bool(session.settings.get(key)))
            box.toggled.connect(
                lambda v, k=key: session.send_admin("set_settings", settings={k: v})
            )
            box.pack(anchor="w")
        section_label(root, "Invite", on_panel=False).pack(anchor="w", pady=(10, 2))
        invite = Frame(root)
        invite.pack(fill="x")
        self.invite_role = ComboBox(invite, width=8)
        self.invite_role.addItems(["editor", "viewer"])
        self.invite_role.pack(side="left")
        self.invite_expiry = ComboBox(invite, width=8)
        for label, seconds in EXPIRY:
            self.invite_expiry.addItem(label, seconds)
        self.invite_expiry.pack(side="left", padx=6)
        make = Button(invite, "Create single-use invite")
        make.clicked.connect(self._create_invite)
        make.pack(side="left")
        self.invite_out = LineEdit(root, placeholder="The invite code appears here")
        self.invite_out.setReadOnly(True)
        self.invite_out.pack(fill="x", pady=(6, 0))
        section_label(root, "Members", on_panel=False).pack(anchor="w", pady=(10, 2))
        self._member_table(root).pack(fill="x")
        close = Button(root, "Close")
        close.clicked.connect(self.accept)
        close.pack(fill="x", pady=(12, 0))

    def _fill_requests(self) -> None:
        for child in self.requests.winfo_children():
            child.destroy()
        pending = list(self.session.pending_requests.values())
        if not pending:
            Label(self.requests, "No pending requests.", "muted").pack(anchor="w")
        for request in pending:
            row = Frame(self.requests)
            row.pack(fill="x", pady=1)
            fp = request["fingerprint"]
            Label(row, f"{request['name']} · {short_fingerprint(fp)}").pack(
                side="left", fill="x", expand=True)
            for label, decision, role in (
                ("Editor", "approve", "editor"),
                ("Viewer", "approve", "viewer"),
                ("Deny", "deny", None),
            ):
                button = Button(row, label)
                button.clicked.connect(lambda d=decision, r=role, f=fp: self._decide(f, d, r))
                button.pack(side="left", padx=(4, 0))

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
        self.invite_out.setReadOnly(False)
        self.invite_out.setText(token)
        self.invite_out.setReadOnly(True)

    def _member_table(self, master: tk.Misc) -> Frame:
        box = Frame(master)
        rows = 0
        for member in self.session.members.values():
            if member.get("role") == "owner":
                continue
            rows += 1
            row = Frame(box)
            row.pack(fill="x", pady=1)
            fp = member["fp"]
            Label(row, f"{member.get('name', '?')} · {short_fingerprint(fp)}").pack(
                side="left", fill="x", expand=True)
            role = ComboBox(row, width=7)
            role.addItems(["editor", "viewer"])
            role.setCurrentText(member.get("role", "viewer"))
            role.activated.connect(
                lambda *_, f=fp, w=role: self.session.send_admin(
                    "set_role", fp=f, role=w.currentText()
                )
            )
            role.pack(side="left", padx=(4, 0))
            mute = Button(row, "Unmute" if member.get("muted") else "Mute")
            mute.clicked.connect(
                lambda f=fp, m=member: self.session.send_admin(
                    "mute", fp=f, muted=not m.get("muted")
                )
            )
            mute.pack(side="left", padx=(4, 0))
            for action in ("revoke", "kick"):
                button = Button(row, action.capitalize(), "danger")
                button.clicked.connect(lambda a=action, f=fp: self._remove(a, f))
                button.pack(side="left", padx=(4, 0))
        if rows == 0:
            Label(box, "Only you so far.", "muted").pack(anchor="w")
        return box

    def _remove(self, action: str, fingerprint: str) -> None:
        if ask_yes_no(self, action.capitalize(), f"{action.capitalize()} this member?"):
            self.session.send_admin(action, fp=fingerprint)
            self.accept()


def _copy_row(master: tk.Misc, label: str, value: str, can_copy: bool) -> None:
    row = Frame(master)
    row.pack(fill="x", pady=2)
    Label(row, label, "muted", width=11).pack(side="left")
    field = LineEdit(row, value)
    field.setReadOnly(True)
    field.pack(side="left", fill="x", expand=True)
    if can_copy:
        button = Button(row, "Copy")

        def copy() -> None:
            row.clipboard_clear()
            row.clipboard_append(value)

        button.clicked.connect(copy)
        button.pack(side="left", padx=(6, 0))
