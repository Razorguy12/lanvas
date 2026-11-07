"""The People popover: who is here, their role and state, plus owner controls."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import Any

from client import theme
from client.widgets import (
    Avatar,
    Button,
    ComboBox,
    Label,
    ScrollFrame,
    ask_yes_no,
    section_label,
)
from common.identity import short_fingerprint


def state_text(member: dict[str, Any]) -> str:
    if member.get("away"):
        return "away"
    return "online" if member.get("online") else "offline"


class PeoplePopover(tk.Toplevel):
    """A borderless popup that closes when it loses focus, like a Qt.Popup window."""

    def __init__(self, master: tk.Misc, session: Any, on_message: Callable[[str], None]) -> None:
        super().__init__(master)
        t = theme.current()
        self.session = session
        self.on_message = on_message
        self.wm_overrideredirect(True)
        self.configure(background=t.border)
        card = tk.Frame(self, background=t.bg_panel, padx=12, pady=12)
        card.pack(padx=1, pady=1, fill="both", expand=True)
        section_label(card, "People in this room").pack(anchor="w", pady=(0, 6))
        area = ScrollFrame(card, background=t.bg_panel, max_height=360)
        area.pack(fill="both", expand=True)
        members = sorted(
            session.members.values(),
            key=lambda m: (
                not m.get("online"), m.get("role") != "owner", m.get("name", "").lower()
            ),
        )
        for member in members:
            self._row(area.body, member).pack(fill="x", pady=2)
        self.bind("<Escape>", lambda e: self.close())
        self.bind("<FocusOut>", self._focus_out)
        self.minsize(360, 0)

    def _focus_out(self, _event: Any) -> None:
        # Combobox dropdowns take focus briefly; only close once focus left the popover for good.
        self.after(150, lambda: self.winfo_exists() and self.focus_get() is None and self.close())

    def close(self) -> None:
        if self.winfo_exists():
            self.destroy()

    def _row(self, master: tk.Misc, member: dict[str, Any]) -> tk.Frame:
        t = theme.current()
        row = tk.Frame(master, background=t.bg_panel)
        fingerprint = member["fp"]
        Avatar(row, member.get("name", "?"), fingerprint, member.get("away", False)).pack(
            side="left", padx=(0, 8))
        you = " (you)" if fingerprint == self.session.fp else ""
        muted = " · muted" if member.get("muted") else ""
        name = member.get("name", "?")
        label = Label(row, f"{name}{you}\n{member.get('role', '')} · {state_text(member)}{muted}",
                      "panel", justify="left")
        label.setToolTip(short_fingerprint(fingerprint))
        label.pack(side="left", fill="x", expand=True)
        if fingerprint != self.session.fp:
            message = Button(row, "Message")
            message.clicked.connect(lambda: (self.close(), self.on_message(fingerprint)))
            message.pack(side="left", padx=2)
        if self.session.role == "owner" and member.get("role") != "owner":
            self._owner_controls(row, member)
        return row

    def _owner_controls(self, row: tk.Frame, member: dict[str, Any]) -> None:
        fingerprint = member["fp"]
        role = ComboBox(row, width=7)
        role.addItems(["editor", "viewer"])
        role.setCurrentText(member.get("role", "viewer"))
        role.activated.connect(
            lambda *_: self.session.send_admin("set_role", fp=fingerprint, role=role.currentText())
        )
        role.pack(side="left", padx=2)
        mute = Button(row, "Unmute" if member.get("muted") else "Mute")
        mute.clicked.connect(lambda: (
            self.session.send_admin("mute", fp=fingerprint, muted=not member.get("muted")),
            self.close(),
        ))
        mute.pack(side="left", padx=2)
        kick = Button(row, "Kick", "danger")
        kick.clicked.connect(lambda: self._kick(fingerprint, member.get("name", "this person")))
        kick.pack(side="left", padx=2)

    def _kick(self, fingerprint: str, name: str) -> None:
        master = self.master
        self.close()
        if ask_yes_no(master, "Kick",
                      f"Remove {name} from this room? They would have to ask to join again."):
            self.session.send_admin("kick", fp=fingerprint)
