"""Live tests: these call Claude and cost a few cents. Run with --live."""

import pytest

from email_agent.ask import ask
from email_agent.tools import READ_TOOL, SEARCH_TOOL

pytestmark = pytest.mark.live


async def test_overdue_invoice_question_searches_then_reads():
    answer = await ask("Which invoices are overdue?")
    names = [name for name, _ in answer.tool_calls]

    assert not answer.error, answer.text
    assert SEARCH_TOOL in names and READ_TOOL in names
    assert names.index(SEARCH_TOOL) < names.index(READ_TOOL)  # search, then read

    read_ids = [i for name, args in answer.tool_calls if name == READ_TOOL for i in args["ids"]]
    assert "e003" in read_ids  # the overdue Design Studio invoice
    assert "DS-0417" in answer.text
    assert answer.cost_usd > 0


async def test_chat_follow_up_uses_earlier_answer():
    from claude_agent_sdk import ClaudeSDKClient

    from email_agent.agent import build_options
    from email_agent.chat import answer_one

    async with ClaudeSDKClient(options=build_options()) as client:
        first = await answer_one(client, "Who sent the urgent email?", show=lambda _: None)
        second = await answer_one(client, "What exactly do they need?", show=lambda _: None)

    assert "dana" in first.result.lower()
    # "they" only makes sense if Claude remembers the first answer
    text = second.result.lower()
    assert "3pm" in text or "3 pm" in text
    assert "slide" in text or "retention" in text
    assert second.total_cost_usd > first.total_cost_usd  # running total grows


async def _star_the_urgent_email(reply: str):
    from claude_agent_sdk import ClaudeSDKClient

    from email_agent.agent import build_options
    from email_agent.chat import answer_one, make_approver
    from email_agent.mailbox import read_email

    asked = []

    def read_input(prompt):
        asked.append(prompt)
        return reply

    tools = []
    options = build_options(approver=make_approver(read_input, show=lambda _: None))
    async with ClaudeSDKClient(options=options) as client:
        result = await answer_one(client, "Star the urgent email.", show=tools.append)
    return asked, tools, result, read_email("e001")[0]


async def test_chat_star_with_approval():
    asked, tools, result, email = await _star_the_urgent_email("y")
    assert asked == ["   ❓ Allow star of 'URGENT: Board numbers needed by 3pm today'? (y/n) "]
    assert any("star_email: e001" in t for t in tools)
    assert email["starred"] is True


async def test_chat_star_declined_changes_nothing():
    asked, tools, result, email = await _star_the_urgent_email("n")
    assert len(asked) >= 1
    assert email["starred"] is False
    # Tells the user it wasn't done. Claude words this differently each time
    # ("not", "didn't", "declined"...), so accept any of the usual phrasings.
    text = result.result.lower().replace("’", "'")
    assert any(word in text for word in ("not", "n't", "declin")), result.result
