"""Self-test for packaged builds (XP-03): ``lanvas-client --smoke HOST PORT OUTDIR``.

A frozen build can fail in ways source runs never do, such as a missing Qt plugin or a font left out
of the bundle. This walks the real path through the real window: discover, create a room, draw, save
and export every format. It prints one line per step and exits non-zero on the first failure.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QEventLoop
from PyQt6.QtGui import QFontDatabase, QImage
from PyQt6.QtWidgets import QApplication

from client import theme
from client.app import AppWindow
from client.export.formats import export_pdf, export_raster, export_svg
from client.export.renderer import Scope, drive


def _spin(app: QApplication, done: Callable[[], object], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        if done():
            return True
        time.sleep(0.005)
    return bool(done())


def _step(ok: object, name: str) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}", flush=True)
    if not ok:
        raise SystemExit(1)


def run(app: QApplication, host: str, port: int, out_dir: Path, udp_port: int) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    _step(theme.FONT_FAMILY in QFontDatabase.families(), "bundled font loaded")
    window = AppWindow(udp_port)
    window.launcher.name.setText("Smoke")
    window.show()
    window.connect_to(host, port, {"create": {"name": "Smoke test", "code": "SMOKE1"}})
    _step(_spin(app, lambda: window.session and window.session.welcomed), "created and joined room")
    session = window.session
    assert session is not None
    _step(
        _spin(app, lambda: any(b.room_id == session.room_id for b in window.boards), 6),
        "room discovered over UDP",
    )
    session.create("rect", {"x": 0, "y": 0, "w": 120, "h": 80, "fill": "#0D99FF"}, obj_id="r")
    session.create("text", {"x": 10, "y": 100, "w": 200, "h": 30, "text": "Smoke"}, obj_id="t")
    _step(_spin(app, lambda: not session.doc.pending), "drew objects, server confirmed")
    session.save()
    _step(_spin(app, lambda: session.save_state == "saved"), "Ctrl+S checkpoint acknowledged")
    _step(session.save_info["board_hash"] == session.doc.hash(), "board hash matches server")
    assert window.board is not None
    scene = window.board.canvas.board_scene
    png = drive(export_raster(scene, Scope()))
    _step(not QImage.fromData(png, "PNG").isNull(), "PNG export")
    jpeg = drive(export_raster(scene, Scope(), "JPEG"))
    _step(not QImage.fromData(jpeg, "JPEG").isNull(), "JPEG export")
    svg = drive(export_svg(scene, Scope()))
    _step(b"<svg" in svg and b"Smoke" in svg, "SVG export (needs the Qt SVG module)")
    pdf = drive(export_pdf(scene, Scope()))
    _step(pdf.startswith(b"%PDF"), "PDF export")
    for name, data in (("s.png", png), ("s.jpg", jpeg), ("s.svg", svg), ("s.pdf", pdf)):
        (out_dir / name).write_bytes(data)
    window.close()
    print("SMOKE OK", flush=True)
    return 0
