"""Configure bounded application logs for the terminal and a local log file."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(
    log_file: Path,
    *,
    console_level: int = logging.WARNING,
    file_level: int = logging.INFO,
    datefmt: str = "%Y-%m-%d %H:%M:%S",
) -> None:
    """Send logs to stderr and a rotating UTF-8 log file."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt=datefmt
    )
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    logging.basicConfig(
        level=min(console_level, file_level),
        handlers=[console, file_handler],
        force=True,
    )
