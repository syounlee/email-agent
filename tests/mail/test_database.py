"""Tests for the SQLite database and Gmail-style search (all free)."""

from contextlib import closing
from datetime import datetime, timedelta

import pytest

from email_agent.mail import database
from email_agent.mail.database import SAMPLE_EMAILS, count, load_json, open_db, parse_query, read, search

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


def test_old_databases_get_the_new_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE emails (num INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, "
                "sender TEXT NOT NULL, recipient TEXT NOT NULL, subject TEXT NOT NULL, "
                "date TEXT NOT NULL, body TEXT NOT NULL, labels TEXT NOT NULL DEFAULT '[]', "
                "read INTEGER NOT NULL DEFAULT 0, starred INTEGER NOT NULL DEFAULT 0, "
                "folder TEXT, uid INTEGER, uidvalidity INTEGER)")
    old.execute("INSERT INTO emails (id, sender, recipient, subject, date, body) "
                "VALUES ('x1', 'a', 'b', 's', '2026-11-01', 'hi')")
    old.commit()
    old.close()
    with closing(open_db(path, load_sample=False)) as c:
        assert database.pending_for_listeners(c) == []  # old emails aren't "new"
        assert database.add_emails(c, [{**read(c, ["x1"])[0], "id": "x2"}], pending=True) == ["x2"]
        assert [e["id"] for e in database.pending_for_listeners(c)] == ["x2"]
        database.mark_listened(c, "x2")
        assert database.pending_for_listeners(c) == []


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
    assert ids(search(conn, "is:starred")) == ["e014"]


def test_newer_than(conn):
    # Oldest email (e014) is 2026-10-29 13:00, just inside 7 days of NOW.
    assert len(search(conn, "newer_than:7d", limit=50, now=NOW)) == 15
    six_days = ids(search(conn, "newer_than:6d", limit=50, now=NOW))
    assert len(six_days) == 14 and "e014" not in six_days
    assert ids(search(conn, "newer_than:1d", limit=50, now=NOW)) == ["e001", "e008", "e015", "e007"]
    assert len(search(conn, "newer_than:1w", limit=50, now=NOW)) == 15


def test_older_than(conn):
    old = ids(search(conn, "older_than:5d", limit=50, now=NOW))
    assert set(old) == {"e014", "e005"}  # 2026-10-29 and 2026-10-31
    assert ids(search(conn, "unsubscribe older_than:3d", now=NOW)) == ["e009"]
    both = search(conn, "older_than:5d", limit=50, now=NOW) + \
        search(conn, "newer_than:5d", limit=50, now=NOW)
    assert len(both) == 15


def test_forms_combine(conn):
    assert ids(search(conn, "invoice is:unread")) == ["e002"]
    assert ids(search(conn, "from:priya is:unread newer_than:3d", now=NOW)) == ["e012"]


def test_results_are_newest_first_and_limited(conn):
    results = search(conn, "", limit=5)
    assert len(results) == 5
    dates = [r["date"] for r in results]
    assert dates == sorted(dates, reverse=True)
    assert set(results[0]) == {"id", "from", "subject", "date", "snippet", "labels", "read", "starred"}


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


# ---------- HTML and labels ----------

def test_html_is_stored_but_never_part_of_the_email_claude_reads(conn):
    [e] = read(conn, ["e008"])
    assert e["has_html"] is True
    assert "html" not in e and "<ul" not in str(e)  # tools send this dict to Claude
    assert "<ul" in database.read_html(conn, "e008")
    assert read(conn, ["e001"])[0]["has_html"] is False
    assert database.read_html(conn, "e001") is None
    assert database.read_html(conn, "no-such-id") is None


def test_old_demo_database_gets_the_sample_html(tmp_path):
    path = tmp_path / "old.db"
    with closing(open_db(path)) as c:  # pretend it was made before HTML was kept
        c.execute("UPDATE emails SET html = NULL")
        c.commit()
    with closing(open_db(path)) as c:
        assert "<ul" in database.read_html(c, "e008")


def test_label_search_ignores_case_and_gmail_backslash(conn):
    assert set(ids(search(conn, "label:finance", now=NOW))) == {"e002", "e003"}
    assert set(ids(search(conn, "label:FINANCE overdue", now=NOW))) == {"e003"}
    database.update_state(conn, "e015", read=True, starred=False, labels=["\\Important"])
    assert "e015" in ids(search(conn, "label:important", now=NOW))
