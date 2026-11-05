"""The board: object schema, op validation, and ``apply_op``.

The visible board is a pure function of the ordered op log. ``apply_op`` is therefore deterministic
and shared by server and client, and convergence follows: apply the same ops in the same ``seq``
order and every machine computes the same ``board_hash``. Per-property last-writer-wins needs no
timestamps, because "last" simply means "applied later in seq order" (Architecture.md section 5.4).
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any

from common.fractional import valid_key
from common.protocol import ErrorCode

KIND_CREATE = "create"
KIND_UPDATE = "update"
KIND_DELETE = "delete"
KIND_CLEAR = "clear"
KIND_RESTORE = "restore"
KIND_BULK_CREATE = "bulk_create"
ALL_KINDS = (KIND_CREATE, KIND_UPDATE, KIND_DELETE, KIND_CLEAR, KIND_RESTORE, KIND_BULK_CREATE)

OBJECT_TYPES = (
    "frame", "rect", "ellipse", "line", "arrow", "path",
    "text", "sticky", "image", "group", "comment",
)  # fmt: skip

MAX_OBJECTS = 50_000
MAX_POINTS = 20_000
MAX_TEXT = 20_000
MAX_ID = 96
MAX_COLOR = 16
MAX_NAME = 200
MAX_IMAGE_B64 = 4 * 1024 * 1024
MAX_BULK = 50_000
COORD_LIMIT = 1e7


def _num(value: Any, lo: float, hi: float) -> bool:
    # bool is a subclass of int in Python, so it has to be excluded explicitly.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and lo <= value <= hi


def _coord(v: Any) -> bool:
    return _num(v, -COORD_LIMIT, COORD_LIMIT)


def _size(v: Any) -> bool:
    return _num(v, 0, COORD_LIMIT)


def _color(v: Any) -> bool:
    if v is None:
        return True  # "no fill" / "no stroke"
    return isinstance(v, str) and 0 < len(v) <= MAX_COLOR and v.startswith("#")


def _text(v: Any) -> bool:
    return isinstance(v, str) and len(v) <= MAX_TEXT


def _name(v: Any) -> bool:
    return isinstance(v, str) and len(v) <= MAX_NAME


def _bool(v: Any) -> bool:
    return isinstance(v, bool)


def _points(v: Any) -> bool:
    """A flat [x0, y0, x1, y1, ...] list, the most compact JSON form for long strokes."""
    if not isinstance(v, list) or len(v) > MAX_POINTS * 2 or len(v) % 2:
        return False
    return all(_coord(n) for n in v)


def _image(v: Any) -> bool:
    return isinstance(v, str) and len(v) <= MAX_IMAGE_B64


def _replies(v: Any) -> bool:
    if not isinstance(v, list) or len(v) > 500:
        return False
    for reply in v:
        if not isinstance(reply, dict) or not _text(reply.get("text")):
            return False
        if not _name(reply.get("author", "")) or not _name(reply.get("ts", "")):
            return False
    return True


PROP_RULES = {
    "x": _coord,
    "y": _coord,
    "w": _size,
    "h": _size,
    "rotation": lambda v: _num(v, -36000, 36000),
    "fill": _color,
    "stroke": _color,
    "stroke_w": lambda v: _num(v, 0, 1000),
    "opacity": lambda v: _num(v, 0, 1),
    "locked": _bool,
    "visible": _bool,
    "name": _name,
    "points": _points,
    "text": _text,
    "font_size": lambda v: _num(v, 1, 1000),
    "font_weight": lambda v: _num(v, 100, 900),
    "image_data": _image,
    "resolved": _bool,
    "replies": _replies,
    "author": _name,
    "x2": _coord,
    "y2": _coord,
}

_LENGTH_CAPS = {
    "text": MAX_TEXT,
    "name": MAX_NAME,
    "author": MAX_NAME,
    "points": MAX_POINTS * 2,
    "image_data": MAX_IMAGE_B64,
    "replies": 500,
}

_COMMON = {"x", "y", "w", "h", "rotation", "opacity", "locked", "visible", "name"}
_PAINT = {"fill", "stroke", "stroke_w"}
_TEXTUAL = {"text", "font_size", "font_weight"}

# Which properties each object type may carry. Anything else is rejected, not silently stored.
TYPE_PROPS: dict[str, set[str]] = {
    "frame": _COMMON | _PAINT,
    "rect": _COMMON | _PAINT,
    "ellipse": _COMMON | _PAINT,
    "line": _COMMON | {"stroke", "stroke_w", "x2", "y2"},
    "arrow": _COMMON | {"stroke", "stroke_w", "x2", "y2"},
    "path": _COMMON | {"stroke", "stroke_w", "points", "fill"},
    "text": _COMMON | _TEXTUAL | {"fill"},
    "sticky": _COMMON | _TEXTUAL | {"fill"},
    "image": _COMMON | {"image_data"},
    "group": _COMMON,
    "comment": {"x", "y", "text", "resolved", "replies", "author", "locked", "visible", "name"},
}


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_ID


def validate_props(obj_type: str, props: Any) -> str | None:
    if not isinstance(props, dict):
        return ErrorCode.BAD_SCHEMA
    allowed = TYPE_PROPS[obj_type]
    for name, value in props.items():
        if name not in allowed:
            return ErrorCode.BAD_SCHEMA
        if not PROP_RULES[name](value):
            # Too long is a different failure from wrong type, and callers can tell them apart.
            cap = _LENGTH_CAPS.get(name)
            if cap is not None and isinstance(value, (str, list)) and len(value) > cap:
                return ErrorCode.LIMIT_EXCEEDED
            return ErrorCode.BAD_SCHEMA
    return None


def validate_object(spec: Any) -> str | None:
    """Validate one object as carried by ``create`` and ``bulk_create``."""
    if not isinstance(spec, dict) or not _valid_id(spec.get("id")):
        return ErrorCode.BAD_SCHEMA
    if spec.get("type") not in OBJECT_TYPES:
        return ErrorCode.BAD_SCHEMA
    parent = spec.get("parent")
    if parent is not None and not _valid_id(parent):
        return ErrorCode.BAD_SCHEMA
    if not valid_key(spec.get("z")):
        return ErrorCode.BAD_SCHEMA
    return validate_props(spec["type"], spec.get("props", {}))


@dataclass
class Board:
    """Flat map of objects. The tree is implied by each object's ``parent`` field.

    Deleted objects stay as tombstones. That is what makes "delete wins over a concurrent update"
    work (OR-04): a late update finds the tombstone and is ignored, rather than recreating anything.
    """

    objects: dict[str, dict[str, Any]] = field(default_factory=dict)

    def live(self) -> list[dict[str, Any]]:
        """Live objects in paint order. Ties on ``z`` break by id, so every client paints alike."""
        alive = [o for o in self.objects.values() if not o["deleted"]]
        return sorted(alive, key=lambda o: (o["z"], o["id"]))

    def get_live(self, obj_id: str) -> dict[str, Any] | None:
        obj = self.objects.get(obj_id)
        return obj if obj is not None and not obj["deleted"] else None

    def to_json(self) -> dict[str, Any]:
        return {"objects": self.objects}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Board:
        return cls(objects=copy.deepcopy(data.get("objects", {})))

    def clone(self) -> Board:
        return Board(objects=copy.deepcopy(self.objects))


def validate_op(board: Board, op: dict[str, Any]) -> str | None:
    """Server-side schema and limit check. Returns an error code, or None if acceptable.

    Runs only on the server, *before* a seq is assigned. ``apply_op`` itself never fails, so every
    replica handles a logged op identically even if its local view differs.
    """
    kind = op.get("kind")
    if kind not in ALL_KINDS:
        return ErrorCode.UNKNOWN_KIND
    if kind == KIND_CREATE:
        err = validate_object(op)
        if err:
            return err
        if board.get_live(op["id"]) is not None:
            return ErrorCode.ID_EXISTS
        if len(board.objects) >= MAX_OBJECTS and op["id"] not in board.objects:
            return ErrorCode.LIMIT_EXCEEDED
    elif kind == KIND_UPDATE:
        if not _valid_id(op.get("id")) or not isinstance(op.get("patch"), dict):
            return ErrorCode.BAD_SCHEMA
        patch = dict(op["patch"])
        # z and parent live on the object itself rather than in props, but travel in the same patch
        # so that a reorder is an ordinary last-writer-wins update.
        if "z" in patch and not valid_key(patch.pop("z")):
            return ErrorCode.BAD_SCHEMA
        if "parent" in patch:
            parent = patch.pop("parent")
            if parent is not None and not _valid_id(parent):
                return ErrorCode.BAD_SCHEMA
        obj = board.objects.get(op["id"])
        # A missing or deleted target is valid and takes a seq but has no effect. Rejecting it
        # would make the outcome depend on whether the sender had seen the delete yet.
        if obj is not None:
            return validate_props(obj["type"], patch)
        for name, value in patch.items():
            if name not in PROP_RULES or not PROP_RULES[name](value):
                return ErrorCode.BAD_SCHEMA
    elif kind == KIND_DELETE:
        if not _valid_id(op.get("id")):
            return ErrorCode.BAD_SCHEMA
    elif kind == KIND_BULK_CREATE:
        objects = op.get("objects")
        if not isinstance(objects, list):
            return ErrorCode.BAD_SCHEMA
        if len(objects) > MAX_BULK:
            return ErrorCode.LIMIT_EXCEEDED
        if not isinstance(op.get("replace", False), bool):
            return ErrorCode.BAD_SCHEMA
        for spec in objects:
            err = validate_object(spec)
            if err:
                return err
            if "deleted" in spec and not isinstance(spec["deleted"], bool):
                return ErrorCode.BAD_SCHEMA
    elif kind == KIND_RESTORE:
        if not isinstance(op.get("checkpoint_id"), str):
            return ErrorCode.BAD_SCHEMA
    return None


def _make_object(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": spec["id"],
        "type": spec["type"],
        "parent": spec.get("parent"),
        "z": spec["z"],
        "props": copy.deepcopy(spec.get("props", {})),
        "deleted": bool(spec.get("deleted", False)),
    }


def apply_op(board: Board, op: dict[str, Any]) -> None:
    """Apply one accepted op. Deterministic, and never raises for a schema-valid op."""
    kind = op["kind"]
    if kind == KIND_CREATE:
        # Creating over a tombstone is how "undo a delete" brings an object back with its id.
        board.objects[op["id"]] = _make_object(op)
    elif kind == KIND_UPDATE:
        obj = board.objects.get(op["id"])
        if obj is None or obj["deleted"]:
            return
        patch = op["patch"]
        for key in ("z", "parent"):
            if key in patch:
                obj[key] = patch[key]
        for name, value in patch.items():
            if name not in ("z", "parent"):
                obj["props"][name] = copy.deepcopy(value)
    elif kind == KIND_DELETE:
        obj = board.objects.get(op["id"])
        if obj is not None:
            obj["deleted"] = True
    elif kind == KIND_CLEAR:
        for obj in board.objects.values():
            obj["deleted"] = True
    elif kind == KIND_BULK_CREATE:
        if op.get("replace"):
            board.objects.clear()
        for spec in op["objects"]:
            board.objects[spec["id"]] = _make_object(spec)
    elif kind == KIND_RESTORE:
        # The server expands a restore into the checkpoint's complete object list before logging
        # it, including tombstones, so the board's hash equals the checkpoint's hash exactly.
        board.objects.clear()
        for spec in op["objects"]:
            board.objects[spec["id"]] = _make_object(spec)
