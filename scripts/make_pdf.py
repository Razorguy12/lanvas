#!/usr/bin/env python3
"""Build the technical documentation PDF from the Markdown sources, offline, with Qt only.

A maintainer tool, not part of the app: it needs PyQt6, which the app itself no longer uses
(``pip install PyQt6`` to run it)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QMarginsF, QSizeF  # noqa: E402
from PyQt6.QtGui import (  # noqa: E402
    QFont,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QTextDocument,
)
from PyQt6.QtWidgets import QApplication  # noqa: E402

from client import theme  # noqa: E402

SOURCES = (
    ("LANvas: technical documentation", None),
    ("Product context", ROOT / "ProductContext.md"),
    ("Architecture", ROOT / "Architecture.md"),
    ("UI design", ROOT / "Design.md"),
    ("User guide", ROOT / "docs" / "USER_GUIDE.md"),
    ("Test log", ROOT / "docs" / "test-log.md"),
)
RESOLUTION = 96
STYLE = """
body { font-size: 11pt; }
h1 { font-size: 20pt; } h2 { font-size: 15pt; } h3 { font-size: 12pt; }
table { border-collapse: collapse; } td, th { padding: 3px; }
code, pre { font-family: monospace; font-size: 9pt; }
"""


def main() -> int:
    app = QApplication([])
    theme.load_fonts()
    out = ROOT / "docs" / "LANvas-Technical-Documentation.pdf"
    writer = QPdfWriter(str(out))
    writer.setResolution(RESOLUTION)
    writer.setTitle("LANvas technical documentation")
    writer.setCreator("LANvas")
    writer.setPageLayout(
        QPageLayout(
            QPageSize(QPageSize.PageSizeId.A4),
            QPageLayout.Orientation.Portrait,
            QMarginsF(18, 18, 18, 18),
        )
    )
    area = writer.pageLayout().paintRectPixels(RESOLUTION)
    painter = QPainter(writer)
    pages = 0
    for index, (title, path) in enumerate(SOURCES):
        doc = QTextDocument()
        doc.setDefaultFont(QFont(theme.FONT_FAMILY, 11))
        doc.setDefaultStyleSheet(STYLE)
        doc.setPageSize(QSizeF(area.width(), area.height()))
        doc.setMarkdown(path.read_text(encoding="utf-8") if path else f"# {title}")
        for page in range(doc.pageCount()):
            if index or page:
                writer.newPage()
            painter.save()
            painter.translate(0, -page * area.height())
            clip = area.adjusted(0, 0, 0, 0)
            clip.moveTop(page * area.height())
            clip.moveLeft(0)
            painter.setClipRect(clip)
            doc.drawContents(painter)
            painter.restore()
            pages += 1
    painter.end()
    del app
    print(f"Wrote {out} ({pages} pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
