"""SVG export: vector shapes and real text, through QSvgGenerator."""

from __future__ import annotations

from collections.abc import Generator

from PyQt6.QtCore import QBuffer, QIODevice, QPointF, QRectF, QSize
from PyQt6.QtGui import QPainter, QTransform
from PyQt6.QtSvg import QSvgGenerator

from client.canvas.scene import BoardScene
from client.export.renderer import Scope, bounds_of, collect_items, paint_items


def export_svg(
    scene: BoardScene, scope: Scope, title: str = "LANvas"
) -> Generator[int, None, bytes]:
    items = collect_items(scene, scope)
    rect = bounds_of(items)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    generator = QSvgGenerator()
    generator.setOutputDevice(buffer)
    generator.setSize(QSize(round(rect.width()), round(rect.height())))
    generator.setViewBox(QRectF(QPointF(0, 0), rect.size()))
    generator.setTitle(title)
    painter = QPainter(generator)
    base = QTransform()
    base.translate(-rect.x(), -rect.y())
    yield from paint_items(painter, items, base)
    painter.end()
    return bytes(buffer.data())
