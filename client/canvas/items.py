"""Scene items. One class paints every object type, driven entirely by the object's properties.

Items hold no authoritative state. They are rebuilt from the document, so whatever the server's op
order decides is what appears on screen.
"""

from __future__ import annotations

import base64
import math
from typing import Any

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import QGraphicsItem

from client import theme

HIT_MARGIN = 6.0
ARROW_SIZE = 12.0
PIN_SIZE = 22.0


def to_color(value: str | None) -> QColor | None:
    if not value:
        return None
    color = QColor(value)
    return color if color.isValid() else None


def text_font(props: dict[str, Any]) -> QFont:
    font = QFont(theme.FONT_FAMILY)
    font.setPixelSize(max(1, int(props.get("font_size", 16))))
    font.setWeight(QFont.Weight(int(props.get("font_weight", 400))))
    return font


def object_rect(obj: dict[str, Any]) -> QRectF:
    """Local-space rectangle of an object, used for selection and handles."""
    props = obj["props"]
    kind = obj["type"]
    if kind in ("line", "arrow"):
        rect = QRectF(0, 0, props.get("x2", 0), props.get("y2", 0)).normalized()
        return rect
    if kind == "comment":
        return QRectF(0, -PIN_SIZE, PIN_SIZE, PIN_SIZE)
    return QRectF(0, 0, props.get("w", 0), props.get("h", 0))


def flat_to_path(points: list[float], sx: float = 1.0, sy: float = 1.0) -> QPainterPath:
    path = QPainterPath()
    if len(points) < 2:
        return path
    path.moveTo(points[0] * sx, points[1] * sy)
    if len(points) == 2:
        path.lineTo(points[0] * sx + 0.01, points[1] * sy)  # a dot still needs a visible segment
        return path
    # Quadratic segments through midpoints make the simplified polyline look hand-drawn again.
    for i in range(2, len(points) - 2, 2):
        cx, cy = points[i] * sx, points[i + 1] * sy
        mx = (points[i] + points[i + 2]) / 2 * sx
        my = (points[i + 1] + points[i + 3]) / 2 * sy
        path.quadTo(cx, cy, mx, my)
    path.lineTo(points[-2] * sx, points[-1] * sy)
    return path


def points_bbox(points: list[float]) -> tuple[float, float]:
    xs, ys = points[0::2] or [0.0], points[1::2] or [0.0]
    return max(xs) - min(xs), max(ys) - min(ys)


