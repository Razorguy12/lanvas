"""Scene items. One class paints every object type, driven entirely by the object's properties.

Items hold no authoritative state. They are rebuilt from the document, so whatever the server's op
order decides is what appears on screen.
"""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Any

from client import theme
from client.canvas.paint import Painter, Pen, decode_image, flatten, rounded_rect_points
from client.geom import (
    Affine,
    Point,
    Rect,
    polygon_contains,
    polygon_hits_rect,
    segment_distance,
    segment_hits_rect,
)

HIT_MARGIN = 6.0
ARROW_SIZE = 12.0
PIN_SIZE = 22.0


def to_color(value: str | None) -> str | None:
    return value if value and theme.valid_color(value) else None


def font_props(props: dict[str, Any]) -> tuple[float, int]:
    """(pixel size, weight) of a text or sticky object."""
    return max(1.0, float(int(props.get("font_size", 16)))), int(props.get("font_weight", 400))


def object_rect(obj: dict[str, Any]) -> Rect:
    """Local-space rectangle of an object, used for selection and handles."""
    props = obj["props"]
    kind = obj["type"]
    if kind in ("line", "arrow"):
        return Rect(0, 0, props.get("x2", 0), props.get("y2", 0)).normalized()
    if kind == "comment":
        return Rect(0, -PIN_SIZE, PIN_SIZE, PIN_SIZE)
    return Rect(0, 0, props.get("w", 0), props.get("h", 0))


def object_transform(obj: dict[str, Any]) -> Affine:
    """Local object space to scene space. Rotation is about the rectangle's center."""
    props = obj["props"]
    center = object_rect(obj).center()
    transform = Affine().translate(props.get("x", 0), props.get("y", 0))
    if obj["type"] != "comment":
        transform = transform.translate(center.x, center.y)
        transform = transform.rotate(props.get("rotation", 0))
        transform = transform.translate(-center.x, -center.y)
    return transform


def flat_to_path(points: list[float], sx: float = 1.0, sy: float = 1.0) -> list[tuple]:
    """Path segments for a stroke. Quadratic segments through midpoints make the simplified
    polyline look hand-drawn again."""
    if len(points) < 2:
        return []
    path: list[tuple] = [("M", points[0] * sx, points[1] * sy)]
    if len(points) == 2:
        path.append(("L", points[0] * sx + 0.01, points[1] * sy))  # a dot still needs a segment
        return path
    for i in range(2, len(points) - 2, 2):
        cx, cy = points[i] * sx, points[i + 1] * sy
        mx = (points[i] + points[i + 2]) / 2 * sx
        my = (points[i + 1] + points[i + 3]) / 2 * sy
        path.append(("Q", cx, cy, mx, my))
    path.append(("L", points[-2] * sx, points[-1] * sy))
    return path


def points_bbox(points: list[float]) -> tuple[float, float]:
    xs, ys = points[0::2] or [0.0], points[1::2] or [0.0]
    return max(xs) - min(xs), max(ys) - min(ys)


def arrow_head(x2: float, y2: float, stroke_w: float) -> list[Point]:
    angle = math.atan2(y2, x2)
    size = max(ARROW_SIZE, stroke_w * 4)
    return [
        Point(x2, y2),
        Point(x2 - size * math.cos(angle - 0.45), y2 - size * math.sin(angle - 0.45)),
        Point(x2 - size * math.cos(angle + 0.45), y2 - size * math.sin(angle + 0.45)),
    ]


def comment_pin() -> list[Point]:
    """A round pin whose bottom-left corner is square, pointing at the commented spot."""
    return rounded_rect_points(Rect(0, -PIN_SIZE, PIN_SIZE, PIN_SIZE), 11,
                               corners=(True, True, False, True))


