"""Tests for the local web server: every route, the WebSocket chat (with a fake
Claude), approvals, live events and the local-only checks. All free."""

import dataclasses
from contextlib import closing
from urllib.parse import quote

import pytest
from claude_agent_sdk import PermissionResultAllow
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from email_agent import database
from email_agent.config import load_config
from email_agent.server import create_app

BASE = "http://127.0.0.1:3000"
WS = "ws://127.0.0.1:3000/ws"  # full address, so the Host header is right
BUG_LISTENER = """
config = {"id": "bug-alert", "name": "Bug alert", "enabled": True, "event": "email_received"}

async def handler(email, context):
    if "bug" not in email["subject"].lower():
        return {"executed": False, "reason": "not a bug report"}
    await context.notify("Bug: " + email["subject"])
    return {"executed": True, "reason": "notified"}
"""


class FakeClaude:
    """Stands in for claude_turn. Streams a little, asks approval for 'star', costs 1 cent."""

    def __init__(self):
        self.calls = []

    async def __call__(self, text, *, resume, approver, emit):
        self.calls.append({"text": text, "resume": resume})
        await emit({"type": "tool_call", "name": "mcp__email__search_emails",
                    "input": {"query": "urgent"}, "summary": "🔎 search: urgent"})
        reply = f"You said: {text}"
        if "star" in text:
            decision = await approver("mcp__email__star_email", {"id": "e001"}, None)
            reply = "Starred it." if isinstance(decision, PermissionResultAllow) else "Not done."
        await emit({"type": "assistant_text", "text": reply})
        return {"session_id": resume or "session-1", "text": reply,
                "total_cost_usd": 0.01 * len(self.calls), "is_error": False, "subtype": "success"}


@pytest.fixture
def listeners_dir(tmp_path):
    folder = tmp_path / "listeners"
    folder.mkdir()
    (folder / "bug_alert.py").write_text(BUG_LISTENER)
    return folder


@pytest.fixture
def fake():
    return FakeClaude()


def make_client(listeners_dir, fake, config=None, **kwargs):
    app = create_app(config or load_config(), chat_turn=fake, start_watcher_thread=False,
                     listeners_dir=listeners_dir, **kwargs)
    return TestClient(app, base_url=BASE)


@pytest.fixture
def client(listeners_dir, fake):
    with make_client(listeners_dir, fake) as c:
        yield c


def receive_until(ws, kind):
    """Read WebSocket messages until one of type `kind`; return all of them."""
    seen = []
    while True:
        message = ws.receive_json()
        seen.append(message)
        if message["type"] == kind:
            return seen


# ---------- HTTP ----------

def test_inbox(client):
    data = client.get("/api/inbox?limit=5").json()
    assert data["mailbox"] == "demo" and len(data["emails"]) == 5
    first = data["emails"][0]
    assert set(first) == {"id", "from", "subject", "date", "snippet", "labels", "read", "starred"}
    dates = [e["date"] for e in data["emails"]]
    assert dates == sorted(dates, reverse=True)


def test_inbox_leaves_out_archived_mail(client):
    from email_agent.actions import apply
    apply("archive", "e008")  # newest sample email
    ids = [e["id"] for e in client.get("/api/inbox?limit=50").json()["emails"]]
    assert "e008" not in ids and "e001" in ids


def test_search(client):
    r = client.post("/api/search", json={"query": "invoice is:unread"})
    assert r.status_code == 200 and [x["id"] for x in r.json()["results"]] == ["e002"]
    assert client.post("/api/search", json={"query": "is:important"}).status_code == 400
    assert client.post("/api/search", json={"nothing": 1}).status_code == 400
    assert client.post("/api/search", json={"query": "x", "limit": 0}).status_code == 400
    assert client.post("/api/search", content=b"not json").status_code == 400


def test_email(client):
    r = client.get("/api/email/e003")
    assert r.status_code == 200 and "overdue" in r.json()["body"]
    assert client.get("/api/email/nope").status_code == 404


def test_email_with_gmail_style_id(client):
    with closing(database.open_db()) as conn:
        database.add_emails(conn, [{"id": "<abc/1@mail.example.com>", "from": "a@example.com",
                                    "to": "me", "subject": "odd id", "date": "2026-11-01T00:00:00",
                                    "body": "hi", "labels": ["inbox"]}])
    r = client.get("/api/email/" + quote("<abc/1@mail.example.com>", safe=""))
    assert r.status_code == 200 and r.json()["subject"] == "odd id"


