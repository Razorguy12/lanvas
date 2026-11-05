"""Canvas tools: each turns mouse input into document operations."""

from __future__ import annotations

import math
import time
import uuid
from typing import Any

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QPainterPath, QTransform

from client.canvas.items import BoardItem, object_rect
from client.theme import STICKY_COLOR
from common.simplify import simplify

DRAG_INTERVAL = 0.033  # Architecture.md section 9: at most one update per object every 33 ms
PREVIEW_INTERVAL = 0.033
HANDLE_PX = 8.0
ROTATE_ZONE_PX = 18.0
SNAP_PX = 5.0
MIN_SIZE = 2.0
DEFAULT_SHAPE = 100.0
ANGLE_STEP = math.radians(15)
SNAP_NEIGHBOR_LIMIT = 300
HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")
SIZED_TYPES = ("rect", "ellipse", "frame", "text", "sticky", "image", "path", "group")


def object_transform(obj: dict[str, Any]) -> QTransform:
    """Local object space to scene space. Rotation is about the rectangle's center."""
    props = obj["props"]
    rect = object_rect(obj)
    center = rect.center()
    transform = QTransform()
    transform.translate(props.get("x", 0), props.get("y", 0))
    if obj["type"] != "comment":
        transform.translate(center.x(), center.y())
        transform.rotate(props.get("rotation", 0))
        transform.translate(-center.x(), -center.y())
    return transform


def handle_points(rect: QRectF) -> dict[str, QPointF]:
    cx, cy = rect.center().x(), rect.center().y()
    return {
        "nw": rect.topLeft(),
        "n": QPointF(cx, rect.top()),
        "ne": rect.topRight(),
        "e": QPointF(rect.right(), cy),
        "se": rect.bottomRight(),
        "s": QPointF(cx, rect.bottom()),
        "sw": rect.bottomLeft(),
        "w": QPointF(rect.left(), cy),
    }


class Tool:
    """Base tool. ``self.c`` is the CanvasView, which gives access to the session and selection."""

    name = ""
    edits = True
    cursor = Qt.CursorShape.CrossCursor

    def __init__(self, canvas: Any) -> None:
        self.c = canvas

    def press(self, pos: QPointF, event: Any) -> None: ...
    def move(self, pos: QPointF, event: Any) -> None: ...
    def release(self, pos: QPointF, event: Any) -> None: ...
    def double_click(self, pos: QPointF, event: Any) -> None: ...
    def overlay(self, painter: Any) -> None: ...
    def cancel(self) -> None: ...


