"""Tests for phone alerts and the morning summary. Free: the network is faked."""

import json
from datetime import date, datetime

import pytest

from email_agent.automation import alerts
from email_agent.automation.listeners import PreviewContext, ScriptContext
from email_agent.core.config import load_config

TOPIC = "ea-test-topic-not-real"


class FakePhone:
    """Stands in for ntfy: records every message instead of sending it."""

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, url, data):
        if self.fail:
            raise OSError("no network")
        self.sent.append({"url": url, **json.loads(data)})


@pytest.fixture
def phone(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    fake = FakePhone()
    monkeypatch.setattr(alerts, "_post", fake)
    return fake


def test_topic_is_never_shown():
    config = load_config()
    assert "ntfy_topic" not in repr(config)


def test_push_sends_title_message_and_priority(phone):
    assert alerts.push("Title 제목", "Message", urgent=True)
    [sent] = phone.sent
    assert sent["url"] == "https://ntfy.sh" and sent["topic"] == TOPIC
    assert sent["title"] == "Title 제목" and sent["priority"] == 4


def test_no_topic_means_no_phone_alerts(monkeypatch):
    fake = FakePhone()
    monkeypatch.setattr(alerts, "_post", fake)
    assert alerts.push("t", "m") is False and fake.sent == []  # NTFY_TOPIC is empty in tests


def test_failed_push_never_prints_the_topic(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    shown = []
    ok = alerts.push("t", "m", post=FakePhone(fail=True), show=shown.append)
    assert ok is False and shown and TOPIC not in " ".join(shown)


async def test_urgent_alert_goes_to_the_phone_now(phone):
    ctx = ScriptContext("important-mail", show=lambda _: None)
    await ctx.alert("e003", "action", "Design Studio: overdue invoice", "Pay $1,200 (by 2026-11-07)",
                    urgent=True)
    assert [s["title"] for s in phone.sent] == ["📝 Design Studio: overdue invoice"]
    assert ctx.actions == ["notified: [phone now] Design Studio: overdue invoice: "
                           "Pay $1,200 (by 2026-11-07)"]
    [saved] = alerts.recent_alerts()
    assert saved["pushed"] == 1 and saved["email_id"] == "e003"


async def test_other_alerts_wait_for_the_summary(phone):
    ctx = ScriptContext("important-mail", show=lambda _: None)
    await ctx.alert("e004", "coupon", "Bookshop: 10% code", "Code BOOK10 until Nov 30")
    assert phone.sent == []
    assert ctx.actions[0].startswith("notified: [summary]")
    assert alerts.recent_alerts()[0]["pushed"] == 0


async def test_security_alerts_never_carry_a_code(phone):
    ctx = ScriptContext("important-mail", show=lambda _: None)
    await ctx.alert("e001", "security", "New sign-in", "Use 123456 if this was you", urgent=True)
    assert "123456" not in phone.sent[0]["message"]


async def test_preview_alert_sends_and_saves_nothing(phone):
    ctx = PreviewContext("t", show=lambda _: None)
    await ctx.alert("e004", "coupon", "Code", "SAVE10", urgent=True)
    assert ctx.actions == ["would alert (phone now): Code: SAVE10"]
    assert phone.sent == [] and alerts.recent_alerts() == []


async def test_summary_sends_once_with_everything_waiting(phone):
    for i, kind in enumerate(["coupon", "money", "action"]):
        alerts.add_alert(f"e00{i}", kind, f"Title {i}", f"Detail {i}", urgent=False, pushed=False)
    alerts.add_alert("e009", "security", "Pushed already", "x", urgent=True, pushed=True)

    assert alerts.send_summary(today=date(2026, 11, 6)) == 3
    [sent] = phone.sent
    assert sent["title"] == "📬 3 things worth knowing"
    lines = sent["message"].splitlines()
    assert lines[0].startswith("📝 Title 2") and len(lines) == 3  # actions first; no repeats
    assert alerts.send_summary() == 0  # nothing new


async def test_summary_keeps_alerts_when_the_phone_is_unreachable(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    alerts.add_alert("e001", "money", "Refund", "₩12,000", urgent=False, pushed=False)
    assert alerts.send_summary(post=FakePhone(fail=True), show=lambda _: None) == 0
    phone = FakePhone()
    assert alerts.send_summary(post=phone) == 1 and len(phone.sent) == 1


def test_summary_goes_out_once_a_day_after_the_hour(phone):
    alerts.add_alert("e001", "money", "Refund", "₩12,000", urgent=False, pushed=False)
    assert alerts.maybe_send_summary(now=datetime(2026, 11, 6, 7, 59)) == 0  # too early
    assert alerts.maybe_send_summary(now=datetime(2026, 11, 6, 8, 0)) == 1
    alerts.add_alert("e002", "money", "Order", "shipped", urgent=False, pushed=False)
    assert alerts.maybe_send_summary(now=datetime(2026, 11, 6, 20, 0)) == 0  # once a day
    assert alerts.maybe_send_summary(now=datetime(2026, 11, 7, 9, 0)) == 1   # next day


def test_long_summary_is_cut_politely():
    items = [{"kind": "money", "title": f"Order {i}", "message": "x" * 100} for i in range(100)]
    title, text = alerts.summary_text(items)
    assert title == "📬 100 things worth knowing"
    assert len(text) <= alerts.MAX_PUSH_CHARS and text.endswith("… and more on the web page")


async def test_alert_says_when_phone_alerts_are_off():
    ctx = ScriptContext("important-mail", show=lambda _: None)
    await ctx.alert("e003", "money", "Overdue", "Pay $240", urgent=True)  # NTFY_TOPIC empty
    assert ctx.actions == ["notified: [phone alerts off] Overdue: Pay $240"]


async def test_one_alert_per_email(phone):
    first = ScriptContext("important-mail", show=lambda _: None)
    second = ScriptContext("work-alerts", show=lambda _: None)
    await first.alert("e010", "action", "Reply about the offsite", "venue", urgent=True)
    await second.alert("e010", "work", "Work: offsite", "From x", urgent=True)
    assert len(phone.sent) == 1 and len(alerts.recent_alerts()) == 1
    assert second.actions == ["already alerted: Work: offsite"]


def test_status_mute_feedback_and_stats():
    alerts.add_alert("e004", "coupon", "Bookshop: 10% code", "BOOK10", urgent=False, pushed=False)
    alerts.add_alert("e008", "action", "Data Digest", "renew", urgent=False, pushed=False)
    [digest, book] = alerts.recent_alerts()
    alerts.set_status(book["num"], "done")
    alerts.set_status(digest["num"], "dismissed", mute=True)
    taught = alerts.feedback()
    assert taught["muted"] == ["newsletter@datadigest.example.com"]
    assert taught["not_important"] == []  # muted: the mute covers it, no example needed
    alerts.add_alert("e002", "money", "Cloud hosting invoice", "$1", urgent=False, pushed=False)
    alerts.set_status(alerts.recent_alerts()[0]["num"], "dismissed")
    assert alerts.feedback()["not_important"] == [
        "Cloud hosting invoice (from billing@cloudhost.example.com)"]
    numbers = alerts.stats(now=datetime(2026, 11, 6, 12))
    assert numbers["counts"] == {"new": 0, "done": 1, "dismissed": 2} and numbers["muted"] == 1
    assert alerts.send_summary() == 0  # handled alerts stay out of the summary
    with pytest.raises(ValueError):
        alerts.set_status(book["num"], "maybe")
