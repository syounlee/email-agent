"""Chat with the email agent in the terminal, with follow-up questions.

    uv run python -m email_agent.chat

One ClaudeSDKClient stays open for the whole chat, so Claude remembers
earlier questions and answers. Type "exit" (or press Ctrl+C) to stop.

Before any change to your mail (archive, star, labels...) and before the
agent writes a file, the chat asks "Allow ...? (y/n)". Only "y" or "yes"
lets it run.

Commands (handled here, not sent to Claude, so they cost nothing):
    /run <action_id> key=value ...   run a one-click action
    /actions                         list the actions
    /deliver <kind>                  demo mode: pretend an email arrived
    /help                            this list
"""

import asyncio
import warnings
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage, CanUseToolShadowedWarning, ClaudeSDKClient, PermissionResultAllow,
    PermissionResultDeny, ResultMessage, ToolUseBlock,
)

from email_agent.actions import approval_question
from email_agent.agent import build_options
from email_agent.config import PROJECT_ROOT, load_config
from email_agent.listen import DEMO_EMAILS
from email_agent.listeners import ListenerRegistry, deliver_demo_email
from email_agent.mailbox import prepare
from email_agent.tools import READ_TOOLS, TOOL_TO_ACTION
from email_agent.user_actions import ActionInputError, ActionRegistry, describe, run_action

# The SDK warns that search/read skip the approval check. That's on purpose.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

EXIT_WORDS = {"exit", "quit", "bye"}
FILE_TOOLS = ("Write", "Edit")
PREVIEW_LINES = 40
HELP = """Commands:
  /run <action_id> key=value ...   run a one-click action (/actions lists them)
  /deliver <kind>                  demo mode: pretend a new email arrived
                                   (kinds: """ + ", ".join(DEMO_EMAILS) + """)
  exit                             leave the chat"""


def describe_tool_call(name: str, args: dict) -> str:
    """One short line per tool call, e.g. '🔎 search: invoice'."""
    short = name.split("__")[-1]  # mcp__email__search_emails -> search_emails
    if name in FILE_TOOLS:
        return f"📝 {name.lower()}: {args.get('file_path', '')}"
    if name == "Read":
        return f"📄 read file: {args.get('file_path', '')}"
    if name == "Glob":
        return f"🗂️  glob: {args.get('pattern', '')}"
    if name == "Skill":
        return f"🧩 skill: {args.get('skill', args.get('name', ''))}"
    if short == "search_emails":
        return f"🔎 search: {args.get('query', '')}"
    if short == "read_email":
        return f"📖 read: {', '.join(args.get('ids', []))}"
    if "label" in args:
        return f"✏️  {short}: {args.get('id', '')} → {args['label']}"
    return f"✏️  {short}: {args.get('id', '')}"