class SelectTool(Tool):
    name = "select"
    edits = False
    cursor = Qt.CursorShape.ArrowCursor

    def __init__(self, canvas: Any) -> None:
        super().__init__(canvas)
        self.mode: str | None = None
        self.start = QPointF()
        self.last_send = 0.0
        self.group = ""
        self.origin: dict[str, dict[str, Any]] = {}
        self.handle = ""
        self.marquee: QRectF | None = None
        self.base_rect = QRectF()
        self.pending_patch: dict[str, dict[str, Any]] = {}
        self.guides: list[tuple[QPointF, QPointF]] = []
        self.additive = False

    # ------------------------------------------------------------------ hit tests
    def _handle_at(self, pos: QPointF) -> tuple[str, str] | None:
        """Which resize handle or rotation zone is under the pointer, tested in local space."""
        if not self.c.can_edit():
            return None
        if self.c.selection_is_single_rotatable():
            obj = self.c.single_selected()
            inverse, ok = object_transform(obj).inverted()
            if not ok:
                return None
            rect = object_rect(obj)
            local = inverse.map(pos)
        else:
            rect = self.c.selection_rect()
            local = pos
            if rect.isNull():
                return None
        scale = self.c.scale_factor()
        tolerance = HANDLE_PX / scale
        points = handle_points(rect)
        for name, point in points.items():
            if abs(local.x() - point.x()) <= tolerance and abs(local.y() - point.y()) <= tolerance:
                return ("resize", name)
        zone = ROTATE_ZONE_PX / scale
        if self.c.selection_is_single_rotatable():
            for name in ("nw", "ne", "se", "sw"):
                point = points[name]
                dx, dy = local.x() - point.x(), local.y() - point.y()
                if math.hypot(dx, dy) <= zone and not rect.contains(local):
                    return ("rotate", name)
        return None

    def hover_cursor(self, pos: QPointF) -> Qt.CursorShape:
        hit = self._handle_at(pos)
        if hit is None:
            return Qt.CursorShape.ArrowCursor
        if hit[0] == "rotate":
            return Qt.CursorShape.CrossCursor
        return {
            "n": Qt.CursorShape.SizeVerCursor,
            "s": Qt.CursorShape.SizeVerCursor,
            "e": Qt.CursorShape.SizeHorCursor,
            "w": Qt.CursorShape.SizeHorCursor,
            "nw": Qt.CursorShape.SizeFDiagCursor,
            "se": Qt.CursorShape.SizeFDiagCursor,
            "ne": Qt.CursorShape.SizeBDiagCursor,
            "sw": Qt.CursorShape.SizeBDiagCursor,
        }[hit[1]]

    # ------------------------------------------------------------------ mouse
    def press(self, pos: QPointF, event: Any) -> None:
        self.start = pos
        self.group = uuid.uuid4().hex[:8]
        self.additive = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        hit = self._handle_at(pos)
        if hit is not None:
            self.mode, self.handle = hit
            self._capture_origin()
            self.base_rect = self.c.selection_rect()
            return
        item = self.c.board_scene.item_at(pos)
        if item is None:
            if not self.additive:
                self.c.set_selection(set())
            self.mode = "marquee"
            self.marquee = QRectF(pos, pos)
            return
        target = self.c.selection_root(item.obj_id)
        if self.additive:
            self.c.toggle_selected(target)
        elif target not in self.c.selection:
            self.c.set_selection({target})
        self.mode = "move" if self.c.can_edit() else None
        self._capture_origin()

    def _capture_origin(self) -> None:
        self.origin = {}
        for obj_id in self.c.selection_with_descendants():
            obj = self.c.doc().view.get_live(obj_id)
            if obj is not None and not obj["props"].get("locked"):
                self.origin[obj_id] = {
                    "type": obj["type"],
                    **{
                        k: obj["props"].get(k) for k in ("x", "y", "w", "h", "x2", "y2", "rotation")
                    },
                }

    def move(self, pos: QPointF, event: Any) -> None:
        if self.mode == "marquee" and self.marquee is not None:
            self.marquee = QRectF(self.start, pos).normalized()
            self.c.viewport().update()
        elif self.mode == "move":
            self._drag_move(pos, event)
        elif self.mode == "resize":
            self._drag_resize(pos, event)
        elif self.mode == "rotate":
            self._drag_rotate(pos, event)

    def _flush(self, force: bool = False) -> None:
        """Send the newest patch per object at most every 33 ms. Older values just get replaced."""
        now = time.monotonic()
        if not force and now - self.last_send < DRAG_INTERVAL:
            return
        self.last_send = now
        for obj_id, patch in self.pending_patch.items():
            self.c.session.update(obj_id, patch, group=self.group)
        self.pending_patch = {}

    def _drag_move(self, pos: QPointF, event: Any) -> None:
        dx, dy = pos.x() - self.start.x(), pos.y() - self.start.y()
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            if abs(dx) > abs(dy):
                dy = 0
            else:
                dx = 0
        dx, dy = self._snap(dx, dy)
        for obj_id, origin in self.origin.items():
            self.pending_patch[obj_id] = {"x": origin["x"] + dx, "y": origin["y"] + dy}
        self._flush()

    def _snap(self, dx: float, dy: float) -> tuple[float, float]:
        """Magenta guides: pull the moving box onto neighbours' edges and centers."""
        self.guides = []
        rect = self.c.origin_rect(self.origin).translated(dx, dy)
        if rect.isNull():
            return dx, dy
        scene = self.c.board_scene
        tolerance = SNAP_PX / self.c.scale_factor()
        moving = set(self.origin)
        others = [i for k, i in scene.items_by_id.items() if k not in moving and i.isVisible()]
        if len(others) > SNAP_NEIGHBOR_LIMIT:
            return dx, dy  # guides are a convenience; they must not make dragging laggy
        best_x: tuple[float, float] | None = None
        best_y: tuple[float, float] | None = None
        ours_x = (rect.left(), rect.center().x(), rect.right())
        ours_y = (rect.top(), rect.center().y(), rect.bottom())
        for item in others:
            box = item.sceneBoundingRect()
            for theirs in (box.left(), box.center().x(), box.right()):
                for mine in ours_x:
                    delta = theirs - mine
                    if abs(delta) <= tolerance and (best_x is None or abs(delta) < abs(best_x[0])):
                        best_x = (delta, theirs)
            for theirs in (box.top(), box.center().y(), box.bottom()):
                for mine in ours_y:
                    delta = theirs - mine
                    if abs(delta) <= tolerance and (best_y is None or abs(delta) < abs(best_y[0])):
                        best_y = (delta, theirs)
        if best_x is not None:
            dx += best_x[0]
            self.guides.append(
                (QPointF(best_x[1], rect.top() - 2000), QPointF(best_x[1], rect.bottom() + 2000))
            )
        if best_y is not None:
            dy += best_y[0]
            self.guides.append(
                (QPointF(rect.left() - 2000, best_y[1]), QPointF(rect.right() + 2000, best_y[1]))
            )
        self.c.viewport().update()
        return dx, dy

    def _drag_resize(self, pos: QPointF, event: Any) -> None:
        keep_ratio = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if self.c.selection_is_single_rotatable():
            self._resize_single(pos, keep_ratio)
        else:
            self._resize_multi(pos, keep_ratio)
        self._flush()

    def _resized_rect(self, base: QRectF, local: QPointF, keep_ratio: bool) -> QRectF:
        left, top, right, bottom = base.left(), base.top(), base.right(), base.bottom()
        if "w" in self.handle:
            left = local.x()
        if "e" in self.handle:
            right = local.x()
        if "n" in self.handle:
            top = local.y()
        if "s" in self.handle:
            bottom = local.y()
        if right - left < MIN_SIZE:
            if "w" in self.handle:
                left = right - MIN_SIZE
            else:
                right = left + MIN_SIZE
        if bottom - top < MIN_SIZE:
            if "n" in self.handle:
                top = bottom - MIN_SIZE
            else:
                bottom = top + MIN_SIZE
        rect = QRectF(QPointF(left, top), QPointF(right, bottom))
        if keep_ratio and base.height() > 0 and len(self.handle) == 2:
            ratio = base.width() / base.height()
            rect.setHeight(rect.width() / ratio)
            if "n" in self.handle:
                rect.moveBottom(base.bottom())
        return rect

    def _resize_single(self, pos: QPointF, keep_ratio: bool) -> None:
        obj_id = next(iter(self.c.selection))
        origin = self.origin.get(obj_id)
        if origin is None:
            return
        model = {
            "type": origin["type"],
            "props": {
                k: origin[k] for k in ("x", "y", "w", "h", "rotation") if origin[k] is not None
            },
        }
        transform = object_transform(model)
        inverse, ok = transform.inverted()
        if not ok:
            return
        base = object_rect(model)
        rect = self._resized_rect(base, inverse.map(pos), keep_ratio)
        # The opposite corner must stay put on screen even though rotation pivots on the center,
        # so the new center is the old transform's image of the new local center.
        center = transform.map(rect.center())
        self.pending_patch[obj_id] = {
            "x": center.x() - rect.width() / 2,
            "y": center.y() - rect.height() / 2,
            "w": rect.width(),
            "h": rect.height(),
        }

    def _resize_multi(self, pos: QPointF, keep_ratio: bool) -> None:
        base = self.base_rect
        if base.width() <= 0 and base.height() <= 0:
            return
        rect = self._resized_rect(base, pos, keep_ratio)
        sx = rect.width() / base.width() if base.width() else 1.0
        sy = rect.height() / base.height() if base.height() else 1.0
        for obj_id, origin in self.origin.items():
            patch: dict[str, Any] = {
                "x": rect.left() + (origin["x"] - base.left()) * sx,
                "y": rect.top() + (origin["y"] - base.top()) * sy,
            }
            if origin["type"] in SIZED_TYPES:
                patch["w"] = max(MIN_SIZE, (origin["w"] or 0) * sx)
                patch["h"] = max(MIN_SIZE, (origin["h"] or 0) * sy)
            elif origin["type"] in ("line", "arrow"):
                patch["x2"] = (origin["x2"] or 0) * sx
                patch["y2"] = (origin["y2"] or 0) * sy
            self.pending_patch[obj_id] = patch

    def _drag_rotate(self, pos: QPointF, event: Any) -> None:
        obj_id = next(iter(self.c.selection))
        origin = self.origin.get(obj_id)
        if origin is None:
            return
        center = QPointF(origin["x"] + (origin["w"] or 0) / 2, origin["y"] + (origin["h"] or 0) / 2)
        begin = math.atan2(self.start.y() - center.y(), self.start.x() - center.x())
        now = math.atan2(pos.y() - center.y(), pos.x() - center.x())
        angle = (origin["rotation"] or 0) + math.degrees(now - begin)
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            angle = round(angle / 15) * 15
        self.pending_patch[obj_id] = {"rotation": angle % 360}
        self._flush()

    def release(self, pos: QPointF, event: Any) -> None:
        if self.mode == "marquee" and self.marquee is not None:
            rect = self.marquee
            hits = set()
            if rect.width() > 1 or rect.height() > 1:
                for item in self.c.board_scene.items(rect):
                    if isinstance(item, BoardItem):
                        hits.add(self.c.selection_root(item.obj_id))
            if self.additive:
                self.c.set_selection(self.c.selection | hits)
            else:
                self.c.set_selection(hits)
            self.marquee = None
        else:
            self._flush(force=True)
            # A group's stored box has to follow its members, or its outline would be stale.
            if self.mode in ("move", "resize"):
                self.c.refresh_group_bounds(self.group)
        self.mode = None
        self.guides = []
        self.c.viewport().update()

    def double_click(self, pos: QPointF, event: Any) -> None:
        item = self.c.board_scene.item_at(pos)
        if item is not None and item.obj["type"] in ("text", "sticky") and self.c.can_edit():
            self.c.edit_text(item.obj_id)
        elif item is not None and item.obj["type"] == "comment":
            self.c.open_comment(item.obj_id)

    def overlay(self, painter: Any) -> None:
        if self.marquee is not None:
            self.c.paint_marquee(painter, self.marquee)
        for start, end in self.guides:
            self.c.paint_guide(painter, start, end)

    def cancel(self) -> None:
        self.mode = None
        self.marquee = None
        self.guides = []
        self.pending_patch = {}


