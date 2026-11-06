"""Connecting to Gmail, Outlook, iCloud Mail, Yahoo Mail or another server.
Free: no real server and no real Microsoft sign-in (both are faked)."""

import json
import stat

import pytest

from email_agent.core.config import guess_provider, load_config
from email_agent.mail import login


def config_for(monkeypatch, tmp_path, address, **settings):
    monkeypatch.setenv("EMAIL_ADDRESS", address)
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "not-a-real-password")
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    return load_config(tmp_path / "no.env")


# ---------- which provider, which server ----------

@pytest.mark.parametrize("address,provider,host", [
    ("me@gmail.com", "gmail", "imap.gmail.com"),
    ("me@outlook.com", "outlook", "outlook.office365.com"),
    ("me@hotmail.com", "outlook", "outlook.office365.com"),
    ("me@icloud.com", "icloud", "imap.mail.me.com"),
    ("me@yahoo.co.uk", "yahoo", "imap.mail.yahoo.com"),
])
def test_provider_is_guessed_from_the_address(monkeypatch, tmp_path, address, provider, host):
    config = config_for(monkeypatch, tmp_path, address)
    assert (config.provider, config.imap_host) == (provider, host)


def test_own_domain_needs_the_provider_or_server(monkeypatch, tmp_path):
    assert guess_provider("me@mycompany.com") == "other"
    config = config_for(monkeypatch, tmp_path, "me@mycompany.com")
    assert not config.can_sign_in and "IMAP_HOST" in login.missing_settings(config)
    config = config_for(monkeypatch, tmp_path, "me@mycompany.com", EMAIL_PROVIDER="gmail")
    assert config.imap_host == "imap.gmail.com" and config.can_sign_in  # Google Workspace
    config = config_for(monkeypatch, tmp_path, "me@mycompany.com", IMAP_HOST="imap.mycompany.com")
    assert config.imap_host == "imap.mycompany.com" and config.can_sign_in


def test_unknown_provider_setting_is_refused(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="EMAIL_PROVIDER"):
        config_for(monkeypatch, tmp_path, "me@gmail.com", EMAIL_PROVIDER="aol")


def test_outlook_needs_a_client_id_not_a_password(monkeypatch, tmp_path):
    config = config_for(monkeypatch, tmp_path, "me@outlook.com")
    assert config.uses_oauth and not config.can_sign_in
    assert "OUTLOOK_CLIENT_ID" in login.missing_settings(config)
    config = config_for(monkeypatch, tmp_path, "me@outlook.com", OUTLOOK_CLIENT_ID="abc-123",
                        EMAIL_APP_PASSWORD="")
    assert config.can_sign_in and login.missing_settings(config) == ""


# ---------- signing in ----------

class FakeServer:
    def __init__(self):
        self.calls = []

    def login(self, user, password):
        self.calls.append(("login", user))

    def oauth2_login(self, user, token):
        self.calls.append(("oauth2_login", user, token))

    def select_folder(self, folder, readonly=False):
        self.calls.append(("select_folder", folder, readonly))
        return {b"EXISTS": 42, b"UIDVALIDITY": 1}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append(("logout",))


def test_app_password_providers_log_in_with_it(monkeypatch, tmp_path):
    server = FakeServer()
    login.sign_in(server, config_for(monkeypatch, tmp_path, "me@icloud.com"))
    assert server.calls == [("login", "me@icloud.com")]


def test_outlook_logs_in_with_a_microsoft_token(monkeypatch, tmp_path):
    server = FakeServer()
    config = config_for(monkeypatch, tmp_path, "me@outlook.com", OUTLOOK_CLIENT_ID="abc")
    login.sign_in(server, config, token=lambda c: "fake-token")
    assert server.calls == [("oauth2_login", "me@outlook.com", "fake-token")]


def test_check_connection_opens_the_inbox_read_only(monkeypatch, tmp_path):
    server = FakeServer()
    config = config_for(monkeypatch, tmp_path, "me@yahoo.com")
    assert login.check_connection(config, connect=lambda c: server) == 42
    assert ("select_folder", "INBOX", True) in server.calls and server.calls[-1] == ("logout",)


class FakeCache:
    def __init__(self):
        self.has_state_changed = False

    def serialize(self):
        return json.dumps({"refresh": "secret-refresh-token"})


class FakeMicrosoft:
    """Stands in for msal's PublicClientApplication."""

    def __init__(self, signed_in=False):
        self.signed_in, self.cache, self.shown = signed_in, FakeCache(), []

    def __call__(self, config):  # the app_factory
        return self, self.cache

    def get_accounts(self, username=None):
        return [{"username": "me@outlook.com"}] if self.signed_in else []

    def acquire_token_silent(self, scopes, account):
        return {"access_token": "fresh-token"}

    def initiate_device_flow(self, scopes):
        assert scopes == login.OUTLOOK_SCOPES
        return {"user_code": "ABCD", "message": "Open https://microsoft.com/devicelogin, enter ABCD"}

    def acquire_token_by_device_flow(self, flow):
        self.signed_in, self.cache.has_state_changed = True, True
        return {"access_token": "first-token"}


def test_outlook_without_a_sign_in_says_what_to_run(monkeypatch, tmp_path):
    config = config_for(monkeypatch, tmp_path, "me@outlook.com", OUTLOOK_CLIENT_ID="abc")
    with pytest.raises(login.SignInError, match="email-connect"):
        login.outlook_token(config, app_factory=FakeMicrosoft())


def test_outlook_sign_in_once_then_tokens_renew_by_themselves(monkeypatch, tmp_path):
    config = config_for(monkeypatch, tmp_path, "me@outlook.com", OUTLOOK_CLIENT_ID="abc")
    microsoft, shown = FakeMicrosoft(), []
    login.outlook_sign_in(config, show=shown.append, app_factory=microsoft)
    assert shown == ["Open https://microsoft.com/devicelogin, enter ABCD"]
    saved = login.signin_path(config)
    assert saved.exists() and stat.S_IMODE(saved.stat().st_mode) == 0o600  # only you can read it
    assert login.outlook_token(config, app_factory=microsoft) == "fresh-token"
    assert "secret-refresh-token" not in repr(config) and "secret" not in " ".join(shown)