def _short_path(raw: str, root: Path) -> str:
    try:
        return str(Path(raw).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return raw


def _preview(lines: list[str], prefix: str, show) -> None:
    for line in lines[:PREVIEW_LINES]:
        show(f"   {prefix} {line}")
    if len(lines) > PREVIEW_LINES:
        show(f"   {prefix} ... ({len(lines) - PREVIEW_LINES} more lines)")


def _file_question(tool_name: str, tool_input: dict, show, root: Path) -> str:
    """Show what the agent wants to write, and return the y/n question."""
    path = _short_path(tool_input.get("file_path", ""), root)
    if tool_name == "Write":
        _preview(tool_input.get("content", "").splitlines(), "│", show)
        return f"Allow writing {path}?"
    _preview(tool_input.get("old_string", "").splitlines(), "-", show)
    _preview(tool_input.get("new_string", "").splitlines(), "+", show)
    return f"Allow editing {path}?"


def make_approver(read_input=input, show=print, question_for=approval_question,
                  root: Path = PROJECT_ROOT):
    """Build the can_use_tool callback: ask the user before any change."""

    async def approve(tool_name: str, tool_input: dict, context):
        if tool_name in READ_TOOLS:  # normally never asked (they're pre-allowed)
            return PermissionResultAllow()
        if tool_name in FILE_TOOLS:
            question = _file_question(tool_name, tool_input, show, root)
        elif tool_name in TOOL_TO_ACTION:
            question = question_for(TOOL_TO_ACTION[tool_name], tool_input.get("id", ""),
                                    tool_input.get("label"))
        else:
            return PermissionResultDeny(message=f"{tool_name} is not allowed.")

        try:
            reply = read_input(f"   ❓ {question} (y/n) ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            reply = "n"
        if reply in ("y", "yes"):
            return PermissionResultAllow()
        show("   🚫 not done")
        return PermissionResultDeny(
            message="The user declined this change. Nothing was changed. "
                    "Tell them it was not done and do not retry unless they ask."
        )

    return approve


async def answer_one(client, question: str, show=print) -> ResultMessage | None:
    """Send one question, print tool calls as they happen, return the result."""
    await client.query(question)
    result = None
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    show("   " + describe_tool_call(block.name, block.input))
        elif isinstance(message, ResultMessage):
            result = message
    return result


def make_command_handler(show=print, listeners: ListenerRegistry | None = None,
                         actions: ActionRegistry | None = None):
    """Handle /run, /actions, /deliver and /help. Returns True if the line was a command."""
    listeners = listeners or ListenerRegistry(show=show)
    actions = actions or ActionRegistry(show=show)

    async def handle(line: str) -> bool:
        if not line.startswith("/"):
            return False
        word, _, rest = line.partition(" ")
        if word == "/run" and rest.strip():
            try:
                entry = await run_action(line, actions, show)
            except ActionInputError as problem:
                show(f"⚠️  {problem}")
                return True
            icon = "✅" if entry["executed"] else ("❌" if "error" in entry else "➖")
            show(f"{icon} {entry['action']}: {entry.get('error') or entry['reason']}")
        elif word in ("/run", "/actions"):
            show("Actions:\n" + describe(actions))
        elif word == "/deliver":
            kind = rest.strip()
            if load_config().mailbox != "demo":
                show("⚠️  /deliver only works with the demo mailbox (MAILBOX=demo).")
            elif kind not in DEMO_EMAILS:
                show(f"⚠️  Choose one of: {', '.join(DEMO_EMAILS)}")
            else:
                await deliver_demo_email(**DEMO_EMAILS[kind], registry=listeners, show=show)
        elif word == "/help":
            show(HELP)
        else:
            show(f"⚠️  Unknown command {word}. Try /help.")
        return True

    return handle


async def chat_loop(client, read_input=input, show=print, budget_usd: float | None = None,
                    commands=None):
    """Ask, answer, repeat, until the user types exit or presses Ctrl+C.

    `commands`: optional handler for /run etc. (see make_command_handler).
    """
    session_cost = 0.0  # Claude reports a running total for the whole chat
    while True:
        try:
            question = read_input("\nyou> ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not question:
            continue
        if question.lower() in EXIT_WORDS:
            break
        if commands and await commands(question):
            continue

        result = await answer_one(client, question, show)
        if result is None:
            show("(no answer received)")
            continue

        new_total = result.total_cost_usd or session_cost
        show(f"\nagent> {result.result or ''}".rstrip())
        show(f"   💲 this answer ${new_total - session_cost:.4f} · chat total ${new_total:.4f}")
        session_cost = new_total

        if result.subtype == "error_max_budget_usd":
            show(f"\nBudget cap reached (${budget_usd}). Ending the chat. "
                 "Raise MAX_BUDGET_USD in .env if you need more.")
            break
        if result.is_error:
            show(f"   (stopped early: {result.subtype})")
    show("Bye!")
    return session_cost


async def main() -> None:
    config = load_config()
    print(f"Email agent chat ({config.model}, budget ${config.max_budget_usd:.2f}). "
          'Type "exit" or press Ctrl+C to stop, /help for commands.')
    prepare(config)
    options = build_options(config, approver=make_approver())
    async with ClaudeSDKClient(options=options) as client:
        await chat_loop(client, budget_usd=config.max_budget_usd,
                        commands=make_command_handler())


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # Ctrl+C while the agent is answering
        print("\nBye!")


if __name__ == "__main__":
    run()
