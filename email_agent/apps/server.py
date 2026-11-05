"""The local web server: everything the agent can do, over HTTP and a WebSocket.

    uv run email-agent        # then open http://127.0.0.1:3000

Only reachable from this computer (127.0.0.1), no login. Two checks stop
OTHER websites open in your browser from using it behind your back:
- the Host header must be 127.0.0.1:<port> or localhost:<port>;
- if a browser sends an Origin header, it must be this server itself.
(curl and scripts send no Origin, so they work.)

HTTP
    GET  /api/alerts?status=new         alerts (new | done | dismissed | all) + weekly stats
    POST /api/alerts/{num} {"status", "mute"}  mark done / not important, mute the sender
    GET  /api/email/{id}
    GET  /api/email-html/{id}?images=1  the email as a web page (shown in a locked frame)
    GET  /api/listeners
    GET  /api/listeners/{id}/logs?limit=50
    POST /api/actions/{id}  {"params": {...}}
    POST /api/sync                      fetch new mail, run the listeners
    POST /api/demo/deliver {"kind"}     demo mailbox only: pretend mail arrived
    GET  /api/info                      mailbox, model, demo email kinds (for the page)
    GET  /api/runs?limit=20             newest listener runs, all listeners
    GET  /                              the web page (files in email_agent/apps/web/)

WebSocket /ws  (JSON messages)
    you send:   {"type": "chat", "content": "...", "session_id": optional}
                {"type": "new_chat"}       forget this browser's conversation
                {"type": "approval", "id": "...", "allow": true/false}
                {"type": "ping"}
    you get:    chat_started, assistant_text, tool_call, approval_request,
                result, error, pong
                and, pushed to every browser: new_mail, listener_run, notification
"""

import asyncio
import dataclasses
import json
import uuid
from pathlib import Path
from contextlib import asynccontextmanager, closing

