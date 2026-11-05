"""The mailbox the agent's tools use, backed by the SQLite database.

Two plain functions:
- search_emails(query, limit=10): Gmail-style search, newest first.
- read_email(ids): get full emails by id.

The database file is DATA_DIR/emails-demo.db or emails-imap.db, depending
on MAILBOX (see database.py).
"""

from contextlib import closing
from pathlib import Path

from email_agent.mail import database
from email_agent.core.config import Config, load_config


def prepare(config: Config | None = None, show=print) -> None:
    """Before chatting: with MAILBOX=imap, copy your newest emails in (read-only)."""
    config = config or load_config()
    if config.mailbox != "imap":
        show("Mailbox: demo (15 sample emails).")
        return
    from email_agent.mail.imap_sync import sync

    show(f"Mailbox: {config.email_address} via IMAP (read-only). Syncing...")
    try:
        result = sync(config)
        show(f"Synced: {result['found']} recent emails, {result['added']} new.")
    except Exception as problem:  # never includes the password
        show(f"Sync failed ({type(problem).__name__}: {problem}). "
             "Using emails already stored.")


def search_emails(query: str, limit: int = 10, db_path: Path | None = None) -> list[dict]:
    """Find emails matching a Gmail-style query, e.g. 'invoice from:billing is:unread'.

    Each result is short: id, from, subject, date and a snippet of the body.
    An empty query returns the newest emails. Raises ValueError for a bad query.
    """
    with closing(database.open_db(db_path)) as conn:
        return database.search(conn, query, limit)


def read_email(ids: str | list[str], db_path: Path | None = None) -> list[dict]:
    """Return the full emails for one id or a list of ids, in the order asked.

    Ids that do not exist are skipped.
    """
    if isinstance(ids, str):
        ids = [ids]
    with closing(database.open_db(db_path)) as conn:
        return database.read(conn, ids)
