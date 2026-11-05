"""Tests for the PreToolUse hook that limits which files the agent may touch."""

import pytest

from email_agent.core.config import PROJECT_ROOT
from email_agent.agent.guard import check_file_access, make_file_guard

ROOT = PROJECT_ROOT


@pytest.mark.parametrize("path", [
    "scripts/listeners/bug_notifier.py",
    str(ROOT / "scripts" / "actions" / "cleanup.py"),
    "scripts/listeners/notes.txt",
    "notes.md",                      # non-Python files elsewhere: allowed (still asks y/n)
])
def test_allowed_writes(path):
    assert check_file_access("Write", {"file_path": path}, ROOT) is None
    assert check_file_access("Edit", {"file_path": path}, ROOT) is None


@pytest.mark.parametrize("path,why", [
    ("email_agent/evil.py", "may only be written"),
    ("tests/test_x.py", "may only be written"),
    ("hello.py", "may only be written"),
    ("scripts/listeners/../../email_agent/x.py", "may only be written"),  # sneaky ..
    ("scripts/x.py", "may only be written"),
    (".claude/skills/listener-creator/SKILL.md", ".claude"),
    (".claude/settings.json", ".claude"),
    (".env", ".env"),
    ("/tmp/outside.py", "outside the project"),
    ("~/Desktop/note.txt", "outside the project"),
])
def test_blocked_writes(path, why):
    for tool in ("Write", "Edit"):
        reason = check_file_access(tool, {"file_path": path}, ROOT)
        assert reason and why in reason, (tool, path, reason)


def test_reading_rules():
    assert check_file_access("Read", {"file_path": "email_agent/agent.py"}, ROOT) is None
    assert check_file_access("Read", {"file_path": "scripts/listeners/finance_labeler.py"}, ROOT) is None
    assert ".env" in check_file_access("Read", {"file_path": ".env"}, ROOT)
    assert ".env" in check_file_access("Read", {"file_path": str(ROOT / ".env.local")}, ROOT)
    assert check_file_access("Read", {"file_path": ".env.example"}, ROOT) is None
    assert "outside" in check_file_access("Read", {"file_path": "/etc/passwd"}, ROOT)


def test_glob_rules():
    assert check_file_access("Glob", {"pattern": "scripts/**/*.py"}, ROOT) is None
    assert "inside the project" in check_file_access("Glob", {"pattern": "/Users/**"}, ROOT)
    assert "inside the project" in check_file_access("Glob", {"pattern": "../**"}, ROOT)
    assert "outside" in check_file_access("Glob", {"pattern": "*", "path": "/"}, ROOT)


def test_symlink_out_of_the_allowed_folder_is_caught(tmp_path):
    (tmp_path / "scripts" / "listeners").mkdir(parents=True)
    (tmp_path / "code").mkdir()
    (tmp_path / "scripts" / "listeners" / "link").symlink_to(tmp_path / "code")
    reason = check_file_access("Write", {"file_path": "scripts/listeners/link/x.py"}, tmp_path)
    assert reason and "may only be written" in reason


async def test_hook_output_denies_with_reason():
    guard = make_file_guard(ROOT)
    blocked = await guard({"tool_name": "Write", "tool_input": {"file_path": "email_agent/x.py",
                                                               "content": "print(1)"}}, "id1", None)
    out = blocked["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert "scripts/listeners/" in out["permissionDecisionReason"]

    allowed = await guard({"tool_name": "Write", "tool_input": {
        "file_path": "scripts/listeners/x.py", "content": ""}}, "id2", None)
    assert allowed == {}  # no opinion: the normal y/n question follows
