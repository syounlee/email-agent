import pytest

from email_agent.core.config import load_config

SETTINGS = ["ANTHROPIC_API_KEY", "MODEL", "MAX_BUDGET_USD", "MAX_TURNS", "DATA_DIR",
            "MAILBOX", "EMAIL_ADDRESS", "EMAIL_APP_PASSWORD", "IMAP_HOST", "IMAP_FOLDER",
            "SYNC_MAX_EMAILS", "SYNC_DAYS"]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No settings in the environment, and an empty .env file."""
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("")
    return env_file


def test_defaults(clean_env):
    cfg = load_config(clean_env)
    assert cfg.model == "claude-sonnet-5-5"
    assert cfg.max_budget_usd == 0.50
    assert cfg.max_turns == 20
    assert cfg.data_dir.name == "data"
    assert not cfg.has_api_key
    assert cfg.mailbox == "demo"
    assert cfg.imap_host == "imap.gmail.com" and cfg.imap_folder == "INBOX"
    assert cfg.sync_max_emails == 30 and cfg.sync_days == 7


def test_values_from_env_file(clean_env):
    clean_env.write_text(
        "ANTHROPIC_API_KEY=sk-test-not-real\nMODEL=claude-haiku-5-5\n"
        "MAX_BUDGET_USD=1.25\nMAX_TURNS=5\n"
    )
    cfg = load_config(clean_env)
    assert cfg.has_api_key
    assert cfg.model == "claude-haiku-5-5"
    assert cfg.max_budget_usd == 1.25
    assert cfg.max_turns == 5


def test_api_key_is_hidden_when_printed(clean_env):
    clean_env.write_text("ANTHROPIC_API_KEY=sk-test-not-real\n")
    cfg = load_config(clean_env)
    assert "sk-test-not-real" not in repr(cfg)
    assert "sk-test-not-real" not in str(cfg)


def test_imap_settings_and_password_hidden(clean_env):
    clean_env.write_text(
        "MAILBOX=IMAP\nEMAIL_ADDRESS=me@example.com\n"
        "EMAIL_APP_PASSWORD=abcd efgh ijkl mnop\nSYNC_MAX_EMAILS=5\n"
    )
    cfg = load_config(clean_env)
    assert cfg.mailbox == "imap"
    assert cfg.email_app_password == "abcdefghijklmnop"  # spaces from Google removed
    assert cfg.sync_max_emails == 5
    assert "abcd" not in repr(cfg)


def test_reading_env_file_does_not_change_environment(clean_env):
    import os

    clean_env.write_text("EMAIL_ADDRESS=leak@example.com\n")
    assert load_config(clean_env).email_address == "leak@example.com"
    assert "EMAIL_ADDRESS" not in os.environ


def test_environment_wins_over_env_file(clean_env, monkeypatch):
    clean_env.write_text("MODEL=from-file\n")
    monkeypatch.setenv("MODEL", "from-environment")
    assert load_config(clean_env).model == "from-environment"


def test_unknown_mailbox_is_rejected(clean_env):
    clean_env.write_text("MAILBOX=outlook\n")
    with pytest.raises(ValueError, match="MAILBOX"):
        load_config(clean_env)


@pytest.mark.live
def test_real_env_has_api_key():
    """Free check that your real .env has a key (nothing is sent to Claude)."""
    assert load_config().has_api_key, "ANTHROPIC_API_KEY is missing from .env"
