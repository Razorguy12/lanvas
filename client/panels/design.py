"""Design panel: properties of the current selection, with "Mixed" for differing values."""

from __future__ import annotations

import tkinter as tk
import uuid
from collections.abc import Callable
from typing import Any

from client.widgets import (
    Button,
    ColorButton,
    ComboBox,
    Frame,
    Label,
    NumberField,
    ScrubLabel,
    Slider,
    ToolButton,
    section_label,
)

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


class DesignPanel(Frame):
    def __init__(self, master: tk.Misc, canvas: Any,
                 on_export_selection: Callable[[], None]) -> None:
        super().__init__(master, "panel", padding=12)
        self.canvas = canvas
        self._loading = False

        self.empty = Label(self, "Select something to edit its properties.", "panelmuted",
                           wraplength=240, justify="left")
        self.empty.pack(anchor="w")
        self.body = Frame(self, "panel")
        self.body.pack(fill="x")
        body = self.body

        section_label(body, "Alignment").pack(anchor="w")
        row = Frame(body, "panel")
        row.pack(fill="x", pady=(2, 8))
        for icon_name, mode, tip in ALIGN_BUTTONS:
            button = ToolButton(row, icon_name, tooltip=tip)
            button.clicked.connect(lambda m=mode: canvas.align(m))
            button.pack(side="left")

        section_label(body, "Position and size").pack(anchor="w")
        grid = Frame(body, "panel")
        grid.pack(fill="x", pady=(2, 8))
        self.fields: dict[str, NumberField] = {}
        labels = (("x", "X", 0, 0), ("y", "Y", 0, 2), ("w", "W", 1, 0), ("h", "H", 1, 2))
        for prop, text, r, c in labels:
            field = NumberField(grid, lo=0 if prop in "wh" else -1e7)
            self.fields[prop] = field
            ScrubLabel(grid, text, field).grid(row=r, column=c, sticky="w", padx=(0, 4), pady=2)
            field.grid(row=r, column=c + 1, sticky="ew", padx=(0, 8), pady=2)
            field.editingFinished.connect(lambda p=prop: self._number_edited(p))
        rotation = NumberField(grid, lo=-36000, hi=36000)
        self.fields["rotation"] = rotation
        ScrubLabel(grid, "R", rotation).grid(row=2, column=0, sticky="w", pady=2)
        rotation.grid(row=2, column=1, sticky="ew", padx=(0, 8), pady=2)
        rotation.editingFinished.connect(lambda: self._number_edited("rotation"))
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        self.fill_label = section_label(body, "Fill")
        self.fill_label.pack(anchor="w")
        self.fill = ColorButton(body, "#D9D9D9")
        self.fill.color_chosen.connect(lambda c: self._set_all("fill", c, HAS_FILL))
        self.fill.pack(anchor="w", pady=(2, 8))

        self.stroke_label = section_label(body, "Stroke")
        self.stroke_label.pack(anchor="w")
        self.stroke_row = Frame(body, "panel")
        self.stroke_row.pack(fill="x", pady=(2, 8))
        self.stroke = ColorButton(self.stroke_row, "#1E1E1E")
        self.stroke.color_chosen.connect(lambda c: self._set_all("stroke", c, HAS_STROKE))
        self.stroke.pack(side="left")
        self.stroke_w = NumberField(self.stroke_row, lo=0, hi=1000)
        self.stroke_w.editingFinished.connect(self._stroke_width_edited)
        ScrubLabel(self.stroke_row, "W", self.stroke_w).pack(side="left", padx=(10, 4))
        self.stroke_w.pack(side="left")

        section_label(body, "Opacity").pack(anchor="w")
        self.opacity = Slider(body, 0, 100)
        self.opacity.sliderReleased.connect(self._opacity_edited)
        self.opacity.valueChanged.connect(lambda *_: self._opacity_preview())
        self.opacity.pack(fill="x", pady=(2, 8))

        self.text_label = section_label(body, "Text")
        self.text_label.pack(anchor="w")
        self.text_row = Frame(body, "panel")
        self.text_row.pack(fill="x", pady=(2, 8))
        self.font_size = NumberField(self.text_row, lo=1, hi=1000, width=5)
        self.font_size.editingFinished.connect(self._font_size_edited)
        ScrubLabel(self.text_row, "Size", self.font_size).pack(side="left", padx=(0, 4))
        self.font_size.pack(side="left")
        self.weight = ComboBox(self.text_row, width=10)
        for value, label in WEIGHTS:
            self.weight.addItem(label, value)
        self.weight.activated.connect(lambda *_: self._weight_edited())
        self.weight.pack(side="left", padx=(8, 0))

        export_button = Button(body, "Export selection…")
        export_button.clicked.connect(on_export_selection)
        export_button.pack(fill="x", pady=(4, 0))

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
        can_edit = self.canvas.can_edit()
        for prop, field in self.fields.items():
            value, mixed = self._common(objects, prop, 0)
            field.show_value(None if mixed else value)
            enabled = can_edit
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
        for widget in (self.fill, self.stroke, self.stroke_w, self.opacity, self.font_size,
                       self.weight):
            widget.setEnabled(can_edit)
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
        if self._loading:
            return
        for obj in self._objects():
            item = self.canvas.board_scene.items_by_id.get(obj["id"])
            if item is not None:
                item.setOpacity(self.opacity.value() / 100)
        self.canvas.schedule_render(interactive=True)

    def _opacity_edited(self) -> None:
        self._apply("opacity", self.opacity.value() / 100)

    def _font_size_edited(self) -> None:
        if not self.font_size.is_mixed():
            self._apply("font_size", self.font_size.value(), HAS_TEXT)

    def _weight_edited(self) -> None:
        self._apply("font_weight", self.weight.currentData(), HAS_TEXT)
