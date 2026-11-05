import pytest

from email_agent.config import load_config

SETTINGS = ["ANTHROPIC_API_KEY", "MODEL", "MAX_BUDGET_USD", "MAX_TURNS", "DATA_DIR"]


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


@pytest.mark.live
def test_real_env_has_api_key():
    """Free check that your real .env has a key (nothing is sent to Claude)."""
    assert load_config().has_api_key, "ANTHROPIC_API_KEY is missing from .env"
