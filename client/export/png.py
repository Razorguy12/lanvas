"""PNG and JPEG export: paint into a Pillow image, then encode to bytes."""

from __future__ import annotations

import io
from collections.abc import Generator

from PIL import Image

from client.canvas.paint import RasterPainter
from client.canvas.scene import BoardScene
from client.export.renderer import (
    MAX_RASTER_SIDE,
    ExportError,
    Scope,
    bounds_of,
    collect_items,
    paint_items,
)
from client.geom import Affine

JPEG_QUALITY = 90


def raster_size(scene: BoardScene, scope: Scope, scale: float) -> tuple[int, int]:
    rect = bounds_of(collect_items(scene, scope))
    return round(rect.w * scale), round(rect.h * scale)


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
    width, height = round(rect.w * scale), round(rect.h * scale)
    if max(width, height) > MAX_RASTER_SIDE:
        raise ExportError(
            f"That would be a {width} x {height} px image. Pick a smaller scale, or use PDF or SVG."
        )
    # JPEG has no alpha channel, so "transparent" would come out black if it were honored there.
    clear = transparent and fmt == "PNG"
    try:
        from client import theme

        fill = (0, 0, 0, 0) if clear else theme.rgb(background) + (255,)
        image = Image.new("RGBA", (max(1, width), max(1, height)), fill)
    except MemoryError as exc:
        raise ExportError("Not enough memory for an image that size. Try a smaller scale.") from exc
    base = Affine(a=scale, d=scale, e=-rect.x * scale, f=-rect.y * scale)
    yield from paint_items(RasterPainter(image, base, supersample=2), items)
    out = io.BytesIO()
    try:
        if fmt == "JPEG":
            image.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY)
        else:
            image.save(out, "PNG")
    except OSError as exc:
        raise ExportError(f"Could not encode the image as {fmt}.") from exc
    return out.getvalue()
