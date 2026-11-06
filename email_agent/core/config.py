"""Load settings from the .env file (or the environment).

Use load_config() to get a Config object. Values in .env never get printed:
the API key is hidden when a Config is shown.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # email_agent/core/config.py -> project

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_BUDGET_USD = 0.50
DEFAULT_MAX_TURNS = 20
DEFAULT_DATA_DIR = "data/"
DEFAULT_MAILBOX = "demo"
MAILBOX_CHOICES = ("demo", "imap")
DEFAULT_IMAP_HOST = "imap.gmail.com"

# The email services this agent knows how to connect to. EMAIL_PROVIDER picks
# one; when it is empty, it is guessed from the end of EMAIL_ADDRESS.
#   name: shown on the page and in messages; host: its IMAP server;
#   oauth: True = sign in through the provider's web page (uv run email-login)
#          instead of an app password.
PROVIDERS = {
    "gmail":   {"name": "Gmail", "host": "imap.gmail.com", "oauth": False,
                "domains": ("gmail.com", "googlemail.com")},
    "outlook": {"name": "Outlook", "host": "outlook.office365.com", "oauth": True,
                "domains": ("outlook.com", "hotmail.com", "live.com", "msn.com")},
    "icloud":  {"name": "iCloud Mail", "host": "imap.mail.me.com", "oauth": False,
                "domains": ("icloud.com", "me.com", "mac.com")},
    "yahoo":   {"name": "Yahoo Mail", "host": "imap.mail.yahoo.com", "oauth": False,
                "domains": ("yahoo.com", "ymail.com", "rocketmail.com")},
    "other":   {"name": "your mail server", "host": "", "oauth": False, "domains": ()},
}


def guess_provider(address: str) -> str:
    """'me@hotmail.com' -> 'outlook'. Unknown domains -> 'other'."""
    domain = address.rsplit("@", 1)[-1].strip().lower()
    for key, info in PROVIDERS.items():
        if domain in info["domains"] or domain.startswith("yahoo.") and key == "yahoo":
            return key
    return "other"
DEFAULT_IMAP_FOLDER = "INBOX"
DEFAULT_SYNC_MAX_EMAILS = 30
DEFAULT_SYNC_DAYS = 7
DEFAULT_PORT = 3000
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
DEFAULT_SUMMARY_HOURS = "8"  # when summaries go out: "8", "8-22" (hourly) or "8,13,18"


@dataclass(frozen=True)
class Config:
    # repr=False keeps the key out of print() and error messages.
    anthropic_api_key: str = field(repr=False)
    model: str
    max_budget_usd: float
    max_turns: int
    data_dir: Path
    # Which mailbox: "demo" (sample emails) or "imap" (your real email).
    mailbox: str = DEFAULT_MAILBOX
    email_address: str = ""
    email_app_password: str = field(default="", repr=False)  # never shown
    provider: str = "gmail"  # a key of PROVIDERS
    imap_host: str = DEFAULT_IMAP_HOST
    imap_folder: str = DEFAULT_IMAP_FOLDER
    sync_max_emails: int = DEFAULT_SYNC_MAX_EMAILS
    sync_days: int = DEFAULT_SYNC_DAYS
    port: int = DEFAULT_PORT  # the local web server (step 9)
    # Phone alerts via ntfy. The topic works like a password: anyone who knows
    # it can read your alerts, so it is never shown. Empty = no phone alerts.
    ntfy_topic: str = field(default="", repr=False)
    ntfy_server: str = DEFAULT_NTFY_SERVER
    summary_hours: tuple = (8,)  # the hours (0-23) a summary of waiting alerts goes out
    # Sender domains whose mail always alerts you (work-alerts). Kept in .env,
    # not in the code, because they say where you work.
    work_senders: tuple = ()
    # Outlook only: the "Application (client) ID" of your own Microsoft app
    # registration, needed for the Microsoft sign-in (see the README).
    outlook_client_id: str = ""

    @property
    def provider_name(self) -> str:
        """'Gmail', 'Outlook', 'iCloud Mail', ..."""
        return PROVIDERS[self.provider]["name"]

    @property
    def uses_oauth(self) -> bool:
        """True: sign in through the provider's web page, not an app password."""
        return PROVIDERS[self.provider]["oauth"]

    @property
    def can_sign_in(self) -> bool:
        """Is there enough in .env to connect to the mailbox?"""
        if not (self.email_address and self.imap_host):
            return False
        return bool(self.outlook_client_id) if self.uses_oauth else bool(self.email_app_password)

    @property
    def summary_schedule(self) -> str:
        """In words, e.g. 'every hour from 8:00 to 22:00'."""
        return describe_hours(self.summary_hours)

    @property
    def has_api_key(self) -> bool:
        return bool(self.anthropic_api_key)


