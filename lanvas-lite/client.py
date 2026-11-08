"""LANvas client: a Tk whiteboard that talks to server.py over one TCP socket.

Threads: a network thread (class Net) does all socket reads and pushes decoded messages into a queue;
the Tk main thread polls that queue every 25 ms and is the only code that touches widgets.
Run:  python client.py
"""
import base64
import io
import json
import math
import os
import queue
import socket
import threading
import time
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, simpledialog
from xml.sax.saxutils import escape

from common import PORT, apply_op, derive_key, erase_pieces, pack, proof, recv_msg

TOKENS = Path.home() / ".lanvas_tokens.json"     # session tokens, so a restarted client keeps its identity
PREFS = Path.home() / ".lanvas_prefs.json"       # UI preferences (dark mode)
TOOLS = [("Pen", "pen"), ("Eraser", "rub"), ("Erase object", "eraser"), ("Fill", "fill"), ("Line", "line"),
         ("Rect", "rect"), ("Oval", "oval"), ("Text", "text"), ("Note", "note"), ("Image", "image")]
MAX_PTS = 1400          # a stroke is split before it hits the server's per-object point limit
EVERYONE = "Everyone"
BOARD_W, BOARD_H = 1600, 1000
ZOOMS = [(1, 4), (1, 3), (1, 2), (2, 3), (1, 1), (3, 2), (2, 1), (3, 1), (4, 1)]   # rational: images scale exactly
MAX_IMG, IMG_SIDE = 700_000, 800        # base64 chars per image (fits one frame); longest side in px
THEMES = {
    "light": {"bg": "#f0f0f0", "fg": "#000000", "field": "#ffffff", "btn": "#e4e4e4", "canvas": "#ffffff",
              "muted": "#888888", "dm": "#7b1fa2", "sel": "#c0d8ff"},
    "dark": {"bg": "#26282b", "fg": "#e6e6e6", "field": "#33363a", "btn": "#3b3f44", "canvas": "#1d1f21",
             "muted": "#8a8f96", "dm": "#ce93d8", "sel": "#44608a"},
}


class Fatal(Exception):
    """Connection failed in a way retrying cannot fix (wrong password, bad room, kicked...)."""


def load_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def load_tokens():
    return load_json(TOKENS)


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
        q.put({"t": "rooms", "rooms": m.get("rooms", []), "where": f"{host}:{port}"})
    except (OSError, ValueError) as e:
        q.put({"t": "rooms", "rooms": None, "text": f"Cannot list rooms: {e}"})


def hit(o, x, y):
    """Is (x, y) inside a fillable object? (rect, oval, note, or a pen stroke whose ends nearly meet)"""
    k, p = o["k"], o["p"]
    if k == "rect":
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


def load_image(path):
    """Read an image file -> base64 PNG small enough to send. Tk reads PNG/GIF itself; Pillow, if
    installed, adds JPEG/BMP/WebP... and better downscaling."""
    try:
        from PIL import Image
    except ImportError:
        Image = None
    if Image:
        try:
            im = Image.open(path)
            im.load()
        except OSError as e:
            raise ValueError(f"Cannot read image: {e}")
        im = im.convert("RGBA")
        side = IMG_SIDE
        while True:
            im.thumbnail((side, side))
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)
            data = base64.b64encode(buf.getvalue()).decode()
            if len(data) <= MAX_IMG or side < 64:
                break
            side = int(side * 0.75)
    else:
        try:
            img = tk.PhotoImage(file=path)
        except tk.TclError:
            raise ValueError("Unsupported image. Use PNG or GIF (or `pip install pillow` for JPEG etc.)")
        n = max(1, math.ceil(max(img.width(), img.height()) / IMG_SIDE))
        while True:
            raw = (img.subsample(n) if n > 1 else img).data(format="png")
            data = base64.b64encode(raw).decode() if isinstance(raw, bytes) else str(raw)
            if len(data) <= MAX_IMG or n > 16:
                break
            n += 1
    if len(data) > MAX_IMG:
        raise ValueError("Image is too large even after shrinking")
    return data


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
            hello.update(create=True, role=c["role"], salt=c["salt"], key=c["key"], private=c["private"])
        hello["room"] = c["room"]
        s.sendall(pack(hello))
        m = recv_msg(s)
        if m["t"] == "challenge":
            s.sendall(pack({"t": "auth", "proof": proof(derive_key(c["password"], m["salt"]), m["nonce"])}))
            m = recv_msg(s)
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
            except (OSError, ValueError) as e:
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


