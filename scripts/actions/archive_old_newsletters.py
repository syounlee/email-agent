"""Example action: archive newsletters older than N days.

    /run archive-old-newsletters                     (30 days)
    /run archive-old-newsletters days=7 dry_run=true (only list what it would do)

"Newsletter" here means: the email contains the word "unsubscribe". That is
almost always true for newsletters, but some shops add it to receipts too,
so try dry_run=true first.
"""

config = {
    "id": "archive-old-newsletters",
    "name": "Archive newsletters older than N days",
    "parameters": {
        "days": {"type": "integer", "default": 30, "description": "Only emails older than this."},
        "dry_run": {"type": "boolean", "default": False,
                    "description": "Only list what would be archived."},
    },
}


def _in_inbox(email):
    return any(label.lstrip("\\").lower() == "inbox" for label in email["labels"])


async def handler(params, context):
    found = await context.search_emails(f"unsubscribe older_than:{params['days']}d", limit=500)
    emails = await context.read_email([r["id"] for r in found])
    to_archive = [e for e in emails if _in_inbox(e)]

    if not to_archive:
        return {"executed": False, "reason": f"no newsletters older than {params['days']} days in the inbox"}
    subjects = "; ".join(e["subject"] for e in to_archive[:5])
    if params["dry_run"]:
        return {"executed": False,
                "reason": f"dry run: would archive {len(to_archive)} newsletter(s): {subjects}"}

    for email in to_archive:
        await context.archive_email(email["id"])
    return {"executed": True, "reason": f"archived {len(to_archive)} newsletter(s): {subjects}"}
