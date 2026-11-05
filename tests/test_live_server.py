"""Live test: chat with the real Claude through the web server's WebSocket,
including a follow-up and an approval. About 2-3 cents. Run with --live."""

import pytest
from starlette.testclient import TestClient

from email_agent.config import load_config
from email_agent.mailbox import read_email
from email_agent.server import create_app

pytestmark = pytest.mark.live


def receive_until(ws, kind):
    seen = []
    while True:
        message = ws.receive_json()
        seen.append(message)
        if message["type"] in (kind, "error"):
            return seen


def test_real_chat_follow_up_and_approval(tmp_path):
    (tmp_path / "listeners").mkdir()
    app = create_app(load_config(), start_watcher_thread=False, listeners_dir=tmp_path / "listeners")
    with TestClient(app, base_url="http://127.0.0.1:3000") as c, \
            c.websocket_connect("ws://127.0.0.1:3000/ws") as ws:
        ws.send_json({"type": "chat", "content": "Who sent the urgent email?"})
        first = receive_until(ws, "result")
        assert first[-1]["type"] == "result" and "dana" in first[-1]["text"].lower()
        assert any(m["type"] == "tool_call" for m in first)

        ws.send_json({"type": "chat", "content": "Star that email."})  # needs the first answer
        ask = receive_until(ws, "approval_request")[-1]
        assert ask["type"] == "approval_request" and "URGENT" in ask["question"]
        ws.send_json({"type": "approval", "id": ask["id"], "allow": True})
        second = receive_until(ws, "result")[-1]
        assert second["type"] == "result" and second["session_id"] == first[-1]["session_id"]
        assert 0 < second["cost_usd"] < second["session_cost_usd"]
    assert read_email("e001")[0]["starred"] is True
