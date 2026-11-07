"""Design tokens from Design.md section 3, turned into ttk styles and Tk fonts."""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Inside a PyInstaller bundle, data files live under sys._MEIPASS rather than beside the source.
_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
ASSETS = _BASE / "client" / "assets"
FONT_FAMILY = "Inter"
FONT_FILES = {400: "Inter-Regular.ttf", 500: "Inter-Medium.ttf", 600: "Inter-SemiBold.ttf",
              700: "Inter-Bold.ttf"}

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
_fonts_loaded = False


def rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def valid_color(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("#") or len(value) not in (4, 7, 9):
        return False
    try:
        int(value[1:], 16)
        return True
    except ValueError:
        return False


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio, so that the accessibility requirement can be tested and not assumed."""

    def luminance(value: str) -> float:
        channels = []
        for c in (v / 255 for v in rgb(value)):
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


def font_path(weight: int = 400) -> Path:
    best = min(FONT_FILES, key=lambda w: abs(w - weight))
    return ASSETS / "fonts" / FONT_FILES[best]


def load_fonts() -> bool:
    """Register the bundled fonts for this process only. Nothing is installed on the machine,
    and the app never relies on, or downloads, anything else. Must run before Tk starts."""
    global _fonts_loaded
    files = [str(p) for p in sorted((ASSETS / "fonts").glob("*.ttf"))]
    if not files:
        return False
    try:
        if sys.platform == "win32":
            add = ctypes.windll.gdi32.AddFontResourceExW  # type: ignore[attr-defined]
            _fonts_loaded = all(add(path, 0x10, 0) > 0 for path in files) or _fonts_loaded
        elif sys.platform == "darwin":
            _fonts_loaded = _register_mac(files) or _fonts_loaded
        else:
            name = ctypes.util.find_library("fontconfig") or "libfontconfig.so.1"
            fc = ctypes.CDLL(name)
            fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            _fonts_loaded = all(fc.FcConfigAppFontAddFile(None, p.encode()) for p in files)
    except (OSError, AttributeError):
        pass
    return _fonts_loaded


def _register_mac(files: list[str]) -> bool:
    ct = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreText.framework/CoreText"
    )
    cf = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
    cf.CFURLCreateFromFileSystemRepresentation.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
    ct.CTFontManagerRegisterFontsForURL.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    ok = True
    for path in files:
        raw = path.encode()
        url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), False)
        ok = bool(ct.CTFontManagerRegisterFontsForURL(url, 1, None)) and ok
    return ok


def font_available(root: Any) -> bool:
    import tkinter.font as tkfont

    return FONT_FAMILY in tkfont.families(root)


def font(size: int = 12, weight: int = 400) -> tuple[str, int, str] | tuple[str, int]:
    """A Tk font spec. Negative sizes are pixels, matching the stylesheet's px values."""
    return (FONT_FAMILY, -size, "bold") if weight >= 600 else (FONT_FAMILY, -size)


def _indicator_images(t: Theme, radio: bool, size: int = 14) -> dict[str, Any]:
    """Pillow-drawn check and radio indicators: a fixed 14 px at any display scaling."""
    from PIL import Image, ImageDraw

    k = 4  # supersampling factor
    images = {}
    for state, fill, border in (("off", t.bg_panel, t.text_muted), ("on", t.accent, t.accent),
                                ("disabled", t.bg_app, t.border),
                                ("disabledon", t.border, t.border)):
        big = Image.new("RGBA", (size * k, size * k), (0, 0, 0, 0))
        draw = ImageDraw.Draw(big)
        box = (k, k, size * k - k - 1, size * k - k - 1)
        if radio:
            draw.ellipse(box, fill=fill, outline=border, width=k)
            if state.endswith("on"):
                c, r = size * k / 2, size * k / 6
                draw.ellipse((c - r, c - r, c + r, c + r), fill="#FFFFFF")
        else:
            draw.rounded_rectangle(box, radius=3 * k, fill=fill, outline=border, width=k)
            if state.endswith("on"):
                draw.line([(4 * k, 7.5 * k), (6.2 * k, 9.8 * k), (10 * k, 4.6 * k)],
                          fill="#FFFFFF", width=2 * k, joint="curve")
        images[state] = big.resize((size, size), Image.LANCZOS)
    return images


def _indicators(root: Any, style: Any, t: Theme) -> None:
    from PIL import Image, ImageDraw, ImageTk

    keep = root.__dict__.setdefault("_lanvas_indicators", {})
    for kind, radio in (("check", False), ("radio", True)):
        photos = {k: ImageTk.PhotoImage(v, master=root)
                  for k, v in _indicator_images(t, radio).items()}
        keep[kind] = photos  # Tk does not hold a reference to images; these must stay alive
        name = f"lanvas.{kind}.{'dark' if t is DARK else 'light'}"
        if name not in style.element_names():
            style.element_create(
                name, "image", photos["off"],
                ("disabled", "selected", photos["disabledon"]),
                ("disabled", photos["disabled"]),
                ("selected", photos["on"]),
                border=0, sticky="w",
            )
        widget = "Checkbutton" if kind == "check" else "Radiobutton"
        layout = [(f"{widget}.padding", {"sticky": "nswe", "children": [
            (name, {"side": "left", "sticky": ""}),
            (f"{widget}.focus", {"side": "left", "sticky": "w", "children": [
                (f"{widget}.label", {"sticky": "nswe"})]})]})]
        for prefix in ("", "panel."):
            style.layout(f"{prefix}T{widget}", layout)
            style.configure(f"{prefix}T{widget}", padding=(0, 2, 6, 2))
    chevrons = {}
    for state, color in (("normal", t.text_primary), ("disabled", t.text_muted)):
        big = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(big).line([(14, 24), (32, 42), (50, 24)], fill=color, width=7,
                                 joint="curve")
        chevrons[state] = ImageTk.PhotoImage(big.resize((16, 16), Image.LANCZOS), master=root)
    keep["chevron"] = chevrons
    name = f"lanvas.chevron.{'dark' if t is DARK else 'light'}"
    if name not in style.element_names():
        style.element_create(name, "image", chevrons["normal"],
                             ("disabled", chevrons["disabled"]), border=0, padding=(4, 0))
    style.layout("TCombobox", [
        (name, {"side": "right", "sticky": ""}),
        ("Combobox.field", {"sticky": "nswe", "children": [
            ("Combobox.padding", {"expand": "1", "sticky": "nswe", "children": [
                ("Combobox.textarea", {"sticky": "nswe"})]})]}),
    ])


def tk_error() -> type[Exception]:
    import tkinter

    return tkinter.TclError


def apply(root: Any, dark: bool = False) -> Theme:
    """Configure ttk so every widget draws from the tokens (the old Qt stylesheet, in ttk)."""
    from tkinter import ttk

    global _current
    _current = DARK if dark else LIGHT
    t = _current
    root.configure(background=t.bg_app)
    import tkinter.font as tkfont

    for name, size, weight in (("TkDefaultFont", 12, "normal"), ("TkTextFont", 12, "normal"),
                               ("TkMenuFont", 12, "normal"), ("TkHeadingFont", 11, "normal"),
                               ("TkCaptionFont", 13, "bold"), ("TkTooltipFont", 11, "normal"),
                               ("TkSmallCaptionFont", 11, "normal"), ("TkIconFont", 12, "normal")):
        try:
            tkfont.nametofont(name, root).configure(family=FONT_FAMILY, size=-size, weight=weight)
        except tk_error():
            pass
    root.option_add("*Background", t.bg_panel)
    root.option_add("*Foreground", t.text_primary)
    root.option_add("*selectBackground", t.accent)
    root.option_add("*selectForeground", "#FFFFFF")
    root.option_add("*insertBackground", t.text_primary)
    root.option_add("*Menu.activeBackground", t.accent)
    root.option_add("*Menu.activeForeground", "#FFFFFF")
    root.option_add("*Menu.relief", "flat")
    root.option_add("*Menu.borderWidth", 1)
    root.option_add("*TCombobox*Listbox.background", t.bg_panel)
    root.option_add("*TCombobox*Listbox.foreground", t.text_primary)
    style = ttk.Style(root)
    style.theme_use("clam")
    flat = {"bordercolor": t.border, "lightcolor": t.bg_panel, "darkcolor": t.bg_panel,
            "focuscolor": t.accent, "borderwidth": 1}
    style.configure(".", background=t.bg_app, foreground=t.text_primary, font=font(12),
                    fieldbackground=t.bg_panel, troughcolor=t.bg_canvas, **flat)
    for name, bg in (("TFrame", t.bg_app), ("page.TFrame", t.bg_app), ("panel.TFrame", t.bg_panel),
                     ("bar.TFrame", t.bg_panel), ("card.TFrame", t.bg_panel),
                     ("banner.TFrame", t.warn), ("private.TFrame", t.private)):
        style.configure(name, background=bg)
    style.configure("card.TFrame", relief="solid", borderwidth=1, bordercolor=t.border)
    labels = {
        "TLabel": (t.bg_app, t.text_primary, font(12)),
        "panel.TLabel": (t.bg_panel, t.text_primary, font(12)),
        "muted.TLabel": (t.bg_app, t.text_muted, font(12)),
        "panelmuted.TLabel": (t.bg_panel, t.text_muted, font(12)),
        "section.TLabel": (t.bg_panel, t.text_muted, font(11, 600)),
        "pagesection.TLabel": (t.bg_app, t.text_muted, font(11, 600)),
        "title.TLabel": (t.bg_panel, t.text_primary, font(13, 600)),
        "pagetitle.TLabel": (t.bg_app, t.text_primary, font(13, 600)),
        "hero.TLabel": (t.bg_app, t.text_primary, font(18, 600)),
        "cardhero.TLabel": (t.bg_panel, t.text_primary, font(18, 600)),
        "banner.TLabel": (t.warn, "#1E1E1E", font(12)),
        "private.TLabel": (t.private, t.on_private, font(11)),
        "danger.TLabel": (t.bg_app, t.danger, font(12)),
    }
    for name, (bg, fg, f) in labels.items():
        style.configure(name, background=bg, foreground=fg, font=f)
    button = {"background": t.bg_panel, "foreground": t.text_primary, "padding": (10, 4),
              "relief": "solid", "anchor": "center"}
    style.configure("TButton", **button)
    style.map("TButton", background=[("disabled", t.bg_panel), ("active", t.hover)],
              foreground=[("disabled", t.text_muted)], bordercolor=[("focus", t.accent)])
    style.configure("primary.TButton", background=t.accent, foreground="#06263F",
                    font=font(12, 600), bordercolor=t.accent)
    style.map("primary.TButton", background=[("disabled", t.hover), ("active", "#3DADFF")])
    style.configure("danger.TButton", foreground=t.danger)
    style.configure("banner.TButton", background=t.warn, foreground="#1E1E1E",
                    bordercolor="#1E1E1E")
    style.configure("flat.TButton", relief="flat", borderwidth=0, background=t.bg_panel,
                    bordercolor=t.bg_panel, padding=(4, 2))
    style.configure("title.TButton", font=font(13, 600))
    style.configure("room.TButton", font=font(13, 600), padding=(8, 4))
    style.configure("Tool.TButton", background=t.bg_panel, relief="flat", borderwidth=0,
                    bordercolor=t.bg_panel, padding=4)
    style.map("Tool.TButton", background=[("disabled", t.bg_panel), ("active", t.hover)])
    style.configure("Tool.TCheckbutton", background=t.bg_panel, relief="flat", borderwidth=0,
                    bordercolor=t.bg_panel, padding=4, indicatorthickness=0)
    style.layout("Tool.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("Checkbutton.label", {"sticky": "nswe"})]})])
    style.map("Tool.TCheckbutton",
              background=[("selected", t.accent), ("disabled", t.bg_panel), ("active", t.hover)],
              foreground=[("selected", "#FFFFFF"), ("disabled", t.text_muted)])
    style.configure("Page.Tool.TCheckbutton", background=t.bg_app, bordercolor=t.bg_app)
    style.map("Page.Tool.TCheckbutton", background=[("active", t.hover), ("!active", t.bg_app)])
    style.configure("TEntry", padding=(6, 3), fieldbackground=t.bg_panel,
                    foreground=t.text_primary, insertcolor=t.text_primary)
    style.map("TEntry", bordercolor=[("focus", t.accent)], lightcolor=[("focus", t.accent)],
              fieldbackground=[("readonly", t.bg_panel), ("disabled", t.bg_app)])
    _indicators(root, style, t)
    style.configure("TCombobox", padding=(6, 2), arrowcolor=t.text_primary,
                    background=t.bg_panel, foreground=t.text_primary)
    style.map("TCombobox", fieldbackground=[("readonly", t.bg_panel)],
              bordercolor=[("focus", t.accent)], foreground=[("disabled", t.text_muted)])
    style.configure("TSpinbox", padding=(6, 2), arrowsize=10, arrowcolor=t.text_primary)
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=t.bg_app, foreground=t.text_primary,
                        indicatorbackground=t.bg_panel, indicatorforeground=t.accent)
        style.map(name, background=[("active", t.bg_app)],
                  foreground=[("disabled", t.text_muted)],
                  indicatorbackground=[("selected", t.accent), ("disabled", t.bg_app)])
        style.configure("panel." + name, background=t.bg_panel)
        style.map("panel." + name, background=[("active", t.bg_panel)])
    style.configure("TNotebook", background=t.bg_panel, borderwidth=0, tabmargins=0)
    style.configure("TNotebook.Tab", background=t.bg_panel, foreground=t.text_muted,
                    padding=(10, 6), borderwidth=0, font=font(12))
    style.map("TNotebook.Tab", background=[("selected", t.bg_panel)],
              foreground=[("selected", t.text_primary)], font=[("selected", font(12, 600))],
              lightcolor=[("selected", t.bg_panel)], bordercolor=[("selected", t.bg_panel)])
    style.configure("Treeview", background=t.bg_panel, fieldbackground=t.bg_panel,
                    foreground=t.text_primary, rowheight=24, borderwidth=0, relief="flat")
    style.map("Treeview", background=[("selected", t.accent)],
              foreground=[("selected", "#FFFFFF")])
    style.configure("Treeview.Heading", background=t.bg_panel, foreground=t.text_muted,
                    font=font(11), relief="flat", bordercolor=t.border)
    style.configure("Horizontal.TScale", background="#FFFFFF", troughcolor=t.accent,
                    bordercolor=t.border, lightcolor="#FFFFFF", darkcolor="#FFFFFF",
                    sliderthickness=14, sliderlength=14, troughrelief="flat", gripcount=0,
                    borderwidth=1)
    style.map("Horizontal.TScale", background=[("active", t.hover)])
    style.configure("TScrollbar", background=t.bg_panel, troughcolor=t.bg_panel,
                    arrowcolor=t.text_muted, bordercolor=t.bg_panel, relief="flat")
    style.configure("TPanedwindow", background=t.border)
    style.configure("Sash", sashthickness=1, gripcount=0, background=t.border)
    style.configure("TProgressbar", background=t.accent, troughcolor=t.border)
    style.configure("TSeparator", background=t.border)
    return _current
