"""Layers panel: a flat, top-first list. Dragging a row writes a new fractional z for that
object."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QAbstractItemView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from client import icons, theme
from client.widgets import section_label
from common import fractional

COL_NAME, COL_EYE, COL_LOCK = 0, 1, 2
REFRESH_MS = 120
ICON_FOR = {"path": "pen", "group": "users"}


class _LayerTree(QTreeWidget):
    def __init__(self, on_reorder: Any) -> None:
        super().__init__()
        self.on_reorder = on_reorder

    def dropEvent(self, event: Any) -> None:  # noqa: N802 (Qt override)
        moved = self.currentItem()
        super().dropEvent(event)
        if moved is not None:
            self.on_reorder(moved)


class LayersPanel(QWidget):
    def __init__(self, canvas: Any) -> None:
        super().__init__()
        self.canvas = canvas
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 0, 0)
        layout.addWidget(section_label("Layers"))
        self.tree = _LayerTree(self._reordered)
        self.tree.setColumnCount(3)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setColumnWidth(COL_EYE, 22)
        self.tree.setColumnWidth(COL_LOCK, 22)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_NAME, header.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_EYE, header.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_LOCK, header.ResizeMode.Fixed)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.itemSelectionChanged.connect(self._selection_from_tree)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemChanged.connect(self._renamed)
        layout.addWidget(self.tree)
        self._updating = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        canvas.selection_changed.connect(self._selection_to_tree)

    def schedule_refresh(self) -> None:
        """Debounced: a burst of ops must not rebuild a thousand rows once per op."""
        if not self._timer.isActive():
            self._timer.start()

    def refresh(self) -> None:
        self._updating = True
        scroll = self.tree.verticalScrollBar().value()
        self.tree.clear()
        colors = self.canvas.remote_selection_colors()
        muted = theme.current().text_muted
        rows = []
        depth: dict[str, int] = {}
        board = self.canvas.doc().view
        for obj in reversed(board.live()):
            level = 0
            parent = obj.get("parent")
            while parent and level < 8:
                level += 1
                parent_obj = board.get_live(parent)
                parent = parent_obj.get("parent") if parent_obj else None
            depth[obj["id"]] = level
            props = obj["props"]
            item = QTreeWidgetItem()
            name = props.get("name") or obj["type"].capitalize()
            item.setText(COL_NAME, "   " * level + name)
            item.setData(COL_NAME, Qt.ItemDataRole.UserRole, obj["id"])
            item.setIcon(COL_NAME, icons.icon(ICON_FOR.get(obj["type"], obj["type"]), muted))
            if obj["id"] in colors:
                item.setForeground(COL_NAME, QColor(colors[obj["id"]]))
                item.setToolTip(COL_NAME, "Someone else has this selected")
            visible = props.get("visible", True)
            item.setIcon(COL_EYE, icons.icon("eye" if visible else "eyeoff", muted))
            item.setToolTip(COL_EYE, "Hide" if visible else "Show")
            locked = props.get("locked", False)
            item.setIcon(COL_LOCK, icons.icon("lock" if locked else "unlock", muted))
            item.setToolTip(COL_LOCK, "Unlock" if locked else "Lock")
            flags = (
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
            if self.canvas.can_edit():
                flags |= Qt.ItemFlag.ItemIsEditable
            item.setFlags(flags)  # not drop-enabled, so rows reorder but never nest
            rows.append(item)
        self.tree.addTopLevelItems(rows)
        self.tree.verticalScrollBar().setValue(scroll)
        self._updating = False
        self._selection_to_tree()

    def _object_id(self, item: QTreeWidgetItem) -> str:
        return item.data(COL_NAME, Qt.ItemDataRole.UserRole)

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
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            item.setSelected(self._object_id(item) in selection)
        self._updating = False

    def _clicked(self, item: QTreeWidgetItem, column: int) -> None:
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

    def _renamed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating or column != COL_NAME:
            return
        name = item.text(COL_NAME).strip()[:200]
        self.canvas.session.update(self._object_id(item), {"name": name})

    def _reordered(self, moved: QTreeWidgetItem) -> None:
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
