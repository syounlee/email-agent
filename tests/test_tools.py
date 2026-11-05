"""Offline tests: call the tool functions directly, no Claude involved."""

import json

from email_agent.agent import SYSTEM_PROMPT, build_options
from email_agent.config import load_config
from email_agent.tools import (
    EMAIL_TOOLS, READ_TOOL, SEARCH_TOOL, read_email_tool, search_emails_tool,
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


def test_options_allow_only_the_two_email_tools():
    opts = build_options(load_config())
    assert opts.tools == []  # no built-in tools (Bash, Read, ...)
    assert sorted(opts.allowed_tools) == sorted([SEARCH_TOOL, READ_TOOL])
    assert EMAIL_TOOLS == [SEARCH_TOOL, READ_TOOL]
    assert list(opts.mcp_servers) == ["email"]
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == []


def test_options_use_caps_from_config():
    cfg = load_config()
    opts = build_options(cfg)
    assert opts.model == cfg.model
    assert opts.max_budget_usd == cfg.max_budget_usd
    assert opts.max_turns == cfg.max_turns
    assert opts.system_prompt == SYSTEM_PROMPT


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
