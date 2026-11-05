"""Copy your newest emails from an IMAP server (e.g. Gmail) into the database.

READ-ONLY by design:
- the folder is opened read-only, and
- messages are fetched with BODY.PEEK[], which does not mark them as read.
Nothing here can change, move, delete or send mail.

    uv run email-sync     # sync now and show a summary

    # one-off: copy in older mail too, WITHOUT running the listeners on it
    uv run email-sync --days 30 --max 2000 --backfill

    # one-off: download the HTML (for the web page) of emails stored earlier
    uv run email-sync --html
"""

import argparse
import base64
import dataclasses

import email
import hashlib
import html
import re
from contextlib import closing
from datetime import date, datetime, timedelta
from email import policy
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from email_agent.mail import database
from email_agent.core.config import Config, load_config

MAX_BODY_CHARS = 20_000  # long newsletters get cut so they don't flood Claude
FETCH_BATCH = 50  # download this many emails per request (and save after each batch)
MAX_HTML_CHARS = 3_000_000  # bigger HTML is not kept (the text version still is)
MAX_INLINE_IMAGES_BYTES = 2_000_000  # pictures attached inside the email (logos etc.)


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


def extract_html(msg) -> str | None:
    """The email's HTML version, for showing it as a web page. None if there is none.

    Pictures attached inside the email ("cid:" links) are put straight into the
    HTML as data: links, so the page can show them without asking any server.
    """
    part = msg.get_body(preferencelist=("html",))
    if part is None:
        return None
    markup = _part_text(part)
    if len(markup) > MAX_HTML_CHARS:
        return None
    budget = MAX_INLINE_IMAGES_BYTES
    for inner in msg.walk():
        cid = (inner.get("Content-ID") or "").strip().strip("<>")
        if not cid or inner.get_content_maintype() != "image" or f"cid:{cid}" not in markup:
            continue
        data = inner.get_payload(decode=True) or b""
        if len(data) > budget:
            continue
        budget -= len(data)
        uri = f"data:{inner.get_content_type()};base64,{base64.b64encode(data).decode()}"
        markup = markup.replace(f"cid:{cid}", uri)
    return markup


def _local_iso(value: datetime) -> str:
    """A date as local time in ISO format, matching the rest of the database."""
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.isoformat(timespec="seconds")


