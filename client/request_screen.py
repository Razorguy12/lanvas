"""The "Request access" screen. Deliberately renders no board, chat or user list of any kind."""

from __future__ import annotations

import tkinter as tk
from typing import Any

from PIL import Image, ImageTk

from client import theme
from client.widgets import Button, Frame, Label, Signal, Spinner, Visible, after_ms
from common.identity import short_fingerprint

STATE_WAITING = "waiting"
STATE_APPROVED = "approved"
STATE_DENIED = "denied"
CARD_WIDTH = 420


class RequestAccessPage(Visible, tk.Canvas):
    def __init__(self, master: tk.Misc) -> None:
        t = theme.current()
        super().__init__(master, highlightthickness=0, borderwidth=0, background=t.bg_app)
        self.cancel = Signal()
        self.retry = Signal()
        self.remaining = 0
        self._timer: str | None = None
        self._backdrop: ImageTk.PhotoImage | None = None
        self._backdrop_item = self.create_image(0, 0, anchor="nw")
        card = Frame(self, "card", padding=24, width=CARD_WIDTH)
        self.room = Label(card, "", "cardhero", wraplength=CARD_WIDTH - 50, justify="left")
        self.room.pack(anchor="w", fill="x")
        self.owner = Label(card, "", "panelmuted")
        self.owner.pack(anchor="w", pady=(6, 0))
        state_row = tk.Frame(card, background=t.bg_panel)
        state_row.pack(fill="x", pady=(10, 0))
        self.spinner = Spinner(state_row)
        self.spinner.pack(side="left", padx=(0, 8))
        self.state = Label(state_row, "", "panel", wraplength=CARD_WIDTH - 80, justify="left")
        self.state.pack(side="left", fill="x", expand=True)
        Label(card, "Your fingerprint, for the owner to check with you:", "panelmuted",
              wraplength=CARD_WIDTH - 50, justify="left").pack(anchor="w", pady=(10, 0))
        self.fingerprint = tk.Entry(card, relief="flat", borderwidth=0,
                                    readonlybackground=t.bg_panel, foreground=t.text_primary,
                                    font=theme.font(13, 600), highlightthickness=0)
        self.fingerprint.configure(state="readonly")  # selectable, so it can be read out or copied
        self.fingerprint.pack(anchor="w", fill="x", pady=(4, 0))
        buttons = tk.Frame(card, background=t.bg_panel)
        buttons.pack(fill="x", pady=(14, 0))
        cancel = Button(buttons, "Cancel")
        cancel.clicked.connect(self.cancel.emit)
        cancel.pack(side="right")
        self.retry_button = Button(buttons, "Ask again")
        self.retry_button.clicked.connect(self.retry.emit)
        self.retry_button.pack(side="right", padx=(0, 6))
        self.retry_button.setVisible(False)
        self._card = self.create_window(0, 0, window=card, width=CARD_WIDTH, anchor="center")
        self.bind("<Configure>", self._layout)

    def _layout(self, event: Any) -> None:
        """A soft abstract backdrop. It is a placeholder drawn from nothing, not real board data."""
        self.coords(self._card, event.width / 2, event.height / 2)
        width, height = max(1, event.width), max(1, event.height)
        t = theme.current()
        start, end = theme.rgb(t.bg_canvas), theme.rgb(t.bg_app)
        small = Image.linear_gradient("L").resize((64, 64)).rotate(45, fillcolor=128)
        gradient = Image.merge("RGB", [
            small.point(lambda v, a=a, b=b: round(a + (b - a) * v / 255))
            for a, b in zip(start, end, strict=True)
        ]).resize((width, height), Image.BILINEAR)
        self._backdrop = ImageTk.PhotoImage(gradient, master=self)
        self.itemconfigure(self._backdrop_item, image=self._backdrop)

    def text_of_fingerprint(self) -> str:
        return self.fingerprint.get()

    def _set_fingerprint(self, text: str) -> None:
        self.fingerprint.configure(state="normal")
        self.fingerprint.delete(0, "end")
        self.fingerprint.insert(0, text)
        self.fingerprint.configure(state="readonly")

    def set_info(self, room: str, owner: str, fingerprint: str) -> None:
        self.room.setText(room)
        self.owner.setText(f"Owner: {owner}" if owner else "")
        self._set_fingerprint(short_fingerprint(fingerprint))
        self.show_state(STATE_WAITING)

    def show_connecting(self, target: str) -> None:
        self.room.setText("Connecting…")
        self.owner.setText(target)
        self._set_fingerprint("")
        self._state_text("Reaching the server…", theme.current().text_muted)
        self.spinner.setVisible(True)

    def show_state(self, state: str, retry_after: float | None = None) -> None:
        t = theme.current()
        self._stop_timer()
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
            self._tick()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None

    def _state_text(self, text: str, color: str) -> None:
        self.state.setText(text)
        self.state.set_color(color, bold=True)

    def _tick(self) -> None:
        t = theme.current()
        self._timer = None
        self.remaining -= 1
        if self.remaining > 0:
            self._state_text(f"Denied. You can ask again in {self.remaining} s.", t.danger)
            self._timer = after_ms(self, 1000, self._tick)
        else:
            self._state_text("Denied. You can ask again now.", t.danger)
            self.retry_button.setVisible(True)
