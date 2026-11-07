"""Keeps BoardItems in step with the document, touching only what changed."""

from __future__ import annotations

from client.canvas.items import BoardItem
from client.geom import Point, Rect
from common.model import Board


class BoardScene:
    def __init__(self, parent: object = None) -> None:
        self.items_by_id: dict[str, BoardItem] = {}
        self.board = Board()
        self._ordered: list[BoardItem] | None = None
        self.version = 0  # bumped on every change, so views know when to repaint

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
                    del self.items_by_id[obj_id]
                    reorder = True
                continue
            if item is None:
                self.items_by_id[obj_id] = BoardItem(obj)
                reorder = True
            else:
                reorder = reorder or obj["z"] != item.obj["z"]
                item.sync(obj)
        if reorder:
            self._apply_z_order(board)
        self._apply_frame_clips(board)
        self.version += 1

    def _apply_z_order(self, board: Board) -> None:
        # Frames paint underneath their contents regardless of fractional z, as containers do.
        ordered = board.live()
        ordered.sort(key=lambda o: 0 if o["type"] == "frame" else 1)
        for rank, obj in enumerate(ordered):
            item = self.items_by_id.get(obj["id"])
            if item is not None:
                item.setZValue(rank)
        self._ordered = None

    def _apply_frame_clips(self, board: Board) -> None:
        for item in self.items_by_id.values():
            parent_id = item.obj.get("parent")
            parent = board.get_live(parent_id) if parent_id else None
            if parent is not None and parent["type"] == "frame":
                p = parent["props"]
                item.clip_rect = Rect(p.get("x", 0), p.get("y", 0), p.get("w", 0), p.get("h", 0))
            else:
                item.clip_rect = None

    def ordered(self) -> list[BoardItem]:
        """Every item, bottom first: paint order."""
        if self._ordered is None or len(self._ordered) != len(self.items_by_id):
            self._ordered = sorted(self.items_by_id.values(), key=lambda i: i.zValue())
        return self._ordered

    def items(self, where: Point | Rect, radius: float = 0.0) -> list[BoardItem]:
        """Items whose shape touches a point (optionally within ``radius``) or a rectangle,
        topmost first, as QGraphicsScene.items does."""
        if isinstance(where, Rect):
            hits = [i for i in self.ordered() if i.intersects(where)]
        else:
            hits = [i for i in self.ordered() if i.contains(where, radius)]
        hits.reverse()
        return hits

    def item_at(self, pos: Point) -> BoardItem | None:
        """Topmost object under a point. A group's members select as the group."""
        for item in self.items(pos):
            if item.obj["type"] != "group":
                return item
        return None

    def content_rect(self, ids: set[str] | None = None) -> Rect:
        rect = Rect()
        for obj_id, item in self.items_by_id.items():
            if (ids is None or obj_id in ids) and item.isVisible():
                rect = rect.united(item.sceneBoundingRect())
        return rect
