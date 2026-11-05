"""Design tokens from Design.md section 3, turned into a Qt stylesheet and palette."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtGui import QColor, QFontDatabase, QPalette
from PyQt6.QtWidgets import QApplication

# Inside a PyInstaller bundle, data files live under sys._MEIPASS rather than beside the source.
_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
ASSETS = _BASE / "client" / "assets"
FONT_FAMILY = "Inter"

USER_COLORS = (
    "#F24822",
    "#0D99FF",
    "#14AE5C",
    "#9747FF",
    "#FFA629",
    "#FF4DA6",
    "#00B8A9",
    "#7A5C00",
)
ROLE_COLORS = {"owner": "#9747FF", "editor": "#0D99FF", "viewer": "#6B6B6B"}
GUIDE_COLOR = "#FF00C8"
SWATCHES = (
    "#1E1E1E",
    "#FFFFFF",
    "#F24822",
    "#FFA629",
    "#FFCD29",
    "#14AE5C",
    "#0D99FF",
    "#9747FF",
    "#FF4DA6",
    "#B3B3B3",
)
STICKY_COLOR = "#FFE27A"


@dataclass(frozen=True)
class Theme:
    bg_app: str
    bg_panel: str
    bg_canvas: str
    border: str
    text_primary: str
    text_muted: str
    private: str
    hover: str
    on_private: str = "#FFFFFF"  # text on the private strip; must keep 4.5:1 against `private`
    accent: str = "#0D99FF"
    success: str = "#14AE5C"
    warn: str = "#FFA629"
    danger: str = "#F24822"


LIGHT = Theme(
    "#F5F5F5", "#FFFFFF", "#E5E5E5", "#E6E6E6", "#1E1E1E", "#6B6B6B", "#9747FF", "#F0F0F0"
)
DARK = Theme(
    "#1E1E1E",
    "#2C2C2C",
    "#1A1A1A",
    "#3C3C3C",
    "#F5F5F5",
    "#A0A0A0",
    "#B98AFF",
    "#383838",
    "#1E1E1E",
)

_current = LIGHT


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio, so that the accessibility requirement can be tested and not assumed."""

    def luminance(value: str) -> float:
        channels = []
        for c in QColor(value).getRgbF()[:3]:
            channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def current() -> Theme:
    return _current


def user_color(fingerprint: str) -> str:
    """Derived from the identity, so every client paints a given person in the same color."""
    digest = hashlib.sha256(fingerprint.encode()).digest()
    return USER_COLORS[digest[0] % len(USER_COLORS)]


def load_fonts() -> bool:
    """Register the bundled fonts. The app never relies on, or downloads, anything else."""
    loaded = False
    for path in sorted((ASSETS / "fonts").glob("*.ttf")):
        loaded = QFontDatabase.addApplicationFont(str(path)) >= 0 or loaded
    return loaded