class App:
    def __init__(self, root):
        self.root, self.net, self.main, self.q = root, None, None, queue.Queue()
        self.dark = bool(load_json(PREFS).get("dark"))
        root.title("LANvas")
        self.login_screen()
        root.after(25, self.poll)

    # ---------- login ----------
    def login_screen(self):
        self.main = None
        f = self.lf = tk.Frame(self.root, padx=24, pady=20)
        f.pack()
        tk.Label(f, text="LANvas", font=("Helvetica", 20, "bold")).grid(columnspan=2, pady=8)
        self.v = {}
        for i, (k, label, d) in enumerate([("host", "Server address", "127.0.0.1"), ("port", "Port", str(PORT)),
                                           ("user", "Your name", ""), ("room", "Room code", ""),
                                           ("pw", "Password (optional)", "")]):
            self.v[k] = tk.StringVar(value=d)
            tk.Label(f, text=label).grid(row=i + 1, column=0, sticky="e", pady=2)
            tk.Entry(f, textvariable=self.v[k], show="*" if k == "pw" else "", width=24).grid(row=i + 1, column=1)
        self.create, self.role, self.private = tk.BooleanVar(), tk.StringVar(value="editor"), tk.BooleanVar()
        tk.Checkbutton(f, text="Create a new room (I am the host)", variable=self.create).grid(columnspan=2)
        tk.Label(f, text="(leave Room code blank for a random one, or pick your own;\n"
                         "a closed room's code reopens it)", font=("Helvetica", 8)).grid(columnspan=2)
        r = tk.Frame(f)
        r.grid(columnspan=2)
        tk.Label(r, text="New joiners can:").pack(side="left")
        tk.OptionMenu(r, self.role, "editor", "viewer").pack(side="left")
        tk.Checkbutton(r, text="Private (hidden from room list)", variable=self.private).pack(side="left")
        self.msg = tk.Label(f, fg="red", wraplength=260)
        self.msg.grid(columnspan=2)
        tk.Button(f, text="Connect", command=self.connect).grid(columnspan=2, pady=6)
        lob = tk.Frame(f)
        lob.grid(row=0, column=2, rowspan=12, sticky="ns", padx=(18, 0))
        tk.Label(lob, text="Open rooms (double-click to join)").pack(anchor="w")
        self.rlist = tk.Listbox(lob, width=34, height=12, exportselection=False)
        self.rlist.pack(fill="both", expand=True)
        self.rlist.bind("<Double-Button-1>", self.pick_room)
        self.rooms = []
        b = tk.Frame(lob)
        b.pack(fill="x")
        tk.Button(b, text="Refresh", command=self.refresh_rooms).pack(side="left")
        tk.Button(b, text="Toggle dark mode", command=self.toggle_dark).pack(side="right")
        self.apply_theme()
        self.refresh_rooms()

    def refresh_rooms(self):
        try:
            port = int(self.v["port"].get())
        except ValueError:
            return
        self.rlist.delete(0, "end")
        self.rlist.insert("end", "Loading...")
        threading.Thread(target=fetch_rooms, args=(self.v["host"].get().strip(), port, self.q), daemon=True).start()

    def on_rooms(self, m):
        if self.main or not self.lf.winfo_exists():
            return
        self.rlist.delete(0, "end")
        self.rooms = m["rooms"] or []
        if m["rooms"] is None:
            self.rlist.insert("end", m["text"][:60])
        elif not self.rooms:
            self.rlist.insert("end", "No open public rooms")
        for r in self.rooms:
            self.rlist.insert("end", f"{r['code']}  host {r['host']}  ({r['online']} online)"
                                     + ("  [password]" if r["locked"] else ""))

    def pick_room(self, _):
        sel = self.rlist.curselection()
        if sel and sel[0] < len(self.rooms):
            self.v["room"].set(self.rooms[sel[0]]["code"])
            self.create.set(False)
            if self.v["user"].get().strip():
                self.connect()
            else:
                self.msg.config(text="Enter your name, then Connect", fg="red")

    def connect(self):
        v = {k: x.get().strip() for k, x in self.v.items()}
        pw = self.v["pw"].get()
        try:
            port = int(v["port"])
        except ValueError:
            self.msg.config(text="Port must be a number")
            return
        cfg = {"host": v["host"], "port": port, "user": v["user"], "room": v["room"].upper(), "password": pw,
               "create": self.create.get(), "role": self.role.get(), "private": self.private.get(),
               "token": load_tokens().get(f"{v['host']}:{port}/{v['room'].upper()}/{v['user']}", "")}
        if cfg["create"]:
            salt = os.urandom(16).hex() if pw else ""
            cfg.update(salt=salt, key=derive_key(pw, salt) if pw else "")
        self.cfg, self.q = cfg, queue.Queue()      # fresh queue per connection: old threads can't leak in
        self.net = Net(cfg, self.q)
        self.net.start()
        self.msg.config(text="Connecting...", fg="gray")

    # ---------- main window ----------
    def build_main(self):
        self.lf.destroy()
        m = self.main = tk.Frame(self.root)
        m.pack(fill="both", expand=True)
        self.objs, self.seq, self.pending, self.live, self.cursors = {}, 0, {}, {}, {}
        self.users, self.checks, self.cur, self.online, self.resyncing = [], [], None, True, False
        self.tool, self.width = tk.StringVar(value="pen"), tk.IntVar(value=3)
        self.color, self.last_cur, self.gest = self.me["color"], 0, None
        self.zi, self.photos, self.boot = ZOOMS.index((1, 1)), {}, None
        self.to = tk.StringVar(value=EVERYONE)
        bar = tk.Frame(m)
        bar.pack(fill="x")
        for label, t in TOOLS:
            tk.Radiobutton(bar, text=label, value=t, variable=self.tool, indicatoron=0, padx=6).pack(side="left")
        self.cbtn = tk.Button(bar, text="Color", bg=self.color, fg="white", command=self.pick_color)
        self.cbtn.pack(side="left", padx=4)
        tk.Scale(bar, from_=1, to=20, orient="horizontal", variable=self.width, length=80, showvalue=False).pack(side="left")
        self.btn = {}
        for label, cmd in [("Undo", lambda: self.send({"t": "undo"})), ("Redo", lambda: self.send({"t": "redo"})),
                           ("Clear", lambda: self.send({"t": "clear"})), ("Save", self.save),
                           ("Restore", self.restore), ("Export SVG", self.export_svg),
                           ("Dark mode", self.toggle_dark), ("Leave", self.leave),
                           ("−", lambda: self.zoom(-1)), ("100%", lambda: self.zoom(0)), ("+", lambda: self.zoom(1))]:
            self.btn[label] = tk.Button(bar, text=label, command=cmd)
            self.btn[label].pack(side="left", padx=1)
        self.banner = tk.Label(m, text="", bg="#c62828", fg="white", font=("Helvetica", 11, "bold"))
        self.status = tk.Label(m, anchor="w")
        self.status.pack(side="bottom", fill="x")
        side = tk.Frame(m, width=240)
        side.pack(side="right", fill="y")
        tk.Label(side, text="Participants").pack(anchor="w")
        self.plist = tk.Listbox(side, height=8, exportselection=False)
        self.plist.pack(fill="x")
        self.plist.bind("<Button-3>", self.user_menu)
        self.plist.bind("<Button-2>", self.user_menu)
        tk.Label(side, text="Chat").pack(anchor="w")
        self.chat = tk.Text(side, width=30, state="disabled", wrap="word")
        self.chat.pack(fill="both", expand=True)
        tor = tk.Frame(side)
        tor.pack(fill="x")
        tk.Label(tor, text="To:").pack(side="left")
        self.to_menu = tk.OptionMenu(tor, self.to, EVERYONE)
        self.to_menu.pack(side="left", fill="x", expand=True)
        self.entry = tk.Entry(side)
        self.entry.pack(fill="x")
        self.entry.bind("<Return>", self.send_chat)
        board = tk.Frame(m)
        board.pack(side="left", fill="both", expand=True)
        self.cv = tk.Canvas(board, scrollregion=(0, 0, BOARD_W, BOARD_H), cursor="crosshair", highlightthickness=0)
        xs = tk.Scrollbar(board, orient="horizontal", command=self.cv.xview)
        ys = tk.Scrollbar(board, orient="vertical", command=self.cv.yview)
        self.cv.config(xscrollcommand=xs.set, yscrollcommand=ys.set)
        self.cv.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        board.rowconfigure(0, weight=1)
        board.columnconfigure(0, weight=1)
        # zoom: Ctrl+wheel (at the pointer), Ctrl +/-/0; pan: wheel, Shift+wheel, middle-drag
        self.cv.bind("<Control-MouseWheel>", lambda e: self.zoom(1 if e.delta > 0 else -1, e))
        self.cv.bind("<Control-Button-4>", lambda e: self.zoom(1, e))
        self.cv.bind("<Control-Button-5>", lambda e: self.zoom(-1, e))
        self.cv.bind("<MouseWheel>", lambda e: self.cv.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.cv.bind("<Shift-MouseWheel>", lambda e: self.cv.xview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.cv.bind("<Button-4>", lambda e: self.cv.yview_scroll(-1, "units"))
        self.cv.bind("<Button-5>", lambda e: self.cv.yview_scroll(1, "units"))
        self.cv.bind("<ButtonPress-2>", lambda e: self.cv.scan_mark(e.x, e.y))
        self.cv.bind("<B2-Motion>", lambda e: self.cv.scan_dragto(e.x, e.y, gain=1))
        for key, d in (("<Control-equal>", 1), ("<Control-plus>", 1), ("<Control-minus>", -1), ("<Control-0>", 0)):
            self.root.bind(key, lambda e, d=d: None if isinstance(self.root.focus_get(), tk.Entry) else self.zoom(d))
        self.cv.bind("<ButtonPress-1>", self.press)
        self.cv.bind("<B1-Motion>", self.drag)
        self.cv.bind("<ButtonRelease-1>", self.release)
        self.cv.bind("<Motion>", self.motion)
        self.cv.bind("<Leave>", lambda e: self.cv.delete("ecur"))
        self.apply_theme()
        for key, msg in (("<Control-z>", "undo"), ("<Control-y>", "redo")):
            self.root.bind(key, lambda e, t=msg: None if isinstance(self.root.focus_get(), tk.Entry) else self.send({"t": t}))

    def me_role(self):
        return next((u["role"] for u in self.users if u["name"] == self.me["name"]), "viewer")

    def can_edit(self):
        return self.me_role() in ("host", "editor")

    def refresh_ui(self):
        """Redraw participant list, status bar and permission-dependent buttons."""
        self.plist.delete(0, "end")
        for i, u in enumerate(self.users):
            tag = " ★" if u["role"] == "host" else (" (view only)" if u["role"] == "viewer" else "")
            self.plist.insert("end", f"● {u['name']}{tag}" + ("" if u["online"] else " [offline]"))
            self.plist.itemconfig(i, foreground=u["color"] if u["online"] else "#aaa")
        host = self.me_role() == "host"
        for b in ("Clear", "Save", "Restore"):
            self.btn[b].config(state="normal" if host else "disabled")
        names = [EVERYONE] + [u["name"] for u in self.users if u["name"] != self.me["name"]]
        menu = self.to_menu["menu"]
        menu.delete(0, "end")
        for n in names:
            menu.add_command(label=n, command=lambda n=n: self.to.set(n))
        if self.to.get() not in names:
            self.to.set(EVERYONE)
        state = "online" if self.online else "OFFLINE - reconnecting (your drawing will sync)"
        self.status.config(text=f"Room {self.me['room']} | {self.cfg['host']}:{self.cfg['port']} | "
                                f"you: {self.me_role()} | {state}", fg=self.t["fg"] if self.online else "red")
        self.root.title(f"LANvas - room {self.me['room']}")

    # ---------- theme ----------
    @property
    def t(self):
        return THEMES["dark" if self.dark else "light"]

    def toggle_dark(self):
        self.dark = not self.dark
        save_json(PREFS, {**load_json(PREFS), "dark": self.dark})
        self.apply_theme()
        if self.main:
            self.refresh_ui()

    def apply_theme(self, w=None):
        """Recolour every widget for the current theme (Tk has no global stylesheet for classic widgets)."""
        t, w = self.t, w or self.root
        if w is getattr(self, "cbtn", None) or w is getattr(self, "banner", None):
            return                                   # the colour button shows the pen colour
        cls = w.winfo_class()
        if cls in ("Entry", "Text", "Listbox"):
            o = {"bg": t["field"], "fg": t["fg"], "insertbackground": t["fg"], "selectbackground": t["sel"],
                 "highlightbackground": t["bg"]}
        elif cls == "Canvas":
            o = {"bg": t["canvas"]}
        elif cls in ("Button", "Radiobutton", "Checkbutton", "Menubutton"):
            o = {"bg": t["btn"], "fg": t["fg"], "activebackground": t["sel"], "activeforeground": t["fg"],
                 "highlightbackground": t["bg"], "selectcolor": t["sel" if cls == "Radiobutton" else "field"]}
        elif cls == "Menu":
            o = {"bg": t["btn"], "fg": t["fg"], "activebackground": t["sel"], "activeforeground": t["fg"]}
        else:
            o = {"bg": t["bg"], "fg": t["fg"], "troughcolor": t["field"], "highlightbackground": t["bg"],
                 "activebackground": t["btn"]}
        if w is getattr(self, "msg", None) or w is getattr(self, "status", None):
            o.pop("fg")                               # keep red error/offline text
        for k, v in o.items():
            try:
                w.config(**{k: v})
            except tk.TclError:
                pass
        if cls == "Menubutton":
            self.apply_theme(w.nametowidget(w["menu"]))
        if cls == "Text":
            w.tag_config("sys", foreground=t["muted"])
            w.tag_config("dm", foreground=t["dm"])
        for c in w.winfo_children():
            self.apply_theme(c)

    def pick_color(self):
        c = colorchooser.askcolor(color=self.color)[1]
        if c:
            self.color = c
            self.cbtn.config(bg=c)

    def leave(self):
        self.net.quit = True
        if self.net.sock:
            self.net.sock.close()
        self.q = queue.Queue()
        self.main.destroy()
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
        finally:
            self.root.after(25, self.poll)

    def on_fatal(self, m):
        if self.main:
            messagebox.showerror("LANvas", m["text"])
            self.leave()
        else:
            self.msg.config(text=m["text"], fg="red")

    def on_error(self, m):
        if m.get("fatal"):
            self.on_fatal(m)
        else:
            self.status.config(text=m["text"], fg="red")

    def on_recover(self, m):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["ts"])) if m.get("ts") else "unknown time"
        use = messagebox.askyesno(
            "Recover room", f"Room {m['room']} was closed unexpectedly.\n\nIts last autosave is from {when} "
                            f"({m['objects']} objects).\n\nYes = restore that saved version\nNo = start with an "
                            f"empty board", parent=self.root)
        self.net.answer = use
        self.net.answered.set()

    def on_welcome(self, m):
        first = self.main is None
        self.me = {**m["you"], "room": m["room"]}
        if first:
            self.build_main()
        self.cfg.update(create=False, room=m["room"], token=m["you"]["token"])
        tokens = load_tokens()
        tokens[f"{self.cfg['host']}:{self.cfg['port']}/{m['room']}/{self.cfg['user']}"] = m["you"]["token"]
        try:
            TOKENS.write_text(json.dumps(tokens))
        except OSError:
            pass
        was_down, restarted = not self.online, self.boot is not None and m.get("boot") != self.boot
        self.online, self.boot = True, m.get("boot")
        self.on_snap(m)
        self.banner.pack_forget()
        if restarted:
            when = time.strftime("%H:%M:%S", time.localtime(m["saved_at"])) if m.get("saved_at") else "?"
            self.notice(f"Server is back. The board was restored to its last saved version ({when}).")
        elif was_down:
            self.notice("Reconnected to the server.")

    def on_down(self, m):
        if self.online:
            self.notice(f"Server disconnected ({m['text']}). Reconnecting... your drawing will sync.")
        self.online = False
        self.banner.config(text="⚠  Server disconnected — trying to reconnect...")
        self.banner.pack(side="top", fill="x", before=self.main.winfo_children()[0])
        self.refresh_ui()

    def notice(self, text):
        self.show_chat({"u": "", "ts": int(time.time()), "text": text})

    def on_snap(self, m):
        """Replace local state with the server's snapshot (join, reconnect, resync, restore)."""
        self.objs = {o["id"]: o for o in m["objects"]}
        self.seq, self.users, self.checks, self.resyncing = m["seq"], m["users"], m["checks"], False
        self.live.clear()
        self.cursors.clear()
        self.render()
        self.chat.config(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.config(state="disabled")
        for c in m["chat"]:
            self.show_chat(c)
        self.refresh_ui()
        # After a reconnect: re-show and resend anything the server never confirmed (it ignores duplicates).
        for key, op in list(self.pending.items()):
            if self.has_effect(op):
                del self.pending[key]       # server already has the effect
            else:
                self.apply(op)
                self.send({"t": "op", "op": op})

    def has_effect(self, op):
        k, objs = op["k"], self.objs
        if k == "add":
            return op["obj"]["id"] in objs
        if k == "del":
            return op["id"] not in objs
        if k == "fill":
            return op["id"] not in objs or objs[op["id"]].get("f", "") == op["f"]
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
            self.cv.delete("live:" + op["obj"]["id"])
            self.live.pop(op["obj"]["id"], None)
            if m["by"] == self.me["name"]:
                self.pending.pop(op["obj"]["id"], None)
        elif m["by"] == self.me["name"] and op["k"] in ("del", "fill"):
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
        self.cv.delete("cur:" + m["u"])
        for i, (u, _) in list(self.live.items()):
            if u == m["u"]:
                self.cv.delete("live:" + i)
                del self.live[i]

    def on_ping(self, m):
        self.send({"t": "pong"})

    def on_chat(self, m):
        self.show_chat(m["m"])

    def on_live(self, m):
        """Another user's in-progress stroke or shape, drawn as a temporary preview."""
        o = m["o"]
        old = self.live.get(o["id"])
        if m["app"] and old:
            o = {**old[1], "p": old[1]["p"] + o["p"]}
        self.live[o["id"]] = (m["u"], o)
        self.cv.delete("live:" + o["id"])
        self.draw(o, "live:" + o["id"])

    def on_cur(self, m):
        u = next((u for u in self.users if u["name"] == m["u"]), None)
        self.cv.delete("cur:" + m["u"])
        if u and isinstance(m["x"], (int, float)) and isinstance(m["y"], (int, float)):
            x, y, tag = m["x"] * self.z, m["y"] * self.z, "cur:" + m["u"]
            self.cv.create_polygon(x, y, x + 12, y + 4, x + 4, y + 12, fill=u["color"], tags=(tag, "cursor"))
            self.cv.create_text(x + 14, y + 10, text=m["u"], anchor="nw", fill=u["color"], tags=(tag, "cursor"))

    # ---------- chat ----------
    def send_chat(self, _):
        if self.entry.get().strip():
            msg = {"t": "chat", "text": self.entry.get()}
            if self.to.get() != EVERYONE:
                msg["to"] = self.to.get()
            self.send(msg)
            self.entry.delete(0, "end")

    def show_chat(self, c):
        self.chat.config(state="normal")
        stamp = time.strftime("%H:%M:%S", time.localtime(c["ts"]))
        if c.get("to"):
            self.chat.insert("end", f"[{stamp}] {c['u']} → {c['to']} (private): {c['text']}\n", "dm")
        elif c["u"]:
            self.chat.insert("end", f"[{stamp}] {c['u']}: {c['text']}\n")
        else:
            self.chat.insert("end", f"[{stamp}] {c['text']}\n", "sys")
        self.chat.config(state="disabled")
        self.chat.see("end")

    # ---------- board ----------
    def apply(self, op):
        """Update the model with apply_op, and the canvas only if the board really changed."""
        if not apply_op(self.objs, op):
            return
        if op["k"] == "add":
            self.draw(op["obj"])
        elif op["k"] == "del":
            self.cv.delete("o:" + op["id"])
        elif op["k"] == "fill":
            self.redraw(op["id"])
        elif op["k"] == "repl":
            for i in op["del"]:
                self.cv.delete("o:" + i)
            for o in op["add"]:
                self.redraw(o["id"])
        else:
            self.cv.delete("obj")

    def redraw(self, oid):
        """Redraw one object in place, keeping its position in the stacking order."""
        if oid not in self.objs:
            return
        below = None
        for i in self.cv.find_withtag("o:" + oid):
            below = self.cv.find_below(i) or below
            break
        self.cv.delete("o:" + oid)
        self.draw(self.objs[oid])
        if below:
            self.cv.tag_raise("o:" + oid, below)

    def do(self, op):
        """Local action: show it immediately, remember it until the server confirms, send it."""
        self.apply(op)
        self.pending[self.pkey(op)] = op
        self.send({"t": "op", "op": op})

    @staticmethod
    def pkey(op):
        k = op["k"]
        return op["obj"]["id"] if k == "add" else "r:" + op["rid"] if k == "repl" else k[0] + ":" + op["id"]

    # ---------- zoom ----------
    @property
    def z(self):
        a, b = ZOOMS[self.zi]
        return a / b

    def zoom(self, d, e=None):
        """d = +1 / -1 step, 0 = reset to 100%. Keeps the board point under the pointer (or centre) still."""
        zi = ZOOMS.index((1, 1)) if d == 0 else min(max(self.zi + d, 0), len(ZOOMS) - 1)
        if zi == self.zi:
            return
        sx, sy = (e.x, e.y) if e else (self.cv.winfo_width() / 2, self.cv.winfo_height() / 2)
        mx, my = self.cv.canvasx(sx) / self.z, self.cv.canvasy(sy) / self.z
        self.zi = zi
        self.photos.clear()
        self.render()
        z = self.z
        self.cv.xview_moveto(max(0, mx * z - sx) / (BOARD_W * z))
        self.cv.yview_moveto(max(0, my * z - sy) / (BOARD_H * z))
        self.btn["100%"].config(text=f"{round(z * 100)}%")

    def render(self):
        """Redraw everything at the current zoom (the model always stays in board coordinates)."""
        self.cv.delete("all")
        self.cv.config(scrollregion=(0, 0, BOARD_W * self.z, BOARD_H * self.z))
        for o in self.objs.values():
            self.draw(o)
        for i, (_, o) in self.live.items():
            self.draw(o, "live:" + i)

    def photo(self, o):
        """The image at the current zoom, cached (Tk only scales images by integer zoom/subsample)."""
        key = o["id"]
        if key not in self.photos:
            try:
                img = tk.PhotoImage(data=o["s"])
            except tk.TclError:
                img = None
            a, b = ZOOMS[self.zi]
            if img and (a, b) != (1, 1):
                img = (img.zoom(a) if a > 1 else img).subsample(b) if b > 1 else img.zoom(a)
            self.photos[key] = img
        return self.photos[key]

    def draw(self, o, tag=None):
        tags = ("obj", "o:" + o["id"]) if tag is None else (tag,)
        z, cv = self.z, self.cv
        k, c, w, s0 = o["k"], o["c"], max(1, o["w"] * z), o["w"]
        p = [v * z for v in o["p"]]
        f = o.get("f", "")
        if k == "stroke" and f and closed(p):
            cv.create_polygon(*p, fill=f, outline="", tags=tags)
        if k in ("stroke", "line"):
            cv.create_line(*(p if len(p) > 2 else p + p), fill=c, width=w, capstyle="round", joinstyle="round", tags=tags)
        elif k == "rect":
            cv.create_rectangle(*p, outline=c, width=w, fill=f, tags=tags)
        elif k == "oval":
            cv.create_oval(*p, outline=c, width=w, fill=f, tags=tags)
        elif k == "text":
            cv.create_text(*p, text=o["s"], fill=c, anchor="nw", width=500 * z,
                           font=("Helvetica", max(1, round((10 + 2 * s0) * z))), tags=tags)
        elif k == "note":
            x, y = p
            cv.create_rectangle(x, y, x + 170 * z, y + 130 * z, fill=f or "#fff59d", outline=c, width=max(1, 2 * z), tags=tags)
            cv.create_text(x + 8 * z, y + 8 * z, text=o["s"], anchor="nw", width=154 * z, fill="#222",
                           font=("Helvetica", max(1, round(10 * z))), tags=tags)
        elif k == "image":
            img = self.photo(o)
            if img:
                cv.create_image(*p, image=img, anchor="nw", tags=tags)
            else:
                cv.create_rectangle(*p, p[0] + 80, p[1] + 60, outline=c, dash=(3, 3), tags=tags)
        cv.tag_raise("cursor")

    def pos(self, e):
        """Pointer position in board coordinates (independent of zoom)."""
        return round(self.cv.canvasx(e.x) / self.z), round(self.cv.canvasy(e.y) / self.z)

    def near(self, x, y, r):
        """Ids of objects drawn within r board units of (x, y)."""
        z = self.z
        return {t[2:] for i in self.cv.find_overlapping((x - r) * z, (y - r) * z, (x + r) * z, (y + r) * z)
                for t in self.cv.gettags(i) if t.startswith("o:")}

    def motion(self, e):
        self.show_eraser(e)
        if time.time() - self.last_cur > 0.05:
            self.last_cur = time.time()
            x, y = self.pos(e)
            self.send({"t": "cur", "x": x, "y": y})

    def rub_r(self):
        return 3 + 2 * self.width.get()

    def show_eraser(self, e):
        self.cv.delete("ecur")
        if self.tool.get() == "rub":
            x, y, r = self.cv.canvasx(e.x), self.cv.canvasy(e.y), self.rub_r() * self.z
            self.cv.create_oval(x - r, y - r, x + r, y + r, outline=self.t["muted"], dash=(2, 2), tags=("ecur",))

    def rub(self, x, y):
        """Area eraser: cut away whatever lies under the circle, leaving the remaining pieces.
        Changes show at once; the whole gesture is sent as one 'repl' op on release (one undo step)."""
        r, g = self.rub_r(), self.gest
        for oid in self.near(x, y, r + 20):
            o = self.objs.get(oid)
            pieces = erase_pieces(o, x, y, r, MAX_PTS) if o else None
            if pieces is None:
                continue
            new = [{"id": uuid.uuid4().hex[:12], "k": "stroke", "c": o["c"], "w": o["w"], "p": p, "s": "", "f": ""}
                   for p in pieces]
            if oid in g["new"]:
                del g["new"][oid]           # a piece made earlier in this same gesture
            else:
                g["del"].append(oid)
            g["new"].update((n["id"], n) for n in new)
            self.apply({"k": "repl", "del": [oid], "add": new})

    def end_rub(self):
        g, self.gest = self.gest, None
        if g and (g["del"] or g["new"]):
            self.do({"k": "repl", "rid": uuid.uuid4().hex[:12], "del": g["del"],
                     "add": [o for i, o in g["new"].items() if i in self.objs]})

    def fill(self, x, y):
        target = next((o for o in reversed(list(self.objs.values())) if hit(o, x, y)), None)
        if not target:
            self.status.config(text="Fill: click inside a rectangle, oval, note or closed pen shape", fg="red")
        elif target.get("f", "") != self.color:
            self.do({"k": "fill", "id": target["id"], "f": self.color})

    def erase(self, x, y):
        for oid in self.near(x, y, 4 / self.z):
            self.do({"k": "del", "id": oid})

    def press(self, e):
        if not self.can_edit():
            self.status.config(text="You have view-only access", fg="red")
            return
        x, y = self.pos(e)
        t = self.tool.get()
        if t == "eraser":
            return self.erase(x, y)
        if t == "fill":
            return self.fill(x, y)
        if t == "rub":
            self.gest = {"del": [], "new": {}}
            return self.rub(x, y)
        if t == "image":
            path = filedialog.askopenfilename(title="Insert image", filetypes=[
                ("Images", "*.png *.gif *.jpg *.jpeg *.bmp *.webp"), ("All files", "*")])
            if path:
                try:
                    data = load_image(path)
                except ValueError as err:
                    return messagebox.showerror("Insert image", str(err), parent=self.root)
                self.do({"k": "add", "obj": {"id": uuid.uuid4().hex[:12], "k": "image", "c": self.color,
                                             "w": 1, "p": [x, y], "s": data}})
            return
        if t in ("text", "note"):
            s = simpledialog.askstring("Text", "Enter text:", parent=self.root)
            if s:
                self.do({"k": "add", "obj": {"id": uuid.uuid4().hex[:12], "k": t, "c": self.color,
                                             "w": self.width.get(), "p": [x, y], "s": s[:500]}})
            return
        self.cur = {"id": uuid.uuid4().hex[:12], "k": "stroke" if t == "pen" else t, "c": self.color,
                    "w": self.width.get(), "p": [x, y] if t == "pen" else [x, y, x, y], "s": ""}
        self.sent, self.last_live = 0, 0

    def drag(self, e):
        x, y = self.pos(e)
        self.show_eraser(e)
        if self.tool.get() == "eraser" and self.can_edit():
            return self.erase(x, y)
        if self.gest is not None:
            return self.rub(x, y)
        o = self.cur
        if not o:
            return
        if o["k"] == "stroke":
            if abs(x - o["p"][-2]) + abs(y - o["p"][-1]) < 3:
                return
            o["p"] += [x, y]
            if len(o["p"]) >= MAX_PTS * 2:      # long stroke: commit this piece, continue in a new one
                self.release(e)
                self.cur = {**o, "id": uuid.uuid4().hex[:12], "p": [x, y]}
                self.sent = 0
                return
        else:
            o["p"][2:] = [x, y]
        self.cv.delete("mine")
        self.draw(o, "mine")
        if time.time() - self.last_live > 0.04:   # throttle preview traffic; strokes send only new points
            self.last_live = time.time()
            if o["k"] == "stroke":
                self.send({"t": "live", "o": {**o, "p": o["p"][self.sent:]}, "app": self.sent > 0})
                self.sent = len(o["p"])
            else:
                self.send({"t": "live", "o": o})

    def release(self, e):
        self.end_rub()
        o, self.cur = self.cur, None
        if not o:
            return
        self.cv.delete("mine")
        if o["k"] == "stroke" or o["p"][:2] != o["p"][2:]:      # ignore zero-size shapes
            self.do({"k": "add", "obj": o})

    # ---------- host actions ----------
    def user_menu(self, e):
        i = self.plist.nearest(e.y)
        if i < 0 or i >= len(self.users) or self.users[i]["name"] == self.me["name"]:
            return
        u = self.users[i]
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Message privately", command=lambda: (self.to.set(u["name"]), self.entry.focus_set()))
        if self.me_role() == "host" and u["role"] != "host":
            other = "viewer" if u["role"] == "editor" else "editor"
            menu.add_command(label=f"Make {other}", command=lambda: self.send({"t": "role", "user": u["name"], "role": other}))
            menu.add_command(label="Kick", command=lambda: self.send({"t": "kick", "user": u["name"]}))
        self.apply_theme(menu)
        menu.tk_popup(e.x_root, e.y_root)

    def save(self):
        name = simpledialog.askstring("Save version", "Version name:", parent=self.root)
        if name is not None:
            self.send({"t": "save", "name": name})

    def restore(self):
        menu = tk.Menu(self.root, tearoff=0)
        for i, c in enumerate(self.checks):
            menu.add_command(label=f"{c['name']}  ({time.strftime('%H:%M:%S', time.localtime(c['ts']))})",
                             command=lambda i=i: self.send({"t": "restore", "idx": i}))
        if not self.checks:
            menu.add_command(label="No saved versions", state="disabled")
        menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def export_svg(self):
        path = filedialog.asksaveasfilename(defaultextension=".svg", filetypes=[("SVG", "*.svg")])
        if not path:
            return
        out = ['<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1000">',
               '<rect width="100%" height="100%" fill="white"/>']
        for o in self.objs.values():
            p, c, w, k, f = o["p"], o["c"], o["w"], o["k"], o.get("f") or "none"
            st = f'stroke="{c}" stroke-width="{w}" fill="{f}" stroke-linecap="round" stroke-linejoin="round"'
            if k == "stroke" and f != "none" and closed(p):
                pts = " ".join(f"{p[i]},{p[i + 1]}" for i in range(0, len(p), 2))
                out.append(f'<polygon points="{pts}" fill="{f}" stroke="none"/>')
            if k in ("stroke", "line"):
                st = st.replace(f'fill="{f}"', 'fill="none"')
                pts = " ".join(f"{p[i]},{p[i + 1]}" for i in range(0, len(p), 2))
                out.append(f'<polyline points="{pts if len(p) > 2 else pts + " " + pts}" {st}/>')
            elif k == "rect":
                out.append(f'<rect x="{min(p[0], p[2])}" y="{min(p[1], p[3])}" width="{abs(p[2] - p[0])}" height="{abs(p[3] - p[1])}" {st}/>')
            elif k == "oval":
                out.append(f'<ellipse cx="{(p[0] + p[2]) / 2}" cy="{(p[1] + p[3]) / 2}" rx="{abs(p[2] - p[0]) / 2}" ry="{abs(p[3] - p[1]) / 2}" {st}/>')
            elif k == "image":
                img = tk.PhotoImage(data=o["s"]) if o["s"] else None
                if img:
                    out.append(f'<image x="{p[0]}" y="{p[1]}" width="{img.width()}" height="{img.height()}" '
                               f'href="data:image/png;base64,{o["s"]}"/>')
            else:
                if k == "note":
                    out.append(f'<rect x="{p[0]}" y="{p[1]}" width="170" height="130" fill="{o.get('f') or '#fff59d'}" stroke="{c}" stroke-width="2"/>')
                out.append(f'<text x="{p[0] + 8}" y="{p[1] + 22}" fill="{c if k == "text" else "#222"}" font-size="{10 + 2 * w if k == "text" else 13}">{escape(o["s"])}</text>')
        out.append("</svg>")
        Path(path).write_text("\n".join(out))


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
