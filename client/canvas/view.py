"""The canvas widget: zoom, pan, tool dispatch, and everything drawn over the document.

The document is rendered by the raster painter into one image. Selection handles, remote cursors,
guides and previews are separate Tk canvas items drawn over it, never part of the rendered
document. Export paints scene items only, so none of these can ever reach an exported file
(Design.md section 12).
"""

from __future__ import annotations

import time
import tkinter as tk
import uuid
from typing import Any

from PIL import Image, ImageTk

from client import theme
from client.canvas.items import flat_to_path, font_props, object_rect, object_transform
from client.canvas.paint import RasterPainter, flatten, text_height
from client.canvas.scene import BoardScene
from client.canvas.tools import (
    CommentTool,
    EraserTool,
    HandTool,
    LineTool,
    Mods,
    PenTool,
    SelectTool,
    ShapeTool,
    StickyTool,
    TextTool,
    Tool,
    handle_points,
)
from client.geom import Affine, Point, Rect
from client.widgets import Signal, after_ms, ask_text
from common import fractional

MIN_ZOOM = 0.05
MAX_ZOOM = 16.0
CURSOR_INTERVAL = 0.05  # about 20 Hz (Architecture.md section 9)
STALE_CURSOR = 15.0
ZOOM_STEP = 1.15
IDLE_RENDER_MS = 140  # after interaction stops, repaint once more with anti-aliasing
WHEEL_STEP = 120


