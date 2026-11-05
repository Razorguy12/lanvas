"""The durable writer (Architecture.md section 12.1). Every data file on disk goes through here.

The spec places this helper in ``server/persist.py`` "mirrored in ``client/recovery.py``". A copied
implementation of crash-safety code would drift, so the one implementation lives in ``common/`` and
both of those modules re-export it. ``client/`` still never imports ``server/``.

Rules enforced:
1. Whole files are replaced atomically, so a crash leaves the old file or the new file, never half.
2. Log lines carry a CRC, so a torn tail is detected on startup and truncated.
3. Files are written with ``\\n`` line endings on every OS.

The whole-file writer keeps the previous version as ``<name>.bak`` when asked, which is what lets
the integrity scan recover from a corrupted ``acl.json`` (SV-06).
"""

from __future__ import annotations

import json
import logging
import os
import time
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Named crash points. tests/harness.py FaultyFS raises at each one in turn (SV-03).
STEP_TMP_WRITTEN = "tmp_written"
STEP_TMP_SYNCED = "tmp_synced"
STEP_BAK_DONE = "bak_done"
STEP_REPLACED = "replaced"
STEP_APPEND_PARTIAL = "append_partial"
STEP_APPEND_WRITTEN = "append_written"

FaultHook = Callable[[str], None]
_fault_hook: FaultHook | None = None


def set_fault_hook(hook: FaultHook | None) -> None:
    """Test-only seam. Production code never sets this."""
    global _fault_hook
    _fault_hook = hook


def _step(name: str) -> None:
    if _fault_hook is not None:
        _fault_hook(name)


def _fsync_dir(directory: Path) -> None:
    """fsync the directory so the rename itself survives power loss on POSIX.

    A rename only changes the directory entry. Without this, the data can be durable while the
    name still points at the old file after a crash. Windows cannot open a directory this way and
    NTFS journals metadata anyway, so failure here is expected there and ignored.
    """
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_with_retry(src: Path, dst: Path, attempts: int = 5) -> None:
    """On Windows os.replace fails while antivirus or a viewer holds the target; retry briefly."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_write(path: Path, data: bytes, keep_bak: bool = False, mode: int | None = None) -> None:
    """Write ``data`` to ``path`` so that a crash at any instant leaves a complete file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, data)
        _step(STEP_TMP_WRITTEN)
        os.fsync(fd)
        _step(STEP_TMP_SYNCED)
    finally:
        os.close(fd)
    if mode is not None:
        os.chmod(tmp, mode)
    if keep_bak and path.exists():
        # The old version is copied through its own atomic path, never renamed away: renaming
        # would open a window in which `path` does not exist at all.
        bak = path.with_name(path.name + ".bak")
        bak_tmp = path.with_name(path.name + ".bak.tmp")
        bak_tmp.write_bytes(path.read_bytes())
        _replace_with_retry(bak_tmp, bak)
    _step(STEP_BAK_DONE)
    _replace_with_retry(tmp, path)
    _step(STEP_REPLACED)
    _fsync_dir(path.parent)


def atomic_write_json(path: Path, obj: Any, keep_bak: bool = False) -> None:
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), indent=None)
    atomic_write(path, (text + "\n").encode(), keep_bak=keep_bak)


def read_json_with_bak(path: Path) -> tuple[Any | None, bool]:
    """Read JSON, falling back to ``.bak``. Returns (value, used_backup)."""
    for candidate, used_bak in ((path, False), (path.with_name(path.name + ".bak"), True)):
        try:
            return json.loads(candidate.read_text(encoding="utf-8")), used_bak
        except (OSError, ValueError):
            continue
    return None, False


def _crc(data: Any) -> int:
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return zlib.crc32(canonical.encode())


def encode_log_line(seq: int, data: Any) -> bytes:
    line = {"seq": seq, "data": data, "crc": _crc(data)}
    return (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def decode_log_line(raw: bytes) -> tuple[int, Any] | None:
    """Return (seq, data), or None if the line is torn or fails its checksum."""
    try:
        line = json.loads(raw.decode("utf-8"))
        if _crc(line["data"]) != line["crc"]:
            return None
        return int(line["seq"]), line["data"]
    except (ValueError, KeyError, TypeError):
        return None


class AppendLog:
    """Append-only, self-checking log with group commit.

    ``append`` hands the bytes straight to the OS with ``os.write``. A killed *process* therefore
    loses nothing, because the page cache outlives it. Only a machine power cut can lose data, and
    that is bounded by the group-commit window: the loop calls :meth:`sync` at most 100 ms after a
    write, which keeps fsync off the per-op latency path (Architecture.md section 9).
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600
        )
        self._dirty = False

    def append(self, seq: int, data: Any) -> None:
        line = encode_log_line(seq, data)
        if _fault_hook is not None:
            # Only tests pay for the split write that lets a crash land in the middle of a line.
            os.write(self._fd, line[: len(line) // 2])
            _step(STEP_APPEND_PARTIAL)
            os.write(self._fd, line[len(line) // 2 :])
        else:
            os.write(self._fd, line)
        self._dirty = True
        _step(STEP_APPEND_WRITTEN)

    @property
    def dirty(self) -> bool:
        return self._dirty

    def sync(self) -> None:
        if self._dirty:
            os.fsync(self._fd)
            self._dirty = False

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


def scan_log(path: Path) -> tuple[list[tuple[int, Any]], int, bool]:
    """Read every good entry from a log. Returns (entries, good_byte_length, had_bad_line).

    Reading stops at the first bad line, because everything after it has lost its ordering
    guarantee. Sequence numbers must also increase, which catches lines spliced in by hand.
    """
    if not path.exists():
        return [], 0, False
    raw = path.read_bytes()
    entries: list[tuple[int, Any]] = []
    good = 0
    pos = 0
    last_seq: int | None = None
    while pos < len(raw):
        end = raw.find(b"\n", pos)
        if end < 0:
            return entries, good, True  # no newline means the last write was torn
        decoded = decode_log_line(raw[pos:end])
        if decoded is None or (last_seq is not None and decoded[0] <= last_seq):
            return entries, good, True
        entries.append(decoded)
        last_seq = decoded[0]
        pos = end + 1
        good = pos
    return entries, good, False


def truncate_log(path: Path, length: int) -> None:
    """Cut a log back to its last good line, durably."""
    fd = os.open(path, os.O_WRONLY | getattr(os, "O_BINARY", 0))
    try:
        os.ftruncate(fd, length)
        os.fsync(fd)
    finally:
        os.close(fd)


def rewrite_log(path: Path, entries: Iterator[tuple[int, Any]] | list[tuple[int, Any]]) -> None:
    """Replace a whole log atomically; used when retention prunes chat history."""
    atomic_write(path, b"".join(encode_log_line(seq, data) for seq, data in entries))
