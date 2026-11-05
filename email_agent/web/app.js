// Email Agent page. Plain JavaScript, no build step.
//
// SAFETY: email text is never inserted as HTML. Everything goes through
// el() / textContent, which shows "<script>" as the literal characters.
"use strict";

const $ = (selector) => document.querySelector(selector);
const SESSION_KEY = "email-agent-session";

let selectedId = null;    // the open email
let socket = null;        // the WebSocket to the server
let answerBubble = null;  // the assistant bubble being filled in
let busy = false;         // waiting for Claude?

// ---------- small helpers ----------

// Make an element. Children that are strings become TEXT (never HTML).
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function senderName(from) {
  const name = (from || "").split("<")[0].replace(/"/g, "").trim();
  return name || from || "(unknown)";
}

function shortTime(iso) {
  const when = new Date(iso);
  if (isNaN(when)) return iso || "";
  const now = new Date();
  if (when.toDateString() === now.toDateString()) {
    return when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return when.toLocaleDateString([], { month: "short", day: "numeric" });
}

// Claude's answers use **bold**. Show that as bold, still building only
// text nodes and <b> elements (never parsing HTML).
function richText(text) {
  const parts = String(text).split(/(\*\*[^*\n]+\*\*)/);
  return parts.map((part) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4
      ? el("b", {}, part.slice(2, -2)) : part);
}

function money(value) {
  return value === null || value === undefined ? "?" : "$" + Number(value).toFixed(4);
}

function toast(text, kind = "info") {
  const note = el("div", { class: "toast " + kind }, text);
  $("#toasts").append(note);
  setTimeout(() => note.classList.add("gone"), 6000);
  setTimeout(() => note.remove(), 6600);
}

// ---------- inbox ----------

async function loadInbox() {
  const query = $("#search").value.trim();
  try {
    let emails;
    if (query) {
      const data = await api("/api/search", {
        method: "POST", body: JSON.stringify({ query, limit: 100 }),
      });
      emails = data.results;
      $("#inbox-title").textContent = "Search results";
    } else {
      emails = (await api("/api/inbox?limit=100")).emails;
      $("#inbox-title").textContent = "Inbox";
    }
    $("#inbox-count").textContent = String(emails.length);
    $("#inbox-list").replaceChildren(...emails.map(mailRow));
    if (!emails.length) {
      $("#inbox-list").append(el("li", { class: "muted center" }, "Nothing here."));
    }
  } catch (problem) {
    toast("Inbox: " + problem.message, "error");
  }
}

let refreshTimer = null;
function refreshInboxSoon() {  // many events in a row -> one refresh
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    loadInbox();
    if (selectedId) openEmail(selectedId);
  }, 400);
}

function mailRow(email) {
  const classes = ["mail"];
  if (email.read === false) classes.push("unread");
  if (email.id === selectedId) classes.push("selected");
  return el("li", {
      class: classes.join(" "), tabindex: "0", "data-id": email.id,
      onclick: () => openEmail(email.id),
      onkeydown: (event) => { if (event.key === "Enter") openEmail(email.id); },
    },
    el("div", { class: "line1" },
      el("span", { class: "from" }, email.read === false ? "● " : "", senderName(email.from)),
      el("span", { class: "time" }, shortTime(email.date))),
    el("div", { class: "subject" },
      email.starred ? el("span", { class: "star" }, "★ ") : null, email.subject || "(no subject)"),
    el("div", { class: "snippet" }, email.snippet || ""));
}

