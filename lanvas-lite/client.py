"""LANvas client: a PyQt6 whiteboard that talks to server.py over one TCP socket.

Threads: a network thread (class Net) does all socket reads and pushes decoded messages into a queue;
the Qt main thread polls that queue every 25 ms (QTimer) and is the only code that touches widgets.
The board is a custom-painted widget: the model stays in board coordinates (1600 x 1000) and is drawn
through a zoom + pan transform.
Run:  python client.py          (needs:  pip install PyQt6)
"""
import base64
import json
import math
import os
import queue
import socket
import sys
import threading
import time
import uuid
from html import escape as html_escape
from pathlib import Path
from xml.sax.saxutils import escape

from PyQt6.QtCore import QBuffer, QByteArray, QMarginsF, QSize, QEasingCurve, QPointF, QPropertyAnimation, QRectF, QSizeF, Qt, QTimer
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QGuiApplication, QImage, QKeySequence,
                         QPageSize, QPainter, QPainterPath, QPainterPathStroker, QPdfWriter, QPen, QPixmap, QPolygonF, QShortcut, QIcon)
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog, QFileDialog,
                             QFormLayout, QFrame, QScrollArea, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                             QMessageBox, QPushButton, QSizePolicy, QSlider, QSpinBox, QStackedWidget, QTabWidget, QTextBrowser,
                             QToolButton, QVBoxLayout, QWidget)

from common import ATTRS, PORT, apply_op, derive_key, erase_pieces, outline, pack, proof, recv_msg

TOKENS = Path.home() / ".lanvas_tokens.json"     # session tokens, so a restarted client keeps its identity
PREFS = Path.home() / ".lanvas_prefs.json"       # UI preferences (dark mode)
MAX_PTS = 1400          # a stroke is split before it hits the server's per-object point limit
EVERYONE = "Everyone"
BOARD_W, BOARD_H = 1600, 1000
ZOOMS = [0.25, 0.33, 0.5, 0.67, 0.8, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0]
MAX_IMG, IMG_SIDE = 700_000, 800        # base64 chars per image (fits one frame); longest side in px
ADMIN = ("host", "admin")
ROLE_NAME = {"host": "Owner", "admin": "Admin", "editor": "Editor", "viewer": "Viewer"}
PALETTE = ["#1d1f24", "#e5484d", "#f76b15", "#ffc53d", "#30a46c", "#0090ff", "#6d5dfc", "#d6409f"]
# (tool id, icon, name, shortcut)
TOOLS = [("move", "", "Move / select — drag to move, handles to resize", "V"), ("pen", "✎", "Pen", "P"), ("rub", "⌫", "Eraser", "E"), ("eraser", "⊗", "Erase object", "X"),
         ("fill", "◍", "Fill", "F"), ("line", "╱", "Line", "L"), ("rect", "▭", "Rectangle", "R"),
         ("oval", "◯", "Oval", "O"), ("text", "T", "Text", "T"), ("note", "▤", "Sticky note", "N"),
         ("poly", "⬠", "Polygon — click points, double-click / Enter to finish", "G"),
         ("image", "▣", "Image", "I"), ("button", "⊡", "Link button (goes to another page)", "B")]

THEMES = {
    "light": {"bg": "#eef0f4", "panel": "#ffffff", "border": "#e3e5ea", "fg": "#1d1f24", "muted": "#7a7f8c",
              "accent": "#6d5dfc", "accent2": "#5a4ae6", "hover": "#f2f3f6", "sel": "#ece9ff", "field": "#f6f7f9",
              "canvas": "#ffffff", "grid": "#dfe2e8", "dm": "#8e44ad", "danger": "#e5484d", "tip": "#1d1f24",
              "ok": "#30a46c", "shadow": 38},
    "dark": {"bg": "#141518", "panel": "#1e1f23", "border": "#2c2e34", "fg": "#ecedf0", "muted": "#8d919c",
             "accent": "#8b7dff", "accent2": "#a094ff", "hover": "#2a2c31", "sel": "#322d55", "field": "#26282d",
             "canvas": "#1a1b1e", "grid": "#2c2e33", "dm": "#d29bf0", "danger": "#ff6369", "tip": "#000000",
             "ok": "#3dd68c", "shadow": 120},
}

QSS = """
* {{ font-family: "Inter", "Segoe UI", "SF Pro Text", "Cantarell", "Noto Sans", sans-serif; font-size: 13px;
     color: {fg}; }}
QMainWindow, #root, #page {{ background: {bg}; }}
#card {{ background: {panel}; border: 1px solid {border}; border-radius: 14px; }}
QLabel#title {{ font-size: 22px; font-weight: 700; }}
QLabel#h2 {{ font-size: 14px; font-weight: 600; }}
QLabel#muted, QLabel#status {{ color: {muted}; }}
QLabel#chip {{ background: {sel}; color: {accent}; border-radius: 9px; padding: 4px 10px; font-weight: 600; }}
QLabel#pill {{ background: {hover}; color: {muted}; border-radius: 8px; padding: 1px 8px; font-size: 11px; }}
QLabel#error {{ color: {danger}; }}
QLabel#banner {{ background: {danger}; color: white; border-radius: 10px; padding: 8px 16px; font-weight: 600; }}
QLabel#toast, QLabel#overlay {{ background: {tip}; color: white; border-radius: 12px; padding: 9px 16px; }}
QToolButton, QPushButton {{ background: transparent; border: none; border-radius: 9px; padding: 6px 10px; }}
QToolButton:hover, QPushButton:hover {{ background: {hover}; }}
QToolButton:pressed, QPushButton:pressed {{ background: {sel}; }}
QToolButton:checked, QPushButton:checked {{ background: {sel}; color: {accent}; }}
QToolButton#tool {{ font-size: 16px; min-width: 34px; min-height: 34px; max-width: 34px; max-height: 34px;
                    padding: 0; }}
QPushButton#primary {{ background: {accent}; color: white; font-weight: 600; padding: 8px 16px; }}
QPushButton#primary:hover {{ background: {accent2}; }}
QPushButton#soft {{ background: {hover}; }}
QPushButton#soft:hover {{ background: {sel}; }}
QPushButton#danger {{ color: {danger}; }}
QPushButton#seg {{ color: {muted}; padding: 6px 16px; }}
QPushButton#seg:checked {{ background: {panel}; color: {fg}; font-weight: 600; }}
#segbar {{ background: {hover}; border-radius: 11px; }}
QPushButton:disabled, QToolButton:disabled {{ color: {muted}; background: transparent; }}
QPushButton#primary:disabled {{ background: {hover}; }}
QLineEdit, QComboBox, QTextBrowser, QListWidget, QSpinBox {{ background: {field}; border: 1px solid {border};
    border-radius: 9px; padding: 6px 8px; selection-background-color: {sel}; selection-color: {fg}; }}
QSpinBox {{ padding: 3px 6px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}
QSpinBox:disabled {{ color: {muted}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {accent}; }}
QLineEdit:disabled {{ color: {muted}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{ background: {panel}; border: 1px solid {border}; selection-background-color: {sel}; }}
QListWidget {{ padding: 4px; outline: none; }}
QListWidget::item {{ padding: 6px; border-radius: 8px; }}
QListWidget::item:hover {{ background: {hover}; }}
QListWidget::item:selected {{ background: {sel}; color: {fg}; }}
QTabWidget::pane {{ border: none; }}
QTabBar {{ background: {hover}; border-radius: 11px; }}
QTabBar::tab {{ background: transparent; color: {muted}; padding: 6px 18px; margin: 3px; border-radius: 8px; }}
QTabBar::tab:selected {{ background: {panel}; color: {fg}; font-weight: 600; }}
QSlider::groove:horizontal {{ height: 4px; background: {border}; border-radius: 2px; }}
QSlider::groove:vertical {{ width: 4px; background: {border}; border-radius: 2px; }}
QSlider::handle {{ background: {accent}; width: 14px; height: 14px; margin: -5px; border-radius: 7px; }}
QSlider::sub-page:horizontal, QSlider::add-page:vertical {{ background: {accent}; border-radius: 2px; }}
QMenu {{ background: {panel}; border: 1px solid {border}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 22px 7px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {sel}; }}
QMenu::item:disabled {{ color: {muted}; }}
QMenu::separator {{ height: 1px; background: {border}; margin: 4px 8px; }}
QToolTip {{ background: {tip}; color: white; border: none; padding: 5px 8px; border-radius: 6px; }}
QScrollBar {{ background: transparent; width: 8px; height: 8px; }}
QScrollBar::handle {{ background: {border}; border-radius: 4px; min-height: 24px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none;
    border: none; width: 0; height: 0; }}
QDialog, QMessageBox, QInputDialog {{ background: {panel}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
"""


class Fatal(Exception):
    """Connection failed in a way retrying cannot fix (wrong password, bad room, kicked...)."""


def load_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_json(path, data):
    try:
        path.write_text(json.dumps(data))
    except OSError:
        pass


def fetch_rooms(host, port, q):
    """Lobby: ask the server for its public open rooms (runs in a short-lived thread)."""
    try:
        with socket.create_connection((host, port), timeout=3) as s:
            s.settimeout(3)
            s.sendall(pack({"t": "list"}))
            m = recv_msg(s)
        q.put({"t": "rooms", "rooms": m.get("rooms", [])})
    except (OSError, ValueError) as e:
        q.put({"t": "rooms", "rooms": None, "text": f"Cannot list rooms: {e}"})


def load_image(path):
    """Read an image file (PNG, JPEG, GIF, BMP, WebP...) -> base64 PNG small enough to send."""
    img = QImage(path)
    if img.isNull():
        raise ValueError("Cannot read that image")
    side = IMG_SIDE
    while True:
        im = img.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation) \
            if max(img.width(), img.height()) > side else img
        buf = QBuffer()
        buf.open(QBuffer.OpenModeFlag.WriteOnly)
        im.save(buf, "PNG")
        data = base64.b64encode(bytes(buf.data())).decode()
        if len(data) <= MAX_IMG or side < 64:
            break
        side = int(side * 0.75)
    if len(data) > MAX_IMG:
        raise ValueError("Image is too large even after shrinking")
    return data


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = 0 if dx == dy == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def hit(o, x, y):
    """Is (x, y) inside a fillable object? (rect, oval, note, polygon, button, or a closed pen stroke)"""
    k, p = o["k"], o["p"]
    if k == "poly":
        o, k, p = {**o, "k": "stroke"}, "stroke", list(p) + list(p[:2])
    if k in ("rect", "button"):
        return min(p[0], p[2]) <= x <= max(p[0], p[2]) and min(p[1], p[3]) <= y <= max(p[1], p[3])
    if k == "oval":
        rx, ry = abs(p[2] - p[0]) / 2, abs(p[3] - p[1]) / 2
        cx, cy = (p[0] + p[2]) / 2, (p[1] + p[3]) / 2
        return rx > 0 and ry > 0 and ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1
    if k == "note":
        return p[0] <= x <= p[0] + 170 and p[1] <= y <= p[1] + 130
    if k == "stroke" and closed(p):
        inside, n = False, len(p) // 2
        for i in range(n):          # ray casting point-in-polygon
            x1, y1, x2, y2 = p[2 * i], p[2 * i + 1], p[2 * ((i + 1) % n)], p[2 * ((i + 1) % n) + 1]
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
        return inside
    return False



def closed(p):
    return len(p) >= 8 and abs(p[0] - p[-2]) + abs(p[1] - p[-1]) <= 25


class Net(threading.Thread):
    """Connects, logs in, then pumps server messages into `q`. After any drop it reconnects
    automatically with backoff, re-authenticating with the saved session token."""

    def __init__(self, cfg, q):
        super().__init__(daemon=True)
        self.cfg, self.q, self.sock, self.quit = cfg, q, None, False
        self.lock, self.was_up = threading.Lock(), False
        self.answered, self.answer = threading.Event(), True

    def send(self, msg):
        with self.lock:
            try:
                if self.sock:
                    self.sock.sendall(pack(msg))
            except OSError:
                pass        # the read loop notices the dead socket and reconnects

    def handshake(self, s):
        c = self.cfg
        hello = {"t": "hello", "user": c["user"], "token": c.get("token", "")}
        if c["create"]:
            hello.update(create=True, role=c["role"], salt=c["salt"], key=c["key"], private=c["private"],
                         approval=c["approval"])
        if c.get("want"):
            hello["want"] = c["want"]
        hello["room"] = c["room"]
        s.sendall(pack(hello))
        m = recv_msg(s)
        if m["t"] == "challenge":
            s.sendall(pack({"t": "auth", "proof": proof(derive_key(c["password"], m["salt"]), m["nonce"])}))
            m = recv_msg(s)
        if m["t"] == "waiting":          # approval room: wait (up to 5 min) for the host or an admin
            self.q.put(m)
            s.settimeout(None)
            m = recv_msg(s)
            s.settimeout(15)
        if m["t"] == "recover":          # reopening a closed room: the UI asks restore-or-fresh
            self.answered.clear()
            self.q.put(m)
            s.settimeout(None)
            self.answered.wait()
            s.settimeout(15)
            s.sendall(pack({"t": "recover", "use": self.answer}))
            m = recv_msg(s)
        if m["t"] != "welcome":
            raise Fatal(m.get("text", "Login failed"))
        self.sock = s       # only now may the UI send; set before the welcome is queued
        self.q.put(m)

    def run(self):
        delay = 1
        while not self.quit:
            s = None
            try:
                s = socket.create_connection((self.cfg["host"], self.cfg["port"]), timeout=5)
                s.settimeout(15)     # server pings every 5 s, so 15 s of silence means it is gone
                self.handshake(s)
                self.was_up, delay = True, 1
                while True:
                    m = recv_msg(s)
                    if m.get("t") == "bye":
                        raise ConnectionError(m.get("text", "server shut down"))
                    self.q.put(m)
            except Fatal as e:
                self.q.put({"t": "fatal", "text": str(e)})
                return
            except Exception as e:          # any other failure: reconnect, never let this thread die silently
                if not isinstance(e, (OSError, ValueError)):
                    sys.__stderr__.write(f"network error: {e!r}\n")
                if not self.was_up:
                    self.q.put({"t": "fatal", "text": f"Cannot connect: {e}"})
                    return
                self.q.put({"t": "down", "text": str(e)})
            finally:
                self.sock = None
                if s:
                    s.close()
            time.sleep(delay)
            delay = min(delay * 2, 8)




