"""One Claude call that returns JSON matching a schema (for listeners).

Uses the Anthropic Messages API with *structured outputs*: we send a JSON
schema in output_config.format, and Claude's reply is guaranteed to be valid
JSON in that shape. No tool calls, no forced tool_choice.
"""

import copy
import json

import anthropic

from email_agent.config import load_config

# Short names -> exact model ids, with price per million tokens (input, output)
MODELS = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}
PRICES_PER_MILLION = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


class ClaudeCallError(Exception):
    """Claude didn't return usable JSON (refused, cut off, ...)."""


def strict_schema(schema: dict) -> dict:
    """Copy of the schema with additionalProperties: false on EVERY object.

    Structured outputs require it, and this way a listener author can't forget.
    """
    schema = copy.deepcopy(schema)

    def fix(node):
        """Walk the schema's own structure (not every dict), fixing objects."""
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
        for child in node.get("properties", {}).values():
            fix(child)
        for key in ("items", "not"):
            fix(node.get(key))
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for child in node.get(key, []):
                fix(child)
        for key in ("$defs", "definitions"):
            for child in node.get(key, {}).values():
                fix(child)

    fix(schema)
    return schema


def cost_usd(model_id: str, usage) -> float:
    """Dollar cost of one call from its token counts (0 for unknown models)."""
    price_in, price_out = PRICES_PER_MILLION.get(model_id, (0.0, 0.0))
    tokens_in = (usage.input_tokens or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0) \
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    return (tokens_in * price_in + (usage.output_tokens or 0) * price_out) / 1_000_000


async def call_claude(prompt: str, schema: dict, model: str = "haiku", *,
                      client=None, max_tokens: int = 2048) -> tuple[dict, dict]:
    """Ask Claude once; return (json_answer, call_info).

    call_info has the model, token counts and cost, for the run log.
    """
    model_id = MODELS.get(model, model)
    if client is None:
        config = load_config()
        client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key or None)

    response = await client.messages.create(
        model=model_id,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        output_config={
            "effort": "low",  # simple classification: keep thinking short and cheap
            "format": {"type": "json_schema", "schema": strict_schema(schema)},
        },
    )

    info = {
        "model": model_id,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cost_usd": round(cost_usd(model_id, response.usage), 6),
    }
    if response.stop_reason == "refusal":
        raise ClaudeCallError("Claude declined this request.")
    if response.stop_reason == "max_tokens":
        raise ClaudeCallError("Claude's answer was cut off (max_tokens).")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ClaudeCallError("Claude returned no text.")
    return json.loads(text), info
