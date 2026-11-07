"""Shared tkinter widgets: signals, the UI-thread queue, toasts, avatars, color buttons, scrubbable
number fields, and thin wrappers that give ttk widgets one consistent, small API."""

from __future__ import annotations

import queue
import tkinter as tk
from collections.abc import Callable
from tkinter import colorchooser, messagebox, ttk
from typing import Any

from client import theme

MIXED_VALUE = -1e9
TOAST_MS = 4000
MAX_TOASTS = 3


# ---------------------------------------------------------------------- signals and threads
class Signal:
    """A list of callbacks. Synchronous: emit runs every slot before returning."""

    def __init__(self) -> None:
        self._slots: list[Callable[..., Any]] = []

    def connect(self, slot: Callable[..., Any]) -> None:
        self._slots.append(slot)

    def disconnect(self, slot: Callable[..., Any] | None = None) -> None:
        self._slots = [] if slot is None else [s for s in self._slots if s != slot]

    def emit(self, *args: Any) -> None:
        for slot in list(self._slots):
            slot(*args)


_ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
_ui_root: tk.Misc | None = None


def call_in_ui(fn: Callable[[], None]) -> None:
    """Thread-safe: run ``fn`` on the Tk thread at its next turn. Tk itself is not thread-safe,
    so every network or worker event reaches widgets only through here."""
    _ui_queue.put(fn)


def pump_ui() -> None:
    """Run everything queued for the UI thread. The installed loop calls it; so can tests."""
    while True:
        try:
            fn = _ui_queue.get_nowait()
        except queue.Empty:
            return
        fn()


def install_ui_queue(root: tk.Misc, interval_ms: int = 8) -> None:
    global _ui_root
    _ui_root = root

    def loop() -> None:
        try:
            pump_ui()
        finally:
            if _ui_root is root:
                root.after(interval_ms, loop)

    root.after(interval_ms, loop)


def after_ms(widget: tk.Misc, ms: int, fn: Callable[[], None]) -> str | None:
    """``widget.after`` that quietly does nothing once the widget is gone."""
    try:
        return widget.after(ms, lambda: _alive(widget) and fn())
    except tk.TclError:
        return None


def _alive(widget: tk.Misc) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


# ---------------------------------------------------------------------- visibility
class Visible:
    """Qt-style show/hide for any widget, remembering where it was laid out."""

    _vis = True
    _pack_order: tuple[list[Any], dict[str, Any]] | None = None

    def setVisible(self, on: bool) -> None:  # noqa: N802 (Qt-style API kept on purpose)
        on = bool(on)
        if on == self._vis:
            return
        self._vis = on
        manager = self.winfo_manager()  # type: ignore[attr-defined]
        if not on:
            if manager == "grid":
                self._grid_pending = True
                self.grid_remove()  # type: ignore[attr-defined]
            elif manager == "pack":
                self._pack_order = (self.master.pack_slaves(), self.pack_info())  # type: ignore
                self.pack_forget()  # type: ignore[attr-defined]
            elif manager == "place":
                self._place_info = self.place_info()  # type: ignore[attr-defined]
                self.place_forget()  # type: ignore[attr-defined]
            return
        if self._pack_order is not None:
            siblings, info = self._pack_order
            info = {k: v for k, v in info.items() if k != "in"}
            after = siblings[siblings.index(self) + 1:] if self in siblings else []
            packed = set(self.master.pack_slaves())  # type: ignore[attr-defined]
            later = next((w for w in after if w in packed), None)
            self._pack_order = None
            if later is not None:
                tk.Pack.pack_configure(self, before=later, **info)  # type: ignore[arg-type]
            else:
                tk.Pack.pack_configure(self, **info)  # type: ignore[arg-type]
        elif getattr(self, "_place_info", None):
            info = {k: v for k, v in self._place_info.items() if k != "in"}
            tk.Place.place_configure(self, **info)  # type: ignore[arg-type]
        elif self._grid_pending:
            self._grid_pending = False
            tk.Grid.grid_configure(self)  # type: ignore[arg-type]

    _grid_pending = False

    def grid(self, *args: Any, **kw: Any) -> None:  # noqa: D401 (tk name)
        tk.Grid.grid_configure(self, *args, **kw)  # type: ignore[arg-type]
        if not self._vis:
            self._grid_pending = True
            self.grid_remove()  # type: ignore[attr-defined]

    def pack(self, *args: Any, **kw: Any) -> None:
        tk.Pack.pack_configure(self, *args, **kw)  # type: ignore[arg-type]
        if not self._vis:
            self._pack_order = (self.master.pack_slaves(), self.pack_info())  # type: ignore
            self.pack_forget()  # type: ignore[attr-defined]

    def show(self) -> None:
        self.setVisible(True)

    def hide(self) -> None:
        self.setVisible(False)

    def isHidden(self) -> bool:  # noqa: N802
        return not self._vis

    def isVisible(self) -> bool:  # noqa: N802
        widget: Any = self
        while widget is not None:
            if getattr(widget, "_vis", True) is False:
                return False
            if isinstance(widget, (tk.Tk, tk.Toplevel)):
                try:
                    return widget.state() != "withdrawn"
                except tk.TclError:
                    return False
            widget = widget.master
        return True

    def setEnabled(self, on: bool) -> None:  # noqa: N802
        try:
            self.state(["!disabled"] if on else ["disabled"])  # type: ignore[attr-defined]
        except (AttributeError, tk.TclError):
            try:
                self.configure(state="normal" if on else "disabled")  # type: ignore
            except tk.TclError:
                pass

    def isEnabled(self) -> bool:  # noqa: N802
        try:
            return not self.instate(["disabled"])  # type: ignore[attr-defined]
        except (AttributeError, tk.TclError):
            try:
                return str(self.cget("state")) != "disabled"  # type: ignore[attr-defined]
            except tk.TclError:
                return True

    def setToolTip(self, text: str) -> None:  # noqa: N802
        tip = getattr(self, "_tooltip", None)
        if tip is None:
            self._tooltip = ToolTip(self, text)  # type: ignore[arg-type]
        else:
            tip.text = text

    def toolTip(self) -> str:  # noqa: N802
        tip = getattr(self, "_tooltip", None)
        return tip.text if tip else ""


