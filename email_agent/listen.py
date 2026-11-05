"""Run the listeners on new mail.

    uv run python -m email_agent.listen                 # MAILBOX=imap: watch Gmail
    uv run python -m email_agent.listen demo receipt    # MAILBOX=demo: fake an arrival
    uv run python -m email_agent.listen preview 100     # try the listeners on the 100
                                                        # newest stored emails, changing nothing

Demo emails you can deliver: receipt, invoice, overdue, newsletter, bug.
Press Ctrl+C to stop watching.
"""

import asyncio
import sys
from datetime import date, timedelta

from email_agent.config import load_config
from email_agent.idle_watcher import start_watcher
from email_agent.imap_sync import sync
from collections import Counter
from contextlib import closing

from email_agent import database
from email_agent.listeners import (
    ListenerRegistry, deliver_demo_email, log_path, process_pending, run_listeners,
)

_today = date.today()
DEMO_EMAILS = {
    "receipt": dict(
        sender="receipts@pharmacy.example.com",
        subject="Your receipt from Example Pharmacy",
        body="Thank you for shopping with us.\n\n1 x Vitamin D ... $12.99\n"
             "1 x Hand cream ... $6.50\nTotal paid: $19.49 (card ending 0000)",
    ),
    "invoice": dict(
        sender="billing@printshop.example.com",
        subject="Invoice PS-118 for business cards",
        body=f"Hello,\n\nInvoice PS-118: 500 business cards, $85.00.\n"
             f"Due date: {_today + timedelta(days=14)}\n\nThank you!",
    ),
    "overdue": dict(
        sender="accounts@lawnservice.example.net",
        subject="Payment reminder: invoice LS-77",
        body=f"Dear customer,\n\nInvoice LS-77 for $240.00 (October lawn care) was due on "
             f"{_today - timedelta(days=21)} and remains unpaid. Please pay as soon as possible.",
    ),
    "bug": dict(
        sender="qa.tester@example.com",
        subject="Bug report: export button does nothing",
        body="Hi,\n\nFound a bug: on the Reports page, clicking Export does nothing in "
             "Safari. Expected: a CSV download. Severity: medium.\n\nThanks,\nQA",
    ),
    "newsletter": dict(
        sender="news@gardening.example.org",
        subject="Gardening Weekly: bulbs to plant now",
        body="This week: five bulbs to plant before the frost, and a reader Q&A on composting.",
    ),
}


async def demo(kind: str) -> None:
    email_id, entries = await deliver_demo_email(**DEMO_EMAILS[kind])
    total = sum(e["cost_usd"] for e in entries)
    print(f"\nDelivered {email_id}. {len(entries)} listener run(s), Claude cost ${total:.5f}.")
    print(f"Log: {log_path()}")


async def preview(count: int, show=print, claude_client=None) -> dict:
    """Run the enabled listeners on the newest stored emails WITHOUT changing anything.

    Claude calls are real, so you see real decisions and the real cost.
    """
    with closing(database.open_db()) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM emails ORDER BY date DESC LIMIT ?", (count,))]
        emails = database.read(conn, ids)
    registry = ListenerRegistry(show=show)
    did, cost, flagged = Counter(), 0.0, []
    for email in reversed(emails):  # oldest first, like real arrivals
        entries = await run_listeners(email, registry, show, claude_client, preview=True)
        cost += sum(e["cost_usd"] for e in entries)
        changes = [a for e in entries for a in e["actions"]]
        for e in entries:
            if "error" in e:
                show(f"   ❌ {e['listener']} failed on '{e['subject'][:50]}': {e['error']}")
        did.update(a for a in changes if a.startswith("would add label"))
        if any("notify" in a or "star" in a for a in changes):
            flagged.append((email["date"][:10], [a for a in changes if "notify" in a] or changes))
    show(f"\nPreview of {len(emails)} emails (nothing was changed):")
    for label, n in did.most_common():
        show(f"  {n:4}  {label}")
    show(f"\n  {len(flagged)} emails would be starred or notified:")
    for day, notes in flagged:
        show(f"    {day}  {notes[0].replace('would notify: ', '🔔 ')[:110]}")
    show(f"\n  Claude cost of this preview: ${cost:.4f} "
         f"(${cost / max(len(emails), 1) * 1000:.2f} per 1,000 emails)")
    return {"labels": dict(did), "flagged": len(flagged), "cost_usd": cost}


async def watch_gmail() -> None:
    config = load_config()
    registry = ListenerRegistry()
    loop = asyncio.get_running_loop()

    def sync_and_run():
        """Called in the watcher thread when Gmail reports new mail."""
        try:
            result = sync(config)
            print(f"📬 synced: {result['added']} new")
        except Exception as problem:
            print(f"⚠️  sync failed: {type(problem).__name__}: {problem}")
        # run the listeners on the main event loop, and wait so emails stay in order
        asyncio.run_coroutine_threadsafe(process_pending(registry), loop).result()

    # Catch up first: anything synced (e.g. by the chat) but not yet seen.
    await asyncio.to_thread(sync_and_run)
    thread, stop = start_watcher(config, sync_and_run)
    try:
        while thread.is_alive():
            await asyncio.sleep(1)
    finally:
        stop.set()


def main() -> None:
    config = load_config()
    args = sys.argv[1:]
    try:
        if args[:1] == ["preview"]:
            asyncio.run(preview(int(args[1]) if len(args) > 1 else 50))
        elif args[:1] == ["demo"]:
            if config.mailbox != "demo":
                sys.exit("Demo delivery needs MAILBOX=demo, e.g.  MAILBOX=demo uv run python "
                         "-m email_agent.listen demo receipt")
            kind = args[1] if len(args) > 1 else ""
            if kind not in DEMO_EMAILS:
                sys.exit(f"Choose one of: {', '.join(DEMO_EMAILS)}")
            asyncio.run(demo(kind))
        elif config.mailbox == "imap":
            asyncio.run(watch_gmail())
        else:
            print(__doc__)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