import uvicorn
from claude_agent_sdk import (
    AssistantMessage, ClaudeSDKClient, PermissionResultAllow, PermissionResultDeny,
    ResultMessage, TextBlock, ToolUseBlock,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from email_agent.mail import database, mailbox
from email_agent.mail.actions import approval_question
from email_agent.agent.options import build_options
from email_agent.apps.chat import FILE_TOOLS, _file_question, describe_tool_call
from email_agent.core.config import PROJECT_ROOT, Config, load_config
from email_agent.mail.idle_watcher import start_watcher
from email_agent.mail.imap_sync import sync
from email_agent.automation.listen import DEMO_EMAILS
from email_agent.automation import alerts
from email_agent.automation.listeners import ListenerRegistry, add_demo_email, process_pending, read_log
from email_agent.agent.tools import READ_TOOLS, TOOL_TO_ACTION
from email_agent.automation.user_actions import ActionInputError, ActionRegistry, run_action_with_params

APPROVAL_TIMEOUT_SECONDS = 300  # no answer in 5 minutes = "no"
WEB_DIR = Path(__file__).resolve().parent / "web"


def log(message: str) -> None:
    print(f"[email-agent] {message}", flush=True)


# ---------- one chat turn with Claude (replaced by a fake in tests) ----------

async def claude_turn(text: str, *, config: Config, resume: str | None, approver, emit) -> dict:
    """Send one message to Claude, streaming what happens to `emit`.

    resume: the session id of this browser's conversation, so Claude
    remembers earlier messages. Returns the session id and running cost.
    """
    options = build_options(config, approver=approver)
    if resume:
        options = dataclasses.replace(options, resume=resume)
    result = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(text)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        await emit({"type": "assistant_text", "text": block.text})
                    elif isinstance(block, ToolUseBlock):
                        await emit({"type": "tool_call", "name": block.name, "input": block.input,
                                    "summary": describe_tool_call(block.name, block.input)})
            elif isinstance(message, ResultMessage):
                result = message
    if result is None:
        raise RuntimeError("Claude ended without a result")
    return {"session_id": result.session_id, "text": result.result or "",
            # Claude reports a running total for the whole (resumed) conversation
            "total_cost_usd": result.total_cost_usd or 0.0,
            "is_error": result.is_error, "subtype": result.subtype}


# ---------- one connected browser ----------

class Browser:
    """A WebSocket connection with its own conversation and approvals."""

    def __init__(self, ws: WebSocket, state: "AppState"):
        self.ws, self.state = ws, state
        self.send_lock = asyncio.Lock()       # one message at a time on the socket
        self.session_id: str | None = None    # this browser's conversation
        self.total_cost: float | None = 0.0   # running cost of that conversation
        self.chat_task: asyncio.Task | None = None
        self.approvals: dict[str, asyncio.Future] = {}

    async def send(self, event: dict) -> None:
        async with self.send_lock:
            await self.ws.send_json(event)

    # --- approvals: ask in the browser instead of the terminal ---
    async def ask(self, question: str, details: list[str]) -> bool:
        approval_id = uuid.uuid4().hex[:12]
        answer = asyncio.get_running_loop().create_future()
        self.approvals[approval_id] = answer
        await self.send({"type": "approval_request", "id": approval_id,
                         "question": question, "details": details})
        try:
            return await asyncio.wait_for(answer, APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return False
        finally:
            self.approvals.pop(approval_id, None)

    def answer(self, approval_id: str, allow: bool) -> bool:
        future = self.approvals.get(approval_id)
        if future is None or future.done():
            return False
        future.set_result(bool(allow))
        return True

    def make_approver(self):
        root = self.state.root

        async def approve(tool_name: str, tool_input: dict, context):
            if tool_name in READ_TOOLS:
                return PermissionResultAllow()
            details: list[str] = []
            if tool_name in FILE_TOOLS:
                question = _file_question(tool_name, tool_input, details.append, root)
                details = [line.strip() for line in details]
            elif tool_name in TOOL_TO_ACTION:
                question = await asyncio.to_thread(
                    approval_question, TOOL_TO_ACTION[tool_name], tool_input.get("id", ""),
                    tool_input.get("label"))
            else:
                return PermissionResultDeny(message=f"{tool_name} is not allowed.")
            if await self.ask(question, details):
                return PermissionResultAllow()
            return PermissionResultDeny(
                message="The user declined this change. Nothing was changed. "
                        "Tell them it was not done and do not retry unless they ask.")

        return approve

    # --- chat ---
    async def run_chat(self, text: str, session_id: str | None) -> None:
        if session_id and session_id != self.session_id:  # e.g. after a page reload
            # Continue that conversation. Its cost so far is known if this server
            # saw it (None = unknown, e.g. after a server restart).
            self.session_id = session_id
            self.total_cost = self.state.session_costs.get(session_id)
        budget = self.state.config.max_budget_usd
        if self.total_cost is not None and self.total_cost >= budget:
            await self.send({"type": "error", "message": f"Budget cap reached (${budget:.2f}) "
                             "for this conversation. Reload the page to start a new one."})
            return
        await self.send({"type": "chat_started"})
        try:
            out = await self.state.chat_turn(text, resume=self.session_id,
                                             approver=self.make_approver(), emit=self.send)
        except asyncio.CancelledError:
            raise
        except Exception as problem:
            await self.send({"type": "error", "message": f"{type(problem).__name__}: {problem}"})
            return
        previous = self.total_cost
        self.session_id, self.total_cost = out["session_id"], out["total_cost_usd"]
        self.state.session_costs[out["session_id"]] = out["total_cost_usd"]
        this_answer = None if previous is None else max(out["total_cost_usd"] - previous, 0.0)
        await self.send({"type": "result", "text": out["text"], "session_id": out["session_id"],
                         "cost_usd": this_answer, "session_cost_usd": out["total_cost_usd"],
                         "is_error": out["is_error"], "subtype": out["subtype"]})

    def new_chat(self) -> bool:
        if self.chat_task and not self.chat_task.done():
            return False
        self.session_id, self.total_cost = None, 0.0
        return True

    def close(self) -> None:
        if self.chat_task and not self.chat_task.done():
            self.chat_task.cancel()
        for future in self.approvals.values():
            if not future.done():
                future.set_result(False)


# ---------- shared server state ----------

class AppState:
    def __init__(self, config: Config, chat_turn, sync_fn, root, listeners, actions):
        self.config, self.chat_turn, self.sync_fn, self.root = config, chat_turn, sync_fn, root
        self.listeners, self.actions = listeners, actions
        self.browsers: set[Browser] = set()
        self.session_costs: dict[str, float] = {}  # conversation -> running cost
        self.sync_lock = asyncio.Lock()

    async def broadcast(self, event: dict) -> None:
        """Send an event to every connected browser (dropping dead ones)."""
        for browser in list(self.browsers):
            try:
                await browser.send(event)
            except Exception:
                self.browsers.discard(browser)

    async def _on_email(self, email: dict) -> None:
        await self.broadcast({"type": "new_mail", "email": {
            k: email[k] for k in ("id", "from", "subject", "date", "labels", "read", "starred")}})

    async def _on_entries(self, email: dict, entries: list[dict]) -> None:
        for entry in entries:
            await self.broadcast({"type": "listener_run", "run": entry})
            for change in entry.get("actions", []):
                if change.startswith("notified: "):
                    await self.broadcast({"type": "notification", "listener": entry["listener"],
                                          "email_id": entry["email_id"],
                                          "message": change[len("notified: "):]})

    async def process_new_mail(self) -> list[dict]:
        """Run the listeners on emails waiting for them, telling browsers as it goes."""
        return await process_pending(self.listeners, log, on_email=self._on_email,
                                     on_entries=self._on_entries)

    async def sync_and_process(self) -> dict:
        """Fetch new mail (imap) and run the listeners. One at a time."""
        async with self.sync_lock:
            result = {"mailbox": self.config.mailbox, "found": 0, "added": 0, "new_ids": []}
            if self.config.mailbox == "imap":
                result.update(await asyncio.to_thread(self.sync_fn, self.config))
            entries = await self.process_new_mail()
            result["listener_runs"] = len(entries)
            return result


def state_of(request) -> AppState:
    return request.app.state.app_state


async def json_body(request: Request) -> dict:
    """The request's JSON object ({} if empty). Raises ValueError if it isn't JSON."""
    raw = await request.body()
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("the body must be a JSON object")
    return data


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _int_param(request: Request, name: str, default: int, top: int = 1000) -> int:
    try:
        return max(1, min(int(request.query_params.get(name, default)), top))
    except ValueError:
        return default


# ---------- HTTP routes ----------

async def get_alerts(request: Request):
    status = request.query_params.get("status", "new")
    if status not in (*alerts.STATUSES, "all"):
        return error(400, "status must be new, done, dismissed or all")
    limit = _int_param(request, "limit", 100)

    def load():
        return (alerts.recent_alerts(limit, None if status == "all" else status),
                alerts.stats())

    found, numbers = await asyncio.to_thread(load)
    config = state_of(request).config
    return JSONResponse({"status": status, "alerts": found, "stats": numbers,
                         "phone": bool(config.ntfy_topic), "mailbox": config.mailbox,
                         "summary_hour": config.summary_hour})


async def post_alert(request: Request):
    num = request.path_params["num"]
    try:
        body = await json_body(request)
    except ValueError as problem:
        return error(400, f"Invalid JSON: {problem}")
    status, mute = body.get("status"), body.get("mute", False)
    if status not in alerts.STATUSES or not isinstance(mute, bool):
        return error(400, 'Send {"status": "done" | "dismissed" | "new", "mute": false}')
    updated = await asyncio.to_thread(alerts.set_status, num, status, mute)
    if updated is None:
        return error(404, f"No alert {num}")
    await state_of(request).broadcast({"type": "alerts_changed"})
    return JSONResponse(updated)


def html_email_headers(images: bool) -> dict:
    """Browser rules for an email shown as a web page.

    The email's HTML comes from strangers, so it gets a sandbox: no scripts, no
    forms, no access to this app. Links open in a new tab. Pictures from the web
    stay off unless you ask (they can tell the sender you opened the email);
    pictures inside the email itself always show.
    """
    web = " https: http:" if images else ""
    return {
        "Content-Security-Policy": (
            "sandbox allow-popups allow-popups-to-escape-sandbox; "
            f"default-src 'none'; img-src data:{web}; style-src 'unsafe-inline'{web}; "
            f"font-src data:{web}; form-action 'none'; frame-ancestors 'self'"),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }


async def get_email_html(request: Request):
    email_id = request.path_params["email_id"]
    images = request.query_params.get("images") == "1"

    def load():
        with closing(database.open_db()) as conn:
            return database.read_html(conn, email_id)

    markup = await asyncio.to_thread(load)
    if markup is None:
        return error(404, f"No HTML version for email {email_id!r}")
    # <base target="_blank">: links open in a new browser tab, not inside the frame.
    page = '<!doctype html><meta charset="utf-8"><base target="_blank">' + markup
    return HTMLResponse(page, headers=html_email_headers(images))


async def get_email(request: Request):
    email_id = request.path_params["email_id"]
    found = await asyncio.to_thread(mailbox.read_email, email_id)
    if not found:
        return error(404, f"No email with id {email_id!r}")
    return JSONResponse(found[0])


async def get_listeners(request: Request):
    state = state_of(request)
    modules = await asyncio.to_thread(state.listeners.all)
    listeners = [{"id": m.config["id"], "name": m.config["name"],
                  "enabled": bool(m.config.get("enabled", True)), "event": m.config["event"],
                  "file": m.__file__.rsplit("/", 1)[-1]} for m in modules]
    errors = {path.name: why for path, why in state.listeners.errors.items()}
    return JSONResponse({"listeners": listeners, "errors": errors})


async def get_listener_logs(request: Request):
    state = state_of(request)
    listener_id = request.path_params["listener_id"]
    known = {m.config["id"] for m in await asyncio.to_thread(state.listeners.all)}
    runs = await asyncio.to_thread(read_log, listener_id, _int_param(request, "limit", 50))
    if listener_id not in known and not runs:
        return error(404, f"No listener {listener_id!r}")
    return JSONResponse({"listener": listener_id, "runs": runs})


async def post_action(request: Request):
    state = state_of(request)
    try:
        body = await json_body(request)
    except ValueError as problem:
        return error(400, f"Invalid JSON: {problem}")
    params = body.get("params", {})
    if not isinstance(params, dict):
        return error(400, '"params" must be an object, e.g. {"params": {"days": 30}}')
    try:
        entry = await run_action_with_params(request.path_params["action_id"], params,
                                             state.actions, show=log)
    except ActionInputError as problem:
        status = 404 if str(problem).startswith("No action") else 400
        return error(status, str(problem))
    return JSONResponse(entry)


async def post_sync(request: Request):
    try:
        return JSONResponse(await state_of(request).sync_and_process())
    except Exception as problem:  # login failure, network... (never contains the password)
        return error(502, f"Sync failed: {type(problem).__name__}: {problem}")


async def post_demo_deliver(request: Request):
    state = state_of(request)
    if state.config.mailbox != "demo":
        return error(400, "Only for the demo mailbox (MAILBOX=demo)")
    try:
        kind = (await json_body(request)).get("kind", "")
    except ValueError as problem:
        return error(400, f"Invalid JSON: {problem}")
    if kind not in DEMO_EMAILS:
        return error(400, f"kind must be one of: {', '.join(DEMO_EMAILS)}")
    email_id = await asyncio.to_thread(add_demo_email, **DEMO_EMAILS[kind])
    entries = await state.process_new_mail()
    return JSONResponse({"email_id": email_id, "listener_runs": entries})


async def get_info(request: Request):
    config = state_of(request).config
    return JSONResponse({"mailbox": config.mailbox, "model": config.model,
                         "budget_usd": config.max_budget_usd,
                         "demo_kinds": list(DEMO_EMAILS) if config.mailbox == "demo" else []})


async def get_runs(request: Request):
    runs = await asyncio.to_thread(read_log, None, _int_param(request, "limit", 20))
    return JSONResponse({"runs": runs[::-1]})  # newest first


def page_headers(port: int) -> dict:
    """Strict browser rules for the page: only our own scripts and connections."""
    sockets = f"ws://127.0.0.1:{port} ws://localhost:{port}"
    return {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "frame-src 'self'; "
            f"connect-src 'self' {sockets}; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'"),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }


async def get_page(request: Request):
    port = state_of(request).config.port
    return FileResponse(WEB_DIR / "index.html", headers=page_headers(port))


# ---------- WebSocket chat ----------

async def websocket_chat(ws: WebSocket):
    state = ws.app.state.app_state
    await ws.accept()
    browser = Browser(ws, state)
    state.browsers.add(browser)
    try:
        while True:
            try:
                message = json.loads(await ws.receive_text())
                kind = message.get("type") if isinstance(message, dict) else None
            except json.JSONDecodeError:
                await browser.send({"type": "error", "message": "Messages must be JSON"})
                continue
            if kind == "chat":
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    await browser.send({"type": "error", "message": "chat needs some content"})
                elif browser.chat_task and not browser.chat_task.done():
                    await browser.send({"type": "error",
                                        "message": "Still answering the previous message"})
                else:
                    # In the background, so approvals and pings still get through meanwhile.
                    browser.chat_task = asyncio.create_task(
                        browser.run_chat(content, message.get("session_id")))
            elif kind == "approval":
                if not browser.answer(str(message.get("id")), message.get("allow") is True):
                    await browser.send({"type": "error", "message": "No such approval waiting"})
            elif kind == "new_chat":
                if browser.new_chat():
                    await browser.send({"type": "new_chat"})
                else:
                    await browser.send({"type": "error",
                                        "message": "Still answering the previous message"})
            elif kind == "ping":
                await browser.send({"type": "pong"})
            else:
                await browser.send({"type": "error", "message": f"Unknown message type {kind!r}"})
    except WebSocketDisconnect:
        pass
    finally:
        browser.close()
        state.browsers.discard(browser)


# ---------- local-only safety checks ----------

class LocalOnly:
    """Refuse requests not addressed to this server, or sent by other websites."""

    def __init__(self, app, port: int):
        self.app = app
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{host}" for host in self.hosts}

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            host, origin = headers.get("host", ""), headers.get("origin")
            if host not in self.hosts or (origin is not None and origin not in self.origins):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4403})
                else:
                    await PlainTextResponse("Forbidden: local use only", status_code=403)(
                        scope, receive, send)
                return
        await self.app(scope, receive, send)