class ToolTip:
    """Dark tooltip after a short hover, like the old QToolTip style."""

    def __init__(self, widget: tk.Misc, text: str) -> None:
        self.widget, self.text = widget, text
        self.tip: tk.Toplevel | None = None
        self.job: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: Any = None) -> None:
        self._hide()
        if self.text:
            self.job = self.widget.after(600, self._show)

    def _show(self) -> None:
        if not _alive(self.widget):
            return
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#1E1E1E", fg="#FFFFFF", padx=6, pady=4,
                 justify="left", font=theme.font(11)).pack()

    def _hide(self, _event: Any = None) -> None:
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


# ---------------------------------------------------------------------- basic widgets
class Frame(Visible, ttk.Frame):
    def __init__(self, master: tk.Misc, style: str = "", **kw: Any) -> None:
        super().__init__(master, style=f"{style}.TFrame" if style else "TFrame", **kw)


class Label(Visible, ttk.Label):
    def __init__(self, master: tk.Misc, text: str = "", style: str = "", **kw: Any) -> None:
        super().__init__(master, text=text, style=f"{style}.TLabel" if style else "TLabel", **kw)

    def text(self) -> str:
        return str(self.cget("text"))

    def setText(self, text: str) -> None:  # noqa: N802
        self.configure(text=text)

    def set_color(self, color: str, bold: bool = False) -> None:
        self.configure(foreground=color, font=theme.font(12, 600 if bold else 400))

    def set_wrap(self, width: int) -> None:
        self.configure(wraplength=width)


def section_label(master: tk.Misc, text: str, on_panel: bool = True) -> Label:
    return Label(master, text.upper(), "section" if on_panel else "pagesection")


class Button(Visible, ttk.Button):
    def __init__(self, master: tk.Misc, text: str = "", style: str = "",
                 image: Any = None, compound: str = "left", **kw: Any) -> None:
        self.clicked = Signal()
        style_name = f"{style}.TButton" if style else "TButton"
        super().__init__(master, text=text, style=style_name, command=self._click, **kw)
        if image is not None:
            self.configure(image=image, compound=compound if text else "image")

    def _click(self) -> None:
        self.clicked.emit()

    def click(self) -> None:
        if self.isEnabled():
            self._click()

    def text(self) -> str:
        return str(self.cget("text"))

    def setText(self, text: str) -> None:  # noqa: N802
        self.configure(text=text)


class ToolButton(Visible, ttk.Checkbutton):
    """A square icon button, optionally checkable (then drawn in the accent color while on)."""

    def __init__(self, master: tk.Misc, icon_name: str = "", text: str = "",
                 checkable: bool = False, tooltip: str = "", color: str | None = None) -> None:
        from client import icons

        self.clicked = Signal()
        self.toggled = Signal()
        self.checkable = checkable
        self.var = tk.BooleanVar(master, False)
        super().__init__(master, text=text, style="Tool.TCheckbutton", variable=self.var,
                         command=self._click, takefocus=False)
        if icon_name:
            normal, checked = icons.icon(icon_name, color or theme.current().text_primary,
                                         master=master)
            self.configure(image=(normal, "selected", checked), compound="image" if not text
                           else "left")
        if tooltip:
            self.setToolTip(tooltip)

    def _click(self) -> None:
        if not self.checkable:
            self.var.set(False)
        self.clicked.emit()
        if self.checkable:
            self.toggled.emit(self.var.get())

    def click(self) -> None:
        if self.isEnabled():
            if self.checkable:
                self.var.set(not self.var.get())
            self._click()

    def setChecked(self, on: bool) -> None:  # noqa: N802
        self.var.set(bool(on))

    def isChecked(self) -> bool:  # noqa: N802
        return bool(self.var.get())

    def setText(self, text: str) -> None:  # noqa: N802
        self.configure(text=text)

    def text(self) -> str:
        return str(self.cget("text"))


