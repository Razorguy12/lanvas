"""Small shared widgets: toasts, avatars, color buttons and scrubbable number fields."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractButton,
    QColorDialog,
    QDoubleSpinBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from client import theme

MIXED_VALUE = -1e9
TOAST_MS = 4000
MAX_TOASTS = 3


class Toast(QFrame):
    """A transient notice in the top-right corner, optionally with one action."""

    def __init__(
        self,
        parent: QWidget,
        text: str,
        action: str | None = None,
        on_action: Callable[[], None] | None = None,
        duration: int = TOAST_MS,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setMaximumWidth(320)
        layout.addWidget(label)
        if action and on_action:
            button = QPushButton(action)
            button.clicked.connect(lambda: (on_action(), self.close_toast()))
            layout.addWidget(button)
        self.adjustSize()
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        self._fade = QPropertyAnimation(effect, b"opacity", self)
        self._fade.setDuration(140)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.start()
        if duration > 0:
            QTimer.singleShot(duration, self.close_toast)

    def close_toast(self) -> None:
        host = self.parent()
        self.setParent(None)
        self.deleteLater()
        if hasattr(host, "layout_toasts"):
            host.layout_toasts()


class ToastHost:
    """Mixin: stacks toasts along the top right of a widget."""

    def show_toast(
        self,
        text: str,
        action: str | None = None,
        on_action: Callable[[], None] | None = None,
        duration: int = TOAST_MS,
    ) -> Toast:
        toast = Toast(self, text, action, on_action, duration)  # type: ignore[arg-type]
        toast.show()
        self.layout_toasts()
        return toast

    def layout_toasts(self) -> None:
        """Newest on top of the stack. A burst must never bury the panels under notices."""
        toasts = self.findChildren(Toast)  # type: ignore[attr-defined]
        for old in toasts[:-MAX_TOASTS]:
            old.setParent(None)
            old.deleteLater()
        y = 52
        for child in toasts[-MAX_TOASTS:]:
            child.adjustSize()
            child.move(self.width() - child.width() - 16, y)  # type: ignore[attr-defined]
            child.raise_()
            y += child.height() + 8


class Avatar(QAbstractButton):
    """Initial letter on the user's color. Dimmed while away, so state isn't color alone."""

    def __init__(self, name: str, fingerprint: str, away: bool = False, size: int = 24) -> None:
        super().__init__()
        self.initial = (name or "?")[:1].upper()
        self.color = QColor(theme.user_color(fingerprint))
        self.away = away
        self.setFixedSize(size, size)
        self.setToolTip(name + (" (away)" if away else ""))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event: Any) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(self.color)
        if self.away:
            color.setAlpha(110)
        painter.setPen(QPen(QColor("#FFFFFF"), 1.5))
        painter.setBrush(color)
        painter.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        font = QFont(theme.FONT_FAMILY)
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.initial)


class ColorButton(QToolButton):
    """A swatch that opens preset colors, a custom picker, and optionally 'None'."""

    color_chosen = pyqtSignal(object)

    def __init__(self, color: str | None = "#000000", allow_none: bool = True) -> None:
        super().__init__()
        self.color = color
        self.allow_none = allow_none
        self.setFixedSize(24, 24)
        self.mixed = False
        self.clicked.connect(self._open_menu)

    def set_color(self, color: str | None, mixed: bool = False) -> None:
        self.color = color
        self.mixed = mixed
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(2, 2, -2, -2)
        painter.setPen(QPen(QColor(theme.current().border), 1))
        if self.mixed:
            painter.setBrush(QColor(theme.current().hover))
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(QColor(theme.current().text_muted))
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), "?")
        elif self.color:
            painter.setBrush(QColor(self.color))
            painter.drawRoundedRect(rect, 4, 4)
        else:
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(QPen(QColor(theme.current().danger), 1.5))
            painter.drawLine(rect.bottomLeft(), rect.topRight())

    def _open_menu(self) -> None:
        menu = QMenu(self)
        grid = QWidget(menu)
        row = QHBoxLayout(grid)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(2)
        for swatch in theme.SWATCHES:
            button = ColorSwatch(swatch)
            button.clicked.connect(lambda _=False, c=swatch: (self._choose(c), menu.close()))
            row.addWidget(button)
        action = menu.addAction("")
        menu.removeAction(action)
        from PyQt6.QtWidgets import QWidgetAction

        holder = QWidgetAction(menu)
        holder.setDefaultWidget(grid)
        menu.addAction(holder)
        menu.addAction("Custom…", self._custom)
        if self.allow_none:
            menu.addAction("None", lambda: self._choose(None))
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _custom(self) -> None:
        chosen = QColorDialog.getColor(QColor(self.color or "#000000"), self, "Pick a color")
        if chosen.isValid():
            self._choose(chosen.name().upper())

    def _choose(self, color: str | None) -> None:
        self.set_color(color)
        self.color_chosen.emit(color)


class ColorSwatch(QToolButton):
    def __init__(self, color: str) -> None:
        super().__init__()
        self.color = color
        self.setFixedSize(20, 20)
        self.setToolTip(color)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(theme.current().border), 1))
        painter.setBrush(QColor(self.color))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 3, 3)


class NumberField(QDoubleSpinBox):
    """A number box able to show 'Mixed' for multi-selections with differing values."""

    def __init__(
        self, lo: float = -1e7, hi: float = 1e7, decimals: int = 1, step: float = 1
    ) -> None:
        super().__init__()
        self.true_min = lo
        self.setRange(MIXED_VALUE, hi)
        self.setDecimals(decimals)
        self.setSingleStep(step)
        self.setSpecialValueText("Mixed")
        self.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.setKeyboardTracking(False)

    def show_value(self, value: float | None) -> None:
        self.blockSignals(True)
        self.setValue(MIXED_VALUE if value is None else value)
        self.blockSignals(False)

    def is_mixed(self) -> bool:
        return self.value() == MIXED_VALUE


class ScrubLabel(QLabel):
    """Drag the label to scrub the number field beside it, as in Figma."""

    def __init__(self, text: str, field: NumberField) -> None:
        super().__init__(text)
        self.field = field
        self.setObjectName("muted")
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self._last_x: float | None = None

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802
        self._last_x = event.position().x()

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802
        if self._last_x is not None and self.field.isEnabled():
            delta = event.position().x() - self._last_x
            self._last_x = event.position().x()
            base = 0.0 if self.field.is_mixed() else self.field.value()
            self.field.setValue(max(self.field.true_min, base + delta * self.field.singleStep()))
            self.field.editingFinished.emit()

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802
        self._last_x = None


class Spinner(QWidget):
    """A subtle activity indicator, used instead of frozen-looking waiting states."""

    def __init__(self, size: int = 16) -> None:
        super().__init__()
        self.setFixedSize(size, size)
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(80)

    def _tick(self) -> None:
        self.angle = (self.angle + 30) % 360
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(theme.current().accent), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        rect = QRect(2, 2, self.width() - 4, self.height() - 4)
        painter.drawArc(rect, -self.angle * 16, 270 * 16)


def section_label(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("section")
    return label


def vbox(
    *widgets: QWidget, margins: tuple[int, int, int, int] = (0, 0, 0, 0), spacing: int = 4
) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget)
    return layout
