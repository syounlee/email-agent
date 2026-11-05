"""The PreToolUse hook: decides which files the chat agent may touch.

It runs BEFORE every Read, Write, Edit and Glob call (even before the y/n
question) and can block it. Rules:

1. Python files (.py) may only be written in scripts/listeners/ or
   scripts/actions/.
2. Nothing in .claude/ may be written (the agent can't change its own skill
   or settings).
3. .env files (your passwords and keys) can't be read or written at all.
4. Nothing outside this project folder can be read or written.
"""

from pathlib import Path

from claude_agent_sdk import HookMatcher

from email_agent.core.config import PROJECT_ROOT

SCRIPT_DIRS = ("scripts/listeners", "scripts/actions")
WRITE_TOOLS = ("Write", "Edit")
GUARDED_TOOLS = "Read|Write|Edit|Glob"


def _is_env_file(path: Path) -> bool:
    return path.name.startswith(".env") and path.name != ".env.example"


def check_file_access(tool_name: str, tool_input: dict, root: Path = PROJECT_ROOT) -> str | None:
    """None if allowed, otherwise the reason it's blocked (shown to Claude)."""
    root = Path(root).resolve()

    if tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        if pattern.startswith(("/", "~")) or ".." in Path(pattern).parts:
            return "Blocked: Glob patterns must stay inside the project folder."
        raw = tool_input.get("path") or "."
    else:
        raw = tool_input.get("file_path") or ""
        if not raw:
            return None  # nothing to check; the tool itself will complain

    # Resolve "..", "~" and symlinks, so tricks like scripts/listeners/../../x.py fail.
    path = Path(raw).expanduser()
    path = (path if path.is_absolute() else root / path).resolve()

    if not path.is_relative_to(root):
        return f"Blocked: {raw} is outside the project folder."
    relative = path.relative_to(root)

    if _is_env_file(path):
        return "Blocked: .env holds passwords and keys; it can't be read or changed."

    if tool_name in WRITE_TOOLS:
        if relative.parts and relative.parts[0] == ".claude":
            return "Blocked: files in .claude/ (skills and settings) can't be changed by the agent."
        if path.suffix == ".py" and not any(
            relative.is_relative_to(folder) for folder in SCRIPT_DIRS
        ):
            return (f"Blocked: Python files may only be written in "
                    f"{' or '.join(d + '/' for d in SCRIPT_DIRS)}, not {relative}.")
    return None


def make_file_guard(root: Path = PROJECT_ROOT):
    """The hook callback, in the shape the Agent SDK expects."""

    async def file_guard(input_data, tool_use_id, context):
        reason = check_file_access(input_data["tool_name"], input_data["tool_input"], root)
        if reason is None:
            return {}  # no opinion: carry on to the normal permission check
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return file_guard


def guard_hooks(root: Path = PROJECT_ROOT) -> dict:
    """The `hooks` option for ClaudeAgentOptions."""
    # on_failure="block": if the guard itself crashes, the tool call is blocked too.
    return {"PreToolUse": [HookMatcher(matcher=GUARDED_TOOLS, hooks=[make_file_guard(root)],
                                       on_failure="block")]}
