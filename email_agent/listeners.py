"""Listeners: small Python scripts in scripts/listeners/ that run on new mail.

Each listener file has:

    config = {"id": "...", "name": "...", "enabled": True, "event": "email_received"}

    async def handler(email, context):
        ...
        return {"executed": True, "reason": "what happened"}

- ListenerRegistry loads every file and reloads any file you change, add or
  delete (checked before each new email, so no restart is needed).
- run_listeners() runs the enabled ones on one email and logs every run to
  DATA_DIR/listener_runs.jsonl.
- context gives a listener: star_email, archive_email, add_label, notify,
  and call_claude(prompt, schema, model="haiku").

Listener changes run WITHOUT the y/n question: they are your own scripts.
In imap mode they change your real Gmail.
"""

import asyncio
import importlib.util
import inspect
import json
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from email_agent import actions, database
from email_agent.claude_json import call_claude
from email_agent.config import PROJECT_ROOT, load_config

LISTENERS_DIR = PROJECT_ROOT / "scripts" / "listeners"
LOG_FILENAME = "listener_runs.jsonl"
HANDLER_TIMEOUT_SECONDS = 120


# ---------- loading ----------

class ListenerRegistry:
    """Keeps the listener files loaded, reloading the ones that change."""

    def __init__(self, folder: Path = LISTENERS_DIR, show=print):
        self.folder = Path(folder)
        self.show = show
        self.modules: dict[Path, object] = {}   # file -> loaded module
        self.mtimes: dict[Path, float] = {}     # file -> last-modified time when loaded
        self.errors: dict[Path, str] = {}       # file -> why it couldn't load

    def refresh(self) -> None:
        """Load new files, reload changed ones, forget deleted ones."""
        files = {p for p in self.folder.glob("*.py") if not p.name.startswith("_")}
        for gone in set(self.mtimes) - files:
            self.modules.pop(gone, None)
            self.errors.pop(gone, None)
            del self.mtimes[gone]
        for path in sorted(files):
            mtime = path.stat().st_mtime
            if self.mtimes.get(path) == mtime:
                continue  # unchanged
            self.mtimes[path] = mtime
            self.modules.pop(path, None)
            try:
                self.modules[path] = _load(path)
                self.errors.pop(path, None)
            except Exception as problem:  # a broken file shouldn't stop the others
                self.errors[path] = f"{type(problem).__name__}: {problem}"
                self.show(f"⚠️  listener {path.name} not loaded: {self.errors[path]}")

    def enabled(self, event: str = "email_received") -> list:
        """Loaded listeners that are switched on for this event."""
        self.refresh()
        return [m for m in self.modules.values()
                if m.config.get("enabled", True) and m.config.get("event") == event]


