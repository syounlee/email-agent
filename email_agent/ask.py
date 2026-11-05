"""Ask the email agent one question from the command line.

    uv run python -m email_agent.ask "Which invoices are overdue?"
"""

import asyncio
import sys
from dataclasses import dataclass, field

from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock, query

from email_agent.agent import build_options
from email_agent.mailbox import prepare


@dataclass
class Answer:
    text: str = ""
    cost_usd: float = 0.0
    error: bool = False
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)  # (name, input)


async def ask(question: str, options=None) -> Answer:
    """Run one query() and collect the answer, cost and tool calls."""
    answer = Answer()
    async for message in query(prompt=question, options=options or build_options()):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    answer.tool_calls.append((block.name, block.input))
        elif isinstance(message, ResultMessage):
            answer.text = message.result or ""
            answer.cost_usd = message.total_cost_usd or 0.0
            answer.error = message.is_error
            if message.is_error and not answer.text:
                answer.text = f"Stopped early: {message.subtype}"
    return answer


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: uv run python -m email_agent.ask "your question"')
        sys.exit(1)
    prepare()
    answer = asyncio.run(ask(" ".join(sys.argv[1:])))
    for name, args in answer.tool_calls:
        print(f"  [tool] {name.split('__')[-1]} {args}")
    print()
    print(answer.text)
    print(f"\nCost: ${answer.cost_usd:.4f}")
    if answer.error:
        sys.exit(1)


if __name__ == "__main__":
    main()
