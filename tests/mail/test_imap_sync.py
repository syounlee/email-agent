"""Offline tests for email parsing and IMAP sync. No network, no Gmail."""

from contextlib import closing
from datetime import date, datetime

import pytest

from email_agent.mail import database
from email_agent.core.config import load_config
from email_agent.mail.imap_sync import html_to_text, parse_email, sync

# ---------- raw example emails, as a mail server would send them ----------

PLAIN = b"""\
From: Alice Example <alice@example.com>
To: me@example.com
Subject: Lunch on Friday?
Date: Wed, 04 Nov 2026 10:30:00 +0000
Message-ID: <plain-1@example.com>
Content-Type: text/plain; charset="utf-8"

Hi! Are you free for lunch on Friday?
Alice
"""

# Plain AND html: we should take the plain part.
MULTIPART = b"""\
From: billing@shop.example.com
To: me@example.com
Subject: Your invoice
Date: Tue, 03 Nov 2026 08:00:00 -0500
Message-ID: <multi-1@example.com>
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="XYZ"

--XYZ
Content-Type: text/plain; charset="utf-8"

Invoice total: $42.00
--XYZ
Content-Type: text/html; charset="utf-8"

<p>Invoice <b>total</b>: $42.00 (HTML version)</p>
--XYZ--
"""

# Only HTML: we should turn it into readable text.
HTML_ONLY = b"""\
From: news@letters.example.org
To: me@example.com
Subject: Weekly news
Date: Mon, 02 Nov 2026 07:00:00 +0000
Message-ID: <html-1@example.com>
Content-Type: text/html; charset="utf-8"

<html><head><style>p {color: red}</style></head><body>
<h1>Top stories</h1><p>Prices &amp; rates are up&nbsp;3%.</p>
<script>alert('x')</script><p>See you next week</p></body></html>
"""

# Encoded subject/sender (non-English), a base64 body, and an attachment.
ENCODED = b"""\
From: =?utf-8?q?Ren=C3=A9e_M=C3=BCller?= <renee@example.de>
To: me@example.com
Subject: =?utf-8?b?R3LDvMOfZSBhdXMgTcO8bmNoZW4g4piV?=
Date: Sun, 01 Nov 2026 18:00:00 +0100
Message-ID: <enc-1@example.de>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="B"

--B
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: base64

U2Now7ZuZSBHcsO8w59lIQ==
--B
Content-Type: application/pdf; name="report.pdf"
Content-Disposition: attachment; filename="report.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQK
--B--
"""

# No Message-ID and no Date: fall back to the server's UID and date.
NO_ID = b"""\
From: someone@example.com
Subject: hello

just a line
"""


def test_plain_email():
    e = parse_email(PLAIN)
    assert e["id"] == "<plain-1@example.com>"
    assert e["from"] == "Alice Example <alice@example.com>"
    assert e["subject"] == "Lunch on Friday?"
    assert e["body"] == "Hi! Are you free for lunch on Friday?\nAlice"
    assert e["date"].startswith("2026-11-0")  # shown in your local time


def test_multipart_prefers_plain_text():
    e = parse_email(MULTIPART)
    assert e["body"] == "Invoice total: $42.00"


def test_html_only_falls_back_to_text():
    body = parse_email(HTML_ONLY)["body"]
    assert "Top stories" in body and "Prices & rates are up 3%." in body
    assert "See you next week" in body
    assert "<p>" not in body and "color: red" not in body and "alert" not in body


def test_encoded_headers_and_base64_body_ignore_attachment():
    e = parse_email(ENCODED)
    assert e["from"] == "Renée Müller <renee@example.de>"
    assert e["subject"] == "Grüße aus München ☕"
    assert e["body"] == "Schöne Grüße!"


def test_flags_uid_and_folder_are_kept():
    e = parse_email(PLAIN, uid=812, folder="INBOX", uidvalidity=7,
                    flags=(b"\\Seen", b"\\Flagged"))
    assert (e["uid"], e["folder"]) == (812, "INBOX")
    assert e["read"] is True and e["starred"] is True
    assert parse_email(PLAIN, flags=())["read"] is False


def test_missing_message_id_and_date_use_server_values():
    e = parse_email(NO_ID, uid=5, folder="INBOX", uidvalidity=9,
                    internal_date=datetime(2026, 11, 1, 9, 0))
    assert e["id"] == "<uid-9-5@INBOX>"
    assert e["date"] == "2026-11-01T09:00:00"
    assert e["to"] == ""


def test_html_to_text_keeps_line_breaks():
    assert html_to_text("<p>one</p><p>two<br>three</p>") == "one\n\ntwo\nthree"


# ---------- sync, with a fake server ----------