def test_listeners_and_logs(client):
    data = client.get("/api/listeners").json()
    assert data["listeners"] == [{"id": "bug-alert", "name": "Bug alert", "enabled": True,
                                  "event": "email_received", "file": "bug_alert.py"}]
    assert client.get("/api/listeners/bug-alert/logs").json() == {"listener": "bug-alert", "runs": []}
    client.post("/api/demo/deliver", json={"kind": "bug"})
    runs = client.get("/api/listeners/bug-alert/logs").json()["runs"]
    assert len(runs) == 1 and runs[0]["executed"] is True
    assert client.get("/api/listeners/nope/logs").status_code == 404


def test_listener_load_errors_are_reported(listeners_dir, fake):
    (listeners_dir / "broken.py").write_text("this is not python(")
    with make_client(listeners_dir, fake) as c:
        assert "broken.py" in c.get("/api/listeners").json()["errors"]


def test_actions(client):
    r = client.post("/api/actions/archive-old-newsletters",
                    json={"params": {"days": 3650, "dry_run": True}})
    assert r.status_code == 200 and r.json()["params"] == {"days": 3650, "dry_run": True}
    assert r.json()["executed"] is False  # nothing that old
    assert client.post("/api/actions/archive-old-newsletters").status_code == 200  # defaults
    assert client.post("/api/actions/nope", json={}).status_code == 404
    bad = client.post("/api/actions/archive-old-newsletters", json={"params": {"days": "soon"}})
    assert bad.status_code == 400 and "valid integer" in bad.json()["error"]
    assert client.post("/api/actions/archive-old-newsletters",
                       json={"params": [1]}).status_code == 400


def test_sync_in_demo_mode_just_runs_waiting_listeners(client):
    r = client.post("/api/sync")
    assert r.status_code == 200
    assert r.json() == {"mailbox": "demo", "found": 0, "added": 0, "new_ids": [], "listener_runs": 0}


def test_sync_in_imap_mode(listeners_dir, fake, monkeypatch):
    monkeypatch.setenv("MAILBOX", "imap")
    config = load_config()

    def fake_sync(cfg):  # pretends one new bug report arrived from Gmail
        with closing(database.open_db(load_sample=False)) as conn:
            ids = database.add_emails(conn, [{
                "id": "<n1@x>", "from": "qa@example.com", "to": "me", "subject": "Bug: crash",
                "date": "2026-11-05T10:00:00", "body": "it crashed", "labels": ["\\\\Inbox"]}],
                pending=True)
        return {"found": 1, "added": 1, "new_ids": ids}

    with make_client(listeners_dir, fake, config, sync_fn=fake_sync) as c:
        r = c.post("/api/sync").json()
        assert r["mailbox"] == "imap" and r["new_ids"] == ["<n1@x>"] and r["listener_runs"] == 1


def test_sync_failure_is_a_502(listeners_dir, fake, monkeypatch):
    monkeypatch.setenv("MAILBOX", "imap")

    def broken_sync(cfg):
        raise OSError("network down")

    with make_client(listeners_dir, fake, load_config(), sync_fn=broken_sync) as c:
        r = c.post("/api/sync")
        assert r.status_code == 502 and "network down" in r.json()["error"]


def test_demo_deliver(client):
    r = client.post("/api/demo/deliver", json={"kind": "bug"})
    assert r.status_code == 200 and r.json()["email_id"].startswith("demo-")
    assert r.json()["listener_runs"][0]["executed"] is True
    assert client.post("/api/demo/deliver", json={"kind": "parcel"}).status_code == 400


# ---------- local-only checks ----------

def test_other_hosts_are_refused(listeners_dir, fake):
    app = create_app(load_config(), chat_turn=fake, start_watcher_thread=False,
                     listeners_dir=listeners_dir)
    with TestClient(app, base_url="http://evil.example.com") as c:
        assert c.get("/api/inbox").status_code == 403


def test_other_websites_are_refused(client):
    assert client.get("/api/inbox", headers={"Origin": "https://evil.example.com"}).status_code == 403
    assert client.post("/api/sync", headers={"Origin": "https://evil.example.com"}).status_code == 403
    assert client.get("/api/inbox", headers={"Origin": BASE}).status_code == 200  # our own page
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(WS, headers={"Origin": "https://evil.example.com"}) as ws:
            ws.receive_json()
    assert closed.value.code == 4403


