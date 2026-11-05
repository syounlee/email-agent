"""Live listener tests: real Claude Haiku calls (a fraction of a cent). Run with --live."""

import shutil

import pytest

from email_agent.core.claude_json import call_claude
from email_agent.core.config import PROJECT_ROOT
from email_agent.automation.listeners import ListenerRegistry, deliver_demo_email
from email_agent.mail.mailbox import read_email

pytestmark = pytest.mark.live


async def test_real_structured_output():
    answer, info = await call_claude(
        "Is 'Your receipt from Example Pharmacy, total paid $19.49' a receipt?",
        {"type": "object", "properties": {"is_receipt": {"type": "boolean"}},
         "required": ["is_receipt"]},
    )
    assert answer == {"is_receipt": True}
    assert info["model"] == "claude-haiku-5-5" and 0 < info["cost_usd"] < 0.01


async def test_real_demo_receipt_gets_finance_label(tmp_path):
    folder = tmp_path / "listeners"
    folder.mkdir()
    src = PROJECT_ROOT / "scripts" / "listeners" / "finance_labeler.py"
    (folder / src.name).write_text(src.read_text().replace('"enabled": False', '"enabled": True'))
    email_id, [entry] = await deliver_demo_email(
        "receipts@pharmacy.example.com", "Your receipt from Example Pharmacy",
        "Thank you!\n1 x Vitamin D ... $12.99\nTotal paid: $12.99 (card ending 0000)",
        registry=ListenerRegistry(folder), show=lambda _: None)
    assert "Finance" in read_email(email_id)[0]["labels"]
    assert entry["executed"] is True and entry["claude_calls"] == 1
