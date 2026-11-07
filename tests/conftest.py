"""Pytest setup: reproducible randomness (--seed) and a Tk root for every UI test."""

from __future__ import annotations

import random

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--seed", type=int, default=None, help="seed for randomized tests")


@pytest.fixture
def seed(request: pytest.FixtureRequest) -> int:
    """Randomized tests print this on failure, so a failing run can be reproduced exactly."""
    value = request.config.getoption("--seed")
    if value is None:
        value = random.SystemRandom().randrange(1 << 32)
    print(f"\n[seed] pytest --seed {value}")
    random.seed(value)
    return value


@pytest.fixture(scope="session")
def qapp():
    """A hidden Tk root for widgets created without a window, as QApplication was."""
    import tkinter as tk

    from client import theme

    theme.load_fonts()  # before the first Tk, so Tk's font machinery can see the bundled fonts
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no display at all
        pytest.skip(f"Tk cannot start: {exc}")
    root.geometry("320x200+-4000+-4000")  # shown (so isVisible holds) but out of the way
    theme.apply(root)
    yield root
    root.destroy()
