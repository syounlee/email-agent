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
