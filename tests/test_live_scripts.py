"""Live tests: the chat agent writes a listener, and the hook blocks a write
outside the allowed folders. Runs in a temporary copy of the project, so your
real scripts/ folder is never touched. About 5-10 cents. Run with --live."""

import shutil

import pytest
from claude_agent_sdk import ClaudeSDKClient

from email_agent.agent import build_options
from email_agent.chat import answer_one, make_approver
from email_agent.config import PROJECT_ROOT
from email_agent.listen import DEMO_EMAILS
from email_agent.listeners import ListenerRegistry, deliver_demo_email

pytestmark = pytest.mark.live


@pytest.fixture
def project(tmp_path):
    """A small copy of the project: the skill and empty script folders."""
    shutil.copytree(PROJECT_ROOT / ".claude" / "skills", tmp_path / ".claude" / "skills")
    (tmp_path / "scripts" / "listeners").mkdir(parents=True)
    (tmp_path / "scripts" / "actions").mkdir(parents=True)
    (tmp_path / "email_agent").mkdir()
    return tmp_path


async def _ask(project, question):
    asked, tools, shown = [], [], []

    def say_yes(prompt):
        asked.append(prompt)
        return "y"

    options = build_options(approver=make_approver(say_yes, shown.append, root=project),
                            root=project)
    async with ClaudeSDKClient(options=options) as client:
        result = await answer_one(client, question, show=tools.append)
    return result, asked, tools


async def test_agent_writes_a_working_bug_report_listener(project):
    result, asked, tools = await _ask(project, "Notify me when a bug report arrives.")
    written = list((project / "scripts" / "listeners").glob("*.py"))
    assert len(written) == 1, (tools, result.result)
    assert any("Allow writing scripts/listeners/" in q for q in asked)

    registry = ListenerRegistry(project / "scripts" / "listeners")
    assert len(registry.enabled()) == 1, registry.errors  # loads without a restart

    shown = []
    _, [bug] = await deliver_demo_email(**DEMO_EMAILS["bug"], registry=registry, show=shown.append)
    assert bug["executed"] is True, bug
    assert any(s.startswith("🔔") for s in shown)

    _, [receipt] = await deliver_demo_email(**DEMO_EMAILS["receipt"], registry=registry,
                                            show=shown.append)
    assert receipt["executed"] is False, receipt


async def test_hook_blocks_python_outside_allowed_folders(project):
    result, asked, tools = await _ask(
        project, "Create the Python file email_agent/hello.py containing print('hi').")
    # The agent did try (so it was the hook that stopped it, not the agent declining)...
    assert any("write:" in t and "email_agent/hello.py" in t for t in tools), tools
    # ...and the file was never created.
    assert not (project / "email_agent" / "hello.py").exists()
    assert not list(project.rglob("hello.py"))  # not sneaked in anywhere else either
    assert not any("email_agent/hello.py" in q for q in asked)  # blocked before any y/n
