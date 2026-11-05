"""The mailbox the agent's tools use, backed by the SQLite database.

Two plain functions:
- search_emails(query, limit=10): Gmail-style search, newest first.
- read_email(ids): get full emails by id.

The database file is DATA_DIR/emails.db (see database.py).
"""

from contextlib import closing
from pathlib import Path

from email_agent import database


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
