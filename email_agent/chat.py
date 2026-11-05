"""Chat with the email agent in the terminal, with follow-up questions.

    uv run python -m email_agent.chat

One ClaudeSDKClient stays open for the whole chat, so Claude remembers
earlier questions and answers. Type "exit" (or press Ctrl+C) to stop.
"""

import asyncio

from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, ToolUseBlock

from email_agent.agent import build_options
from email_agent.config import load_config

EXIT_WORDS = {"exit", "quit", "bye"}


def describe_tool_call(name: str, args: dict) -> str:
    """One short line per tool call, e.g. '🔎 search: invoice'."""
    short = name.split("__")[-1]  # mcp__email__search_emails -> search_emails
    if short == "search_emails":
        return f"🔎 search: {args.get('query', '')}"
    if short == "read_email":
        return f"📖 read: {', '.join(args.get('ids', []))}"
    return f"🔧 {short}: {args}"


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
    async with ClaudeSDKClient(options=build_options(config)) as client:
        await chat_loop(client, budget_usd=config.max_budget_usd)


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # Ctrl+C while the agent is answering
        print("\nBye!")


if __name__ == "__main__":
    run()