class HandTool(Tool):
    name = "hand"
    edits = False
    cursor = Qt.CursorShape.OpenHandCursor


class PenTool(Tool):
    name = "pen"

    def __init__(self, canvas: Any) -> None:
        super().__init__(canvas)
        self.points: list[float] = []
        self.sent = 0
        self.last_send = 0.0
        self.stroke_id = ""

    def press(self, pos: QPointF, event: Any) -> None:
        self.points = [pos.x(), pos.y()]
        self.sent = 0
        self.stroke_id = uuid.uuid4().hex[:10]

    def move(self, pos: QPointF, event: Any) -> None:
        if not self.points:
            return
        self.points += [pos.x(), pos.y()]
        self.c.viewport().update()
        now = time.monotonic()
        if now - self.last_send >= PREVIEW_INTERVAL:
            self.last_send = now
            self._send_preview()

    def _send_preview(self) -> None:
        """Only new points travel. A lost or dropped batch just leaves a brief preview gap."""
        fresh = self.points[self.sent :][:400]
        if fresh:
            start = max(0, self.sent - 2)
            self.c.session.send_transient(
                "stroke_preview",
                {
                    "id": self.stroke_id,
                    "points": self.points[start : self.sent + len(fresh)],
                    "color": self.c.stroke_color,
                    "w": self.c.stroke_width,
                },
            )
            self.sent += len(fresh)

    def release(self, pos: QPointF, event: Any) -> None:
        if not self.points:
            return
        points = simplify(self.points, tolerance=1.0 / max(self.c.scale_factor(), 0.25))
        xs, ys = points[0::2], points[1::2]
        x0, y0 = min(xs), min(ys)
        relative: list[float] = []
        for i in range(0, len(points), 2):
            relative += [round(points[i] - x0, 2), round(points[i + 1] - y0, 2)]
        self.c.session.create(
            "path",
            {
                "x": x0,
                "y": y0,
                "w": max(xs) - x0,
                "h": max(ys) - y0,
                "points": relative,
                "stroke": self.c.stroke_color,
                "stroke_w": self.c.stroke_width,
                "name": "Stroke",
            },
            parent=self.c.frame_at(QPointF(x0, y0)),
        )
        self.c.session.send_transient("stroke_end", {"id": self.stroke_id})
        self.points = []
        self.c.viewport().update()

    def overlay(self, painter: Any) -> None:
        if len(self.points) >= 4:
            self.c.paint_stroke(painter, self.points, self.c.stroke_color, self.c.stroke_width)

    def cancel(self) -> None:
        self.points = []