class BoardItem:
    def __init__(self, obj: dict[str, Any]) -> None:
        self.obj = obj
        self.clip_rect: Rect | None = None
        self.z = 0.0
        self._image: Any = None
        self._image_key = ""
        self._path: list[tuple] | None = None
        self._bounds = Rect()
        self._opacity = 1.0
        self._visible = True
        self._transform = Affine()
        self._scene_bounds = Rect()
        self.sync(obj)

    @property
    def obj_id(self) -> str:
        return self.obj["id"]

    def sync(self, obj: dict[str, Any]) -> None:
        self.obj = obj
        props = obj["props"]
        self._path = None
        self._transform = object_transform(obj)
        self._opacity = props.get("opacity", 1)
        self._visible = props.get("visible", True) and not obj["deleted"]
        rect = object_rect(obj)
        margin = props.get("stroke_w", 0) if props.get("stroke") else 0
        margin = margin / 2 + (ARROW_SIZE if obj["type"] == "arrow" else 0) + 1
        bounds = rect.adjusted(-margin, -margin, margin, margin)
        if obj["type"] == "frame":
            bounds = bounds.adjusted(0, -18, 0, 0)  # room for the title above the frame
        if obj["type"] == "arrow":
            stroke_w = props.get("stroke_w", 1)
            reach = max(ARROW_SIZE, stroke_w * 4) + stroke_w
            bounds = rect.adjusted(-reach, -reach, reach, reach)
        self._bounds = bounds
        self._scene_bounds = self._transform.map_rect(bounds)
        if obj["type"] == "image":
            self._load_image(props.get("image_data", ""))

    def _load_image(self, data: str) -> None:
        key = str(hash(data))
        if key != self._image_key:
            self._image_key = key
            self._image = decode_image(data) if data else None

    # -------------------------------------------------------------- Qt-like item queries
    def isVisible(self) -> bool:  # noqa: N802
        return self._visible

    def opacity(self) -> float:
        return self._opacity

    def setOpacity(self, value: float) -> None:  # noqa: N802 (live preview from a slider)
        self._opacity = value

    def zValue(self) -> float:  # noqa: N802
        return self.z

    def setZValue(self, z: float) -> None:  # noqa: N802
        self.z = z

    def x(self) -> float:
        return float(self.obj["props"].get("x", 0))

    def y(self) -> float:
        return float(self.obj["props"].get("y", 0))

    def boundingRect(self) -> Rect:  # noqa: N802
        return self._bounds

    def sceneTransform(self) -> Affine:  # noqa: N802
        return self._transform

    def sceneBoundingRect(self) -> Rect:  # noqa: N802
        return self._scene_bounds

    def stroke_path(self) -> list[tuple]:
        if self._path is None:
            props = self.obj["props"]
            points = props.get("points", [])
            bw, bh = points_bbox(points) if points else (0.0, 0.0)
            sx = props["w"] / bw if bw and props.get("w") else 1.0
            sy = props["h"] / bh if bh and props.get("h") else 1.0
            self._path = flat_to_path(points, sx, sy)
        return self._path

    # -------------------------------------------------------------- hit testing (Qt's shape())
    def _local(self, p: Point) -> Point | None:
        inverse, ok = self._transform.inverted()
        return inverse.map(p) if ok else None

    def _hit_width(self) -> float:
        return max(self.obj["props"].get("stroke_w", 1), 1) + HIT_MARGIN

    def contains(self, scene_point: Point, radius: float = 0.0) -> bool:
        """Whether the item's shape contains the point, or comes within ``radius`` of it."""
        if not self._visible or not self.sceneBoundingRect().adjusted(
                -radius, -radius, radius, radius).contains(scene_point):
            return False
        local = self._local(scene_point)
        if local is None:
            return False
        kind = self.obj["type"]
        scale = self._transform.scale_factor() or 1.0
        reach = self._hit_width() / 2 + radius / scale
        if kind == "path":
            pts = flatten(self.stroke_path(), 6)
            if len(pts) == 1:
                return math.hypot(local.x - pts[0].x, local.y - pts[0].y) <= reach
            return any(segment_distance(local, a, b) <= reach for a, b in pairwise(pts))
        if kind in ("line", "arrow"):
            props = self.obj["props"]
            end = Point(props.get("x2", 0), props.get("y2", 0))
            return segment_distance(local, Point(0, 0), end) <= reach
        rect = object_rect(self.obj)
        grow = radius / scale
        if kind == "ellipse":
            rx, ry = rect.w / 2 + grow, rect.h / 2 + grow
            if rx <= 0 or ry <= 0:
                return False
            c = rect.center()
            return ((local.x - c.x) / rx) ** 2 + ((local.y - c.y) / ry) ** 2 <= 1
        return rect.adjusted(-grow, -grow, grow, grow).contains(local)

    def intersects(self, scene_rect: Rect) -> bool:
        """Whether the item's shape touches the rectangle (Qt's IntersectsItemShape)."""
        if not self._visible or not self.sceneBoundingRect().intersects(scene_rect):
            return False
        kind = self.obj["type"]
        if kind in ("path", "line", "arrow"):
            if kind == "path":
                pts = [self._transform.map(p) for p in flatten(self.stroke_path(), 6)]
            else:
                props = self.obj["props"]
                pts = [self._transform.map(Point(0, 0)),
                       self._transform.map(Point(props.get("x2", 0), props.get("y2", 0)))]
            if len(pts) == 1:
                return scene_rect.contains(pts[0])
            return any(segment_hits_rect(a, b, scene_rect) for a, b in pairwise(pts))
        rect = object_rect(self.obj)
        if kind == "ellipse":
            from client.canvas.paint import ellipse_points

            poly = [self._transform.map(p) for p in ellipse_points(rect, 32)]
        else:
            poly = self._transform.map_polygon(rect)
        return polygon_hits_rect(poly, scene_rect) or polygon_contains(poly, scene_rect.center())

    # -------------------------------------------------------------- painting
    def _pen(self) -> Pen | None:
        props = self.obj["props"]
        color = to_color(props.get("stroke"))
        return Pen(color, props.get("stroke_w", 1)) if color else None

    def _brush(self) -> str | None:
        return to_color(self.obj["props"].get("fill"))

    def paint_with(self, painter: Painter, base: Affine | None = None) -> None:
        transform = self._transform if base is None else self._transform.then(base)
        clip = self.clip_rect
        if not painter.begin_item(transform, self._opacity, self.sceneBoundingRect(), clip):
            return
        try:
            self.paint(painter)
        finally:
            painter.end_item()

    def paint(self, painter: Painter) -> None:
        getattr(self, f"_paint_{self.obj['type']}", self._paint_rect)(painter)

    def _paint_rect(self, painter: Painter) -> None:
        painter.rect(object_rect(self.obj), self._pen(), self._brush())

    def _paint_ellipse(self, painter: Painter) -> None:
        painter.ellipse(object_rect(self.obj), self._pen(), self._brush())

    def _paint_frame(self, painter: Painter) -> None:
        painter.rect(object_rect(self.obj), self._pen(), self._brush())
        name = self.obj["props"].get("name") or "Frame"
        painter.text_at(Point(0, -5), name, 11, 400, "#6B6B6B")

    def _paint_group(self, painter: Painter) -> None:
        return  # a group has no appearance of its own; only its selection outline is drawn

    def _paint_line(self, painter: Painter) -> None:
        props = self.obj["props"]
        pen = self._pen()
        if pen is not None:
            painter.polyline([Point(0, 0), Point(props.get("x2", 0), props.get("y2", 0))], pen)

    def _paint_arrow(self, painter: Painter) -> None:
        props = self.obj["props"]
        x2, y2 = props.get("x2", 0), props.get("y2", 0)
        pen = self._pen()
        if pen is not None:
            painter.polyline([Point(0, 0), Point(x2, y2)], pen)
        head_color = to_color(props.get("stroke")) or "#1E1E1E"
        painter.polygon(arrow_head(x2, y2, props.get("stroke_w", 1)), pen, head_color)

    def _paint_path(self, painter: Painter) -> None:
        pen = self._pen()
        if pen is not None:
            painter.path(self.stroke_path(), pen)

    def _paint_text(self, painter: Painter) -> None:
        props = self.obj["props"]
        size, weight = font_props(props)
        color = to_color(props.get("fill")) or "#1E1E1E"
        painter.text(object_rect(self.obj), props.get("text", ""), size, weight, color)

    def _paint_sticky(self, painter: Painter) -> None:
        props = self.obj["props"]
        rect = object_rect(self.obj)
        painter.rect(rect, None, to_color(props.get("fill")) or theme.STICKY_COLOR, radius=4)
        size, weight = font_props(props)
        painter.text(rect.adjusted(10, 10, -10, -10), props.get("text", ""), size, weight,
                     "#1E1E1E")

    def _paint_image(self, painter: Painter) -> None:
        rect = object_rect(self.obj)
        if self._image is not None:
            painter.image(rect, self._image, self.obj["props"].get("image_data", ""))
        else:
            painter.rect(rect, Pen("#B3B3B3", 1, dash=(4, 2)), None)

    def _paint_comment(self, painter: Painter) -> None:
        props = self.obj["props"]
        color = "#14AE5C" if props.get("resolved") else "#FFA629"
        painter.polygon(comment_pin(), Pen("#FFFFFF", 1.5), color)
        initial = (props.get("author") or "?")[:1].upper()
        painter.text(Rect(0, -PIN_SIZE, PIN_SIZE, PIN_SIZE), initial, 11, 600, "#1E1E1E",
                     align="center", valign="center", wrap=False)