# ---------------------------------------------------------------- drawing
class Renderer:
    """Draws board objects with QPainter in board coordinates. The board widget and the PNG/JPG/PDF
    exports share it, so what you export is exactly what you see."""

    def __init__(self):
        self.paths, self.images = {}, {}

    @staticmethod
    def font(px, bold=False):
        f = QFont()
        f.setPixelSize(max(1, round(px)))
        f.setBold(bold)
        return f

    def path(self, o):
        hit = self.paths.get(o["id"])
        if hit and hit[0] is o["p"]:
            return hit[1]
        p, path = o["p"], QPainterPath()
        path.moveTo(p[0], p[1])
        for i in range(2, len(p), 2):
            path.lineTo(p[i], p[i + 1])
        if o["k"] == "poly":
            path.closeSubpath()
        self.paths[o["id"]] = (o["p"], path)
        return path

    def image(self, o):
        if o["id"] not in self.images:
            img = QImage()
            img.loadFromData(QByteArray(base64.b64decode(o["s"])))
            self.images[o["id"]] = img
        return self.images[o["id"]]

    def text_font(self, o):
        if o["k"] == "button":
            return self.font(14, True)
        return self.font((10 + 2 * o["w"]) * 4 / 3) if o["k"] == "text" else self.font(13)

    def gbox(self, o):
        """Geometric box (the points themselves): what move / resize and the X/Y/W/H fields act on."""
        k, p = o["k"], o["p"]
        if k == "group":
            r = QRectF()
            for it in o["items"]:
                r = r.united(self.gbox(it))
            return r
        if k in ("text", "note") or (k == "image" and len(p) == 2):
            return self.bbox(o)
        xs, ys = p[0::2], p[1::2]
        return QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    def bbox(self, o):
        """Visual box (includes stroke width): hit testing and selection outline."""
        k, p, w = o["k"], o["p"], o["w"]
        if k == "text":
            flags = int(Qt.TextFlag.TextWordWrap)
            return QFontMetricsF(self.text_font(o)).boundingRect(QRectF(p[0], p[1], 500, 1e5), flags, o["s"])
        if k == "note":
            return QRectF(p[0], p[1], 170, 130)
        if k == "image" and len(p) == 2:
            img = self.image(o)
            return QRectF(p[0], p[1], img.width() or 80, img.height() or 60)
        if k == "group":
            r = QRectF()
            for it in o["items"]:
                r = r.united(self.bbox(it))
            return r
        g = self.gbox(o)
        return g if k in ("image", "button") else g.adjusted(-w / 2, -w / 2, w / 2, w / 2)

    def area(self, o):
        """The painted region of a layer: what a clipped layer above it is limited to (FireAlpaca-style)."""
        k, path = o["k"], QPainterPath()
        if k == "group":
            for it in o["items"]:
                path = path.united(self.area(it))
        elif k == "oval":
            path.addEllipse(self.gbox(o))
        elif k == "poly":
            path = QPainterPath(self.path(o))
        elif k in ("stroke", "line"):
            st = QPainterPathStroker()
            st.setWidth(max(1, o["w"]))
            st.setCapStyle(Qt.PenCapStyle.RoundCap)
            st.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            path = st.createStroke(self.path(o))
            if o.get("f") and closed(o["p"]):
                path = path.united(self.path(o))
        else:
            path.addRect(self.bbox(o))
        return path

    def draw_page(self, g, objs):
        """Draw a page's layers in order. Hidden layers are skipped; a clipped layer is only painted where
        the nearest unclipped layer below it is painted (and vanishes if that layer is hidden)."""
        base = None
        for o in objs:
            if o.get("cl") and base is not None:
                if not (o.get("h") or base.get("h")):
                    g.save()
                    g.setClipPath(self.area(base), Qt.ClipOperation.IntersectClip)
                    self.draw(g, o)
                    g.restore()
                continue
            base = o
            if not o.get("h"):
                self.draw(g, o)

    def draw(self, g, o):
        a = o.get("a", 100)
        if a < 100:
            g.save()
            g.setOpacity(g.opacity() * a / 100)
        k, p, c, w, f = o["k"], o["p"], QColor(o["c"]), o["w"], o.get("f") or ""
        pen = QPen(c, w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        g.setPen(pen)
        g.setBrush(QBrush(QColor(f)) if f else Qt.BrushStyle.NoBrush)
        if k == "group":
            for it in o["items"]:
                self.draw(g, it)
        elif k in ("stroke", "line"):
            if k == "stroke" and f and closed(p):
                g.fillPath(self.path(o), QColor(f))
            if len(p) == 2:
                g.drawPoint(QPointF(p[0], p[1]))
            else:
                g.strokePath(self.path(o), pen)
        elif k == "poly":
            g.drawPath(self.path(o))
        elif k == "rect":
            g.drawRect(self.gbox(o))
        elif k == "oval":
            g.drawEllipse(self.gbox(o))
        elif k == "button":
            r = self.gbox(o)
            g.setPen(Qt.PenStyle.NoPen)
            g.setBrush(QColor(f or "#6d5dfc"))
            g.drawRoundedRect(r, min(12, r.height() / 2), min(12, r.height() / 2))
            g.setPen(c)
            g.setFont(self.text_font(o))
            g.drawText(r, int(Qt.AlignmentFlag.AlignCenter), o["s"])
        elif k == "text":
            g.setFont(self.text_font(o))
            g.drawText(QRectF(p[0], p[1], 500, 1e5), int(Qt.TextFlag.TextWordWrap), o["s"])
        elif k == "note":
            g.setPen(QPen(c, 2))
            g.setBrush(QColor(f or "#fff59d"))
            g.drawRoundedRect(QRectF(p[0], p[1], 170, 130), 6, 6)
            g.setPen(QColor("#222222"))
            g.setFont(self.font(13))
            g.drawText(QRectF(p[0] + 8, p[1] + 8, 154, 114), int(Qt.TextFlag.TextWordWrap), o["s"])
        elif k == "image":
            img, box = self.image(o), self.bbox(o)
            if img.isNull():
                g.setPen(QPen(c, 1, Qt.PenStyle.DashLine))
                g.drawRect(box)
            else:
                g.drawImage(box, img)
        if a < 100:
            g.restore()


def make_icon(name, color, size=18):
    """Small line icons painted with QPainter (no image files needed)."""
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.GlobalColor.transparent)
    g = QPainter(pm)
    g.setRenderHint(QPainter.RenderHint.Antialiasing)
    g.scale(2 * size / 18, 2 * size / 18)
    pen = QPen(QColor(color), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    g.setPen(pen)
    g.setBrush(Qt.BrushStyle.NoBrush)
    if name == "move":          # pointer arrow + four-way move cross
        g.setBrush(QColor(color))
        g.drawPolygon(QPolygonF([QPointF(2, 2), QPointF(10, 5), QPointF(6.5, 6.5), QPointF(5, 10)]))
        g.setBrush(Qt.BrushStyle.NoBrush)
        for a, b in (((13, 8.5), (13, 17)), ((8.5, 13), (17, 13))):
            g.drawLine(QPointF(*a), QPointF(*b))
        for x, y, dx, dy in ((13, 8.5, 1, 1), (13, 17, 1, -1), (8.5, 13, 1, 1), (17, 13, -1, 1)):
            if x == 13:
                g.drawPolyline(QPolygonF([QPointF(x - 1.6, y + 1.6 * dy), QPointF(x, y), QPointF(x + 1.6, y + 1.6 * dy)]))
            else:
                g.drawPolyline(QPolygonF([QPointF(x + 1.6 * dx, y - 1.6), QPointF(x, y), QPointF(x + 1.6 * dx, y + 1.6)]))
    elif name in ("eye", "eye-off"):
        path = QPainterPath()
        path.moveTo(1.5, 9)
        path.quadTo(9, 0.5, 16.5, 9)
        path.quadTo(9, 17.5, 1.5, 9)
        g.drawPath(path)
        g.setBrush(QColor(color))
        g.drawEllipse(QPointF(9, 9), 2.4, 2.4)
        if name == "eye-off":
            g.drawLine(QPointF(3, 15), QPointF(15, 3))
    elif name in ("lock", "unlock"):
        g.drawRoundedRect(QRectF(4, 8, 10, 8), 1.5, 1.5)
        path = QPainterPath()
        path.moveTo(6.2, 8)
        path.lineTo(6.2, 5.5)
        path.arcTo(QRectF(6.2, 2, 5.6, 6), 180, -180)
        path.lineTo(11.8, 8 if name == "lock" else 6)
        g.drawPath(path)
    elif name == "clip":        # FireAlpaca's "clip to layer below" arrow
        g.drawPolyline(QPolygonF([QPointF(5, 3), QPointF(5, 12), QPointF(14, 12)]))
        g.drawPolyline(QPolygonF([QPointF(11, 9), QPointF(14, 12), QPointF(11, 15)]))
    elif name == "link":
        g.drawRoundedRect(QRectF(2, 7, 8, 5), 2.5, 2.5)
        g.drawRoundedRect(QRectF(8, 6, 8, 5), 2.5, 2.5)
    g.end()
    return QIcon(pm)


def card(shadow=True):
    """A white rounded panel with a soft drop shadow (Figma / Canva style)."""
    f = QFrame()
    f.setObjectName("card")
    if shadow:
        fx = QGraphicsDropShadowEffect(f)
        fx.setBlurRadius(24)
        fx.setOffset(0, 2)
        fx.setColor(QColor(0, 0, 0, 30))
        f.setGraphicsEffect(fx)
    return f


def scroller(inner, vpolicy=Qt.ScrollBarPolicy.ScrollBarAsNeeded):
    """Wrap a widget so it scrolls instead of forcing the window to grow (small / scaled screens)."""
    sa = QScrollArea()
    sa.setWidget(inner)
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    sa.setVerticalScrollBarPolicy(vpolicy)
    sa.setMinimumHeight(40)
    return sa


def label(text="", name=None, wrap=False):
    w = QLabel(text)
    if name:
        w.setObjectName(name)
    w.setWordWrap(wrap)
    return w


def button(text="", name=None, tip="", cb=None, checkable=False, tool=False):
    b = QToolButton() if tool else QPushButton()
    b.setText(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if name:
        b.setObjectName(name)
    if tip:
        b.setToolTip(tip)
    if cb:
        b.clicked.connect(lambda *_: cb())
    b.setCheckable(checkable)
    return b


class Avatar(QWidget):
    def __init__(self, name, color, size=28, online=True):
        super().__init__()
        self.name, self.color, self.online = name, color, online
        self.setFixedSize(size, size)
        self.setToolTip(name)

    def paintEvent(self, _):
        g = QPainter(self)
        g.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self.width()
        g.setPen(QPen(QColor("#ffffff"), 2))
        g.setBrush(QColor(self.color if self.online else "#9aa0aa"))
        g.drawEllipse(QRectF(1, 1, s - 2, s - 2))
        g.setPen(QColor("#ffffff"))
        g.setFont(Renderer.font(s * 0.4))
        initials = "".join(p[0] for p in self.name.split()[:2]).upper()[:2] or "?"
        g.drawText(QRectF(0, 0, s, s), int(Qt.AlignmentFlag.AlignCenter), initials)


class Switch(QCheckBox):
    """A toggle switch (pill + knob) instead of a square checkbox."""

    def __init__(self, text, app):
        super().__init__(text)
        self.app = app
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self):
        s = super().sizeHint()
        return s.expandedTo(s.__class__(46 + self.fontMetrics().horizontalAdvance(self.text()), 26))

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, _):
        t, on = self.app.t, self.isChecked()
        g = QPainter(self)
        g.setRenderHint(QPainter.RenderHint.Antialiasing)
        g.setPen(Qt.PenStyle.NoPen)
        track = QColor(t["accent"] if on else t["border"])
        if not self.isEnabled():
            track = QColor(t["hover"])
        g.setBrush(track)
        y = (self.height() - 20) / 2
        g.drawRoundedRect(QRectF(0, y, 36, 20), 10, 10)
        g.setBrush(QColor("#ffffff"))
        g.drawEllipse(QRectF(18 if on else 2, y + 2, 16, 16))
        g.setPen(QColor(t["fg"] if self.isEnabled() else t["muted"]))
        g.drawText(QRectF(44, 0, self.width() - 44, self.height()), int(Qt.AlignmentFlag.AlignVCenter), self.text())


