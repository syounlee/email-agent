"""Login codes: tell me the moment one arrives. Free (no Claude).

The notice names the sender but never shows the code itself, so codes
don't end up in the terminal history or the log.
"""

import re

config = {
    "id": "login-codes",
    "name": "Notify when a login or verification code arrives",
    "enabled": True,
    "event": "email_received",
}

CODE_SUBJECT = re.compile(
    r"\b(verification|login|sign-in|one-time|temporary|security)\s+code\b|password reset"
    r"|\d{3}[- ]?\d{3} is your|인증번호|인증 ?코드", re.I)


def sender_name(email):
    name = email["from"].split("<")[0].strip().strip('"')
    return name or email["from"]


async def handler(email, context):
    if not CODE_SUBJECT.search(email["subject"]):
        return {"executed": False, "reason": "not a login code"}
    await context.notify(f"🔐 Login code from {sender_name(email)} arrived")
    return {"executed": True, "reason": "login code: notified"}
