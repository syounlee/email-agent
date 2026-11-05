"""Offline tests for the chat loop, using a fake client instead of Claude."""

from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

from email_agent.chat import chat_loop, describe_tool_call


def test_describe_tool_call():
    assert describe_tool_call("mcp__email__search_emails", {"query": "invoice"}) == "🔎 search: invoice"
    assert describe_tool_call("mcp__email__read_email", {"ids": ["e001", "e003"]}) == "📖 read: e001, e003"


def _result(text, total_cost, subtype="success", is_error=False):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id="s", total_cost_usd=total_cost, result=text)


class FakeClient:
    """Plays back scripted replies, one list of messages per question."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.questions = []

    async def query(self, prompt):
        self.questions.append(prompt)

    async def receive_response(self):
        for message in self.replies.pop(0):
            yield message


def _inputs(*lines):
    it = iter(lines)
    return lambda prompt="": next(it)


async def test_chat_shows_tools_answers_and_cost_then_exits():
    client = FakeClient([
        [AssistantMessage(content=[ToolUseBlock(id="1", name="mcp__email__search_emails",
                                                input={"query": "urgent"})], model="m"),
         _result("Dana sent it.", 0.010)],
        [_result("Board numbers by 3pm.", 0.016)],  # running total, as Claude reports it
    ])
    out = []
    total = await chat_loop(client, _inputs("Who sent the urgent email?", "",
                                            "What exactly do they need?", "exit"), out.append)

    assert client.questions == ["Who sent the urgent email?", "What exactly do they need?"]
    text = "\n".join(out)
    assert "🔎 search: urgent" in text
    assert "Dana sent it." in text and "Board numbers by 3pm." in text
    assert "this answer $0.0100 · chat total $0.0100" in text
    assert "this answer $0.0060 · chat total $0.0160" in text  # per-answer = difference
    assert total == 0.016
    assert out[-1] == "Bye!"


async def test_ctrl_c_at_prompt_stops_cleanly():
    def press_ctrl_c(prompt=""):
        raise KeyboardInterrupt

    out = []
    await chat_loop(FakeClient([]), press_ctrl_c, out.append)
    assert out == ["Bye!"]


async def test_chat_ends_when_budget_cap_is_hit():
    client = FakeClient([[_result("", 0.51, subtype="error_max_budget_usd", is_error=True)]])
    out = []
    await chat_loop(client, _inputs("a big question", "never asked"), out.append, budget_usd=0.5)
    assert any("Budget cap reached" in line for line in out)
    assert client.questions == ["a big question"]


# ---------- asking before changes ----------

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from email_agent.chat import make_approver


def _approver(reply):
    asked, shown = [], []

    def read_input(prompt):
        asked.append(prompt)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    approve = make_approver(read_input, shown.append,
                            question_for=lambda action, i, label: f"Allow {action} of 'S'?")
    return approve, asked, shown


async def test_yes_allows_the_change():
    approve, asked, _ = _approver("y")
    result = await approve("mcp__email__archive_email", {"id": "e001"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert asked == ["   ❓ Allow archive of 'S'? (y/n) "]


async def test_anything_but_yes_declines():
    for reply in ["n", "", "maybe", "no", KeyboardInterrupt()]:
        approve, _, shown = _approver(reply)
        result = await approve("mcp__email__star_email", {"id": "e001"}, None)
        assert isinstance(result, PermissionResultDeny)
        assert "declined" in result.message


async def test_unknown_tools_are_denied_without_asking():
    approve, asked, _ = _approver("y")
    result = await approve("Bash", {"command": "rm -rf /"}, None)
    assert isinstance(result, PermissionResultDeny)
    assert asked == []


def test_describe_change_tool_calls():
    assert describe_tool_call("mcp__email__star_email", {"id": "e001"}) == "✏️  star_email: e001"
    assert describe_tool_call("mcp__email__add_label", {"id": "e4", "label": "Books"}) == \
        "✏️  add_label: e4 → Books"