# ---------- WebSocket chat ----------

def test_chat_streams_and_keeps_its_conversation(client, fake):
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "chat", "content": "hello"})
        first = receive_until(ws, "result")
        assert [m["type"] for m in first] == ["chat_started", "tool_call", "assistant_text", "result"]
        assert first[1]["summary"] == "🔎 search: urgent"
        assert first[-1]["text"] == "You said: hello" and first[-1]["session_id"] == "session-1"
        assert first[-1]["cost_usd"] == pytest.approx(0.01)

        ws.send_json({"type": "chat", "content": "and again"})
        second = receive_until(ws, "result")
        assert second[-1]["cost_usd"] == pytest.approx(0.01)  # this answer only
        assert second[-1]["session_cost_usd"] == pytest.approx(0.02)
    assert [c["resume"] for c in fake.calls] == [None, "session-1"]  # resumed the session


def test_each_browser_has_its_own_conversation(client, fake):
    with client.websocket_connect(WS) as a, client.websocket_connect(WS) as b:
        a.send_json({"type": "chat", "content": "from a"})
        receive_until(a, "result")
        b.send_json({"type": "chat", "content": "from b"})
        receive_until(b, "result")
    assert [c["resume"] for c in fake.calls] == [None, None]  # b did not continue a's chat


def test_resume_after_page_reload(client, fake):
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "chat", "content": "hi", "session_id": "old-session"})
        result = receive_until(ws, "result")[-1]
    assert fake.calls[0]["resume"] == "old-session"
    assert result["cost_usd"] is None  # earlier cost of that conversation is unknown


def test_reconnecting_browser_keeps_the_cost_total(client, fake):
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "chat", "content": "hi"})
        first = receive_until(ws, "result")[-1]
    with client.websocket_connect(WS) as ws:  # page reload: a new connection
        ws.send_json({"type": "chat", "content": "again", "session_id": first["session_id"]})
        second = receive_until(ws, "result")[-1]
    assert fake.calls[1]["resume"] == "session-1"
    assert second["cost_usd"] == pytest.approx(0.01)  # known, not "unknown"


def test_approval_while_chat_runs_in_background(client):
    from email_agent.mailbox import read_email

    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "chat", "content": "star the urgent email"})
        ask = receive_until(ws, "approval_request")[-1]
        assert ask["question"] == "Allow star of 'URGENT: Board numbers needed by 3pm today'?"

        # Claude is still waiting, yet other messages are answered:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        ws.send_json({"type": "chat", "content": "another"})
        assert "Still answering" in ws.receive_json()["message"]

        ws.send_json({"type": "approval", "id": ask["id"], "allow": True})
        assert receive_until(ws, "result")[-1]["text"] == "Starred it."

        ws.send_json({"type": "chat", "content": "star it again"})
        ask = receive_until(ws, "approval_request")[-1]
        ws.send_json({"type": "approval", "id": ask["id"], "allow": False})
        assert receive_until(ws, "result")[-1]["text"] == "Not done."
        ws.send_json({"type": "approval", "id": ask["id"], "allow": True})  # too late
        assert "No such approval" in ws.receive_json()["message"]


def test_file_write_approval_shows_the_code(listeners_dir):
    class WritesAFile(FakeClaude):
        async def __call__(self, text, *, resume, approver, emit):
            decision = await approver("Write", {"file_path": "scripts/listeners/x.py",
                                                "content": "line one\nline two"}, None)
            ok = isinstance(decision, PermissionResultAllow)
            return {"session_id": "s", "text": "written" if ok else "not written",
                    "total_cost_usd": 0.0, "is_error": False, "subtype": "success"}

    with make_client(listeners_dir, WritesAFile()) as c, c.websocket_connect(WS) as ws:
        ws.send_json({"type": "chat", "content": "write it"})
        ask = receive_until(ws, "approval_request")[-1]
        assert ask["question"] == "Allow writing scripts/listeners/x.py?"
        assert ask["details"] == ["│ line one", "│ line two"]
        ws.send_json({"type": "approval", "id": ask["id"], "allow": True})
        assert receive_until(ws, "result")[-1]["text"] == "written"


def test_budget_cap_per_conversation(listeners_dir, fake):
    config = dataclasses.replace(load_config(), max_budget_usd=0.015)
    with make_client(listeners_dir, fake, config) as c, c.websocket_connect(WS) as ws:
        for _ in range(2):  # 1 cent, then 2 cents in total
            ws.send_json({"type": "chat", "content": "hi"})
            receive_until(ws, "result")
        ws.send_json({"type": "chat", "content": "one more"})
        assert "Budget cap reached" in receive_until(ws, "error")[-1]["message"]