class Board(QWidget):
    """The canvas: an infinite-feeling workspace with the white board page on it. Zoom with Ctrl+wheel
    (at the pointer); pan with the wheel, middle-drag or Space+drag."""

    def __init__(self, app):
        super().__init__()
        self.app, self.zi, self.ox, self.oy = app, ZOOMS.index(1.0), -40.0, -40.0
        self.panning, self.space, self.last, self.mouse = None, False, None, None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.banner = label("", "banner")
        self.overlay = label("", "overlay")
        for w in (self.banner, self.overlay):
            w.setParent(self)
            w.hide()

    @property
    def z(self):
        return ZOOMS[self.zi]

    def to_board(self, pt):
        return round((pt.x() + self.ox) / self.z), round((pt.y() + self.oy) / self.z)

    def place_labels(self):
        for w, y in ((self.banner, 12), (self.overlay, self.height() - 52)):
            w.adjustSize()
            w.move((self.width() - w.width()) // 2, y)

    def resizeEvent(self, e):
        self.place_labels()

    def set_zoom(self, zi, anchor=None):
        zi = min(max(zi, 0), len(ZOOMS) - 1)
        ax, ay = (anchor.x(), anchor.y()) if anchor else (self.width() / 2, self.height() / 2)
        bx, by = (ax + self.ox) / self.z, (ay + self.oy) / self.z
        self.zi = zi
        self.ox, self.oy = bx * self.z - ax, by * self.z - ay
        self.app.zoom_changed()
        self.update()

    def fit(self):
        w, h = self.width() - 60, self.height() - 60
        self.zi = max([i for i, z in enumerate(ZOOMS) if BOARD_W * z <= w and BOARD_H * z <= h] or [0])
        self.ox = (BOARD_W * self.z - self.width()) / 2
        self.oy = (BOARD_H * self.z - self.height()) / 2
        self.app.zoom_changed()
        self.update()

    def center_on(self, cx, cy):
        self.ox, self.oy = cx * self.z - self.width() / 2, cy * self.z - self.height() / 2
        self.update()

    # ---- input
    def mousePressEvent(self, e):
        self.setFocus()
        if e.button() == Qt.MouseButton.MiddleButton or (e.button() == Qt.MouseButton.LeftButton and self.space):
            self.panning = e.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif e.button() == Qt.MouseButton.LeftButton:
            self.app.press(*self.to_board(e.position()))
        elif e.button() == Qt.MouseButton.RightButton:
            self.app.board_menu(e.globalPosition().toPoint(), *self.to_board(e.position()))

    def mouseMoveEvent(self, e):
        if self.panning is not None:
            d = e.position() - self.panning
            self.panning = e.position()
            self.ox, self.oy = self.ox - d.x(), self.oy - d.y()
            return self.update()
        x, y = self.to_board(e.position())
        self.mouse = (x, y)
        self.app.motion(x, y)
        if e.buttons() & Qt.MouseButton.LeftButton:
            self.app.drag(x, y)
        else:
            if not self.space:
                self.setCursor(self.app.hover_cursor(x, y))
            if self.app.tool in ("rub", "poly"):
                self.update()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.app.finish_poly()

    def mouseReleaseEvent(self, e):
        if self.panning is not None:
            self.panning = None
            self.setCursor(Qt.CursorShape.OpenHandCursor if self.space else Qt.CursorShape.CrossCursor)
        elif e.button() == Qt.MouseButton.LeftButton:
            self.app.release(*self.to_board(e.position()))

    def leaveEvent(self, _):
        self.mouse = None
        self.update()

    def wheelEvent(self, e):
        d = e.angleDelta()
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return self.set_zoom(self.zi + (1 if d.y() > 0 else -1), e.position())
        dx, dy = (d.y(), 0) if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else (d.x(), d.y())
        self.ox, self.oy = self.ox - dx / 2, self.oy - dy / 2
        self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Space and not e.isAutoRepeat():
            self.space = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.app.finish_poly()
        else:
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key.Key_Space and not e.isAutoRepeat():
            self.space = False
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            super().keyReleaseEvent(e)

    # ---- painting
    def paintEvent(self, _):
        app, t, z = self.app, self.app.t, self.z
        g = QPainter(self)
        g.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        g.fillRect(self.rect(), QColor(t["bg"]))
        g.translate(-self.ox, -self.oy)
        g.scale(z, z)
        page = QRectF(0, 0, BOARD_W, BOARD_H)
        g.setPen(Qt.PenStyle.NoPen)
        for i, a in enumerate((8, 14, 22)):           # soft page shadow
            g.setBrush(QColor(0, 0, 0, round(t["shadow"] * a / 100 / (i + 1))))
            g.drawRoundedRect(page.adjusted(-i * 2 / z, -i / z, i * 2 / z, (i * 3 + 2) / z), 10, 10)
        g.setBrush(QColor(t["canvas"]))
        g.drawRoundedRect(page, 8, 8)
        if z >= 0.5:                                   # dot grid
            vis = QRectF(self.ox / z, self.oy / z, self.width() / z, self.height() / z).intersected(page)
            step = 25
            pts = QPolygonF([QPointF(x, y)
                             for x in range(int(vis.left() // step + 1) * step, int(vis.right()), step)
                             for y in range(int(vis.top() // step + 1) * step, int(vis.bottom()), step)])
            dot = QPen(QColor(t["grid"]), 2.2)
            dot.setCosmetic(True)
            g.setPen(dot)
            g.drawPoints(pts)
        objs = app.page_objs()
        app.rend.draw_page(g, objs)
        for _, o in app.live.values():
            if o.get("pg", "main") == app.pg:
                app.rend.draw(g, o)
        if app.cur:
            app.rend.draw(g, app.cur)
        accent = QPen(QColor(t["accent"]), 1.5, Qt.PenStyle.DashLine)
        accent.setCosmetic(True)
        g.setBrush(Qt.BrushStyle.NoBrush)
        for oid in app.dsels:
            o = app.view_obj(oid)
            if o and not o.get("h"):
                g.setPen(accent)
                g.drawRect(app.rend.bbox(o).adjusted(-4 / z, -4 / z, 4 / z, 4 / z))
        if app.poly:                                   # polygon being drawn
            solid = QPen(QColor(app.color), 2)
            solid.setCosmetic(True)
            g.setPen(solid)
            pts = [QPointF(app.poly[i], app.poly[i + 1]) for i in range(0, len(app.poly), 2)]
            g.drawPolyline(QPolygonF(pts + ([QPointF(*self.mouse)] if self.mouse else [])))
            g.setBrush(QColor(t["panel"]))
            for q in pts:
                g.drawEllipse(q, 3.5 / z, 3.5 / z)
        if app.tool == "rub" and self.mouse and app.can_edit():
            r = app.rub_r()
            pen = QPen(QColor(t["muted"]), 1, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            g.setPen(pen)
            g.drawEllipse(QPointF(*self.mouse), r, r)
        g.resetTransform()
        g.setPen(QPen(QColor(t["accent"]), 1.5))
        g.setBrush(QColor("#ffffff"))
        for hx, hy in app.handles():                   # resize handles, fixed size on screen
            g.drawRect(QRectF(hx * z - self.ox - 4, hy * z - self.oy - 4, 8, 8))
        g.setFont(Renderer.font(11, True))
        for o in objs:                                 # link badges (Figma-style prototype links)
            if o.get("ln") and not o.get("h"):
                b = app.rend.bbox(o)
                bx, by = b.right() * z - self.ox, b.top() * z - self.oy
                g.setPen(Qt.PenStyle.NoPen)
                g.setBrush(QColor(t["accent"]))
                g.drawEllipse(QPointF(bx, by), 9, 9)
                g.setPen(QColor("#ffffff"))
                g.drawText(QRectF(bx - 9, by - 9, 18, 18), int(Qt.AlignmentFlag.AlignCenter), "↗")
        for name, (x, y) in app.cursors.items():       # other people's cursors, fixed size on screen
            u = next((u for u in app.users if u["name"] == name), None)
            if not u:
                continue
            sx, sy, col = x * z - self.ox, y * z - self.oy, QColor(u["color"])
            g.setPen(QPen(QColor("#ffffff"), 1.5))
            g.setBrush(col)
            g.drawPolygon(QPolygonF([QPointF(sx, sy), QPointF(sx + 13, sy + 5), QPointF(sx + 6, sy + 7),
                                     QPointF(sx + 4, sy + 14)]))
            g.setFont(Renderer.font(11))
            tw = g.fontMetrics().horizontalAdvance(name) + 12
            g.setPen(Qt.PenStyle.NoPen)
            g.drawRoundedRect(QRectF(sx + 12, sy + 14, tw, 18), 9, 9)
            g.setPen(QColor("#ffffff"))
            g.drawText(QRectF(sx + 12, sy + 14, tw, 18), int(Qt.AlignmentFlag.AlignCenter), name)


# ---------------------------------------------------------------- application
class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.net, self.main, self.q = None, None, queue.Queue()
        self.dark = bool(load_json(PREFS).get("dark"))
        self.rend = Renderer()
        self.setWindowTitle("LANvas")
        avail = QGuiApplication.primaryScreen().availableGeometry()
        self.setMinimumSize(min(860, avail.width()), min(520, avail.height()))
        self.resize(min(1360, int(avail.width() * 0.92)), min(860, int(avail.height() * 0.9)))
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.apply_theme()
        self.login_screen()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(25)

    @property
    def t(self):
        return THEMES["dark" if self.dark else "light"]

    def apply_theme(self):
        QApplication.instance().setStyleSheet(QSS.format(**self.t))
        if self.main:
            self.board.update()
            self.refresh_ui()

    def toggle_dark(self):
        self.dark = not self.dark
        save_json(PREFS, {**load_json(PREFS), "dark": self.dark})
        self.apply_theme()

    def toast(self, text, kind="info"):
        """A rounded message that slides in at the bottom of the window and fades away."""
        host = self.stack.currentWidget()
        old, self._toast = getattr(self, "_toast", None), None
        if old is not None:
            try:
                old.deleteLater()
            except RuntimeError:            # already faded out and deleted by Qt
                pass
        w = self._toast = label(text, "toast")
        w.setParent(host)
        if kind == "error":
            w.setStyleSheet(f"background: {self.t['danger']};")
        elif kind == "ok":
            w.setStyleSheet(f"background: {self.t['ok']};")
        w.adjustSize()
        w.move((host.width() - w.width()) // 2, host.height() - w.height() - 46)
        fx = QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(fx)
        w.show()
        w.raise_()
        a = w.anim = QPropertyAnimation(fx, b"opacity", w)
        a.setDuration(3200)
        a.setKeyValueAt(0, 0.0)
        a.setKeyValueAt(0.08, 1.0)
        a.setKeyValueAt(0.85, 1.0)
        a.setKeyValueAt(1, 0.0)
        a.setEasingCurve(QEasingCurve.Type.InOutQuad)
        a.finished.connect(lambda: self._forget_toast(w))
        a.start()

    def _forget_toast(self, w):
        if getattr(self, "_toast", None) is w:
            self._toast = None
        w.deleteLater()

    # ---------- login ----------
    def login_screen(self):
        self.main = None
        page = QWidget()
        page.setObjectName("page")
        outer = QHBoxLayout(page)
        outer.addStretch()
        form = card()
        form.setFixedWidth(420)
        v = QVBoxLayout(form)
        v.setContentsMargins(28, 26, 28, 26)
        v.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(label("◆ LANvas", "title"))
        head.addStretch()
        head.addWidget(button("☾", tip="Dark mode (Ctrl+D)", cb=self.toggle_dark, tool=True))
        v.addLayout(head)
        v.addWidget(label("A shared whiteboard for your local network.", "muted"))
        seg = QFrame()
        seg.setObjectName("segbar")
        sh = QHBoxLayout(seg)
        sh.setContentsMargins(3, 3, 3, 3)
        self.seg = QButtonGroup(self)
        for i, name in enumerate(("Join a room", "Create a room")):
            b = button(name, "seg", checkable=True)
            self.seg.addButton(b, i)
            sh.addWidget(b)
        self.seg.button(0).setChecked(True)
        self.seg.idToggled.connect(lambda *_: self.mode_changed())
        v.addWidget(seg)
        self.v = {}

        def field(key, placeholder, default="", pw=False):
            e = self.v[key] = QLineEdit(default)
            e.setPlaceholderText(placeholder)
            if pw:
                e.setEchoMode(QLineEdit.EchoMode.Password)
            e.returnPressed.connect(self.connect)
            return e
        row = QHBoxLayout()
        row.addWidget(field("host", "Server address", "127.0.0.1"), 3)
        row.addWidget(field("port", "Port", str(PORT)), 1)
        v.addWidget(label("Server", "muted"))
        v.addLayout(row)
        v.addWidget(label("Your name", "muted"))
        v.addWidget(field("user", "e.g. Alex"))
        v.addWidget(label("Password (optional)", "muted"))
        v.addWidget(field("pw", "Room password", pw=True))
        self.join_box, self.create_box = QWidget(), QWidget()
        j = QGridLayout(self.join_box)
        j.setContentsMargins(0, 0, 0, 0)
        j.addWidget(label("Room code", "muted"), 0, 0)
        j.addWidget(label("Ask for", "muted"), 0, 1)
        j.addWidget(field("room", "ABC123"), 1, 0)
        self.want = QComboBox()
        self.want.addItems(["room default", "viewer", "editor", "admin"])
        j.addWidget(self.want, 1, 1)
        c = QVBoxLayout(self.create_box)
        c.setContentsMargins(0, 0, 0, 0)
        r = QHBoxLayout()
        r.addWidget(label("New joiners enter as", "muted"))
        self.role = QComboBox()
        self.role.addItems(["editor", "viewer"])
        r.addWidget(self.role)
        c.addLayout(r)
        self.private = Switch("Private — hidden from the room list", self)
        self.approval = Switch("Approval needed to enter", self)
        c.addWidget(self.private)
        c.addWidget(self.approval)
        c.addWidget(label("The server assigns the room code.", "muted"))
        v.addWidget(self.join_box)
        v.addWidget(self.create_box)
        self.msg = label("", "error", wrap=True)
        v.addWidget(self.msg)
        self.go = button("Join room", "primary", cb=self.connect)
        v.addWidget(self.go)
        outer.addWidget(form, 0, Qt.AlignmentFlag.AlignVCenter)
        lobby = card()
        lobby.setFixedSize(340, 470)
        lv = QVBoxLayout(lobby)
        lv.setContentsMargins(20, 20, 20, 20)
        lh = QHBoxLayout()
        lh.addWidget(label("Open rooms", "h2"))
        lh.addStretch()
        lh.addWidget(button("⟳", tip="Refresh", cb=self.refresh_rooms, tool=True))
        lv.addLayout(lh)
        lv.addWidget(label("Click to fill in the code, double-click to join.", "muted", wrap=True))
        self.rlist = QListWidget()
        self.rlist.itemClicked.connect(lambda it: self.pick_room(it, False))
        self.rlist.itemDoubleClicked.connect(lambda it: self.pick_room(it, True))
        lv.addWidget(self.rlist)
        outer.addSpacing(18)
        outer.addWidget(lobby, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addStretch()
        self.rooms = []
        self.login = scroller(page)
        self.login.setObjectName("page")
        self.stack.addWidget(self.login)
        self.stack.setCurrentWidget(self.login)
        self.mode_changed()
        self.refresh_rooms()

    def mode_changed(self):
        create = self.seg.checkedId() == 1
        self.join_box.setVisible(not create)
        self.create_box.setVisible(create)
        self.go.setText("Create room" if create else "Join room")

    def refresh_rooms(self):
        try:
            port = int(self.v["port"].text())
        except ValueError:
            return
        self.rlist.clear()
        self.rlist.addItem("Loading…")
        threading.Thread(target=fetch_rooms, args=(self.v["host"].text().strip(), port, self.q), daemon=True).start()

    def on_rooms(self, m):
        if self.main:
            return
        self.rlist.clear()
        self.rooms = m["rooms"] or []
        if m["rooms"] is None:
            self.rlist.addItem(m["text"][:70])
        elif not self.rooms:
            self.rlist.addItem("No open public rooms yet")
        for r in self.rooms:
            flags = ("   · password" if r["locked"] else "") + ("   · approval" if r.get("approval") else "")
            it = QListWidgetItem(f"{r['code']}{flags}\n{r['host']}'s room · {r['online']} online")
            it.setData(Qt.ItemDataRole.UserRole, r["code"])
            self.rlist.addItem(it)

    def pick_room(self, it, join):
        code = it.data(Qt.ItemDataRole.UserRole)
        if not code:
            return
        self.v["room"].setText(code)
        self.seg.button(0).setChecked(True)
        if join:
            if self.v["user"].text().strip():
                self.connect()
            else:
                self.msg.setText("Enter your name, then Join")
                self.v["user"].setFocus()

    def connect(self):
        v = {k: e.text().strip() for k, e in self.v.items()}
        pw, create = self.v["pw"].text(), self.seg.checkedId() == 1
        try:
            port = int(v["port"])
        except ValueError:
            return self.msg.setText("Port must be a number")
        room = "" if create else v["room"].upper()
        want = self.want.currentText()
        cfg = {"host": v["host"], "port": port, "user": v["user"], "room": room, "password": pw,
               "create": create, "role": self.role.currentText(), "private": self.private.isChecked(),
               "approval": self.approval.isChecked(), "want": None if want == "room default" else want,
               "token": load_json(TOKENS).get(f"{v['host']}:{port}/{room}/{v['user']}", "")}
        if create:
            salt = os.urandom(16).hex() if pw else ""
            cfg.update(salt=salt, key=derive_key(pw, salt) if pw else "")
        self.cfg, self.q = cfg, queue.Queue()      # fresh queue per connection: old threads can't leak in
        self.net = Net(cfg, self.q)
        self.net.start()
        self.msg.setText("")
        self.go.setEnabled(False)
        self.go.setText("Connecting…")

    # ---------- main window ----------
    def build_main(self):
        self.objs, self.seq, self.pending, self.live, self.cursors = {}, 0, {}, {}, {}
        self.users, self.checks, self.cur, self.online, self.resyncing = [], [], None, True, False
        self.tool, self.width, self.color, self.last_cur, self.gest = "pen", 3, self.me["color"], 0, None
        self.dsel, self.dsels, self.dids, self.reqwins, self.boot = None, [], [], {}, None
        self.poly, self.xf, self.preview, self.pg = None, None, {}, "main"
        self.pages, self.bans, self.ips = [{"id": "main", "name": "Page 1"}], {"users": [], "ips": []}, {}
        self.presenter, self.presenting, self.following, self.last_view = None, False, False, None
        self.rend = Renderer()
        page = self.main = QWidget()
        page.setObjectName("page")
        grid = QGridLayout(page)
        grid.setContentsMargins(10, 10, 10, 6)
        grid.setSpacing(10)
        self.btn = {}

        # top bar
        top = self.top = card()
        h = QHBoxLayout(top)
        h.setContentsMargins(14, 6, 10, 6)
        h.setSpacing(4)
        logo = label("◆ LANvas", "h2")
        logo.setStyleSheet(f"color: {self.t['accent']}; font-size: 15px; font-weight: 700;")
        h.addWidget(logo)
        h.addSpacing(10)
        self.chip = button("", "soft", "Copy room code (Ctrl+Shift+C)", self.copy_code)
        h.addWidget(self.chip)
        self.conn_lbl = label("", "muted")
        h.addWidget(self.conn_lbl)
        h.addStretch()
        for key, icon, tip, cb in (("Undo", "↶", "Undo (Ctrl+Z)", lambda: self.send({"t": "undo"})),
                                   ("Redo", "↷", "Redo (Ctrl+Y)", lambda: self.send({"t": "redo"})),
                                   ("ZoomOut", "−", "Zoom out (Ctrl+−)", lambda: self.zoom(-1)),
                                   ("Zoom", "100%", "Zoom presets", self.zoom_menu),
                                   ("ZoomIn", "+", "Zoom in (Ctrl+=)", lambda: self.zoom(1))):
            b = self.btn[key] = button(icon, tip=tip, cb=cb, tool=True)
            b.setStyleSheet("font-size: 16px;" if key != "Zoom" else "min-width: 52px;")
            h.addWidget(b)
            if key == "Redo":
                h.addSpacing(14)
        h.addStretch()
        self.avatars = QHBoxLayout()
        self.avatars.setSpacing(2)
        h.addLayout(self.avatars)
        h.addSpacing(8)
        self.btn["Export"] = button("⤓  Export", "soft", "Export as SVG / PNG / JPG / PDF (Ctrl+E)", self.export_menu)
        self.btn["Present"] = button("▶  Present", "primary", "Presentation mode (F5)", self.toggle_present)
        self.btn["More"] = button("⋯", tip="More (save, restore, roles, settings…)", cb=self.more_menu, tool=True)
        self.btn["More"].setStyleSheet("font-size: 18px;")
        for k in ("Export", "Present", "More"):
            h.addWidget(self.btn[k])
        grid.addWidget(top, 0, 0, 1, 3)

        # tool rail
        rail = self.rail = card()
        rail.setFixedWidth(62)
        outer = QVBoxLayout(rail)
        outer.setContentsMargins(0, 6, 0, 6)
        inner = QWidget()
        rv = QVBoxLayout(inner)
        rv.setContentsMargins(13, 2, 13, 2)
        rv.setSpacing(2)
        outer.addWidget(scroller(inner, Qt.ScrollBarPolicy.ScrollBarAlwaysOff))
        self.tools = QButtonGroup(self)
        self.tool_btn = {}
        for tid, icon, name, key in TOOLS:
            b = self.tool_btn[tid] = button(icon, "tool", f"{name}  ({key})", lambda tid=tid: self.set_tool(tid),
                                            checkable=True, tool=True)
            self.tools.addButton(b)
            rv.addWidget(b)
        self.tool_btn["pen"].setChecked(True)
        self.tool_btn["move"].setIconSize(QSize(20, 20))
        rv.addSpacing(8)
        sw = QGridLayout()
        sw.setSpacing(4)
        self.swatches = []
        for i, c in enumerate(PALETTE):
            b = button("", tip=f"Colour {i + 1}  ({i + 1})", cb=lambda c=c: self.set_color(c))
            b.setFixedSize(18, 18)
            b.colour = c
            self.swatches.append(b)
            sw.addWidget(b, i // 2, i % 2)
        rv.addLayout(sw)
        self.cbtn = button("", tip="Custom colour (C)", cb=self.pick_color)
        self.cbtn.setFixedSize(40, 22)
        rv.addWidget(self.cbtn, 0, Qt.AlignmentFlag.AlignHCenter)
        rv.addSpacing(6)
        self.wslider = QSlider(Qt.Orientation.Vertical)
        self.wslider.setRange(1, 20)
        self.wslider.setValue(self.width)
        self.wslider.setToolTip("Width  ([ and ])")
        self.wslider.valueChanged.connect(self.set_width)
        self.wslider.setFixedHeight(64)
        rv.addWidget(self.wslider, 0, Qt.AlignmentFlag.AlignHCenter)
        self.wlabel = label("3", "muted")
        self.wlabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rv.addWidget(self.wlabel)
        rv.addStretch()
        grid.addWidget(rail, 1, 0)

        self.board = Board(self)
        grid.addWidget(self.board, 1, 1)
        self.page_bar = QWidget()
        pl = QHBoxLayout(self.page_bar)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(4)
        grid.addWidget(self.page_bar, 2, 1)

        # right panel with tabs
        side = self.side = card()
        side.setFixedWidth(310)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(12, 12, 12, 12)
        self.tabs = QTabWidget()
        self.tabs.tabBar().setExpanding(True)
        self.tabs.tabBar().setDocumentMode(True)
        sv.addWidget(self.tabs)
        people = QWidget()
        pv = QVBoxLayout(people)
        pv.setContentsMargins(0, 8, 0, 0)
        self.plist = QListWidget()
        self.plist.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.plist.customContextMenuRequested.connect(self.user_menu)
        self.plist.itemDoubleClicked.connect(lambda it: self.dm(it.data(Qt.ItemDataRole.UserRole)))
        pv.addWidget(self.plist)
        pv.addWidget(label("Right-click someone for options · double-click to message", "muted", wrap=True))
        self.btn["Request role"] = button("Request a different role", "soft", "Ctrl+R", self.request_role)
        pv.addWidget(self.btn["Request role"])
        chat = QWidget()
        cv = QVBoxLayout(chat)
        cv.setContentsMargins(0, 8, 0, 0)
        self.chat = QTextBrowser()
        cv.addWidget(self.chat)
        self.to = QComboBox()
        self.to.addItem(EVERYONE)
        cv.addWidget(self.to)
        row = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Message…  ( / to focus)")
        self.entry.returnPressed.connect(self.send_chat)
        row.addWidget(self.entry)
        row.addWidget(button("➤", "primary", "Send (Enter)", self.send_chat))
        cv.addLayout(row)
        design = QWidget()
        self.build_design(design)
        for w, name in ((people, "People"), (chat, "Chat"), (design, "Design")):
            self.tabs.addTab(w, name)
        grid.addWidget(side, 1, 2)
        self.status = label("", "status")
        grid.addWidget(self.status, 3, 0, 1, 3)
        self.tabs.currentChanged.connect(lambda i: i == 2 and self.design_refresh())
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)
        self.stack.addWidget(page)
        self.stack.setCurrentWidget(page)
        self.bind_keys()
        self.build_pages()
        self.set_color(self.color)
        QTimer.singleShot(0, self.board.fit)

    def bind_keys(self):
        """Every action has a key. `?` lists them all."""
        tools = [(k, f"{n} tool", lambda t=t: self.set_tool(t)) for t, _, n, k in TOOLS]
        self.keymap = [
            ("Tools", tools + [(str(i + 1), f"Colour {i + 1}", lambda c=c: self.set_color(c))
                               for i, c in enumerate(PALETTE)] +
             [("C", "Custom colour", self.pick_color), ("[", "Thinner", lambda: self.set_width(self.width - 1)),
              ("]", "Thicker", lambda: self.set_width(self.width + 1))]),
            ("Edit", [("Ctrl+Z", "Undo", lambda: self.send({"t": "undo"})),
                      ("Ctrl+Y", "Redo", lambda: self.send({"t": "redo"})),
                      ("Ctrl+Shift+Z", "Redo", lambda: self.send({"t": "redo"})),
                      ("Del", "Delete selected layers", self.delete_selected),
                      ("H", "Hide / show selected layers", self.toggle_hidden),
                      ("Ctrl+L", "Lock / unlock selected layers", self.toggle_lock),
                      ("Ctrl+Alt+G", "Clip to layer below", self.toggle_clip),
                      ("Ctrl+G", "Merge selected layers", self.merge),
                      ("Shift+click", "Select several layers", None),
                      ("Enter", "Finish polygon", None),
                      ("Ctrl+S", "Save a version", self.save),
                      ("Ctrl+Shift+S", "Restore a version", self.restore),
                      ("Ctrl+Shift+Backspace", "Clear this page (admin)", self.clear)]),
            ("View", [("Ctrl+=", "Zoom in", lambda: self.zoom(1)), ("Ctrl+-", "Zoom out", lambda: self.zoom(-1)),
                      ("Ctrl+0", "Zoom to 100%", lambda: self.zoom(0)), ("Ctrl+1", "Fit board", self.board.fit),
                      ("Space+drag", "Pan (also middle-drag / wheel)", None),
                      ("F5", "Present / stop presenting", self.toggle_present),
                      ("Esc", "Cancel / leave presentation", self.escape),
                      ("Ctrl+D", "Dark mode", self.toggle_dark)]),
            ("Room", [("Ctrl+PgDown", "Next page", lambda: self.step_page(1)),
                      ("Ctrl+PgUp", "Previous page", lambda: self.step_page(-1)),
                      ("Ctrl+Shift+N", "New page", self.add_page),
                      ("Alt+1", "People tab", lambda: self.tabs.setCurrentIndex(0)),
                      ("Alt+2", "Chat tab", lambda: self.tabs.setCurrentIndex(1)),
                      ("Alt+3", "Design tab", lambda: self.tabs.setCurrentIndex(2)),
                      ("/", "Write a chat message", self.focus_chat),
                      ("Ctrl+E", "Export", self.export_menu),
                      ("Ctrl+Shift+C", "Copy room code", self.copy_code),
                      ("Ctrl+R", "Request a role", self.request_role),
                      ("Ctrl+B", "Bans (admin)", self.show_bans),
                      ("Ctrl+Q", "Leave the room", self.leave),
                      ("?", "Show keyboard shortcuts", self.show_keys),
                      ("F1", "Show keyboard shortcuts", self.show_keys)]),
        ]
        for _, items in self.keymap:
            for key, _, cb in items:
                if cb:
                    s = QShortcut(QKeySequence(key.replace("Del", "Delete")), self.main)
                    s.activated.connect(cb)

    def show_keys(self):
        d = QDialog(self)
        d.setWindowTitle("Keyboard shortcuts")
        g = QGridLayout(d)
        g.setContentsMargins(22, 18, 22, 18)
        g.setHorizontalSpacing(26)
        for col, (section, items) in enumerate(self.keymap):
            box = QGridLayout()
            box.setVerticalSpacing(5)
            box.addWidget(label(section, "h2"), 0, 0, 1, 2)
            for i, (key, desc, _) in enumerate(items, 1):
                k = label(key, "pill")
                k.ensurePolished()
                k.setMinimumWidth(max(36, k.sizeHint().width() + 4))
                k.setAlignment(Qt.AlignmentFlag.AlignCenter)
                box.addWidget(k, i, 0, Qt.AlignmentFlag.AlignRight)
                box.addWidget(label(desc), i, 1)
            box.setRowStretch(len(items) + 1, 1)
            g.addLayout(box, 0, col)
        g.addWidget(button("Close", "primary", cb=d.accept), 1, len(self.keymap) - 1,
                    alignment=Qt.AlignmentFlag.AlignRight)
        d.exec()

    # ---------- small UI helpers
    def me_role(self):
        return next((u["role"] for u in self.users if u["name"] == self.me["name"]), "viewer")

    def can_edit(self):
        return self.me_role() in ("host", "admin", "editor")

    def is_admin(self):
        return self.me_role() in ADMIN

    def mine(self, o):
        """Editors may only erase/change their own work; admins and the owner may change anyone's.
        (The server enforces this too; this just keeps the UI from trying.)"""
        return self.is_admin() or o.get("u") in (None, self.me["name"])

    def set_tool(self, t):
        if self.poly and t != "poly":
            self.finish_poly()
        self.tool = t
        self.tool_btn[t].setChecked(True)
        self.tool_btn["move"].setIcon(make_icon("move", self.t["accent"] if t == "move" else self.t["fg"]))
        self.board.update()

    def set_color(self, c):
        self.color = c
        for b in self.swatches:
            ring = f"2px solid {self.t['accent']}" if b.colour == c else f"1px solid {self.t['border']}"
            b.setStyleSheet(f"background: {b.colour}; border-radius: 9px; border: {ring}; padding: 0;")
        self.cbtn.setStyleSheet(f"background: {c}; border-radius: 8px; border: 1px solid {self.t['border']};")

    def pick_color(self):
        c = QColorDialog.getColor(QColor(self.color), self, "Colour")
        if c.isValid():
            self.set_color(c.name())

    def set_width(self, w):
        self.width = max(1, min(20, int(w)))
        self.wslider.blockSignals(True)
        self.wslider.setValue(self.width)
        self.wslider.blockSignals(False)
        self.wlabel.setText(str(self.width))
        self.board.update()

    def focus_chat(self):
        self.tabs.setCurrentIndex(1)
        self.entry.setFocus()

    def copy_code(self):
        QGuiApplication.clipboard().setText(self.me["room"])
        self.toast(f"Room code {self.me['room']} copied", "ok")

    def popup(self, entries, at=None):
        """Show a menu of (label, callback or None for disabled / '-' for separator)."""
        m = QMenu(self)
        for text, cb in entries:
            if text == "-":
                m.addSeparator()
                continue
            a = m.addAction(text)
            a.setEnabled(cb is not None)
            if cb:
                a.triggered.connect(lambda _=False, cb=cb: cb())
        m.exec(at or self.cursor().pos())

    def refresh_ui(self):
        """Redraw participants, avatars, status and permission-dependent buttons."""
        if not self.main:
            return
        self.plist.clear()
        while self.avatars.count():
            self.avatars.takeAt(0).widget().deleteLater()
        for u in self.users:
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, u["name"])
            row = QWidget()
            r = QHBoxLayout(row)
            r.setContentsMargins(4, 2, 4, 2)
            r.addWidget(Avatar(u["name"], u["color"], 30, u["online"]))
            name = label(u["name"] + ("  (you)" if u["name"] == self.me["name"] else ""))
            r.addWidget(name)
            r.addStretch()
            r.addWidget(label(ROLE_NAME.get(u["role"], u["role"]), "pill"))
            dot = label("●")
            dot.setStyleSheet(f"color: {self.t['ok'] if u['online'] else self.t['border']};")
            r.addWidget(dot)
            it.setSizeHint(row.sizeHint().expandedTo(QSize(0, 42)))
            self.plist.addItem(it)
            self.plist.setItemWidget(it, row)
        for u in [u for u in self.users if u["online"]][:6]:
            self.avatars.addWidget(Avatar(u["name"], u["color"], 28))
        admin, edit = self.is_admin(), self.can_edit()
        self.btn["Present"].setEnabled(bool(admin or (self.presenter and not self.following)))
        self.btn["Present"].setText("■  Stop" if self.presenting else
                                    "▶  Follow" if self.presenter and not self.following else "▶  Present")
        self.btn["Request role"].setEnabled(self.me_role() != "host")
        for t, b in self.tool_btn.items():
            b.setEnabled(edit or t == "move")
        self.tool_btn["move"].setIcon(make_icon("move", self.t["accent"] if self.tool == "move" else self.t["fg"]))
        cur = self.to.currentText()
        self.to.clear()
        names = [EVERYONE] + [u["name"] for u in self.users if u["name"] != self.me["name"]]
        self.to.addItems([n if n == EVERYONE else f"→ {n}" for n in names])
        self.to.setCurrentIndex(max(0, [self.to.itemText(i) for i in range(self.to.count())].index(cur)
                                    if cur in [self.to.itemText(i) for i in range(self.to.count())] else 0))
        self.chip.setText(f"#  {self.me['room']}")
        self.conn_lbl.setText("● Online" if self.online else "● Reconnecting…")
        self.conn_lbl.setStyleSheet(f"color: {self.t['ok'] if self.online else self.t['danger']};")
        self.update_status()
        self.setWindowTitle(f"LANvas — {self.me['room']}")
        self.set_color(self.color)
        self.design_later()

    def update_status(self):
        self.status.setText(f"{self.cfg['host']}:{self.cfg['port']}  ·  you are {ROLE_NAME[self.me_role()].lower()}"
                            f"  ·  {len(self.objs)} objects  ·  press ? for shortcuts")

    def leave(self):
        self.net.quit = True
        if self.net.sock:
            try:
                self.net.sock.close()
            except OSError:
                pass
        self.q = queue.Queue()
        for w in self.reqwins.values():
            w.close()
        self.showNormal()
        self.stack.removeWidget(self.main)
        self.main.deleteLater()
        self.main = None
        self.stack.removeWidget(self.login)
        self.login.deleteLater()
        self.login_screen()

    # ---------- network events ----------
    def send(self, msg):
        if self.net:
            self.net.send(msg)

    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                getattr(self, "on_" + m.get("t", ""), lambda m: None)(m)
        except queue.Empty:
            pass

    def on_fatal(self, m):
        if self.main:
            QMessageBox.critical(self, "LANvas", m["text"])
            self.leave()
        else:
            self.msg.setText(m["text"])
            self.go.setEnabled(True)
            self.mode_changed()

    def on_error(self, m):
        if m.get("fatal"):
            self.on_fatal(m)
        elif self.main:
            self.toast(m["text"], "error")

    def on_waiting(self, m):
        if not self.main:
            self.msg.setText(m["text"])

    def on_req(self, m):
        """Someone asks to enter (or for a new role): accept as asked, as another role, or refuse."""
        user, want = m["user"], m["role"]
        if user in self.reqwins:
            self.reqwins.pop(user).close()
        d = self.reqwins[user] = QDialog(self)
        d.setWindowTitle("Access request")
        v = QVBoxLayout(d)
        v.setContentsMargins(22, 18, 22, 18)
        verb = "wants to become" if m.get("inroom") else "wants to join as"
        v.addWidget(label(f"<b>{html_escape(user)}</b> {verb} <b>{want}</b>."))
        row = QHBoxLayout()

        def answer(t, role=None):
            self.send({"t": t, "user": user, "role": role})
            d.close()
        for r in [want] + [r for r in ("viewer", "editor", "admin") if r != want]:
            row.addWidget(button(f"Accept as {r}", "primary" if r == want else "soft", cb=lambda r=r: answer("grant", r)))
        row.addWidget(button("Refuse", "danger", cb=lambda: answer("refuse")))
        v.addLayout(row)
        d.show()
        d.raise_()
        self.toast(f"{user} {verb} {want}")

    def on_reqdone(self, m):
        d = self.reqwins.pop(m["user"], None)
        if d:
            d.close()

    def request_role(self):
        self.popup([(f"Ask to become {r}", lambda r=r: self.send({"t": "ask", "role": r}))
                    for r in ("viewer", "editor", "admin") if r != self.me_role()])

    def on_recover(self, m):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["ts"])) if m.get("ts") else "unknown time"
        r = QMessageBox.question(
            self, "Recover room", f"Room {m['room']} was closed unexpectedly.\n\nIts last autosave is from {when} "
                                  f"({m['objects']} objects).\n\nRestore that saved version?\n"
                                  f"(No = start with an empty board)")
        self.net.answer = r == QMessageBox.StandardButton.Yes
        self.net.answered.set()

    def on_welcome(self, m):
        first = self.main is None
        self.me = {**m["you"], "room": m["room"]}
        if first:
            self.build_main()
        self.cfg.update(create=False, room=m["room"], token=m["you"]["token"])
        tokens = load_json(TOKENS)
        tokens[f"{self.cfg['host']}:{self.cfg['port']}/{m['room']}/{self.cfg['user']}"] = m["you"]["token"]
        save_json(TOKENS, tokens)
        was_down, restarted = not self.online, self.boot is not None and m.get("boot") != self.boot
        self.online, self.boot = True, m.get("boot")
        self.on_snap(m)
        self.board.banner.hide()
        if restarted:
            when = time.strftime("%H:%M:%S", time.localtime(m["saved_at"])) if m.get("saved_at") else "?"
            self.notice(f"Server is back. The board was restored to its last saved version ({when}).")
            self.toast("Server is back — board restored to its last save", "ok")
        elif was_down:
            self.toast("Reconnected", "ok")

    def on_down(self, m):
        if self.online:
            self.notice(f"Server disconnected ({m['text']}). Reconnecting… your drawing will sync.")
        self.online = False
        self.board.banner.setText("⚠  Server disconnected — trying to reconnect…")
        self.board.banner.show()
        self.board.place_labels()
        self.refresh_ui()

    def notice(self, text):
        self.show_chat({"u": "", "ts": int(time.time()), "text": text})

    def on_snap(self, m):
        """Replace local state with the server's snapshot (join, reconnect, resync, restore)."""
        self.objs = {o["id"]: o for o in m["objects"]}
        self.seq, self.users, self.checks, self.resyncing = m["seq"], m["users"], m["checks"], False
        self.on_pages({"pages": m.get("pages") or [{"id": "main", "name": "Page 1"}]})
        self.live.clear()
        self.cursors.clear()
        self.chat.clear()
        for c in m["chat"]:
            self.show_chat(c)
        pr = m.get("present")
        self.on_present({"by": pr["by"], "view": pr["view"]} if pr else {"by": None})
        # After a reconnect: re-show and resend anything the server never confirmed (it ignores duplicates).
        for key, op in list(self.pending.items()):
            if self.has_effect(op):
                del self.pending[key]       # server already has the effect
            else:
                self.apply(op)
                self.send({"t": "op", "op": op})
        self.refresh_ui()
        self.board.update()

    def has_effect(self, op):
        k, objs = op["k"], self.objs
        if k == "add":
            return op["obj"]["id"] in objs
        if k == "del":
            return op["id"] not in objs
        if k == "set":
            o = objs.get(op["id"])
            return not o or all(o.get(a) == v for a, v in op["attrs"].items())
        return all(i not in objs for i in op["del"]) and all(o["id"] in objs for o in op["add"])

    def on_op(self, m):
        if m["seq"] <= self.seq:
            return                          # duplicate or already-applied: ignore
        if m["seq"] != self.seq + 1:        # gap: an update went missing, so fetch a fresh snapshot
            if not self.resyncing:
                self.resyncing = True
                self.send({"t": "resync"})
            return
        self.seq = m["seq"]
        op = m["op"]
        if op["k"] == "add":
            self.live.pop(op["obj"]["id"], None)
            if m["by"] == self.me["name"]:
                self.pending.pop(op["obj"]["id"], None)
        elif m["by"] == self.me["name"] and op["k"] in ("del", "set"):
            self.pending.pop(op["k"][0] + ":" + op["id"], None)
        elif m["by"] == self.me["name"] and op["k"] == "repl":
            self.pending.pop("r:" + op.get("rid", ""), None)
        self.apply(op)

    def on_ack(self, m):
        self.pending.pop(m["id"], None)

    def on_users(self, m):
        self.users = m["users"]
        self.refresh_ui()

    def on_checks(self, m):
        self.checks = m["checks"]

    def on_gone(self, m):
        self.cursors.pop(m["u"], None)
        for i, (u, _) in list(self.live.items()):
            if u == m["u"]:
                del self.live[i]
        self.board.update()

    def on_ping(self, m):
        self.send({"t": "pong"})

    def on_chat(self, m):
        self.show_chat(m["m"])
        if self.tabs.currentIndex() != 1 and m["m"]["u"] and m["m"]["u"] != self.me["name"]:
            self.toast(f"{m['m']['u']}: {m['m']['text'][:60]}")

    def on_live(self, m):
        """Another user's in-progress stroke or shape, drawn as a temporary preview."""
        o = m["o"]
        old = self.live.get(o["id"])
        if m["app"] and old:
            o = {**old[1], "p": old[1]["p"] + o["p"]}
        self.live[o["id"]] = (m["u"], o)
        self.board.update()

    def on_cur(self, m):
        if m.get("pg", "main") != self.pg:
            self.cursors.pop(m["u"], None)
        elif isinstance(m["x"], (int, float)) and isinstance(m["y"], (int, float)):
            self.cursors[m["u"]] = (m["x"], m["y"])
            self.board.update()

    # ---------- chat ----------
    def send_chat(self):
        text = self.entry.text().strip()
        if text:
            msg, to = {"t": "chat", "text": text}, self.to.currentText()
            if to != EVERYONE:
                msg["to"] = to.replace("→ ", "")
            self.send(msg)
            self.entry.clear()

    def dm(self, name):
        if name and name != self.me["name"]:
            self.tabs.setCurrentIndex(1)
            self.to.setCurrentText(f"→ {name}")
            self.entry.setFocus()

    def show_chat(self, c):
        t, stamp = self.t, time.strftime("%H:%M", time.localtime(c["ts"]))
        text = html_escape(c["text"])
        when = f'<span style="color:{t["muted"]}">{stamp}</span>'
        if c.get("to"):
            line = (f'{when} <span style="color:{t["dm"]}"><b>{html_escape(c["u"])} → '
                    f'{html_escape(c["to"])}</b> (private): {text}</span>')
        elif c["u"]:
            col = next((u["color"] for u in self.users if u["name"] == c["u"]), t["fg"])
            line = f'{when} <b style="color:{col}">{html_escape(c["u"])}</b>  {text}'
        else:
            line = f'<i style="color:{t["muted"]}">{stamp}  {text}</i>'
        self.chat.append(line)

    # ---------- board model ----------
    def apply(self, op):
        """Update the model with apply_op and repaint if the board really changed."""
        if apply_op(self.objs, op):
            self.board.update()
            self.design_later()
            self.update_status()

    def do(self, op):
        """Local action: show it immediately, remember it until the server confirms, send it."""
        self.apply(op)
        self.pending[self.pkey(op)] = op
        self.send({"t": "op", "op": op})

    @staticmethod
    def pkey(op):
        k = op["k"]
        return op["obj"]["id"] if k == "add" else "r:" + op["rid"] if k == "repl" else k[0] + ":" + op["id"]

    def zoom(self, d, anchor=None):
        if d == 0:
            return self.board.set_zoom(ZOOMS.index(1.0))
        self.board.set_zoom(self.board.zi + d, anchor)

    def zoom_menu(self):
        self.popup([(f"{round(z * 100)}%", lambda i=i: self.board.set_zoom(i)) for i, z in enumerate(ZOOMS)]
                   + [("-", None), ("Fit board   Ctrl+1", self.board.fit)])

    def zoom_changed(self):
        self.btn["Zoom"].setText(f"{round(self.board.z * 100)}%")

    # ---------- pages (multiple whiteboards in one room) ----------
    def page_objs(self):
        """The current page's layers in drawing order, with in-progress move/resize previews applied."""
        pv = self.preview
        return [pv.get(o["id"], o) for o in self.objs.values() if o.get("pg", "main") == self.pg]

    def view_obj(self, oid):
        return self.preview.get(oid) or self.objs.get(oid)

    def page_name(self, pid):
        return next((p["name"] for p in self.pages if p["id"] == pid), "?")

    def goto_page(self, pid, quiet=False):
        if pid not in {p["id"] for p in self.pages} or pid == self.pg:
            return
        self.pg, self.dsel, self.dsels, self.poly, self.cur = pid, None, [], None, None
        self.build_pages()
        self.design_later()
        self.board.update()
        if not quiet:
            self.toast(f"→ {self.page_name(pid)}")

    def step_page(self, d):
        ids = [p["id"] for p in self.pages]
        self.goto_page(ids[(ids.index(self.pg) + d) % len(ids)])

    def on_pages(self, m):
        self.pages = m["pages"]
        if self.pg not in {p["id"] for p in self.pages}:
            self.pg = self.pages[0]["id"]
        self.build_pages()
        self.design_later()
        self.board.update()

    def build_pages(self):
        lay = self.page_bar.layout()
        while lay.count():
            w = lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        for p in self.pages:
            b = button(p["name"], "seg", f"Open {p['name']} (Ctrl+PgUp / PgDn)", lambda pid=p["id"]: self.goto_page(pid),
                       checkable=True)
            b.setChecked(p["id"] == self.pg)
            b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pos, p=p, b=b: self.page_menu(p, b.mapToGlobal(pos)))
            lay.addWidget(b)
        lay.addWidget(button("+", tip="New page (Ctrl+Shift+N)", cb=self.add_page, tool=True))
        lay.addStretch()

    def add_page(self):
        if not self.can_edit():
            return self.toast("Viewers can't add pages", "error")
        name, ok = QInputDialog.getText(self, "New page", "Page name:", text=f"Page {len(self.pages) + 1}")
        if ok:
            self.send({"t": "page_add", "name": name})

    def page_menu(self, p, at):
        def rename():
            name, ok = QInputDialog.getText(self, "Rename page", "Page name:", text=p["name"])
            if ok:
                self.send({"t": "page_rename", "id": p["id"], "name": name})

        def delete():
            if QMessageBox.question(self, "Delete page", f"Delete “{p['name']}” and everything on it?") \
                    == QMessageBox.StandardButton.Yes:
                self.send({"t": "page_del", "id": p["id"]})
        self.popup([("Rename…", rename if self.can_edit() else None),
                    ("Delete page", delete if self.is_admin() and len(self.pages) > 1 else None)], at)

    # ---------- hit testing ----------
    def touches(self, o, x, y, r):
        if o["k"] == "group":
            return any(self.touches(i, x, y, r) for i in o["items"])
        if not self.rend.bbox(o).adjusted(-r, -r, r, r).contains(QPointF(x, y)):
            return False
        pts = outline(o)
        if pts is None or o["k"] in ("button",) or (o.get("f") and o["k"] != "line"):
            return True
        rr = r + o["w"] / 2
        return len(pts) == 2 and math.hypot(pts[0] - x, pts[1] - y) <= rr or any(
            seg_dist(x, y, pts[i], pts[i + 1], pts[i + 2], pts[i + 3]) <= rr for i in range(0, len(pts) - 2, 2))

    def near(self, x, y, r):
        """Ids of visible layers on this page within r board units of (x, y), bottom to top."""
        return [o["id"] for o in self.page_objs() if not o.get("h") and self.touches(o, x, y, r)]

    def top_at(self, x, y):
        hits = self.near(x, y, 5 / self.board.z)
        return hits[-1] if hits else None

    def editable(self, o):
        return o is not None and self.can_edit() and self.mine(o) and not o.get("lk")

    def resizable(self, o):
        return self.editable(o) and o["k"] not in ("text", "note")

    def handles(self):
        """Eight resize handles around the selected layer (corners first, then edges)."""
        o = self.view_obj(self.dsel) if self.dsel and len(self.dsels) == 1 else None
        if not o or o.get("h") or not self.resizable(o) or self.tool != "move":
            return []
        b = self.rend.gbox(o)
        l, t, r, bt, cx, cy = b.left(), b.top(), b.right(), b.bottom(), b.center().x(), b.center().y()
        return [(l, t), (r, t), (r, bt), (l, bt), (cx, t), (r, cy), (cx, bt), (l, cy)]

    def handle_at(self, x, y):
        tol = 7 / self.board.z
        return next((i for i, (hx, hy) in enumerate(self.handles()) if abs(hx - x) <= tol and abs(hy - y) <= tol), None)

    def hover_cursor(self, x, y):
        C = Qt.CursorShape
        if self.tool != "move":
            return C.CrossCursor
        h = self.handle_at(x, y)
        if h is not None:
            return [C.SizeFDiagCursor, C.SizeBDiagCursor] [h % 2] if h < 4 else [C.SizeVerCursor, C.SizeHorCursor][h % 2]
        oid = self.top_at(x, y)
        if oid:
            o = self.objs[oid]
            return C.PointingHandCursor if o.get("ln") else C.SizeAllCursor if self.editable(o) else C.ArrowCursor
        return C.ArrowCursor

    # ---------- move / resize ----------
    def boxed(self, o):
        """Images placed at natural size get an explicit box so they can be scaled."""
        if o["k"] == "image" and len(o["p"]) == 2:
            b = self.rend.bbox(o)
            return {**o, "p": [b.left(), b.top(), b.right(), b.bottom()]}
        return o

    def xform(self, o, f):
        """Map every point of o through f(x, y) -> (x, y); groups map all their parts."""
        if o["k"] == "group":
            items = [self.xform(i, f) for i in o["items"]]
            b = QRectF()
            for i in items:
                b = b.united(self.rend.gbox(i))
            return {**o, "items": items, "p": [round(b.left(), 1), round(b.top(), 1), round(b.right(), 1),
                                               round(b.bottom(), 1)]}
        p = o["p"]
        out = []
        for i in range(0, len(p), 2):
            nx, ny = f(p[i], p[i + 1])
            out += [round(nx, 1), round(ny, 1)]
        return {**o, "p": out}

    def refit(self, o, nb):
        """o scaled from its current geometric box into the box nb."""
        o = self.boxed(o)
        ob = self.rend.gbox(o)
        sx = nb.width() / ob.width() if ob.width() else 1
        sy = nb.height() / ob.height() if ob.height() else 1
        return self.xform(o, lambda x, y: (nb.left() + (x - ob.left()) * sx, nb.top() + (y - ob.top()) * sy))

    def commit_geometry(self, oid, new):
        attrs = {"p": new["p"]}
        if new["k"] == "group":
            attrs["items"] = new["items"]
        self.do({"k": "set", "id": oid, "attrs": attrs})

    def drag_xf(self, x, y):
        xf, (x0, y0) = self.xf, self.xf["start"]
        if abs(x - x0) + abs(y - y0) < 3 / self.board.z and not xf["moved"]:
            return
        xf["moved"] = True
        o = xf["orig"]
        if xf["mode"] == "move" and self.editable(o):
            self.preview[o["id"]] = self.xform(o, lambda a, b: (a + x - x0, b + y - y0))
        elif xf["mode"] == "resize":
            b, h = QRectF(xf["box"]), xf["h"]
            l, t, r, bt = b.left(), b.top(), b.right(), b.bottom()
            if h in (0, 3, 7):
                l = x
            if h in (1, 2, 5):
                r = x
            if h in (0, 1, 4):
                t = y
            if h in (2, 3, 6):
                bt = y
            nb = QRectF(QPointF(l, t), QPointF(r, bt)).normalized()
            if h < 4 and QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier and b.width() and b.height():
                k = max(nb.width() / b.width(), nb.height() / b.height())      # Shift keeps proportions
                nb.setWidth(b.width() * k)
                nb.setHeight(b.height() * k)
                if h in (0, 3):
                    nb.moveRight(b.right() if l < b.right() else nb.right())
                if h in (0, 1):
                    nb.moveBottom(b.bottom() if t < b.bottom() else nb.bottom())
            self.preview[o["id"]] = self.refit(o, nb)
        self.board.update()

    def end_xf(self):
        xf, self.xf = self.xf, None
        new = self.preview.pop(xf["orig"]["id"], None)
        if xf["moved"] and new:
            self.commit_geometry(xf["orig"]["id"], new)
        elif not xf["moved"] and xf["orig"].get("ln") and xf["mode"] != "resize":
            self.goto_page(xf["orig"]["ln"])            # clicking a linked object follows the link
        self.board.update()

    # ---------- drawing tools ----------
    def motion(self, x, y):
        if time.time() - self.last_cur > 0.05:
            self.last_cur = time.time()
            self.send({"t": "cur", "x": x, "y": y, "pg": self.pg})

    def rub_r(self):
        return 3 + 2 * self.width

    def rub(self, x, y):
        """Area eraser: cut away whatever lies under the circle, leaving the remaining pieces.
        Changes show at once; the whole gesture is sent as one 'repl' op on release (one undo step)."""
        r, g = self.rub_r(), self.gest
        for oid in self.near(x, y, r + 20):
            o = self.objs.get(oid)
            pieces = erase_pieces(o, x, y, r, MAX_PTS) if self.editable(o) else None
            if pieces is None:
                continue
            src = o.get("src", oid) if oid in g["new"] else oid
            new = [{"id": uuid.uuid4().hex[:12], "k": "stroke", "c": o["c"], "w": o["w"], "p": p, "s": "", "f": "",
                    "src": src, "pg": self.pg} for p in pieces]
            if oid in g["new"]:
                del g["new"][oid]           # a piece made earlier in this same gesture
            else:
                g["del"].append(oid)
            g["new"].update((n["id"], n) for n in new)
            self.apply({"k": "repl", "del": [oid], "add": new})
        self.board.update()

    def end_rub(self):
        g, self.gest = self.gest, None
        if g and (g["del"] or g["new"]):
            self.do({"k": "repl", "rid": uuid.uuid4().hex[:12], "del": g["del"],
                     "add": [o for i, o in g["new"].items() if i in self.objs]})

    def fill(self, x, y):
        target = next((o for o in reversed(self.page_objs()) if not o.get("h") and hit(o, x, y)), None)
        if not target:
            self.toast("Fill: click inside a shape, polygon, button, note or closed pen stroke", "error")
        elif not self.editable(target):
            self.toast("That layer is locked" if self.mine(target) else "You can only change your own work", "error")
        elif target.get("f", "") != self.color:
            self.do({"k": "set", "id": target["id"], "attrs": {"f": self.color}})

    def erase(self, x, y):
        for oid in self.near(x, y, 4 / self.board.z):
            if self.editable(self.objs[oid]):
                self.do({"k": "del", "id": oid})

    def new_obj(self, k, p, **kw):
        return {"id": uuid.uuid4().hex[:12], "k": k, "c": self.color, "w": self.width, "p": p, "s": "",
                "pg": self.pg, **kw}

    def finish_poly(self):
        pts, self.poly = self.poly, None
        if pts and len(pts) >= 6:
            self.do({"k": "add", "obj": self.new_obj("poly", pts)})
        self.board.update()

    def press(self, x, y):
        t = self.tool
        if t == "move":
            h = self.handle_at(x, y)
            if h is not None:
                o = self.boxed(self.objs[self.dsel])
                self.xf = {"mode": "resize", "h": h, "orig": o, "box": self.rend.gbox(o), "start": (x, y),
                           "moved": False}
                return
            oid = self.top_at(x, y)
            shift = QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
            if shift and oid:
                self.dsels = [i for i in self.dsels if i != oid] if oid in self.dsels else self.dsels + [oid]
                self.dsel = self.dsels[-1] if self.dsels else None
            else:
                self.dsel, self.dsels = oid, [oid] if oid else []
                if oid:
                    self.xf = {"mode": "move", "orig": self.objs[oid], "start": (x, y), "moved": False}
            self.design_later()
            return self.board.update()
        if not self.can_edit():
            return self.toast("You have view-only access", "error")
        if t == "eraser":
            return self.erase(x, y)
        if t == "fill":
            return self.fill(x, y)
        if t == "rub":
            self.gest = {"del": [], "new": {}}
            return self.rub(x, y)
        if t == "poly":
            if self.poly and len(self.poly) >= 6 and math.hypot(x - self.poly[0], y - self.poly[1]) <= 10 / self.board.z:
                return self.finish_poly()           # clicked the first point again: close it
            self.poly = (self.poly or []) + [x, y]
            return self.board.update()
        if t == "image":
            path, _ = QFileDialog.getOpenFileName(self, "Insert image", "",
                                                  "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;All files (*)")
            if path:
                try:
                    data = load_image(path)
                except ValueError as err:
                    return self.toast(str(err), "error")
                self.do({"k": "add", "obj": self.new_obj("image", [x, y], w=1, s=data)})
            return
        if t in ("text", "note"):
            s, ok = QInputDialog.getMultiLineText(self, "Sticky note" if t == "note" else "Text", "Text:")
            if ok and s.strip():
                self.do({"k": "add", "obj": self.new_obj(t, [x, y], s=s[:500])})
            return
        if t == "button":
            self.cur = self.new_obj("button", [x, y, x, y], c="#ffffff", f=self.color, s="Button")
        else:
            self.cur = self.new_obj("stroke" if t == "pen" else t, [x, y] if t == "pen" else [x, y, x, y])
        self.sent, self.last_live = 0, 0

    def drag(self, x, y):
        if self.xf:
            return self.drag_xf(x, y)
        if self.tool == "eraser" and self.can_edit():
            return self.erase(x, y)
        if self.gest is not None:
            return self.rub(x, y)
        o = self.cur
        if not o:
            return
        if o["k"] == "stroke":
            if abs(x - o["p"][-2]) + abs(y - o["p"][-1]) < 3:
                return
            o["p"] = o["p"] + [x, y]
            if len(o["p"]) >= MAX_PTS * 2:      # long stroke: commit this piece, continue in a new one
                self.release(x, y)
                self.cur = {**o, "id": uuid.uuid4().hex[:12], "p": [x, y]}
                self.sent = 0
                return
        else:
            o["p"] = o["p"][:2] + [x, y]
        self.board.update()
        if time.time() - self.last_live > 0.04:   # throttle preview traffic; strokes send only new points
            self.last_live = time.time()
            if o["k"] == "stroke":
                self.send({"t": "live", "o": {**o, "p": o["p"][self.sent:]}, "app": self.sent > 0})
                self.sent = len(o["p"])
            else:
                self.send({"t": "live", "o": o})

    def release(self, x, y):
        if self.xf:
            return self.end_xf()
        self.end_rub()
        o, self.cur = self.cur, None
        if not o:
            return
        if o["k"] == "button":
            self.board.update()
            return self.make_button(o)
        if o["k"] == "stroke" or o["p"][:2] != o["p"][2:]:      # ignore zero-size shapes
            self.do({"k": "add", "obj": o})
        self.board.update()

    def make_button(self, o):
        """A clickable button that takes people to another page (like a Figma prototype link)."""
        x0, y0, x1, y1 = o["p"]
        if abs(x1 - x0) < 20 or abs(y1 - y0) < 12:
            x1, y1 = x0 + 150, y0 + 46
        name, ok = QInputDialog.getText(self, "Link button", "Button label:", text="Next")
        if not ok:
            return
        choices = ["(no link)"] + [p["name"] for p in self.pages]
        target, ok = QInputDialog.getItem(self, "Link button", "Go to page:", choices, 0, False)
        ln = next((p["id"] for p in self.pages if p["name"] == target), "") if ok else ""
        self.do({"k": "add", "obj": {**o, "p": [x0, y0, x1, y1], "s": name[:60] or "Button", "ln": ln}})

    def board_menu(self, at, x, y):
        oid = self.top_at(x, y)
        if not oid:
            return self.popup([("New page", self.add_page), ("Fit board", self.board.fit),
                               ("Keyboard shortcuts", self.show_keys)], at)
        if oid not in self.dsels:
            self.dsel, self.dsels = oid, [oid]
        self.design_later()
        self.board.update()
        o = self.objs[oid]
        ok = self.editable(o)
        self.popup([("Open in Design tab", lambda: self.tabs.setCurrentIndex(2)),
                    (f"Go to “{self.page_name(o['ln'])}”" if o.get("ln") else "No link", 
                     (lambda: self.goto_page(o["ln"])) if o.get("ln") else None),
                    ("-", None),
                    ("Show" if o.get("h") else "Hide", self.toggle_hidden if self.mine(o) and self.can_edit() else None),
                    ("Unlock" if o.get("lk") else "Lock", self.toggle_lock if self.mine(o) and self.can_edit() else None),
                    ("Merge selected  Ctrl+G", self.merge if len(self.dsels) > 1 else None),
                    ("-", None), ("Delete", self.delete_selected if ok else None)], at)

    # ---------- room actions ----------
    def user_menu(self, pos):
        it = self.plist.itemAt(pos)
        name = it and it.data(Qt.ItemDataRole.UserRole)
        u = next((u for u in self.users if u["name"] == name), None)
        if not u or name == self.me["name"]:
            return
        entries = [("Message privately", lambda: self.dm(name))]
        me = self.me_role()
        if u["role"] != "host" and (me == "host" or (me == "admin" and u["role"] != "admin")):
            entries.append(("-", None))
            entries += [(f"Make {r}", lambda r=r: self.send({"t": "role", "user": name, "role": r}))
                        for r in ("viewer", "editor", "admin") if r != u["role"]]
            ip = self.ips.get(name, "?")
            entries += [("-", None), ("Kick from room", lambda: self.send({"t": "kick", "user": name})),
                        (f"Ban “{name}”", lambda: self.ban(name, False)),
                        (f"Ban IP address  {ip}", lambda: self.ban(name, True))]
        self.popup(entries, self.plist.mapToGlobal(pos))

    def ban(self, name, by_ip):
        what = f"the address {self.ips.get(name, '?')} (everyone on it)" if by_ip else f"“{name}”"
        if QMessageBox.question(self, "Ban", f"Ban {what} from this room?") == QMessageBox.StandardButton.Yes:
            self.send({"t": "ban", "user": name, "ip": by_ip})

    def on_bans(self, m):
        self.bans, self.ips = m["bans"], m["ips"]

    def show_bans(self):
        if not self.is_admin():
            return self.toast("Only the owner or an admin can manage bans", "error")
        entries = [(f"Unban user “{u}”", lambda u=u: self.send({"t": "unban", "user": u})) for u in self.bans["users"]]
        entries += [(f"Unban address {ip}", lambda ip=ip: self.send({"t": "unban", "ip": ip})) for ip in self.bans["ips"]]
        self.popup(entries or [("Nobody is banned", None)])

    def more_menu(self):
        admin, edit = self.is_admin(), self.can_edit()
        self.popup([("Save a version…        Ctrl+S", self.save if edit else None),
                    ("Restore a version…     Ctrl+Shift+S", self.restore if admin else None),
                    ("Clear this page", self.clear if admin else None),
                    ("Bans…", self.show_bans if admin else None), ("-", None),
                    ("Request a role…         Ctrl+R", self.request_role if self.me_role() != "host" else None),
                    ("Copy room code", self.copy_code),
                    ("Light mode" if self.dark else "Dark mode", self.toggle_dark),
                    ("Keyboard shortcuts     ?", self.show_keys), ("-", None),
                    ("Leave room                Ctrl+Q", self.leave)],
                   self.btn["More"].mapToGlobal(self.btn["More"].rect().bottomLeft()))

    def save(self):
        if not self.can_edit():
            return self.toast("Viewers can't save versions", "error")
        name, ok = QInputDialog.getText(self, "Save version", "Version name:")
        if ok:
            self.send({"t": "save", "name": name})
            self.toast("Version saved", "ok")

    def restore(self):
        if not self.is_admin():
            return self.toast("Only the owner or an admin can restore", "error")
        self.popup([(f"{c['name']}   ·   {time.strftime('%H:%M:%S', time.localtime(c['ts']))}",
                     lambda i=i: self.send({"t": "restore", "idx": i})) for i, c in enumerate(self.checks)]
                   or [("No saved versions yet", None)])

    def clear(self):
        if not self.is_admin():
            return self.toast("Only the owner or an admin can clear the board", "error")
        if QMessageBox.question(self, "Clear page", f"Remove everything from “{self.page_name(self.pg)}” for "
                                                    f"everyone?\n(You can undo it.)") == QMessageBox.StandardButton.Yes:
            self.send({"t": "clear", "pg": self.pg})

    # ---------- export ----------
    def export_menu(self):
        b = self.btn["Export"]
        self.popup([(f"Export as {f}", lambda f=f: self.export(f)) for f in ("PNG", "JPG", "PDF", "SVG")],
                   b.mapToGlobal(b.rect().bottomLeft()))

    def export(self, fmt):
        ext = "." + fmt.lower()
        path, _ = QFileDialog.getSaveFileName(self, f"Export {fmt}", f"lanvas-{self.me['room']}{ext}", f"{fmt} (*{ext})")
        if not path:
            return
        if not path.lower().endswith(ext):
            path += ext
        objs = [o for o in self.objs.values() if o.get("pg", "main") == self.pg]      # the current page
        if fmt == "SVG":
            ok = self.export_svg(path, objs)
        elif fmt == "PDF":
            w = QPdfWriter(path)
            w.setResolution(72)
            w.setPageSize(QPageSize(QSizeF(BOARD_W, BOARD_H), QPageSize.Unit.Point))
            w.setPageMargins(QMarginsF(0, 0, 0, 0))
            g = QPainter(w)
            g.setRenderHint(QPainter.RenderHint.Antialiasing)
            sx = w.width() / BOARD_W
            g.scale(sx, sx)
            self.rend.draw_page(g, objs)
            ok = g.end()
        else:
            img = QImage(BOARD_W, BOARD_H, QImage.Format.Format_RGB32)
            img.fill(QColor("#ffffff"))
            g = QPainter(img)
            g.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
            self.rend.draw_page(g, objs)
            g.end()
            ok = img.save(path, "JPG" if fmt == "JPG" else "PNG", 92)
        self.toast(f"Exported {Path(path).name}" if ok else "Export failed", "ok" if ok else "error")

    def svg(self, o):
        """One layer as SVG (clipping to the layer below is not exported to SVG)."""
        p, c, w, k, f = o["p"], o["c"], o["w"], o["k"], o.get("f") or "none"
        op = f' opacity="{o["a"] / 100}"' if o.get("a", 100) < 100 else ""
        st = f'stroke="{c}" stroke-width="{w}" fill="{f}" stroke-linecap="round" stroke-linejoin="round"{op}'
        pts = " ".join(f"{p[i]},{p[i + 1]}" for i in range(0, len(p), 2))
        b = self.rend.gbox(o)
        if k == "group":
            return f"<g{op}>" + "".join(self.svg(i) for i in o["items"]) + "</g>"
        if k in ("stroke", "line"):
            fill = f'<polygon points="{pts}" fill="{f}" stroke="none"/>' if k == "stroke" and f != "none" and closed(p) else ""
            line = st.replace(f'fill="{f}"', 'fill="none"')
            return fill + f'<polyline points="{pts if len(p) > 2 else pts + " " + pts}" {line}/>'
        if k == "poly":
            return f'<polygon points="{pts}" {st}/>'
        if k in ("rect", "oval"):
            if k == "rect":
                return f'<rect x="{b.x()}" y="{b.y()}" width="{b.width()}" height="{b.height()}" {st}/>'
            return (f'<ellipse cx="{b.center().x()}" cy="{b.center().y()}" rx="{b.width() / 2}" ry="{b.height() / 2}" '
                    f'{st}/>')
        if k == "image":
            b = self.rend.bbox(o)
            return (f'<image x="{b.x()}" y="{b.y()}" width="{b.width()}" height="{b.height()}"{op} '
                    f'preserveAspectRatio="none" href="data:image/png;base64,{o["s"]}"/>')
        if k == "button":
            return (f'<g{op}><rect x="{b.x()}" y="{b.y()}" width="{b.width()}" height="{b.height()}" rx="12" '
                    f'fill="{o.get("f") or "#6d5dfc"}"/><text x="{b.center().x()}" y="{b.center().y() + 5}" '
                    f'text-anchor="middle" font-weight="bold" font-size="14" fill="{c}">{escape(o["s"])}</text></g>')
        out = ""
        if k == "note":
            out = (f'<rect x="{p[0]}" y="{p[1]}" width="170" height="130" rx="6" fill="{o.get("f") or "#fff59d"}" '
                   f'stroke="{c}" stroke-width="2"/>')
        size = round((10 + 2 * w) * 4 / 3) if k == "text" else 13
        return out + (f'<text x="{p[0] + (0 if k == "text" else 8)}" y="{p[1] + size + (0 if k == "text" else 8)}" '
                      f'fill="{c if k == "text" else "#222"}" font-size="{size}"{op}>{escape(o["s"])}</text>')

    def export_svg(self, path, objs):
        out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{BOARD_W}" height="{BOARD_H}">',
               '<rect width="100%" height="100%" fill="white"/>']
        out += [self.svg(o) for o in objs if not o.get("h")]
        out.append("</svg>")
        try:
            Path(path).write_text("\n".join(out))
            return True
        except OSError:
            return False

    # ---------- design tab (FireAlpaca-style layers) ----------
    def build_design(self, w):
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(label("Layers", "h2"))
        head.addStretch()
        self.merge_btn = button("Merge", "soft", "Merge selected layers (Ctrl+G) — Shift/Ctrl-click to pick several",
                                self.merge)
        self.del_btn = button("Delete", "danger", "Delete selected layers (Del)", self.delete_selected)
        head.addWidget(self.merge_btn)
        head.addWidget(self.del_btn)
        v.addLayout(head)
        self.dlist = QListWidget()
        self.dlist.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.dlist.itemSelectionChanged.connect(self.design_pick)
        v.addWidget(self.dlist, 3)
        op = QHBoxLayout()
        op.addWidget(label("Opacity", "muted"))
        self.dop = QSlider(Qt.Orientation.Horizontal)
        self.dop.setRange(10, 100)
        self.dop.sliderReleased.connect(lambda: self.dset(a=self.dop.value()))
        self.dop.valueChanged.connect(lambda val: self.dop_lbl.setText(f"{val}%"))
        self.dop_lbl = label("100%", "muted")
        self.dop_lbl.setFixedWidth(38)
        op.addWidget(self.dop, 1)
        op.addWidget(self.dop_lbl)
        v.addLayout(op)
        v.addWidget(label("Properties", "h2"))
        self.props = QWidget()
        self.form = QFormLayout(self.props)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.form.setHorizontalSpacing(10)
        v.addWidget(scroller(self.props), 2)

    def layer_name(self, o):
        icons = {"stroke": "✎", "line": "╱", "rect": "▭", "oval": "◯", "text": "T", "note": "▤", "image": "▣",
                 "poly": "⬠", "button": "⊡", "group": "❖"}
        k = o["k"]
        name = f"Merged ({len(o['items'])})" if k == "group" else \
            {"poly": "Polygon", "rect": "Rectangle", "button": "Button"}.get(k, k.capitalize())
        if k in ("text", "note", "button"):
            name += f" “{o['s'][:14]}”"
        return f"{icons.get(k, '•')}  {name}"

    def layer_row(self, o):
        """One layer: visibility, lock and clip toggles, colour chip, name, link badge."""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 4, 0)
        h.setSpacing(1)
        can = self.can_edit() and self.mine(o)
        t = self.t
        for attr, icon, tip in (("h", "eye-off" if o.get("h") else "eye", "Show / hide (H)"),
                                ("lk", "lock" if o.get("lk") else "unlock", "Lock / unlock (Ctrl+L)"),
                                ("cl", "clip", "Clip to the layer below (Ctrl+Alt+G)")):
            on = bool(o.get(attr)) != (attr == "h")
            b = QToolButton()
            b.setIcon(make_icon(icon, t["accent"] if on else t["muted"], 16))
            b.setIconSize(QSize(16, 16))
            b.setFixedSize(26, 26)
            b.setStyleSheet("padding: 0;")
            b.setToolTip(tip)
            b.setEnabled(can)
            b.clicked.connect(lambda _=False, a=attr: self.set_attrs(o["id"], {a: not o.get(a, False)}))
            h.addWidget(b)
        h.addSpacing(4)
        if o.get("cl"):
            h.addWidget(label("↳", "muted"))
        chip = QLabel()
        chip.setFixedSize(12, 12)
        chip.setStyleSheet(f"background: {o.get('f') or o['c']}; border-radius: 3px; border: 1px solid {t['border']};")
        h.addWidget(chip)
        h.addSpacing(4)
        name = label(self.layer_name(o))
        if o.get("h"):
            name.setStyleSheet(f"color: {t['muted']};")
        h.addWidget(name, 1)
        if o.get("ln"):
            ln = QLabel()
            ln.setPixmap(make_icon("link", t["accent"], 14).pixmap(14, 14))
            ln.setToolTip(f"Links to {self.page_name(o['ln'])}")
            h.addWidget(ln)
        return row

    def design_later(self):
        if self.main and not getattr(self, "_dpending", False):
            self._dpending = True
            QTimer.singleShot(0, self.design_refresh)

    def design_refresh(self):
        self._dpending = False
        if not self.main:
            return
        self.dsels = [i for i in self.dsels if i in self.objs and self.objs[i].get("pg", "main") == self.pg]
        if self.dsel not in self.dsels:
            self.dsel = self.dsels[-1] if self.dsels else None
        self.board.update()
        if self.tabs.currentIndex() != 2:
            return                                      # rebuilt when the tab is opened
        objs = list(reversed(self.page_objs()))[:400]
        self.dids = [o["id"] for o in objs]
        self.dlist.blockSignals(True)
        self.dlist.clear()
        for o in objs:
            it = QListWidgetItem()
            it.setSizeHint(QSize(0, 32))
            self.dlist.addItem(it)
            self.dlist.setItemWidget(it, self.layer_row(o))
            it.setSelected(o["id"] in self.dsels)
        self.dlist.blockSignals(False)
        o = self.objs.get(self.dsel)
        self.merge_btn.setEnabled(len(self.dsels) > 1 and all(self.editable(self.objs[i]) for i in self.dsels))
        self.del_btn.setEnabled(bool(self.dsels) and all(self.editable(self.objs[i]) for i in self.dsels))
        self.dop.setEnabled(self.editable(o))
        self.dop.setValue(o.get("a", 100) if o else 100)
        self.build_props(o)

    def build_props(self, o):
        """Options for the selected layer, specific to its kind; a merged layer shows only base options."""
        f = self.form
        while f.rowCount():
            f.removeRow(0)
        if not o:
            return f.addRow(label("Select a layer, or click one with the Move tool (V). "
                                  "Shift/Ctrl-click layers to merge them.", "muted", wrap=True))
        if len(self.dsels) > 1:
            return f.addRow(label(f"{len(self.dsels)} layers selected — Merge (Ctrl+G) combines them into one.",
                                  "muted", wrap=True))
        k, ok, t = o["k"], self.editable(o), self.t
        who = html_escape(o.get("u") or self.me["name"])
        note = "" if ok else (" · locked" if o.get("lk") else " · read-only")
        f.addRow(label(("Merged layer · base options" if k == "group" else f"{self.layer_name(o)}") +
                       f"<br><span style='color:{t['muted']}'>by {who}{note}</span>", wrap=True))

        def spin(val, cb, lo=-99999, hi=99999):
            s = QSpinBox()
            s.setRange(lo, hi)
            s.setValue(round(val))
            s.setEnabled(ok)
            s.editingFinished.connect(lambda: s.value() != round(val) and cb(s.value()))
            return s

        def pair(a, b, la, lb):
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            for lab, s in ((la, a), (lb, b)):
                h.addWidget(label(lab, "muted"))
                h.addWidget(s, 1)
            return w

        def chip(colour, cb, tip):
            b = button("", tip=tip, cb=cb)
            b.setFixedSize(46, 24)
            b.setEnabled(ok)
            b.setStyleSheet(f"background: {colour or 'transparent'}; border-radius: 7px; border: 1px solid {t['border']};")
            return b
        if k != "image":
            f.addRow("Colour", chip(o["c"], self.design_color, "Stroke / text colour"))
        if k in ("stroke", "line", "rect", "oval", "poly", "group", "text"):
            f.addRow("Size" if k == "text" else "Width", spin(o["w"], lambda v: self.set_base(w=v), 1, 40))
        if k in ("rect", "oval", "poly", "note", "button") or (k == "stroke" and closed(o["p"])):
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(chip(o.get("f"), lambda: self.design_color(fill=True), "Fill colour"))
            nb = button("None", "soft", cb=lambda: self.dset(f=""))
            nb.setEnabled(ok and bool(o.get("f")))
            h.addWidget(nb)
            h.addStretch()
            f.addRow("Fill", w)
        if k in ("text", "note", "button"):
            e = QLineEdit(o["s"])
            e.setEnabled(ok)
            e.editingFinished.connect(lambda: e.text() != o["s"] and self.dset(s=e.text()[:500]))
            f.addRow("Text", e)
        if k == "line":
            pts = list(o["p"])
            setp = lambda i: (lambda v: self.dset(p=[*pts[:i], v, *pts[i + 1:]]))     # noqa: E731
            f.addRow("Start", pair(spin(pts[0], setp(0)), spin(pts[1], setp(1)), "X", "Y"))
            f.addRow("End", pair(spin(pts[2], setp(2)), spin(pts[3], setp(3)), "X", "Y"))
        elif k in ("text", "note"):
            b = self.rend.gbox(o)
            mv = lambda dx, dy: self.commit_geometry(o["id"], self.xform(o, lambda x, y: (x + dx, y + dy)))  # noqa
            f.addRow("Position", pair(spin(b.x(), lambda v: mv(v - b.x(), 0)), spin(b.y(), lambda v: mv(0, v - b.y())),
                                      "X", "Y"))
        elif k != "group":
            b = self.rend.gbox(self.boxed(o))

            def box(x=None, y=None, w=None, h=None):
                nb = QRectF(b.x() if x is None else x, b.y() if y is None else y,
                            max(1, b.width() if w is None else w), max(1, b.height() if h is None else h))
                self.commit_geometry(o["id"], self.refit(o, nb))
            f.addRow("Position", pair(spin(b.x(), lambda v: box(x=v)), spin(b.y(), lambda v: box(y=v)), "X", "Y"))
            f.addRow("Size ", pair(spin(b.width(), lambda v: box(w=v), 1), spin(b.height(), lambda v: box(h=v), 1),
                                   "W", "H"))
        link = QComboBox()
        link.addItem("No link", "")
        for page in self.pages:
            if page["id"] != self.pg:
                link.addItem(f"→ {page['name']}", page["id"])
        link.setCurrentIndex(max(0, link.findData(o.get("ln", ""))))
        link.setEnabled(ok)
        link.activated.connect(lambda i: self.dset(ln=link.itemData(i)))
        f.addRow("Link", link)

    def design_pick(self):
        rows = sorted(i.row() for i in self.dlist.selectedIndexes())
        # the list can be a moment behind the board (someone else just erased / merged a layer)
        self.dsels = [self.dids[r] for r in rows if r < len(self.dids) and self.dids[r] in self.objs]
        cur = self.dlist.currentRow()
        self.dsel = self.dids[cur] if 0 <= cur < len(self.dids) and self.dids[cur] in self.dsels else (
            self.dsels[0] if self.dsels else None)
        if self.dsel not in self.objs:
            self.dsel = self.dsels[0] if self.dsels else None
        self.dop.setValue(self.objs[self.dsel].get("a", 100) if self.dsel else 100)
        self.merge_btn.setEnabled(len(self.dsels) > 1 and all(self.editable(self.objs[i]) for i in self.dsels))
        self.del_btn.setEnabled(bool(self.dsels) and all(self.editable(self.objs[i]) for i in self.dsels))
        self.build_props(self.objs.get(self.dsel))
        self.board.update()

    def set_attrs(self, oid, attrs):
        o = self.objs.get(oid)
        if not o or not (self.can_edit() and self.mine(o)):
            return self.toast("You can only change your own work", "error")
        if o.get("lk") and not set(attrs) <= {"lk", "h"}:
            return self.toast("That layer is locked — unlock it first (Ctrl+L)", "error")
        if any(o.get(a, ATTRS[a]) != v for a, v in attrs.items()):
            self.do({"k": "set", "id": oid, "attrs": attrs})

    def dset(self, **attrs):
        if self.dsel:
            self.set_attrs(self.dsel, attrs)

    def set_base(self, **attrs):
        """Base options on a merged layer apply to all of its parts."""
        o = self.objs.get(self.dsel)
        if o and o["k"] == "group" and set(attrs) & {"c", "w"}:
            attrs["items"] = [{**i, **{a: v for a, v in attrs.items() if a in ("c", "w")}} for i in o["items"]]
        self.dset(**attrs)

    def toggle_hidden(self):
        for oid in self.dsels:
            if oid in self.objs:
                self.set_attrs(oid, {"h": not self.objs[oid].get("h", False)})

    def toggle_lock(self):
        for oid in self.dsels:
            if oid in self.objs:
                self.set_attrs(oid, {"lk": not self.objs[oid].get("lk", False)})

    def toggle_clip(self):
        for oid in self.dsels:
            if oid in self.objs:
                self.set_attrs(oid, {"cl": not self.objs[oid].get("cl", False)})

    def delete_selected(self):
        for oid in list(self.dsels):
            if self.editable(self.objs.get(oid)):
                self.do({"k": "del", "id": oid})
        self.dsel, self.dsels = None, []
        self.design_later()

    def merge(self):
        """Merge the selected layers into one (FireAlpaca 'merge down'); undo splits them again."""
        ids = [i for i in self.objs if i in self.dsels]          # keep drawing order
        objs = [self.objs[i] for i in ids]
        if len(objs) < 2:
            return self.toast("Select two or more layers to merge (Shift-click)", "error")
        if not all(self.editable(o) for o in objs):
            return self.toast("You can only merge your own, unlocked layers", "error")
        items = []
        for o in objs:
            items += o["items"] if o["k"] == "group" else [{k: v for k, v in o.items() if k not in ("pg", "src")}]
        b = QRectF()
        for o in objs:
            b = b.united(self.rend.gbox(o))
        g = {"id": uuid.uuid4().hex[:12], "k": "group", "c": objs[0]["c"], "w": objs[0]["w"], "s": "", "items": items,
             "p": [round(b.left(), 1), round(b.top(), 1), round(b.right(), 1), round(b.bottom(), 1)], "pg": self.pg}
        self.do({"k": "repl", "rid": uuid.uuid4().hex[:12], "del": ids, "add": [g]})
        self.dsel, self.dsels = g["id"], [g["id"]]
        self.design_later()

    def design_color(self, fill=False):
        o = self.objs.get(self.dsel)
        if o:
            c = QColorDialog.getColor(QColor((o.get("f") if fill else o["c"]) or self.color), self, "Colour")
            if c.isValid():
                self.dset(f=c.name()) if fill else self.set_base(c=c.name())

    # ---------- presentation mode ----------
    def toggle_present(self):
        if self.presenting:
            self.send({"t": "present", "on": False})
        elif self.presenter and not self.following:
            self.following = True
            self.layout(True)
            self.on_view({"v": self.last_view})
        elif self.is_admin():
            self.send({"t": "present", "on": True, "v": self.view_now()})
        else:
            self.toast("Only the owner or an admin can present", "error")
        self.refresh_ui()

    def escape(self):
        if self.poly:
            self.poly = None
        elif self.xf:
            self.preview.pop(self.xf["orig"]["id"], None)
            self.xf = None
        elif self.presenting:
            self.send({"t": "present", "on": False})
        elif self.following:
            self.following = False
            self.layout(False)
            self.refresh_ui()
        elif self.dsels:
            self.dsel, self.dsels = None, []
            self.design_later()
        self.board.update()

    def on_present(self, m):
        by = m.get("by")
        self.presenter = by
        self.presenting = by == self.me["name"]
        self.following = bool(by) and not self.presenting
        self.layout(bool(by))
        if self.presenting:
            QTimer.singleShot(150, self.present_tick)
        elif self.following and m.get("view"):
            self.on_view({"v": m["view"]})
        self.refresh_ui()

    def layout(self, full):
        """Presentation layout: full screen board; the presenter keeps the tool rail to annotate."""
        self.top.setVisible(not full)
        self.side.setVisible(not full)
        self.status.setVisible(not full)
        self.rail.setVisible(not full or self.presenting)
        if full and not self.isFullScreen():
            self.showFullScreen()
        elif not full and self.isFullScreen():
            self.showNormal()
        ov = self.board.overlay
        if full:
            who = "You are presenting" if self.presenting else f"Following {self.presenter}'s presentation"
            ov.setText(f"{who}   ·   Esc to {'stop' if self.presenting else 'leave'}")
            ov.show()
        else:
            ov.hide()
        QTimer.singleShot(50, self.board.place_labels)

    def view_now(self):
        b = self.board
        return {"z": b.zi, "cx": round((b.width() / 2 + b.ox) / b.z), "cy": round((b.height() / 2 + b.oy) / b.z),
                "pg": self.pg}

    def present_tick(self):
        """While presenting, send our viewport (zoom + centre) whenever it changes."""
        if not (self.main and self.presenting):
            return
        v = self.view_now()
        if v != self.last_view:
            self.last_view = v
            self.send({"t": "view", "v": v})
        QTimer.singleShot(150, self.present_tick)

    def on_view(self, m):
        v = m.get("v")
        if not v:
            return
        self.last_view = v
        if not self.following:
            return
        if v.get("pg"):
            self.goto_page(v["pg"], quiet=True)
        if isinstance(v.get("z"), int) and 0 <= v["z"] < len(ZOOMS):
            self.board.zi = v["z"]
            self.zoom_changed()
        if isinstance(v.get("cx"), (int, float)) and isinstance(v.get("cy"), (int, float)):
            self.board.center_on(v["cx"], v["cy"])

    def closeEvent(self, e):
        if self.net:
            self.net.quit = True
        super().closeEvent(e)


LOG = Path.home() / ".lanvas_client.log"


def survive(kind, value, tb):
    """PyQt6 aborts the whole program when a Python exception escapes a Qt callback. Instead, log it to
    ~/.lanvas_client.log, tell the user, and keep the session (and the connection) running."""
    import traceback
    text = "".join(traceback.format_exception(kind, value, tb))
    sys.__stderr__.write(text)
    try:
        with LOG.open("a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass
    win = QApplication.activeWindow()
    if isinstance(win, App):
        win.toast(f"Something went wrong ({kind.__name__}) — details in {LOG.name}", "error")


if __name__ == "__main__":
    sys.excepthook = survive
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qt = QApplication(sys.argv)
    qt.setApplicationName("LANvas")
    win = App()
    win.show()
    sys.exit(qt.exec())
