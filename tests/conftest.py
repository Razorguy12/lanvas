"""Pytest setup: reproducible randomness (--seed) and headless Qt for every UI test."""

from __future__ import annotations

import os
import random

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


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
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
