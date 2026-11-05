"""Wrap the mailbox functions as tools Claude can call.

Each tool gets the same input Claude sends (a dict) and returns text.
The tools live in an in-process "MCP server" named "email", so Claude sees
them as  mcp__email__search_emails  and  mcp__email__read_email.
"""

import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from email_agent import mailbox

SERVER_NAME = "email"
SEARCH_TOOL = f"mcp__{SERVER_NAME}__search_emails"
READ_TOOL = f"mcp__{SERVER_NAME}__read_email"
EMAIL_TOOLS = [SEARCH_TOOL, READ_TOOL]


def _as_text(data) -> dict:
    """Package Python data as the text reply a tool must return."""
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]}


@tool(
    "search_emails",
    "Search the mailbox. ALWAYS use this first to find emails relevant to the "
    "question. Gmail-style syntax; all parts must match: plain words (matched "
    'in subject, sender or body; "invoices" also matches "invoice"), '
    '"exact phrase", from:name (part of the sender address), subject:word, '
    "is:unread, is:read, is:starred, newer_than:7d (d/w/m/y). "
    "Example: 'invoice from:billing is:unread'. Results are newest first and "
    "are only short previews (id, from, subject, date, snippet), not full "
    "text. Start with one or two distinctive keywords; if nothing comes back, "
    "try different or fewer words. Then call read_email on the promising ids "
    "before answering.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Gmail-style search, e.g. 'invoice is:unread'."},
            "limit": {"type": "integer", "description": "Max results (default 10).", "default": 10},
        },
        "required": ["query"],
    },
    annotations={"readOnlyHint": True},
)
async def search_emails_tool(args: dict) -> dict:
    try:
        results = mailbox.search_emails(args["query"], limit=args.get("limit", 10))
    except ValueError as problem:  # e.g. is:important, which we don't support
        return _as_text({"error": str(problem)})
    if not results:
        return _as_text({"results": [], "note": "No emails matched. Try other keywords."})
    return _as_text({"results": results})


@tool(
    "read_email",
    "Read the full text of emails by id. Use this AFTER search_emails, on the "
    "ids whose previews look relevant, before answering. Snippets alone are "
    "not enough to answer. You can pass several ids at once.",
    {
        "type": "object",
        "properties": {
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Email ids from search_emails, e.g. ['e003'].",
            },
        },
        "required": ["ids"],
    },
    annotations={"readOnlyHint": True},
)
async def read_email_tool(args: dict) -> dict:
    emails = mailbox.read_email(args["ids"])
    missing = [i for i in args["ids"] if i not in {e["id"] for e in emails}]
    reply = {"emails": emails}
    if missing:
        reply["not_found"] = missing
    return _as_text(reply)


def build_email_server():
    """The in-process tool server that holds both email tools."""
    return create_sdk_mcp_server(
        name=SERVER_NAME, tools=[search_emails_tool, read_email_tool]
    )
