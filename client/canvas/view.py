"""The canvas widget: zoom, pan, tool dispatch, and everything drawn over the document.

Selection handles, remote cursors, guides and previews are painted in ``drawForeground``, in the
view
and never in the scene. Export paints scene items only, so none of these can ever reach an exported
file (Design.md section 12).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QTransform,
)
from PyQt6.QtWidgets import QGraphicsView, QInputDialog, QPlainTextEdit

from client import theme
from client.canvas.items import flat_to_path, object_rect, text_font
from client.canvas.scene import BoardScene
from client.canvas.tools import (
    CommentTool,
    EraserTool,
    HandTool,
    LineTool,
    PenTool,
    SelectTool,
    ShapeTool,
    StickyTool,
    TextTool,
    Tool,
    handle_points,
    object_transform,
)
from common import fractional

MIN_ZOOM = 0.05
MAX_ZOOM = 16.0
CURSOR_INTERVAL = 0.05  # about 20 Hz (Architecture.md section 9)
STALE_CURSOR = 15.0
ZOOM_STEP = 1.15


class CanvasView(QGraphicsView):
    selection_changed = pyqtSignal()
    zoom_changed = pyqtSignal(float)
    tool_changed = pyqtSignal(str)
    comment_requested = pyqtSignal(str)

    def __init__(self, session: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.session = session
        self.board_scene = BoardScene(self)
        self.setScene(self.board_scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setBackgroundBrush(QBrush(QColor(theme.current().bg_canvas)))
        self.selection: set[str] = set()
        self.stroke_color = "#1E1E1E"
        self.fill_color: str | None = "#D9D9D9"
        self.stroke_width = 2.0
        self.tools: dict[str, Tool] = {
            "select": SelectTool(self),
            "hand": HandTool(self),
            "pen": PenTool(self),
            "eraser": EraserTool(self),
            "rect": ShapeTool(self, "rect"),
            "ellipse": ShapeTool(self, "ellipse"),
            "frame": ShapeTool(self, "frame"),
            "line": LineTool(self, "line"),
            "arrow": LineTool(self, "arrow"),
            "text": TextTool(self),
            "sticky": StickyTool(self),
            "comment": CommentTool(self),
        }
        self.tool: Tool = self.tools["select"]
        self.remote_cursors: dict[str, dict[str, Any]] = {}
        self.remote_selections: dict[str, dict[str, Any]] = {}
        self.remote_strokes: dict[str, dict[str, Any]] = {}
        self._panning = False
        self._space_down = False
        self._pan_anchor = QPointF()
        self._press_tool: Tool | None = None
        self._last_cursor_send = 0.0
        self._editor: QPlainTextEdit | None = None
        self._editing: tuple[str, bool] | None = None
        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(2000)
        self._stale_timer.timeout.connect(self._drop_stale_presence)
        self._stale_timer.start()
        self.translate(self.viewport().width() / 2, self.viewport().height() / 2)

    # ------------------------------------------------------------------ session helpers
    def doc(self) -> Any:
        return self.session.doc

    def can_edit(self) -> bool:
        if self.session.role == "owner":
            return True
        return self.session.role == "editor" and not self.session.settings.get("locked")

    def scale_factor(self) -> float:
        return self.transform().m11()

    def sync(self, changed: set[str] | None = None) -> None:
        self.board_scene.sync(self.doc().view, changed)
        gone = {i for i in self.selection if self.doc().view.get_live(i) is None}
        if gone:
            self.selection -= gone
            self.selection_changed.emit()
        self.viewport().update()

    # ------------------------------------------------------------------ tools
    def set_tool(self, name: str) -> None:
        if self.tools[name].edits and not self.can_edit():
            self.session.emit("toast", text="Viewers can't edit. Ask the owner.")
            return
        self.tool.cancel()
        self.tool = self.tools[name]
        self.setCursor(self.tool.cursor)
        self.tool_changed.emit(name)
        self.viewport().update()

    def select_tool_after_create(self) -> None:
        self.set_tool("select")

    def next_name(self, kind: str) -> str:
        count = sum(1 for o in self.doc().view.objects.values() if o["type"] == kind) + 1
        return f"{kind.capitalize()} {count}"

    def bottom_z(self) -> str:
        keys = [o["z"] for o in self.doc().view.objects.values() if not o["deleted"]]
        return fractional.between(None, min(keys)) if keys else fractional.between(None, None)

    def frame_at(self, pos: QPointF) -> str | None:
        """The innermost frame under a point. New objects become its children and move with it."""
        for item in self.board_scene.items(pos):
            obj = getattr(item, "obj", None)
            if obj is not None and obj["type"] == "frame":
                return obj["id"]
        return None

    # ------------------------------------------------------------------ selection model
    def set_selection(self, ids: set[str]) -> None:
        if ids == self.selection:
            return
        self.selection = set(ids)
        self.session.send_transient("selection", {"ids": sorted(self.selection)[:200]})
        self.selection_changed.emit()
        self.viewport().update()

    def toggle_selected(self, obj_id: str) -> None:
        self.set_selection(self.selection ^ {obj_id})

    def selection_root(self, obj_id: str) -> str:
        """Clicking a group member selects the outermost group, as in Figma."""
        board = self.doc().view
        current = obj_id
        while True:
            obj = board.get_live(current)
            parent = board.get_live(obj["parent"]) if obj and obj.get("parent") else None
            if parent is None or parent["type"] != "group":
                return current
            current = parent["id"]

    def descendants(self, obj_id: str) -> set[str]:
        found: set[str] = set()
        frontier = [obj_id]
        objects = self.doc().view.live()
        while frontier:
            current = frontier.pop()
            for obj in objects:
                if obj.get("parent") == current and obj["id"] not in found:
                    found.add(obj["id"])
                    frontier.append(obj["id"])
        return found

    def selection_with_descendants(self) -> set[str]:
        ids = set(self.selection)
        for obj_id in self.selection:
            ids |= self.descendants(obj_id)
        return ids

    def single_selected(self) -> dict[str, Any] | None:
        if len(self.selection) != 1:
            return None
        return self.doc().view.get_live(next(iter(self.selection)))

    def selection_is_single_rotatable(self) -> bool:
        obj = self.single_selected()
        return obj is not None and obj["type"] in (
            "rect",
            "ellipse",
            "frame",
            "text",
            "sticky",
            "image",
            "path",
        )

    def selection_rect(self) -> QRectF:
        return self.board_scene.content_rect(self.selection) if self.selection else QRectF()

    def origin_rect(self, origin: dict[str, dict[str, Any]]) -> QRectF:
        """Bounding box of objects at their drag-start positions, without any stroke margin."""
        rect = QRectF()
        for obj_id in self.selection:
            data = origin.get(obj_id)
            if data is None:
                continue
            if data["type"] in ("line", "arrow"):
                box = QRectF(
                    data["x"], data["y"], data.get("x2") or 0, data.get("y2") or 0
                ).normalized()
            else:
                box = QRectF(data["x"], data["y"], data.get("w") or 0, data.get("h") or 0)
            rect = rect.united(box) if not rect.isNull() else box
        return rect

    def refresh_group_bounds(self, undo_group: str) -> None:
        board = self.doc().view
        for obj_id in self.selection:
            obj = board.get_live(obj_id)
            if obj is not None and obj["type"] == "group":
                members = self.descendants(obj_id)
                rect = self.board_scene.content_rect(members)
                if not rect.isNull():
                    self.session.update(
                        obj_id,
                        {"x": rect.x(), "y": rect.y(), "w": rect.width(), "h": rect.height()},
                        group=undo_group,
                    )

    # ------------------------------------------------------------------ commands
    def delete_selected(self) -> None:
        if not self.can_edit():
            return
        group = uuid.uuid4().hex[:8]
        for obj_id in sorted(self.selection_with_descendants()):
            obj = self.doc().view.get_live(obj_id)
            if obj is not None and not obj["props"].get("locked"):
                self.session.delete(obj_id, group=group)

    def duplicate_selected(self) -> None:
        if not self.can_edit():
            return
        fresh = set()
        for obj_id in sorted(self.selection):
            obj = self.doc().view.get_live(obj_id)
            if obj is None:
                continue
            props = dict(obj["props"])
            props["x"] = props.get("x", 0) + 20
            props["y"] = props.get("y", 0) + 20
            op = self.session.create(obj["type"], props, parent=obj.get("parent"))
            if op:
                fresh.add(op["id"])
        if fresh:
            self.set_selection(fresh)

    def group_selected(self) -> None:
        if not self.can_edit() or len(self.selection) < 2:
            return
        rect = self.selection_rect()
        op = self.session.create(
            "group",
            {
                "x": rect.x(),
                "y": rect.y(),
                "w": rect.width(),
                "h": rect.height(),
                "name": self.next_name("group"),
            },
        )
        if op:
            token = uuid.uuid4().hex[:8]
            for obj_id in sorted(self.selection):
                self.session.update(obj_id, {"parent": op["id"]}, group=token)
            self.set_selection({op["id"]})

    def change_z(self, forward: bool) -> None:
        """Bring forward or send back: move one step past the neighbor in the z-sorted list."""
        if not self.can_edit():
            return
        ordered = self.doc().view.live()
        for obj_id in sorted(self.selection, key=lambda i: keys_index(ordered, i), reverse=forward):
            index = keys_index(ordered, obj_id)
            target = index + (1 if forward else -1)
            if index < 0 or not 0 <= target < len(ordered):
                continue
            if forward:
                lo = ordered[target]["z"]
                hi = ordered[target + 1]["z"] if target + 1 < len(ordered) else None
            else:
                lo = ordered[target - 1]["z"] if target > 0 else None
                hi = ordered[target]["z"]
            if lo == hi or (lo is not None and hi is not None and lo > hi):
                continue
            self.session.update(obj_id, {"z": fractional.between(lo, hi)})
            ordered = self.doc().view.live()

    def align(self, mode: str) -> None:
        if not self.can_edit() or len(self.selection) < 2:
            return
        box = self.selection_rect()
        token = uuid.uuid4().hex[:8]
        for obj_id in sorted(self.selection):
            item = self.board_scene.items_by_id.get(obj_id)
            if item is None or item.obj["props"].get("locked"):
                continue
            rect = item.sceneBoundingRect()
            dx = dy = 0.0
            if mode == "left":
                dx = box.left() - rect.left()
            elif mode == "right":
                dx = box.right() - rect.right()
            elif mode == "top":
                dy = box.top() - rect.top()
            elif mode == "bottom":
                dy = box.bottom() - rect.bottom()
            elif mode == "hcenter":
                dx = box.center().x() - rect.center().x()
            elif mode == "vcenter":
                dy = box.center().y() - rect.center().y()
            if dx or dy:
                props = item.obj["props"]
                self.session.update(
                    obj_id,
                    {"x": props.get("x", 0) + dx, "y": props.get("y", 0) + dy},
                    group=token,
                )

    def zoom_to(self, factor: float, anchor: QPointF | None = None) -> None:
        factor = max(MIN_ZOOM, min(MAX_ZOOM, factor))
        anchor = anchor if anchor is not None else QPointF(self.viewport().rect().center())
        before = self.mapToScene(anchor.toPoint())
        transform = QTransform()
        transform.scale(factor, factor)
        self.setTransform(transform)
        after = self.mapToScene(anchor.toPoint())
        delta = after - before
        self.translate(delta.x(), delta.y())
        self.zoom_changed.emit(factor)
        self.viewport().update()

    def zoom_fit(self) -> None:
        rect = self.board_scene.content_rect()
        if rect.isNull():
            self.zoom_to(1.0)
            return
        self.fitInView(rect.adjusted(-60, -60, 60, 60), Qt.AspectRatioMode.KeepAspectRatio)
        if self.scale_factor() > 1.0:
            self.zoom_to(1.0)
            self.centerOn(rect.center())
        self.zoom_changed.emit(self.scale_factor())

    # ------------------------------------------------------------------ text editing
    def edit_text(self, obj_id: str, new_object: bool = False) -> None:
        obj = self.doc().view.get_live(obj_id)
        if obj is None:
            return
        self._commit_editor()
        editor = QPlainTextEdit(self.viewport())
        editor.setPlainText(obj["props"].get("text", ""))
        font = text_font(obj["props"])
        font.setPixelSize(max(8, int(obj["props"].get("font_size", 16) * self.scale_factor())))
        editor.setFont(font)
        editor.setStyleSheet(
            f"background: rgba(255,255,255,235); color:#1E1E1E; border: 1px solid "
            f"{theme.current().accent}; border-radius: 0; padding: 0;"
        )
        top_left = self.mapFromScene(QPointF(obj["props"]["x"], obj["props"]["y"]))
        scale = self.scale_factor()
        editor.setGeometry(
            top_left.x(),
            top_left.y(),
            max(120, int(obj["props"].get("w", 200) * scale)),
            max(40, int(obj["props"].get("h", 40) * scale)),
        )
        editor.show()
        editor.setFocus()
        editor.installEventFilter(self)
        self._editor = editor
        self._editing = (obj_id, new_object)

    def _commit_editor(self, cancel: bool = False) -> None:
        if self._editor is None or self._editing is None:
            return
        editor, (obj_id, new_object) = self._editor, self._editing
        self._editor, self._editing = None, None
        text = editor.toPlainText()
        editor.hide()
        editor.deleteLater()
        obj = self.doc().view.get_live(obj_id)
        if obj is None:
            return
        if not text.strip() and new_object and obj["type"] == "text":
            self.session.delete(obj_id)  # an empty text box left behind is just clutter
        elif not cancel and text != obj["props"].get("text", ""):
            patch: dict[str, Any] = {"text": text[:20000]}
            if obj["type"] == "text":
                metrics = QFontMetricsF(text_font(obj["props"]))
                box = metrics.boundingRect(
                    QRectF(0, 0, obj["props"].get("w", 240), 100000),
                    int(Qt.TextFlag.TextWordWrap),
                    text,
                )
                patch["h"] = max(20.0, box.height() + 4)
            self.session.update(obj_id, patch)
        self.setFocus()

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 (Qt override)
        if watched is self._editor:
            if event.type() == event.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self._commit_editor()
                return True
            if event.type() == event.Type.FocusOut:
                self._commit_editor()
        return super().eventFilter(watched, event)

    def create_comment(self, pos: QPointF) -> None:
        text, ok = QInputDialog.getMultiLineText(self, "Add a comment", "Comment")
        if not ok or not text.strip():
            return
        op = self.session.create(
            "comment",
            {
                "x": pos.x(),
                "y": pos.y(),
                "text": text.strip()[:20000],
                "author": self.session.username,
                "resolved": False,
                "replies": [],
            },
        )
        if op:
            self.comment_requested.emit(op["id"])

    def open_comment(self, obj_id: str) -> None:
        self.comment_requested.emit(obj_id)

    # ------------------------------------------------------------------ remote presence
    def on_transient(self, msg: dict[str, Any]) -> None:
        kind = msg.get("k")
        data = msg.get("d")
        source = msg.get("client_id")
        # Transients are forwarded from other clients unvalidated, so every field gets checked.
        if not isinstance(data, dict) or not isinstance(source, str):
            return
        now = time.monotonic()
        if kind == "cursor":
            x, y = data.get("x"), data.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                self.remote_cursors[source] = {
                    "x": float(x),
                    "y": float(y),
                    "name": str(msg.get("name", ""))[:40],
                    "fp": str(msg.get("from", "")),
                    "viewer": data.get("viewer") is True,
                    "time": now,
                }
        elif kind == "selection":
            ids = data.get("ids")
            if isinstance(ids, list):
                self.remote_selections[source] = {
                    "ids": [i for i in ids if isinstance(i, str)][:200],
                    "name": str(msg.get("name", ""))[:40],
                    "fp": str(msg.get("from", "")),
                    "time": now,
                }
        elif kind == "stroke_preview":
            points = data.get("points")
            if isinstance(points, list) and all(isinstance(n, (int, float)) for n in points):
                stroke = self.remote_strokes.setdefault(
                    str(data.get("id")),
                    {"points": [], "color": "#1E1E1E", "w": 2.0, "time": now},
                )
                stroke["points"] = (stroke["points"] + points)[-8000:]
                color = data.get("color")
                width = data.get("w")
                stroke["color"] = color if isinstance(color, str) else "#1E1E1E"
                stroke["w"] = float(width) if isinstance(width, (int, float)) else 2.0
                stroke["time"] = now
        elif kind == "stroke_end":
            self.remote_strokes.pop(str(data.get("id")), None)
        self.viewport().update()

    def _drop_stale_presence(self) -> None:
        now = time.monotonic()
        for table in (self.remote_cursors, self.remote_strokes):
            for key in [k for k, v in table.items() if now - v["time"] > STALE_CURSOR]:
                del table[key]
        self.viewport().update()

    def forget_presence(self, fingerprint: str) -> None:
        for table in (self.remote_cursors, self.remote_selections):
            for key in [k for k, v in table.items() if v.get("fp") == fingerprint]:
                del table[key]
        self.viewport().update()

    def remote_selection_colors(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for selection in self.remote_selections.values():
            for obj_id in selection["ids"]:
                out[obj_id] = theme.user_color(selection["fp"])
        return out

    # ------------------------------------------------------------------ Qt events
    def _scene_pos(self, event: Any) -> QPointF:
        return self.mapToScene(event.position().toPoint())

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802
        self._commit_editor()
        if (
            event.button() == Qt.MouseButton.MiddleButton
            or self._space_down
            or self.tool.name == "hand"
        ):
            self._panning = True
            self._pan_anchor = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_tool = self.tool
            self.tool.press(self._scene_pos(event), event)

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802
        pos = self._scene_pos(event)
        self._send_cursor(pos)
        if self._panning:
            delta = (event.position() - self._pan_anchor) / self.scale_factor()
            self._pan_anchor = event.position()
            self.translate(delta.x(), delta.y())
            self.viewport().update()
            return
        if event.buttons() & Qt.MouseButton.LeftButton and self._press_tool is not None:
            self._press_tool.move(pos, event)
        elif isinstance(self.tool, SelectTool):
            self.setCursor(self.tool.hover_cursor(pos))

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802
        if self._panning:
            self._panning = False
            self.setCursor(self.tool.cursor)
            return
        if event.button() == Qt.MouseButton.LeftButton and self._press_tool is not None:
            tool, self._press_tool = self._press_tool, None
            tool.release(self._scene_pos(event), event)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802
        self.tool.double_click(self._scene_pos(event), event)

    def wheelEvent(self, event: Any) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            step = ZOOM_STEP if event.angleDelta().y() > 0 else 1 / ZOOM_STEP
            self.zoom_to(self.scale_factor() * step, event.position())
        else:
            scale = self.scale_factor()
            self.translate(event.angleDelta().x() / scale / 2, event.angleDelta().y() / scale / 2)
            self.viewport().update()

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_down = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            return
        if event.key() == Qt.Key.Key_Escape:
            self.tool.cancel()
            self.set_selection(set())
            self.set_tool("select")
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: Any) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_down = False
            self.setCursor(self.tool.cursor)
            return
        super().keyReleaseEvent(event)

    def _send_cursor(self, pos: QPointF) -> None:
        now = time.monotonic()
        if now - self._last_cursor_send >= CURSOR_INTERVAL:
            self._last_cursor_send = now
            self.session.send_transient(
                "cursor",
                {
                    "x": round(pos.x(), 1),
                    "y": round(pos.y(), 1),
                    "viewer": self.session.role == "viewer",
                },
            )

    # ------------------------------------------------------------------ overlay painting
    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        painter.save()
        self._paint_empty_hint(painter)
        self._paint_remote_selections(painter)
        self._paint_selection(painter)
        self.tool.overlay(painter)
        for stroke in self.remote_strokes.values():
            if len(stroke["points"]) >= 4:
                self.paint_stroke(painter, stroke["points"], stroke["color"], stroke["w"])
        self._paint_remote_cursors(painter)
        painter.restore()

    def _paint_empty_hint(self, painter: QPainter) -> None:
        if self.board_scene.items_by_id:
            return
        painter.save()
        painter.resetTransform()
        painter.setPen(QColor(theme.current().text_muted))
        font = QFont(theme.FONT_FAMILY)
        font.setPixelSize(13)
        painter.setFont(font)
        hint = "Press P to draw, R for a shape" if self.can_edit() else "Nothing here yet"
        painter.drawText(self.viewport().rect(), int(Qt.AlignmentFlag.AlignCenter), hint)
        painter.restore()

    def _outline(self, obj: dict[str, Any]) -> QPolygonF:
        rect = object_rect(obj)
        return object_transform(obj).map(QPolygonF(rect))

    def _paint_selection(self, painter: QPainter) -> None:
        if not self.selection:
            return
        scale = self.scale_factor()
        accent = QColor(theme.current().accent)
        pen = QPen(accent, 1 / scale)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        board = self.doc().view
        if len(self.selection) == 1:
            obj = self.single_selected()
            if obj is None:
                return
            painter.drawPolygon(self._outline(obj))
            if not self.can_edit() or obj["type"] == "comment":
                return
            if obj["props"].get("locked"):
                self._paint_lock(painter, obj)
                return
            points = handle_points(object_rect(obj))
            transform = object_transform(obj)
            self._paint_handles(painter, [transform.map(p) for p in points.values()])
            return
        for obj_id in self.selection:
            obj = board.get_live(obj_id)
            if obj is not None:
                painter.drawPolygon(self._outline(obj))
        box = self.selection_rect()
        painter.drawRect(box)
        if self.can_edit():
            self._paint_handles(painter, list(handle_points(box).values()))

    def _paint_handles(self, painter: QPainter, points: list[QPointF]) -> None:
        scale = self.scale_factor()
        size = 8 / scale
        painter.setPen(QPen(QColor(theme.current().accent), 1 / scale))
        painter.setBrush(QColor("#FFFFFF"))
        for point in points:
            painter.drawRoundedRect(
                QRectF(point.x() - size / 2, point.y() - size / 2, size, size), 2 / scale, 2 / scale
            )

    def _paint_lock(self, painter: QPainter, obj: dict[str, Any]) -> None:
        painter.save()
        scale = self.scale_factor()
        font = QFont(theme.FONT_FAMILY)
        font.setPixelSize(max(1, int(11 / scale)))
        painter.setFont(font)
        painter.setPen(QColor(theme.current().accent))
        anchor = object_transform(obj).map(object_rect(obj).topLeft())
        painter.drawText(QPointF(anchor.x(), anchor.y() - 4 / scale), "Locked")
        painter.restore()

    def _paint_remote_selections(self, painter: QPainter) -> None:
        scale = self.scale_factor()
        board = self.doc().view
        for selection in self.remote_selections.values():
            color = QColor(theme.user_color(selection["fp"]))
            painter.setPen(QPen(color, 1.5 / scale))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            first = True
            for obj_id in selection["ids"]:
                obj = board.get_live(obj_id)
                if obj is None:
                    continue
                outline = self._outline(obj)
                painter.drawPolygon(outline)
                if first:
                    self._paint_chip(
                        painter, outline.boundingRect().topLeft(), selection["name"], color, -1
                    )
                    first = False
                    painter.setPen(QPen(color, 1.5 / scale))
                    painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_chip(
        self, painter: QPainter, at: QPointF, text: str, color: QColor, side: int = 1
    ) -> None:
        """A name tag at constant screen size. Text is never the only cue, nor color alone."""
        scale = self.scale_factor()
        painter.save()
        font = QFont(theme.FONT_FAMILY)
        font.setPixelSize(max(1, round(11 / scale)))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        metrics = QFontMetricsF(font)
        width = metrics.horizontalAdvance(text) + 10 / scale
        height = metrics.height() + 4 / scale
        top = at.y() + (4 / scale if side > 0 else -height - 2 / scale)
        chip = QRectF(at.x(), top, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(chip, 3 / scale, 3 / scale)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(chip, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.restore()

    def _paint_remote_cursors(self, painter: QPainter) -> None:
        scale = self.scale_factor()
        for cursor in self.remote_cursors.values():
            color = QColor(theme.user_color(cursor["fp"]))
            x, y = cursor["x"], cursor["y"]
            shape = QPolygonF(
                [
                    QPointF(x, y),
                    QPointF(x, y + 16 / scale),
                    QPointF(x + 4.5 / scale, y + 12 / scale),
                    QPointF(x + 11 / scale, y + 12 / scale),
                ]
            )
            painter.setPen(QPen(QColor("#FFFFFF"), 1 / scale))
            painter.setBrush(color)
            painter.drawPolygon(shape)
            label = cursor["name"] + (" (viewing)" if cursor["viewer"] else "")
            self._paint_chip(painter, QPointF(x + 8 / scale, y + 12 / scale), label, color)

    def paint_marquee(self, painter: QPainter, rect: QRectF) -> None:
        accent = QColor(theme.current().accent)
        painter.setPen(QPen(accent, 1 / self.scale_factor()))
        fill = QColor(accent)
        fill.setAlpha(30)
        painter.setBrush(fill)
        painter.drawRect(rect)

    def paint_guide(self, painter: QPainter, start: QPointF, end: QPointF) -> None:
        painter.setPen(QPen(QColor(theme.GUIDE_COLOR), 1 / self.scale_factor()))
        painter.drawLine(start, end)

    def paint_stroke(
        self, painter: QPainter, points: list[float], color: str, width: float
    ) -> None:
        pen = QPen(QColor(color), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(flat_to_path(points))

    def paint_shape_preview(self, painter: QPainter, kind: str, rect: QRectF) -> None:
        fill = QColor(self.fill_color) if self.fill_color and kind != "frame" else QColor("#FFFFFF")
        painter.setPen(QPen(QColor(theme.current().accent), 1 / self.scale_factor()))
        painter.setBrush(fill)
        path = QPainterPath()
        if kind == "ellipse":
            path.addEllipse(rect)
        else:
            path.addRect(rect)
        painter.drawPath(path)

    def paint_line_preview(
        self, painter: QPainter, start: QPointF, end: QPointF, arrow: bool
    ) -> None:
        painter.setPen(QPen(QColor(self.stroke_color), self.stroke_width))
        painter.drawLine(start, end)


def keys_index(ordered: list[dict[str, Any]], obj_id: str) -> int:
    for i, obj in enumerate(ordered):
        if obj["id"] == obj_id:
            return i
    return -1
