"""Offline tests: call the tool functions directly, no Claude involved."""

import json
from datetime import datetime

from email_agent.agent import SYSTEM_PROMPT, build_options
from email_agent.config import load_config
from email_agent.tools import (
    ACTION_TOOLS, READ_TOOL, READ_TOOLS, SEARCH_TOOL, TOOL_TO_ACTION, read_email_tool,
    search_emails_tool, star_email_tool, add_label_tool,
)


def _payload(reply: dict) -> dict:
    return json.loads(reply["content"][0]["text"])


async def test_search_tool_finds_invoices():
    data = _payload(await search_emails_tool.handler({"query": "invoice"}))
    ids = [r["id"] for r in data["results"]]
    assert {"e002", "e003"} <= set(ids)
    assert "body" not in data["results"][0]  # previews only


async def test_search_tool_says_when_nothing_matches():
    data = _payload(await search_emails_tool.handler({"query": "zebra-unicorn"}))
    assert data["results"] == []
    assert "No emails matched" in data["note"]


async def test_read_tool_returns_body_and_flags_unknown_ids():
    data = _payload(await read_email_tool.handler({"ids": ["e003", "nope"]}))
    assert "overdue" in data["emails"][0]["body"]
    assert data["not_found"] == ["nope"]


def test_tool_descriptions_say_search_first_then_read():
    assert "first" in search_emails_tool.description.lower()
    assert "read_email" in search_emails_tool.description
    assert "after search_emails" in read_email_tool.description.lower()


def test_options_without_approver_switch_change_tools_off():
    opts = build_options(load_config())
    assert opts.tools == []  # no built-in tools (Bash, Read, ...)
    assert sorted(opts.allowed_tools) == sorted([SEARCH_TOOL, READ_TOOL])
    assert READ_TOOLS == [SEARCH_TOOL, READ_TOOL]
    assert list(opts.mcp_servers) == ["email"]
    assert opts.permission_mode == "dontAsk"
    assert opts.disallowed_tools == ACTION_TOOLS  # can't change mail at all
    assert opts.can_use_tool is None
    assert opts.setting_sources == []


def test_options_with_approver_ask_before_changes():
    async def approver(name, args, ctx):
        raise AssertionError("not called here")

    opts = build_options(load_config(), approver=approver)
    # only reading runs without asking; Write/Edit and mail changes go to approver
    assert opts.allowed_tools == READ_TOOLS + ["Read", "Glob"]
    assert opts.tools == ["Read", "Write", "Edit", "Glob"]  # no Bash, no web
    assert opts.can_use_tool is approver
    assert opts.permission_mode == "default"
    assert opts.disallowed_tools == []
    assert "approves every change" in opts.system_prompt
    assert "listener-creator" in opts.system_prompt


def test_chat_options_load_project_settings_skill_and_guard():
    from email_agent.config import PROJECT_ROOT

    async def approver(name, args, ctx):
        return None

    opts = build_options(load_config(), approver=approver)
    assert opts.cwd == str(PROJECT_ROOT)
    assert opts.setting_sources == ["project"]  # finds .claude/
    assert opts.skills == ["listener-creator"]
    [matcher] = opts.hooks["PreToolUse"]
    assert matcher.matcher == "Read|Write|Edit|Glob" and matcher.on_failure == "block"
    assert (PROJECT_ROOT / ".claude" / "skills" / "listener-creator" / "SKILL.md").exists()


def test_seven_action_tools():
    assert len(ACTION_TOOLS) == 7
    assert TOOL_TO_ACTION["mcp__email__star_email"] == "star"
    assert TOOL_TO_ACTION["mcp__email__archive_email"] == "archive"
    assert "approve" in star_email_tool.description


async def test_star_tool_changes_demo_mailbox():
    data = _payload(await star_email_tool.handler({"id": "e001"}))
    assert data["done"].startswith("Starred 'URGENT")
    from email_agent.mailbox import read_email
    assert read_email("e001")[0]["starred"] is True


async def test_action_tool_reports_problems_to_claude():
    data = _payload(await add_label_tool.handler({"id": "e001", "label": "\\Trash"}))
    assert "system label" in data["error"]
    data = _payload(await star_email_tool.handler({"id": "nope"}))
    assert "No email with id" in data["error"]


def test_options_use_caps_from_config():
    cfg = load_config()
    opts = build_options(cfg)
    assert opts.model == cfg.model
    assert opts.max_budget_usd == cfg.max_budget_usd
    assert opts.max_turns == cfg.max_turns
    assert opts.system_prompt.startswith(SYSTEM_PROMPT)
    assert f"{datetime.now():%Y-%m-%d}" in opts.system_prompt.split("Today is")[1]


def test_system_prompt_rules():
    p = SYSTEM_PROMPT.lower()
    assert "only answer from emails you have read" in p
    assert "sender and date" in p
    assert "found nothing" in p


async def test_search_tool_reports_bad_query_to_claude():
    data = _payload(await search_emails_tool.handler({"query": "is:important"}))
    assert "Unsupported is:important" in data["error"]


async def test_search_tool_understands_gmail_syntax():
    data = _payload(await search_emails_tool.handler({"query": "invoice is:unread"}))
    assert [r["id"] for r in data["results"]] == ["e002"]
