"""Client-side board: optimistic apply, reconciliation by seq, and multi-user undo.

Two boards are kept. ``confirmed`` is exactly the server's order up to ``seq`` and is what gets
hashed
and saved. ``view`` is ``confirmed`` plus still-pending local ops and is what the canvas draws. When
a
broadcast returns, it is applied to ``confirmed`` and affected objects in ``view`` are rebuilt,
which
is the reconciliation described in Architecture.md section 5.4.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from common import fractional
from common.fileformat import board_hash
from common.model import (
    KIND_BULK_CREATE,
    KIND_CLEAR,
    KIND_CREATE,
    KIND_DELETE,
    KIND_RESTORE,
    KIND_UPDATE,
    Board,
    apply_op,
)

MAX_UNDO = 200
# A property that was absent before an edit has no recorded value, so undo restores its default.
_PROP_DEFAULTS: dict[str, Any] = {
    "fill": None,
    "stroke": None,
    "stroke_w": 1,
    "opacity": 1,
    "rotation": 0,
    "locked": False,
    "visible": True,
    "name": "",
    "text": "",
    "font_size": 16,
    "font_weight": 400,
    "x": 0,
    "y": 0,
    "w": 0,
    "h": 0,
    "resolved": False,
    "parent": None,
}
_STRUCTURAL = ("z", "parent")


@dataclass
class UndoStep:
    """One user action. ``before`` and ``after`` hold per-object property values."""

    kind: str
    updates: dict[str, dict[str, tuple[Any, Any]]] = field(
        default_factory=dict
    )  # id -> prop -> old,new
    created: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[dict[str, Any]] = field(default_factory=list)
    replaced: list[dict[str, Any]] | None = None  # the full board before a clear or restore
    op_ids: set[str] = field(default_factory=set)
    group: str | None = None


@dataclass
class UndoResult:
    ops: list[dict[str, Any]]
    skipped: int = 0
    skipped_by: set[str] = field(default_factory=set)


def _object_spec(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": obj["id"],
        "type": obj["type"],
        "parent": obj.get("parent"),
        "z": obj["z"],
        "props": copy.deepcopy(obj["props"]),
    }


def _current(obj: dict[str, Any], prop: str) -> Any:
    return obj.get(prop) if prop in _STRUCTURAL else obj["props"].get(prop)


class Document:
    def __init__(self, client_id: str = "local") -> None:
        self.client_id = client_id
        self.counter = 0
        self.confirmed = Board()
        self.view = Board()
        self.seq = 0
        self.pending: list[dict[str, Any]] = []
        self._buffer: dict[int, dict[str, Any]] = {}
        self.undo_stack: list[UndoStep] = []
        self.redo_stack: list[UndoStep] = []
        self.last_editor: dict[tuple[str, str], str] = {}
        self.my_fp = ""

    # ------------------------------------------------------------------ ids
    def next_op_id(self) -> str:
        self.counter += 1
        return f"{self.client_id}:{self.counter}"

    def new_object_id(self) -> str:
        self.counter += 1
        return f"{self.client_id}-{self.counter}"

    def top_z(self) -> str:
        keys = [o["z"] for o in self.view.objects.values() if not o["deleted"]]
        return fractional.between(max(keys) if keys else None, None)

    # ------------------------------------------------------------------ local edits
    def _send_local(self, op: dict[str, Any]) -> dict[str, Any]:
        op = {"op_id": self.next_op_id(), "base_seq": self.seq, **op}
        self.pending.append(op)
        apply_op(self.view, op)
        return op

    def _push_undo(self, step: UndoStep) -> None:
        self.undo_stack.append(step)
        del self.undo_stack[:-MAX_UNDO]
        self.redo_stack.clear()

    def create(
        self,
        obj_type: str,
        props: dict[str, Any],
        parent: str | None = None,
        z: str | None = None,
        obj_id: str | None = None,
    ) -> dict[str, Any]:
        spec = {
            "id": obj_id or self.new_object_id(),
            "type": obj_type,
            "parent": parent,
            "z": z or self.top_z(),
            "props": props,
        }
        op = self._send_local({"kind": KIND_CREATE, **spec})
        self._push_undo(UndoStep(KIND_CREATE, created=[copy.deepcopy(spec)], op_ids={op["op_id"]}))
        return op

    def update(
        self, obj_id: str, patch: dict[str, Any], group: str | None = None
    ) -> dict[str, Any] | None:
        """Change properties. Calls sharing ``group`` become one undo step (one whole drag)."""
        obj = self.view.get_live(obj_id)
        if obj is None:
            return None
        changes = {p: (copy.deepcopy(_current(obj, p)), copy.deepcopy(v)) for p, v in patch.items()}
        op = self._send_local({"kind": KIND_UPDATE, "id": obj_id, "patch": patch})
        top = self.undo_stack[-1] if self.undo_stack else None
        if group is not None and top is not None and top.group == group:
            step = top
        else:
            step = UndoStep(KIND_UPDATE, group=group)
            self._push_undo(step)
        step.op_ids.add(op["op_id"])
        slot = step.updates.setdefault(obj_id, {})
        for prop, (old, new) in changes.items():
            # Keep the oldest "before" and the newest "after" across a coalesced drag.
            slot[prop] = (slot[prop][0] if prop in slot else old, new)
        return op

    def delete(self, obj_id: str, group: str | None = None) -> dict[str, Any] | None:
        obj = self.view.get_live(obj_id)
        if obj is None:
            return None
        spec = _object_spec(obj)
        op = self._send_local({"kind": KIND_DELETE, "id": obj_id})
        top = self.undo_stack[-1] if self.undo_stack else None
        if group is not None and top is not None and top.group == group:
            top.deleted.append(spec)
            top.op_ids.add(op["op_id"])
        else:
            self._push_undo(
                UndoStep(KIND_DELETE, deleted=[spec], op_ids={op["op_id"]}, group=group)
            )
        return op

    def clear(self) -> dict[str, Any]:
        removed = [_object_spec(o) for o in self.view.live()]
        op = self._send_local({"kind": KIND_CLEAR})
        self._push_undo(UndoStep(KIND_CLEAR, deleted=removed, op_ids={op["op_id"]}))
        return op

    def restore(self, checkpoint_id: str) -> dict[str, Any]:
        """The server fills in the objects, so nothing is applied locally until the broadcast."""
        before = copy.deepcopy(list(self.confirmed.objects.values()))
        op = {
            "kind": KIND_RESTORE,
            "op_id": self.next_op_id(),
            "base_seq": self.seq,
            "checkpoint_id": checkpoint_id,
        }
        self._push_undo(UndoStep(KIND_RESTORE, replaced=before, op_ids={op["op_id"]}))
        return op

    def replace_board(self, objects: list[dict[str, Any]]) -> dict[str, Any]:
        """Load a whole board (create a room from a file). Undo returns to the previous board."""
        before = copy.deepcopy(list(self.confirmed.objects.values()))
        op = self._send_local({"kind": KIND_BULK_CREATE, "objects": objects, "replace": True})
        self._push_undo(UndoStep(KIND_RESTORE, replaced=before, op_ids={op["op_id"]}))
        return op

    # ------------------------------------------------------------------ undo / redo
    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    def undo(self) -> UndoResult:
        return self._travel(self.undo_stack, self.redo_stack, forward=False)

    def redo(self) -> UndoResult:
        return self._travel(self.redo_stack, self.undo_stack, forward=True)

    def _travel(self, source: list[UndoStep], target: list[UndoStep], forward: bool) -> UndoResult:
        if not source:
            return UndoResult([])
        step = source.pop()
        target.append(step)
        step.group = None  # a later drag must never coalesce into a step that was undone
        result = UndoResult([])
        if step.kind == KIND_UPDATE:
            self._travel_updates(step, forward, result)
        elif step.kind == KIND_CREATE:
            if forward:
                for spec in step.created:
                    self._emit(result, {"kind": KIND_CREATE, **copy.deepcopy(spec)})
            else:
                for spec in step.created:
                    self._emit_delete(result, spec["id"])
        elif step.kind == KIND_DELETE:
            for spec in step.deleted:
                if forward:
                    self._emit_delete(result, spec["id"])
                elif self.view.get_live(spec["id"]) is None:
                    self._emit(result, {"kind": KIND_CREATE, **copy.deepcopy(spec)})
        elif step.kind == KIND_CLEAR:
            if forward:
                self._emit(result, {"kind": KIND_CLEAR})
            else:
                self._emit(
                    result, {"kind": KIND_BULK_CREATE, "objects": copy.deepcopy(step.deleted)}
                )
        elif step.kind == KIND_RESTORE:
            # Swap: what was the board becomes the stored version, so redo can come back again.
            current = copy.deepcopy(list(self.view.objects.values()))
            self._emit(
                result, {"kind": KIND_BULK_CREATE, "objects": step.replaced, "replace": True}
            )
            step.replaced = current
        return result

    def _emit(self, result: UndoResult, op: dict[str, Any]) -> None:
        result.ops.append(self._send_local(op))

    def _emit_delete(self, result: UndoResult, obj_id: str) -> None:
        if self.view.get_live(obj_id) is not None:
            self._emit(result, {"kind": KIND_DELETE, "id": obj_id})

    def _travel_updates(self, step: UndoStep, forward: bool, result: UndoResult) -> None:
        """Conflict-aware: restore a property only if it still holds what this user wrote."""
        for obj_id, props in step.updates.items():
            obj = self.view.get_live(obj_id)
            if obj is None:
                result.skipped += len(props)
                continue
            patch: dict[str, Any] = {}
            for prop, (old, new) in props.items():
                expected, wanted = (old, new) if forward else (new, old)
                if _current(obj, prop) != expected:
                    result.skipped += 1
                    editor = self.last_editor.get((obj_id, prop))
                    if editor:
                        result.skipped_by.add(editor)
                    continue
                if wanted is None and prop in _PROP_DEFAULTS:
                    wanted = _PROP_DEFAULTS[prop]
                patch[prop] = copy.deepcopy(wanted)
            if patch:
                self._emit(result, {"kind": KIND_UPDATE, "id": obj_id, "patch": patch})

    # ------------------------------------------------------------------ server events
    def load_snapshot(self, board: dict[str, Any], seq: int) -> set[str]:
        """Adopt the server's board. Pending ops are re-applied on top until they are resolved."""
        before = set(self.view.objects)
        self.confirmed = Board.from_json(board)
        self.seq = seq
        self._buffer = {s: op for s, op in self._buffer.items() if s > seq}
        self._rebuild_view()
        return before | set(self.view.objects)

    def receive(self, op: dict[str, Any]) -> tuple[set[str], bool]:
        """Handle one sequenced op. Returns (changed object ids, gap_detected)."""
        seq = op["seq"]
        if seq <= self.seq:
            return set(), False  # duplicate delivery
        self._buffer[seq] = op
        changed: set[str] = set()
        while self.seq + 1 in self._buffer:
            changed |= self._apply_confirmed(self._buffer.pop(self.seq + 1))
        return changed, bool(self._buffer)

    def _apply_confirmed(self, op: dict[str, Any]) -> set[str]:
        self.seq = op["seq"]
        apply_op(self.confirmed, op)
        was_mine = False
        op_id = op.get("op_id")
        if op_id is not None:
            remaining = [p for p in self.pending if p["op_id"] != op_id]
            was_mine = len(remaining) != len(self.pending)
            self.pending = remaining
        if not was_mine and op["kind"] == KIND_UPDATE:
            editor = op.get("author_name", "another user")
            for prop in op.get("patch", {}):
                self.last_editor[(op["id"], prop)] = editor
        if op["kind"] in (KIND_CLEAR, KIND_RESTORE, KIND_BULK_CREATE):
            before = set(self.view.objects)
            self._rebuild_view()
            return before | set(self.view.objects)
        target = op.get("id")
        if not self.pending:
            # Common case with nothing pending: view and confirmed differ only by this object.
            self._copy_to_view(target)
        else:
            self._rebuild_object(target)
        return {target}

    def _copy_to_view(self, obj_id: str) -> None:
        obj = self.confirmed.objects.get(obj_id)
        if obj is None:
            self.view.objects.pop(obj_id, None)
        else:
            self.view.objects[obj_id] = copy.deepcopy(obj)

    def _rebuild_object(self, obj_id: str) -> None:
        self._copy_to_view(obj_id)
        for op in self.pending:
            if op.get("id") == obj_id:
                apply_op(self.view, op)
            elif op["kind"] in (KIND_CLEAR, KIND_BULK_CREATE):
                self._rebuild_view()
                return

    def _rebuild_view(self) -> None:
        self.view = self.confirmed.clone()
        for op in self.pending:
            if op["kind"] != KIND_RESTORE:
                apply_op(self.view, op)

    def on_ack(self, op_id: str, seq: int) -> set[str]:
        """A duplicate ack can reference an op that a snapshot already included."""
        if seq <= self.seq:
            return self._forget_pending(op_id)
        return set()

    def on_reject(self, op_id: str) -> set[str]:
        """The server refused an op: roll it back, and drop any undo step it belonged to."""
        changed = self._forget_pending(op_id)
        for stack in (self.undo_stack, self.redo_stack):
            for step in list(stack):
                if op_id in step.op_ids:
                    step.op_ids.discard(op_id)
                    if not step.op_ids:
                        stack.remove(step)
        return changed

    def _forget_pending(self, op_id: str) -> set[str]:
        ops = [p for p in self.pending if p["op_id"] == op_id]
        if not ops:
            return set()
        self.pending = [p for p in self.pending if p["op_id"] != op_id]
        before = set(self.view.objects)
        self._rebuild_view()
        target = ops[0].get("id")
        if target and ops[0]["kind"] in (KIND_CREATE, KIND_UPDATE, KIND_DELETE):
            return {target}
        return before | set(self.view.objects)

    def hash(self) -> str:
        return board_hash(self.confirmed)