class EraserTool(Tool):
    """Object-level erase: a stroke goes as a whole, for everyone (Architecture.md section 5.1)."""

    name = "eraser"
    TOLERANCE = 6.0

    def __init__(self, canvas: Any) -> None:
        super().__init__(canvas)
        self.active = False
        self.group = ""

    def press(self, pos: QPointF, event: Any) -> None:
        self.active = True
        self.group = uuid.uuid4().hex[:8]
        self._erase(pos)

    def move(self, pos: QPointF, event: Any) -> None:
        if self.active:
            self._erase(pos)

    def release(self, pos: QPointF, event: Any) -> None:
        self.active = False

    def _erase(self, pos: QPointF) -> None:
        radius = self.TOLERANCE / self.c.scale_factor()
        area = QPainterPath()
        area.addEllipse(pos, radius, radius)
        for item in self.c.board_scene.items(area):
            if (
                isinstance(item, BoardItem)
                and item.obj["type"] == "path"
                and not item.obj["props"].get("locked")
            ):
                self.c.session.delete(item.obj_id, group=self.group)


class ShapeTool(Tool):
    """Rectangle, ellipse and frame: drag out a box, or click for a default size."""

    def __init__(self, canvas: Any, obj_type: str) -> None:
        super().__init__(canvas)
        self.obj_type = obj_type
        self.name = obj_type
        self.start: QPointF | None = None
        self.current = QPointF()
        self.shift = False

    def press(self, pos: QPointF, event: Any) -> None:
        self.start = pos
        self.current = pos

    def move(self, pos: QPointF, event: Any) -> None:
        if self.start is not None:
            self.current = pos
            self.shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.c.viewport().update()

    def _rect(self) -> QRectF:
        assert self.start is not None
        dx, dy = self.current.x() - self.start.x(), self.current.y() - self.start.y()
        if self.shift:
            side = max(abs(dx), abs(dy))
            dx, dy = math.copysign(side, dx), math.copysign(side, dy)
        return QRectF(self.start, QPointF(self.start.x() + dx, self.start.y() + dy)).normalized()

    def release(self, pos: QPointF, event: Any) -> None:
        if self.start is None:
            return
        rect = self._rect()
        if rect.width() < 3 and rect.height() < 3:
            rect = QRectF(self.start.x(), self.start.y(), DEFAULT_SHAPE, DEFAULT_SHAPE)
        self.start = None
        if self.obj_type == "frame":
            props = {"fill": "#FFFFFF", "stroke": "#B3B3B3", "stroke_w": 1}
            z = self.c.bottom_z()
            parent = None
        else:
            props = {
                "fill": self.c.fill_color,
                "stroke": self.c.stroke_color,
                "stroke_w": self.c.stroke_width,
            }
            z = None
            parent = self.c.frame_at(rect.center())
        name = self.c.next_name(self.obj_type)
        op = self.c.session.create(
            self.obj_type,
            {
                "x": rect.x(),
                "y": rect.y(),
                "w": rect.width(),
                "h": rect.height(),
                "name": name,
                **props,
            },
            parent=parent,
            z=z,
        )
        if op:
            self.c.set_selection({op["id"]})
        self.c.select_tool_after_create()

    def overlay(self, painter: Any) -> None:
        if self.start is not None:
            self.c.paint_shape_preview(painter, self.obj_type, self._rect())

    def cancel(self) -> None:
        self.start = None


