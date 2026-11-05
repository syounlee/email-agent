"""Offline tests for listeners: loading, reloading, running, logging, demo delivery,
call_claude's request shape, and the IMAP IDLE watcher. No Claude, no Gmail."""

import json
import os
import shutil
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest

from email_agent import database
from email_agent.claude_json import ClaudeCallError, call_claude, cost_usd, strict_schema
from email_agent.config import PROJECT_ROOT, load_config
from email_agent.idle_watcher import watch
from email_agent.listeners import ListenerRegistry, deliver_demo_email, log_path, run_listeners

EXAMPLE = PROJECT_ROOT / "scripts" / "listeners" / "finance_labeler.py"


# ---------- a fake Claude that answers with whatever JSON we tell it ----------

class FakeClaude:
    def __init__(self, *answers, stop_reason="end_turn"):
        self.answers = list(answers)
        self.stop_reason = stop_reason
        self.requests = []
        self.messages = self  # so client.messages.create(...) works

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        text = json.dumps(self.answers.pop(0)) if self.answers else "{}"
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(input_tokens=600, output_tokens=40,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )


def write_listener(folder, name, body, enabled=True, listener_id=None):
    path = folder / f"{name}.py"
    path.write_text(
        f"config = {{'id': {listener_id or name!r}, 'name': 'test', "
        f"'enabled': {enabled}, 'event': 'email_received'}}\n\n"
        f"async def handler(email, context):\n{body}\n"
    )
    return path


