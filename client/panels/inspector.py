"""Packet Inspector and Chaos panel (Design.md section 9): the protocol, made visible."""

from __future__ import annotations

import time
import tkinter as tk
from collections import deque
from tkinter import ttk
from typing import Any

from client import theme
from client.widgets import (
    Button,
    CheckBox,
    ComboBox,
    Frame,
    Label,
    Slider,
    TextView,
    after_ms,
)
from common.protocol import MsgType

MAX_ROWS = 300
REFRESH_MS = 400
SPARK_POINTS = 60
COLUMNS = ("Time", "Dir", "Type", "Seq", "Size", "RTT")


class Sparkline(tk.Canvas):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, width=160, height=28, highlightthickness=0,
                         background=theme.current().bg_panel)
        self.values: deque[float] = deque(maxlen=SPARK_POINTS)

    def add(self, value: float) -> None:
        self.values.append(value)
        self.delete("all")
        if len(self.values) < 2:
            return
        top = max(max(self.values), 1.0)
        step = 160 / (SPARK_POINTS - 1)
        coords: list[float] = []
        for i, v in enumerate(self.values):
            coords += [i * step, 28 - 2 - v / top * 24]
        self.create_line(*coords, fill=theme.current().accent, width=1.5, smooth=True)


def _table(master: tk.Misc, columns: tuple[str, ...], widths: tuple[int, ...]) -> ttk.Treeview:
    table = ttk.Treeview(master, columns=columns, show="headings", selectmode="browse")
    for name, width in zip(columns, widths, strict=True):
        table.heading(name, text=name, anchor="w")
        table.column(name, width=width, stretch=name == columns[-1], anchor="w")
    return table


