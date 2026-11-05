"""Work alerts: notify on mail from the platforms I work with. Free (no Claude).

Add or remove senders in WORK_SENDERS; saving the file is enough.
Automated digests (e.g. "Your Job Digest") are skipped.
"""

import re

config = {
    "id": "work-alerts",
    "name": "Notify on mail from my work platforms",
    "enabled": True,
    "event": "email_received",
}

WORK_SENDERS = ("workplatform.example.com", "workplatform2.example.com")
SKIP_SUBJECT = re.compile(r"job digest|newsletter|webinar", re.I)


async def handler(email, context):
    sender = email["from"].lower()
    if not any(domain in sender for domain in WORK_SENDERS):
        return {"executed": False, "reason": "not from a work platform"}
    if SKIP_SUBJECT.search(email["subject"]):
        return {"executed": False, "reason": "automated digest, skipped"}
    await context.notify(f"💼 Work: {email['subject']}  ({email['from']})")
    return {"executed": True, "reason": "work mail: notified"}
