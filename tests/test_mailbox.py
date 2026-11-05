import json

from email_agent.database import SAMPLE_EMAILS
from email_agent.mailbox import read_email, search_emails


def load_emails():
    return json.loads(SAMPLE_EMAILS.read_text())


def test_sample_data_has_15_emails_with_all_fields():
    emails = load_emails()
    assert len(emails) == 15
    fields = {"id", "from", "to", "subject", "date", "body", "labels", "read", "starred"}
    for email in emails:
        assert fields <= email.keys()
    assert len({e["id"] for e in emails}) == 15  # ids are unique


def test_search_invoice_finds_both_invoices():
    ids = [r["id"] for r in search_emails("invoice")]
    assert "e002" in ids  # CloudHost invoice
    assert "e003" in ids  # overdue Design Studio invoice


def test_search_is_case_insensitive():
    assert search_emails("INVOICE") == search_emails("invoice")


def test_search_matches_sender():
    results = search_emails("dana.manager")
    assert "e001" in [r["id"] for r in results]


def test_search_results_are_short_and_newest_first():
    results = search_emails("offsite")
    assert len(results) == 3
    dates = [r["date"] for r in results]
    assert dates == sorted(dates, reverse=True)
    assert set(results[0].keys()) == {"id", "from", "subject", "date", "snippet"}


def test_search_needs_all_words():
    assert [r["id"] for r in search_emails("invoice overdue")] == ["e003"]


def test_search_respects_limit_and_no_match():
    assert len(search_emails("", limit=4)) == 4
    assert search_emails("zebra-unicorn") == []


def test_read_email_returns_body():
    [email] = read_email("e003")
    assert "overdue" in email["body"]
    assert email["subject"].startswith("OVERDUE")


def test_read_email_many_ids_keeps_order_and_skips_unknown():
    emails = read_email(["e006", "nope", "e001"])
    assert [e["id"] for e in emails] == ["e006", "e001"]


def test_read_email_gives_booleans_not_numbers():
    [email] = read_email("e001")
    assert email["read"] is False and email["starred"] is False
    assert email["labels"] == ["inbox", "work", "important"]
