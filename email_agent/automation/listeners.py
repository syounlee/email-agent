"""Listeners: small Python scripts in scripts/listeners/ that run on new mail.

Each listener file has:

    config = {"id": "...", "name": "...", "enabled": True, "event": "email_received"}

    async def handler(email, context):
        ...
        return {"executed": True, "reason": "what happened"}

- ListenerRegistry loads every file and reloads any file you change, add or
  delete (checked before each new email, so no restart is needed).
- run_listeners() runs the enabled ones on one email and logs every run to
  DATA_DIR/listener_runs-<mailbox>.jsonl (one log per mailbox).
- context gives a listener: star_email, archive_email, add_label, notify,
  and call_claude(prompt, schema, model="haiku").

Listener changes run WITHOUT the y/n question: they are your own scripts.
In imap mode they change your real mailbox.
"""

import asyncio
import importlib.util
import inspect
import json
import re
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from email_agent.mail import actions, database, mailbox
from email_agent.core.claude_json import call_claude
from email_agent.core.config import PROJECT_ROOT, load_config

LISTENERS_DIR = PROJECT_ROOT / "scripts" / "listeners"
LOG_FILENAME = "listener_runs-{mailbox}.jsonl"  # one log per mailbox, so they never mix
HANDLER_TIMEOUT_SECONDS = 120


# ---------- loading ----------

class ScriptRegistry:
    """Keeps a folder of script files loaded, reloading the ones that change.

    `check` validates one loaded module (raises ValueError if it's wrong).
    """

    def __init__(self, folder: Path, check, show=print):
        self.folder = Path(folder)
        self.check = check
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
                self.modules[path] = _load(path, self.check)
                self.errors.pop(path, None)
            except Exception as problem:  # a broken file shouldn't stop the others
                self.errors[path] = f"{type(problem).__name__}: {problem}"
                self.show(f"⚠️  {path.name} not loaded: {self.errors[path]}")

    def all(self) -> list:
        """Every script that loaded fine (in file-name order), after checking for changes."""
        self.refresh()
        return [self.modules[path] for path in sorted(self.modules)]


def _load(path: Path, check):
    """Import one script file fresh and check it has what it needs."""
    spec = importlib.util.spec_from_file_location(f"script_{path.parent.name}_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = getattr(module, "config", None)
    if not isinstance(config, dict):
        raise ValueError("needs a `config` dict")
    if not inspect.iscoroutinefunction(getattr(module, "handler", None)):
        raise ValueError("needs an `async def handler(...)`")
    check(module)
    return module


def _check_listener(module) -> None:
    missing = {"id", "name", "enabled", "event"} - module.config.keys()
    if missing:
        raise ValueError(f"config is missing {sorted(missing)}")
    if module.config["event"] != "email_received":
        raise ValueError(f"unknown event {module.config['event']!r} (only 'email_received')")


class ListenerRegistry(ScriptRegistry):
    """The listeners in scripts/listeners/."""

    def __init__(self, folder: Path = LISTENERS_DIR, show=print):
        super().__init__(folder, _check_listener, show)

    def enabled(self, event: str = "email_received") -> list:
        """Loaded listeners that are switched on for this event."""
        return [m for m in self.all()
                if m.config.get("enabled", True) and m.config.get("event") == event]


# ---------- what a listener can do ----------

class ScriptContext:
    """The `context` passed to listener and action handlers."""

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

    async def mark_read(self, email_id: str) -> str:
        return await self._apply("mark_read", email_id)

    async def mark_unread(self, email_id: str) -> str:
        return await self._apply("mark_unread", email_id)

    async def notify(self, message: str) -> None:
        """A notice on the web page and in the terminal (not on your phone)."""
        self.show(f"🔔 {message}")
        self.actions.append(f"notified: {message}")

    async def alert(self, email_id: str, kind: str, title: str, message: str,
                    urgent: bool = False) -> None:
        """An important email: to your phone now (urgent) or in the next summary.

        kind: "action", "coupon", "money", "security", "work" or "travel" (the icon).
        Nothing happens if another listener already alerted about this email, or
        if you muted its sender.
        """
        from email_agent.automation.alerts import send_alert  # avoids a circular import

        if kind == "security":
            message = mask_codes(message)  # never send a login code anywhere
        pushed = await send_alert(email_id, kind, title, message, urgent, show=self.show)
        if isinstance(pushed, str):  # "already alerted" or "sender muted"
            self.actions.append(f"{pushed}: {title}")
            return
        if not load_config().ntfy_topic:
            when = "phone alerts off"
        else:
            when = "phone now" if pushed else ("phone failed, in summary" if urgent else "summary")
        self.show(f"🔔 {title}: {message} ({when})")
        self.actions.append(f"notified: [{when}] {title}: {message}")

    async def alert_feedback(self) -> dict:
        """What you taught on the Alerts page:
        {"muted": [sender addresses], "not_important": ["title (from sender)", ...]}"""
        from email_agent.automation.alerts import feedback

        return await asyncio.to_thread(feedback)

    async def call_claude(self, prompt: str, schema: dict, model: str = "haiku") -> dict:
        answer, info = await call_claude(prompt, schema, model, client=self.claude_client)
        self.claude_calls.append(info)
        return answer

    async def search_emails(self, query: str, limit: int = 50) -> list[dict]:
        return await asyncio.to_thread(mailbox.search_emails, query, limit)

    async def read_email(self, ids) -> list[dict]:
        return await asyncio.to_thread(mailbox.read_email, ids)


ListenerContext = ScriptContext  # older name


class PreviewContext(ScriptContext):
    """Like ScriptContext, but changes nothing: it only records what WOULD happen.

    Claude calls are real (so you see real decisions and real costs).
    """

    async def _apply(self, action: str, email_id: str, label: str | None = None) -> str:
        message = f"would {action.replace('_', ' ')}" + (f" '{label}'" if label else "")
        self.actions.append(message)
        return message

    async def notify(self, message: str) -> None:
        self.actions.append(f"would notify: {message}")

    async def alert(self, email_id: str, kind: str, title: str, message: str,
                    urgent: bool = False) -> None:
        when = "phone now" if urgent else "summary"
        self.actions.append(f"would alert ({when}): {title}: {message}")


_CODE = re.compile(r"\b\d{3}[- ]?\d{3}\b")


def mask_codes(text: str) -> str:
    """Hide 6-digit login codes (e.g. '946636 is your code') before writing to the log."""
    return _CODE.sub("••••••", text)


def usage_summary(context: ScriptContext) -> dict:
    """Claude calls, tokens and cost of one run, for the log."""
    calls = context.claude_calls
    return {
        "claude_calls": len(calls),
        "tokens_in": sum(c["input_tokens"] for c in calls),
        "tokens_out": sum(c["output_tokens"] for c in calls),
        "cost_usd": round(sum(c["cost_usd"] for c in calls), 6),
    }


def check_result(result) -> dict:
    """Handlers must return {"executed": bool, "reason": str}."""
    if not (isinstance(result, dict) and isinstance(result.get("executed"), bool)
            and isinstance(result.get("reason"), str)):
        raise ValueError(f"handler must return {{'executed': bool, 'reason': str}}, got {result!r}")
    return result


# ---------- running ----------

def log_path() -> Path:
    config = load_config()
    return config.data_dir / LOG_FILENAME.format(mailbox=config.mailbox)


def write_log(entry: dict, path: Path | None = None) -> None:
    path = path or log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def run_listeners(email: dict, registry: ListenerRegistry, show=print,
                        claude_client=None, preview: bool = False) -> list[dict]:
    """Run every enabled listener on one email. Returns (and logs) one entry per run.

    preview=True: change nothing and write no log; just report what would happen.
    """
    entries = []
    for module in registry.enabled("email_received"):
        listener_id = module.config["id"]
        context = (PreviewContext if preview else ScriptContext)(listener_id, show, claude_client)
        started = time.monotonic()
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "listener": listener_id,
            "email_id": email["id"],
            "subject": mask_codes(email["subject"]),
        }
        try:
            result = check_result(await asyncio.wait_for(module.handler(dict(email), context),
                                                         HANDLER_TIMEOUT_SECONDS))
            entry.update(executed=result["executed"], reason=result["reason"])
        except Exception as problem:  # one failing listener mustn't stop the rest
            entry.update(executed=False, reason="error",
                         error=f"{type(problem).__name__}: {problem}")
        entry.update(actions=context.actions, **usage_summary(context),
                     duration_ms=round((time.monotonic() - started) * 1000))
        if preview:
            entries.append(entry)
            continue
        write_log(entry)
        status = "✅" if entry["executed"] else ("❌" if "error" in entry else "➖")
        show(f"🎧 {status} {listener_id}: {entry.get('error') or entry['reason']}")
        entries.append(entry)
    return entries


