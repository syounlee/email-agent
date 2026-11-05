"""Chat with the email agent in the terminal, with follow-up questions.

    uv run python -m email_agent.chat

One ClaudeSDKClient stays open for the whole chat, so Claude remembers
earlier questions and answers. Type "exit" (or press Ctrl+C) to stop.

Before any change to your mail (archive, star, labels...), the chat asks
"Allow ...? (y/n)". Only "y" or "yes" lets it run.
"""

import asyncio
import warnings

from claude_agent_sdk import (
    AssistantMessage, CanUseToolShadowedWarning, ClaudeSDKClient, PermissionResultAllow,
    PermissionResultDeny, ResultMessage, ToolUseBlock,
)

from email_agent.actions import approval_question
from email_agent.agent import build_options
from email_agent.config import load_config
from email_agent.mailbox import prepare
from email_agent.tools import READ_TOOLS, TOOL_TO_ACTION

# The SDK warns that search/read skip the approval check. That's on purpose.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

EXIT_WORDS = {"exit", "quit", "bye"}


def describe_tool_call(name: str, args: dict) -> str:
    """One short line per tool call, e.g. '🔎 search: invoice'."""
    short = name.split("__")[-1]  # mcp__email__search_emails -> search_emails
    if short == "search_emails":
        return f"🔎 search: {args.get('query', '')}"
    if short == "read_email":
        return f"📖 read: {', '.join(args.get('ids', []))}"
    if "label" in args:
        return f"✏️  {short}: {args.get('id', '')} → {args['label']}"
    return f"✏️  {short}: {args.get('id', '')}"


def make_approver(read_input=input, show=print, question_for=approval_question):
    """Build the can_use_tool callback: ask the user before any mail change."""

    async def approve(tool_name: str, tool_input: dict, context):
        if tool_name in READ_TOOLS:  # normally never asked (they're pre-allowed)
            return PermissionResultAllow()
        action = TOOL_TO_ACTION.get(tool_name)
        if action is None:
            return PermissionResultDeny(message=f"{tool_name} is not allowed.")

        question = question_for(action, tool_input.get("id", ""), tool_input.get("label"))
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


async def chat_loop(client, read_input=input, show=print, budget_usd: float | None = None):
    """Ask, answer, repeat, until the user types exit or presses Ctrl+C."""
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
          'Type "exit" or press Ctrl+C to stop.')
    prepare(config)
    options = build_options(config, approver=make_approver())
    async with ClaudeSDKClient(options=options) as client:
        await chat_loop(client, budget_usd=config.max_budget_usd)


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # Ctrl+C while the agent is answering
        print("\nBye!")


if __name__ == "__main__":
    run()
