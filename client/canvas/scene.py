"""Keeps QGraphicsItems in step with the document, touching only what changed."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QRectF
from PyQt6.QtWidgets import QGraphicsScene

from client.canvas.items import BoardItem
from common.model import Board

SCENE_EXTENT = 1_000_000.0


class BoardScene(QGraphicsScene):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setSceneRect(QRectF(-SCENE_EXTENT / 2, -SCENE_EXTENT / 2, SCENE_EXTENT, SCENE_EXTENT))
        self.items_by_id: dict[str, BoardItem] = {}
        self.board = Board()

    def sync(self, board: Board, changed: set[str] | None = None) -> None:
        """Apply document changes. ``None`` means everything may have changed."""
        self.board = board
        ids = set(board.objects) | set(self.items_by_id) if changed is None else changed
        reorder = False
        for obj_id in ids:
            obj = board.objects.get(obj_id)
            item = self.items_by_id.get(obj_id)
            if obj is None or obj["deleted"]:
                if item is not None:
                    self.removeItem(item)
                    del self.items_by_id[obj_id]
                continue
            if item is None:
                item = BoardItem(obj)
                self.items_by_id[obj_id] = item
                self.addItem(item)
                reorder = True
            else:
                reorder = reorder or obj["z"] != item.obj["z"]
                item.sync(obj)
        if reorder:
            self._apply_z_order(board)
        self._apply_frame_clips(board)

    def _apply_z_order(self, board: Board) -> None:
        # Frames paint underneath their contents regardless of fractional z, as containers do.
        ordered = board.live()
        ordered.sort(key=lambda o: 0 if o["type"] == "frame" else 1)
        for rank, obj in enumerate(ordered):
            item = self.items_by_id.get(obj["id"])
            if item is not None:
                item.setZValue(rank)

    def _apply_frame_clips(self, board: Board) -> None:
        for item in self.items_by_id.values():
            parent_id = item.obj.get("parent")
            parent = board.get_live(parent_id) if parent_id else None
            if parent is not None and parent["type"] == "frame":
                p = parent["props"]
                item.clip_rect = QRectF(p.get("x", 0), p.get("y", 0), p.get("w", 0), p.get("h", 0))
            else:
                item.clip_rect = None

    def item_at(self, pos: Any) -> BoardItem | None:
        """Topmost object under a point. A group's members select as the group."""
        for item in self.items(pos):
            if isinstance(item, BoardItem) and item.obj["type"] != "group":
                return item
        return None

    def content_rect(self, ids: set[str] | None = None) -> QRectF:
        rect = QRectF()
        for obj_id, item in self.items_by_id.items():
            if (ids is None or obj_id in ids) and item.isVisible():
                rect = rect.united(item.sceneBoundingRect())
        return rect
