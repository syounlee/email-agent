"""A tiny read-only mailbox backed by a JSON file.

Two plain functions:
- search_emails(query, limit=10): find emails by words, newest first.
- read_email(ids): get full emails by id.
"""

import json
from pathlib import Path

from email_agent.config import PROJECT_ROOT

DEFAULT_MAILBOX = PROJECT_ROOT / "sample_data" / "emails.json"
SNIPPET_LENGTH = 120


def load_emails(path: Path = DEFAULT_MAILBOX) -> list[dict]:
    """Read every email from the JSON file."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _make_snippet(body: str, word: str) -> str:
    """A short, single-line piece of the body, starting near the first match."""
    flat = " ".join(body.split())  # collapse newlines and extra spaces
    start = flat.lower().find(word) if word else -1
    start = max(start - 30, 0) if start >= 0 else 0
    snippet = flat[start : start + SNIPPET_LENGTH]
    if start > 0:
        snippet = "..." + snippet
    if start + SNIPPET_LENGTH < len(flat):
        snippet += "..."
    return snippet


def search_emails(query: str, limit: int = 10, path: Path = DEFAULT_MAILBOX) -> list[dict]:
    """Find emails whose subject, sender or body contain every word in `query`.

    Matching ignores upper/lower case. Results are newest first, at most `limit`.
    Each result is short: id, from, subject, date and a snippet of the body.
    An empty query returns the newest emails.
    """
    words = query.lower().split()
    matches = []
    for email in load_emails(path):
        text = f"{email['subject']} {email['from']} {email['body']}".lower()
        if all(word in text for word in words):
            matches.append(email)

    matches.sort(key=lambda e: e["date"], reverse=True)  # ISO dates sort as text

    return [
        {
            "id": e["id"],
            "from": e["from"],
            "subject": e["subject"],
            "date": e["date"],
            "snippet": _make_snippet(e["body"], words[0] if words else ""),
        }
        for e in matches[:limit]
    ]


def read_email(ids: str | list[str], path: Path = DEFAULT_MAILBOX) -> list[dict]:
    """Return the full emails for one id or a list of ids, in the order asked.

    Ids that do not exist are skipped.
    """
    if isinstance(ids, str):
        ids = [ids]
    by_id = {e["id"]: e for e in load_emails(path)}
    return [by_id[i] for i in ids if i in by_id]