class LineTool(Tool):
    def __init__(self, canvas: Any, obj_type: str) -> None:
        super().__init__(canvas)
        self.obj_type = obj_type
        self.name = obj_type
        self.start: QPointF | None = None
        self.end = QPointF()

    def press(self, pos: QPointF, event: Any) -> None:
        self.start = pos
        self.end = pos

    def move(self, pos: QPointF, event: Any) -> None:
        if self.start is None:
            return
        self.end = pos
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            dx, dy = pos.x() - self.start.x(), pos.y() - self.start.y()
            length = math.hypot(dx, dy)
            angle = round(math.atan2(dy, dx) / ANGLE_STEP) * ANGLE_STEP
            self.end = QPointF(
                self.start.x() + length * math.cos(angle),
                self.start.y() + length * math.sin(angle),
            )
        self.c.viewport().update()

    def release(self, pos: QPointF, event: Any) -> None:
        if self.start is None:
            return
        dx, dy = self.end.x() - self.start.x(), self.end.y() - self.start.y()
        if math.hypot(dx, dy) < 3:
            dx, dy = DEFAULT_SHAPE, 0.0
        op = self.c.session.create(
            self.obj_type,
            {
                "x": self.start.x(),
                "y": self.start.y(),
                "x2": dx,
                "y2": dy,
                "stroke": self.c.stroke_color,
                "stroke_w": self.c.stroke_width,
                "name": self.c.next_name(self.obj_type),
            },
            parent=self.c.frame_at(self.start),
        )
        self.start = None
        if op:
            self.c.set_selection({op["id"]})
        self.c.select_tool_after_create()

    def overlay(self, painter: Any) -> None:
        if self.start is not None:
            self.c.paint_line_preview(painter, self.start, self.end, self.obj_type == "arrow")

    def cancel(self) -> None:
        self.start = None


