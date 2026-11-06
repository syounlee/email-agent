"""Change mail: archive, star/unstar, mark read/unread, add/remove a label.

Each action changes the mail server FIRST and the local database SECOND.
If the server says no, the database is left alone, so it never claims a
change that didn't happen. In demo mode there is no server: only the demo
database changes.

Star = the \\Flagged flag and read = the \\Seen flag work on every IMAP
server (Gmail, Outlook, iCloud Mail, Yahoo Mail...).
Labels and archive are Gmail's own (X-GM-LABELS; archive = remove \\Inbox).
On other servers a label is kept on this computer only (the server is left
alone), and archive isn't available.

Nothing here sends or deletes mail.
"""

from contextlib import closing

from email_agent.mail import database
from email_agent.core.config import Config, load_config
from email_agent.mail.login import sign_in

ACTIONS = ("archive", "star", "unstar", "mark_read", "mark_unread", "add_label", "remove_label")
LABEL_ACTIONS = ("add_label", "remove_label")


class ActionError(Exception):
    """An action couldn't be done. The message is safe to show Claude and you."""


def _is_inbox(label: str) -> bool:
    return label.lstrip("\\").lower() == "inbox"


def _check_label(label: str | None) -> str:
    label = (label or "").strip()
    if not label:
        raise ActionError("Please give a label name.")
    # System labels like \\Trash or \\Spam would delete or junk the email,
    # so only ordinary, user-made labels are allowed here.
    if label.startswith("\\") or label.lower().startswith("[gmail]") or _is_inbox(label):
        raise ActionError(f"'{label}' is a system label; only your own labels can be changed.")
    return label


def new_state(email: dict, action: str, label: str | None = None) -> dict:
    """What read/starred/labels become after the action (pure, no side effects)."""
    read, starred, labels = email["read"], email["starred"], list(email["labels"])
    if action == "star":
        starred = True
    elif action == "unstar":
        starred = False
    elif action == "mark_read":
        read = True
    elif action == "mark_unread":
        read = False
    elif action == "archive":
        labels = [l for l in labels if not _is_inbox(l)]
    elif action == "add_label":
        if label.lower() not in (l.lower() for l in labels):
            labels.append(label)
    elif action == "remove_label":
        labels = [l for l in labels if l.lower() != label.lower()]
    return {"read": read, "starred": starred, "labels": labels}


# ---------- the mail server part ----------

def _connect(config: Config):
    from imapclient import IMAPClient

    return IMAPClient(config.imap_host, ssl=True, timeout=30)


def _change_server(config: Config, email: dict, action: str, label: str | None, connect) -> str:
    """Make the change on the IMAP server. Raises ActionError if it can't.

    Returns "" normally, or a note such as "this computer only"."""
    if email.get("uid") is None or not email.get("folder"):
        raise ActionError("This email has no server location; try syncing again.")
    if not any(_is_inbox(l) for l in email["labels"]):
        raise ActionError("This email is no longer in your inbox, so it can't be changed here.")

    with connect(config) as server:
        sign_in(server, config)
        info = server.select_folder(email["folder"])  # read-write this time
        if int(info[b"UIDVALIDITY"]) != email["uidvalidity"]:
            raise ActionError(f"{config.provider_name} renumbered this folder. Please sync again first.")

        uid = [email["uid"]]
        if not server.fetch(uid, ["FLAGS"]):  # make sure it's still there
            raise ActionError("The email is no longer in this folder (moved or deleted?).")

        if not server.has_capability("X-GM-EXT-1"):  # not Gmail: no labels, no archive
            if action == "archive":
                raise ActionError(f"Archive needs Gmail; {config.provider_name} doesn't have it.")
            if action in LABEL_ACTIONS:
                return "this computer only"  # the label is kept in the local database

        if action == "star":
            server.add_flags(uid, [b"\\Flagged"])
        elif action == "unstar":
            server.remove_flags(uid, [b"\\Flagged"])
        elif action == "mark_read":
            server.add_flags(uid, [b"\\Seen"])
        elif action == "mark_unread":
            server.remove_flags(uid, [b"\\Seen"])
        elif action == "archive":
            server.remove_gmail_labels(uid, ["\\Inbox"])
        elif action == "add_label":
            server.add_gmail_labels(uid, [label])
        elif action == "remove_label":
            server.remove_gmail_labels(uid, [label])
    return ""


# ---------- the one function the tools call ----------

def apply(action: str, email_id: str, label: str | None = None, *,
          config: Config | None = None, conn=None, connect=_connect) -> str:
    """Do one action on one email. Returns a short message of what changed."""
    if action not in ACTIONS:
        raise ActionError(f"Unknown action {action!r}.")
    if action in LABEL_ACTIONS:
        label = _check_label(label)
    config = config or load_config()

    own_conn = conn is None
    conn = conn or database.open_db()
    try:
        found = database.read(conn, [email_id])
        if not found:
            raise ActionError(f"No email with id {email_id!r}. Search for it first.")
        email = found[0]

        note = ""
        if config.mailbox == "imap":
            try:
                note = _change_server(config, email, action, label, connect)
            except ActionError:
                raise
            except Exception as problem:  # network, login, server refusal
                raise ActionError(f"{config.provider_name} refused the change "
                                  f"({type(problem).__name__}: {problem}). "
                                  "Nothing was changed.") from problem

        database.update_state(conn, email_id, **new_state(email, action, label))
    finally:
        if own_conn:
            conn.close()

    done = {
        "archive": "Archived", "star": "Starred", "unstar": "Unstarred",
        "mark_read": "Marked as read", "mark_unread": "Marked as unread",
        "add_label": f"Added label '{label}' to", "remove_label": f"Removed label '{label}' from",
    }[action]
    return f"{done} '{email['subject']}'" + (f" ({note})." if note else ".")


def approval_question(action: str, email_id: str, label: str | None = None, conn=None) -> str:
    """The yes/no question shown before an action, e.g. "Allow archive of 'Lunch?'?"."""
    if conn is None:
        with closing(database.open_db()) as c:
            found = database.read(c, [email_id])
    else:
        found = database.read(conn, [email_id])
    subject = f"'{found[0]['subject']}'" if found else f"unknown email {email_id}"
    phrase = {
        "archive": "archive of", "star": "star of", "unstar": "unstar of",
        "mark_read": "mark as read of", "mark_unread": "mark as unread of",
        "add_label": f"adding label '{label}' to", "remove_label": f"removing label '{label}' from",
    }[action]
    return f"Allow {phrase} {subject}?"