class FakeServer:
    """Pretends to be Gmail. Records what we asked for."""

    def __init__(self, messages: dict[int, bytes]):
        self.messages = messages
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.calls.append(("login", user))

    def select_folder(self, folder, readonly=False):
        self.calls.append(("select_folder", folder, readonly))
        return {b"UIDVALIDITY": 77}

    def search(self, criteria):
        self.calls.append(("search", criteria))
        return list(self.messages)

    def has_capability(self, name):
        return name == "X-GM-EXT-1"  # behaves like Gmail

    def fetch(self, uids, items):
        self.calls.append(("fetch", sorted(uids), items))
        return {u: {b"BODY[]": self.messages[u], b"FLAGS": (), b"INTERNALDATE": None,
                    b"X-GM-LABELS": (b"\\Important", "Work"), b"SEQ": u} for u in uids}

    # These would change mail. The sync must never call them.
    def add_flags(self, *a, **k): raise AssertionError("sync tried to change flags")
    def delete_messages(self, *a, **k): raise AssertionError("sync tried to delete")
    def move(self, *a, **k): raise AssertionError("sync tried to move mail")


def _numbered(n):
    """n distinct raw emails with UIDs 101, 102, ... (higher = newer)."""
    return {100 + i: PLAIN.replace(b"plain-1@", f"msg-{i}@".encode())
            .replace(b"Lunch on Friday?", f"Message {i}".encode()) for i in range(1, n + 1)}