class BoardItem(QGraphicsItem):
    def __init__(self, obj: dict[str, Any]) -> None:
        super().__init__()
        self.obj = obj
        self.clip_rect: QRectF | None = None
        self._image: QImage | None = None
        self._image_key = ""
        self._path: QPainterPath | None = None
        self._bounds = QRectF()
        self.setAcceptHoverEvents(False)
        self.sync(obj)

    @property
    def obj_id(self) -> str:
        return self.obj["id"]

    def sync(self, obj: dict[str, Any]) -> None:
        """Adopt new property values. prepareGeometryChange must come first, or Qt's index rots."""
        self.prepareGeometryChange()
        self.obj = obj
        props = obj["props"]
        self._path = None
        self.setPos(props.get("x", 0), props.get("y", 0))
        rect = object_rect(obj)
        self.setTransformOriginPoint(rect.center())
        self.setRotation(props.get("rotation", 0) if obj["type"] not in ("comment",) else 0)
        self.setOpacity(props.get("opacity", 1))
        self.setVisible(props.get("visible", True) and not obj["deleted"])
        margin = props.get("stroke_w", 0) if props.get("stroke") else 0
        margin = margin / 2 + (ARROW_SIZE if obj["type"] == "arrow" else 0) + 1
        self._bounds = rect.adjusted(-margin, -margin, margin, margin)
        if obj["type"] == "frame":
            self._bounds.setTop(self._bounds.top() - 18)  # room for the title above the frame
        if obj["type"] == "image":
            self._load_image(props.get("image_data", ""))
        self.update()

    def _load_image(self, data: str) -> None:
        key = str(hash(data))
        if key == self._image_key:
            return
        self._image_key = key
        self._image = QImage()
        try:
            self._image.loadFromData(base64.b64decode(data))
        except ValueError:
            self._image = QImage()

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt override)
        return self._bounds

    def _stroke_path(self) -> QPainterPath:
        if self._path is None:
            props = self.obj["props"]
            points = props.get("points", [])
            bw, bh = points_bbox(points) if points else (0.0, 0.0)
            sx = props["w"] / bw if bw and props.get("w") else 1.0
            sy = props["h"] / bh if bh and props.get("h") else 1.0
            self._path = flat_to_path(points, sx, sy)
        return self._path

    def shape(self) -> QPainterPath:
        kind = self.obj["type"]
        props = self.obj["props"]
        stroker = QPainterPathStroker()
        stroker.setWidth(max(props.get("stroke_w", 1), 1) + HIT_MARGIN)
        if kind == "path":
            return stroker.createStroke(self._stroke_path())
        if kind in ("line", "arrow"):
            path = QPainterPath()
            path.moveTo(0, 0)
            path.lineTo(props.get("x2", 0), props.get("y2", 0))
            return stroker.createStroke(path)
        path = QPainterPath()
        rect = object_rect(self.obj)
        if kind == "ellipse":
            path.addEllipse(rect)
        else:
            path.addRect(rect)
        return path

    def _pen(self) -> QPen:
        props = self.obj["props"]
        color = to_color(props.get("stroke"))
        if color is None:
            return QPen(Qt.PenStyle.NoPen)
        pen = QPen(color, props.get("stroke_w", 1))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return pen

    def _brush(self) -> QBrush:
        color = to_color(self.obj["props"].get("fill"))
        return QBrush(color) if color else QBrush(Qt.BrushStyle.NoBrush)

    def paint(self, painter: QPainter, option: Any = None, widget: Any = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.clip_rect is not None:
            # Painted rather than via parent-child items, so moving an object never reparents it.
            painter.setClipRect(self.mapRectFromScene(self.clip_rect))
        kind = self.obj["type"]
        painter.setPen(self._pen())
        painter.setBrush(self._brush())
        getattr(self, f"_paint_{kind}", self._paint_rect)(painter)

    def _paint_rect(self, painter: QPainter) -> None:
        painter.drawRect(object_rect(self.obj))

    def _paint_ellipse(self, painter: QPainter) -> None:
        painter.drawEllipse(object_rect(self.obj))

    def _paint_frame(self, painter: QPainter) -> None:
        painter.drawRect(object_rect(self.obj))
        painter.setPen(QColor("#6B6B6B"))
        font = QFont(theme.FONT_FAMILY)
        font.setPixelSize(11)
        painter.setFont(font)
        name = self.obj["props"].get("name") or "Frame"
        painter.drawText(QPointF(0, -5), name)

    def _paint_group(self, painter: QPainter) -> None:
        return  # a group has no appearance of its own; only its selection outline is drawn

    def _paint_line(self, painter: QPainter) -> None:
        props = self.obj["props"]
        painter.drawLine(QPointF(0, 0), QPointF(props.get("x2", 0), props.get("y2", 0)))

    def _paint_arrow(self, painter: QPainter) -> None:
        props = self.obj["props"]
        x2, y2 = props.get("x2", 0), props.get("y2", 0)
        painter.drawLine(QPointF(0, 0), QPointF(x2, y2))
        angle = math.atan2(y2, x2)
        size = max(ARROW_SIZE, props.get("stroke_w", 1) * 4)
        head = QPolygonF(
            [
                QPointF(x2, y2),
                QPointF(x2 - size * math.cos(angle - 0.45), y2 - size * math.sin(angle - 0.45)),
                QPointF(x2 - size * math.cos(angle + 0.45), y2 - size * math.sin(angle + 0.45)),
            ]
        )
        painter.setBrush(QBrush(to_color(props.get("stroke")) or QColor("#1E1E1E")))
        painter.drawPolygon(head)

    def _paint_path(self, painter: QPainter) -> None:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self._stroke_path())

    def _paint_text(self, painter: QPainter) -> None:
        props = self.obj["props"]
        painter.setPen(to_color(props.get("fill")) or QColor("#1E1E1E"))
        painter.setFont(text_font(props))
        rect = object_rect(self.obj)
        flags = Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        painter.drawText(rect, int(flags), props.get("text", ""))

    def _paint_sticky(self, painter: QPainter) -> None:
        props = self.obj["props"]
        rect = object_rect(self.obj)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(to_color(props.get("fill")) or QColor(theme.STICKY_COLOR)))
        painter.drawRoundedRect(rect, 4, 4)
        painter.setPen(QColor("#1E1E1E"))
        painter.setFont(text_font(props))
        inner = rect.adjusted(10, 10, -10, -10)
        flags = Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        painter.drawText(inner, int(flags), props.get("text", ""))

    def _paint_image(self, painter: QPainter) -> None:
        rect = object_rect(self.obj)
        if self._image is not None and not self._image.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(rect, self._image)
        else:
            painter.setPen(QPen(QColor("#B3B3B3"), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)

    def _paint_comment(self, painter: QPainter) -> None:
        props = self.obj["props"]
        color = QColor("#14AE5C") if props.get("resolved") else QColor("#FFA629")
        painter.setPen(QPen(QColor("#FFFFFF"), 1.5))
        painter.setBrush(color)
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)  # the overlap is solid, not a hole
        path.addRoundedRect(QRectF(0, -PIN_SIZE, PIN_SIZE, PIN_SIZE), 11, 11)
        path.addRect(QRectF(0, -11, 11, 11))
        painter.drawPath(path.simplified())
        painter.setPen(QColor("#1E1E1E"))
        font = QFont(theme.FONT_FAMILY)
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        initial = (props.get("author") or "?")[:1].upper()
        painter.drawText(
            QRectF(0, -PIN_SIZE, PIN_SIZE, PIN_SIZE), int(Qt.AlignmentFlag.AlignCenter), initial
        )