class LineEdit(Visible, ttk.Entry):
    """Entry with placeholder text, a length cap, and Qt-like signals."""

    def __init__(self, master: tk.Misc, text: str = "", placeholder: str = "",
                 max_length: int = 0, password: bool = False, width: int = 20) -> None:
        self.var = tk.StringVar(master, text)
        self.returnPressed = Signal()
        self.textChanged = Signal()
        self.editingFinished = Signal()
        super().__init__(master, textvariable=self.var, width=width)
        self._password = password
        self._placeholder = ""
        self._showing_placeholder = False
        self.max_length = max_length
        self.bind("<Return>", lambda e: self.returnPressed.emit(), add="+")
        self.bind("<KP_Enter>", lambda e: self.returnPressed.emit(), add="+")
        self.bind("<FocusIn>", self._focus_in, add="+")
        self.bind("<FocusOut>", self._focus_out, add="+")
        self._trace = self.var.trace_add("write", self._changed)
        if password:
            self.configure(show="•")
        if placeholder:
            self.setPlaceholderText(placeholder)

    def _changed(self, *_: Any) -> None:
        if self._showing_placeholder:
            return
        value = self.var.get()
        if self.max_length and len(value) > self.max_length:
            self.var.set(value[: self.max_length])
            return
        self.textChanged.emit(value)

    def _focus_in(self, _event: Any = None) -> None:
        if self._showing_placeholder:
            self._showing_placeholder = False
            self.var.set("")
            self.configure(foreground=theme.current().text_primary)
            if self._password:
                self.configure(show="•")

    def _focus_out(self, _event: Any = None) -> None:
        self.editingFinished.emit()
        self._update_placeholder()

    def _update_placeholder(self) -> None:
        if self._placeholder and not self.var.get() and self.focus_get() is not self:
            self._showing_placeholder = True
            self.configure(foreground=theme.current().text_muted, show="")
            self.var.set(self._placeholder)

    def setPlaceholderText(self, text: str) -> None:  # noqa: N802
        if self._showing_placeholder:
            self._showing_placeholder = False
            self.var.set("")
        self._placeholder = text
        self._update_placeholder()

    def placeholderText(self) -> str:  # noqa: N802
        return self._placeholder

    def text(self) -> str:
        return "" if self._showing_placeholder else self.var.get()

    def setText(self, text: str) -> None:  # noqa: N802
        if self._showing_placeholder:
            self._showing_placeholder = False
            self.configure(foreground=theme.current().text_primary)
            if self._password:
                self.configure(show="•")
        self.var.set(text)
        if not text:
            self._update_placeholder()

    def clear(self) -> None:
        self.setText("")

    def setReadOnly(self, on: bool) -> None:  # noqa: N802
        self.state(["readonly"] if on else ["!readonly"])

    def setFocus(self) -> None:  # noqa: N802
        self.focus_set()
        self._focus_in()


class CheckBox(Visible, ttk.Checkbutton):
    def __init__(self, master: tk.Misc, text: str, checked: bool = False, panel: bool = False):
        self.var = tk.BooleanVar(master, checked)
        self.toggled = Signal()
        super().__init__(master, text=text, variable=self.var, command=self._toggle,
                         style="panel.TCheckbutton" if panel else "TCheckbutton")

    def _toggle(self) -> None:
        self.toggled.emit(self.var.get())

    def isChecked(self) -> bool:  # noqa: N802
        return bool(self.var.get())

    def setChecked(self, on: bool) -> None:  # noqa: N802
        if bool(on) != self.isChecked():
            self.var.set(bool(on))
            self._toggle()

    def text(self) -> str:
        return str(self.cget("text"))


class RadioGroup:
    """Exclusive radio buttons sharing one variable; ``toggled`` fires on every change."""

    def __init__(self, master: tk.Misc) -> None:
        self.var = tk.StringVar(master, "")
        self.buttons: list[RadioButton] = []
        self.toggled = Signal()

    def add(self, master: tk.Misc, text: str, panel: bool = False) -> RadioButton:
        button = RadioButton(master, text, self, panel)
        self.buttons.append(button)
        return button

    def checked(self) -> RadioButton | None:
        return next((b for b in self.buttons if b.value == self.var.get()), None)


class RadioButton(Visible, ttk.Radiobutton):
    def __init__(self, master: tk.Misc, text: str, group: RadioGroup, panel: bool = False):
        self.group = group
        self.value = f"{text}#{len(group.buttons)}"
        self.toggled = Signal()
        super().__init__(master, text=text, variable=group.var, value=self.value,
                         command=lambda: group.toggled.emit(),
                         style="panel.TRadiobutton" if panel else "TRadiobutton")

    def isChecked(self) -> bool:  # noqa: N802
        return self.group.var.get() == self.value

    def setChecked(self, on: bool) -> None:  # noqa: N802
        if on and not self.isChecked():
            self.group.var.set(self.value)
            self.group.toggled.emit()

    def text(self) -> str:
        return str(self.cget("text"))


