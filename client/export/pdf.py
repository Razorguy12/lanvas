"""PDF export: vector output, either fit to content or one page per frame."""

from __future__ import annotations

from collections.abc import Generator

from PyQt6.QtCore import QBuffer, QIODevice, QMarginsF, QRectF, QSizeF
from PyQt6.QtGui import QPageLayout, QPageSize, QPainter, QPdfWriter, QTransform

from client.canvas.items import BoardItem
from client.canvas.scene import BoardScene
from client.export.renderer import SCOPE_FRAME, Scope, bounds_of, collect_items, paint_items

PAGE_FIT = "fit"
PAGE_A4 = "A4"
PAGE_A3 = "A3"
RESOLUTION = 96  # dots per inch, so one board unit is one device dot on a fitted page
POINTS_PER_UNIT = 72.0 / RESOLUTION


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
    if each_frame:
        sections = []
        for frame in frames_in(scene, scope):
            inner = Scope(SCOPE_FRAME, frame_id=frame.obj_id)
            sections.append((collect_items(scene, inner), frame.sceneBoundingRect()))
    else:
        sections = []
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
