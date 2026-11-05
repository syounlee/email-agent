"""Tests for the SQLite database and Gmail-style search (all free)."""

from contextlib import closing
from datetime import datetime, timedelta

import pytest

from email_agent import database
from email_agent.database import SAMPLE_EMAILS, count, load_json, open_db, parse_query, read, search

NOW = datetime(2026, 11, 5, 12, 0)  # fixed "today" so date tests never drift


@pytest.fixture
def conn(tmp_path):
    with closing(open_db(tmp_path / "emails.db")) as c:
        yield c


def ids(results):
    return [r["id"] for r in results]


# ---------- storage ----------

def test_first_run_loads_the_15_sample_emails(conn):
    assert count(conn) == 15


def test_loading_twice_keeps_15_emails(conn):
    assert load_json(conn, SAMPLE_EMAILS) == 0  # nothing new added
    assert count(conn) == 15


def test_reopening_does_not_reload(tmp_path):
    path = tmp_path / "emails.db"
    open_db(path).close()
    with closing(open_db(path)) as again:
        assert count(again) == 15


def test_database_file_lives_in_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "somewhere"))
    assert database.default_db_path() == tmp_path / "somewhere" / "emails-demo.db"
    monkeypatch.setenv("MAILBOX", "imap")
    assert database.default_db_path() == tmp_path / "somewhere" / "emails-imap.db"


def test_new_imap_database_starts_empty(tmp_path):
    with closing(open_db(tmp_path / "emails-imap.db", load_sample=False)) as c:
        assert count(c) == 0


def test_read_returns_booleans_and_lists(conn):
    [email] = read(conn, ["e003"])
    assert email["read"] is True and email["starred"] is False
    assert email["labels"] == ["inbox", "finance"]
    assert email["from"] == "accounts@designstudio.example.org"


def test_index_follows_changes_to_the_table(conn):
    conn.execute("UPDATE emails SET subject = 'Totally different' WHERE id = 'e003'")
    assert "e003" not in ids(search(conn, "subject:overdue"))
    assert "e003" in ids(search(conn, "invoice"))  # body still says invoice
    conn.execute("DELETE FROM emails WHERE id = 'e002'")
    assert ids(search(conn, "invoice")) == ["e003"]


# ---------- each search form ----------

def test_plain_word_finds_both_invoices(conn):
    assert set(ids(search(conn, "invoice"))) == {"e002", "e003"}


def test_plain_words_are_case_insensitive_and_stemmed(conn):
    assert ids(search(conn, "INVOICES")) == ids(search(conn, "invoice"))


def test_all_words_must_match(conn):
    assert ids(search(conn, "invoice overdue")) == ["e003"]


def test_quoted_phrase_must_appear_in_that_order(conn):
    assert ids(search(conn, '"board numbers"')) == ["e001"]
    assert ids(search(conn, '"numbers board"')) == []


def test_from_matches_part_of_the_sender(conn):
    assert set(ids(search(conn, "from:bookshop"))) == {"e004", "e006"}
    assert ids(search(conn, "from:DANA")) == ["e001"]
    assert ids(search(conn, 'from:"priya.colleague"')) == ["e012", "e010"]


def test_subject_only_looks_at_the_subject(conn):
    assert ids(search(conn, "subject:receipt")) == ["e004", "e005"]
    # 'receipt' is also in e004's body, but 'keep' is only in the body:
    assert ids(search(conn, "subject:keep")) == []
    assert ids(search(conn, 'subject:"planning offsite"')) == ["e012", "e011", "e010"]


def test_is_unread_and_is_read(conn):
    unread = ids(search(conn, "is:unread", limit=50))
    read_ = ids(search(conn, "is:read", limit=50))
    assert set(unread) == {"e001", "e002", "e006", "e007", "e008", "e012", "e015"}
    assert len(unread) + len(read_) == 15


def test_is_starred(conn):
    assert ids(search(conn, "is:starred")) == ["e001"]


def test_newer_than(conn):
    # Oldest email (e014) is 2026-10-29 13:00, just inside 7 days of NOW.
    assert len(search(conn, "newer_than:7d", limit=50, now=NOW)) == 15
    six_days = ids(search(conn, "newer_than:6d", limit=50, now=NOW))
    assert len(six_days) == 14 and "e014" not in six_days
    assert ids(search(conn, "newer_than:1d", limit=50, now=NOW)) == ["e001", "e008", "e015", "e007"]
    assert len(search(conn, "newer_than:1w", limit=50, now=NOW)) == 15


def test_forms_combine(conn):
    assert ids(search(conn, "invoice is:unread")) == ["e002"]
    assert ids(search(conn, "from:priya is:unread newer_than:3d", now=NOW)) == ["e012"]


def test_results_are_newest_first_and_limited(conn):
    results = search(conn, "", limit=5)
    assert len(results) == 5
    dates = [r["date"] for r in results]
    assert dates == sorted(dates, reverse=True)
    assert set(results[0]) == {"id", "from", "subject", "date", "snippet"}


def test_odd_input_does_not_crash(conn):
    assert ids(search(conn, '"unclosed quote invoice')) == []  # treated as words
    assert ids(search(conn, "- * ( )")) != []  # no real words -> no word filter
    # unknown op -> ordinary text: re:q4 is the phrase "re q4"
    assert set(ids(search(conn, "re:q4"))) == {"e011", "e012"}


def test_bad_operators_explain_themselves(conn):
    with pytest.raises(ValueError, match="is:important"):
        search(conn, "is:important")
    with pytest.raises(ValueError, match="newer_than"):
        search(conn, "newer_than:soon")


def test_parse_query_pieces():
    p = parse_query('budget "board numbers" from:dana subject:"q3" is:unread newer_than:2w')
    assert p.words == ["budget", "board numbers"]
    assert p.senders == ["dana"] and p.subject_words == ["q3"]
    assert p.unread is True and p.newer_than == timedelta(days=14)
