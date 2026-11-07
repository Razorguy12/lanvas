"""Layers panel: a flat, top-first list. Dragging a row writes a new fractional z for that
object."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import Any

from client import icons, theme
from client.widgets import Frame, ScrollFrame, ToolTip, after_ms, section_label
from common import fractional

COL_NAME, COL_EYE, COL_LOCK = 0, 1, 2
REFRESH_MS = 120
ICON_FOR = {"path": "pen", "group": "users"}
DRAG_START_PX = 4


class LayerRow(tk.Frame):
    """One row: type icon and name, then eye and lock toggles."""

    def __init__(self, tree: LayerTree, obj_id: str, name: str, icon: str, visible: bool,
                 locked: bool, color: str | None) -> None:
        t = theme.current()
        super().__init__(tree.body, background=t.bg_panel, height=24)
        self.tree = tree
        self.obj_id = obj_id
        self.selected = False
        self._name = name
        self.icon = tk.Label(self, image=icons.photo(icon, t.text_muted, master=tree),
                             background=t.bg_panel)
        self.icon.pack(side="left", padx=(4, 4))
        self.label = tk.Label(self, text=name, anchor="w", background=t.bg_panel,
                              foreground=color or t.text_primary, font=theme.font(12))
        self.label.pack(side="left", fill="x", expand=True)
        if color:
            ToolTip(self.label, "Someone else has this selected")
        self.lock = tk.Label(self, image=icons.photo("lock" if locked else "unlock", t.text_muted,
                                             master=tree),
                             background=t.bg_panel, width=22, cursor="hand2")
        self.lock.pack(side="right")
        ToolTip(self.lock, "Unlock" if locked else "Lock")
        self.eye = tk.Label(self, image=icons.photo("eye" if visible else "eyeoff", t.text_muted,
                                            master=tree),
                            background=t.bg_panel, width=22, cursor="hand2")
        self.eye.pack(side="right")
        ToolTip(self.eye, "Hide" if visible else "Show")
        self.eye.bind("<Button-1>", lambda e: tree.on_clicked(self, COL_EYE))
        self.lock.bind("<Button-1>", lambda e: tree.on_clicked(self, COL_LOCK))
        for widget in (self, self.icon, self.label):
            widget.bind("<ButtonPress-1>", lambda e: tree.press(self, e))
            widget.bind("<B1-Motion>", tree.motion)
            widget.bind("<ButtonRelease-1>", tree.release)
            widget.bind("<Double-Button-1>", lambda e: tree.rename(self))

    def text(self, column: int = COL_NAME) -> str:
        return self._name

    def setText(self, column: int, text: str) -> None:  # noqa: N802
        self._name = text
        self.label.configure(text=text)

    def setSelected(self, on: bool) -> None:  # noqa: N802
        self.selected = on
        t = theme.current()
        bg = t.accent if on else t.bg_panel
        for widget in (self, self.icon, self.label, self.eye, self.lock):
            widget.configure(background=bg)
        if on:
            self.label.configure(foreground="#FFFFFF")
        else:
            self.label.configure(foreground=self.tree.colors.get(self.obj_id, t.text_primary))

    def isSelected(self) -> bool:  # noqa: N802
        return self.selected


class LayerTree(ScrollFrame):
    """The list behind the panel, with the QTreeWidget calls the panel and tests rely on."""

    def __init__(self, master: tk.Misc, on_reorder: Callable[[LayerRow], None],
                 on_select: Callable[[], None], on_click: Callable[[LayerRow, int], None],
                 on_rename: Callable[[LayerRow], None]) -> None:
        super().__init__(master, background=theme.current().bg_panel)
        self.rows: list[LayerRow] = []
        self.colors: dict[str, str] = {}
        self.on_reorder, self.on_select = on_reorder, on_select
        self.on_click, self.on_rename = on_click, on_rename
        self.editable = True
        self._anchor: LayerRow | None = None
        self._drag: dict[str, Any] | None = None
        self._editor: tk.Entry | None = None

    # QTreeWidget-style access
    def topLevelItemCount(self) -> int:  # noqa: N802
        return len(self.rows)

    def topLevelItem(self, index: int) -> LayerRow:  # noqa: N802
        return self.rows[index]

    def indexOfTopLevelItem(self, row: LayerRow) -> int:  # noqa: N802
        return self.rows.index(row)

    def takeTopLevelItem(self, index: int) -> LayerRow:  # noqa: N802
        row = self.rows.pop(index)
        row.pack_forget()
        return row

    def insertTopLevelItem(self, index: int, row: LayerRow) -> None:  # noqa: N802
        self.rows.insert(index, row)
        self._repack()

    def addTopLevelItems(self, rows: list[LayerRow]) -> None:  # noqa: N802
        self.rows.extend(rows)
        self._repack()

    def clear(self) -> None:
        for row in self.rows:
            row.destroy()
        self.rows = []

    def selectedItems(self) -> list[LayerRow]:  # noqa: N802
        return [r for r in self.rows if r.selected]

    def _repack(self) -> None:
        for row in self.rows:
            row.pack_forget()
        for row in self.rows:
            row.pack(fill="x")

    def scroll_position(self) -> float:
        return self.canvas.yview()[0]

    def set_scroll_position(self, value: float) -> None:
        self.canvas.yview_moveto(value)

    # selection, drag and rename
    def press(self, row: LayerRow, event: Any) -> None:
        state = int(getattr(event, "state", 0))
        if state & 0x1 and self._anchor in self.rows:  # Shift: range
            a, b = sorted((self.rows.index(self._anchor), self.rows.index(row)))
            for i, r in enumerate(self.rows):
                r.setSelected(a <= i <= b)
        elif state & 0x4:  # Ctrl: toggle
            row.setSelected(not row.selected)
            self._anchor = row
        else:
            if not row.selected:
                for r in self.rows:
                    r.setSelected(r is row)
            self._anchor = row
        self._drag = {"row": row, "y": event.y_root, "active": False, "plain": not state & 0x5}
        self.on_select()

    def motion(self, event: Any) -> None:
        drag = self._drag
        if drag is None or not self.editable:
            return
        if not drag["active"] and abs(event.y_root - drag["y"]) >= DRAG_START_PX:
            drag["active"] = True
            drag["row"].configure(cursor="fleur")
        if drag["active"]:
            target = self._index_at(event.y_root)
            drag["target"] = target

    def _index_at(self, y_root: int) -> int:
        for i, row in enumerate(self.rows):
            top = row.winfo_rooty()
            if y_root < top + row.winfo_height() / 2:
                return i
        return len(self.rows)

    def release(self, event: Any) -> None:
        drag, self._drag = self._drag, None
        if drag is None:
            return
        row = drag["row"]
        if not drag["active"]:
            if drag["plain"]:  # a plain click on a multi-selection narrows it to this row
                for r in self.rows:
                    r.setSelected(r is row)
                self.on_select()
            return
        row.configure(cursor="")
        target = drag.get("target")
        if target is None:
            return
        index = self.rows.index(row)
        if target > index:
            target -= 1
        if target != index:
            self.takeTopLevelItem(index)
            self.insertTopLevelItem(target, row)
            self.on_reorder(row)

    def rename(self, row: LayerRow) -> None:
        if not self.editable:
            return
        entry = tk.Entry(row, font=theme.font(12), relief="solid", borderwidth=1)
        entry.insert(0, row.text().strip())
        entry.select_range(0, "end")
        entry.place(in_=row.label, relx=0, rely=0, relwidth=1, relheight=1)
        entry.focus_set()
        self._editor = entry

        def done(commit: bool) -> None:
            if self._editor is not entry:
                return
            self._editor = None
            text = entry.get()
            entry.destroy()
            if commit:
                row.setText(COL_NAME, text)
                self.on_rename(row)

        entry.bind("<Return>", lambda e: done(True))
        entry.bind("<Escape>", lambda e: done(False))
        entry.bind("<FocusOut>", lambda e: done(True))


class LayersPanel(Frame):
    def __init__(self, master: tk.Misc, canvas: Any) -> None:
        super().__init__(master, "panel", padding=(8, 8, 0, 0))
        self.canvas = canvas
        section_label(self, "Layers").pack(anchor="w", pady=(0, 4))
        self.tree = LayerTree(self, self._reordered, self._selection_from_tree, self._clicked,
                              self._renamed)
        self.tree.pack(fill="both", expand=True)
        self._updating = False
        self._timer: str | None = None
        canvas.selection_changed.connect(self._selection_to_tree)

    def schedule_refresh(self) -> None:
        """Debounced: a burst of ops must not rebuild a thousand rows once per op."""
        if self._timer is None:
            self._timer = after_ms(self, REFRESH_MS, self._timed_refresh)

    def _timed_refresh(self) -> None:
        self._timer = None
        self.refresh()

    def refresh(self) -> None:
        self._updating = True
        scroll = self.tree.scroll_position()
        self.tree.clear()
        colors = self.canvas.remote_selection_colors()
        self.tree.colors = colors
        self.tree.editable = self.canvas.can_edit()
        rows = []
        board = self.canvas.doc().view
        for obj in reversed(board.live()):
            if obj["type"] == "comment":
                continue
            level = 0
            parent = obj.get("parent")
            while parent and level < 8:
                level += 1
                parent_obj = board.get_live(parent)
                parent = parent_obj.get("parent") if parent_obj else None
            props = obj["props"]
            name = props.get("name") or obj["type"].capitalize()
            rows.append(LayerRow(
                self.tree, obj["id"], "   " * level + name, ICON_FOR.get(obj["type"], obj["type"]),
                props.get("visible", True), props.get("locked", False), colors.get(obj["id"]),
            ))
        self.tree.addTopLevelItems(rows)
        self.tree.update_idletasks()
        self.tree.set_scroll_position(scroll)
        self._updating = False
        self._selection_to_tree()

    def _object_id(self, item: LayerRow) -> str:
        return item.obj_id

    def _selection_from_tree(self) -> None:
        if self._updating:
            return
        ids = {self._object_id(i) for i in self.tree.selectedItems()}
        self.canvas.set_selection(ids)

    def _selection_to_tree(self) -> None:
        if self._updating:
            return
        self._updating = True
        selection = self.canvas.selection
        for row in self.tree.rows:
            row.setSelected(self._object_id(row) in selection)
        self._updating = False

    def _clicked(self, item: LayerRow, column: int) -> None:
        if not self.canvas.can_edit():
            return
        obj_id = self._object_id(item)
        obj = self.canvas.doc().view.get_live(obj_id)
        if obj is None:
            return
        if column == COL_EYE:
            self.canvas.session.update(obj_id, {"visible": not obj["props"].get("visible", True)})
        elif column == COL_LOCK:
            self.canvas.session.update(obj_id, {"locked": not obj["props"].get("locked", False)})

    def _renamed(self, item: LayerRow) -> None:
        if self._updating:
            return
        name = item.text(COL_NAME).strip()[:200]
        self.canvas.session.update(self._object_id(item), {"name": name})

    def _reordered(self, moved: LayerRow) -> None:
        """Rows run top-first while z runs bottom-up, so 'above' in the list means a higher z."""
        index = self.tree.indexOfTopLevelItem(moved)
        board = self.canvas.doc().view

        def z_at(i: int) -> str | None:
            if 0 <= i < self.tree.topLevelItemCount():
                obj = board.get_live(self._object_id(self.tree.topLevelItem(i)))
                return obj["z"] if obj else None
            return None

        hi, lo = z_at(index - 1), z_at(index + 1)
        if lo is not None and hi is not None and lo >= hi:
            # Equal keys come from concurrent inserts; respace the whole list to separate them.
            self._rebalance()
            return
        self.canvas.session.update(self._object_id(moved), {"z": fractional.between(lo, hi)})
        self.schedule_refresh()

    def _rebalance(self) -> None:
        count = self.tree.topLevelItemCount()
        keys = fractional.spaced_keys(count)
        group = "rebalance"
        for row in range(count):
            obj_id = self._object_id(self.tree.topLevelItem(row))
            self.canvas.session.update(obj_id, {"z": keys[count - 1 - row]}, group=group)
        self.schedule_refresh()
