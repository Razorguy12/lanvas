"""The one renderer behind PNG, JPEG, SVG and PDF (Design.md section 12).

Items are painted directly, one by one, in document order. This paints only document objects, never
overlays. It also yields regularly, so the UI stays responsive on big boards (EX-08): painting is
chunked into a generator, and the caller decides when to run the next chunk.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QPainter, QTransform

from client.canvas.items import BoardItem
from client.canvas.scene import BoardScene

SCOPE_BOARD = "board"
SCOPE_SELECTION = "selection"
SCOPE_FRAME = "frame"
CHUNK = 50
EXPORT_MARGIN = 20.0
MAX_RASTER_SIDE = 16000


class ExportError(Exception):
    """Export cannot proceed. The message is written for the person who asked."""


@dataclass(frozen=True)
class Scope:
    kind: str = SCOPE_BOARD
    selection: frozenset[str] = frozenset()
    frame_id: str | None = None


def _descendant_ids(scene: BoardScene, roots: set[str]) -> set[str]:
    ids = set(roots)
    grew = True
    while grew:
        grew = False
        for obj_id, item in scene.items_by_id.items():
            if obj_id not in ids and item.obj.get("parent") in ids:
                ids.add(obj_id)
                grew = True
    return ids


def collect_items(scene: BoardScene, scope: Scope) -> list[BoardItem]:
    """Visible items in scope, in paint order."""
    if scope.kind == SCOPE_SELECTION:
        wanted = _descendant_ids(scene, set(scope.selection))
    elif scope.kind == SCOPE_FRAME and scope.frame_id:
        wanted = _descendant_ids(scene, {scope.frame_id})
    else:
        wanted = set(scene.items_by_id)
    items = [
        item
        for obj_id, item in scene.items_by_id.items()
        if obj_id in wanted and item.isVisible()
    ]
    items.sort(key=lambda i: i.zValue())
    return items


def bounds_of(items: list[BoardItem], margin: float = EXPORT_MARGIN) -> QRectF:
    rect = QRectF()
    for item in items:
        rect = rect.united(item.sceneBoundingRect())
    if rect.isNull():
        return QRectF(0, 0, 100, 100)
    return rect.adjusted(-margin, -margin, margin, margin)


def paint_items(
    painter: QPainter, items: list[BoardItem], base: QTransform
) -> Generator[int, None, None]:
    """Paint in chunks, yielding the number of items done so the caller can show progress."""
    for index, item in enumerate(items):
        painter.save()
        painter.setTransform(item.sceneTransform() * base)
        painter.setOpacity(item.opacity())
        item.paint(painter)
        painter.restore()
        if (index + 1) % CHUNK == 0:
            yield index + 1
    yield len(items)


def drive(steps: Generator[int, None, object]) -> object:
    """Run a generator to the end and return its result. Tests and small exports use this."""
    while True:
        try:
            next(steps)
        except StopIteration as stop:
            return stop.value
