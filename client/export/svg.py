"""SVG export: vector shapes and real, selectable text, written directly as SVG elements."""

from __future__ import annotations

from collections.abc import Generator
from xml.sax.saxutils import escape

from PIL import Image

from client import theme
from client.canvas.paint import Painter, Pen, Segment, layout_text, line_spacing, pil_font
from client.canvas.scene import BoardScene
from client.export.renderer import Scope, bounds_of, collect_items, paint_items
from client.geom import Affine, Point, Rect


def _num(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def color_parts(color: str) -> tuple[str, float]:
    """(#RRGGBB, opacity): 8-digit colors become a solid color plus an opacity attribute."""
    r, g, b = theme.rgb(color)
    value = color.lstrip("#")
    alpha = int(value[6:8], 16) / 255 if len(value) >= 8 else 1.0
    return f"#{r:02X}{g:02X}{b:02X}", alpha


def image_mime(data: str) -> str:
    return "image/jpeg" if data.startswith("/9j/") else "image/png"


class SvgPainter(Painter):
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.defs: list[str] = []
        self._clips = 0
        self._open = 0

    def begin_item(self, transform: Affine, opacity: float, bounds: Rect,
                   clip: Rect | None) -> bool:
        self._open = 0
        if clip is not None:
            clip_id = f"clip{self._clips}"
            self._clips += 1
            self.defs.append(
                f'<clipPath id="{clip_id}"><rect x="{_num(clip.x)}" y="{_num(clip.y)}" '
                f'width="{_num(clip.w)}" height="{_num(clip.h)}"/></clipPath>'
            )
            self.parts.append(f'<g clip-path="url(#{clip_id})">')
            self._open += 1
        t = transform
        matrix = " ".join(_num(v) for v in (t.a, t.b, t.c, t.d, t.e, t.f))
        extra = f' opacity="{_num(opacity)}"' if opacity < 1 else ""
        self.parts.append(f'<g transform="matrix({matrix})"{extra}>')
        self._open += 1
        return True

    def end_item(self) -> None:
        self.parts.append("</g>" * self._open)
        self._open = 0

    def _paint_attrs(self, pen: Pen | None, brush: str | None) -> str:
        attrs = []
        if brush:
            color, alpha = color_parts(brush)
            attrs.append(f'fill="{color}"')
            if alpha < 1:
                attrs.append(f'fill-opacity="{_num(alpha)}"')
        else:
            attrs.append('fill="none"')
        if pen is not None and pen.color:
            color, alpha = color_parts(pen.color)
            attrs.append(f'stroke="{color}" stroke-width="{_num(pen.width or 1)}" '
                         'stroke-linecap="round" stroke-linejoin="round"')
            if alpha < 1:
                attrs.append(f'stroke-opacity="{_num(alpha)}"')
            if pen.dash:
                width = pen.width or 1
                attrs.append(f'stroke-dasharray="{_num(pen.dash[0] * width)} '
                             f'{_num(pen.dash[1] * width)}"')
        return " ".join(attrs)

    def polygon(self, pts: list[Point], pen: Pen | None, brush: str | None) -> None:
        if len(pts) < 2:
            return
        d = "M" + " L".join(f"{_num(p.x)} {_num(p.y)}" for p in pts) + " Z"
        self.parts.append(f'<path d="{d}" {self._paint_attrs(pen, brush)}/>')

    def polyline(self, pts: list[Point], pen: Pen) -> None:
        if pts:
            d = "M" + " L".join(f"{_num(p.x)} {_num(p.y)}" for p in pts)
            self.parts.append(f'<path d="{d}" {self._paint_attrs(pen, None)}/>')

    def path(self, segments: list[Segment], pen: Pen) -> None:
        d = []
        for seg in segments:
            if seg[0] == "Q":
                d.append("Q" + " ".join(_num(v) for v in seg[1:]))
            else:
                d.append(f"{seg[0]}{_num(seg[1])} {_num(seg[2])}")
        if d:
            self.parts.append(f'<path d="{" ".join(d)}" {self._paint_attrs(pen, None)}/>')

    def _text_lines(self, lines: list[str], x: float, top: float, size: float, weight: int,
                    color: str, anchor: str) -> None:
        ascent, _ = pil_font(size, weight).getmetrics()
        spacing = line_spacing(size, weight)
        fill, alpha = color_parts(color)
        opacity = f' fill-opacity="{_num(alpha)}"' if alpha < 1 else ""
        spans = "".join(
            f'<tspan x="{_num(x)}" y="{_num(top + ascent + i * spacing)}">{escape(line)}</tspan>'
            for i, line in enumerate(lines)
        )
        self.parts.append(
            f'<text font-family="{theme.FONT_FAMILY}, sans-serif" font-size="{_num(size)}" '
            f'font-weight="{weight}" fill="{fill}"{opacity} text-anchor="{anchor}" '
            f'xml:space="preserve">{spans}</text>'
        )

    def text(self, r: Rect, text: str, size: float, weight: int, color: str,
             align: str = "left", valign: str = "top", wrap: bool = True) -> None:
        if not text:
            return
        lines = layout_text(text, r.w, size, weight, wrap)
        top = r.y
        if valign == "center":
            top = r.y + (r.h - len(lines) * line_spacing(size, weight)) / 2
        if align == "center":
            self._text_lines(lines, r.x + r.w / 2, top, size, weight, color, "middle")
        else:
            self._text_lines(lines, r.x, top, size, weight, color, "start")

    def text_at(self, p: Point, text: str, size: float, weight: int, color: str) -> None:
        ascent, _ = pil_font(size, weight).getmetrics()
        self._text_lines([text], p.x, p.y - ascent, size, weight, color, "start")

    def image(self, r: Rect, image: Image.Image | None, data: str) -> None:
        if data:
            self.parts.append(
                f'<image x="{_num(r.x)}" y="{_num(r.y)}" width="{_num(r.w)}" '
                f'height="{_num(r.h)}" preserveAspectRatio="none" '
                f'href="data:{image_mime(data)};base64,{data}"/>'
            )


def export_svg(
    scene: BoardScene, scope: Scope, title: str = "LANvas"
) -> Generator[int, None, bytes]:
    items = collect_items(scene, scope)
    rect = bounds_of(items)
    painter = SvgPainter()
    yield from paint_items(painter, items)
    width, height = round(rect.w), round(rect.h)
    head = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="{width}" '
        f'height="{height}" viewBox="0 0 {_num(rect.w)} {_num(rect.h)}">\n'
        f"<title>{escape(title)}</title>\n<desc>Made with LANvas</desc>\n"
    )
    defs = f"<defs>{''.join(painter.defs)}</defs>\n" if painter.defs else ""
    body = (
        f'<g transform="translate({_num(-rect.x)} {_num(-rect.y)})">\n'
        + "\n".join(painter.parts)
        + "\n</g>\n</svg>\n"
    )
    return (head + defs + body).encode("utf-8")


