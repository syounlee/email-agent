# Email Agent

An email assistant you run on your own computer, built with the
[Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/) for Python.

![The web page: inbox, open email with listeners below, and chat](docs/screenshot.png)

## What it does

- **Answers questions about your mail.** "Which invoices are overdue?", "What does my
  manager need today?". It searches, reads the relevant emails, and cites sender and
  date. Follow-up questions work ("and the other one?").
- **Changes mail when you approve it.** Archive, star/unstar, mark read/unread,
  add/remove your own labels. Every change asks *Allow …? (y/n)* first.
- **Runs listeners on new mail.** Small Python scripts that run automatically on every
  new email, for example: label it by type, flag deadlines, alert on security emails,
  notify on cheap flights.
- **Runs one-click actions.** Scripts you start yourself, e.g.
  `/run archive-old-newsletters days=30 dry_run=true`.
- **Writes listeners and actions for you.** Ask "notify me when a bug report arrives"
  and it writes the script, shows you the code, and asks before saving it.
- **Has a local web page.** Inbox, email reader, chat, listener activity and pop-up
  notifications at `http://127.0.0.1:3000`.

It works with a **demo mailbox** (15 made-up emails, nothing to set up) or your
**Gmail** over IMAP.

## Setup

You need [uv](https://docs.astral.sh/uv/) and an Anthropic API key.

```bash
uv sync
cp .env.example .env        # then open .env and paste your ANTHROPIC_API_KEY
```

`.env` holds your settings and secrets. It is never committed to git, and the program
never prints it. All settings, with their defaults, are explained in `.env.example`:
model, budget per conversation (`MAX_BUDGET_USD`, default $0.50), turn limit, data
folder, mailbox, Gmail details, sync size, web port.

### Demo mailbox (default)

Nothing else to do: `MAILBOX=demo` uses `sample_data/emails.json`.

### Gmail

1. Turn on 2-Step Verification for your Google account.
2. Create an **app password** at <https://myaccount.google.com/apppasswords> (name it
   e.g. `email-agent`), and copy the 16 letters.
3. In `.env`: `MAILBOX=imap`, `EMAIL_ADDRESS=you@gmail.com`,
   `EMAIL_APP_PASSWORD=` the 16 letters (spaces are fine).

Each start copies your newest 30 emails from the last 7 days into a local database
(`data/emails-imap.db`), **read-only**: the folder is opened read-only and mail is
fetched with `BODY.PEEK[]`, so nothing is marked as read. To copy in a whole month
once, without running the listeners on old mail:

```bash
uv run python -m email_agent.imap_sync --days 30 --max 2000 --backfill
```

To cut off access at any time, delete the app password in your Google account.

## Running it

| Command | What it does |
|---|---|
| `uv run email-agent` | The web page at <http://127.0.0.1:3000> (and the mail watcher, with Gmail) |
| `uv run python -m email_agent.chat` | Chat in the terminal (`/help` lists commands) |
| `uv run python -m email_agent.ask "question"` | One question, one answer (read-only) |
| `uv run python -m email_agent.listen` | Gmail: watch for new mail and run the listeners |
| `uv run python -m email_agent.listen demo receipt` | Demo: pretend an email arrived |
| `uv run python -m email_agent.listen preview 100` | Try the listeners on 100 stored emails, **changing nothing** |

In the demo, try: ask *"Which invoices are overdue?"*, click **receipt** (top right) and
watch the listeners fire, then ask *"Star the overdue invoice from Design Studio"* and
click **Allow**.

## Writing a listener

A listener is one file in `scripts/listeners/`. It is picked up automatically: no
restart. Files starting with `_` are ignored.

```python
config = {
    "id": "notify-from-boss",
    "name": "Notify me about mail from my boss",
    "enabled": True,                 # False switches it off
    "event": "email_received",
}

async def handler(email, context):
    if "boss@example.com" not in email["from"].lower():
        return {"executed": False, "reason": "not from my boss"}
    await context.notify(f"Mail from your boss: {email['subject']}")
    return {"executed": True, "reason": "notified"}
```

`email` has `id, from, to, subject, date, body, labels, read, starred`.
`context` offers (all with `await`):

| | |
|---|---|
| `notify(message)` | a 🔔 notice (terminal and web page) |
| `star_email(id)`, `archive_email(id)`, `mark_read(id)`, `mark_unread(id)` | change the email |
| `add_label(id, "Label")` | your own labels only (never `\Trash` etc.) |
| `call_claude(prompt, schema, model="haiku")` | one Claude call that returns JSON matching `schema` |
| `search_emails(query)`, `read_email(ids)` | look at other emails |

