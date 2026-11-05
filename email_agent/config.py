"""Load settings from the .env file (or the environment).

Use load_config() to get a Config object. Values in .env never get printed:
the API key is hidden when a Config is shown.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_BUDGET_USD = 0.50
DEFAULT_MAX_TURNS = 20
DEFAULT_DATA_DIR = "data/"
DEFAULT_MAILBOX = "demo"
MAILBOX_CHOICES = ("demo", "imap")
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_FOLDER = "INBOX"
DEFAULT_SYNC_MAX_EMAILS = 30
DEFAULT_SYNC_DAYS = 7
DEFAULT_PORT = 3000


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
    imap_host: str = DEFAULT_IMAP_HOST
    imap_folder: str = DEFAULT_IMAP_FOLDER
    sync_max_emails: int = DEFAULT_SYNC_MAX_EMAILS
    sync_days: int = DEFAULT_SYNC_DAYS
    port: int = DEFAULT_PORT  # the local web server (step 9)

    @property
    def has_api_key(self) -> bool:
        return bool(self.anthropic_api_key)


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
        imap_host=getenv("IMAP_HOST") or DEFAULT_IMAP_HOST,
        imap_folder=getenv("IMAP_FOLDER") or DEFAULT_IMAP_FOLDER,
        sync_max_emails=int(getenv("SYNC_MAX_EMAILS") or DEFAULT_SYNC_MAX_EMAILS),
        sync_days=int(getenv("SYNC_DAYS") or DEFAULT_SYNC_DAYS),
        port=int(getenv("PORT") or DEFAULT_PORT),
    )
