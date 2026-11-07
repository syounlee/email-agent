"""Alerts: important emails, sent to your phone.

A listener calls  await context.alert(email_id, kind, title, message, urgent=...)
- urgent=True: pushed to your phone right away,
- urgent=False: saved for the next summary (one push with everything waiting),
  sent at the SUMMARY_HOURS in .env, e.g. every hour from 8:00 to 22:00.
Every alert is also kept in the database. On the web page you mark each one
Done, Not important (the listener learns from these) or Mute sender.

Phone delivery uses ntfy (https://ntfy.sh): a free app that shows messages
sent to a "topic". Set NTFY_TOPIC in .env and subscribe to it in the app.
The topic works like a password, so it is never printed.

    uv run email-alerts            # the newest alerts
    uv run email-alerts test       # send a test message to your phone
    uv run email-alerts summary    # send the summary now
    uv run email-alerts check 200  # check the 200 newest stored emails for alerts
                                   # (for real: stars them, and labels them if the mailbox has labels)
"""

import asyncio
import json
import re
import sys
import urllib.request
from contextlib import closing
from datetime import date, datetime, timedelta

from email_agent.core.config import Config, load_config
from email_agent.mail import database

KINDS = {  # kind -> icon
    "action": "📝",    # something you must do
    "coupon": "🏷️",    # a discount code or credit
    "money": "💳",     # payments, refunds, orders, deliveries, renewals
    "security": "🔐",  # new sign-ins, password changes
    "work": "💼",      # mail from the platforms you work with
    "travel": "✈️",    # e.g. a flight price drop
}
MAX_PUSH_CHARS = 3500  # ntfy allows 4096 bytes per message; stay well under

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    num        INTEGER PRIMARY KEY,
    created    TEXT NOT NULL,
    email_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    message    TEXT NOT NULL,
    urgent     INTEGER NOT NULL,
    pushed     INTEGER NOT NULL DEFAULT 0,  -- 1 = reached the phone right away
    summarized TEXT,                        -- date of the summary it went out in
    status     TEXT NOT NULL DEFAULT 'new', -- new | done | dismissed (not important)
    sender     TEXT NOT NULL DEFAULT '',
    subject    TEXT NOT NULL DEFAULT '',
    seen       TEXT                         -- when you first opened it on the page (NULL = not yet)
);
-- Senders you muted: their mail is never sent to Claude or alerted again.
CREATE TABLE IF NOT EXISTS muted_senders (
    sender  TEXT PRIMARY KEY,               -- the address, lower case
    created TEXT NOT NULL
);
"""
STATUSES = ("new", "done", "dismissed")


def open_alerts(path=None):
    conn = database.open_db(path)
    conn.executescript(SCHEMA)
    have = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
    for column, kind in [("status", "TEXT NOT NULL DEFAULT 'new'"),
                         ("sender", "TEXT NOT NULL DEFAULT ''"),
                         ("subject", "TEXT NOT NULL DEFAULT ''"),
                         ("seen", "TEXT")]:
        if column not in have:  # alerts saved before these existed
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {column} {kind}")
            if column == "seen":  # ones you already handled were clearly seen
                conn.execute("UPDATE alerts SET seen = created WHERE status != 'new'")
    conn.commit()
    return conn


def sender_address(sender: str) -> str:
    """'Shop <news@shop.com>' -> 'news@shop.com'."""
    match = re.search(r"<([^>]+)>", sender)
    return (match.group(1) if match else sender).strip().lower()


# ---------- sending to the phone ----------

def _post(url: str, data: bytes) -> None:
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


def push(title: str, message: str, *, urgent: bool = False, tags=(), config: Config | None = None,
         post=None, show=print) -> bool:
    """Send one message to your phone. False if phone alerts are off or it failed.

    `post` can be swapped in tests, so tests never use the network.
    """
    config = config or load_config()
    if not config.ntfy_topic:
        return False
    body = {
        "topic": config.ntfy_topic,
        "title": title[:200],
        "message": message[:MAX_PUSH_CHARS],
        "priority": 4 if urgent else 3,  # 4 = high: sound and banner
        "tags": list(tags),
    }
    try:
        (post or _post)(config.ntfy_server, json.dumps(body).encode("utf-8"))
        return True
    except Exception as problem:  # no network etc. Never print the topic.
        show(f"⚠️ phone alert failed: {type(problem).__name__}")
        return False


# ---------- remembering alerts ----------

def add_alert(email_id: str, kind: str, title: str, message: str, urgent: bool, pushed: bool,
              path=None) -> None:
    with closing(open_alerts(path)) as conn:
        row = conn.execute("SELECT sender, subject FROM emails WHERE id = ?",
                           (email_id,)).fetchone()
        sender, subject = (row["sender"], row["subject"]) if row else ("", "")
        conn.execute(
            "INSERT INTO alerts (created, email_id, kind, title, message, urgent, pushed, "
            "sender, subject) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), email_id, kind, title, message,
             int(urgent), int(pushed), sender, subject))
        conn.commit()


def recent_alerts(limit: int = 30, status: str | None = None, path=None) -> list[dict]:
    """Newest first. status: 'new', 'done', 'dismissed', or None for all."""
    with closing(open_alerts(path)) as conn:
        if status:
            rows = conn.execute("SELECT * FROM alerts WHERE status = ? ORDER BY num DESC "
                                "LIMIT ?", (status, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM alerts ORDER BY num DESC LIMIT ?",
                                (limit,)).fetchall()
    return [dict(r) for r in rows]


def set_status(num: int, status: str, mute: bool = False, path=None) -> dict | None:
    """Mark an alert done / dismissed / new. mute=True also mutes its sender.

    Returns the updated alert, or None if there is no such alert.
    """
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    with closing(open_alerts(path)) as conn:
        conn.execute("UPDATE alerts SET status = ? WHERE num = ?", (status, num))
        if status != "new":  # handling an alert means you've seen it
            conn.execute("UPDATE alerts SET seen = COALESCE(seen, ?) WHERE num = ?",
                         (_now(), num))
        row = conn.execute("SELECT * FROM alerts WHERE num = ?", (num,)).fetchone()
        if row and mute and row["sender"]:
            conn.execute("INSERT OR IGNORE INTO muted_senders (sender, created) VALUES (?, ?)",
                         (sender_address(row["sender"]),
                          datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    return dict(row) if row else None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def mark_seen(num: int, seen: bool = True, path=None) -> dict | None:
    """Mark an alert as opened (keeps the first time), or as not opened yet.

    Returns the updated alert, or None if there is no such alert.
    """
    with closing(open_alerts(path)) as conn:
        if seen:
            conn.execute("UPDATE alerts SET seen = COALESCE(seen, ?) WHERE num = ?", (_now(), num))
        else:
            conn.execute("UPDATE alerts SET seen = NULL WHERE num = ?", (num,))
        row = conn.execute("SELECT * FROM alerts WHERE num = ?", (num,)).fetchone()
        conn.commit()
    return dict(row) if row else None


def unmute(sender: str, path=None) -> None:
    with closing(open_alerts(path)) as conn:
        conn.execute("DELETE FROM muted_senders WHERE sender = ?", (sender_address(sender),))
        conn.commit()


def feedback(limit: int = 20, path=None) -> dict:
    """What the user taught: muted senders, and alerts they said were not important."""
    with closing(open_alerts(path)) as conn:
        muted = [r["sender"] for r in conn.execute("SELECT sender FROM muted_senders")]
        # Muted senders are handled by the mute itself, so their alerts are no
        # example for Claude (it might wrongly learn "invoices are not important").
        rows = conn.execute("SELECT title, sender FROM alerts WHERE status = 'dismissed' "
                            "ORDER BY num DESC LIMIT ?", (limit * 3,)).fetchall()
        dismissed = [f"{r['title']} (from {sender_address(r['sender']) or 'unknown'})"
                     for r in rows if sender_address(r["sender"]) not in muted][:limit]
    return {"muted": muted, "not_important": dismissed}


def stats(days: int = 7, now: datetime | None = None, path=None) -> dict:
    """How many emails arrived, and how many were worth an alert, in the last `days`."""
    since = ((now or datetime.now()) - timedelta(days=days)).isoformat(timespec="seconds")
    with closing(open_alerts(path)) as conn:
        emails = conn.execute("SELECT COUNT(*) FROM emails WHERE date >= ?", (since,)).fetchone()[0]
        alerted = conn.execute("SELECT COUNT(*) FROM alerts WHERE created >= ?",
                               (since,)).fetchone()[0]
        counts = {s: 0 for s in STATUSES}
        counts.update({r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM alerts GROUP BY status")})
        muted = conn.execute("SELECT COUNT(*) FROM muted_senders").fetchone()[0]
        unseen = conn.execute("SELECT COUNT(*) FROM alerts WHERE status = 'new' "
                              "AND seen IS NULL").fetchone()[0]
    return {"days": days, "emails": emails, "alerts": alerted, "counts": counts, "muted": muted,
            "unseen": unseen}


def why_skip(email_id: str, path=None) -> str | None:
    """'already alerted' (one alert per email), 'sender muted', or None to go ahead."""
    with closing(open_alerts(path)) as conn:
        if conn.execute("SELECT 1 FROM alerts WHERE email_id = ?", (email_id,)).fetchone():
            return "already alerted"
        row = conn.execute("SELECT sender FROM emails WHERE id = ?", (email_id,)).fetchone()
        if row and conn.execute("SELECT 1 FROM muted_senders WHERE sender = ?",
                                (sender_address(row["sender"]),)).fetchone():
            return "sender muted"
    return None


async def send_alert(email_id: str, kind: str, title: str, message: str, urgent: bool,
                     show=print) -> bool | str:
    """What context.alert does: save the alert, and push it now if urgent.

    Returns True if it reached the phone now, False if it waits for the summary,
    or a reason it was skipped: 'already alerted' (one alert per email, e.g. from
    another listener) or 'sender muted' (you pressed Mute; this holds for every
    listener).
    """
    skip = await asyncio.to_thread(why_skip, email_id)
    if skip:
        return skip
    icon = KINDS.get(kind, "🔔")
    pushed = False
    if urgent:
        pushed = await asyncio.to_thread(push, f"{icon} {title}", message, urgent=True,
                                         show=show)
    await asyncio.to_thread(add_alert, email_id, kind, title, message, urgent, pushed)
    return pushed


# ---------- the summary of waiting alerts ----------

def summary_text(items: list[dict]) -> tuple[str, str]:
    """Title and text of the summary push: one line per alert, grouped by kind."""
    title = f"📬 {len(items)} thing{'s' if len(items) != 1 else ''} worth knowing"
    lines = []
    for kind in [*KINDS, "other"]:
        group = [i for i in items if (i["kind"] if i["kind"] in KINDS else "other") == kind]
        for item in group:
            lines.append(f"{KINDS.get(kind, '🔔')} {item['title']}: {item['message']}")
    text = "\n".join(lines)
    if len(text) > MAX_PUSH_CHARS:
        text = text[:MAX_PUSH_CHARS - 40].rsplit("\n", 1)[0] + "\n… and more on the web page"
    return title, text


def _last_summary_file(config: Config):
    return config.data_dir / f"summary-{config.mailbox}.last"


def send_summary(config: Config | None = None, today: date | None = None, post=None,
                 show=print, path=None) -> int:
    """Push one message with every alert not sent yet. Returns how many it covered."""
    config = config or load_config()
    today = today or date.today()
    with closing(open_alerts(path)) as conn:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM alerts WHERE pushed = 0 AND summarized IS NULL "
            "AND status = 'new' ORDER BY num")]
        if not items:
            return 0
        title, text = summary_text(items)
        if not push(title, text, tags=["email"], config=config, post=post, show=show):
            return 0  # phone alerts off or no network: keep them for next time
        conn.executemany("UPDATE alerts SET summarized = ? WHERE num = ?",
                         [(today.isoformat(), i["num"]) for i in items])
        conn.commit()
    show(f"📬 summary sent ({len(items)} alerts)")
    return len(items)


def current_slot(config: Config, now: datetime) -> str | None:
    """The latest scheduled summary hour that has started today, e.g. '2026-11-06T14'.

    None when nothing should go out now: before the first hour of the day, and,
    with several hours (e.g. 8-22), after the last one: the night stays quiet.
    With one hour a day (e.g. 8), starting the agent later still sends it.
    """
    hours = config.summary_hours
    if len(hours) > 1 and now.hour > max(hours):
        return None
    started = [h for h in hours if h <= now.hour]
    return f"{now.date().isoformat()}T{max(started):02}" if started else None


def summary_due(config: Config, now: datetime) -> bool:
    """Has a scheduled hour started for which no summary went out yet?"""
    slot = current_slot(config, now)
    last = _last_summary_file(config)
    return slot is not None and not (last.exists() and last.read_text().strip() == slot)


def maybe_send_summary(config: Config | None = None, now: datetime | None = None, post=None,
                       show=print) -> int:
    """Send a summary once per scheduled hour (if anything is waiting).

    Called every minute while email-watch or email-agent runs. An hour with
    nothing waiting sends nothing.
    """
    config = config or load_config()
    now = now or datetime.now()
    if not config.ntfy_topic or not summary_due(config, now):
        return 0
    count = send_summary(config, now.date(), post=post, show=show)
    _last_summary_file(config).parent.mkdir(parents=True, exist_ok=True)
    _last_summary_file(config).write_text(current_slot(config, now))
    return count


async def summary_loop(first=None, every_seconds: int = 60, show=print) -> None:
    """Background task: wait for the first catch-up (`first`), then check the clock."""
    if first is not None:
        try:
            await first
        except BaseException:
            pass  # a failed catch-up still deserves a summary
    while True:
        try:
            await asyncio.to_thread(maybe_send_summary, show=show)
        except Exception as problem:
            show(f"summary failed: {type(problem).__name__}: {problem}")
        await asyncio.sleep(every_seconds)


# ---------- checking mail that is already stored ----------

LISTENER_ID = "important-mail"


async def check_stored(count: int = 200, show=print, claude_client=None) -> int:
    """Run important-mail for real on the newest stored emails it hasn't seen yet.

    For mail copied in before the listener existed. Returns how many it checked.
    """
    from email_agent.automation.listeners import ListenerRegistry, read_log, run_listeners

    module = next((m for m in ListenerRegistry(show=show).enabled()
                   if m.config["id"] == LISTENER_ID), None)
    if module is None:
        raise RuntimeError(f"the {LISTENER_ID} listener is missing or switched off")
    seen = {e["email_id"] for e in read_log(LISTENER_ID, limit=1_000_000)}
    with closing(database.open_db()) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM emails ORDER BY date DESC LIMIT ?", (count,))]
        emails = [e for e in database.read(conn, ids) if e["id"] not in seen]

    class OnlyThisListener:
        def enabled(self, event="email_received"):
            return [module]

    for email in reversed(emails):  # oldest first, like real arrivals
        await run_listeners(email, OnlyThisListener(), show, claude_client)
    return len(emails)


# ---------- the email-alerts command ----------

def main() -> None:
    args = sys.argv[1:]
    config = load_config()
    if args[:1] == ["test"]:
        if not config.ntfy_topic:
            sys.exit("Set NTFY_TOPIC in .env first (see .env.example).")
        ok = push("✅ Email agent", "Phone alerts work. Important emails will show up here.",
                  config=config)
        print("Sent. Check your phone." if ok else "Could not send (see the message above).")
    elif args[:1] == ["summary"]:
        count = send_summary(config)
        print(f"Summary sent with {count} alerts." if count else
              "Nothing to send (no new alerts, or phone alerts are off).")
    elif args[:1] == ["check"]:
        count = int(args[1]) if len(args) > 1 else 200
        print(f"Checking up to {count} stored emails for alerts (this stars and labels "
              "the important ones in your mailbox)...")
        checked = asyncio.run(check_stored(count, show=lambda m: None))
        print(f"Checked {checked} emails (others were checked before). New alerts:")
        for a in reversed(recent_alerts(limit=50, status="new")):
            print(f"  {KINDS.get(a['kind'], '🔔')} {a['title']}: {a['message']}")
    elif not args:
        alerts = recent_alerts()
        if not alerts:
            print("No alerts yet.")
        for a in reversed(alerts):
            how = "📱 now" if a["pushed"] else (f"📬 {a['summarized']}" if a["summarized"]
                                                else "⏳ next summary")
            print(f"{a['created'][:16]}  {KINDS.get(a['kind'], '🔔')} {a['title']}: "
                  f"{a['message']}  [{how}]")
    else:
        sys.exit("Usage: uv run email-alerts [test | summary | check [N]]")


if __name__ == "__main__":
    main()
