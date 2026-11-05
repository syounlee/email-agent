"""Security alerts: star them and notify right away. Free (no Claude).

Login codes are left to login_codes.py; this is for "new sign-in",
"security alert", "you shared account data" and similar.
"""

import re

config = {
    "id": "security-alerts",
    "name": "Star and notify on security alerts",
    "enabled": False,  # replaced by important_mail.py (phone alerts)
    "event": "email_received",
}

ALERT_SUBJECT = re.compile(
    r"security alert|new sign-in|sign-in attempt|new login|account data"
    r"|suspicious (sign-in|login|activity)|password (was )?changed"
    r"|보안 ?(알림|경고)|로그인 알림|새로운 기기|비밀번호 변경", re.I)
AD_TAG = re.compile(r"^\s*[(\[](광고|AD)[)\]]|\(advertisement\)", re.I)
CODE_SUBJECT = re.compile(
    r"\b(verification|login|sign-in|one-time|temporary|security)\s+code\b|password reset"
    r"|\d{3}[- ]?\d{3} is your|인증번호|인증 ?코드", re.I)


async def handler(email, context):
    subject = email["subject"]
    if CODE_SUBJECT.search(subject):
        return {"executed": False, "reason": "a login code, not an alert"}
    if AD_TAG.search(subject):
        return {"executed": False, "reason": "an ad, not an alert"}
    if not (ALERT_SUBJECT.search(subject) or "accounts.google.com" in email["from"].lower()):
        return {"executed": False, "reason": "not a security alert"}
    await context.star_email(email["id"])
    await context.notify(f"Security alert from {email['from']}: {subject}")
    return {"executed": True, "reason": "security alert: starred and notified"}
