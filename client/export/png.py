"""PNG and JPEG export: paint into a QImage, then encode to bytes."""

from __future__ import annotations

from collections.abc import Generator

from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QTransform

from client.canvas.scene import BoardScene
from client.export.renderer import (
    MAX_RASTER_SIDE,
    ExportError,
    Scope,
    bounds_of,
    collect_items,
    paint_items,
)

JPEG_QUALITY = 90


def raster_size(scene: BoardScene, scope: Scope, scale: float) -> tuple[int, int]:
    rect = bounds_of(collect_items(scene, scope))
    return round(rect.width() * scale), round(rect.height() * scale)


def export_raster(
    scene: BoardScene,
    scope: Scope,
    fmt: str = "PNG",
    scale: float = 1.0,
    transparent: bool = False,
    background: str = "#FFFFFF",
) -> Generator[int, None, bytes]:
    items = collect_items(scene, scope)
    rect = bounds_of(items)
    width, height = round(rect.width() * scale), round(rect.height() * scale)
    if max(width, height) > MAX_RASTER_SIDE:
        raise ExportError(
            f"That would be a {width} x {height} px image. Pick a smaller scale, or use PDF or SVG."
        )
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    if image.isNull():
        raise ExportError("Not enough memory for an image that size. Try a smaller scale.")
    # JPEG has no alpha channel, so "transparent" would come out black if it were honored there.
    if transparent and fmt == "PNG":
        image.fill(Qt.GlobalColor.transparent)
    else:
        image.fill(QColor(background))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    base = QTransform()
    base.scale(scale, scale)
    base.translate(-rect.x(), -rect.y())
    yield from paint_items(painter, items, base)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, fmt, JPEG_QUALITY if fmt == "JPEG" else -1):
        raise ExportError(f"Could not encode the image as {fmt}.")
    return bytes(buffer.data())
