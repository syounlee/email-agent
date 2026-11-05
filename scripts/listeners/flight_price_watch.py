"""Flight price watch: notify when a tracked Google Flights price is low. Free.

Google Flights emails look like:
    "Your tracked route: Seoul to Tokyo flights from ₩195k"
Change MAX_PRICE_WON below to your own target; saving the file is enough.
"""

import re

config = {
    "id": "flight-price-watch",
    "name": "Notify when a tracked flight price drops below my target",
    "enabled": True,
    "event": "email_received",
}

MAX_PRICE_WON = 220_000   # notify at or below this price
ROUTE_WORDS = ()          # e.g. ("Tokyo",) to watch one route only; empty = all routes

PRICE = re.compile(r"from\s*₩\s*([\d,.]+)\s*(k|천|만)?", re.I)


def price_in_won(subject):
    """'from ₩195k' -> 195000, 'from ₩195,000' -> 195000; None if no price."""
    match = PRICE.search(subject)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    unit = (match.group(2) or "").lower()
    return round(number * {"k": 1_000, "천": 1_000, "만": 10_000}.get(unit, 1))


async def handler(email, context):
    subject = email["subject"]
    if "tracked route" not in subject.lower() and "noreply-travel@google.com" not in email["from"]:
        return {"executed": False, "reason": "not a flight price alert"}
    if ROUTE_WORDS and not any(w.lower() in subject.lower() for w in ROUTE_WORDS):
        return {"executed": False, "reason": "a route I'm not watching"}
    price = price_in_won(subject)
    if price is None:
        return {"executed": False, "reason": "no price found in the subject"}
    if price > MAX_PRICE_WON:
        return {"executed": False, "reason": f"₩{price:,} is above ₩{MAX_PRICE_WON:,}"}
    await context.star_email(email["id"])
    await context.notify(f"✈️ Price drop: {subject}  (target ₩{MAX_PRICE_WON:,})")
    return {"executed": True, "reason": f"₩{price:,} ≤ ₩{MAX_PRICE_WON:,}: starred and notified"}