async function openEmail(id) {
  selectedId = id;
  document.querySelectorAll(".mail").forEach((row) =>
    row.classList.toggle("selected", row.dataset.id === id));
  try {
    const email = await api("/api/email/" + encodeURIComponent(id));
    $("#reader").replaceChildren(
      el("h2", {}, email.starred ? "★ " : "", email.subject || "(no subject)"),
      el("div", { class: "meta" },
        el("div", {}, el("b", {}, "From: "), email.from),
        el("div", {}, el("b", {}, "To: "), email.to || ""),
        el("div", {}, el("b", {}, "Date: "), new Date(email.date).toLocaleString())),
      el("div", { class: "chips" },
        ...email.labels.map((label) => el("span", { class: "chip" }, label))),
      el("pre", { class: "body" }, email.body || ""));  // text only: tags stay visible as text
  } catch (problem) {
    toast("Email: " + problem.message, "error");
  }
}

// ---------- listeners ----------

async function loadListeners() {
  try {
    const data = await api("/api/listeners");
    $("#listener-list").replaceChildren(
      ...data.listeners.map((l) => el("li", {
        class: "chip " + (l.enabled ? "on" : "off"), title: l.name + " (" + l.file + ")",
      }, (l.enabled ? "● " : "○ ") + l.id)),
      ...Object.entries(data.errors).map(([file, why]) =>
        el("li", { class: "chip error", title: why }, "⚠ " + file)));
    const runs = (await api("/api/runs?limit=25")).runs;
    $("#run-list").replaceChildren(...runs.map(runRow));
  } catch (problem) {
    toast("Listeners: " + problem.message, "error");
  }
}

function runRow(run) {
  const icon = run.error ? "❌" : run.executed ? "✅" : "➖";
  return el("li", { class: run.executed ? "run hit" : "run", title: run.error || run.reason },
    el("span", { class: "time" }, shortTime(run.time)),
    el("span", {}, `${icon} ${run.listener}`),
    el("span", { class: "muted" }, " · ", run.subject || "", " — ", run.error || run.reason));
}

// ---------- chat ----------

function chatLog() { return $("#chat-log"); }

function scrollChat() { chatLog().scrollTop = chatLog().scrollHeight; }

function addBubble(kind, text) {
  const intro = chatLog().querySelector(".center");
  if (intro) intro.remove();
  const bubble = el("div", { class: "bubble " + kind }, text);
  chatLog().append(bubble);
  scrollChat();
  return bubble;
}

function setBusy(value) {
  busy = value;
  $("#send").disabled = value;
  $("#send").textContent = value ? "…" : "Send";
}

function sendChat(text) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    toast("Not connected to the server yet", "error");
    return;
  }
  addBubble("user", text);
  answerBubble = null;
  setBusy(true);
  const message = { type: "chat", content: text };
  const saved = localStorage.getItem(SESSION_KEY);
  if (saved) message.session_id = saved;
  socket.send(JSON.stringify(message));
}

function approvalCard(request) {
  const allow = el("button", { type: "button", class: "allow" }, "Allow");
  const deny = el("button", { type: "button", class: "deny" }, "Deny");
  const answer = (yes) => {
    socket.send(JSON.stringify({ type: "approval", id: request.id, allow: yes }));
    allow.disabled = deny.disabled = true;
    card.classList.add(yes ? "allowed" : "denied");
    buttons.replaceChildren(el("span", { class: "muted" }, yes ? "✔ allowed" : "✖ declined"));
  };
  allow.addEventListener("click", () => answer(true));
  deny.addEventListener("click", () => answer(false));
  const buttons = el("div", { class: "buttons" }, allow, deny);
  const card = el("div", { class: "approval" },
    el("div", { class: "question" }, "❓ ", request.question),
    request.details && request.details.length
      ? el("pre", { class: "details" }, request.details.join("\n")) : null,
    buttons);
  return card;
}