class ComboBox(Visible, ttk.Combobox):
    """Read-only choice list where each entry carries a data value, like QComboBox."""

    def __init__(self, master: tk.Misc, width: int = 14) -> None:
        self.items: list[tuple[str, Any]] = []
        self.activated = Signal()
        self.currentIndexChanged = Signal()
        self._index = -1  # kept in Python, so it can still be read once a dialog has closed
        super().__init__(master, state="readonly", width=width, values=[])
        self.bind("<<ComboboxSelected>>", self._selected, add="+")

    def _selected(self, _event: Any = None) -> None:
        self.selection_clear()
        self._index = int(self.current())
        self.activated.emit(self.currentIndex())
        self.currentIndexChanged.emit(self.currentIndex())

    def addItem(self, label: str, data: Any = None) -> None:  # noqa: N802
        self.items.append((label, label if data is None else data))
        self.configure(values=[i[0] for i in self.items])
        if len(self.items) == 1:
            self.current(0)
            self._index = 0

    def addItems(self, labels: list[str]) -> None:  # noqa: N802
        for label in labels:
            self.addItem(label)

    def count(self) -> int:
        return len(self.items)

    def currentIndex(self) -> int:  # noqa: N802
        return self._index

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        if 0 <= index < len(self.items) and index != self.currentIndex():
            self.current(index)
            self._index = index
            self.currentIndexChanged.emit(index)

    def currentText(self) -> str:  # noqa: N802
        index = self._index
        return self.items[index][0] if 0 <= index < len(self.items) else ""

    def setCurrentText(self, text: str) -> None:  # noqa: N802
        for i, (label, _) in enumerate(self.items):
            if label == text:
                self.setCurrentIndex(i)

    def currentData(self) -> Any:  # noqa: N802
        index = self.currentIndex()
        return self.items[index][1] if 0 <= index < len(self.items) else None

    def findData(self, data: Any) -> int:  # noqa: N802
        return next((i for i, item in enumerate(self.items) if item[1] == data), -1)

    def setEnabled(self, on: bool) -> None:  # noqa: N802
        self.state(["!disabled", "readonly"] if on else ["disabled"])


class SpinBox(Visible, ttk.Spinbox):
    def __init__(self, master: tk.Misc, lo: int, hi: int, value: int, width: int = 8) -> None:
        self.var = tk.StringVar(master, str(value))
        self.lo, self.hi = lo, hi
        super().__init__(master, from_=lo, to=hi, textvariable=self.var, width=width)

    def value(self) -> int:
        try:
            return max(self.lo, min(self.hi, int(float(self.var.get()))))
        except ValueError:
            return self.lo

    def setValue(self, value: int) -> None:  # noqa: N802
        self.var.set(str(value))


