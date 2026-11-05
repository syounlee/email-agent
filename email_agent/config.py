"""Load settings from the .env file (or the environment).

Use load_config() to get a Config object. Values in .env never get printed:
the API key is hidden when a Config is shown.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_BUDGET_USD = 0.50
DEFAULT_MAX_TURNS = 20
DEFAULT_DATA_DIR = "data/"


@dataclass(frozen=True)
class Config:
    # repr=False keeps the key out of print() and error messages.
    anthropic_api_key: str = field(repr=False)
    model: str
    max_budget_usd: float
    max_turns: int
    data_dir: Path

    @property
    def has_api_key(self) -> bool:
        return bool(self.anthropic_api_key)


def load_config(env_file: Path | None = None) -> Config:
    """Read settings from .env (in the project folder) and the environment."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

    data_dir = Path(os.getenv("DATA_DIR") or DEFAULT_DATA_DIR)
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    return Config(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        model=os.getenv("MODEL") or DEFAULT_MODEL,
        max_budget_usd=float(os.getenv("MAX_BUDGET_USD") or DEFAULT_MAX_BUDGET_USD),
        max_turns=int(os.getenv("MAX_TURNS") or DEFAULT_MAX_TURNS),
        data_dir=data_dir,
    )