function handleMessage(message) {
  switch (message.type) {
    case "chat_started":
      break;
    case "assistant_text":
      if (!answerBubble) answerBubble = addBubble("assistant", "");
      if (answerBubble.textContent) answerBubble.append("\n\n");
      answerBubble.append(...richText(message.text));
      scrollChat();
      break;
    case "tool_call":
      answerBubble = null;  // text after a tool call goes into a new bubble
      addBubble("tool", message.summary || message.name);
      break;
    case "approval_request":
      chatLog().append(approvalCard(message));
      scrollChat();
      break;
    case "result":
      if (!answerBubble && message.text) addBubble("assistant", "").append(...richText(message.text));
      addBubble("cost", message.cost_usd === null
        ? `${money(message.session_cost_usd)} conversation so far`
        : `${money(message.cost_usd)} this answer · ${money(message.session_cost_usd)} conversation`);
      if (message.session_id) localStorage.setItem(SESSION_KEY, message.session_id);
      answerBubble = null;
      setBusy(false);
      loadInbox();  // stars or labels may have changed
      if (selectedId) openEmail(selectedId);
      break;
    case "new_chat":
      localStorage.removeItem(SESSION_KEY);
      chatLog().replaceChildren(el("p", { class: "muted center" }, "New conversation."));
      break;
    case "error":
      addBubble("error", message.message);
      if (!/Still answering|No such approval/.test(message.message)) setBusy(false);
      break;
    case "new_mail":
      toast("📨 New mail: " + (message.email.subject || ""), "mail");
      loadInbox();
      break;
    case "listener_run":
      $("#run-list").prepend(runRow(message.run));
      // a listener may have starred or labeled something: refresh (once, shortly)
      if ((message.run.actions || []).some((a) => !a.startsWith("notified"))) refreshInboxSoon();
      break;
    case "notification":
      toast("🔔 " + message.message, "notify");
      break;
  }
}

function connect() {
  socket = new WebSocket(`ws://${location.host}/ws`);
  socket.addEventListener("open", () => {
    $("#connection").textContent = "connected";
    $("#connection").className = "badge on";
  });
  socket.addEventListener("message", (event) => handleMessage(JSON.parse(event.data)));
  socket.addEventListener("close", () => {
    $("#connection").textContent = "reconnecting…";
    $("#connection").className = "badge off";
    if (busy) { addBubble("error", "Connection lost during the answer."); setBusy(false); }
    setTimeout(connect, 2000);
  });
}

// ---------- header: demo mail buttons or sync ----------

async function setupHeader() {
  const info = await api("/api/info");
  $("#mode").textContent = `${info.mailbox} mailbox · ${info.model}`;
  const area = $("#arrive");
  if (info.mailbox === "demo") {
    area.append(el("span", { class: "muted" }, "Pretend new mail: "));
    for (const kind of info.demo_kinds) {
      area.append(el("button", {
        type: "button", class: "small",
        onclick: async (event) => {
          event.target.disabled = true;
          try {
            await api("/api/demo/deliver", { method: "POST", body: JSON.stringify({ kind }) });
          } catch (problem) { toast(problem.message, "error"); }
          event.target.disabled = false;
        },
      }, kind));
    }
  } else {
    area.append(el("button", {
      type: "button", class: "small",
      onclick: async (event) => {
        event.target.disabled = true;
        try {
          const result = await api("/api/sync", { method: "POST" });
          toast(`Synced: ${result.added} new`, "info");
          loadInbox();
        } catch (problem) { toast(problem.message, "error"); }
        event.target.disabled = false;
      },
    }, "Sync now"));
  }
}

// ---------- start ----------

document.addEventListener("DOMContentLoaded", () => {
  $("#search-form").addEventListener("submit", (event) => { event.preventDefault(); loadInbox(); });
  $("#search").addEventListener("search", loadInbox);  // the little ✕ clears the search
  $("#chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = $("#chat-input").value.trim();
    if (text && !busy) { sendChat(text); $("#chat-input").value = ""; }
  });
  $("#chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("#chat-form").requestSubmit();
    }
  });
  $("#new-chat").addEventListener("click", () => {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "new_chat" }));
  });
  $("#refresh-runs").addEventListener("click", loadListeners);

  setupHeader().catch((problem) => toast(problem.message, "error"));
  loadInbox();
  loadListeners();
  connect();
});
