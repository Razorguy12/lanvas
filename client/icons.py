"""Monochrome 16 px line icons drawn in the Lucide style, from inline SVG path data.

The paths are rasterized here with a tiny path flattener and Pillow, supersampled for smooth
edges, so there are no icon files and no SVG library involved.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw

_PATHS = {
    "select": '<path d="M4 3l7 18 2.5-7.5L21 11z"/>',
    "frame": '<rect x="3" y="6" width="18" height="15" rx="1"/><path d="M3 3h6"/>',
    "rect": '<rect x="4" y="5" width="16" height="14" rx="1"/>',
    "ellipse": '<circle cx="12" cy="12" r="8.5"/>',
    "line": '<path d="M5 19L19 5"/>',
    "arrow": '<path d="M5 19L19 5M10 5h9v9"/>',
    "pen": '<path d="M12 19l7-7 3 3-7 7-3-3zM18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5zM2 2l7.5 7.5"/>',
    "eraser": '<path d="M7 21L3 17a2 2 0 010-3L14 3a2 2 0 013 0l4 4a2 2 0 010 3L10 21zM22 21H7M9 8l7 7"/>',
    "text": '<path d="M4 7V4h16v3M9 20h6M12 4v16"/>',
    "sticky": '<path d="M4 4h16v10l-6 6H4zM14 20v-6h6"/>',
    "comment": '<path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/>'
    '<path d="M21 15l-5-5L5 21"/>',
    "hand": '<path d="M18 11V6a2 2 0 00-4 0M14 10V4a2 2 0 00-4 0v2M10 10.5V6a2 2 0 00-4 0v8M18 8a2 2 '
    '0 014 0v6a8 8 0 01-8 8h-2c-2.8 0-4.5-.9-6-2.3L2.4 16a2 2 0 013-2.6L8 16"/>',
    "lock": '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/>',
    "unlock": '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 017.5-2"/>',
    "eye": '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8S1 12 1 12z"/><circle cx="12" cy="12" r="3"/>',
    "eyeoff": '<path d="M3 3l18 18M10 5a10 10 0 0113 7 16 16 0 01-3 4M6 6a16 16 0 00-5 6s4 8 11 8a10 10 '
    '0 005-1.5"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/>',
    "menu": '<path d="M3 6h18M3 12h18M3 18h18"/>',
    "refresh": '<path d="M21 12a9 9 0 11-3-6.7L21 8M21 3v5h-5"/>',
    "undo": '<path d="M3 7v6h6M3 13a9 9 0 103-7"/>',
    "redo": '<path d="M21 7v6h-6M21 13a9 9 0 11-3-7"/>',
    "users": '<circle cx="9" cy="8" r="4"/><path d="M2 21v-2a5 5 0 015-5h4a5 5 0 015 5v2M16 4a4 4 0 010 '
    '8M22 21v-2a5 5 0 00-3-4.5"/>',
    "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "share": '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/>'
    '<path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/>',
    "new": '<path d="M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/>',
    "alignleft": '<path d="M4 3v18M8 7h10v4H8zM8 14h6v4H8z"/>',
    "alignright": '<path d="M20 3v18M6 7h10v4H6zM10 14h6v4h-6z"/>',
    "aligntop": '<path d="M3 4h18M7 8v10h4V8zM14 8v6h4V8z"/>',
    "alignbottom": '<path d="M3 20h18M7 6v10h4V6zM14 10v6h4v-6z"/>',
    "alignh": '<path d="M12 3v18M7 7h10v4H7zM9 14h6v4H9z"/>',
    "alignv": '<path d="M3 12h18M7 7v10h4V7zM14 9v6h4V9z"/>',
    "palette": '<circle cx="12" cy="12" r="9"/><circle cx="8" cy="10" r="1"/><circle cx="12" cy="7.5" '
    'r="1"/><circle cx="16" cy="10" r="1"/>',
}
STROKE = 1.8
SUPERSAMPLE = 4
ICON_SIZE = 16


def svg(name: str, color: str) -> bytes:
    """The icon as a standalone SVG document (kept for export and for tests)."""
    body = _PATHS[name]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="{STROKE}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    ).encode()


# ---------------------------------------------------------------------- SVG geometry
_NUMBER = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE]-?\d+)?")
_ATTR = re.compile(r'(\w+)="([^"]*)"')


def _arc(x1, y1, rx, ry, phi, large, sweep, x2, y2, steps=16):  # noqa: ANN001, ANN202
    """SVG endpoint arc to points (SVG spec, appendix F.6)."""
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    phi = math.radians(phi)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    rx, ry = abs(rx), abs(ry)
    lam = x1p**2 / rx**2 + y1p**2 / ry**2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx**2 * ry**2 - rx**2 * y1p**2 - ry**2 * x1p**2
    den = rx**2 * y1p**2 + ry**2 * x1p**2
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def angle(ux, uy, vx, vy):  # noqa: ANN001, ANN202
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    pts = []
    for i in range(1, steps + 1):
        t = t1 + dt * i / steps
        x, y = rx * math.cos(t), ry * math.sin(t)
        pts.append((cp * x - sp * y + cx, sp * x + cp * y + cy))
    return pts


def _cubic(p0, p1, p2, p3, steps=12):  # noqa: ANN001, ANN202
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        out.append((
            u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
        ))
    return out


def path_polylines(d: str) -> list[tuple[list[tuple[float, float]], bool]]:
    """Flatten SVG path data into (points, closed) polylines."""
    out: list[tuple[list[tuple[float, float]], bool]] = []
    current: list[tuple[float, float]] = []
    x = y = sx = sy = 0.0
    last_ctrl: tuple[float, float] | None = None
    cmd = ""
    pos = 0

    def skip() -> None:
        nonlocal pos
        while pos < len(d) and d[pos] in " ,\t\n":
            pos += 1

    def num() -> float:
        nonlocal pos
        skip()
        match = _NUMBER.match(d, pos)
        if match is None:
            raise ValueError(f"bad path data at {pos}: {d!r}")
        pos = match.end()
        return float(match.group())

    def flag() -> bool:
        """Arc flags are single digits and are often packed together, as in ``a2 2 0 010-3``."""
        nonlocal pos
        skip()
        pos += 1
        return d[pos - 1] == "1"

    while True:
        skip()
        if pos >= len(d):
            break
        if d[pos].isalpha():
            cmd = d[pos]
            pos += 1
            if cmd in "Zz":
                if current:
                    out.append((current, True))
                current = [(sx, sy)]  # a later lineto starts where the subpath began
                x, y = sx, sy
                last_ctrl = None
                continue
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            if len(current) > 1:
                out.append((current, False))
            current = [(x, y)]
            sx, sy = x, y
            cmd = "l" if rel else "L"  # extra pairs after a moveto are linetos
            last_ctrl = None
        elif c == "L":
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            current.append((x, y))
            last_ctrl = None
        elif c == "H":
            nx = num()
            x = x + nx if rel else nx
            current.append((x, y))
            last_ctrl = None
        elif c == "V":
            ny = num()
            y = y + ny if rel else ny
            current.append((x, y))
            last_ctrl = None
        elif c in "CS":
            if c == "C":
                c1 = (num(), num())
                if rel:
                    c1 = (x + c1[0], y + c1[1])
            else:
                c1 = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_ctrl else (x, y)
            c2, end = (num(), num()), (num(), num())
            if rel:
                c2, end = (x + c2[0], y + c2[1]), (x + end[0], y + end[1])
            current += _cubic((x, y), c1, c2, end)
            last_ctrl = c2
            x, y = end
        elif c == "A":
            rx, ry, phi = num(), num(), num()
            large, sweep = flag(), flag()
            nx, ny = num(), num()
            if rel:
                nx, ny = x + nx, y + ny
            current += _arc(x, y, rx, ry, phi, bool(large), bool(sweep), nx, ny)
            x, y = nx, ny
            last_ctrl = None
        else:
            raise ValueError(f"unsupported path command {cmd!r}")
    if len(current) > 1:
        out.append((current, False))
    return out


def _shapes(body: str) -> list[tuple[list[tuple[float, float]], bool]]:
    shapes: list[tuple[list[tuple[float, float]], bool]] = []
    for tag, attrs in re.findall(r"<(\w+)([^>]*)/>", body):
        a = {k: v for k, v in _ATTR.findall(attrs)}
        if tag == "path":
            shapes += path_polylines(a["d"])
        elif tag == "circle":
            cx, cy, r = float(a["cx"]), float(a["cy"]), float(a["r"])
            pts = [(cx + r * math.cos(t / 32 * math.tau), cy + r * math.sin(t / 32 * math.tau))
                   for t in range(32)]
            shapes.append((pts, True))
        elif tag == "rect":
            x, y = float(a["x"]), float(a["y"])
            w, h, rx = float(a["width"]), float(a["height"]), float(a.get("rx", 0))
            shapes.append((rounded_rect_points(x, y, w, h, rx), True))
    return shapes


def rounded_rect_points(x: float, y: float, w: float, h: float, r: float, steps: int = 6):  # noqa: ANN201
    r = max(0.0, min(r, w / 2, h / 2))
    if r == 0:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    pts = []
    for cx, cy, start in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0),
                          (x + r, y + h - r, 90), (x + r, y + r, 180)):
        for i in range(steps + 1):
            t = math.radians(start + 90 * i / steps)
            pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
    return pts


@lru_cache(maxsize=512)
def image(name: str, color: str = "#1E1E1E", size: int = ICON_SIZE) -> Image.Image:
    """The icon as an RGBA Pillow image, ``size`` pixels square."""
    big = size * SUPERSAMPLE
    k = big / 24
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    width = max(1, round(STROKE * k))
    r = width / 2
    for points, closed in _shapes(_PATHS[name]):
        pts = [(px * k, py * k) for px, py in points]
        if closed and pts:
            pts.append(pts[0])
        if len(pts) > 1:
            draw.line(pts, fill=color, width=width, joint="curve")
        for px, py in (pts[0], pts[-1]) if pts else ():
            draw.ellipse((px - r, py - r, px + r, py + r), fill=color)
    return canvas.resize((size, size), Image.LANCZOS)


def photo(name: str, color: str = "#1E1E1E", size: int = ICON_SIZE, master: Any = None) -> Any:
    """A Tk PhotoImage, cached on its Tk root. Tk images belong to one interpreter, so each root
    (each window session, or each test) gets its own cache that lives exactly as long as it."""
    import tkinter

    from PIL import ImageTk

    root = master._root() if master is not None else tkinter._get_default_root()
    cache = root.__dict__.setdefault("_lanvas_icons", {})
    key = (name, color, size)
    if key not in cache:
        cache[key] = ImageTk.PhotoImage(image(name, color, size), master=root)
    return cache[key]


def icon(name: str, color: str = "#1E1E1E", checked_color: str = "#FFFFFF",
         master: Any = None) -> tuple[Any, Any]:
    """(normal, selected) images, as a checkable tool button needs both."""
    return photo(name, color, master=master), photo(name, checked_color, master=master)


__all__ = ["ICON_SIZE", "icon", "image", "photo", "svg"]
