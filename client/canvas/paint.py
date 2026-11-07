"""The painting vocabulary every backend shares: pens, text layout and the raster painter.

``BoardItem.paint`` speaks only this API, in the object's local coordinates. Three backends
implement it: :class:`RasterPainter` here (the live canvas, PNG and JPEG), and the SVG and PDF
writers in ``client/export``. Text is laid out once, here, so every backend wraps identically.
"""

from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from client import theme
from client.geom import Affine, Point, Rect

Segment = tuple  # ("M", x, y) | ("L", x, y) | ("Q", cx, cy, x, y)


@dataclass(frozen=True)
class Pen:
    color: str
    width: float = 1.0
    dash: tuple[float, float] | None = None


# ---------------------------------------------------------------------- fonts and text layout
@lru_cache(maxsize=256)
def pil_font(size: float, weight: int = 400) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(theme.font_path(weight)), max(1.0, round(size * 4) / 4))


def line_spacing(size: float, weight: int = 400) -> float:
    ascent, descent = pil_font(size, weight).getmetrics()
    return float(ascent + descent)


def text_width(text: str, size: float, weight: int = 400) -> float:
    return float(pil_font(size, weight).getlength(text))


def layout_text(text: str, width: float, size: float, weight: int = 400,
                wrap: bool = True) -> list[str]:
    """Break text into lines no wider than ``width`` at word boundaries, keeping explicit
    newlines. A single word longer than the box overflows rather than being split, as in Qt."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not wrap or width <= 0:
            lines.append(paragraph)
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if current and text_width(candidate, size, weight) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


def text_height(text: str, width: float, size: float, weight: int = 400) -> float:
    return len(layout_text(text, width, size, weight)) * line_spacing(size, weight)


# ---------------------------------------------------------------------- geometry helpers
def ellipse_points(r: Rect, steps: int = 64) -> list[Point]:
    cx, cy, rx, ry = r.x + r.w / 2, r.y + r.h / 2, r.w / 2, r.h / 2
    return [Point(cx + rx * math.cos(2 * math.pi * i / steps),
                  cy + ry * math.sin(2 * math.pi * i / steps)) for i in range(steps)]


def rounded_rect_points(r: Rect, radius: float, steps: int = 6,
                        corners: tuple[bool, bool, bool, bool] = (True, True, True, True)
                        ) -> list[Point]:
    """Corners in order: top-right, bottom-right, bottom-left, top-left."""
    radius = max(0.0, min(radius, abs(r.w) / 2, abs(r.h) / 2))
    spec = ((r.right - radius, r.top + radius, -90, r.right, r.top),
            (r.right - radius, r.bottom - radius, 0, r.right, r.bottom),
            (r.left + radius, r.bottom - radius, 90, r.left, r.bottom),
            (r.left + radius, r.top + radius, 180, r.left, r.top))
    pts: list[Point] = []
    for rounded, (cx, cy, start, sx, sy) in zip(corners, spec, strict=True):
        if not rounded or radius == 0:
            pts.append(Point(sx, sy))
            continue
        for i in range(steps + 1):
            t = math.radians(start + 90 * i / steps)
            pts.append(Point(cx + radius * math.cos(t), cy + radius * math.sin(t)))
    return pts


def flatten(segments: list[Segment], tolerance_steps: int = 8) -> list[Point]:
    pts: list[Point] = []
    x = y = 0.0
    for seg in segments:
        if seg[0] in ("M", "L"):
            x, y = seg[1], seg[2]
            pts.append(Point(x, y))
        elif seg[0] == "Q":
            cx, cy, ex, ey = seg[1:]
            for i in range(1, tolerance_steps + 1):
                t = i / tolerance_steps
                u = 1 - t
                pts.append(Point(u * u * x + 2 * u * t * cx + t * t * ex,
                                 u * u * y + 2 * u * t * cy + t * t * ey))
            x, y = ex, ey
    return pts


def decode_image(data: str) -> Image.Image | None:
    try:
        image = Image.open(io.BytesIO(base64.b64decode(data)))
        image.load()
        return image.convert("RGBA")
    except Exception:  # noqa: BLE001 (any undecodable payload is just "no image")
        return None


# ---------------------------------------------------------------------- painter API
class Painter:
    """Backends override these. Coordinates are the current item's local space."""

    def begin_item(self, transform: Affine, opacity: float, bounds: Rect,
                   clip: Rect | None) -> bool:
        return True

    def end_item(self) -> None: ...
    def polygon(self, pts: list[Point], pen: Pen | None, brush: str | None) -> None: ...
    def polyline(self, pts: list[Point], pen: Pen) -> None: ...
    def path(self, segments: list[Segment], pen: Pen) -> None: ...

    def text(self, r: Rect, text: str, size: float, weight: int, color: str,
             align: str = "left", valign: str = "top", wrap: bool = True) -> None: ...

    def text_at(self, p: Point, text: str, size: float, weight: int, color: str) -> None: ...
    def image(self, r: Rect, image: Image.Image | None, data: str) -> None: ...

    def rect(self, r: Rect, pen: Pen | None, brush: str | None, radius: float = 0) -> None:
        pts = rounded_rect_points(r, radius) if radius else r.corners()
        self.polygon(pts, pen, brush)

    def ellipse(self, r: Rect, pen: Pen | None, brush: str | None) -> None:
        self.polygon(ellipse_points(r), pen, brush)


