"""Tests for auto-start (launchd) and the one-watcher lock. Free; never touches your
real login items: launchctl is faked and the LaunchAgents folder is temporary."""

import plistlib
from types import SimpleNamespace

import pytest

from email_agent.apps import autostart
from email_agent.core.watch_lock import WatchLock


class FakeLaunchctl:
    def __init__(self, loaded=False):
        self.calls, self.loaded = [], loaded

    def __call__(self, args, **kwargs):
        self.calls.append(args[1])
        if args[1] == "print":
            return SimpleNamespace(returncode=0 if self.loaded else 113,
                                   stdout="state = running" if self.loaded else "", stderr="")
        if args[1] == "bootstrap":
            self.loaded = True
        if args[1] == "bootout":
            self.loaded = False
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def gmail(monkeypatch):
    monkeypatch.setenv("MAILBOX", "imap")


def test_plist_runs_this_projects_email_watch(tmp_path):
    plist = autostart.build_plist(root=tmp_path)
    assert plist["ProgramArguments"] == [str(tmp_path / ".venv" / "bin" / "email-watch")]
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["WorkingDirectory"] == str(tmp_path)
    assert plist["StandardOutPath"].endswith("logs/email-watch.log")
    awake = autostart.build_plist(keep_awake=True, root=tmp_path)
    assert awake["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-i"]


def test_on_then_off(tmp_path, gmail):
    (tmp_path / "project" / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / "project" / ".venv" / "bin" / "email-watch").touch()
    launchctl, agents = FakeLaunchctl(), tmp_path / "LaunchAgents"

    path = autostart.turn_on(run=launchctl, agents_dir=agents, root=tmp_path / "project")
    assert plistlib.loads(path.read_bytes())["Label"] == "local.email-agent.watch"
    assert launchctl.calls == ["print", "bootstrap"] and launchctl.loaded
    assert "Auto-start is ON" in autostart.status_text(run=launchctl, agents_dir=agents)

    assert autostart.turn_off(run=launchctl, agents_dir=agents) is True
    assert not path.exists() and not launchctl.loaded
    assert "Auto-start is OFF" in autostart.status_text(run=launchctl, agents_dir=agents)
    assert autostart.turn_off(run=launchctl, agents_dir=agents) is False


def test_on_twice_replaces_the_old_one(tmp_path, gmail):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "email-watch").touch()
    launchctl = FakeLaunchctl(loaded=True)
    autostart.turn_on(run=launchctl, agents_dir=tmp_path / "LA", root=tmp_path)
    assert launchctl.calls == ["print", "bootout", "bootstrap"]


def test_on_needs_gmail_and_an_installed_project(tmp_path):
    with pytest.raises(RuntimeError, match="uv sync"):
        autostart.turn_on(run=FakeLaunchctl(), agents_dir=tmp_path, root=tmp_path)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "email-watch").touch()
    with pytest.raises(RuntimeError, match="MAILBOX=imap"):  # tests run with MAILBOX=demo
        autostart.turn_on(run=FakeLaunchctl(), agents_dir=tmp_path, root=tmp_path)


def test_only_one_watcher_at_a_time():
    first, second = WatchLock(), WatchLock()
    assert first.acquire() and first.holder() is not None
    assert second.acquire() is False      # someone is watching already
    first.release()
    assert second.acquire() is True       # now it's free
    second.release()
    assert WatchLock().holder() is None


def test_web_page_leaves_watching_to_email_watch(monkeypatch, tmp_path):
    """If email-watch already holds the lock, the page starts no second watcher."""
    from starlette.testclient import TestClient

    from email_agent.apps import server
    from email_agent.core.config import load_config

    monkeypatch.setenv("MAILBOX", "imap")
    monkeypatch.setenv("EMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "not-real")
    started = []
    monkeypatch.setattr(server, "start_watcher", lambda *a, **k: started.append(1))
    held = WatchLock()
    assert held.acquire()
    try:
        app = server.create_app(load_config(), chat_turn=lambda *a, **k: None,
                                sync_fn=lambda c: {"found": 0, "added": 0, "new_ids": []})
        with TestClient(app, base_url="http://127.0.0.1:3000") as client:
            assert client.get("/api/alerts").status_code == 200
        assert started == []
    finally:
        held.release()