def bump_mtime(path):
    """Make sure the loader sees the file as changed, even within the same second."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))


def read_log():
    return [json.loads(line) for line in log_path().read_text().splitlines()]


EMAIL = {"id": "e005", "from": "x@example.com", "subject": "Receipt", "body": "Paid $5",
         "labels": ["inbox"], "read": False, "starred": False}


# ---------- loading and reloading ----------

def test_loader_skips_disabled_and_broken_files(tmp_path):
    write_listener(tmp_path, "on", "    return {'executed': True, 'reason': 'ok'}")
    write_listener(tmp_path, "off", "    return {'executed': True, 'reason': 'ok'}", enabled=False)
    (tmp_path / "broken.py").write_text("this is not python(")
    (tmp_path / "no_handler.py").write_text(
        "config = {'id': 'x', 'name': 'x', 'enabled': True, 'event': 'email_received'}")
    (tmp_path / "_helper.py").write_text("raise SystemExit('should never be imported')")
    shown = []
    registry = ListenerRegistry(tmp_path, show=shown.append)

    assert [m.config["id"] for m in registry.enabled()] == ["on"]
    assert len(registry.errors) == 2  # broken.py and no_handler.py, reported not fatal
    assert any("broken.py" in s for s in shown)


def test_changed_file_is_reloaded_without_restart(tmp_path):
    path = write_listener(tmp_path, "l1", "    return {'executed': True, 'reason': 'version 1'}")
    registry = ListenerRegistry(tmp_path)
    assert registry.enabled()[0].config["enabled"] is True

    write_listener(tmp_path, "l1", "    return {'executed': True, 'reason': 'version 2'}",
                   enabled=False)
    bump_mtime(path)
    assert registry.enabled() == []  # switched off just by saving the file

    write_listener(tmp_path, "l1", "    return {'executed': True, 'reason': 'version 3'}")
    bump_mtime(path)
    write_listener(tmp_path, "l2", "    return {'executed': False, 'reason': 'new file'}")
    assert sorted(m.config["id"] for m in registry.enabled()) == ["l1", "l2"]

    (tmp_path / "l2.py").unlink()
    assert [m.config["id"] for m in registry.enabled()] == ["l1"]


# ---------- running and logging ----------

async def test_run_logs_every_run_including_failures(tmp_path):
    write_listener(tmp_path, "a_ok", "    await context.notify('hi')\n"
                                     "    return {'executed': True, 'reason': 'did it'}")
    write_listener(tmp_path, "b_skip", "    return {'executed': False, 'reason': 'not for me'}")
    write_listener(tmp_path, "c_crash", "    raise RuntimeError('boom')")
    write_listener(tmp_path, "d_bad", "    return 'oops'")
    shown = []
    entries = await run_listeners(EMAIL, ListenerRegistry(tmp_path), show=shown.append)

    by_id = {e["listener"]: e for e in entries}
    assert by_id["a_ok"]["executed"] is True and by_id["a_ok"]["actions"] == ["notified: hi"]
    assert by_id["b_skip"] == {**by_id["b_skip"], "executed": False, "reason": "not for me"}
    assert "boom" in by_id["c_crash"]["error"]
    assert "must return" in by_id["d_bad"]["error"]
    assert [e["listener"] for e in read_log()] == ["a_ok", "b_skip", "c_crash", "d_bad"]
    assert read_log()[0]["email_id"] == "e005"
    assert "🔔 hi" in shown


async def test_context_actions_change_the_mailbox(tmp_path):
    write_listener(tmp_path, "acts",
                   "    await context.add_label(email['id'], 'Finance')\n"
                   "    await context.star_email(email['id'])\n"
                   "    await context.archive_email(email['id'])\n"
                   "    return {'executed': True, 'reason': 'all three'}")
    [entry] = await run_listeners(EMAIL, ListenerRegistry(tmp_path), show=lambda _: None)
    assert len(entry["actions"]) == 3
    from email_agent.mailbox import read_email
    email = read_email("e005")[0]
    assert email["starred"] is True and email["labels"] == ["receipts", "Finance"]


# ---------- the example listener, with a fake Claude ----------

@pytest.fixture
def example_only(tmp_path):
    folder = tmp_path / "listeners"
    folder.mkdir()
    shutil.copy(EXAMPLE, folder)
    return ListenerRegistry(folder)


async def test_demo_receipt_gets_finance_label_and_log(example_only):
    claude = FakeClaude({"kind": "receipt", "overdue": False, "why": "payment confirmed"})
    email_id, [entry] = await deliver_demo_email(
        "receipts@shop.example.com", "Your receipt", "Total paid: $9",
        registry=example_only, show=lambda _: None, claude_client=claude)

    from email_agent.mailbox import read_email
    email = read_email(email_id)[0]
    assert email["labels"] == ["inbox", "Finance"] and email["starred"] is False
    assert entry["executed"] is True and entry["claude_calls"] == 1
    assert entry["cost_usd"] == pytest.approx((600 * 0.10 + 40 * 0.50) / 1e6)
    assert read_log()[-1]["email_id"] == email_id
    with closing(database.open_db()) as conn:  # handled: no longer waiting
        assert database.pending_for_listeners(conn) == []


async def test_demo_overdue_invoice_is_starred_too(example_only):
    claude = FakeClaude({"kind": "invoice", "overdue": True, "why": "due date passed"})
    shown = []
    email_id, [entry] = await deliver_demo_email(
        "a@lawn.example.net", "Payment reminder", "was due last month",
        registry=example_only, show=shown.append, claude_client=claude)
    from email_agent.mailbox import read_email
    email = read_email(email_id)[0]
    assert email["starred"] is True and "Finance" in email["labels"]
    assert any("Overdue invoice" in s for s in shown)


async def test_demo_newsletter_is_left_alone(example_only):
    claude = FakeClaude({"kind": "other", "overdue": False, "why": "newsletter"})
    email_id, [entry] = await deliver_demo_email(
        "news@x.example.org", "Weekly news", "stories", registry=example_only,
        show=lambda _: None, claude_client=claude)
    from email_agent.mailbox import read_email
    assert read_email(email_id)[0]["labels"] == ["inbox"]
    assert entry["executed"] is False and entry["actions"] == []


async def test_deliver_demo_needs_demo_mode(monkeypatch):
    monkeypatch.setenv("MAILBOX", "imap")
    with pytest.raises(RuntimeError, match="MAILBOX=demo"):
        await deliver_demo_email("a", "b", "c")


# ---------- call_claude: the request we send ----------

def test_strict_schema_closes_every_object_without_touching_the_original():
    schema = {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {"x": {"type": "string"}}}},
        "properties": {"type": "object", "properties": {}},  # a field literally named "properties"
        "choice": {"anyOf": [{"type": "object", "properties": {"y": {"type": "integer"}}},
                             {"type": "string"}]},
    }}
    fixed = strict_schema(schema)
    assert fixed["additionalProperties"] is False
    assert fixed["properties"]["items"]["items"]["additionalProperties"] is False
    assert fixed["properties"]["properties"]["additionalProperties"] is False
    assert "additionalProperties" not in fixed["properties"]["properties"]["properties"]
    assert fixed["properties"]["choice"]["anyOf"][0]["additionalProperties"] is False
    assert "additionalProperties" not in fixed["properties"]["choice"]["anyOf"][1]
    assert "additionalProperties" not in schema  # original unchanged


async def test_call_claude_uses_structured_outputs_and_haiku():
    claude = FakeClaude({"kind": "receipt"})
    answer, info = await call_claude("classify", {"type": "object", "properties": {
        "kind": {"type": "string"}}, "required": ["kind"]}, client=claude)
    [request] = claude.requests
    assert request["model"] == "claude-haiku-5-5"
    fmt = request["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False
    assert "tool_choice" not in request and "tools" not in request
    assert answer == {"kind": "receipt"}
    assert info["input_tokens"] == 600 and info["cost_usd"] == pytest.approx(8e-5)


async def test_call_claude_reports_refusals():
    with pytest.raises(ClaudeCallError, match="declined"):
        await call_claude("x", {"type": "object", "properties": {}},
                          client=FakeClaude({}, stop_reason="refusal"))


def test_cost_for_unknown_model_is_zero():
    usage = SimpleNamespace(input_tokens=10, output_tokens=10)
    assert cost_usd("some-other-model", usage) == 0


# ---------- the IMAP IDLE watcher, with a fake server ----------

class FakeIdleServer:
    """Answers idle_check() from a script; sets `stop` when the script runs out."""

    def __init__(self, script, stop, max_idles=50):
        self.script, self.stop, self.max_idles = list(script), stop, max_idles
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.calls.append("login")

    def select_folder(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))

    def idle(self):
        self.calls.append("idle")
        if self.calls.count("idle") >= self.max_idles:  # safety net: never loop forever
            self.stop.set()

    def idle_check(self, timeout):
        if not self.script:
            self.stop.set()
            return []
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def idle_done(self):
        self.calls.append("idle_done")


def imap_config(monkeypatch, tmp_path):
    monkeypatch.setenv("MAILBOX", "imap")
    monkeypatch.setenv("EMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "not-real")
    return load_config(tmp_path / "no.env")


def test_watcher_calls_back_on_new_mail_and_is_read_only(monkeypatch, tmp_path):
    stop = threading.Event()
    server = FakeIdleServer([[], [(7, b"EXISTS")], [(1, b"EXPUNGE")]], stop)
    arrivals = []
    watch(imap_config(monkeypatch, tmp_path), lambda: arrivals.append(1), stop,
          connect=lambda c: server, show=lambda _: None)
    assert arrivals == [1]  # EXISTS = new mail; EXPUNGE is not
    assert ("select", "INBOX", True) in server.calls  # watching only
    assert server.calls.count("idle") == server.calls.count("idle_done")  # always leaves IDLE


def test_watcher_restarts_idle_periodically(monkeypatch, tmp_path):
    stop = threading.Event()
    server = FakeIdleServer([], stop, max_idles=3)
    # reidle_seconds=0: "9 minutes" pass at once, so IDLE restarts straight away
    watch(imap_config(monkeypatch, tmp_path), lambda: None, stop, connect=lambda c: server,
          reidle_seconds=0, show=lambda _: None)
    assert server.calls.count("idle") == 3
    assert server.calls.count("idle_done") == 3


def test_watcher_reconnects_after_a_dropped_connection(monkeypatch, tmp_path):
    stop = threading.Event()
    servers = [FakeIdleServer([OSError("wifi off")], stop),
               FakeIdleServer([[(3, b"EXISTS")]], stop)]
    arrivals, shown = [], []
    watch(imap_config(monkeypatch, tmp_path), lambda: arrivals.append(1), stop,
          connect=lambda c: servers.pop(0), retry_seconds=0, show=shown.append)
    assert arrivals == [1]
    assert any("lost the connection" in s for s in shown)
