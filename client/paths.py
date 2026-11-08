"""Where the client keeps its files. Stdlib only, so non-UI code and tests avoid importing Qt."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_profile = ""


def set_profile(name: str) -> None:
    """A profile is a separate identity. It lets two clients share one machine for a demo or
    test."""
    global _profile
    _profile = "".join(c for c in name if c.isalnum() or c in "-_")


def app_dir() -> Path:
    override = os.environ.get("LANVAS_HOME")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home()))) / "LANvas"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "LANvas"
    else:
        base = (
            Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "lanvas"
        )
    return base / "profiles" / _profile if _profile else base


def identity_file() -> Path:
    return app_dir() / "identity.id"


def settings_file() -> Path:
    return app_dir() / "settings.json"


def journal_dir() -> Path:
    return app_dir() / "journal"
