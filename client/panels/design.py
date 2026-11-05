"""Design panel: properties of the current selection, with "Mixed" for differing values."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from client import icons, theme
from client.widgets import ColorButton, NumberField, ScrubLabel, section_label

GEOMETRY = ("x", "y", "w", "h", "rotation")
HAS_FILL = ("rect", "ellipse", "frame", "path", "text", "sticky")
HAS_STROKE = ("rect", "ellipse", "frame", "line", "arrow", "path")
HAS_TEXT = ("text", "sticky")
HAS_SIZE = ("rect", "ellipse", "frame", "text", "sticky", "image", "path")
WEIGHTS = ((400, "Regular"), (500, "Medium"), (600, "Semi-bold"), (700, "Bold"))
ALIGN_BUTTONS = (
    ("alignleft", "left", "Align left (Alt+A)"),
    ("alignh", "hcenter", "Align horizontal centers (Alt+H)"),
    ("alignright", "right", "Align right (Alt+D)"),
    ("aligntop", "top", "Align top (Alt+W)"),
    ("alignv", "vcenter", "Align vertical centers (Alt+V)"),
    ("alignbottom", "bottom", "Align bottom (Alt+S)"),
)


class DesignPanel(QWidget):
    def __init__(self, canvas: Any, on_export_selection: Callable[[], None]) -> None:
        super().__init__()
        self.canvas = canvas
        self._loading = False
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        self.empty = QLabel("Select something to edit its properties.")
        self.empty.setObjectName("muted")
        self.empty.setWordWrap(True)
        root.addWidget(self.empty)

        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)
        root.addWidget(self.body)
        root.addStretch(1)

        body.addWidget(section_label("Alignment"))
        row = QHBoxLayout()
        for icon_name, mode, tip in ALIGN_BUTTONS:
            button = QToolButton()
            button.setIcon(icons.icon(icon_name, theme.current().text_primary))
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, m=mode: canvas.align(m))
            row.addWidget(button)
        row.addStretch(1)
        body.addLayout(row)

        body.addWidget(section_label("Position and size"))
        grid = QGridLayout()
        self.fields: dict[str, NumberField] = {}
        labels = (("x", "X", 0, 0), ("y", "Y", 0, 2), ("w", "W", 1, 0), ("h", "H", 1, 2))
        for prop, text, r, c in labels:
            field = NumberField(lo=0 if prop in "wh" else -1e7)
            self.fields[prop] = field
            grid.addWidget(ScrubLabel(text, field), r, c)
            grid.addWidget(field, r, c + 1)
            field.editingFinished.connect(lambda p=prop: self._number_edited(p))
        rotation = NumberField(lo=-36000, hi=36000)
        self.fields["rotation"] = rotation
        grid.addWidget(ScrubLabel("R", rotation), 2, 0)
        grid.addWidget(rotation, 2, 1)
        rotation.editingFinished.connect(lambda: self._number_edited("rotation"))
        body.addLayout(grid)

        self.fill_label = section_label("Fill")
        body.addWidget(self.fill_label)
        self.fill = ColorButton("#D9D9D9")
        self.fill.color_chosen.connect(lambda c: self._set_all("fill", c, HAS_FILL))
        body.addWidget(self.fill)

        self.stroke_label = section_label("Stroke")
        body.addWidget(self.stroke_label)
        self.stroke_row = QWidget()
        stroke_layout = QHBoxLayout(self.stroke_row)
        stroke_layout.setContentsMargins(0, 0, 0, 0)
        self.stroke = ColorButton("#1E1E1E")
        self.stroke.color_chosen.connect(lambda c: self._set_all("stroke", c, HAS_STROKE))
        stroke_layout.addWidget(self.stroke)
        self.stroke_w = NumberField(lo=0, hi=1000)
        self.stroke_w.editingFinished.connect(self._stroke_width_edited)
        stroke_layout.addWidget(ScrubLabel("W", self.stroke_w))
        stroke_layout.addWidget(self.stroke_w)
        body.addWidget(self.stroke_row)

        body.addWidget(section_label("Opacity"))
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity.sliderReleased.connect(self._opacity_edited)
        self.opacity.valueChanged.connect(self._opacity_preview)
        body.addWidget(self.opacity)

        self.text_label = section_label("Text")
        body.addWidget(self.text_label)
        self.text_row = QWidget()
        text_layout = QHBoxLayout(self.text_row)
        text_layout.setContentsMargins(0, 0, 0, 0)
        self.font_size = NumberField(lo=1, hi=1000)
        self.font_size.editingFinished.connect(self._font_size_edited)
        text_layout.addWidget(ScrubLabel("Size", self.font_size))
        text_layout.addWidget(self.font_size)
        self.weight = QComboBox()
        for value, label in WEIGHTS:
            self.weight.addItem(label, value)
        self.weight.activated.connect(self._weight_edited)
        text_layout.addWidget(self.weight)
        body.addWidget(self.text_row)

        export_button = QPushButton("Export selection…")
        export_button.clicked.connect(on_export_selection)
        body.addWidget(export_button)

        canvas.selection_changed.connect(self.refresh)
        self._group = ""
        self.refresh()

    def _objects(self) -> list[dict[str, Any]]:
        board = self.canvas.doc().view
        found = (board.get_live(i) for i in self.canvas.selection)
        return [o for o in found if o is not None]

    @staticmethod
    def _common(objects: list[dict[str, Any]], prop: str, default: Any = None) -> tuple[Any, bool]:
        """Returns (value, mixed). A property the objects do not carry reads as its default."""
        values = [o["props"].get(prop, default) for o in objects]
        if not values:
            return default, False
        first = values[0]
        return first, any(v != first for v in values)

    def refresh(self) -> None:
        objects = self._objects()
        self.empty.setVisible(not objects)
        self.body.setVisible(bool(objects))
        if not objects:
            return
        self._loading = True
        types = {o["type"] for o in objects}
        for prop, field in self.fields.items():
            value, mixed = self._common(objects, prop, 0)
            field.show_value(None if mixed else value)
            enabled = self.canvas.can_edit()
            if prop in ("w", "h", "rotation"):
                enabled = enabled and types <= set(HAS_SIZE)
            field.setEnabled(enabled)
        show_fill = bool(types & set(HAS_FILL))
        self.fill_label.setVisible(show_fill)
        self.fill.setVisible(show_fill)
        value, mixed = self._common(objects, "fill")
        self.fill.set_color(value, mixed)
        show_stroke = bool(types & set(HAS_STROKE))
        self.stroke_label.setVisible(show_stroke)
        self.stroke_row.setVisible(show_stroke)
        value, mixed = self._common(objects, "stroke")
        self.stroke.set_color(value, mixed)
        value, mixed = self._common(objects, "stroke_w", 1)
        self.stroke_w.show_value(None if mixed else value)
        value, mixed = self._common(objects, "opacity", 1)
        self.opacity.blockSignals(True)
        self.opacity.setValue(round((value if not mixed else 1) * 100))
        self.opacity.blockSignals(False)
        show_text = bool(types & set(HAS_TEXT))
        self.text_label.setVisible(show_text)
        self.text_row.setVisible(show_text)
        value, mixed = self._common(objects, "font_size", 16)
        self.font_size.show_value(None if mixed else value)
        value, mixed = self._common(objects, "font_weight", 400)
        index = self.weight.findData(value)
        self.weight.setCurrentIndex(index if index >= 0 and not mixed else 0)
        for widget in (
            self.fill,
            self.stroke,
            self.stroke_w,
            self.opacity,
            self.font_size,
            self.weight,
        ):
            widget.setEnabled(self.canvas.can_edit())
        self._loading = False

    def _apply(self, prop: str, value: Any, allowed: tuple[str, ...] | None = None) -> None:
        """Apply to every selected object that supports the property, as a single undo step."""
        if self._loading or not self.canvas.can_edit():
            return
        token = uuid.uuid4().hex[:8]
        for obj in self._objects():
            if obj["props"].get("locked"):
                continue
            if allowed is None or obj["type"] in allowed:
                self.canvas.session.update(obj["id"], {prop: value}, group=token)

    def _set_all(self, prop: str, color: str | None, allowed: tuple[str, ...]) -> None:
        self._apply(prop, color, allowed)
        if prop == "fill":
            self.canvas.fill_color = color
        elif color:
            self.canvas.stroke_color = color

    def _number_edited(self, prop: str) -> None:
        field = self.fields[prop]
        if not field.is_mixed():
            self._apply(prop, field.value(), HAS_SIZE if prop in ("w", "h", "rotation") else None)

    def _stroke_width_edited(self) -> None:
        if not self.stroke_w.is_mixed():
            self.canvas.stroke_width = self.stroke_w.value()
            self._apply("stroke_w", self.stroke_w.value(), HAS_STROKE)

    def _opacity_preview(self) -> None:
        for obj in self._objects():
            item = self.canvas.board_scene.items_by_id.get(obj["id"])
            if item is not None and not self._loading:
                item.setOpacity(self.opacity.value() / 100)

    def _opacity_edited(self) -> None:
        self._apply("opacity", self.opacity.value() / 100)

    def _font_size_edited(self) -> None:
        if not self.font_size.is_mixed():
            self._apply("font_size", self.font_size.value(), HAS_TEXT)

    def _weight_edited(self) -> None:
        self._apply("font_weight", self.weight.currentData(), HAS_TEXT)
