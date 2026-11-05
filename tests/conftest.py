"""Shared pytest setup.

Tests marked @pytest.mark.live talk to Claude and cost money.
They are skipped unless you run:  uv run pytest --live
"""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live", action="store_true", default=False,
        help="also run tests marked live (these call Claude and cost money)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="live test: run with --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