def _load(path: Path):
    """Import one listener file fresh and check it has what it needs."""
    spec = importlib.util.spec_from_file_location(f"listener_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    config = getattr(module, "config", None)
    if not isinstance(config, dict):
        raise ValueError("needs a `config` dict")
    missing = {"id", "name", "enabled", "event"} - config.keys()
    if missing:
        raise ValueError(f"config is missing {sorted(missing)}")
    if config["event"] != "email_received":
        raise ValueError(f"unknown event {config['event']!r} (only 'email_received')")
    if not inspect.iscoroutinefunction(getattr(module, "handler", None)):
        raise ValueError("needs `async def handler(email, context)`")
    return module


# ---------- what a listener can do ----------

class ListenerContext:
    """The `context` passed to handler(email, context)."""

    def __init__(self, listener_id: str, show=print, claude_client=None):
        self.listener_id = listener_id
        self.show = show
        self.claude_client = claude_client
        self.actions: list[str] = []         # what this run changed, for the log
        self.claude_calls: list[dict] = []   # model, tokens, cost of each call

    async def _apply(self, action: str, email_id: str, label: str | None = None) -> str:
        # actions.apply may talk to Gmail, so run it off the event loop
        message = await asyncio.to_thread(actions.apply, action, email_id, label)
        self.actions.append(message)
        return message

    async def star_email(self, email_id: str) -> str:
        return await self._apply("star", email_id)

    async def archive_email(self, email_id: str) -> str:
        return await self._apply("archive", email_id)

    async def add_label(self, email_id: str, label: str) -> str:
        return await self._apply("add_label", email_id, label)

    async def notify(self, message: str) -> None:
        self.show(f"🔔 {message}")
        self.actions.append(f"notified: {message}")

    async def call_claude(self, prompt: str, schema: dict, model: str = "haiku") -> dict:
        answer, info = await call_claude(prompt, schema, model, client=self.claude_client)
        self.claude_calls.append(info)
        return answer


# ---------- running ----------

def log_path() -> Path:
    return load_config().data_dir / LOG_FILENAME


def _write_log(entry: dict) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def run_listeners(email: dict, registry: ListenerRegistry, show=print,
                        claude_client=None) -> list[dict]:
    """Run every enabled listener on one email. Returns (and logs) one entry per run."""
    entries = []
    for module in registry.enabled("email_received"):
        listener_id = module.config["id"]
        context = ListenerContext(listener_id, show, claude_client)
        started = time.monotonic()
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "listener": listener_id,
            "email_id": email["id"],
            "subject": email["subject"],
        }
        try:
            result = await asyncio.wait_for(module.handler(dict(email), context),
                                            HANDLER_TIMEOUT_SECONDS)
            if not (isinstance(result, dict) and isinstance(result.get("executed"), bool)
                    and isinstance(result.get("reason"), str)):
                raise ValueError(f"handler must return {{'executed': bool, 'reason': str}}, "
                                 f"got {result!r}")
            entry.update(executed=result["executed"], reason=result["reason"])
        except Exception as problem:  # one failing listener mustn't stop the rest
            entry.update(executed=False, reason="error",
                         error=f"{type(problem).__name__}: {problem}")
        entry.update(
            actions=context.actions,
            claude_calls=len(context.claude_calls),
            tokens_in=sum(c["input_tokens"] for c in context.claude_calls),
            tokens_out=sum(c["output_tokens"] for c in context.claude_calls),
            cost_usd=round(sum(c["cost_usd"] for c in context.claude_calls), 6),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        _write_log(entry)
        status = "✅" if entry["executed"] else ("❌" if "error" in entry else "➖")
        show(f"🎧 {status} {listener_id}: {entry.get('error') or entry['reason']}")
        entries.append(entry)
    return entries


async def process_pending(registry: ListenerRegistry, show=print, claude_client=None) -> list[dict]:
    """Run the listeners on every email still waiting for them, then unmark it."""
    with closing(database.open_db()) as conn:
        waiting = database.pending_for_listeners(conn)
    entries = []
    for email in waiting:
        show(f"📨 new: {email['subject']}  ({email['from']})")
        entries += await run_listeners(email, registry, show, claude_client)
        with closing(database.open_db()) as conn:
            database.mark_listened(conn, email["id"])
    return entries


# ---------- demo mode: pretend an email just arrived ----------

async def deliver_demo_email(sender: str, subject: str, body: str, *, to: str = "me@example.com",
                             registry: ListenerRegistry | None = None, show=print,
                             claude_client=None) -> tuple[str, list[dict]]:
    """Add an email to the demo mailbox as if it just arrived, and run the listeners.

    Returns (email_id, log entries).
    """
    if load_config().mailbox != "demo":
        raise RuntimeError("deliver_demo_email only works with MAILBOX=demo")
    email = {
        "id": f"demo-{uuid.uuid4().hex[:8]}",
        "from": sender, "to": to, "subject": subject, "body": body,
        "date": datetime.now().isoformat(timespec="seconds"),
        "labels": ["inbox"], "read": False, "starred": False,
    }
    with closing(database.open_db()) as conn:
        database.add_emails(conn, [email], pending=True)
    entries = await process_pending(registry or ListenerRegistry(show=show), show, claude_client)
    return email["id"], entries