class Slider(Visible, tk.Canvas):
    """A thin groove with an accent fill and a round handle (QSlider, Figma-styled)."""

    HEIGHT, PAD, KNOB = 18, 8, 7

    def __init__(self, master: tk.Misc, lo: int = 0, hi: int = 100) -> None:
        super().__init__(master, height=self.HEIGHT, highlightthickness=0, borderwidth=0,
                         background=theme.current().bg_panel, cursor="hand2")
        self.lo, self.hi = lo, hi
        self._value = lo
        self._block = False
        self.enabled = True
        self.valueChanged = Signal()
        self.sliderReleased = Signal()
        self.bind("<Configure>", lambda e: self._paint())
        self.bind("<ButtonPress-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda e: self.enabled and self.sliderReleased.emit())

    def _x_for(self, value: float) -> float:
        span = max(1, self.winfo_width() - 2 * self.PAD)
        return self.PAD + span * (value - self.lo) / max(1, self.hi - self.lo)

    def _paint(self) -> None:
        t = theme.current()
        self.delete("all")
        mid, x = self.HEIGHT / 2, self._x_for(self._value)
        right = max(self.PAD, self.winfo_width() - self.PAD)
        self.create_line(self.PAD, mid, right, mid, fill=t.border, width=4, capstyle="round")
        color = t.accent if self.enabled else t.text_muted
        self.create_line(self.PAD, mid, x, mid, fill=color, width=4, capstyle="round")
        k = self.KNOB
        self.create_oval(x - k, mid - k, x + k, mid + k, fill="#FFFFFF", outline=t.border)

    def _drag(self, event: Any) -> None:
        if not self.enabled:
            return
        span = max(1, self.winfo_width() - 2 * self.PAD)
        ratio = min(1.0, max(0.0, (event.x - self.PAD) / span))
        self.setValue(round(self.lo + ratio * (self.hi - self.lo)))

    def value(self) -> int:
        return self._value

    def setValue(self, value: int) -> None:  # noqa: N802
        value = max(self.lo, min(self.hi, int(value)))
        if value != self._value:
            self._value = value
            self._paint()
            if not self._block:
                self.valueChanged.emit(value)

    def blockSignals(self, on: bool) -> None:  # noqa: N802
        self._block = on

    def setEnabled(self, on: bool) -> None:  # noqa: N802
        self.enabled = bool(on)
        self.configure(cursor="hand2" if on else "arrow")
        self._paint()

    def isEnabled(self) -> bool:  # noqa: N802
        return self.enabled


class ListWidget(Visible, ttk.Treeview):
    """A single-column list of (text, data, icon) rows."""

    def __init__(self, master: tk.Misc, height: int = 6) -> None:
        self.itemClicked = Signal()
        self.itemActivated = Signal()
        self._data: dict[str, Any] = {}
        self._count = 0
        super().__init__(master, show="tree", selectmode="browse", height=height)
        self.column("#0", stretch=True)
        self.bind("<ButtonRelease-1>", self._clicked, add="+")
        self.bind("<Return>", self._activated, add="+")

    def addItem(self, text: str, data: Any = None, icon: Any = None,  # noqa: N802
                selected: bool = False, color: str | None = None) -> str:
        iid = f"i{self._count}"
        self._count += 1
        options: dict[str, Any] = {"text": text}
        if icon is not None:
            options["image"] = icon
        if color:
            options["tags"] = (f"c{color}",)
            self.tag_configure(f"c{color}", foreground=color)
        self.insert("", "end", iid=iid, **options)
        self._data[iid] = data
        if selected:
            self.selection_add(iid)
        return iid

    def clear(self) -> None:
        self.delete(*self.get_children())
        self._data.clear()

    def count(self) -> int:
        return len(self.get_children())

    def item_text(self, index: int) -> str:
        return str(self.item(self.get_children()[index], "text"))

    def item_data(self, index: int) -> Any:
        return self._data.get(self.get_children()[index])

    def item_icon(self, index: int) -> Any:
        return self.item(self.get_children()[index], "image")

    def currentData(self) -> Any:  # noqa: N802
        sel = self.selection()
        return self._data.get(sel[0]) if sel else None

    def setCurrentRow(self, index: int) -> None:  # noqa: N802
        children = self.get_children()
        if 0 <= index < len(children):
            self.selection_set(children[index])

    def _clicked(self, event: Any) -> None:
        iid = self.identify_row(event.y)
        if iid:
            self.itemClicked.emit(self._data.get(iid))

    def _activated(self, _event: Any) -> None:
        data = self.currentData()
        if data is not None:
            self.itemActivated.emit(data)


class TextView(Visible, tk.Text):
    """Read-only rich text. Content is inserted as plain text with tags, never as markup, so
    untrusted text cannot inject formatting or links."""

    def __init__(self, master: tk.Misc, height: int = 10) -> None:
        t = theme.current()
        super().__init__(master, height=height, wrap="word", relief="flat", borderwidth=0,
                         background=t.bg_panel, foreground=t.text_primary, padx=8, pady=6,
                         highlightthickness=0, font=theme.font(12), cursor="arrow")
        self.configure(state="disabled")
        self._links = 0
        self.tag_configure("muted", foreground=t.text_muted, font=theme.font(11))
        self.tag_configure("bold", font=theme.font(12, 600))
        self.tag_configure("right", justify="right")
        self.tag_configure("center", justify="center")
        self.tag_configure("italic", font=(theme.FONT_FAMILY, -12, "italic"))

    def begin(self) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self._links = 0

    def end(self) -> None:
        self.configure(state="disabled")

    def write(self, text: str, *tags: str) -> None:
        self.insert("end", text, tags)

    def link(self, text: str, on_click: Callable[[], None], color: str, *tags: str) -> None:
        tag = f"link{self._links}"
        self._links += 1
        self.tag_configure(tag, foreground=color)
        self.tag_bind(tag, "<Button-1>", lambda e: on_click())
        self.tag_bind(tag, "<Enter>", lambda e: self.configure(cursor="hand2"))
        self.tag_bind(tag, "<Leave>", lambda e: self.configure(cursor="arrow"))
        self.insert("end", text, (tag, *tags))

    def toPlainText(self) -> str:  # noqa: N802
        return self.get("1.0", "end-1c")

    def set_plain(self, text: str) -> None:
        self.begin()
        self.write(text)
        self.end()


# ---------------------------------------------------------------------- composite widgets
class Toast(tk.Frame):
    """A transient notice in the top-right corner, optionally with one action."""

    def __init__(self, host: Any, text: str, action: str | None = None,
                 on_action: Callable[[], None] | None = None, duration: int = TOAST_MS) -> None:
        super().__init__(host, background="#1E1E1E", padx=12, pady=8)
        self.host = host
        self.label = tk.Label(self, text=text, bg="#1E1E1E", fg="#FFFFFF", wraplength=320,
                              justify="left", font=theme.font(12))
        self.label.pack(side="left")
        if action and on_action:
            link = tk.Label(self, text=action, bg="#1E1E1E", fg="#7CC8FF", cursor="hand2",
                            font=theme.font(12, 600), padx=8)
            link.pack(side="left")
            link.bind("<Button-1>", lambda e: (on_action(), self.close_toast()))
        self.closed = False
        if duration > 0:
            after_ms(self, duration, self.close_toast)

    def text(self) -> str:
        return str(self.label.cget("text"))

    def close_toast(self) -> None:
        if self.closed:
            return
        self.closed = True
        host = self.host
        if self in getattr(host, "_toasts", []):
            host._toasts.remove(self)
        self.destroy()
        if hasattr(host, "layout_toasts"):
            host.layout_toasts()


class ToastHost:
    """Mixin for a window: stacks toasts along its top right."""

    _toasts: list[Toast]

    def show_toast(self, text: str, action: str | None = None,
                   on_action: Callable[[], None] | None = None, duration: int = TOAST_MS) -> Toast:
        if not hasattr(self, "_toasts"):
            self._toasts = []
        toast = Toast(self, text, action, on_action, duration)
        self._toasts.append(toast)
        self.layout_toasts()
        return toast

    def toasts(self) -> list[Toast]:
        return list(getattr(self, "_toasts", []))

    def layout_toasts(self) -> None:
        """Newest on top of the stack. A burst must never bury the panels under notices."""
        toasts = getattr(self, "_toasts", [])
        for old in toasts[:-MAX_TOASTS]:
            old.close_toast()
        y = 52
        for child in self._toasts[-MAX_TOASTS:]:
            child.place(relx=1.0, x=-16, y=y, anchor="ne")
            child.lift()
            child.update_idletasks()
            y += child.winfo_reqheight() + 8


class Avatar(Visible, tk.Canvas):
    """Initial letter on the user's color. Dimmed while away, so state isn't color alone."""

    def __init__(self, master: tk.Misc, name: str, fingerprint: str, away: bool = False,
                 size: int = 24) -> None:
        bg = theme.current().bg_panel
        super().__init__(master, width=size, height=size, highlightthickness=0, background=bg,
                         cursor="hand2")
        self.clicked = Signal()
        self.initial = (name or "?")[:1].upper()
        self.away = away
        color = theme.user_color(fingerprint)
        fill = blend(color, bg, 0.43) if away else color
        self.create_oval(1, 1, size - 1, size - 1, fill=fill, outline="#FFFFFF", width=1.5)
        self.create_text(size / 2, size / 2, text=self.initial, fill="#FFFFFF",
                         font=theme.font(11, 600))
        self.setToolTip(name + (" (away)" if away else ""))
        self.bind("<Button-1>", lambda e: self.clicked.emit())


def blend(color: str, onto: str, alpha: float) -> str:
    a, b = theme.rgb(color), theme.rgb(onto)
    return "#" + "".join(
        f"{round(x * alpha + y * (1 - alpha)):02X}" for x, y in zip(a, b, strict=True)
    )


class ColorButton(Visible, tk.Canvas):
    """A swatch that opens preset colors, a custom picker, and optionally 'None'."""

    def __init__(self, master: tk.Misc, color: str | None = "#000000",
                 allow_none: bool = True) -> None:
        super().__init__(master, width=24, height=24, highlightthickness=0,
                         background=theme.current().bg_panel, cursor="hand2")
        self.color = color
        self.allow_none = allow_none
        self.mixed = False
        self.enabled = True
        self.color_chosen = Signal()
        self.bind("<Button-1>", lambda e: self.enabled and self._open_menu())
        self._paint()

    def setEnabled(self, on: bool) -> None:  # noqa: N802
        self.enabled = bool(on)

    def isEnabled(self) -> bool:  # noqa: N802
        return self.enabled

    def set_color(self, color: str | None, mixed: bool = False) -> None:
        self.color = color
        self.mixed = mixed
        self._paint()

    def _paint(self) -> None:
        t = theme.current()
        self.delete("all")
        if self.mixed:
            self.create_rectangle(2, 2, 22, 22, fill=t.hover, outline=t.border)
            self.create_text(12, 12, text="?", fill=t.text_muted, font=theme.font(12))
        elif self.color:
            self.create_rectangle(2, 2, 22, 22, fill=self.color[:7], outline=t.border)
        else:
            self.create_rectangle(2, 2, 22, 22, fill="#FFFFFF", outline=t.border)
            self.create_line(2, 22, 22, 2, fill=t.danger, width=1.5)

    def _open_menu(self) -> None:
        t = theme.current()
        popup = tk.Toplevel(self)
        popup.wm_overrideredirect(True)
        popup.configure(background=t.border)
        body = tk.Frame(popup, background=t.bg_panel, padx=4, pady=4)
        body.pack(padx=1, pady=1)
        row = tk.Frame(body, background=t.bg_panel)
        row.pack(fill="x")

        def choose(color: str | None) -> None:
            popup.destroy()
            self._choose(color)

        for swatch in theme.SWATCHES:
            cell = tk.Canvas(row, width=20, height=20, highlightthickness=0, background=t.bg_panel,
                             cursor="hand2")
            cell.create_rectangle(1, 1, 19, 19, fill=swatch, outline=t.border)
            cell.pack(side="left", padx=1)
            cell.bind("<Button-1>", lambda e, c=swatch: choose(c))
            ToolTip(cell, swatch)
        for text, fn in (("Custom…", lambda: (popup.destroy(), self._custom())),
                         ("None", lambda: choose(None))):
            if text == "None" and not self.allow_none:
                continue
            item = tk.Label(body, text=text, anchor="w", background=t.bg_panel,
                            foreground=t.text_primary, padx=12, pady=4, font=theme.font(12))
            item.pack(fill="x")
            item.bind("<Enter>", lambda e, w=item: w.configure(bg=t.accent, fg="#FFFFFF"))
            item.bind("<Leave>", lambda e, w=item: w.configure(bg=t.bg_panel, fg=t.text_primary))
            item.bind("<Button-1>", lambda e, f=fn: f())
        popup.wm_geometry(f"+{self.winfo_rootx()}+{self.winfo_rooty() + self.winfo_height()}")
        popup.bind("<FocusOut>", lambda e: popup.destroy())
        popup.bind("<Escape>", lambda e: popup.destroy())
        popup.focus_force()

    def _custom(self) -> None:
        chosen = colorchooser.askcolor(self.color or "#000000", parent=self,
                                       title="Pick a color")[1]
        if chosen:
            self._choose(chosen.upper())

    def _choose(self, color: str | None) -> None:
        self.set_color(color)
        self.color_chosen.emit(color)


class NumberField(Visible, ttk.Entry):
    """A number box able to show 'Mixed' for multi-selections with differing values."""

    def __init__(self, master: tk.Misc, lo: float = -1e7, hi: float = 1e7, decimals: int = 1,
                 step: float = 1, width: int = 7) -> None:
        self.var = tk.StringVar(master, "0")
        self.true_min, self.hi, self.decimals, self.step = lo, hi, decimals, step
        self.editingFinished = Signal()
        self._value = 0.0
        self._blocked = False
        super().__init__(master, textvariable=self.var, width=width)
        self.bind("<Return>", self._commit, add="+")
        self.bind("<FocusOut>", self._commit, add="+")

    def _format(self, value: float) -> str:
        if value == MIXED_VALUE:
            return "Mixed"
        text = f"{value:.{self.decimals}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text

    def _commit(self, _event: Any = None) -> None:
        raw = self.var.get().strip()
        if raw == "Mixed":
            return
        try:
            value = max(self.true_min, min(self.hi, float(raw)))
        except ValueError:
            self.var.set(self._format(self._value))
            return
        changed = value != self._value
        self._value = value
        self.var.set(self._format(value))
        if changed and not self._blocked:
            self.editingFinished.emit()

    def value(self) -> float:
        return self._value

    def setValue(self, value: float) -> None:  # noqa: N802
        if value != MIXED_VALUE:
            value = max(self.true_min, min(self.hi, value))
        self._value = value
        self.var.set(self._format(value))

    def text(self) -> str:
        return self.var.get()

    def singleStep(self) -> float:  # noqa: N802
        return self.step

    def show_value(self, value: float | None) -> None:
        self.setValue(MIXED_VALUE if value is None else value)

    def is_mixed(self) -> bool:
        return self._value == MIXED_VALUE


class ScrubLabel(Label):
    """Drag the label to scrub the number field beside it, as in Figma."""

    def __init__(self, master: tk.Misc, text: str, field: NumberField) -> None:
        super().__init__(master, text, "panelmuted", cursor="sb_h_double_arrow")
        self.field = field
        self._last_x: float | None = None
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_last_x", None))

    def _press(self, event: Any) -> None:
        self._last_x = event.x

    def _move(self, event: Any) -> None:
        if self._last_x is not None and self.field.isEnabled():
            delta = event.x - self._last_x
            self._last_x = event.x
            base = 0.0 if self.field.is_mixed() else self.field.value()
            self.field.setValue(max(self.field.true_min, base + delta * self.field.singleStep()))
            self.field.editingFinished.emit()


class Spinner(Visible, tk.Canvas):
    """A subtle activity indicator, used instead of frozen-looking waiting states."""

    def __init__(self, master: tk.Misc, size: int = 16, background: str | None = None) -> None:
        bg = background or theme.current().bg_panel
        super().__init__(master, width=size, height=size, highlightthickness=0, background=bg)
        self.size, self.angle = size, 0
        self._tick()

    def _tick(self) -> None:
        self.angle = (self.angle + 30) % 360
        self.delete("all")
        s = self.size
        self.create_arc(2, 2, s - 2, s - 2, start=-self.angle, extent=270, style="arc",
                        outline=theme.current().accent, width=2)
        after_ms(self, 80, self._tick)


# ---------------------------------------------------------------------- dialogs and menus
class Dialog(tk.Toplevel):
    """A modal dialog. ``exec()`` returns True when accepted, as QDialog.exec does."""

    def __init__(self, parent: Any = None, title: str = "") -> None:
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.configure(background=theme.current().bg_app)
        self.result_ok = False
        self.protocol("WM_DELETE_WINDOW", self.reject)
        self.bind("<Escape>", lambda e: self.reject())
        if parent is not None:
            self.transient(parent.winfo_toplevel())

    def exec(self) -> bool:
        self.deiconify()
        self.update_idletasks()
        master = self.master.winfo_toplevel() if self.master is not None else None
        if master is not None and master.winfo_ismapped():
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_reqwidth()) // 2
            y = master.winfo_rooty() + (master.winfo_height() - self.winfo_reqheight()) // 3
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_set()
        self._done = tk.BooleanVar(self, False)
        self.wait_variable(self._done)
        # Callers read the fields right after exec() returns, so the window is only hidden here
        # and destroyed a moment later.
        self.after(1000, self._dispose)
        return self.result_ok

    def _dispose(self) -> None:
        if self.winfo_exists():
            self.destroy()

    def _finish(self, ok: bool) -> None:
        self.result_ok = ok
        try:
            self.grab_release()
            self.withdraw()
        except tk.TclError:
            pass
        done = getattr(self, "_done", None)
        if done is not None:
            done.set(True)
        else:
            self._dispose()

    def accept(self) -> None:
        self._finish(True)

    def reject(self) -> None:
        self._finish(False)


