"""Live test: does Claude (Haiku) tell important mail from noise
(for an address you give out freely)?
Uses made-up emails and a preview context (nothing changes, nothing is sent).
About a tenth of a cent. Run with --live."""

import importlib.util
from datetime import date, timedelta

import pytest

from email_agent.automation.listeners import PreviewContext
from email_agent.core.config import PROJECT_ROOT

pytestmark = pytest.mark.live

spec = importlib.util.spec_from_file_location(
    "live_important", PROJECT_ROOT / "scripts" / "listeners" / "important_mail.py")
important = importlib.util.module_from_spec(spec)
spec.loader.exec_module(important)

LATER = (date.today() + timedelta(days=20)).isoformat()
SOON = (date.today() + timedelta(days=1)).isoformat()

EMAILS = {
    "coupon": ("hello@shoes.example.com", "A thank-you gift inside",
               f"Thanks for joining! Use code WELCOME15 for 15% off your next order. "
               f"Valid until {LATER}."),
    "sale": ("deals@shoes.example.com", "BLACK FRIDAY: up to 60% off everything",
             "Our biggest sale of the year. Shop now, while stocks last! Unsubscribe here."),
    "refund": ("billing@stream.example.com", "Your refund has been processed",
               "We refunded $12.99 to your card ending 0000 for your cancelled plan."),
    "action": ("accounts@cloud.example.com", "Your account will be deleted",
               f"Your free account has been inactive. Sign in before {SOON} to keep your "
               "files, or they will be permanently deleted."),
    "newsletter": ("digest@news.example.org", "This week in tech",
                   "Top stories: new phones, a chip shortage, and a review of e-readers."),
}


async def run(key):
    sender, subject, body = EMAILS[key]
    email = {"id": key, "from": sender, "to": "me@example.com", "subject": subject,
             "body": body, "labels": ["\\Inbox"], "read": False, "starred": False}
    ctx = PreviewContext("important-mail", show=lambda _: None)
    result = await important.handler(email, ctx)
    return result, ctx.actions


async def test_coupon_with_a_code_goes_in_the_summary():
    result, actions = await run("coupon")
    assert result["executed"] and "coupon" in result["reason"]
    assert any(a.startswith("would alert (summary)") and "WELCOME15" in a for a in actions)


async def test_plain_sale_and_newsletter_are_ignored():
    for key in ("sale", "newsletter"):
        result, actions = await run(key)
        assert result["executed"] is False, (key, result)
        assert actions == []


async def test_refund_is_money():
    result, _ = await run("refund")
    assert result["executed"] and result["reason"].startswith("money")


async def test_account_deletion_tomorrow_is_urgent():
    result, actions = await run("action")
    assert result["executed"] and "urgent" in result["reason"]
    assert any(a.startswith("would alert (phone now)") for a in actions)