class InspectorPanel(Frame):
    def __init__(self, master: tk.Misc, get_core: Any, discovery: Any) -> None:
        super().__init__(master, "panel", padding=(8, 4, 8, 8))
        self.get_core = get_core
        self.discovery = discovery
        self.paused = False
        self._last_total = 0
        self._last_time = time.monotonic()
        self._records: list[Any] = []
        self._job: str | None = None
        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True)
        tabs.add(self._frames_tab(tabs), text="Frames")
        tabs.add(self._discovery_tab(tabs), text="Discovery")
        tabs.add(self._chaos_tab(tabs), text="Chaos (Debug)")

    def start(self) -> None:
        """Nothing is polled while closed, so a closed panel costs nothing."""
        if self._job is None:
            self._tick()

    def stop(self) -> None:
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None

    def _tick(self) -> None:
        self.refresh()
        self._job = after_ms(self, REFRESH_MS, self._tick)

    # ------------------------------------------------------------------ frames
    def _frames_tab(self, master: tk.Misc) -> Frame:
        page = Frame(master, "panel", padding=(0, 4, 0, 0))
        controls = Frame(page, "panel")
        controls.pack(fill="x")
        self.filter = ComboBox(controls, width=16)
        self.filter.addItem("All types", "")
        for member in MsgType:
            self.filter.addItem(member.name, member.name)
        self.filter.currentIndexChanged.connect(lambda *_: self.refresh())
        self.filter.pack(side="left")
        self.hide_noise = CheckBox(controls, "Hide cursors and pings", checked=True, panel=True)
        self.hide_noise.toggled.connect(lambda *_: self.refresh())
        self.hide_noise.pack(side="left", padx=8)
        self.pause = Button(controls, "Pause")
        self.pause.clicked.connect(self._toggle_pause)
        self.pause.pack(side="left")
        self.rate = Label(controls, "0 msgs/s", "panelmuted")
        self.rate.pack(side="left", padx=8)
        self.spark = Sparkline(controls)
        self.spark.pack(side="left")
        split = ttk.PanedWindow(page, orient="horizontal")
        split.pack(fill="both", expand=True, pady=(4, 0))
        self.table = _table(split, COLUMNS, (80, 36, 140, 60, 60, 80))
        self.table.bind("<<TreeviewSelect>>", lambda e: self._show_details())
        split.add(self.table, weight=2)
        self.details = TextView(split, height=8)
        self.details.set_plain("Select a frame to see its decoded JSON.")
        split.add(self.details, weight=1)
        return page

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause.setText("Resume" if self.paused else "Pause")

    def rows(self) -> list[tuple[str, ...]]:
        return [tuple(self.table.item(i, "values")) for i in self.table.get_children()]

    def refresh(self) -> None:
        core = self.get_core()
        if core is None:
            return
        now = time.monotonic()
        total = core.inspector.total
        elapsed = now - self._last_time
        if elapsed >= 1.0:
            rate = (total - self._last_total) / elapsed
            self._last_total, self._last_time = total, now
            self.rate.setText(f"{rate:.0f} msgs/s")
            self.spark.add(rate)
        self._refresh_discovery()
        if self.paused:
            return
        wanted = self.filter.currentData()
        noise = ("TRANSIENT", "PING", "PONG") if self.hide_noise.isChecked() else ()
        rows = [
            r
            for r in core.inspector.snapshot()
            if (not wanted or r.type_name == wanted) and r.type_name not in noise
        ][-MAX_ROWS:]
        if len(rows) == len(self._records) and rows[-1:] == self._records[-1:]:
            return
        self._records = rows
        self.table.delete(*self.table.get_children())
        for index, record in enumerate(reversed(rows)):
            rtt = f"{record.rtt * 1000:.1f} ms" if record.rtt is not None else ""
            self.table.insert("", "end", iid=str(index), values=(
                time.strftime("%H:%M:%S", time.localtime(record.time)),
                "←" if record.direction == "in" else "→",
                record.type_name,
                "" if record.seq is None else str(record.seq),
                str(record.size),
                rtt,
            ))

    def _show_details(self) -> None:
        selected = self.table.selection()
        if selected:
            row = int(selected[0])
            if 0 <= row < len(self._records):
                self.details.set_plain(self._records[len(self._records) - 1 - row].preview)

    # ------------------------------------------------------------------ discovery
    def _discovery_tab(self, master: tk.Misc) -> Frame:
        page = Frame(master, "panel", padding=(0, 4, 0, 0))
        self.discovery_table = _table(page, ("Source IP", "Rooms", "Age"), (140, 300, 80))
        self.discovery_table.pack(fill="both", expand=True)
        return page

    def _refresh_discovery(self) -> None:
        if self.discovery is None:
            return
        newest: dict[str, dict[str, Any]] = {}
        for entry in list(self.discovery.tracker.log):
            newest[entry["source"]] = entry
        table = self.discovery_table
        table.delete(*table.get_children())
        now = time.time()
        for source, entry in sorted(newest.items()):
            names = ", ".join(r.get("name", "?") for r in entry["msg"]["rooms"])
            table.insert("", "end", values=(source, names, f"{now - entry['time']:.1f} s"))

    # ------------------------------------------------------------------ chaos
    def _chaos_tab(self, master: tk.Misc) -> Frame:
        page = Frame(master, "panel", padding=8)
        Label(page, "Debug: this deliberately damages this client's own traffic, to show that "
                    "ordering and recovery hold. Everyone else is unaffected.", "panelmuted",
              wraplength=640, justify="left").pack(anchor="w")
        row = Frame(page, "panel")
        row.pack(fill="x", pady=(8, 0))
        self.latency_label = Label(row, "Added latency: 0 ms", "panel", width=24)
        self.latency_label.pack(side="left")
        self.latency = Slider(row, 0, 1000)
        self.latency.valueChanged.connect(self._latency_changed)
        self.latency.pack(side="left", fill="x", expand=True)
        row = Frame(page, "panel")
        row.pack(fill="x", pady=(8, 0))
        self.dup_label = Label(row, "Duplicate frames: 0%", "panel", width=24)
        self.dup_label.pack(side="left")
        self.dup = Slider(row, 0, 100)
        self.dup.valueChanged.connect(self._dup_changed)
        self.dup.pack(side="left", fill="x", expand=True)
        row = Frame(page, "panel")
        row.pack(fill="x", pady=(8, 0))
        for text, action in (("Drop connection", self._drop), ("Reorder next 5", self._reorder),
                             ("Reset", self._reset)):
            button = Button(row, text)
            button.clicked.connect(action)
            button.pack(side="left", padx=(0, 6))
        return page

    def _latency_changed(self, value: int) -> None:
        self.latency_label.setText(f"Added latency: {value} ms")
        core = self.get_core()
        if core is not None:
            core.chaos.latency = value / 1000

    def _dup_changed(self, value: int) -> None:
        self.dup_label.setText(f"Duplicate frames: {value}%")
        core = self.get_core()
        if core is not None:
            core.chaos.duplicate = value / 100

    def _drop(self) -> None:
        core = self.get_core()
        if core is not None:
            core.request_drop()

    def _reorder(self) -> None:
        core = self.get_core()
        if core is not None:
            core.chaos.reorder_next = 5

    def _reset(self) -> None:
        self.latency.setValue(0)
        self.dup.setValue(0)
        core = self.get_core()
        if core is not None:
            core.chaos.reorder_next = 0
