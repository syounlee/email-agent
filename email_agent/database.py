"""Store emails in SQLite, with a full-text index for fast word search.

- open_db(): open (or create) the database for the current MAILBOX:
  DATA_DIR/emails-demo.db or DATA_DIR/emails-imap.db. The demo one loads
  sample_data/emails.json on first run.
- add_emails() / load_json(): store emails. Emails whose id is already in
  the database are skipped, so loading twice never makes duplicates.
- search(): Gmail-style search, newest first.
- read(): full emails by id.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from email_agent.config import PROJECT_ROOT, load_config

SAMPLE_EMAILS = PROJECT_ROOT / "sample_data" / "emails.json"
SNIPPET_LENGTH = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    num       INTEGER PRIMARY KEY,      -- internal row number, used by the index
    id        TEXT NOT NULL UNIQUE,     -- the email's message id, e.g. 'e003'
    sender    TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject   TEXT NOT NULL,
    date      TEXT NOT NULL,            -- ISO format, so it sorts as text
    body      TEXT NOT NULL,
    labels    TEXT NOT NULL DEFAULT '[]',  -- JSON list
    read      INTEGER NOT NULL DEFAULT 0,
    starred   INTEGER NOT NULL DEFAULT 0,
    -- Where the email lives on the IMAP server (empty for demo emails).
    -- A UID only means something together with its folder and UIDVALIDITY.
    folder      TEXT,
    uid         INTEGER,
    uidvalidity INTEGER
);
CREATE INDEX IF NOT EXISTS emails_by_date ON emails(date);
CREATE INDEX IF NOT EXISTS emails_by_uid ON emails(folder, uidvalidity, uid);

-- Full-text index over subject, sender and body. 'porter' makes
-- 'invoices' match 'invoice'. It reads its text from the emails table.
CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
    subject, sender, body,
    content='emails', content_rowid='num', tokenize='porter unicode61'
);

-- Keep the index in step with the table.
CREATE TRIGGER IF NOT EXISTS emails_ai AFTER INSERT ON emails BEGIN
    INSERT INTO emails_fts(rowid, subject, sender, body)
    VALUES (new.num, new.subject, new.sender, new.body);
END;
CREATE TRIGGER IF NOT EXISTS emails_ad AFTER DELETE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, sender, body)
    VALUES ('delete', old.num, old.subject, old.sender, old.body);
END;
CREATE TRIGGER IF NOT EXISTS emails_au AFTER UPDATE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, sender, body)
    VALUES ('delete', old.num, old.subject, old.sender, old.body);
    INSERT INTO emails_fts(rowid, subject, sender, body)
    VALUES (new.num, new.subject, new.sender, new.body);
END;
"""


# ---------- opening and loading ----------

def default_db_path(mailbox: str | None = None) -> Path:
    """DATA_DIR/emails-demo.db or DATA_DIR/emails-imap.db, so they never mix."""
    config = load_config()
    return config.data_dir / f"emails-{mailbox or config.mailbox}.db"