# ---------- the app ----------

def create_app(config: Config | None = None, *, chat_turn=None, sync_fn=sync,
               start_watcher_thread: bool = True, root=PROJECT_ROOT,
               listeners_dir=None, actions_dir=None) -> Starlette:
    """Build the web app. Tests pass a fake chat_turn, their own folders, no watcher."""
    config = config or load_config()
    if chat_turn is None:
        async def chat_turn(text, **kwargs):
            return await claude_turn(text, config=config, **kwargs)

    listeners = ListenerRegistry(listeners_dir, show=log) if listeners_dir else ListenerRegistry(show=log)
    actions = ActionRegistry(actions_dir, show=log) if actions_dir else ActionRegistry(show=log)
    state = AppState(config, chat_turn, sync_fn, root, listeners, actions)

    @asynccontextmanager
    async def lifespan(app):
        loop = asyncio.get_running_loop()
        state.listeners.refresh()  # load listeners now, report broken files early
        enabled = [m.config["id"] for m in state.listeners.enabled()]
        log(f"mailbox: {config.mailbox} | listeners on: {', '.join(enabled) or 'none'}")
        background, watcher = [], None
        can_watch = config.mailbox == "imap" and config.email_address and config.email_app_password
        if start_watcher_thread and can_watch:
            async def safe_sync():
                try:
                    await state.sync_and_process()
                except Exception as problem:
                    log(f"sync failed: {type(problem).__name__}: {problem}")

            def on_new_mail():  # runs in the watcher thread
                try:
                    asyncio.run_coroutine_threadsafe(safe_sync(), loop).result(timeout=600)
                except Exception as problem:
                    log(f"new-mail handling failed: {type(problem).__name__}: {problem}")

            background.append(asyncio.create_task(safe_sync()))  # catch up first
            watcher = start_watcher(config, on_new_mail, show=log)
        if start_watcher_thread:  # the morning summary, after the catch-up is done
            first = background[0] if background else None
            background.append(asyncio.create_task(alerts.summary_loop(first, show=log)))
        log("phone alerts: " + ("on" if config.ntfy_topic else "off (set NTFY_TOPIC in .env)"))
        try:
            yield
        finally:
            if watcher:
                thread, stop = watcher
                stop.set()
                await asyncio.to_thread(thread.join, 5)
            for task in background:
                task.cancel()
            for browser in list(state.browsers):
                browser.close()
            log("stopped")

    app = Starlette(routes=[
        Route("/api/alerts", get_alerts, methods=["GET"]),
        Route("/api/alerts/{num:int}", post_alert, methods=["POST"]),
        Route("/api/email/{email_id:path}", get_email, methods=["GET"]),
        Route("/api/email-html/{email_id:path}", get_email_html, methods=["GET"]),
        Route("/api/listeners", get_listeners, methods=["GET"]),
        Route("/api/listeners/{listener_id}/logs", get_listener_logs, methods=["GET"]),
        Route("/api/actions/{action_id}", post_action, methods=["POST"]),
        Route("/api/sync", post_sync, methods=["POST"]),
        Route("/api/demo/deliver", post_demo_deliver, methods=["POST"]),
        Route("/api/info", get_info, methods=["GET"]),
        Route("/api/runs", get_runs, methods=["GET"]),
        WebSocketRoute("/ws", websocket_chat),
        Route("/", get_page, methods=["GET"]),
        Mount("/static", StaticFiles(directory=WEB_DIR), name="static"),
    ], lifespan=lifespan)
    app.state.app_state = state
    app.add_middleware(LocalOnly, port=config.port)
    return app


def main() -> None:
    """`uv run email-agent`: start the server on this computer only."""
    config = load_config()
    log(f"starting on http://127.0.0.1:{config.port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(config), host="127.0.0.1", port=config.port, log_level="warning")


if __name__ == "__main__":
    main()
