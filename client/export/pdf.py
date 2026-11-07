"""PDF export: vector output, either fit to content or one page per frame.

A small PDF 1.4 writer: shapes become path operators, text stays real text in the embedded Inter
font (so it renders the same anywhere and remains selectable), and images become image XObjects.
"""

from __future__ import annotations

import io
import struct
import zlib
from collections.abc import Generator

from PIL import Image

from client import theme
from client.canvas.items import BoardItem
from client.canvas.paint import Painter, Pen, Segment, layout_text, line_spacing, pil_font
from client.canvas.scene import BoardScene
from client.export.renderer import SCOPE_FRAME, Scope, bounds_of, collect_items, paint_items
from client.geom import Affine, Point, Rect

PAGE_FIT = "fit"
PAGE_A4 = "A4"
PAGE_A3 = "A3"
RESOLUTION = 96  # dots per inch, so one board unit is one device dot on a fitted page
POINTS_PER_UNIT = 72.0 / RESOLUTION
PAPER = {PAGE_A4: (841.89, 595.28), PAGE_A3: (1190.55, 841.89)}  # landscape, in points


def _n(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _rgb(color: str) -> tuple[str, float]:
    r, g, b = theme.rgb(color)
    value = color.lstrip("#")
    alpha = int(value[6:8], 16) / 255 if len(value) >= 8 else 1.0
    return f"{_n(r / 255)} {_n(g / 255)} {_n(b / 255)}", alpha


def _pdf_string(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


# ---------------------------------------------------------------------- font embedding
def _ttf_metrics(data: bytes) -> dict[str, int]:
    """unitsPerEm, bounding box, ascent and descent from the font's own tables."""
    tables = {}
    count = struct.unpack(">H", data[4:6])[0]
    for i in range(count):
        tag, _, offset, length = struct.unpack(">4sIII", data[12 + 16 * i: 28 + 16 * i])
        tables[tag] = (offset, length)
    head = tables[b"head"][0]
    units = struct.unpack(">H", data[head + 18: head + 20])[0]
    xmin, ymin, xmax, ymax = struct.unpack(">hhhh", data[head + 36: head + 44])
    hhea = tables[b"hhea"][0]
    ascent, descent = struct.unpack(">hh", data[hhea + 4: hhea + 8])
    k = 1000 / units
    return {"bbox": [round(xmin * k), round(ymin * k), round(xmax * k), round(ymax * k)],
            "ascent": round(ascent * k), "descent": round(descent * k)}


class PdfWriter:
    def __init__(self, title: str) -> None:
        self.objects: list[bytes | None] = [None]  # object 0 is the free entry
        self.pages: list[int] = []
        self.fonts: dict[int, int] = {}  # weight -> font object number
        self.states: dict[tuple[float, float], str] = {}
        self.images: list[int] = []
        self.title = title
        self.pages_ref = self.reserve()

    def reserve(self) -> int:
        self.objects.append(None)
        return len(self.objects) - 1

    def set(self, ref: int, body: bytes) -> None:
        self.objects[ref] = body

    def add(self, body: bytes) -> int:
        self.objects.append(body)
        return len(self.objects) - 1

    def stream(self, data: bytes, extra: str = "", compress: bool = True) -> int:
        if compress:
            data = zlib.compress(data)
            extra += " /Filter /FlateDecode"
        return self.add(f"<< /Length {len(data)}{extra} >>\nstream\n".encode() + data
                        + b"\nendstream")

    def font(self, weight: int) -> str:
        path = theme.font_path(weight)
        best = min(theme.FONT_FILES, key=lambda w: abs(w - weight))
        if best not in self.fonts:
            data = path.read_bytes()
            metrics = _ttf_metrics(data)
            name = path.stem  # e.g. Inter-SemiBold: the PDF names the bundled font itself
            font_file = self.stream(data, f" /Length1 {len(data)}")
            descriptor = self.add(
                f"<< /Type /FontDescriptor /FontName /{name} /Flags 32 "
                f"/FontBBox [{' '.join(map(str, metrics['bbox']))}] /ItalicAngle 0 "
                f"/Ascent {metrics['ascent']} /Descent {metrics['descent']} "
                f"/CapHeight {metrics['ascent']} /StemV {80 + (best - 400) // 3} "
                f"/FontFile2 {font_file} 0 R >>".encode()
            )
            font = pil_font(1000, best)
            widths = []
            for code in range(32, 256):
                try:
                    char = bytes([code]).decode("cp1252")
                    widths.append(str(round(font.getlength(char))))
                except UnicodeDecodeError:
                    widths.append("0")
            self.fonts[best] = self.add(
                f"<< /Type /Font /Subtype /TrueType /BaseFont /{name} /FirstChar 32 "
                f"/LastChar 255 /Widths [{' '.join(widths)}] /Encoding /WinAnsiEncoding "
                f"/FontDescriptor {descriptor} 0 R >>".encode()
            )
        return f"F{best}"

    def gstate(self, fill: float, stroke: float) -> str:
        key = (round(fill, 3), round(stroke, 3))
        if key not in self.states:
            self.states[key] = f"GS{len(self.states)}"
        return self.states[key]

    def image(self, image: Image.Image) -> str:
        rgba = image.convert("RGBA")
        rgb = rgba.convert("RGB")
        width, height = rgba.size
        smask = ""
        alpha = rgba.getchannel("A")
        if alpha.getextrema() != (255, 255):
            mask = self.stream(alpha.tobytes(), f" /Type /XObject /Subtype /Image /Width {width} "
                               f"/Height {height} /ColorSpace /DeviceGray /BitsPerComponent 8")
            smask = f" /SMask {mask} 0 R"
        ref = self.stream(rgb.tobytes(), f" /Type /XObject /Subtype /Image /Width {width} "
                          f"/Height {height} /ColorSpace /DeviceRGB /BitsPerComponent 8{smask}")
        self.images.append(ref)
        return f"Im{len(self.images) - 1}"

    def add_page(self, width: float, height: float, content: bytes) -> None:
        stream = self.stream(content)
        page = self.reserve()
        self.pages.append(page)
        self.set(page, (
            f"<< /Type /Page /Parent {self.pages_ref} 0 R "
            f"/MediaBox [0 0 {_n(width)} {_n(height)}] /Contents {stream} 0 R "
            f"/Resources RESOURCES >>"
        ).encode())

    def finish(self) -> bytes:
        resources = self.add(self._resources())
        for ref in self.pages:
            body = self.objects[ref] or b""
            self.objects[ref] = body.replace(b"RESOURCES", f"{resources} 0 R".encode())
        kids = " ".join(f"{p} 0 R" for p in self.pages)
        self.set(self.pages_ref, f"<< /Type /Pages /Kids [{kids}] /Count {len(self.pages)} >>"
                 .encode())
        catalog = self.add(f"<< /Type /Catalog /Pages {self.pages_ref} 0 R >>".encode())
        info = self.add(b"<< /Title " + _pdf_string(self.title) + b" /Creator (LANvas) "
                        b"/Producer (LANvas) >>")
        out = io.BytesIO()
        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for number, body in enumerate(self.objects[1:], start=1):
            offsets.append(out.tell())
            out.write(f"{number} 0 obj\n".encode() + (body or b"null") + b"\nendobj\n")
        xref = out.tell()
        out.write(f"xref\n0 {len(self.objects)}\n0000000000 65535 f \n".encode())
        for offset in offsets[1:]:
            out.write(f"{offset:010d} 00000 n \n".encode())
        out.write(f"trailer\n<< /Size {len(self.objects)} /Root {catalog} 0 R "
                  f"/Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        return out.getvalue()

    def _resources(self) -> bytes:
        fonts = " ".join(f"/F{w} {ref} 0 R" for w, ref in self.fonts.items())
        states = " ".join(f"/{name} << /ca {_n(fill)} /CA {_n(stroke)} >>"
                          for (fill, stroke), name in self.states.items())
        images = " ".join(f"/Im{i} {ref} 0 R" for i, ref in enumerate(self.images))
        return (f"<< /ProcSet [/PDF /Text /ImageC] /Font << {fonts} >> "
                f"/ExtGState << {states} >> /XObject << {images} >> >>").encode()


class PdfPainter(Painter):
    """Writes one page's content stream in board units, y pointing down."""

    def __init__(self, writer: PdfWriter) -> None:
        self.writer = writer
        self.ops: list[str] = []
        self.opacity = 1.0

    def begin_item(self, transform: Affine, opacity: float, bounds: Rect,
                   clip: Rect | None) -> bool:
        self.ops.append("q")
        if clip is not None:
            self.ops.append(f"{_n(clip.x)} {_n(clip.y)} {_n(clip.w)} {_n(clip.h)} re W n")
        t = transform
        self.ops.append(" ".join(_n(v) for v in (t.a, t.b, t.c, t.d, t.e, t.f)) + " cm")
        self.opacity = opacity
        return True

    def end_item(self) -> None:
        self.ops.append("Q")

    def _style(self, pen: Pen | None, brush: str | None) -> str | None:
        fill_alpha = stroke_alpha = 1.0
        if brush:
            color, fill_alpha = _rgb(brush)
            self.ops.append(f"{color} rg")
        if pen is not None and pen.color:
            color, stroke_alpha = _rgb(pen.color)
            self.ops.append(f"{color} RG {_n(pen.width or 1)} w 1 J 1 j")
            if pen.dash:
                width = pen.width or 1
                self.ops.append(f"[{_n(pen.dash[0] * width)} {_n(pen.dash[1] * width)}] 0 d")
            else:
                self.ops.append("[] 0 d")
        name = self.writer.gstate(fill_alpha * self.opacity, stroke_alpha * self.opacity)
        self.ops.append(f"/{name} gs")
        stroke = pen is not None and bool(pen.color)
        if brush and stroke:
            return "B"
        if brush:
            return "f"
        return "S" if stroke else None

    def _trace(self, pts: list[Point]) -> str:
        first, *rest = pts
        return " ".join([f"{_n(first.x)} {_n(first.y)} m"] + [f"{_n(p.x)} {_n(p.y)} l"
                                                               for p in rest])

    def polygon(self, pts: list[Point], pen: Pen | None, brush: str | None) -> None:
        if len(pts) < 2:
            return
        op = self._style(pen, brush)
        if op:
            self.ops.append(f"{self._trace(pts)} h {op}")

    def polyline(self, pts: list[Point], pen: Pen) -> None:
        if pts and self._style(pen, None):
            self.ops.append(f"{self._trace(pts)} S")

    def path(self, segments: list[Segment], pen: Pen) -> None:
        if not segments or not self._style(pen, None):
            return
        parts, x, y = [], 0.0, 0.0
        for seg in segments:
            if seg[0] == "M":
                x, y = seg[1], seg[2]
                parts.append(f"{_n(x)} {_n(y)} m")
            elif seg[0] == "L":
                x, y = seg[1], seg[2]
                parts.append(f"{_n(x)} {_n(y)} l")
            else:  # quadratic to cubic: control points two thirds of the way to the quad's
                qx, qy, ex, ey = seg[1:]
                c1 = (x + 2 / 3 * (qx - x), y + 2 / 3 * (qy - y))
                c2 = (ex + 2 / 3 * (qx - ex), ey + 2 / 3 * (qy - ey))
                parts.append(" ".join(_n(v) for v in (*c1, *c2, ex, ey)) + " c")
                x, y = ex, ey
        self.ops.append(" ".join(parts) + " S")

    def _lines(self, lines: list[str], x: float, top: float, size: float, weight: int,
               color: str, width: float | None) -> None:
        font = self.writer.font(weight)
        fill, alpha = _rgb(color)
        name = self.writer.gstate(alpha * self.opacity, alpha * self.opacity)
        ascent, _ = pil_font(size, weight).getmetrics()
        spacing = line_spacing(size, weight)
        self.ops.append(f"/{name} gs {fill} rg BT /{font} {_n(size)} Tf")
        for i, line in enumerate(lines):
            lx = x if width is None else x + (width - pil_font(size, weight).getlength(line)) / 2
            # The page is y-down, so each line's text matrix flips glyphs back upright.
            self.ops.append(f"1 0 0 -1 {_n(lx)} {_n(top + ascent + i * spacing)} Tm "
                            + _pdf_string(line).decode("latin-1") + " Tj")
        self.ops.append("ET")

    def text(self, r: Rect, text: str, size: float, weight: int, color: str,
             align: str = "left", valign: str = "top", wrap: bool = True) -> None:
        if not text:
            return
        lines = layout_text(text, r.w, size, weight, wrap)
        top = r.y
        if valign == "center":
            top = r.y + (r.h - len(lines) * line_spacing(size, weight)) / 2
        self.ops.append(f"q {_n(r.x)} {_n(r.y)} {_n(r.w)} {_n(r.h)} re W n")
        self._lines(lines, r.x, top, size, weight, color, r.w if align == "center" else None)
        self.ops.append("Q")

    def text_at(self, p: Point, text: str, size: float, weight: int, color: str) -> None:
        ascent, _ = pil_font(size, weight).getmetrics()
        self._lines([text], p.x, p.y - ascent, size, weight, color, None)

    def image(self, r: Rect, image: Image.Image | None, data: str) -> None:
        if image is None:
            return
        name = self.writer.image(image)
        gs = self.writer.gstate(self.opacity, self.opacity)
        # Image space is a unit square with y up; map it onto the rectangle in y-down space.
        self.ops.append(f"q /{gs} gs {_n(r.w)} 0 0 {_n(-r.h)} {_n(r.x)} {_n(r.y + r.h)} cm "
                        f"/{name} Do Q")


def _page_geometry(rect: Rect, page: str) -> tuple[float, float, float]:
    """(page width, page height, scale from board units to points)."""
    if page == PAGE_FIT:
        return rect.w * POINTS_PER_UNIT, rect.h * POINTS_PER_UNIT, POINTS_PER_UNIT
    width, height = PAPER[page]
    return width, height, min(width / rect.w, height / rect.h)


def frames_in(scene: BoardScene, scope: Scope) -> list[BoardItem]:
    frames = [i for i in collect_items(scene, scope) if i.obj["type"] == "frame"]
    frames.sort(key=lambda i: (i.y(), i.x(), i.obj_id))
    return frames


def export_pdf(
    scene: BoardScene,
    scope: Scope,
    page: str = PAGE_FIT,
    each_frame: bool = False,
    title: str = "LANvas",
) -> Generator[int, None, bytes]:
    sections: list[tuple[list[BoardItem], Rect]] = []
    if each_frame:
        for frame in frames_in(scene, scope):
            inner = Scope(SCOPE_FRAME, frame_id=frame.obj_id)
            sections.append((collect_items(scene, inner), frame.sceneBoundingRect()))
    if not sections:
        items = collect_items(scene, scope)
        sections = [(items, bounds_of(items))]
    writer = PdfWriter(title)
    done = 0
    for items, rect in sections:
        width, height, scale = _page_geometry(rect, page)
        painter = PdfPainter(writer)
        # Flip to y-down, then board units to points with the section's top-left at the origin.
        painter.ops.append(f"1 0 0 -1 0 {_n(height)} cm")
        painter.ops.append(f"{_n(scale)} 0 0 {_n(scale)} {_n(-rect.x * scale)} "
                           f"{_n(-rect.y * scale)} cm")
        for count in paint_items(painter, items):
            yield done + count
        done += len(items)
        writer.add_page(width, height, "\n".join(painter.ops).encode("latin-1"))
    return writer.finish()