@pytest.fixture
def imap_config(monkeypatch, tmp_path):
    monkeypatch.setenv("MAILBOX", "imap")
    monkeypatch.setenv("EMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "not-a-real-password")
    monkeypatch.setenv("SYNC_MAX_EMAILS", "3")
    monkeypatch.setenv("SYNC_DAYS", "7")
    return load_config(tmp_path / "no.env")


@pytest.fixture
def imap_db(tmp_path):
    with closing(database.open_db(tmp_path / "emails-imap.db", load_sample=False)) as c:
        yield c


def test_sync_is_read_only_and_takes_newest(imap_config, imap_db):
    server = FakeServer(_numbered(5))
    result = sync(imap_config, imap_db, connect=lambda cfg: server, today=date(2026, 11, 5))

    assert ("select_folder", "INBOX", True) in server.calls  # readonly=True
    assert ("search", ["SINCE", date(2026, 10, 29)]) in server.calls  # last 7 days
    [fetch] = [c for c in server.calls if c[0] == "fetch"]
    assert fetch[1] == [103, 104, 105]  # the 3 NEWEST, not the oldest
    assert "BODY.PEEK[]" in fetch[2]  # PEEK = don't mark as read
    assert result == {"found": 3, "already_stored": 0, "added": 3,  # newest first
                      "new_ids": ["<msg-5@example.com>", "<msg-4@example.com>",
                                  "<msg-3@example.com>"]}


def test_sync_stores_uid_folder_and_gmail_labels(imap_config, imap_db):
    server = FakeServer(_numbered(1))
    sync(imap_config, imap_db, connect=lambda cfg: server)
    [email] = database.read(imap_db, ["<msg-1@example.com>"])
    assert (email["uid"], email["folder"], email["uidvalidity"]) == (101, "INBOX", 77)
    # Gmail omits the label of the open folder, so \\Inbox is added back
    assert email["labels"] == ["\\Inbox", "\\Important", "Work"]
    [fetch] = [c for c in server.calls if c[0] == "fetch"]
    assert "X-GM-LABELS" in fetch[2]
    assert database.stored_uids(imap_db, "INBOX", 77) == {101}


def test_sync_skips_emails_already_stored(imap_config, imap_db):
    sync(imap_config, imap_db, connect=lambda cfg: FakeServer(_numbered(3)))
    server = FakeServer(_numbered(4))  # one new email arrived
    result = sync(imap_config, imap_db, connect=lambda cfg: server)

    [fetch] = [c for c in server.calls if c[0] == "fetch"]
    assert fetch[1] == [104]  # only downloads the new one
    assert result == {"found": 3, "already_stored": 2, "added": 1,
                      "new_ids": ["<msg-4@example.com>"]}
    assert database.count(imap_db) == 4


def test_only_mail_after_the_first_sync_waits_for_listeners(imap_config, imap_db):
    sync(imap_config, imap_db, connect=lambda cfg: FakeServer(_numbered(3)))
    assert database.pending_for_listeners(imap_db) == []  # old mail: not "new"
    sync(imap_config, imap_db, connect=lambda cfg: FakeServer(_numbered(4)))
    assert [e["id"] for e in database.pending_for_listeners(imap_db)] == ["<msg-4@example.com>"]


def test_sync_with_nothing_new_fetches_nothing(imap_config, imap_db):
    sync(imap_config, imap_db, connect=lambda cfg: FakeServer(_numbered(2)))
    server = FakeServer(_numbered(2))
    assert sync(imap_config, imap_db, connect=lambda cfg: server)["added"] == 0
    assert not [c for c in server.calls if c[0] == "fetch"]


def test_sync_needs_credentials(monkeypatch, tmp_path, imap_db):
    monkeypatch.setenv("MAILBOX", "imap")
    monkeypatch.delenv("EMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("EMAIL_APP_PASSWORD", raising=False)
    cfg = load_config(tmp_path / "no.env")
    with pytest.raises(ValueError, match="EMAIL_ADDRESS"):
        sync(cfg, imap_db, connect=lambda c: FakeServer({}))
    monkeypatch.setenv("EMAIL_ADDRESS", "me@icloud.com")
    with pytest.raises(ValueError, match="EMAIL_APP_PASSWORD.*iCloud Mail"):
        sync(load_config(tmp_path / "no.env"), imap_db, connect=lambda c: FakeServer({}))


def test_synced_emails_are_searchable(imap_config, imap_db):
    sync(imap_config, imap_db, connect=lambda cfg: FakeServer({1: MULTIPART, 2: ENCODED}))
    assert [r["id"] for r in database.search(imap_db, "invoice")] == ["<multi-1@example.com>"]
    assert [r["id"] for r in database.search(imap_db, "from:renee")] == ["<enc-1@example.de>"]


@pytest.mark.live
def test_real_mailbox_sync_is_read_only(tmp_path, monkeypatch):
    """Talks to your real mailbox (free, no Claude). Skipped unless .env has its details."""
    monkeypatch.delenv("MAILBOX")  # use the value from .env
    cfg = load_config()
    if not cfg.can_sign_in:
        pytest.skip("no mailbox details in .env")
    with closing(database.open_db(tmp_path / "real.db", load_sample=False)) as conn:
        result = sync(cfg, conn)
        assert result["found"] <= cfg.sync_max_emails
        assert database.count(conn) == result["added"]


def test_big_sync_downloads_in_batches_newest_first(imap_config, imap_db, monkeypatch):
    import dataclasses

    cfg = dataclasses.replace(imap_config, sync_max_emails=120)
    server = FakeServer(_numbered(120))
    progress = []
    result = sync(cfg, imap_db, connect=lambda c: server, show=progress.append)
    fetches = [c[1] for c in server.calls if c[0] == "fetch"]
    assert [len(f) for f in fetches] == [50, 50, 20]
    assert fetches[0][-1] == 220  # newest batch first
    assert result["added"] == 120 and database.count(imap_db) == 120
    assert progress[-1] == "  ... 120/120 downloaded"


def test_backfill_never_marks_mail_for_listeners(imap_config, imap_db):
    sync(imap_config, imap_db, connect=lambda c: FakeServer(_numbered(2)))  # first sync
    sync(imap_config, imap_db, connect=lambda c: FakeServer(_numbered(3)), pending=False)
    assert database.count(imap_db) == 3
    assert database.pending_for_listeners(imap_db) == []  # old mail, not "new"


# ---------- the HTML version (for showing emails as web pages) ----------

# HTML with a logo attached inside the email (a "cid:" link).
HTML_WITH_LOGO = b"""\
From: shop@example.com
To: me@example.com
Subject: Your order
Date: Mon, 02 Nov 2026 07:00:00 +0000
Message-ID: <logo-1@example.com>
MIME-Version: 1.0
Content-Type: multipart/related; boundary="R"

--R
Content-Type: text/html; charset="utf-8"

<p>Thanks!</p><img src="cid:logo123">
--R
Content-Type: image/png
Content-ID: <logo123>
Content-Transfer-Encoding: base64

iVBORw0KGgo=
--R--
"""


def test_html_version_is_kept_next_to_the_text():
    e = parse_email(MULTIPART)
    assert e["body"] == "Invoice total: $42.00"  # Claude still reads the text
    assert "<b>total</b>" in e["html"]           # the page can show the HTML


def test_plain_email_has_no_html():
    assert parse_email(PLAIN)["html"] is None


def test_pictures_inside_the_email_become_data_links():
    e = parse_email(HTML_WITH_LOGO)
    assert "cid:" not in e["html"]
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in e["html"]


def test_fetch_missing_html_fills_old_emails_read_only(imap_config, imap_db):
    from email_agent.mail.imap_sync import fetch_missing_html

    # Two emails stored before HTML was kept: one has HTML, one is plain text.
    for uid, raw in {201: MULTIPART, 202: PLAIN}.items():
        e = parse_email(raw, uid=uid, folder="INBOX", uidvalidity=77)
        e["html"] = None
        database.add_emails(imap_db, [e])
    server = FakeServer({201: MULTIPART, 202: PLAIN})

    found = fetch_missing_html(imap_config, imap_db, connect=lambda cfg: server)

    assert found == 1
    assert ("select_folder", "INBOX", True) in server.calls  # read-only
    assert all("BODY.PEEK[]" in c[2] for c in server.calls if c[0] == "fetch")
    assert "<b>total</b>" in database.read_html(imap_db, "<multi-1@example.com>")
    assert database.read_html(imap_db, "<plain-1@example.com>") is None
    # Both are now marked as checked, so a second run asks for nothing.
    assert database.uids_without_html(imap_db, "INBOX", 77) == {}