def parse_email(raw: bytes, *, uid: int | None = None, folder: str | None = None,
                uidvalidity: int | None = None, flags=(), internal_date=None,
                gmail_labels=None) -> dict:
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
    if gmail_labels is not None:  # Gmail's own labels, e.g. \\Inbox, \\Important, Work
        labels = [l.decode("utf-8", "replace") if isinstance(l, bytes) else str(l)
                  for l in gmail_labels]
        if folder and folder.upper() == "INBOX" and not any(l.lower() == "\\inbox" for l in labels):
            labels.insert(0, "\\Inbox")  # Gmail leaves out the label of the open folder
    else:
        labels = [folder] if folder else []
    return {
        "id": message_id,
        "from": header("From"),
        "to": header("To"),
        "subject": header("Subject") or "(no subject)",
        "date": _local_iso(when),
        "body": extract_body(msg),
        "html": extract_html(msg),
        "labels": labels,
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


def sync(config: Config | None = None, conn=None, connect=_connect, today: date | None = None,
         pending: bool | None = None, show=lambda message: None) -> dict:
    """Copy the newest emails from the last few days into the database.

    pending: should the listeners treat the added emails as new mail?
      None (normal): yes, except on the very first sync into an empty database.
      False (backfill): no, these are old emails being copied in.
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
            if pending is None:
                # The very first sync fills an empty database: don't treat those
                # (possibly 30) old emails as "new mail" for the listeners.
                pending = database.count(conn) > 0
            # Newest first, so if a long backfill is interrupted, the recent mail is in.
            to_fetch = [u for u in reversed(newest) if u not in already]

            # BODY.PEEK[] reads the whole message WITHOUT setting the \Seen flag.
            items = ["BODY.PEEK[]", "FLAGS", "INTERNALDATE"]
            if to_fetch and server.has_capability("X-GM-EXT-1"):  # Gmail: also its labels
                items.append("X-GM-LABELS")
            new_ids = []
            for start in range(0, len(to_fetch), FETCH_BATCH):
                batch = to_fetch[start:start + FETCH_BATCH]
                emails = []
                for uid, data in server.fetch(batch, items).items():
                    raw = data.get(b"BODY[]")  # the server answers PEEK as BODY[]
                    if raw is None:
                        continue
                    emails.append(parse_email(
                        raw, uid=uid, folder=config.imap_folder, uidvalidity=uidvalidity,
                        flags=data.get(b"FLAGS", ()), internal_date=data.get(b"INTERNALDATE"),
                        gmail_labels=data.get(b"X-GM-LABELS"),
                    ))
                new_ids += database.add_emails(conn, emails, pending=pending)  # saved per batch
                show(f"  ... {min(start + FETCH_BATCH, len(to_fetch))}/{len(to_fetch)} downloaded")
    finally:
        if own_conn:
            conn.close()

    return {"found": len(newest), "already_stored": len(newest) - len(to_fetch),
            "added": len(new_ids), "new_ids": new_ids}


def fetch_missing_html(config: Config | None = None, conn=None, connect=_connect,
                       show=lambda message: None) -> int:
    """Download the HTML of stored emails that were copied in before HTML was kept.

    Read-only like sync(). Emails without an HTML version are stored as ""
    so they are not asked for again. Returns how many emails got HTML.
    """
    config = config or load_config()
    if not (config.email_address and config.email_app_password):
        raise ValueError("Set EMAIL_ADDRESS and EMAIL_APP_PASSWORD in .env for MAILBOX=imap.")
    own_conn = conn is None
    conn = conn or database.open_db(database.default_db_path("imap"), load_sample=False)
    found = 0
    try:
        with connect(config) as server:
            server.login(config.email_address, config.email_app_password)
            info = server.select_folder(config.imap_folder, readonly=True)  # read-only!
            uidvalidity = int(info[b"UIDVALIDITY"])
            missing = database.uids_without_html(conn, config.imap_folder, uidvalidity)
            uids = sorted(missing, reverse=True)  # newest first
            for start in range(0, len(uids), FETCH_BATCH):
                batch = uids[start:start + FETCH_BATCH]
                for uid, data in server.fetch(batch, ["BODY.PEEK[]"]).items():
                    raw = data.get(b"BODY[]")
                    if raw is None or uid not in missing:
                        continue
                    msg = email.message_from_bytes(raw, policy=policy.default)
                    markup = extract_html(msg)
                    found += markup is not None
                    database.set_html(conn, missing[uid], markup if markup is not None else "")
                show(f"  ... {min(start + FETCH_BATCH, len(uids))}/{len(uids)} checked")
    finally:
        if own_conn:
            conn.close()
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy recent Gmail into the database (read-only).")
    parser.add_argument("--days", type=int, help="look back this many days (default: SYNC_DAYS)")
    parser.add_argument("--max", type=int, help="at most this many emails (default: SYNC_MAX_EMAILS)")
    parser.add_argument("--backfill", action="store_true",
                        help="old mail: don't run the listeners on what this copies in")
    parser.add_argument("--html", action="store_true",
                        help="also download the HTML of emails stored before HTML was kept")
    args = parser.parse_args()

    config = load_config()
    config = dataclasses.replace(config, sync_days=args.days or config.sync_days,
                                 sync_max_emails=args.max or config.sync_max_emails)
    print(f"Syncing up to {config.sync_max_emails} emails from the last "
          f"{config.sync_days} days of {config.imap_folder} (read-only)"
          f"{', backfill: listeners will not run on these' if args.backfill else ''}...")
    try:
        result = sync(config, pending=False if args.backfill else None, show=print)
    except Exception as problem:  # login errors etc.; message never includes the password
        print(f"Sync failed: {type(problem).__name__}: {problem}")
        raise SystemExit(1)
    print(f"Found {result['found']}, already stored {result['already_stored']}, "
          f"added {result['added']}.")
    if args.html:
        print("Downloading the HTML version of older stored emails (read-only)...")
        print(f"{fetch_missing_html(config, show=print)} emails have an HTML version.")
    with closing(database.open_db(database.default_db_path("imap"), load_sample=False)) as conn:
        print(f"Database now holds {database.count(conn)} emails.")


if __name__ == "__main__":
    main()
