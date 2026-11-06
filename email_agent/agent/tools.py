"""Wrap the mailbox functions as tools Claude can call.

Each tool gets the same input Claude sends (a dict) and returns text.
The tools live in an in-process "MCP server" named "email", so Claude sees
them as e.g.  mcp__email__search_emails.
- search_emails, read_email, list_alerts: read-only, run without asking.
- archive_email, star_email, unstar_email, mark_read, mark_unread,
  add_label, remove_label: change mail; the chat asks you first.
"""

import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from email_agent.automation import alerts
from email_agent.mail import actions, mailbox

SERVER_NAME = "email"
SEARCH_TOOL = f"mcp__{SERVER_NAME}__search_emails"
READ_TOOL = f"mcp__{SERVER_NAME}__read_email"
ALERTS_TOOL = f"mcp__{SERVER_NAME}__list_alerts"
READ_TOOLS = [SEARCH_TOOL, READ_TOOL, ALERTS_TOOL]  # safe: run without asking


def _as_text(data) -> dict:
    """Package Python data as the text reply a tool must return."""
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]}


@tool(
    "search_emails",
    "Search the mailbox. ALWAYS use this first to find emails relevant to the "
    "question. Gmail-style syntax; all parts must match: plain words (matched "
    'in subject, sender or body; "invoices" also matches "invoice"), '
    '"exact phrase", from:name (part of the sender address), subject:word, '
    "is:unread, is:read, is:starred, newer_than:7d, older_than:30d (d/w/m/y). "
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


@tool(
    "list_alerts",
    "The alerts the important-mail listener sent the user (to their phone right away, "
    "or in the next summary), newest first, plus what the user taught it: muted "
    "senders and alerts they marked not important. Use for 'what did I miss?', 'why did "
    "you alert me about X?' or 'what codes do I have?'. Each alert has email_id: call "
    "read_email on it for the full email.",
    {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["new", "done", "dismissed", "all"],
                       "description": "new = not handled yet (default all)."},
            "limit": {"type": "integer", "description": "Max alerts (default 30).", "default": 30},
        },
    },
    annotations={"readOnlyHint": True},
)
async def list_alerts_tool(args: dict) -> dict:
    status = args.get("status", "all")
    found = alerts.recent_alerts(args.get("limit", 30), None if status == "all" else status)
    keep = ("num", "created", "email_id", "kind", "title", "message", "urgent", "status",
            "sender", "subject")
    return _as_text({"alerts": [{k: a[k] for k in keep} for a in found],
                     "taught": alerts.feedback(), "stats": alerts.stats()})


# ---------- tools that change mail (each needs your approval in the chat) ----------

_ID_ONLY = {
    "type": "object",
    "properties": {"id": {"type": "string", "description": "Email id from search_emails."}},
    "required": ["id"],
}
_ID_AND_LABEL = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Email id from search_emails."},
        "label": {"type": "string", "description": "Label name, e.g. 'Receipts'."},
    },
    "required": ["id", "label"],
}
_CHANGE_NOTE = (" Only use when the user asked for this change. Find the id with "
                "search_emails first. The user is asked to approve before it runs.")


def _action_tool(action: str, tool_name: str, description: str, schema: dict):
    @tool(tool_name, description + _CHANGE_NOTE, schema, annotations={"readOnlyHint": False})
    async def run(args: dict) -> dict:
        try:
            return _as_text({"done": actions.apply(action, args["id"], args.get("label"))})
        except actions.ActionError as problem:
            return _as_text({"error": str(problem)})
    return run


archive_email_tool = _action_tool("archive", "archive_email",
    "Archive an email: remove it from the inbox (it is kept, not deleted).", _ID_ONLY)
star_email_tool = _action_tool("star", "star_email", "Star an email.", _ID_ONLY)
unstar_email_tool = _action_tool("unstar", "unstar_email", "Remove the star from an email.", _ID_ONLY)
mark_read_tool = _action_tool("mark_read", "mark_read", "Mark an email as read.", _ID_ONLY)
mark_unread_tool = _action_tool("mark_unread", "mark_unread", "Mark an email as unread.", _ID_ONLY)
add_label_tool = _action_tool("add_label", "add_label",
    "Add one of the user's own labels to an email (created if new).", _ID_AND_LABEL)
remove_label_tool = _action_tool("remove_label", "remove_label",
    "Remove one of the user's own labels from an email.", _ID_AND_LABEL)

CHANGE_TOOLS = [archive_email_tool, star_email_tool, unstar_email_tool, mark_read_tool,
                mark_unread_tool, add_label_tool, remove_label_tool]

# Names as Claude sees them, and which action each one does
# (CHANGE_TOOLS is in the same order as actions.ACTIONS).
ACTION_TOOLS = [f"mcp__{SERVER_NAME}__{t.name}" for t in CHANGE_TOOLS]
TOOL_TO_ACTION = dict(zip(ACTION_TOOLS, actions.ACTIONS))


def build_email_server():
    """The in-process tool server that holds all the email tools."""
    return create_sdk_mcp_server(
        name=SERVER_NAME, tools=[search_emails_tool, read_email_tool, list_alerts_tool,
                                 *CHANGE_TOOLS]
    )
