"""Plain 2D geometry: points, rectangles and affine transforms, with no UI toolkit involved.

The names and semantics follow the ones the canvas code was designed around (``QPointF``,
``QRectF``, ``QTransform``), so tools, hit tests and exports all share one tested vocabulary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(slots=True)
class Point:
    x: float = 0.0
    y: float = 0.0

    def __add__(self, other: Point) -> Point:
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Point) -> Point:
        return Point(self.x - other.x, self.y - other.y)

    def __truediv__(self, k: float) -> Point:
        return Point(self.x / k, self.y / k)


@dataclass(slots=True)
class Rect:
    """Axis-aligned rectangle. A rectangle with zero width and height is "null", as in Qt."""

    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0

    @staticmethod
    def from_points(a: Point, b: Point) -> Rect:
        return Rect(a.x, a.y, b.x - a.x, b.y - a.y)

    @property
    def left(self) -> float:
        return self.x

    @property
    def top(self) -> float:
        return self.y

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def center(self) -> Point:
        return Point(self.x + self.w / 2, self.y + self.h / 2)

    def is_null(self) -> bool:
        return self.w == 0 and self.h == 0

    def normalized(self) -> Rect:
        x, w = (self.x + self.w, -self.w) if self.w < 0 else (self.x, self.w)
        y, h = (self.y + self.h, -self.h) if self.h < 0 else (self.y, self.h)
        return Rect(x, y, w, h)

    def adjusted(self, dx1: float, dy1: float, dx2: float, dy2: float) -> Rect:
        return Rect(self.x + dx1, self.y + dy1, self.w - dx1 + dx2, self.h - dy1 + dy2)

    def translated(self, dx: float, dy: float) -> Rect:
        return Rect(self.x + dx, self.y + dy, self.w, self.h)

    def united(self, other: Rect) -> Rect:
        if self.is_null():
            return other
        if other.is_null():
            return self
        a, b = self.normalized(), other.normalized()
        left, top = min(a.left, b.left), min(a.top, b.top)
        return Rect(left, top, max(a.right, b.right) - left, max(a.bottom, b.bottom) - top)

    def contains(self, p: Point) -> bool:
        r = self.normalized()
        return r.left <= p.x <= r.right and r.top <= p.y <= r.bottom

    def intersects(self, other: Rect) -> bool:
        a, b = self.normalized(), other.normalized()
        return a.left <= b.right and b.left <= a.right and a.top <= b.bottom and b.top <= a.bottom

    def corners(self) -> list[Point]:
        return [
            Point(self.left, self.top),
            Point(self.right, self.top),
            Point(self.right, self.bottom),
            Point(self.left, self.bottom),
        ]


@dataclass(slots=True)
class Affine:
    """x' = a*x + c*y + e, y' = b*x + d*y + f. Composition follows Qt: ``t.translate(...)``
    returns a transform that applies the translation first, then ``t``."""

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    def then(self, other: Affine) -> Affine:
        """Apply self, then other."""
        return Affine(
            self.a * other.a + self.b * other.c,
            self.a * other.b + self.b * other.d,
            self.c * other.a + self.d * other.c,
            self.c * other.b + self.d * other.d,
            self.e * other.a + self.f * other.c + other.e,
            self.e * other.b + self.f * other.d + other.f,
        )

    def translate(self, dx: float, dy: float) -> Affine:
        return Affine(e=dx, f=dy).then(self)

    def scale(self, sx: float, sy: float | None = None) -> Affine:
        return Affine(a=sx, d=sx if sy is None else sy).then(self)

    def rotate(self, degrees: float) -> Affine:
        r = math.radians(degrees)
        cos, sin = math.cos(r), math.sin(r)
        return Affine(cos, sin, -sin, cos).then(self)

    def map(self, p: Point) -> Point:
        return Point(self.a * p.x + self.c * p.y + self.e, self.b * p.x + self.d * p.y + self.f)

    def map_xy(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f

    def map_rect(self, r: Rect) -> Rect:
        """Bounding box of the mapped rectangle."""
        if self.b == 0 and self.c == 0:  # scale and translate only: no corners needed
            x0, x1 = self.a * r.x + self.e, self.a * (r.x + r.w) + self.e
            y0, y1 = self.d * r.y + self.f, self.d * (r.y + r.h) + self.f
            return Rect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
        pts = [self.map(p) for p in r.corners()]
        xs, ys = [p.x for p in pts], [p.y for p in pts]
        return Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    def map_polygon(self, r: Rect) -> list[Point]:
        return [self.map(p) for p in r.corners()]

    def inverted(self) -> tuple[Affine, bool]:
        det = self.a * self.d - self.b * self.c
        if abs(det) < 1e-12:
            return Affine(), False
        a, b, c, d = self.d / det, -self.b / det, -self.c / det, self.a / det
        return Affine(a, b, c, d, -(a * self.e + c * self.f), -(b * self.e + d * self.f)), True

    def scale_factor(self) -> float:
        return math.sqrt(abs(self.a * self.d - self.b * self.c)) or 1.0

    def is_axis_aligned(self) -> bool:
        return abs(self.b) < 1e-9 and abs(self.c) < 1e-9


def polygon_contains(poly: list[Point], p: Point) -> bool:
    inside = False
    j = len(poly) - 1
    for i, a in enumerate(poly):
        b = poly[j]
        if (a.y > p.y) != (b.y > p.y):
            cross = (b.x - a.x) * (p.y - a.y) / (b.y - a.y) + a.x
            if p.x < cross:
                inside = not inside
        j = i
    return inside


def segment_distance(p: Point, a: Point, b: Point) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / length))
    return math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))


def _segments_cross(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    def orient(a: Point, b: Point, c: Point) -> float:
        return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)

    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    return (d1 * d2 <= 0) and (d3 * d4 <= 0)


def segment_hits_rect(a: Point, b: Point, r: Rect) -> bool:
    if r.contains(a) or r.contains(b):
        return True
    corners = r.normalized().corners()
    return any(_segments_cross(a, b, corners[i], corners[(i + 1) % 4]) for i in range(4))


def polygon_hits_rect(poly: list[Point], r: Rect) -> bool:
    if any(r.contains(p) for p in poly):
        return True
    if any(polygon_contains(poly, c) for c in r.normalized().corners()):
        return True
    return any(segment_hits_rect(poly[i], poly[(i + 1) % len(poly)], r) for i in range(len(poly)))