def stylesheet(t: Theme) -> str:
    return f"""
    * {{ font-family: "{FONT_FAMILY}", "Segoe UI", "SF Pro", "Noto Sans"; font-size: 12px; }}
    QMainWindow, QDialog, QStackedWidget, #page {{ background: {t.bg_app}; color: {t.text_primary};
    }}
    QWidget {{ color: {t.text_primary}; }}
    #panel, QTabWidget::pane {{ background: {t.bg_panel}; border: none; }}
    #topbar, #statusbar, #toolrail {{ background: {t.bg_panel}; }}
    #topbar {{ border-bottom: 1px solid {t.border}; }}
    #statusbar {{ border-top: 1px solid {t.border}; }}
    #toolrail {{ border-right: 1px solid {t.border}; }}
    #leftpanel {{ background: {t.bg_panel}; border-right: 1px solid {t.border}; }}
    #rightpanel {{ background: {t.bg_panel}; border-left: 1px solid {t.border}; }}
    #card {{ background: {t.bg_panel}; border: 1px solid {t.border}; border-radius: 6px; }}
    #section {{ font-size: 11px; font-weight: 600; color: {t.text_muted}; }}
    #title {{ font-size: 13px; font-weight: 600; }}
    #hero {{ font-size: 18px; font-weight: 600; }}
    #muted {{ color: {t.text_muted}; }}
    #banner {{ background: {t.warn}; color: #1E1E1E; padding: 6px 12px; }}
    #banner QLabel, #banner QPushButton {{ color: #1E1E1E; }}
    #privatestrip {{
        background: {t.private}; color: {t.on_private}; padding: 4px 8px; font-size: 11px;
    }}
    #toast {{ background: #1E1E1E; color: #FFFFFF; border-radius: 6px; padding: 8px 12px; }}
    #toast QLabel {{ color: #FFFFFF; background: transparent; }}
    #toast QPushButton {{ color: #7CC8FF; background: transparent; border: none; font-weight: 600;
    }}
    QPushButton {{
        background: {t.bg_panel}; border: 1px solid {t.border}; border-radius: 4px; padding: 4px
        10px;
    }}
    QPushButton:hover {{ background: {t.hover}; }}
    QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
    QDoubleSpinBox:focus, QPlainTextEdit:focus, QListWidget:focus, QTreeWidget:focus {{
        outline: none; border: 2px solid {t.accent};
    }}
    QPushButton:disabled {{ color: {t.text_muted}; }}
    QPushButton#primary {{ background: {t.accent}; color: #06263F; border: none; font-weight: 600;
    }}
    QPushButton#danger {{ color: {t.danger}; }}
    QToolButton {{ background: transparent; border: none; border-radius: 4px; padding: 4px; }}
    QToolButton:hover {{ background: {t.hover}; }}
    QToolButton:checked {{ background: {t.accent}; color: #FFFFFF; }}
    QToolButton:disabled {{ color: {t.text_muted}; }}
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{
        background: {t.bg_panel}; border: 1px solid {t.border}; border-radius: 4px; padding: 3px
        6px;
    }}
    QListWidget, QTreeWidget, QTableWidget, QTextBrowser {{
        background: {t.bg_panel}; border: none; outline: none;
    }}
    QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected {{
        background: {t.accent}; color: #FFFFFF;
    }}
    QTabBar::tab {{ background: transparent; padding: 6px 10px; color: {t.text_muted}; }}
    QTabBar::tab:selected {{ color: {t.text_primary}; font-weight: 600; }}
    QMenu {{ background: {t.bg_panel}; border: 1px solid {t.border}; border-radius: 6px; padding:
    4px; }}
    QMenu::item {{ padding: 5px 16px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {t.accent}; color: #FFFFFF; }}
    QToolTip {{ background: #1E1E1E; color: #FFFFFF; border: none; padding: 4px; }}
    QHeaderView::section {{
        background: {t.bg_panel}; border: none; border-bottom: 1px solid {t.border};
        padding: 3px 6px; color: {t.text_muted}; font-size: 11px;
    }}
    """


def apply(app: QApplication, dark: bool = False) -> Theme:
    global _current
    _current = DARK if dark else LIGHT
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: _current.bg_app,
        QPalette.ColorRole.Base: _current.bg_panel,
        QPalette.ColorRole.AlternateBase: _current.bg_app,
        QPalette.ColorRole.Text: _current.text_primary,
        QPalette.ColorRole.WindowText: _current.text_primary,
        QPalette.ColorRole.Button: _current.bg_panel,
        QPalette.ColorRole.ButtonText: _current.text_primary,
        QPalette.ColorRole.Highlight: _current.accent,
        QPalette.ColorRole.PlaceholderText: _current.text_muted,
    }
    for role, value in roles.items():
        palette.setColor(role, QColor(value))
    app.setPalette(palette)
    app.setStyleSheet(stylesheet(_current))
    return _current