def open_db(path: Path | None = None, load_sample: bool | None = None) -> sqlite3.Connection:
    """Open the database, creating it if new.

    A new demo database gets the sample emails. A new imap database starts
    empty until you sync.
    """
    if load_sample is None:
        load_sample = load_config().mailbox == "demo"
    path = Path(path or default_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if load_sample and count(conn) == 0:
        load_json(conn, SAMPLE_EMAILS)
    return conn


def add_emails(conn: sqlite3.Connection, emails: list[dict]) -> int:
    """Store emails (dicts like the JSON file); skip ids already stored.

    Returns how many were added.
    """
    before = count(conn)
    conn.executemany(
        """INSERT OR IGNORE INTO emails
           (id, sender, recipient, subject, date, body, labels, read, starred,
            folder, uid, uidvalidity)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (e["id"], e["from"], e["to"], e["subject"], e["date"], e["body"],
             json.dumps(e.get("labels", [])), int(e.get("read", False)),
             int(e.get("starred", False)),
             e.get("folder"), e.get("uid"), e.get("uidvalidity"))
            for e in emails
        ],
    )
    conn.commit()
    return count(conn) - before


def load_json(conn: sqlite3.Connection, path: Path) -> int:
    """Add emails from a JSON file; skip ids already stored. Returns how many were added."""
    with open(path, encoding="utf-8") as f:
        return add_emails(conn, json.load(f))


def stored_uids(conn: sqlite3.Connection, folder: str, uidvalidity: int) -> set[int]:
    """IMAP UIDs already in the database for this folder."""
    rows = conn.execute(
        "SELECT uid FROM emails WHERE folder = ? AND uidvalidity = ? AND uid IS NOT NULL",
        (folder, uidvalidity),
    )
    return {r[0] for r in rows}


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]


def _row_to_email(row: sqlite3.Row) -> dict:
    """A database row as a plain dict, with the same keys as the JSON file."""
    return {
        "id": row["id"],
        "from": row["sender"],
        "to": row["recipient"],
        "subject": row["subject"],
        "date": row["date"],
        "body": row["body"],
        "labels": json.loads(row["labels"]),
        "read": bool(row["read"]),
        "starred": bool(row["starred"]),
        "folder": row["folder"],
        "uid": row["uid"],
        "uidvalidity": row["uidvalidity"],
    }


# ---------- Gmail-style query parsing ----------

@dataclass
class ParsedQuery:
    words: list[str] = field(default_factory=list)          # plain words and "phrases"
    subject_words: list[str] = field(default_factory=list)  # subject:...
    senders: list[str] = field(default_factory=list)        # from:...
    unread: bool | None = None                              # is:unread / is:read
    starred: bool | None = None                             # is:starred
    newer_than: timedelta | None = None                     # newer_than:7d


# Pieces of a query: op:"quoted value" | op:value | "quoted phrase" | word
_TOKEN = re.compile(r'(\w+):"([^"]*)"|(\w+):(\S+)|"([^"]*)"|(\S+)')
_AGE = re.compile(r"(\d+)([dwmy])")
_DAYS_PER_UNIT = {"d": 1, "w": 7, "m": 30, "y": 365}

SEARCH_HELP = (
    'words, "exact phrase", from:name, subject:word, is:unread, is:read, '
    "is:starred, newer_than:7d (d=days, w=weeks, m=months, y=years)"
)


def parse_query(query: str) -> ParsedQuery:
    """Split a Gmail-style query into its parts. Raises ValueError if malformed."""
    parsed = ParsedQuery()
    for m in _TOKEN.finditer(query):
        op = (m.group(1) or m.group(3) or "").lower()
        value = m.group(2) if m.group(1) else m.group(4)
        if op == "from":
            parsed.senders.append(value)
        elif op == "subject":
            parsed.subject_words.append(value)
        elif op == "is":
            flag = value.lower()
            if flag == "unread":
                parsed.unread = True
            elif flag == "read":
                parsed.unread = False
            elif flag == "starred":
                parsed.starred = True
            else:
                raise ValueError(f"Unsupported is:{value}. Supported: {SEARCH_HELP}")
        elif op == "newer_than":
            age = _AGE.fullmatch(value.lower())
            if not age:
                raise ValueError(f"Bad newer_than:{value}. Use e.g. newer_than:7d")
            days = int(age.group(1)) * _DAYS_PER_UNIT[age.group(2)]
            parsed.newer_than = timedelta(days=days)
        elif op:  # something like re:hello -> treat as ordinary text
            parsed.words.append(m.group(0).replace('"', ""))
        else:  # a "quoted phrase" or a plain word
            parsed.words.append(m.group(5) if m.group(5) is not None else m.group(6))
    return parsed


def _fts_phrase(text: str) -> str | None:
    """Quote text for the full-text index; None if it has no letters or digits."""
    if not re.search(r"\w", text):
        return None
    return '"' + text.replace('"', '""') + '"'


# ---------- searching and reading ----------

def search(conn: sqlite3.Connection, query: str, limit: int = 10,
           now: datetime | None = None) -> list[dict]:
    """Gmail-style search. Returns short previews, newest first."""
    parsed = parse_query(query)
    conditions, params = [], []

    # Words, phrases and subject: go through the full-text index.
    match = [p for p in map(_fts_phrase, parsed.words) if p]
    match += [f"subject : {p}" for p in map(_fts_phrase, parsed.subject_words) if p]
    if match:
        conditions.append("num IN (SELECT rowid FROM emails_fts WHERE emails_fts MATCH ?)")
        params.append(" AND ".join(match))

    for sender in parsed.senders:  # part of the address, any case
        conditions.append("instr(lower(sender), lower(?)) > 0")
        params.append(sender)
    if parsed.unread is not None:
        conditions.append("read = ?")
        params.append(0 if parsed.unread else 1)
    if parsed.starred:
        conditions.append("starred = 1")
    if parsed.newer_than is not None:
        cutoff = (now or datetime.now()) - parsed.newer_than
        conditions.append("date >= ?")
        params.append(cutoff.isoformat(timespec="seconds"))

    where = " AND ".join(conditions) or "1"
    rows = conn.execute(
        f"SELECT * FROM emails WHERE {where} ORDER BY date DESC LIMIT ?",
        [*params, limit],
    ).fetchall()

    hint = (parsed.words or parsed.subject_words or [""])[0]
    return [
        {
            "id": r["id"],
            "from": r["sender"],
            "subject": r["subject"],
            "date": r["date"],
            "snippet": _make_snippet(r["body"], hint),
        }
        for r in rows
    ]


def update_state(conn: sqlite3.Connection, email_id: str, *, read: bool,
                 starred: bool, labels: list[str]) -> None:
    """Save an email's read/starred status and labels."""
    conn.execute(
        "UPDATE emails SET read = ?, starred = ?, labels = ? WHERE id = ?",
        (int(read), int(starred), json.dumps(labels), email_id),
    )
    conn.commit()


def read(conn: sqlite3.Connection, ids: list[str]) -> list[dict]:
    """Full emails for the given ids, in the order asked. Unknown ids are skipped."""
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM emails WHERE id IN ({marks})", ids).fetchall()
    by_id = {r["id"]: _row_to_email(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _make_snippet(body: str, word: str) -> str:
    """A short, single-line piece of the body, starting near the first match."""
    flat = " ".join(body.split())
    start = flat.lower().find(word.lower()) if word else -1
    start = max(start - 30, 0) if start >= 0 else 0
    snippet = flat[start : start + SNIPPET_LENGTH]
    if start > 0:
        snippet = "..." + snippet
    if start + SNIPPET_LENGTH < len(flat):
        snippet += "..."
    return snippet