class TextTool(Tool):
    name = "text"
    cursor = Qt.CursorShape.IBeamCursor

    def press(self, pos: QPointF, event: Any) -> None:
        op = self.c.session.create(
            "text",
            {
                "x": pos.x(),
                "y": pos.y(),
                "w": 240,
                "h": 40,
                "text": "",
                "font_size": 20,
                "fill": self.c.stroke_color,
                "name": self.c.next_name("text"),
            },
            parent=self.c.frame_at(pos),
        )
        if op:
            self.c.set_selection({op["id"]})
            self.c.edit_text(op["id"], new_object=True)
        self.c.select_tool_after_create()


class StickyTool(Tool):
    name = "sticky"

    def press(self, pos: QPointF, event: Any) -> None:
        op = self.c.session.create(
            "sticky",
            {
                "x": pos.x() - 80,
                "y": pos.y() - 80,
                "w": 160,
                "h": 160,
                "text": "",
                "font_size": 14,
                "fill": STICKY_COLOR,
                "name": self.c.next_name("sticky"),
            },
            parent=self.c.frame_at(pos),
        )
        if op:
            self.c.set_selection({op["id"]})
            self.c.edit_text(op["id"], new_object=True)
        self.c.select_tool_after_create()


class CommentTool(Tool):
    name = "comment"

    def press(self, pos: QPointF, event: Any) -> None:
        self.c.create_comment(pos)
        self.c.select_tool_after_create()