def test_chat_errors_are_reported(listeners_dir):
    async def broken(text, **kwargs):
        raise RuntimeError("Claude is down")

    with make_client(listeners_dir, broken) as c, c.websocket_connect(WS) as ws:
        ws.send_json({"type": "chat", "content": "hi"})
        assert "Claude is down" in receive_until(ws, "error")[-1]["message"]
        ws.send_text("not json")
        assert ws.receive_json()["message"] == "Messages must be JSON"
        ws.send_json({"type": "dance"})
        assert "Unknown message type" in ws.receive_json()["message"]
        ws.send_json({"type": "chat", "content": "  "})
        assert "needs some content" in ws.receive_json()["message"]


# ---------- live events pushed to every browser ----------

def test_new_mail_and_listener_events_reach_all_browsers(client):
    with client.websocket_connect(WS) as a, client.websocket_connect(WS) as b:
        client.post("/api/demo/deliver", json={"kind": "bug"})
        for ws in (a, b):
            events = [ws.receive_json() for _ in range(3)]
            assert [e["type"] for e in events] == ["new_mail", "listener_run", "notification"]
            assert events[0]["email"]["subject"] == "Bug report: export button does nothing"
            assert events[1]["run"]["listener"] == "bug-alert"
            assert events[2]["message"].startswith("Bug: ")


# ---------- start-up and shutdown ----------

def test_watcher_starts_and_stops_with_the_server(listeners_dir, fake, monkeypatch):
    import email_agent.server as server

    monkeypatch.setenv("MAILBOX", "imap")
    monkeypatch.setenv("EMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "not-real")
    started = {}

    def fake_start_watcher(config, on_new_mail, show):
        import threading
        stop = threading.Event()
        thread = threading.Thread(target=stop.wait)
        thread.start()
        started.update(thread=thread, stop=stop, on_new_mail=on_new_mail)
        return thread, stop

    monkeypatch.setattr(server, "start_watcher", fake_start_watcher)
    syncs = []
    app = create_app(load_config(), chat_turn=fake, listeners_dir=listeners_dir,
                     sync_fn=lambda cfg: syncs.append(1) or {"found": 0, "added": 0, "new_ids": []})
    with TestClient(app, base_url=BASE) as c:
        assert started["thread"].is_alive()
        c.get("/api/inbox")  # let the start-up catch-up sync run
        started["on_new_mail"]()  # what the watcher thread calls when Gmail says "new mail"
        assert len(syncs) == 2  # one catch-up at start + one for the new mail
    assert started["stop"].is_set() and not started["thread"].is_alive()  # stopped cleanly


# ---------- the web page (step 10) ----------

def test_page_is_served_with_strict_browser_rules(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>Email Agent</title>" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "ws://127.0.0.1:3000" in csp
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/../server.py").status_code == 404  # no escaping the folder


def test_page_never_inserts_text_as_html():
    from email_agent.server import WEB_DIR

    js = (WEB_DIR / "app.js").read_text()
    for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert risky not in js, risky
    html = (WEB_DIR / "index.html").read_text()
    assert "<script>" not in html and "onclick=" not in html  # no inline scripts (CSP blocks them)


def test_info_and_runs(client):
    info = client.get("/api/info").json()
    assert info["mailbox"] == "demo" and "bug" in info["demo_kinds"]
    assert client.get("/api/runs").json() == {"runs": []}
    client.post("/api/demo/deliver", json={"kind": "bug"})
    client.post("/api/demo/deliver", json={"kind": "receipt"})
    runs = client.get("/api/runs?limit=5").json()["runs"]
    assert [r["subject"][:10] for r in runs] == ["Your recei", "Bug report"]  # newest first


def test_new_chat_starts_a_fresh_conversation(client, fake):
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "chat", "content": "one"})
        receive_until(ws, "result")
        ws.send_json({"type": "new_chat"})
        assert ws.receive_json() == {"type": "new_chat"}
        ws.send_json({"type": "chat", "content": "two"})
        result = receive_until(ws, "result")[-1]
    assert [c["resume"] for c in fake.calls] == [None, None]
    assert result["cost_usd"] == pytest.approx(0.02)  # fake's running total; new chat starts at 0