def popup_menu(widget: tk.Misc, entries: list[Any], x: int | None = None,
               y: int | None = None) -> tk.Menu:
    """entries: (label, callback, enabled=True) | (label, [submenu entries]) | None (separator)."""
    t = theme.current()

    def build(parent: tk.Misc, items: list[Any]) -> tk.Menu:
        menu = tk.Menu(parent, tearoff=0, background=t.bg_panel, foreground=t.text_primary,
                       activebackground=t.accent, activeforeground="#FFFFFF",
                       disabledforeground=t.text_muted, borderwidth=1, relief="solid",
                       font=theme.font(12))
        for entry in items:
            if entry is None:
                menu.add_separator()
                continue
            label, target, *rest = entry
            enabled = rest[0] if rest else True
            state = "normal" if enabled else "disabled"
            if isinstance(target, list):
                menu.add_cascade(label=label, menu=build(menu, target),
                                 state="normal" if target and enabled else "disabled")
            else:
                text, _, accel = label.partition("\t")
                menu.add_command(label=text, accelerator=accel, command=target, state=state)
        return menu

    menu = build(widget, entries)
    if x is None or y is None:
        x, y = widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height()
    try:
        menu.tk_popup(x, y)
    finally:
        menu.grab_release()
    return menu


def ask_yes_no(parent: Any, title: str, text: str) -> bool:
    return bool(messagebox.askyesno(title, text, parent=parent))


