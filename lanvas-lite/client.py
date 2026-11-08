"""LANvas client: a Tk whiteboard that talks to server.py over one TCP socket.

Threads: a network thread (class Net) does all socket reads and pushes decoded messages into a queue;
the Tk main thread polls that queue every 25 ms and is the only code that touches widgets.
Run:  python client.py
"""
import json
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

from common import PORT, apply_op, derive_key, pack, proof, recv_msg

TOKENS = Path.home() / ".lanvas_tokens.json"     # session tokens, so a restarted client keeps its identity
TOOLS = [("Pen", "pen"), ("Eraser", "eraser"), ("Line", "line"), ("Rect", "rect"),
         ("Oval", "oval"), ("Text", "text"), ("Note", "note")]
MAX_PTS = 1400          # a stroke is split before it hits the server's per-object point limit


class Fatal(Exception):
    """Connection failed in a way retrying cannot fix (wrong password, bad room, kicked...)."""


def load_tokens():
    try:
        return json.loads(TOKENS.read_text())
    except (OSError, ValueError):
        return {}


class Net(threading.Thread):
    """Connects, logs in, then pumps server messages into `q`. After any drop it reconnects
    automatically with backoff, re-authenticating with the saved session token."""

    def __init__(self, cfg, q):
        super().__init__(daemon=True)
        self.cfg, self.q, self.sock, self.quit = cfg, q, None, False
        self.lock, self.was_up = threading.Lock(), False

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
            hello.update(create=True, role=c["role"], salt=c["salt"], key=c["key"])
        else:
            hello["room"] = c["room"]
        s.sendall(pack(hello))
        m = recv_msg(s)
        if m["t"] == "challenge":
            s.sendall(pack({"t": "auth", "proof": proof(derive_key(c["password"], m["salt"]), m["nonce"])}))
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
                    self.q.put(recv_msg(s))
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
        self.create, self.role = tk.BooleanVar(), tk.StringVar(value="editor")
        tk.Checkbutton(f, text="Create a new room (I am the host)", variable=self.create).grid(columnspan=2)
        r = tk.Frame(f)
        r.grid(columnspan=2)
        tk.Label(r, text="New joiners can:").pack(side="left")
        tk.OptionMenu(r, self.role, "editor", "viewer").pack(side="left")
        self.msg = tk.Label(f, fg="red", wraplength=260)
        self.msg.grid(columnspan=2)
        tk.Button(f, text="Connect", command=self.connect).grid(columnspan=2, pady=6)

    def connect(self):
        v = {k: x.get().strip() for k, x in self.v.items()}
        pw = self.v["pw"].get()
        try:
            port = int(v["port"])
        except ValueError:
            self.msg.config(text="Port must be a number")
            return
        cfg = {"host": v["host"], "port": port, "user": v["user"], "room": v["room"].upper(), "password": pw,
               "create": self.create.get(), "role": self.role.get(),
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
        self.color, self.last_cur = self.me["color"], 0
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
                           ("Restore", self.restore), ("Export SVG", self.export_svg), ("Leave", self.leave)]:
            self.btn[label] = tk.Button(bar, text=label, command=cmd)
            self.btn[label].pack(side="left", padx=1)
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
        self.chat.tag_config("sys", foreground="gray")
        self.entry = tk.Entry(side)
        self.entry.pack(fill="x")
        self.entry.bind("<Return>", self.send_chat)
        self.cv = tk.Canvas(m, bg="white", scrollregion=(0, 0, 1600, 1000), cursor="crosshair")
        self.cv.pack(side="left", fill="both", expand=True)
        self.cv.bind("<ButtonPress-1>", self.press)
        self.cv.bind("<B1-Motion>", self.drag)
        self.cv.bind("<ButtonRelease-1>", self.release)
        self.cv.bind("<Motion>", self.motion)
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
        state = "online" if self.online else "OFFLINE - reconnecting (your drawing will sync)"
        self.status.config(text=f"Room {self.me['room']} | {self.cfg['host']}:{self.cfg['port']} | "
                                f"you: {self.me_role()} | {state}", fg="black" if self.online else "red")
        self.root.title(f"LANvas - room {self.me['room']}")

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
        self.online = True
        self.on_snap(m)

    def on_down(self, m):
        self.online = False
        self.refresh_ui()

    def on_snap(self, m):
        """Replace local state with the server's snapshot (join, reconnect, resync, restore)."""
        self.objs = {o["id"]: o for o in m["objects"]}
        self.seq, self.users, self.checks, self.resyncing = m["seq"], m["users"], m["checks"], False
        self.live.clear()
        self.cursors.clear()
        self.cv.delete("all")
        for o in self.objs.values():
            self.draw(o)
        self.chat.config(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.config(state="disabled")
        for c in m["chat"]:
            self.show_chat(c)
        self.refresh_ui()
        # After a reconnect: re-show and resend anything the server never confirmed (it ignores duplicates).
        for key, op in list(self.pending.items()):
            oid = op["obj"]["id"] if op["k"] == "add" else op["id"]
            if (oid in self.objs) == (op["k"] == "add"):
                del self.pending[key]       # server already has the effect
            else:
                self.apply(op)
                self.send({"t": "op", "op": op})

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
        elif op["k"] == "del" and m["by"] == self.me["name"]:
            self.pending.pop("d:" + op["id"], None)
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
            x, y, tag = m["x"], m["y"], "cur:" + m["u"]
            self.cv.create_polygon(x, y, x + 12, y + 4, x + 4, y + 12, fill=u["color"], tags=(tag, "cursor"))
            self.cv.create_text(x + 14, y + 10, text=m["u"], anchor="nw", fill=u["color"], tags=(tag, "cursor"))

    # ---------- chat ----------
    def send_chat(self, _):
        if self.entry.get().strip():
            self.send({"t": "chat", "text": self.entry.get()})
            self.entry.delete(0, "end")

    def show_chat(self, c):
        self.chat.config(state="normal")
        stamp = time.strftime("%H:%M:%S", time.localtime(c["ts"]))
        if c["u"]:
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
        else:
            self.cv.delete("obj")

    def do(self, op):
        """Local action: show it immediately, remember it until the server confirms, send it."""
        self.apply(op)
        self.pending[op["obj"]["id"] if op["k"] == "add" else "d:" + op["id"]] = op
        self.send({"t": "op", "op": op})

    def draw(self, o, tag=None):
        tags = ("obj", "o:" + o["id"]) if tag is None else (tag,)
        k, p, c, w, cv = o["k"], o["p"], o["c"], o["w"], self.cv
        if k in ("stroke", "line"):
            cv.create_line(*(p if len(p) > 2 else p + p), fill=c, width=w, capstyle="round", joinstyle="round", tags=tags)
        elif k == "rect":
            cv.create_rectangle(*p, outline=c, width=w, tags=tags)
        elif k == "oval":
            cv.create_oval(*p, outline=c, width=w, tags=tags)
        elif k == "text":
            cv.create_text(*p, text=o["s"], fill=c, anchor="nw", width=500, font=("Helvetica", 10 + 2 * w), tags=tags)
        elif k == "note":
            x, y = p
            cv.create_rectangle(x, y, x + 170, y + 130, fill="#fff59d", outline=c, width=2, tags=tags)
            cv.create_text(x + 8, y + 8, text=o["s"], anchor="nw", width=154, fill="#222", tags=tags)
        cv.tag_raise("cursor")

    def pos(self, e):
        return round(self.cv.canvasx(e.x)), round(self.cv.canvasy(e.y))

    def motion(self, e):
        if time.time() - self.last_cur > 0.05:
            self.last_cur = time.time()
            x, y = self.pos(e)
            self.send({"t": "cur", "x": x, "y": y})

    def erase(self, x, y):
        ids = {t[2:] for i in self.cv.find_overlapping(x - 4, y - 4, x + 4, y + 4)
               for t in self.cv.gettags(i) if t.startswith("o:")}
        for oid in ids:
            self.do({"k": "del", "id": oid})

    def press(self, e):
        if not self.can_edit():
            self.status.config(text="You have view-only access", fg="red")
            return
        x, y = self.pos(e)
        t = self.tool.get()
        if t == "eraser":
            return self.erase(x, y)
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
        if self.tool.get() == "eraser" and self.can_edit():
            return self.erase(x, y)
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
        o, self.cur = self.cur, None
        if not o:
            return
        self.cv.delete("mine")
        if o["k"] == "stroke" or o["p"][:2] != o["p"][2:]:      # ignore zero-size shapes
            self.do({"k": "add", "obj": o})

    # ---------- host actions ----------
    def user_menu(self, e):
        i = self.plist.nearest(e.y)
        if self.me_role() != "host" or i < 0 or i >= len(self.users):
            return
        u = self.users[i]
        if u["role"] == "host":
            return
        menu = tk.Menu(self.root, tearoff=0)
        other = "viewer" if u["role"] == "editor" else "editor"
        menu.add_command(label=f"Make {other}", command=lambda: self.send({"t": "role", "user": u["name"], "role": other}))
        menu.add_command(label="Kick", command=lambda: self.send({"t": "kick", "user": u["name"]}))
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
            p, c, w, k = o["p"], o["c"], o["w"], o["k"]
            st = f'stroke="{c}" stroke-width="{w}" fill="none" stroke-linecap="round" stroke-linejoin="round"'
            if k in ("stroke", "line"):
                pts = " ".join(f"{p[i]},{p[i + 1]}" for i in range(0, len(p), 2))
                out.append(f'<polyline points="{pts if len(p) > 2 else pts + " " + pts}" {st}/>')
            elif k == "rect":
                out.append(f'<rect x="{min(p[0], p[2])}" y="{min(p[1], p[3])}" width="{abs(p[2] - p[0])}" height="{abs(p[3] - p[1])}" {st}/>')
            elif k == "oval":
                out.append(f'<ellipse cx="{(p[0] + p[2]) / 2}" cy="{(p[1] + p[3]) / 2}" rx="{abs(p[2] - p[0]) / 2}" ry="{abs(p[3] - p[1]) / 2}" {st}/>')
            else:
                if k == "note":
                    out.append(f'<rect x="{p[0]}" y="{p[1]}" width="170" height="130" fill="#fff59d" stroke="{c}" stroke-width="2"/>')
                out.append(f'<text x="{p[0] + 8}" y="{p[1] + 22}" fill="{c if k == "text" else "#222"}" font-size="{10 + 2 * w if k == "text" else 13}">{escape(o["s"])}</text>')
        out.append("</svg>")
        Path(path).write_text("\n".join(out))


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