@lru_cache(maxsize=1024)
def _rgba(color: str) -> tuple[int, int, int, int]:
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    alpha = int(value[6:8], 16) if len(value) >= 8 else 255
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha


def _pil_affine(transform: Affine) -> tuple[float, ...] | None:
    inverse, ok = transform.inverted()
    if not ok:
        return None
    return (inverse.a, inverse.c, inverse.e, inverse.b, inverse.d, inverse.f)


class RasterPainter(Painter):
    """Paints into an RGBA Pillow image. Each item is drawn on its own layer at ``supersample``
    times the resolution and averaged down, which anti-aliases edges and makes per-item opacity
    and frame clipping exact."""

    def __init__(self, target: Image.Image, base: Affine, supersample: int = 2) -> None:
        self.target = target
        self.base = base
        self.ss = max(1, supersample)
        self.layer: Image.Image | None = None
        self.draw: ImageDraw.ImageDraw | None = None
        self.m = Affine()
        self.origin = (0, 0)
        self.opacity = 1.0
        self.clip: Rect | None = None
        self.direct = False
        # An opaque RGB target (the live canvas) blends drawing straight in; see begin_item.
        self._target_draw = ImageDraw.Draw(target, "RGBA") if target.mode == "RGB" else None

    def begin_item(self, transform: Affine, opacity: float, bounds: Rect,
                   clip: Rect | None) -> bool:
        device = self.base.map_rect(bounds).adjusted(-2, -2, 2, 2)
        left, top = max(0, math.floor(device.left)), max(0, math.floor(device.top))
        right = min(self.target.width, math.ceil(device.right))
        bottom = min(self.target.height, math.ceil(device.bottom))
        if clip is not None:
            c = self.base.map_rect(clip)
            left, top = max(left, math.floor(c.left)), max(top, math.floor(c.top))
            right, bottom = min(right, math.ceil(c.right)), min(bottom, math.ceil(c.bottom))
        if right <= left or bottom <= top or opacity <= 0:
            self.layer = None
            return False
        full = transform.then(self.base)
        if (self._target_draw is not None and self.ss == 1 and opacity >= 1 and clip is None
                and full.is_axis_aligned()):
            # Fast path while panning or dragging: nothing to clip, fade or rotate, so draw
            # directly onto the target instead of compositing a layer.
            self.direct = True
            self.origin = (0, 0)
            self.layer = self.target
            self.draw = self._target_draw
            self.m = full
            self.opacity, self.clip = opacity, clip
            return True
        self.direct = False
        self.origin = (left, top)
        self.layer = Image.new("RGBA", ((right - left) * self.ss, (bottom - top) * self.ss),
                               (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.layer)
        self.m = transform.then(self.base).then(Affine(e=-left, f=-top)).then(
            Affine(a=self.ss, d=self.ss))
        self.opacity = opacity
        self.clip = clip
        return True

    def end_item(self) -> None:
        layer = self.layer
        if layer is None:
            return
        if self.direct:
            self.layer = None
            self.draw = None
            return
        if self.ss > 1:
            layer = layer.reduce(self.ss)
        if self.clip is not None:
            c = self.base.map_rect(self.clip).translated(-self.origin[0], -self.origin[1])
            mask = Image.new("L", layer.size, 0)
            ImageDraw.Draw(mask).rectangle((c.left, c.top, c.right - 1, c.bottom - 1), fill=255)
            alpha = layer.getchannel("A")
            layer.putalpha(Image.composite(alpha, mask, mask))
        if self.opacity < 1:
            alpha = layer.getchannel("A").point(lambda v: round(v * self.opacity))
            layer.putalpha(alpha)
        if self.target.mode == "RGB":
            self.target.paste(layer, self.origin, layer)
        else:
            self.target.alpha_composite(layer, self.origin)
        self.layer = None
        self.draw = None

    # -------------------------------------------------------------- primitives
    def _device(self, pts: list[Point]) -> list[tuple[float, float]]:
        m = self.m
        return [m.map_xy(p.x, p.y) for p in pts]

    def _width(self, pen: Pen) -> float:
        return max(float(self.ss), pen.width * self.m.scale_factor()) if pen.width else self.ss

    def polygon(self, pts: list[Point], pen: Pen | None, brush: str | None) -> None:
        if self.draw is None or len(pts) < 2:
            return
        device = self._device(pts)
        if brush:
            self.draw.polygon(device, fill=_rgba(brush))
        if pen is not None and pen.color:
            self._stroke(device + [device[0]], pen, caps=False)

    def polyline(self, pts: list[Point], pen: Pen) -> None:
        if self.draw is not None and pts and pen.color:
            self._stroke(self._device(pts), pen, caps=True)

    def path(self, segments: list[Segment], pen: Pen) -> None:
        if self.draw is None or not segments:
            return
        steps = max(4, min(24, round(self.m.scale_factor() * 3)))
        self.polyline(flatten(segments, steps), pen)

    def _stroke(self, device: list[tuple[float, float]], pen: Pen, caps: bool) -> None:
        assert self.draw is not None
        color = _rgba(pen.color)
        width = self._width(pen)
        if pen.dash:
            for dash in _dashes(device, pen.dash[0] * self.m.scale_factor(),
                                pen.dash[1] * self.m.scale_factor()):
                self.draw.line(dash, fill=color, width=max(1, round(width)))
            return
        w = max(1, round(width))
        if len(device) > 1:
            self.draw.line(device, fill=color, width=w, joint="curve")
        if (caps or len(device) == 1) and w > 2:
            r = width / 2
            for x, y in (device[0], device[-1]):
                self.draw.ellipse((x - r, y - r, x + r, y + r), fill=color)

    def _blit(self, r: Rect, image: Image.Image) -> None:
        """Place an image covering local rect ``r`` under the current transform."""
        assert self.layer is not None
        full = Affine(a=r.w / image.width, d=r.h / image.height, e=r.x, f=r.y).then(self.m)
        if full.is_axis_aligned() and full.a > 0 and full.d > 0:
            size = (max(1, round(image.width * full.a)), max(1, round(image.height * full.d)))
            if size != image.size:
                image = image.resize(size, Image.LANCZOS)
            left, top = round(full.e), round(full.f)
            # Only the part that lands inside the layer is composited.
            crop = (max(0, -left), max(0, -top),
                    min(image.width, self.layer.width - left),
                    min(image.height, self.layer.height - top))
            if crop[2] <= crop[0] or crop[3] <= crop[1]:
                return
            piece = image.crop(crop)
            dest = (left + crop[0], top + crop[1])
            if self.layer.mode == "RGB":
                self.layer.paste(piece, dest, piece)
            else:
                self.layer.alpha_composite(piece, dest)
            return
        data = _pil_affine(full)
        if data is not None:
            warped = image.transform(self.layer.size, Image.AFFINE, data, Image.BICUBIC)
            if self.layer.mode == "RGB":
                self.layer.paste(warped, (0, 0), warped)
            else:
                self.layer.alpha_composite(warped)

    def _text_image(self, lines: list[str], width: float, height: float, size: float,
                    weight: int, color: str, align: str, valign: str,
                    scale: float) -> Image.Image:
        w, h = max(1, math.ceil(width * scale)), max(1, math.ceil(height * scale))
        image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        f = pil_font(size * scale, weight)
        ascent, descent = f.getmetrics()
        spacing = ascent + descent
        y = 0.0
        if valign == "center":
            y = (h - spacing * len(lines)) / 2
        for line in lines:
            x = 0.0
            if align == "center":
                x = (w - f.getlength(line)) / 2
            draw.text((x, y + ascent), line, font=f, fill=_rgba(color), anchor="ls")
            y += spacing
        return image

    def text(self, r: Rect, text: str, size: float, weight: int, color: str,
             align: str = "left", valign: str = "top", wrap: bool = True) -> None:
        if self.layer is None or not text or r.w <= 0 or r.h <= 0:
            return
        scale = self.m.scale_factor()
        lines = layout_text(text, r.w, size, weight, wrap)
        # Clipped to the box, as QPainter.drawText(rect, ...) is without TextDontClip.
        image = self._text_image(lines, r.w, r.h, size, weight, color, align, valign, scale)
        self._blit(Rect(r.x, r.y, image.width / scale, image.height / scale), image)

    def text_at(self, p: Point, text: str, size: float, weight: int, color: str) -> None:
        if self.layer is None or not text:
            return
        ascent, _ = pil_font(size, weight).getmetrics()
        width = text_width(text, size, weight) + 2
        self.text(Rect(p.x, p.y - ascent, width, line_spacing(size, weight)), text, size,
                  weight, color, wrap=False)

    def image(self, r: Rect, image: Image.Image | None, data: str) -> None:
        if self.layer is not None and image is not None and r.w > 0 and r.h > 0:
            self._blit(r, image)


def _dashes(
    pts: list[tuple[float, float]], on: float, off: float
) -> list[list[tuple[float, float]]]:
    on, off = max(on, 1.0), max(off, 1.0)
    out: list[list[tuple[float, float]]] = []
    drawing, left = True, on
    current: list[tuple[float, float]] = [pts[0]] if pts else []
    for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
        length = math.hypot(x2 - x1, y2 - y1)
        pos = 0.0
        while length - pos > left:
            pos += left
            t = pos / length
            point = (x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
            if drawing:
                current.append(point)
                out.append(current)
            current = [point]
            drawing = not drawing
            left = on if drawing else off
        left -= length - pos
        if drawing:
            current.append((x2, y2))
    if drawing and len(current) > 1:
        out.append(current)
    return out


def render_scene(items: list[Any], size: tuple[int, int], base: Affine, background: str | None,
                 supersample: int = 2) -> Image.Image:
    """Paint items (already in z order) into a fresh image."""
    image = Image.new("RGBA", size, _rgba(background) if background else (0, 0, 0, 0))
    painter = RasterPainter(image, base, supersample)
    for item in items:
        item.paint_with(painter)
    return image


__all__ = ["Pen", "Painter", "RasterPainter", "layout_text", "line_spacing", "render_scene"]
