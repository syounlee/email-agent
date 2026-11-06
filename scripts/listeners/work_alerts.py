"""Work alerts: alert your phone about mail from the platforms I work with. Free (no Claude).

The senders are the WORK_SENDERS setting in .env (comma-separated domains, e.g.
WORK_SENDERS=mycompany.com,jobs.example.org). They live in .env, not here, because
they say where you work. Edits to .env apply to the next email, no restart needed.
Automated digests (e.g. "Your Job Digest") are skipped.
"""

import re

from email_agent.core.config import load_config

config = {
    "id": "work-alerts",
    "name": "Notify on mail from my work platforms",
    "enabled": True,
    "event": "email_received",
}

SKIP_SUBJECT = re.compile(r"job digest|newsletter|webinar", re.I)


async def handler(email, context):
    sender = email["from"].lower()
    work_senders = load_config().work_senders
    if not work_senders or not any(domain in sender for domain in work_senders):
        return {"executed": False, "reason": "not from a work platform"}
    if SKIP_SUBJECT.search(email["subject"]):
        return {"executed": False, "reason": "automated digest, skipped"}
    await context.alert(email["id"], "work", f"Work: {email['subject']}"[:80],
                        f"From {email['from']}", urgent=True)
    return {"executed": True, "reason": "work mail: alerted"}
