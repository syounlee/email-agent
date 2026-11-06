"""Tests for changing mail. Demo mode uses the sample mailbox; the Gmail part
uses a fake server, so nothing real is touched and nothing costs money."""

from contextlib import closing

import pytest

from email_agent.mail import database
from email_agent.mail.actions import ActionError, apply, approval_question, new_state
from email_agent.core.config import load_config


@pytest.fixture
def conn(tmp_path):
    with closing(database.open_db(tmp_path / "emails-demo.db")) as c:
        yield c


def get(conn, email_id):
    return database.read(conn, [email_id])[0]


# ---------- each action in demo mode ----------

def test_star_and_unstar(conn):
    assert get(conn, "e001")["starred"] is False
    assert apply("star", "e001", conn=conn) == "Starred 'URGENT: Board numbers needed by 3pm today'."
    assert get(conn, "e001")["starred"] is True
    assert database.search(conn, "is:starred")[0]["id"] == "e001"  # search sees it
    apply("unstar", "e001", conn=conn)
    assert get(conn, "e001")["starred"] is False


def test_mark_read_and_unread(conn):
    assert get(conn, "e002")["read"] is False
    apply("mark_read", "e002", conn=conn)
    assert get(conn, "e002")["read"] is True
    assert "e002" not in [r["id"] for r in database.search(conn, "is:unread", limit=50)]
    apply("mark_unread", "e002", conn=conn)
    assert get(conn, "e002")["read"] is False


def test_archive_removes_inbox_label_only(conn):
    assert get(conn, "e003")["labels"] == ["inbox", "finance"]
    assert apply("archive", "e003", conn=conn).startswith("Archived 'OVERDUE")
    email = get(conn, "e003")
    assert email["labels"] == ["finance"]  # still there, just not in the inbox
    assert database.count(conn) == 15  # nothing deleted


def test_add_and_remove_label(conn):
    apply("add_label", "e004", "Books", conn=conn)
    assert get(conn, "e004")["labels"] == ["inbox", "receipts", "Books"]
    apply("add_label", "e004", "books", conn=conn)  # no duplicate, any case
    assert get(conn, "e004")["labels"] == ["inbox", "receipts", "Books"]
    apply("remove_label", "e004", "BOOKS", conn=conn)
    assert get(conn, "e004")["labels"] == ["inbox", "receipts"]


@pytest.mark.parametrize("label", ["\\Trash", "\\Spam", "[Gmail]/Trash", "Inbox", "  "])
def test_system_labels_are_refused(conn, label):
    with pytest.raises(ActionError):
        apply("add_label", "e004", label, conn=conn)
    assert get(conn, "e004")["labels"] == ["inbox", "receipts"]


def test_unknown_email_and_action(conn):
    with pytest.raises(ActionError, match="No email"):
        apply("star", "nope", conn=conn)
    with pytest.raises(ActionError, match="Unknown action"):
        apply("delete", "e001", conn=conn)


def test_approval_questions(conn):
    assert approval_question("archive", "e001", conn=conn) == \
        "Allow archive of 'URGENT: Board numbers needed by 3pm today'?"
    assert approval_question("add_label", "e004", "Books", conn=conn) == \
        "Allow adding label 'Books' to 'Your receipt for order #BK-55102'?"
    assert approval_question("star", "zzz", conn=conn) == "Allow star of unknown email zzz?"


def test_new_state_does_not_touch_the_original():
    email = {"read": False, "starred": False, "labels": ["inbox"]}
    assert new_state(email, "archive") == {"read": False, "starred": False, "labels": []}
    assert email["labels"] == ["inbox"]


# ---------- Gmail mode, with a fake server ----------

class FakeGmail:
    def __init__(self, uidvalidity=77, present=True, gmail=True, fail=None):
        self.uidvalidity, self.present, self.gmail, self.fail = uidvalidity, present, gmail, fail
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.calls.append(("login", user))

    def select_folder(self, folder, readonly=False):
        self.calls.append(("select_folder", folder, readonly))
        return {b"UIDVALIDITY": self.uidvalidity}

    def fetch(self, uids, items):
        return {u: {b"FLAGS": ()} for u in uids} if self.present else {}

    def has_capability(self, name):
        return self.gmail and name == "X-GM-EXT-1"

    def _record(self, name, *args):
        if self.fail:
            raise self.fail
        self.calls.append((name, *args))

    def add_flags(self, uids, flags): self._record("add_flags", uids, flags)
    def remove_flags(self, uids, flags): self._record("remove_flags", uids, flags)
    def add_gmail_labels(self, uids, labels): self._record("add_gmail_labels", uids, labels)
    def remove_gmail_labels(self, uids, labels): self._record("remove_gmail_labels", uids, labels)


