"""Packet Inspector and Chaos panel (Design.md section 9): the protocol, made visible."""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from PyQt6.QtCore import QPointF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from client import theme
from common.protocol import MsgType

MAX_ROWS = 300
REFRESH_MS = 400
SPARK_POINTS = 60


class Sparkline(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.values: deque[float] = deque(maxlen=SPARK_POINTS)
        self.setFixedSize(160, 28)

    def add(self, value: float) -> None:
        self.values.append(value)
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 (Qt override)
        if len(self.values) < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(theme.current().accent), 1.5))
        top = max(max(self.values), 1.0)
        step = self.width() / (SPARK_POINTS - 1)
        points = [
            QPointF(i * step, self.height() - 2 - v / top * (self.height() - 4))
            for i, v in enumerate(self.values)
        ]
        painter.drawPolyline(QPolygonF(points))


class InspectorPanel(QWidget):
    def __init__(self, get_core: Any, discovery: Any) -> None:
        super().__init__()
        self.get_core = get_core
        self.discovery = discovery
        self.paused = False
        self._last_total = 0
        self._last_time = time.monotonic()
        self._records: list[Any] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 8)
        tabs = QTabWidget()
        tabs.addTab(self._frames_tab(), "Frames")
        tabs.addTab(self._discovery_tab(), "Discovery")
        tabs.addTab(self._chaos_tab(), "Chaos (Debug)")
        root.addWidget(tabs)
        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)

    def showEvent(self, event: Any) -> None:  # noqa: N802
        self.timer.start()  # nothing is polled while closed, so a closed panel costs nothing

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self.timer.stop()

    # ------------------------------------------------------------------ frames
    def _frames_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 0)
        controls = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItem("All types", "")
        for member in MsgType:
            self.filter.addItem(member.name, member.name)
        self.filter.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.filter)
        self.hide_noise = QCheckBox("Hide cursors and pings")
        self.hide_noise.setChecked(True)
        self.hide_noise.toggled.connect(self.refresh)
        controls.addWidget(self.hide_noise)
        pause = QPushButton("Pause")
        pause.setCheckable(True)
        pause.toggled.connect(self._toggle_pause)
        controls.addWidget(pause)
        self.rate = QLabel("0 msgs/s")
        self.rate.setObjectName("muted")
        controls.addWidget(self.rate)
        self.spark = Sparkline()
        controls.addWidget(self.spark)
        controls.addStretch(1)
        layout.addLayout(controls)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Time", "Dir", "Type", "Seq", "Size", "RTT"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._show_details)
        split.addWidget(self.table)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Select a frame to see its decoded JSON.")
        split.addWidget(self.details)
        split.setSizes([640, 360])
        layout.addWidget(split)
        return page

    def _toggle_pause(self, paused: bool) -> None:
        self.paused = paused

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
        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for index, record in enumerate(reversed(rows)):
            rtt = f"{record.rtt * 1000:.1f} ms" if record.rtt is not None else ""
            cells = (
                time.strftime("%H:%M:%S", time.localtime(record.time)),
                "←" if record.direction == "in" else "→",
                record.type_name,
                "" if record.seq is None else str(record.seq),
                str(record.size),
                rtt,
            )
            for col, value in enumerate(cells):
                self.table.setItem(index, col, QTableWidgetItem(value))
        self.table.setUpdatesEnabled(True)
        self.table.blockSignals(False)

    def _show_details(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._records):
            self.details.setPlainText(self._records[len(self._records) - 1 - row].preview)

    # ------------------------------------------------------------------ discovery
    def _discovery_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 0)
        self.discovery_table = QTableWidget(0, 3)
        self.discovery_table.setHorizontalHeaderLabels(["Source IP", "Rooms", "Age"])
        self.discovery_table.verticalHeader().setVisible(False)
        self.discovery_table.horizontalHeader().setStretchLastSection(True)
        self.discovery_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.discovery_table)
        return page

    def _refresh_discovery(self) -> None:
        if self.discovery is None:
            return
        newest: dict[str, dict[str, Any]] = {}
        for entry in list(self.discovery.tracker.log):
            newest[entry["source"]] = entry
        self.discovery_table.setRowCount(len(newest))
        now = time.time()
        for row, (source, entry) in enumerate(sorted(newest.items())):
            names = ", ".join(r.get("name", "?") for r in entry["msg"]["rooms"])
            cells = (source, names, f"{now - entry['time']:.1f} s")
            for col, value in enumerate(cells):
                self.discovery_table.setItem(row, col, QTableWidgetItem(value))

    # ------------------------------------------------------------------ chaos
    def _chaos_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        warning = QLabel(
            "Debug: this deliberately damages this client's own traffic, to show that ordering and "
            "recovery hold. Everyone else is unaffected."
        )
        warning.setWordWrap(True)
        warning.setObjectName("muted")
        layout.addWidget(warning)
        row = QHBoxLayout()
        self.latency_label = QLabel("Added latency: 0 ms")
        row.addWidget(self.latency_label)
        self.latency = QSlider(Qt.Orientation.Horizontal)
        self.latency.setRange(0, 1000)
        self.latency.valueChanged.connect(self._latency_changed)
        row.addWidget(self.latency, 1)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.dup_label = QLabel("Duplicate frames: 0%")
        row.addWidget(self.dup_label)
        self.dup = QSlider(Qt.Orientation.Horizontal)
        self.dup.setRange(0, 100)
        self.dup.valueChanged.connect(self._dup_changed)
        row.addWidget(self.dup, 1)
        layout.addLayout(row)
        row = QHBoxLayout()
        drop = QPushButton("Drop connection")
        drop.clicked.connect(self._drop)
        row.addWidget(drop)
        reorder = QPushButton("Reorder next 5")
        reorder.clicked.connect(self._reorder)
        row.addWidget(reorder)
        reset = QPushButton("Reset")
        reset.clicked.connect(self._reset)
        row.addWidget(reset)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
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