def warn(parent: Any, title: str, text: str) -> None:
    messagebox.showwarning(title, text, parent=parent)


def ask_text(parent: Any, title: str, label: str, multiline: bool = False,
             password: bool = False, initial: str = "") -> str | None:
    """A small modal text prompt (QInputDialog.getText / getMultiLineText)."""
    dialog = Dialog(parent, title)
    body = Frame(dialog, padding=16)
    body.pack(fill="both", expand=True)
    Label(body, label).pack(anchor="w", pady=(0, 6))
    if multiline:
        box = tk.Text(body, width=40, height=5, wrap="word", relief="solid", borderwidth=1,
                      highlightthickness=0, font=theme.font(12))
        box.insert("1.0", initial)
        box.pack(fill="both", expand=True)
        get = lambda: box.get("1.0", "end-1c")  # noqa: E731
        focus: Any = box
    else:
        line = LineEdit(body, initial, password=password, width=36)
        line.pack(fill="x")
        line.returnPressed.connect(dialog.accept)
        get = line.text
        focus = line
    value: list[str] = []
    row = Frame(body)
    row.pack(fill="x", pady=(12, 0))
    ok = Button(row, "OK", "primary")
    ok.pack(side="right")
    cancel = Button(row, "Cancel")
    cancel.pack(side="right", padx=6)
    ok.clicked.connect(lambda: (value.append(get()), dialog.accept()))
    cancel.clicked.connect(dialog.reject)
    dialog.after(10, focus.focus_set)
    if not multiline:
        line.returnPressed.disconnect()
        line.returnPressed.connect(lambda: (value.append(get()), dialog.accept()))
    return value[0] if dialog.exec() and value else None


