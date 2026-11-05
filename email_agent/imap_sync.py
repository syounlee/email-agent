"""Copy your newest emails from an IMAP server (e.g. Gmail) into the database.

READ-ONLY by design:
- the folder is opened read-only, and
- messages are fetched with BODY.PEEK[], which does not mark them as read.
Nothing here can change, move, delete or send mail.

    uv run python -m email_agent.imap_sync     # sync now and show a summary
"""

import email
import hashlib
import html
import re
from contextlib import closing
from datetime import date, datetime, timedelta
from email import policy
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from email_agent import database
from email_agent.config import Config, load_config

MAX_BODY_CHARS = 20_000  # long newsletters get cut so they don't flood Claude


# ---------- parsing one raw email (offline, built-in `email` package) ----------

class _HTMLToText(HTMLParser):
    """Very small HTML-to-text converter: keeps text, drops scripts and styles."""

    BREAKS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _HTMLToText()
    parser.feed(markup)
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")  # &nbsp;
    text = re.sub(r"[ \t\r\f\v]+", " ", text)          # squeeze spaces
    text = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", text)  # at most one blank line
    return text.strip()


def _part_text(part) -> str:
    """Decoded text of one MIME part, even if its charset label is wrong."""
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, KeyError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def extract_body(msg) -> str:
    """Plain-text body; if the email only has HTML, convert that to text."""
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        text = _part_text(part)
    else:
        part = msg.get_body(preferencelist=("html",))
        text = html_to_text(_part_text(part)) if part is not None else ""
    text = text.replace("\r\n", "\n").strip()
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + "\n[... email cut here, too long ...]"
    return text


def _local_iso(value: datetime) -> str:
    """A date as local time in ISO format, matching the rest of the database."""
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.isoformat(timespec="seconds")


def parse_email(raw: bytes, *, uid: int | None = None, folder: str | None = None,
                uidvalidity: int | None = None, flags=(), internal_date=None) -> dict:
    """Turn raw email bytes into a dict shaped like the sample emails."""
    msg = email.message_from_bytes(raw, policy=policy.default)

    def header(name: str) -> str:
        try:
            return " ".join(str(msg.get(name, "")).split())
        except (ValueError, TypeError):  # badly encoded header
            return ""

    when = None
    try:
        when = parsedate_to_datetime(header("Date")) if header("Date") else None
    except (TypeError, ValueError, IndexError):
        pass
    when = when or internal_date or datetime.now()

    # Prefer the Message-ID header as the key. Without one, use the server's
    # UID, or as a last resort a fingerprint of the raw bytes.
    message_id = header("Message-ID")
    if not message_id:
        message_id = (f"<uid-{uidvalidity}-{uid}@{folder}>" if uid is not None
                      else f"<sha1-{hashlib.sha1(raw).hexdigest()}>")

    flags = {f.decode() if isinstance(f, bytes) else str(f) for f in flags}
    return {
        "id": message_id,
        "from": header("From"),
        "to": header("To"),
        "subject": header("Subject") or "(no subject)",
        "date": _local_iso(when),
        "body": extract_body(msg),
        "labels": [folder] if folder else [],
        "read": "\\Seen" in flags,
        "starred": "\\Flagged" in flags,
        "folder": folder,
        "uid": uid,
        "uidvalidity": uidvalidity,
    }


# ---------- syncing from the server ----------

def _connect(config: Config):
    from imapclient import IMAPClient  # imported here so offline tests don't need a server

    return IMAPClient(config.imap_host, ssl=True, timeout=30)


def sync(config: Config | None = None, conn=None, connect=_connect, today: date | None = None) -> dict:
    """Copy the newest emails from the last few days into the database.

    Returns a summary: how many were found, already stored, and added.
    `connect` and `today` can be swapped in tests.
    """
    config = config or load_config()
    if not (config.email_address and config.email_app_password):
        raise ValueError("Set EMAIL_ADDRESS and EMAIL_APP_PASSWORD in .env for MAILBOX=imap.")

    own_conn = conn is None
    conn = conn or database.open_db(database.default_db_path("imap"), load_sample=False)
    try:
        with connect(config) as server:
            server.login(config.email_address, config.email_app_password)
            info = server.select_folder(config.imap_folder, readonly=True)  # read-only!
            uidvalidity = int(info[b"UIDVALIDITY"])

            since = (today or date.today()) - timedelta(days=config.sync_days)
            uids = server.search(["SINCE", since])
            # UIDs grow as mail arrives, so the largest UIDs are the newest emails.
            newest = sorted(uids)[-config.sync_max_emails:] if config.sync_max_emails else []

            already = database.stored_uids(conn, config.imap_folder, uidvalidity)
            to_fetch = [u for u in newest if u not in already]

            emails = []
            if to_fetch:
                # BODY.PEEK[] reads the whole message WITHOUT setting the \Seen flag.
                fetched = server.fetch(to_fetch, ["BODY.PEEK[]", "FLAGS", "INTERNALDATE"])
                for uid, data in fetched.items():
                    raw = data.get(b"BODY[]")  # the server answers PEEK as BODY[]
                    if raw is None:
                        continue
                    emails.append(parse_email(
                        raw, uid=uid, folder=config.imap_folder, uidvalidity=uidvalidity,
                        flags=data.get(b"FLAGS", ()), internal_date=data.get(b"INTERNALDATE"),
                    ))
            added = database.add_emails(conn, emails)
    finally:
        if own_conn:
            conn.close()

    return {"found": len(newest), "already_stored": len(newest) - len(to_fetch), "added": added}


def main() -> None:
    config = load_config()
    print(f"Syncing up to {config.sync_max_emails} emails from the last "
          f"{config.sync_days} days of {config.imap_folder} (read-only)...")
    try:
        result = sync(config)
    except Exception as problem:  # login errors etc.; message never includes the password
        print(f"Sync failed: {type(problem).__name__}: {problem}")
        raise SystemExit(1)
    print(f"Found {result['found']}, already stored {result['already_stored']}, "
          f"added {result['added']}.")
    with closing(database.open_db(database.default_db_path("imap"), load_sample=False)) as conn:
        print(f"Database now holds {database.count(conn)} emails.")


if __name__ == "__main__":
    main()
