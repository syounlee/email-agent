"""Wait for new mail with IMAP IDLE, in a background thread.

IDLE means: "server, tell me the moment something changes" (no polling).
Gmail drops an IDLE after about 10 minutes of silence, so we stop and
re-start it every 9 minutes. When the server says a message EXISTS (new
mail), we call on_new_mail(); the caller then syncs and runs the listeners.

The IDLE connection opens the folder read-only: it only watches.
"""

import threading
import time

from email_agent.core.config import Config
from email_agent.mail.login import sign_in

REIDLE_SECONDS = 9 * 60   # restart IDLE before Gmail's ~10-minute cut-off
CHECK_SECONDS = 30        # how often we wake up to see if we should stop
RETRY_SECONDS = 30        # wait after a dropped connection before reconnecting


def _connect(config: Config):
    from imapclient import IMAPClient

    return IMAPClient(config.imap_host, ssl=True, timeout=60)


def _has_new_mail(responses) -> bool:
    """IDLE responses look like [(5, b'EXISTS'), (3, b'RECENT')]."""
    return any(isinstance(r, tuple) and len(r) >= 2 and r[1] == b"EXISTS" for r in responses)


def watch(config: Config, on_new_mail, stop: threading.Event, *, connect=_connect,
          reidle_seconds: float = REIDLE_SECONDS, check_seconds: float = CHECK_SECONDS,
          retry_seconds: float = RETRY_SECONDS, show=print) -> None:
    """Loop until `stop` is set: IDLE, and call on_new_mail() when mail arrives.

    After a dropped connection (Wi-Fi off, the Mac asleep) it reconnects, and
    calls on_new_mail() once to catch up on what arrived in the meantime.
    """
    reconnecting = False
    while not stop.is_set():
        try:
            with connect(config) as server:
                sign_in(server, config)
                server.select_folder(config.imap_folder, readonly=True)
                show(f"👂 watching {config.imap_folder} for new mail (IMAP IDLE)")
                if reconnecting:  # mail may have arrived while we were away
                    reconnecting = False
                    on_new_mail()
                while not stop.is_set():
                    server.idle()
                    started = time.monotonic()
                    new_mail = False
                    while not stop.is_set() and time.monotonic() - started < reidle_seconds:
                        if _has_new_mail(server.idle_check(timeout=check_seconds)):
                            new_mail = True
                            break
                    server.idle_done()  # leave IDLE (also every 9 minutes)
                    if new_mail:
                        on_new_mail()
        except Exception as problem:  # dropped connection, Wi-Fi off, ...
            if stop.is_set():
                break
            show(f"⚠️  watcher lost the connection ({type(problem).__name__}: {problem}); "
                 f"retrying in {retry_seconds:.0f}s")
            reconnecting = True
            stop.wait(retry_seconds)


def start_watcher(config: Config, on_new_mail, show=print) -> tuple[threading.Thread, threading.Event]:
    """Run watch() in a background thread. Set the returned event to stop it."""
    stop = threading.Event()
    thread = threading.Thread(target=watch, args=(config, on_new_mail, stop),
                              kwargs={"show": show}, daemon=True, name="imap-idle")
    thread.start()
    return thread, stop