def ask_int(parent: Any, title: str, label: str, value: int, lo: int, hi: int) -> int | None:
    text = ask_text(parent, title, label, initial=str(value))
    if text is None:
        return None
    try:
        return max(lo, min(hi, int(text)))
    except ValueError:
        return None


class ScrollFrame(Visible, ttk.Frame):
    """A vertically scrolling area; put children in ``self.body`` (QScrollArea)."""

    def __init__(self, master: tk.Misc, background: str | None = None,
                 max_height: int | None = None) -> None:
        super().__init__(master, style="page.TFrame")
        bg = background or theme.current().bg_app
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=bg)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = tk.Frame(self.canvas, background=bg)
        self._window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.max_height = max_height
        self.body.bind("<Configure>", self._body_changed)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window,
                                                                            width=e.width))
        for widget in (self.canvas, self.body):
            widget.bind("<Enter>", lambda e: self._bind_wheel(True))
            widget.bind("<Leave>", lambda e: self._bind_wheel(False))

    def _body_changed(self, _event: Any = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        needed = self.body.winfo_reqheight()
        if self.max_height:
            self.canvas.configure(height=min(needed, self.max_height))
        overflow = needed > max(self.canvas.winfo_height(), 1)
        if overflow and not self.bar.winfo_ismapped():
            self.bar.pack(side="right", fill="y")
        elif not overflow and self.bar.winfo_ismapped():
            self.bar.pack_forget()

    def _bind_wheel(self, on: bool) -> None:
        if on:
            self.canvas.bind_all("<MouseWheel>", self._wheel)
            self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
            self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))
        else:
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.canvas.unbind_all(sequence)

    def _wheel(self, event: Any) -> None:
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
