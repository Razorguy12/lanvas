"""Monochrome 16 px line icons drawn in the Lucide style, as inline SVG with no external files."""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

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


def svg(name: str, color: str) -> bytes:
    body = _PATHS[name]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    ).encode()


def pixmap(name: str, color: str = "#1E1E1E", size: int = 32) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg(name, color)))
    image = QPixmap(size, size)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image


def icon(name: str, color: str = "#1E1E1E", checked_color: str = "#FFFFFF") -> QIcon:
    result = QIcon()
    result.addPixmap(pixmap(name, color), QIcon.Mode.Normal, QIcon.State.Off)
    result.addPixmap(pixmap(name, checked_color), QIcon.Mode.Normal, QIcon.State.On)
    # A selected row is accent-colored, where a muted or purple icon would simply disappear.
    result.addPixmap(pixmap(name, checked_color), QIcon.Mode.Selected, QIcon.State.Off)
    return result


ICON_SIZE = QSize(16, 16)
__all__ = ["ICON_SIZE", "Qt", "icon", "pixmap", "svg"]