Every run is logged to `data/listener_runs-<mailbox>.jsonl`. Tip: decide with free
rules (sender, keywords) where you can, and use `call_claude` only for the rest. See
`scripts/listeners/triage.py` for a full example, and test with
`listen preview` before letting it change real mail.

Included listeners: **triage** (labels every email as News, Promotions, Finance, Social,
Work, Security or Travel; stars and notifies when you need to act), **security-alerts**,
**login-codes**, **flight-price-watch** (set `MAX_PRICE_WON`), **work-alerts**.

## Writing an action

An action is one file in `scripts/actions/`, run with `/run <id> key=value` in the chat
or `POST /api/actions/<id>` on the web server.

```python
config = {
    "id": "archive-old-newsletters",
    "name": "Archive newsletters older than N days",
    "parameters": {   # string | integer | number | boolean; no default = required
        "days": {"type": "integer", "default": 30},
        "dry_run": {"type": "boolean", "default": False},
    },
}

async def handler(params, context):
    found = await context.search_emails(f"unsubscribe older_than:{params['days']}d", limit=500)
    ...
    return {"executed": True, "reason": "archived 12 newsletters"}
```

Search understands Gmail-style queries: words, `"exact phrase"`, `from:`, `subject:`,
`is:unread`, `is:read`, `is:starred`, `newer_than:7d`, `older_than:30d`.

## Costs

Measured on this project's runs (Claude Sonnet 5.5 for the chat, Haiku 5.5 for listeners):

| What | Typical cost |
|---|---|
| One chat question (demo) | $0.005 – $0.02 |
| One chat question (real Gmail; emails are longer) | up to ~$0.07 |
| A follow-up the agent can answer from memory | ~$0.002 |
| Listeners per new email (triage asks Haiku only when rules can't decide) | ~$0.00004 average |
| A month of one real inbox (1,076 emails) through all listeners | $0.04 |
| `uv run pytest --live` (all live tests) | ~$0.20 |
| `uv run pytest` (offline tests) | free |

The budget cap (`MAX_BUDGET_USD`) stops a conversation once it has spent that much.

## Safety notes

- **Your secrets stay in `.env`.** It is git-ignored, never printed, and the agent's
  file tools are blocked from reading it.
- **Gmail is read-only until you approve a change.** Syncing never marks mail as read.
  Nothing can send, forward or delete email; the code for that doesn't exist.
- **Every change from the chat asks you first**: mail changes and file writes, with the
  code shown. One-shot `ask` can't change anything.
- **Listeners and actions run without asking**: they are your own scripts. In Gmail mode
  they change your real mail (labels and stars); try them with `listen preview` first.
- **The agent can only write Python into `scripts/listeners/` and `scripts/actions/`**
  (a PreToolUse hook blocks everything else, including `.claude/` and `.env`). It has no
  shell and no web access.
- **Emails are data, not instructions.** The agent is told not to obey instructions
  inside emails, and listener prompts wrap emails in `<email>` tags marked as data.
- **The web server is local only.** It listens on 127.0.0.1, rejects requests addressed
  to other host names, and rejects requests from other websites (Origin check), so a web
  page you visit can't drive the agent. The page shows email text as text only (never
  as HTML) and allows only its own scripts (Content-Security-Policy).
- **Login codes** are announced without the code, and 6-digit codes are blanked in logs.

## Tests

```bash
uv run pytest            # offline: free, ~5 seconds (Claude and Gmail are faked)
uv run pytest --live     # also the live tests: real Claude calls and a read-only Gmail sync
```

Live tests that need Gmail are skipped unless `.env` has your Gmail details. Live tests
run on the demo mailbox and temporary folders, so they never change your real mail or
your `scripts/` folder.

## Project layout

```
email_agent/
  config.py        settings from .env
  database.py      SQLite + full-text search, Gmail-style queries
  mailbox.py       search_emails / read_email
  imap_sync.py     read-only Gmail sync and email parsing
  actions.py       archive, star, labels... (Gmail first, then the database)
  tools.py         the agent's tools
  agent.py         system prompt and agent options
  guard.py         the PreToolUse hook (where files may be written)
  ask.py, chat.py  terminal interfaces
  listeners.py     loading and running listeners, preview mode, logs
  claude_json.py   call_claude with structured outputs
  idle_watcher.py  IMAP IDLE in a background thread
  listen.py        the listen command, demo emails
  user_actions.py  one-click actions
  server.py        the web server (HTTP + WebSocket)
  web/             the page (HTML, JavaScript, CSS; no build step)
scripts/listeners/ your listeners
scripts/actions/   your actions
.claude/skills/listener-creator/  the skill the agent uses to write scripts
sample_data/       the demo mailbox
tests/             offline and live tests
```
