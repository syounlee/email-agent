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


@pytest.fixture(autouse=True)
def separate_database(tmp_path, monkeypatch):
    """Every test gets its own fresh demo database, never your real one."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MAILBOX", "demo")  # tests never touch your real Gmail
    monkeypatch.setenv("NTFY_TOPIC", "")    # ...and never buzz your phone
    monkeypatch.setenv("SUMMARY_HOURS", "8")  # same schedule for every test, whatever .env says
    monkeypatch.setenv("WORK_SENDERS", "workplatform.example.com")  # not your real ones


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="live test: run with --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