GMAIL_EMAIL = {
    "id": "<g1@mail.example.com>", "from": "a@example.com", "to": "me@example.com",
    "subject": "Gmail test", "date": "2026-11-05T09:00:00", "body": "hi",
    "labels": ["\\Inbox", "Work"], "read": False, "starred": False,
    "folder": "INBOX", "uid": 812, "uidvalidity": 77,
}


@pytest.fixture
def gmail(monkeypatch, tmp_path):
    monkeypatch.setenv("MAILBOX", "imap")
    monkeypatch.setenv("EMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "not-real")
    cfg = load_config(tmp_path / "no.env")
    with closing(database.open_db(tmp_path / "emails-imap.db", load_sample=False)) as c:
        database.add_emails(c, [GMAIL_EMAIL])
        yield cfg, c


@pytest.mark.parametrize("action,label,expected_call", [
    ("star", None, ("add_flags", [812], [b"\\Flagged"])),
    ("unstar", None, ("remove_flags", [812], [b"\\Flagged"])),
    ("mark_read", None, ("add_flags", [812], [b"\\Seen"])),
    ("mark_unread", None, ("remove_flags", [812], [b"\\Seen"])),
    ("archive", None, ("remove_gmail_labels", [812], ["\\Inbox"])),
    ("add_label", "Receipts", ("add_gmail_labels", [812], ["Receipts"])),
    ("remove_label", "Work", ("remove_gmail_labels", [812], ["Work"])),
])
def test_each_action_on_gmail(gmail, action, label, expected_call):
    cfg, conn = gmail
    server = FakeGmail()
    apply(action, GMAIL_EMAIL["id"], label, config=cfg, conn=conn, connect=lambda c: server)
    assert ("select_folder", "INBOX", False) in server.calls  # opened for writing
    assert server.calls[-1] == expected_call


def test_gmail_archive_updates_database_after_server(gmail):
    cfg, conn = gmail
    apply("archive", GMAIL_EMAIL["id"], config=cfg, conn=conn, connect=lambda c: FakeGmail())
    assert get(conn, GMAIL_EMAIL["id"])["labels"] == ["Work"]
    # once archived, it isn't in INBOX any more, so we don't try again
    with pytest.raises(ActionError, match="no longer in your inbox"):
        apply("star", GMAIL_EMAIL["id"], config=cfg, conn=conn, connect=lambda c: FakeGmail())


def test_server_failure_leaves_database_unchanged(gmail):
    cfg, conn = gmail
    server = FakeGmail(fail=OSError("connection reset"))
    with pytest.raises(ActionError, match="Nothing was changed"):
        apply("star", GMAIL_EMAIL["id"], config=cfg, conn=conn, connect=lambda c: server)
    assert get(conn, GMAIL_EMAIL["id"])["starred"] is False


def test_renumbered_folder_is_refused(gmail):
    cfg, conn = gmail
    server = FakeGmail(uidvalidity=99)
    with pytest.raises(ActionError, match="sync again"):
        apply("star", GMAIL_EMAIL["id"], config=cfg, conn=conn, connect=lambda c: server)
    assert not [c for c in server.calls if c[0] == "add_flags"]


def test_email_gone_from_server_is_refused(gmail):
    cfg, conn = gmail
    with pytest.raises(ActionError, match="no longer in this folder"):
        apply("star", GMAIL_EMAIL["id"], config=cfg, conn=conn,
              connect=lambda c: FakeGmail(present=False))


def test_archive_needs_gmail(gmail):
    cfg, conn = gmail
    with pytest.raises(ActionError, match="needs Gmail"):
        apply("archive", GMAIL_EMAIL["id"], config=cfg, conn=conn,
              connect=lambda c: FakeGmail(gmail=False))


def test_other_servers_star_for_real_and_keep_labels_locally(gmail):
    """Outlook, iCloud, Yahoo: no Gmail labels, but an alert's label must not fail."""
    cfg, conn = gmail
    server = FakeGmail(gmail=False)
    message = apply("add_label", GMAIL_EMAIL["id"], "Alerts", config=cfg, conn=conn,
                    connect=lambda c: server)
    assert message.endswith("(this computer only).")
    assert not [c for c in server.calls if c[0] == "add_gmail_labels"]  # server left alone
    assert "Alerts" in get(conn, GMAIL_EMAIL["id"])["labels"]
    apply("star", GMAIL_EMAIL["id"], config=cfg, conn=conn, connect=lambda c: server)
    assert server.calls[-1] == ("add_flags", [812], [b"\\Flagged"])  # a real star