class CanvasView(tk.Canvas):
    def __init__(self, session: Any, parent: Any = None) -> None:
        super().__init__(parent, highlightthickness=0, borderwidth=0,
                         background=theme.current().bg_canvas, takefocus=True)
        self.session = session
        self.board_scene = BoardScene(self)
        self.selection_changed = Signal()
        self.zoom_changed = Signal()
        self.tool_changed = Signal()
        self.comment_requested = Signal()
        self.selection: set[str] = set()
        self.stroke_color = "#1E1E1E"
        self.fill_color: str | None = "#D9D9D9"
        self.stroke_width = 2.0
        self.tools: dict[str, Tool] = {
            "select": SelectTool(self),
            "hand": HandTool(self),
            "pen": PenTool(self),
            "eraser": EraserTool(self),
            "rect": ShapeTool(self, "rect"),
            "ellipse": ShapeTool(self, "ellipse"),
            "frame": ShapeTool(self, "frame"),
            "line": LineTool(self, "line"),
            "arrow": LineTool(self, "arrow"),
            "text": TextTool(self),
            "sticky": StickyTool(self),
            "comment": CommentTool(self),
        }
        self.tool: Tool = self.tools["select"]
        self.remote_cursors: dict[str, dict[str, Any]] = {}
        self.remote_selections: dict[str, dict[str, Any]] = {}
        self.remote_strokes: dict[str, dict[str, Any]] = {}
        self.view = Affine()  # scene to widget pixels: uniform scale, then translation
        self._size = (0, 0)
        self._panning = False
        self._space_down = False
        self._pan_anchor = (0.0, 0.0)
        self._press_tool: Tool | None = None
        self._last_cursor_send = 0.0
        self._editor: tk.Text | None = None
        self._editor_window: int | None = None
        self._editing: tuple[str, bool] | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._image_item = self.create_image(0, 0, anchor="nw", tags=("scene",))
        self._render_job: str | None = None
        self._idle_job: str | None = None
        self._overlay_job: str | None = None
        self._interactive = False
        self._rendered_view: Affine | None = None
        self._buttons = 0
        self.bind("<Configure>", self._on_configure)
        self.bind("<ButtonPress-1>", lambda e: self._press(e, 1))
        self.bind("<ButtonPress-2>", lambda e: self._press(e, 2))
        self.bind("<B1-Motion>", self._move)
        self.bind("<B2-Motion>", self._move)
        self.bind("<Motion>", self._move)
        self.bind("<ButtonRelease-1>", lambda e: self._release(e, 1))
        self.bind("<ButtonRelease-2>", lambda e: self._release(e, 2))
        self.bind("<Double-Button-1>", self._double)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Shift-MouseWheel>", lambda e: self._wheel(e, horizontal=True))
        self.bind("<Button-4>", lambda e: self._wheel(e, delta=WHEEL_STEP))
        self.bind("<Button-5>", lambda e: self._wheel(e, delta=-WHEEL_STEP))
        self.bind("<Shift-Button-4>", lambda e: self._wheel(e, WHEEL_STEP, True))
        self.bind("<Shift-Button-5>", lambda e: self._wheel(e, -WHEEL_STEP, True))
        self.bind("<KeyPress-space>", self._space_press)
        self.bind("<KeyRelease-space>", self._space_release)
        self.bind("<Escape>", self._escape)
        self._stale_tick()

    # ------------------------------------------------------------------ session helpers
    def doc(self) -> Any:
        return self.session.doc

    def can_edit(self) -> bool:
        if self.session.role == "owner":
            return True
        return self.session.role == "editor" and not self.session.settings.get("locked")

    def scale_factor(self) -> float:
        return self.view.a

    def sync(self, changed: set[str] | None = None) -> None:
        self.board_scene.sync(self.doc().view, changed)
        gone = {i for i in self.selection if self.doc().view.get_live(i) is None}
        if gone:
            self.selection -= gone
            self.selection_changed.emit()
        self.schedule_render()

    # ------------------------------------------------------------------ coordinates
    def width_px(self) -> int:
        return max(1, self.winfo_width() if self._size[0] == 0 else self._size[0])

    def height_px(self) -> int:
        return max(1, self.winfo_height() if self._size[1] == 0 else self._size[1])

    def mapToScene(self, x: float, y: float) -> Point:  # noqa: N802
        inverse, _ = self.view.inverted()
        return inverse.map(Point(x, y))

    def mapFromScene(self, p: Point) -> tuple[float, float]:  # noqa: N802
        return self.view.map_xy(p.x, p.y)

    def visible_scene_rect(self) -> Rect:
        a = self.mapToScene(0, 0)
        b = self.mapToScene(self.width_px(), self.height_px())
        return Rect.from_points(a, b).normalized()

    def view_center_scene(self) -> Point:
        return self.mapToScene(self.width_px() / 2, self.height_px() / 2)

    def translate(self, dx: float, dy: float) -> None:
        """Move the scene by (dx, dy) scene units, as QGraphicsView.translate does."""
        s = self.view.a
        self.view = Affine(a=s, d=s, e=self.view.e + dx * s, f=self.view.f + dy * s)

    def _on_configure(self, event: Any) -> None:
        old = self._size
        self._size = (event.width, event.height)
        if old == (0, 0):
            self.view = Affine(e=event.width / 2, f=event.height / 2)
        else:
            # Keep the view centered while the window resizes (Qt's AnchorViewCenter).
            self.view = Affine(a=self.view.a, d=self.view.d,
                               e=self.view.e + (event.width - old[0]) / 2,
                               f=self.view.f + (event.height - old[1]) / 2)
        self.schedule_render()

    # ------------------------------------------------------------------ rendering
    def schedule_render(self, interactive: bool = False) -> None:
        if interactive:
            self._interactive = True
        if self._render_job is None:
            self._render_job = self.after_idle(self._render)

    def _render(self) -> None:
        self._render_job = None
        quick = self._interactive
        self._interactive = False
        self.render_now(supersample=1 if quick else 2)
        if quick:
            if self._idle_job is not None:
                self.after_cancel(self._idle_job)
            self._idle_job = after_ms(self, IDLE_RENDER_MS, self._idle_render)

    def _idle_render(self) -> None:
        self._idle_job = None
        self.render_now(supersample=2)

    def render_now(self, supersample: int = 2) -> Image.Image:
        width, height = self.width_px(), self.height_px()
        image = Image.new("RGB", (width, height), theme.rgb(theme.current().bg_canvas))
        visible = self.visible_scene_rect()
        painter = RasterPainter(image, self.view, supersample)
        for item in self.board_scene.ordered():
            if item.isVisible() and item.sceneBoundingRect().intersects(visible):
                item.paint_with(painter)
        if self._photo is None or (self._photo.width(), self._photo.height()) != (width, height):
            self._photo = ImageTk.PhotoImage(image, master=self)
            self.itemconfigure(self._image_item, image=self._photo)
        else:
            self._photo.paste(image)
        self.coords(self._image_item, 0, 0)
        self._rendered_view = self.view
        self._draw_overlay()
        self.last_render = image
        return image

    def refresh_overlay(self) -> None:
        if self._overlay_job is None:
            self._overlay_job = self.after_idle(self._draw_overlay)

    # ------------------------------------------------------------------ tools
    def set_tool(self, name: str) -> None:
        if self.tools[name].edits and not self.can_edit():
            self.session.emit("toast", text="Viewers can't edit. Ask the owner.")
            return
        self.tool.cancel()
        self.tool = self.tools[name]
        self.setCursor(self.tool.cursor)
        self.tool_changed.emit(name)
        self.refresh_overlay()

    def setCursor(self, cursor: str) -> None:  # noqa: N802
        try:
            self.configure(cursor=cursor)
        except tk.TclError:
            self.configure(cursor="arrow")

    def select_tool_after_create(self) -> None:
        self.set_tool("select")

    def next_name(self, kind: str) -> str:
        count = sum(1 for o in self.doc().view.objects.values() if o["type"] == kind) + 1
        return f"{kind.capitalize()} {count}"

    def bottom_z(self) -> str:
        keys = [o["z"] for o in self.doc().view.objects.values() if not o["deleted"]]
        return fractional.between(None, min(keys)) if keys else fractional.between(None, None)

    def frame_at(self, pos: Point) -> str | None:
        """The innermost frame under a point. New objects become its children and move with it."""
        for item in self.board_scene.items(pos):
            if item.obj["type"] == "frame":
                return item.obj["id"]
        return None

    # ------------------------------------------------------------------ selection model
    def set_selection(self, ids: set[str]) -> None:
        if ids == self.selection:
            return
        self.selection = set(ids)
        self.session.send_transient("selection", {"ids": sorted(self.selection)[:200]})
        self.selection_changed.emit()
        self.refresh_overlay()

    def toggle_selected(self, obj_id: str) -> None:
        self.set_selection(self.selection ^ {obj_id})

    def selection_root(self, obj_id: str) -> str:
        """Clicking a group member selects the outermost group, as in Figma."""
        board = self.doc().view
        current = obj_id
        while True:
            obj = board.get_live(current)
            parent = board.get_live(obj["parent"]) if obj and obj.get("parent") else None
            if parent is None or parent["type"] != "group":
                return current
            current = parent["id"]

    def descendants(self, obj_id: str) -> set[str]:
        found: set[str] = set()
        frontier = [obj_id]
        objects = self.doc().view.live()
        while frontier:
            current = frontier.pop()
            for obj in objects:
                if obj.get("parent") == current and obj["id"] not in found:
                    found.add(obj["id"])
                    frontier.append(obj["id"])
        return found

    def selection_with_descendants(self) -> set[str]:
        ids = set(self.selection)
        for obj_id in self.selection:
            ids |= self.descendants(obj_id)
        return ids

    def single_selected(self) -> dict[str, Any] | None:
        if len(self.selection) != 1:
            return None
        return self.doc().view.get_live(next(iter(self.selection)))

    def selection_is_single_rotatable(self) -> bool:
        obj = self.single_selected()
        return obj is not None and obj["type"] in (
            "rect", "ellipse", "frame", "text", "sticky", "image", "path",
        )

    def selection_rect(self) -> Rect:
        return self.board_scene.content_rect(self.selection) if self.selection else Rect()

    def origin_rect(self, origin: dict[str, dict[str, Any]]) -> Rect:
        """Bounding box of objects at their drag-start positions, without any stroke margin."""
        rect = Rect()
        for obj_id in self.selection:
            data = origin.get(obj_id)
            if data is None:
                continue
            if data["type"] in ("line", "arrow"):
                box = Rect(data["x"], data["y"], data.get("x2") or 0, data.get("y2") or 0)
                box = box.normalized()
            else:
                box = Rect(data["x"], data["y"], data.get("w") or 0, data.get("h") or 0)
            rect = rect.united(box) if not rect.is_null() else box
        return rect

    def refresh_group_bounds(self, undo_group: str) -> None:
        board = self.doc().view
        for obj_id in self.selection:
            obj = board.get_live(obj_id)
            if obj is not None and obj["type"] == "group":
                rect = self.board_scene.content_rect(self.descendants(obj_id))
                if not rect.is_null():
                    self.session.update(
                        obj_id, {"x": rect.x, "y": rect.y, "w": rect.w, "h": rect.h},
                        group=undo_group,
                    )

    # ------------------------------------------------------------------ commands
    def delete_selected(self) -> None:
        if not self.can_edit():
            return
        group = uuid.uuid4().hex[:8]
        for obj_id in sorted(self.selection_with_descendants()):
            obj = self.doc().view.get_live(obj_id)
            if obj is not None and not obj["props"].get("locked"):
                self.session.delete(obj_id, group=group)

    def duplicate_selected(self) -> None:
        if not self.can_edit():
            return
        fresh = set()
        for obj_id in sorted(self.selection):
            obj = self.doc().view.get_live(obj_id)
            if obj is None:
                continue
            props = dict(obj["props"])
            props["x"] = props.get("x", 0) + 20
            props["y"] = props.get("y", 0) + 20
            op = self.session.create(obj["type"], props, parent=obj.get("parent"))
            if op:
                fresh.add(op["id"])
        if fresh:
            self.set_selection(fresh)

    def group_selected(self) -> None:
        if not self.can_edit() or len(self.selection) < 2:
            return
        rect = self.selection_rect()
        op = self.session.create(
            "group",
            {"x": rect.x, "y": rect.y, "w": rect.w, "h": rect.h,
             "name": self.next_name("group")},
        )
        if op:
            token = uuid.uuid4().hex[:8]
            for obj_id in sorted(self.selection):
                self.session.update(obj_id, {"parent": op["id"]}, group=token)
            self.set_selection({op["id"]})

    def change_z(self, forward: bool) -> None:
        """Bring forward or send back: move one step past the neighbor in the z-sorted list."""
        if not self.can_edit():
            return
        ordered = self.doc().view.live()
        for obj_id in sorted(self.selection, key=lambda i: keys_index(ordered, i), reverse=forward):
            index = keys_index(ordered, obj_id)
            target = index + (1 if forward else -1)
            if index < 0 or not 0 <= target < len(ordered):
                continue
            if forward:
                lo = ordered[target]["z"]
                hi = ordered[target + 1]["z"] if target + 1 < len(ordered) else None
            else:
                lo = ordered[target - 1]["z"] if target > 0 else None
                hi = ordered[target]["z"]
            if lo == hi or (lo is not None and hi is not None and lo > hi):
                continue
            self.session.update(obj_id, {"z": fractional.between(lo, hi)})
            ordered = self.doc().view.live()

    def align(self, mode: str) -> None:
        if not self.can_edit() or len(self.selection) < 2:
            return
        box = self.selection_rect()
        token = uuid.uuid4().hex[:8]
        for obj_id in sorted(self.selection):
            item = self.board_scene.items_by_id.get(obj_id)
            if item is None or item.obj["props"].get("locked"):
                continue
            rect = item.sceneBoundingRect()
            dx = dy = 0.0
            if mode == "left":
                dx = box.left - rect.left
            elif mode == "right":
                dx = box.right - rect.right
            elif mode == "top":
                dy = box.top - rect.top
            elif mode == "bottom":
                dy = box.bottom - rect.bottom
            elif mode == "hcenter":
                dx = box.center().x - rect.center().x
            elif mode == "vcenter":
                dy = box.center().y - rect.center().y
            if dx or dy:
                props = item.obj["props"]
                self.session.update(
                    obj_id, {"x": props.get("x", 0) + dx, "y": props.get("y", 0) + dy},
                    group=token,
                )

    def zoom_to(self, factor: float, anchor: tuple[float, float] | None = None) -> None:
        factor = max(MIN_ZOOM, min(MAX_ZOOM, factor))
        ax, ay = anchor if anchor is not None else (self.width_px() / 2, self.height_px() / 2)
        before = self.mapToScene(ax, ay)
        self.view = Affine(a=factor, d=factor, e=ax - before.x * factor, f=ay - before.y * factor)
        self.zoom_changed.emit(factor)
        self.schedule_render(interactive=True)

    def centerOn(self, x: float, y: float) -> None:  # noqa: N802
        s = self.view.a
        self.view = Affine(a=s, d=s, e=self.width_px() / 2 - x * s,
                           f=self.height_px() / 2 - y * s)
        self.schedule_render()

    def fitInView(self, rect: Rect) -> None:  # noqa: N802
        if rect.w <= 0 or rect.h <= 0:
            return
        s = min(self.width_px() / rect.w, self.height_px() / rect.h)
        self.view = Affine(a=s, d=s)
        self.centerOn(rect.center().x, rect.center().y)

    def zoom_fit(self) -> None:
        rect = self.board_scene.content_rect()
        if rect.is_null():
            self.zoom_to(1.0)
            return
        self.fitInView(rect.adjusted(-60, -60, 60, 60))
        if self.scale_factor() > 1.0:
            self.zoom_to(1.0)
            self.centerOn(rect.center().x, rect.center().y)
        self.zoom_changed.emit(self.scale_factor())

    # ------------------------------------------------------------------ text editing
    def edit_text(self, obj_id: str, new_object: bool = False) -> None:
        obj = self.doc().view.get_live(obj_id)
        if obj is None:
            return
        self._commit_editor()
        size, weight = font_props(obj["props"])
        scale = self.scale_factor()
        editor = tk.Text(self, wrap="word", relief="solid", borderwidth=1, highlightthickness=1,
                         highlightcolor=theme.current().accent, background="#FFFFFF",
                         foreground="#1E1E1E", insertbackground="#1E1E1E", padx=0, pady=0,
                         font=theme.font(max(8, int(size * scale)), weight), undo=True)
        editor.insert("1.0", obj["props"].get("text", ""))
        x, y = self.mapFromScene(Point(obj["props"]["x"], obj["props"]["y"]))
        width = max(120, int(obj["props"].get("w", 200) * scale))
        height = max(40, int(obj["props"].get("h", 40) * scale))
        self._editor_window = self.create_window(x, y, window=editor, anchor="nw", width=width,
                                                 height=height, tags=("editor",))
        editor.bind("<Escape>", lambda e: (self._commit_editor(), "break")[1])
        editor.bind("<FocusOut>", lambda e: self._commit_editor())
        editor.focus_set()
        self._editor = editor
        self._editing = (obj_id, new_object)

    def editor_text(self) -> str | None:
        return self._editor.get("1.0", "end-1c") if self._editor is not None else None

    def set_editor_text(self, text: str) -> None:
        if self._editor is not None:
            self._editor.delete("1.0", "end")
            self._editor.insert("1.0", text)

    def _commit_editor(self, cancel: bool = False) -> None:
        if self._editor is None or self._editing is None:
            return
        editor, (obj_id, new_object) = self._editor, self._editing
        self._editor, self._editing = None, None
        text = editor.get("1.0", "end-1c")
        if self._editor_window is not None:
            self.delete(self._editor_window)
            self._editor_window = None
        editor.destroy()
        obj = self.doc().view.get_live(obj_id)
        if obj is None:
            return
        if not text.strip() and new_object and obj["type"] == "text":
            self.session.delete(obj_id)  # an empty text box left behind is just clutter
        elif not cancel and text != obj["props"].get("text", ""):
            patch: dict[str, Any] = {"text": text[:20000]}
            if obj["type"] == "text":
                size, weight = font_props(obj["props"])
                height = text_height(text, obj["props"].get("w", 240), size, weight)
                patch["h"] = max(20.0, height + 4)
            self.session.update(obj_id, patch)
        try:
            self.focus_set()
        except tk.TclError:
            pass

    def create_comment(self, pos: Point) -> None:
        text = ask_text(self, "Add a comment", "Comment", multiline=True)
        if text is None or not text.strip():
            return
        op = self.session.create(
            "comment",
            {"x": pos.x, "y": pos.y, "text": text.strip()[:20000],
             "author": self.session.username, "resolved": False, "replies": []},
        )
        if op:
            self.comment_requested.emit(op["id"])

    def open_comment(self, obj_id: str) -> None:
        self.comment_requested.emit(obj_id)

    # ------------------------------------------------------------------ remote presence
    def on_transient(self, msg: dict[str, Any]) -> None:
        kind = msg.get("k")
        data = msg.get("d")
        source = msg.get("client_id")
        # Transients are forwarded from other clients unvalidated, so every field gets checked.
        if not isinstance(data, dict) or not isinstance(source, str):
            return
        now = time.monotonic()
        if kind == "cursor":
            x, y = data.get("x"), data.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                self.remote_cursors[source] = {
                    "x": float(x), "y": float(y), "name": str(msg.get("name", ""))[:40],
                    "fp": str(msg.get("from", "")), "viewer": data.get("viewer") is True,
                    "time": now,
                }
        elif kind == "selection":
            ids = data.get("ids")
            if isinstance(ids, list):
                self.remote_selections[source] = {
                    "ids": [i for i in ids if isinstance(i, str)][:200],
                    "name": str(msg.get("name", ""))[:40], "fp": str(msg.get("from", "")),
                    "time": now,
                }
        elif kind == "stroke_preview":
            points = data.get("points")
            if isinstance(points, list) and all(isinstance(n, (int, float)) for n in points):
                stroke = self.remote_strokes.setdefault(
                    str(data.get("id")), {"points": [], "color": "#1E1E1E", "w": 2.0, "time": now},
                )
                stroke["points"] = (stroke["points"] + points)[-8000:]
                color = data.get("color")
                width = data.get("w")
                stroke["color"] = color if theme.valid_color(color) else "#1E1E1E"
                stroke["w"] = float(width) if isinstance(width, (int, float)) else 2.0
                stroke["time"] = now
        elif kind == "stroke_end":
            self.remote_strokes.pop(str(data.get("id")), None)
        self.refresh_overlay()

    def _stale_tick(self) -> None:
        self._drop_stale_presence()
        after_ms(self, 2000, self._stale_tick)

    def _drop_stale_presence(self) -> None:
        now = time.monotonic()
        for table in (self.remote_cursors, self.remote_strokes):
            for key in [k for k, v in table.items() if now - v["time"] > STALE_CURSOR]:
                del table[key]
        self.refresh_overlay()

    def forget_presence(self, fingerprint: str) -> None:
        for table in (self.remote_cursors, self.remote_selections):
            for key in [k for k, v in table.items() if v.get("fp") == fingerprint]:
                del table[key]
        self.refresh_overlay()

    def remote_selection_colors(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for selection in self.remote_selections.values():
            for obj_id in selection["ids"]:
                out[obj_id] = theme.user_color(selection["fp"])
        return out

    # ------------------------------------------------------------------ mouse and keys
    def _mods(self, event: Any) -> Mods:
        state = int(getattr(event, "state", 0) or 0)
        return Mods(shift=bool(state & 0x1), ctrl=bool(state & 0x4))

    def _press(self, event: Any, button: int) -> None:
        self._commit_editor()
        self.focus_set()
        self._buttons |= 1 << button
        if button == 2 or self._space_down or self.tool.name == "hand":
            self._panning = True
            self._pan_anchor = (event.x, event.y)
            self.setCursor("fleur")
            return
        if button == 1:
            self._press_tool = self.tool
            self.tool.press(self.mapToScene(event.x, event.y), self._mods(event))

    def _move(self, event: Any) -> None:
        pos = self.mapToScene(event.x, event.y)
        self._send_cursor(pos)
        if self._panning:
            dx, dy = event.x - self._pan_anchor[0], event.y - self._pan_anchor[1]
            self._pan_anchor = (event.x, event.y)
            self.pan_by(dx, dy)
            return
        if self._buttons & 2 and self._press_tool is not None:
            self._press_tool.move(pos, self._mods(event))
            if self._press_tool is not None and self._press_tool.name in ("select",):
                self._interactive = True
        elif isinstance(self.tool, SelectTool):
            self.setCursor(self.tool.hover_cursor(pos))

    def pan_by(self, dx: float, dy: float) -> None:
        """Pan in widget pixels. The rendered image just slides; a full render follows."""
        self.view = Affine(a=self.view.a, d=self.view.d, e=self.view.e + dx, f=self.view.f + dy)
        self.move("scene", dx, dy)
        self.move("overlay", dx, dy)
        self.schedule_render(interactive=True)

    def _release(self, event: Any, button: int) -> None:
        self._buttons &= ~(1 << button)
        if self._panning:
            self._panning = False
            self.setCursor(self.tool.cursor)
            return
        if button == 1 and self._press_tool is not None:
            tool, self._press_tool = self._press_tool, None
            tool.release(self.mapToScene(event.x, event.y), self._mods(event))

    def _double(self, event: Any) -> None:
        self.tool.double_click(self.mapToScene(event.x, event.y), self._mods(event))

    def _wheel(self, event: Any, delta: int | None = None, horizontal: bool = False) -> None:
        amount = delta if delta is not None else int(getattr(event, "delta", 0))
        if abs(amount) < WHEEL_STEP and amount:
            amount = WHEEL_STEP if amount > 0 else -WHEEL_STEP  # macOS sends small deltas
        if self._mods(event).ctrl:
            step = ZOOM_STEP if amount > 0 else 1 / ZOOM_STEP
            self.zoom_to(self.scale_factor() * step, (event.x, event.y))
            return
        if horizontal:
            self.pan_by(amount / 2, 0)
        else:
            self.pan_by(0, amount / 2)

    def _space_press(self, event: Any) -> None:
        if not self._space_down:
            self._space_down = True
            self.setCursor("hand2")

    def _space_release(self, event: Any) -> None:
        self._space_down = False
        self.setCursor(self.tool.cursor)

    def _escape(self, event: Any = None) -> None:
        self.tool.cancel()
        self.set_selection(set())
        self.set_tool("select")

    def _send_cursor(self, pos: Point) -> None:
        now = time.monotonic()
        if now - self._last_cursor_send >= CURSOR_INTERVAL:
            self._last_cursor_send = now
            self.session.send_transient(
                "cursor",
                {"x": round(pos.x, 1), "y": round(pos.y, 1),
                 "viewer": self.session.role == "viewer"},
            )

    # ------------------------------------------------------------------ overlay painting
    def _draw_overlay(self) -> None:
        self._overlay_job = None
        self.delete("overlay")
        self._paint_empty_hint()
        self._paint_remote_selections()
        self._paint_selection()
        self.tool.overlay(self)
        for stroke in self.remote_strokes.values():
            if len(stroke["points"]) >= 4:
                self.paint_stroke(self, stroke["points"], stroke["color"], stroke["w"])
        self._paint_remote_cursors()
        if self._editor_window is not None:
            self.tag_raise("editor")

    def _dev(self, p: Point) -> tuple[float, float]:
        return self.view.map_xy(p.x, p.y)

    def _poly(self, pts: list[Point]) -> list[float]:
        out: list[float] = []
        for p in pts:
            out.extend(self._dev(p))
        return out

    def _paint_empty_hint(self) -> None:
        if self.board_scene.items_by_id:
            return
        hint = "Press P to draw, R for a shape" if self.can_edit() else "Nothing here yet"
        self.create_text(self.width_px() / 2, self.height_px() / 2, text=hint,
                         fill=theme.current().text_muted, font=theme.font(13), tags="overlay")

    def _outline(self, obj: dict[str, Any]) -> list[Point]:
        return object_transform(obj).map_polygon(object_rect(obj))

    def _paint_selection(self) -> None:
        if not self.selection:
            return
        accent = theme.current().accent
        board = self.doc().view
        if len(self.selection) == 1:
            obj = self.single_selected()
            if obj is None:
                return
            self.create_polygon(self._poly(self._outline(obj)), outline=accent, fill="",
                                tags="overlay")
            if not self.can_edit() or obj["type"] == "comment":
                return
            if obj["props"].get("locked"):
                self._paint_lock(obj)
                return
            transform = object_transform(obj)
            points = handle_points(object_rect(obj))
            self._paint_handles([transform.map(p) for p in points.values()])
            return
        for obj_id in self.selection:
            obj = board.get_live(obj_id)
            if obj is not None:
                self.create_polygon(self._poly(self._outline(obj)), outline=accent, fill="",
                                    tags="overlay")
        box = self.selection_rect()
        self.create_polygon(self._poly(box.corners()), outline=accent, fill="", tags="overlay")
        if self.can_edit():
            self._paint_handles(list(handle_points(box).values()))

    def _paint_handles(self, points: list[Point]) -> None:
        accent = theme.current().accent
        for point in points:
            x, y = self._dev(point)
            self.create_rectangle(x - 4, y - 4, x + 4, y + 4, outline=accent, fill="#FFFFFF",
                                  tags="overlay")

    def _paint_lock(self, obj: dict[str, Any]) -> None:
        anchor = object_transform(obj).map(object_rect(obj).corners()[0])
        x, y = self._dev(anchor)
        self.create_text(x, y - 4, text="Locked", anchor="sw", fill=theme.current().accent,
                         font=theme.font(11), tags="overlay")

    def _paint_remote_selections(self) -> None:
        board = self.doc().view
        for selection in self.remote_selections.values():
            color = theme.user_color(selection["fp"])
            first = True
            for obj_id in selection["ids"]:
                obj = board.get_live(obj_id)
                if obj is None:
                    continue
                outline = self._outline(obj)
                self.create_polygon(self._poly(outline), outline=color, fill="", width=1.5,
                                    tags="overlay")
                if first:
                    xs = [self._dev(p)[0] for p in outline]
                    ys = [self._dev(p)[1] for p in outline]
                    self._paint_chip(min(xs), min(ys), selection["name"], color, -1)
                    first = False

    def _paint_chip(self, x: float, y: float, text: str, color: str, side: int = 1) -> None:
        """A name tag at constant screen size. Text is never the only cue, nor color alone."""
        height = 17
        top = y + 4 if side > 0 else y - height - 2
        label = self.create_text(x + 5, top + height / 2, text=text, anchor="w", fill="#FFFFFF",
                                 font=theme.font(11, 600), tags="overlay")
        x0, _, x1, _ = self.bbox(label)
        self.create_polygon(_round_rect(x, top, x1 + 5, top + height, 3), fill=color,
                            outline="", smooth=True, tags="overlay")
        self.tag_raise(label)

    def _paint_remote_cursors(self) -> None:
        for cursor in self.remote_cursors.values():
            color = theme.user_color(cursor["fp"])
            x, y = self._dev(Point(cursor["x"], cursor["y"]))
            self.create_polygon(x, y, x, y + 16, x + 4.5, y + 12, x + 11, y + 12, fill=color,
                                outline="#FFFFFF", tags="overlay")
            label = cursor["name"] + (" (viewing)" if cursor["viewer"] else "")
            self._paint_chip(x + 8, y + 12, label, color)

    def paint_marquee(self, painter: Any, rect: Rect) -> None:
        accent = theme.current().accent
        self.create_polygon(self._poly(rect.corners()), outline=accent, fill=accent,
                            stipple="gray12", tags="overlay")

    def paint_guide(self, painter: Any, start: Point, end: Point) -> None:
        self.create_line(*self._dev(start), *self._dev(end), fill=theme.GUIDE_COLOR,
                         tags="overlay")

    def paint_stroke(self, painter: Any, points: list[float], color: str, width: float) -> None:
        pts = flatten(flat_to_path(points), 6)
        coords = self._poly(pts)
        if len(coords) >= 4:
            self.create_line(*coords, fill=color[:7], width=max(1.0, width * self.scale_factor()),
                             capstyle="round", joinstyle="round", tags="overlay")

    def paint_shape_preview(self, painter: Any, kind: str, rect: Rect) -> None:
        fill = self.fill_color[:7] if self.fill_color and kind != "frame" else "#FFFFFF"
        accent = theme.current().accent
        if kind == "ellipse":
            (x0, y0), (x1, y1) = self._dev(Point(rect.left, rect.top)), self._dev(
                Point(rect.right, rect.bottom))
            self.create_oval(x0, y0, x1, y1, outline=accent, fill=fill, tags="overlay")
        else:
            self.create_polygon(self._poly(rect.corners()), outline=accent, fill=fill,
                                tags="overlay")

    def paint_line_preview(self, painter: Any, start: Point, end: Point, arrow: bool) -> None:
        self.create_line(*self._dev(start), *self._dev(end), fill=self.stroke_color[:7],
                         width=max(1.0, self.stroke_width * self.scale_factor()), tags="overlay")


def _round_rect(x0: float, y0: float, x1: float, y1: float, r: float) -> list[float]:
    """Points for a smoothed polygon that Tk draws as a rounded rectangle."""
    return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
            x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]


def keys_index(ordered: list[dict[str, Any]], obj_id: str) -> int:
    for i, obj in enumerate(ordered):
        if obj["id"] == obj_id:
            return i
    return -1

