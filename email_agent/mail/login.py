"""Sign in to your mail server, and check that it works.

    uv run email-connect     # test the connection (Outlook: sign in once first)

Gmail, iCloud Mail, Yahoo Mail and most others: an *app password* (a separate
password the provider makes for apps like this one), EMAIL_APP_PASSWORD in .env.

Outlook (outlook.com, hotmail.com, live.com): Microsoft stopped accepting
passwords from mail apps in September 2024. Instead, you sign in once on
Microsoft's own web page, and this program is given a "sign-in" it can renew
by itself. It is kept in DATA_DIR/outlook-signin.json. That file works like a
password: it is never printed, and DATA_DIR is never committed.
"""

import os
import sys
from email_agent.core.config import Config, load_config

# What we ask Microsoft for: reading and changing your mail over IMAP.
# (msal adds "offline_access" itself, so the sign-in can be renewed.)
OUTLOOK_SCOPES = ["https://outlook.office.com/IMAP.AccessAsUser.All"]
OUTLOOK_AUTHORITY = "https://login.microsoftonline.com/common"  # personal and work accounts


class SignInError(Exception):
    """Can't sign in. The message says what to do, and never contains a secret."""


def signin_path(config: Config):
    return config.data_dir / "outlook-signin.json"


def _outlook_app(config: Config):
    """Microsoft's sign-in helper, with the saved sign-in loaded."""
    import msal  # only needed for Outlook

    cache = msal.SerializableTokenCache()
    path = signin_path(config)
    if path.exists():
        cache.deserialize(path.read_text())
    app = msal.PublicClientApplication(config.outlook_client_id, authority=OUTLOOK_AUTHORITY,
                                       token_cache=cache)
    return app, cache


def _save(config: Config, cache) -> None:
    if cache.has_state_changed:
        path = signin_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cache.serialize())
        os.chmod(path, 0o600)  # only you can read it


def outlook_token(config: Config, app_factory=_outlook_app) -> str:
    """A fresh access token from the saved sign-in (renewed silently when needed)."""
    if not config.outlook_client_id:
        raise SignInError("Outlook needs OUTLOOK_CLIENT_ID in .env (see the README).")
    app, cache = app_factory(config)
    accounts = app.get_accounts(username=config.email_address) or app.get_accounts()
    result = app.acquire_token_silent(OUTLOOK_SCOPES, account=accounts[0]) if accounts else None
    _save(config, cache)
    if not result or "access_token" not in result:
        raise SignInError("Not signed in to Outlook yet, or the sign-in has expired. "
                          "Run:  uv run email-connect")
    return result["access_token"]


def sign_in(server, config: Config, token=outlook_token) -> None:
    """Log in on an open IMAP connection, the right way for the provider."""
    if config.uses_oauth:
        server.oauth2_login(config.email_address, token(config))
    else:
        server.login(config.email_address, config.email_app_password)


def outlook_sign_in(config: Config, show=print, app_factory=_outlook_app) -> None:
    """The one-time sign-in: you open Microsoft's page and type a short code."""
    app, cache = app_factory(config)
    flow = app.initiate_device_flow(scopes=OUTLOOK_SCOPES)
    if "user_code" not in flow:
        raise SignInError(f"Microsoft didn't start the sign-in: {flow.get('error_description', flow)}")
    show(flow["message"])  # "To sign in, use a web browser to open ... and enter the code ..."
    result = app.acquire_token_by_device_flow(flow)  # waits until you've signed in
    if "access_token" not in result:
        raise SignInError(f"Sign-in didn't finish: {result.get('error_description', 'unknown')}")
    _save(config, cache)


def missing_settings(config: Config) -> str:
    """What still has to go in .env before we can connect ('' = nothing)."""
    if not config.email_address:
        return "Set EMAIL_ADDRESS in .env."
    if not config.imap_host:
        return "Set IMAP_HOST in .env (your provider's IMAP server, e.g. imap.example.com)."
    if config.uses_oauth and not config.outlook_client_id:
        return "Outlook needs OUTLOOK_CLIENT_ID in .env (see 'Connecting Outlook' in the README)."
    if not config.uses_oauth and not config.email_app_password:
        return (f"Set EMAIL_APP_PASSWORD in .env: an app password from {config.provider_name} "
                "(see the README for where to make one).")
    return ""


def check_connection(config: Config, connect=None, token=outlook_token) -> int:
    """Sign in and open the inbox read-only. Returns how many emails it holds."""
    if connect is None:
        from imapclient import IMAPClient

        def connect(c):
            return IMAPClient(c.imap_host, ssl=True, timeout=30)
    with connect(config) as server:  # signs out when done
        sign_in(server, config, token)
        info = server.select_folder(config.imap_folder, readonly=True)
        return int(info.get(b"EXISTS", 0))


def main() -> None:
    config = load_config()
    problem = missing_settings(config)
    if problem:
        sys.exit(f"❌ {problem}")
    print(f"Connecting to {config.provider_name} ({config.imap_host}) as {config.email_address} ...")
    try:
        if config.uses_oauth:
            try:
                outlook_token(config)
                print("Already signed in to Microsoft.")
            except SignInError:
                print("First, sign in to Microsoft once:")
                outlook_sign_in(config)
        count = check_connection(config)
    except SignInError as problem:
        sys.exit(f"❌ {problem}")
    except Exception as problem:  # wrong password, IMAP turned off, no network
        hint = ("Check the app password, and that IMAP is turned on in your mail settings."
                if not config.uses_oauth else "Try signing in again: delete "
                f"{signin_path(config)} and run  uv run email-connect")
        sys.exit(f"❌ Could not connect ({type(problem).__name__}: {problem}).\n   {hint}")
    from email_agent.core.watch_lock import WatchLock

    watcher = WatchLock(config).holder() if config.mailbox == "imap" else None
    if watcher:
        next_step = f"It is already being watched (process {watcher}). Nothing else to do."
    elif config.mailbox == "imap":
        next_step = "Start watching with:  uv run email-watch  (or  uv run email-autostart on)"
    else:
        next_step = "Set MAILBOX=imap in .env, then start watching with:  uv run email-watch"
    print(f"✅ Connected. Your {config.imap_folder} has {count} emails.\n   {next_step}")


if __name__ == "__main__":
    main()
