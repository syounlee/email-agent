"""Tests for one-click actions and the chat commands (/run, /actions, /deliver)."""

import json

import pytest

from email_agent.chat import make_approver, make_command_handler
from email_agent.config import PROJECT_ROOT, load_config
from email_agent.listeners import ListenerRegistry
from email_agent.mailbox import read_email
from email_agent.user_actions import (
    ActionInputError, ActionRegistry, build_params, describe, parse_run_command,
    run_action,
)

SPEC = {"days": {"type": "integer", "default": 30}, "dry_run": {"type": "boolean", "default": False},
        "label": {"type": "string"}, "ratio": {"type": "number", "default": 0.5}}


def test_parse_run_command():
    assert parse_run_command('/run tidy days=7 note="two words"') == \
        ("tidy", {"days": "7", "note": "two words"})
    with pytest.raises(ActionInputError, match="key=value"):
        parse_run_command("/run tidy seven")
    with pytest.raises(ActionInputError, match="Use: /run"):
        parse_run_command("/run")


def test_build_params_types_defaults_and_errors():
    assert build_params(SPEC, {"label": "Work", "dry_run": "yes", "days": "7"}) == \
        {"days": 7, "dry_run": True, "label": "Work", "ratio": 0.5}
    with pytest.raises(ActionInputError, match="Missing required parameter label"):
        build_params(SPEC, {})
    with pytest.raises(ActionInputError, match="not a valid integer"):
        build_params(SPEC, {"label": "x", "days": "soon"})
    with pytest.raises(ActionInputError, match="Unknown parameter"):
        build_params(SPEC, {"label": "x", "colour": "red"})


def test_example_action_is_listed():
    text = describe(ActionRegistry())
    assert "/run archive-old-newsletters days=30 dry_run=False" in text


async def test_archive_old_newsletters_dry_run_then_real(monkeypatch):
    # Pretend "today" is 2026-11-05 noon, so the sample dates line up
    import email_agent.database as db
    from datetime import datetime as real_datetime

    class FixedNow(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 11, 5, 12, 0)

    monkeypatch.setattr(db, "datetime", FixedNow)

    dry = await run_action("/run archive-old-newsletters days=3 dry_run=true", show=lambda _: None)
    assert dry["executed"] is False and "would archive 1" in dry["reason"]
    assert "inbox" in read_email("e009")[0]["labels"]  # nothing changed

    real = await run_action("/run archive-old-newsletters days=3", show=lambda _: None)
    assert real["executed"] is True and "archived 1" in real["reason"]
    assert read_email("e009")[0]["labels"] == ["newsletters"]  # e009: 2026-11-01
    assert "inbox" in read_email("e008")[0]["labels"]  # e008 is newer: kept

    again = await run_action("/run archive-old-newsletters days=3", show=lambda _: None)
    assert again["executed"] is False  # already archived

    from email_agent.user_actions import action_log_path
    assert action_log_path().name == "action_runs-demo.jsonl"
    log = action_log_path().read_text().splitlines()
    assert [json.loads(line)["params"]["dry_run"] for line in log] == [True, False, False]


async def test_crashing_action_is_logged_not_raised(tmp_path):
    (tmp_path / "boom.py").write_text(
        "config = {'id': 'boom', 'name': 'b', 'parameters': {}}\n"
        "async def handler(params, context):\n    raise RuntimeError('kaput')\n")
    entry = await run_action("/run boom", ActionRegistry(tmp_path), show=lambda _: None)
    assert entry["executed"] is False and "kaput" in entry["error"]


def test_bad_action_files_are_skipped(tmp_path):
    (tmp_path / "no_params.py").write_text(
        "config = {'id': 'x', 'name': 'x'}\nasync def handler(params, context):\n    pass\n")
    (tmp_path / "bad_type.py").write_text(
        "config = {'id': 'y', 'name': 'y', 'parameters': {'n': {'type': 'float'}}}\n"
        "async def handler(params, context):\n    pass\n")
    registry = ActionRegistry(tmp_path, show=lambda _: None)
    assert registry.all() == [] and len(registry.errors) == 2


# ---------- chat commands ----------

async def test_commands_run_actions_and_deliver_demo_mail(tmp_path):
    listeners = tmp_path / "listeners"
    listeners.mkdir()
    shown = []
    handle = make_command_handler(shown.append, ListenerRegistry(listeners, shown.append),
                                  ActionRegistry(show=shown.append))

    assert await handle("what's new?") is False  # normal questions go to Claude
    assert await handle("/actions") is True and "archive-old-newsletters" in shown[-1]
    assert await handle("/run nope") is True and "No action 'nope'" in shown[-1]
    assert await handle("/run archive-old-newsletters days=x") is True and "valid integer" in shown[-1]
    assert await handle("/deliver parcel") is True and "Choose one of" in shown[-1]
    assert await handle("/frobnicate") is True and "Unknown command" in shown[-1]

    # A listener written while the chat is running is picked up by the next /deliver
    (listeners / "bugs.py").write_text(
        "config = {'id': 'bug-alert', 'name': 'b', 'enabled': True, 'event': 'email_received'}\n"
        "async def handler(email, context):\n"
        "    if 'bug' not in email['subject'].lower():\n"
        "        return {'executed': False, 'reason': 'not a bug report'}\n"
        "    await context.notify('Bug report: ' + email['subject'])\n"
        "    return {'executed': True, 'reason': 'notified'}\n")
    await handle("/deliver bug")
    assert any(s.startswith("🔔 Bug report:") for s in shown)
    assert any("🎧 ✅ bug-alert" in s for s in shown)


async def test_deliver_refused_outside_demo(monkeypatch):
    monkeypatch.setenv("MAILBOX", "imap")
    shown = []
    await make_command_handler(shown.append)("/deliver bug")
    assert "only works with the demo mailbox" in shown[-1]


# ---------- y/n before the agent writes a file ----------

async def test_file_write_shows_code_and_asks():
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    asked, shown = [], []
    approve = make_approver(lambda q: asked.append(q) or "y", shown.append)
    result = await approve("Write", {"file_path": str(PROJECT_ROOT / "scripts/listeners/a.py"),
                                     "content": "line one\nline two"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert asked == ["   ❓ Allow writing scripts/listeners/a.py? (y/n) "]
    assert shown == ["   │ line one", "   │ line two"]

    approve_no = make_approver(lambda q: "n", shown.append)
    edit = await approve_no("Edit", {"file_path": "scripts/listeners/a.py",
                                     "old_string": "x = 1", "new_string": "x = 2"}, None)
    assert isinstance(edit, PermissionResultDeny)
    assert "   - x = 1" in shown and "   + x = 2" in shown
