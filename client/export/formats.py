"""PNG, JPEG, SVG, and PDF export adapters over the shared renderer."""

from __future__ import annotations

from collections.abc import Generator

from PyQt6.QtCore import QBuffer, QIODevice, QMarginsF, QPointF, QRectF, QSize, QSizeF, Qt
from PyQt6.QtGui import (
    QColor,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QTransform,
)
from PyQt6.QtSvg import QSvgGenerator

from client.canvas.items import BoardItem
from client.canvas.scene import BoardScene
from client.export.renderer import (
    MAX_RASTER_SIDE,
    SCOPE_FRAME,
    ExportError,
    Scope,
    bounds_of,
    collect_items,
    paint_items,
)

JPEG_QUALITY = 90
PAGE_FIT = "fit"
PAGE_A4 = "A4"
PAGE_A3 = "A3"
RESOLUTION = 96
POINTS_PER_UNIT = 72.0 / RESOLUTION


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


def _page_layout(rect: QRectF, page: str) -> QPageLayout:
    if page == PAGE_FIT:
        size = QPageSize(
            QSizeF(rect.width() * POINTS_PER_UNIT, rect.height() * POINTS_PER_UNIT),
            QPageSize.Unit.Point,
            "Fit",
        )
        orientation = QPageLayout.Orientation.Portrait
    else:
        size = QPageSize(QPageSize.PageSizeId.A4 if page == PAGE_A4 else QPageSize.PageSizeId.A3)
        orientation = QPageLayout.Orientation.Landscape
    return QPageLayout(size, orientation, QMarginsF(0, 0, 0, 0))


def _page_transform(writer: QPdfWriter, rect: QRectF, page: str) -> QTransform:
    transform = QTransform()
    if page == PAGE_FIT:
        scale = 1.0
    else:
        area = writer.pageLayout().paintRectPixels(RESOLUTION)
        scale = min(area.width() / rect.width(), area.height() / rect.height())
    transform.scale(scale, scale)
    transform.translate(-rect.x(), -rect.y())
    return transform


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
    sections = []
    if each_frame:
        for frame in frames_in(scene, scope):
            inner = Scope(SCOPE_FRAME, frame_id=frame.obj_id)
            sections.append((collect_items(scene, inner), frame.sceneBoundingRect()))
    if not sections:
        items = collect_items(scene, scope)
        sections = [(items, bounds_of(items))]
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QPdfWriter(buffer)
    writer.setResolution(RESOLUTION)
    writer.setTitle(title)
    writer.setCreator("LANvas")
    writer.setPageLayout(_page_layout(sections[0][1], page))
    painter = QPainter(writer)
    done = 0
    for index, (items, rect) in enumerate(sections):
        if index:
            writer.setPageLayout(_page_layout(rect, page))
            writer.newPage()
        for count in paint_items(painter, items, _page_transform(writer, rect, page)):
            yield done + count
        done += len(items)
    painter.end()
    return bytes(buffer.data())