def parse_hours(text: str) -> tuple:
    """'8' -> (8,);  '8-22' -> (8, 9, ..., 22);  '8,13,18' -> (8, 13, 18)."""
    hours = set()
    for part in str(text).replace(" ", "").split(","):
        if not part:
            continue
        first, _, last = part.partition("-")
        start, end = int(first), int(last or first)
        if not (0 <= start <= end <= 23):
            raise ValueError(f"SUMMARY_HOURS: {part!r} is not an hour or range within 0-23")
        hours.update(range(start, end + 1))
    if not hours:
        raise ValueError("SUMMARY_HOURS is empty; use e.g. 8 or 8-22")
    return tuple(sorted(hours))


def parse_list(text: str) -> tuple:
    """'a.com, B.com' -> ('a.com', 'b.com'). Empty text -> ()."""
    return tuple(part.strip().lower() for part in text.split(",") if part.strip())


def describe_hours(hours) -> str:
    hours = tuple(hours)
    if len(hours) == 1:
        return f"at {hours[0]}:00"
    if hours == tuple(range(hours[0], hours[-1] + 1)):
        return f"every hour from {hours[0]}:00 to {hours[-1]}:00"
    return "at " + ", ".join(f"{h}:00" for h in hours)


def load_config(env_file: Path | None = None) -> Config:
    """Read settings from .env (in the project folder) and the environment.

    A setting in the environment wins over .env. Reading .env never changes
    the environment, so one test's settings can't leak into another.
    """
    values = {**dotenv_values(env_file or PROJECT_ROOT / ".env"), **os.environ}

    def getenv(name: str, default: str | None = None) -> str | None:
        value = values.get(name)
        return default if value is None else value

    data_dir = Path(getenv("DATA_DIR") or DEFAULT_DATA_DIR)
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    mailbox = (getenv("MAILBOX") or DEFAULT_MAILBOX).strip().lower()
    if mailbox not in MAILBOX_CHOICES:
        raise ValueError(f"MAILBOX must be one of {MAILBOX_CHOICES}, not {mailbox!r}")

    address = getenv("EMAIL_ADDRESS", "").strip()
    provider = (getenv("EMAIL_PROVIDER") or guess_provider(address) if address else
                getenv("EMAIL_PROVIDER") or "gmail").strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"EMAIL_PROVIDER must be one of {tuple(PROVIDERS)}, not {provider!r}")

    return Config(
        anthropic_api_key=getenv("ANTHROPIC_API_KEY", ""),
        model=getenv("MODEL") or DEFAULT_MODEL,
        max_budget_usd=float(getenv("MAX_BUDGET_USD") or DEFAULT_MAX_BUDGET_USD),
        max_turns=int(getenv("MAX_TURNS") or DEFAULT_MAX_TURNS),
        data_dir=data_dir,
        mailbox=mailbox,
        email_address=getenv("EMAIL_ADDRESS", "").strip(),
        # Google shows app passwords in groups of 4 with spaces; spaces are fine.
        email_app_password=getenv("EMAIL_APP_PASSWORD", "").replace(" ", ""),
        provider=provider,
        imap_host=(getenv("IMAP_HOST") or PROVIDERS[provider]["host"]).strip(),  # "other": set it
        imap_folder=getenv("IMAP_FOLDER") or DEFAULT_IMAP_FOLDER,
        sync_max_emails=int(getenv("SYNC_MAX_EMAILS") or DEFAULT_SYNC_MAX_EMAILS),
        sync_days=int(getenv("SYNC_DAYS") or DEFAULT_SYNC_DAYS),
        port=int(getenv("PORT") or DEFAULT_PORT),
        ntfy_topic=(getenv("NTFY_TOPIC") or "").strip(),
        ntfy_server=(getenv("NTFY_SERVER") or DEFAULT_NTFY_SERVER).rstrip("/"),
        work_senders=parse_list(getenv("WORK_SENDERS") or ""),
        outlook_client_id=(getenv("OUTLOOK_CLIENT_ID") or "").strip(),
        summary_hours=parse_hours(getenv("SUMMARY_HOURS") or getenv("SUMMARY_HOUR")
                                  or DEFAULT_SUMMARY_HOURS),
    )
