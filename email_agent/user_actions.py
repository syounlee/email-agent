"""One-click actions: scripts in scripts/actions/ that you run from the chat.

    /run archive-old-newsletters days=7 dry_run=true

Each action file has:

    config = {"id": "...", "name": "...",
              "parameters": {"days": {"type": "integer", "default": 30}}}

    async def handler(params, context):
        return {"executed": True, "reason": "what was done"}

Runs are logged to DATA_DIR/action_runs-<mailbox>.jsonl. Files reload automatically.
"""

import shlex
import time
from datetime import datetime
from pathlib import Path

from email_agent.config import PROJECT_ROOT, load_config
from email_agent.listeners import (
    ScriptContext, ScriptRegistry, check_result, usage_summary, write_log,
)

ACTIONS_DIR = PROJECT_ROOT / "scripts" / "actions"
LOG_FILENAME = "action_runs-{mailbox}.jsonl"  # one log per mailbox
TYPES = ("string", "integer", "number", "boolean")


def action_log_path() -> Path:
    config = load_config()
    return config.data_dir / LOG_FILENAME.format(mailbox=config.mailbox)


class ActionInputError(Exception):
    """The /run command or its parameters are wrong. Safe to show the user."""


def _check_action(module) -> None:
    config = module.config
    missing = {"id", "name", "parameters"} - config.keys()
    if missing:
        raise ValueError(f"config is missing {sorted(missing)}")
    if not isinstance(config["parameters"], dict):
        raise ValueError("parameters must be a dict")
    for name, spec in config["parameters"].items():
        if spec.get("type") not in TYPES:
            raise ValueError(f"parameter {name!r} needs a type: one of {TYPES}")


class ActionRegistry(ScriptRegistry):
    def __init__(self, folder: Path = ACTIONS_DIR, show=print):
        super().__init__(folder, _check_action, show)

    def get(self, action_id: str):
        for module in self.all():
            if module.config["id"] == action_id:
                return module
        known = ", ".join(sorted(m.config["id"] for m in self.all())) or "none yet"
        raise ActionInputError(f"No action {action_id!r}. Available: {known}")


def parse_run_command(text: str) -> tuple[str, dict[str, str]]:
    """'/run my-id days=7 note="two words"' -> ('my-id', {'days': '7', 'note': 'two words'})"""
    try:
        parts = shlex.split(text)
    except ValueError as problem:  # e.g. an unclosed quote
        raise ActionInputError(f"Couldn't read that command: {problem}")
    if parts[:1] != ["/run"] or len(parts) < 2:
        raise ActionInputError("Use: /run <action_id> key=value ...")
    raw = {}
    for part in parts[2:]:
        if "=" not in part:
            raise ActionInputError(f"'{part}' should look like key=value")
        key, value = part.split("=", 1)
        raw[key] = value
    return parts[1], raw


def _convert(name: str, kind: str, value):
    # Values from JSON (the web server) may already have the right type.
    if not isinstance(value, str):
        if kind == "boolean" and isinstance(value, bool):
            return value
        if kind == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return value
        if kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        value = str(value)
    try:
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
        if kind == "boolean":
            lowered = value.lower()
            if lowered in ("true", "yes", "1", "y"):
                return True
            if lowered in ("false", "no", "0", "n"):
                return False
            raise ValueError
        return value
    except ValueError:
        raise ActionInputError(f"{name}={value!r} is not a valid {kind}")


def build_params(spec: dict, raw: dict[str, str]) -> dict:
    """Turn the typed-in key=value strings into proper values, with defaults."""
    unknown = set(raw) - set(spec)
    if unknown:
        raise ActionInputError(f"Unknown parameter(s): {', '.join(sorted(unknown))}. "
                               f"This action takes: {', '.join(spec) or 'nothing'}")
    params = {}
    for name, info in spec.items():
        if name in raw:
            params[name] = _convert(name, info["type"], raw[name])
        elif "default" in info:
            params[name] = info["default"]
        else:
            raise ActionInputError(f"Missing required parameter {name}=...")
    return params


def describe(registry: ActionRegistry) -> str:
    """A short list of the available actions and their parameters."""
    lines = []
    for module in sorted(registry.all(), key=lambda m: m.config["id"]):
        params = " ".join(
            f"{n}={info.get('default', '<' + info['type'] + '>')}"
            for n, info in module.config["parameters"].items())
        lines.append(f"  /run {module.config['id']} {params}  — {module.config['name']}")
    return "\n".join(lines) or "  (no actions yet: ask me to write one)"


async def run_action(command: str, registry: ActionRegistry | None = None, show=print,
                     claude_client=None) -> dict:
    """Run '/run <id> key=value ...'. Returns (and logs) one entry."""
    action_id, raw = parse_run_command(command)
    return await run_action_with_params(action_id, raw, registry, show, claude_client)


async def run_action_with_params(action_id: str, raw: dict, registry: ActionRegistry | None = None,
                                 show=print, claude_client=None) -> dict:
    """Run one action with parameters given as a dict (strings or proper values)."""
    registry = registry or ActionRegistry(show=show)
    module = registry.get(action_id)
    params = build_params(module.config["parameters"], raw)

    context = ScriptContext(action_id, show, claude_client)
    started = time.monotonic()
    entry = {"time": datetime.now().isoformat(timespec="seconds"), "action": action_id,
             "params": params}
    try:
        result = check_result(await module.handler(dict(params), context))
        entry.update(executed=result["executed"], reason=result["reason"])
    except Exception as problem:
        entry.update(executed=False, reason="error", error=f"{type(problem).__name__}: {problem}")
    entry.update(changes=context.actions, **usage_summary(context),
                 duration_ms=round((time.monotonic() - started) * 1000))
    write_log(entry, action_log_path())
    return entry
