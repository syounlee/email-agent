"""Make sure only one program watches your mailbox at a time.

email-watch (started by you or automatically at login) and email-agent (the
web page) can both watch the mailbox. If two did it at once, every new email would
be checked twice. Whoever starts first holds this lock; the other one either
waits (email-watch) or just shows the page (email-agent).

The lock is a file in DATA_DIR. The operating system releases it when the
program holding it stops, even if it crashes, so it can never get stuck.
"""

import fcntl
import os

from email_agent.core.config import Config, load_config


class WatchLock:
    def __init__(self, config: Config | None = None):
        config = config or load_config()
        self.path = config.data_dir / f"watch-{config.mailbox}.lock"
        self._file = None

    def acquire(self, wait: bool = False) -> bool:
        """Take the lock. wait=False: give up at once if someone has it."""
        if self._file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))  # who holds it, for `email-autostart status`
        handle.flush()
        self._file = handle
        return True

    def release(self) -> None:
        if self._file is not None:
            fcntl.flock(self._file, fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    def holder(self) -> int | None:
        """The process id of whoever holds the lock now, or None if it's free."""
        if not self.path.exists():
            return None
        probe = WatchLock.__new__(WatchLock)
        probe.path, probe._file = self.path, None
        if probe.acquire():
            probe.release()
            return None
        try:
            return int(self.path.read_text().strip() or 0) or None
        except (OSError, ValueError):
            return None