def read_log(listener_id: str | None = None, limit: int = 50) -> list[dict]:
    """The newest `limit` runs from the listener log (optionally for one listener)."""
    path = log_path()
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a half-written line; skip it
        if listener_id is None or entry.get("listener") == listener_id:
            entries.append(entry)
    return entries[-limit:]


async def process_pending(registry: ListenerRegistry, show=print, claude_client=None,
                          on_email=None, on_entries=None) -> list[dict]:
    """Run the listeners on every email still waiting for them, then unmark it.

    on_email(email) and on_entries(email, entries): optional async callbacks,
    used by the web server to tell browsers what happened.
    """
    with closing(database.open_db()) as conn:
        waiting = database.pending_for_listeners(conn)
    entries = []
    for email in waiting:
        show(f"📨 new: {email['subject']}  ({email['from']})")
        if on_email:
            await on_email(email)
        these = await run_listeners(email, registry, show, claude_client)
        if on_entries:
            await on_entries(email, these)
        entries += these
        with closing(database.open_db()) as conn:
            database.mark_listened(conn, email["id"])
    return entries


# ---------- demo mode: pretend an email just arrived ----------

def add_demo_email(sender: str, subject: str, body: str, *, to: str = "me@example.com",
                   html: str | None = None) -> str:
    """Put an email into the demo mailbox as new mail (waiting for the listeners)."""
    if load_config().mailbox != "demo":
        raise RuntimeError("deliver_demo_email only works with MAILBOX=demo")
    email = {
        "id": f"demo-{uuid.uuid4().hex[:8]}",
        "from": sender, "to": to, "subject": subject, "body": body,
        "date": datetime.now().isoformat(timespec="seconds"),
        "labels": ["inbox"], "read": False, "starred": False, "html": html,
    }
    with closing(database.open_db()) as conn:
        database.add_emails(conn, [email], pending=True)
    return email["id"]


async def deliver_demo_email(sender: str, subject: str, body: str, *, to: str = "me@example.com",
                             html: str | None = None,
                             registry: ListenerRegistry | None = None, show=print,
                             claude_client=None) -> tuple[str, list[dict]]:
    """Add an email to the demo mailbox as if it just arrived, and run the listeners.

    Returns (email_id, log entries).
    """
    email_id = add_demo_email(sender, subject, body, to=to, html=html)
    entries = await process_pending(registry or ListenerRegistry(show=show), show, claude_client)
    return email_id, entries
