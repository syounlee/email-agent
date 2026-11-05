"""Run the listeners on new mail.

    uv run python -m email_agent.listen                 # MAILBOX=imap: watch Gmail
    uv run python -m email_agent.listen demo receipt    # MAILBOX=demo: fake an arrival

Demo emails you can deliver: receipt, invoice, overdue, newsletter.
Press Ctrl+C to stop watching.
"""

import asyncio
import sys
from datetime import date, timedelta

from email_agent.config import load_config
from email_agent.idle_watcher import start_watcher
from email_agent.imap_sync import sync
from email_agent.listeners import ListenerRegistry, deliver_demo_email, log_path, process_pending

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
        if args[:1] == ["demo"]:
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
