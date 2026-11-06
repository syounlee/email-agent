// Email Alerts page. Plain JavaScript, no build step.
//
// SAFETY: email text is never inserted as HTML. Everything goes through
// el() / textContent, which shows "<script>" as the literal characters.
// HTML emails are shown in a sandboxed frame (see htmlFrame).
"use strict";

const $ = (selector) => document.querySelector(selector);
const SESSION_KEY = "email-agent-session";
const KINDS = { action: "📝", coupon: "🏷️", money: "💳", security: "🔐" };
const FILTERS = [["new", "New"], ["done", "Done"], ["dismissed", "Not important"], ["all", "All"]];

let filter = "new";       // which alerts the list shows
let selected = null;      // the alert whose email is open
let mailbox = "demo";
let provider = "gmail";   // which email service (gmail, outlook, icloud, yahoo, other)
let phoneOn = false;
let showText = false;     // show HTML emails as plain text instead of a web page
const imagesAllowed = new Set();  // emails whose pictures from the web you allowed
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

// ---------- alerts ----------

async function loadAlerts() {
  try {
    const data = await api("/api/alerts?limit=200&status=" + filter);
    mailbox = data.mailbox;
    phoneOn = data.phone;
    showStats(data);
    $("#alert-list").replaceChildren(...data.alerts.map(alertCard));
    if (!data.alerts.length) {
      $("#alert-list").append(el("li", { class: "muted center" },
        filter === "new" ? "Nothing needs you right now. 🎉" : "Nothing here."));
    }
  } catch (problem) {
    toast("Alerts: " + problem.message, "error");
  }
}

let refreshTimer = null;
function refreshSoon() {  // many events in a row -> one refresh
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(loadAlerts, 400);
}

function showStats(data) {
  const s = data.stats;
  $("#stats").replaceChildren(
    el("div", { class: "big" }, `${s.alerts} of ${s.emails}`),
    el("div", { class: "muted" }, `emails in the last ${s.days} days were worth an alert`,
      s.muted ? ` · ${s.muted} sender${s.muted === 1 ? "" : "s"} muted` : ""),
    el("div", { class: "muted small-text" }, data.phone
      ? `Urgent ones go to your phone right away; the rest in a summary ${data.summary_schedule}.`
      : "Phone alerts are off: set NTFY_TOPIC in .env."));
  $("#phone").textContent = data.phone ? "📱 phone on" : "📱 phone off";
  $("#phone").className = "badge " + (data.phone ? "on" : "off");
  $("#filters").replaceChildren(...FILTERS.map(([key, text]) => el("button", {
      type: "button", class: "tab" + (key === filter ? " active" : ""),
      onclick: () => { filter = key; loadAlerts(); },
    }, text, key === "new" || key === "done" || key === "dismissed"
      ? el("span", { class: "count" }, " " + s.counts[key]) : null)));
}

function delivery(alert) {
  if (alert.pushed) return "📱 sent right away";
  if (alert.summarized) return `📬 in the ${alert.summarized} summary`;
  if (!phoneOn) return "📵 phone alerts off";
  return alert.urgent ? "⚠️ phone failed · next summary" : "⏳ next summary";
}

function alertCard(alert) {
  const act = (status, mute = false) => async (event) => {
    event.stopPropagation();  // don't also open the email
    try {
      await api("/api/alerts/" + alert.num, {
        method: "POST", body: JSON.stringify({ status, mute }),
      });
      if (mute) toast("🔇 Muted " + senderName(alert.sender), "info");
      loadAlerts();
    } catch (problem) { toast(problem.message, "error"); }
  };
  const button = (text, title, onclick) =>
    el("button", { type: "button", class: "small", title, onclick }, text);
  const buttons = alert.status === "new"
    ? [button("✅ Done", "I've handled it", act("done")),
       button("🙈 Not important", "Teach the agent: don't alert about emails like this",
         act("dismissed")),
       button("🔇 Mute", "Never alert about this sender again", act("dismissed", true))]
    : [button("↩ Back to new", "Move it back to New", act("new"))];
  return el("li", {
      class: "alert kind-" + alert.kind + (alert.urgent ? " urgent" : "")
        + (selected && selected.num === alert.num ? " selected" : ""),
      tabindex: "0", "data-num": String(alert.num),
      onclick: () => openAlert(alert),
      onkeydown: (event) => { if (event.key === "Enter") openAlert(alert); },
    },
    el("div", { class: "line1" },
      el("span", { class: "title" }, (KINDS[alert.kind] || "🔔") + " " + alert.title),
      el("span", { class: "time" }, shortTime(alert.created))),
    el("div", { class: "message" }, alert.message),
    el("div", { class: "meta" }, senderName(alert.sender), " · ", delivery(alert)),
    el("div", { class: "buttons" }, ...buttons));
}

function gmailLink(emailId) {
  // Gmail finds an email by its Message-ID header.
  const id = emailId.replace(/^<|>$/g, "");
  return "https://mail.google.com/mail/u/0/#search/rfc822msgid:" + encodeURIComponent(id);
}

async function openAlert(alert) {
  selected = alert;
  document.querySelectorAll(".alert").forEach((card) =>
    card.classList.toggle("selected", card.dataset.num === String(alert.num)));
  try {
    const email = await api("/api/email/" + encodeURIComponent(alert.email_id));
    const asPage = email.has_html && !showText;
    $("#reader").classList.toggle("page-mode", asPage);
    const parts = [
      el("div", { class: "why kind-" + alert.kind },
        el("b", {}, (KINDS[alert.kind] || "🔔") + " " + alert.title), el("br"), alert.message),
      el("h2", {}, email.subject || "(no subject)"),
      el("div", { class: "meta" },
        el("div", {}, el("b", {}, "From: "), email.from),
        el("div", {}, el("b", {}, "Date: "), new Date(email.date).toLocaleString()),
        mailbox === "imap" && provider === "gmail"
          ? el("div", {}, el("a", { href: gmailLink(email.id), target: "_blank",
              rel: "noopener noreferrer" }, "Open in Gmail ↗")) : null),
      email.has_html ? viewSwitch(alert, asPage) : null,
      asPage ? htmlFrame(email.id)
             : el("pre", { class: "body" }, email.body || "")];  // text only: tags stay text
    // replaceChildren would show a null as the text "null", so leave those out
    $("#reader").replaceChildren(...parts.filter((part) => part));
  } catch (problem) {
    toast("Email: " + problem.message, "error");
  }
}

// Buttons above an HTML email: web page / plain text, and pictures from the web.
function viewSwitch(alert, asPage) {
  const id = alert.email_id;
  const button = (text, active, onclick) =>
    el("button", { type: "button", class: "small" + (active ? " active" : ""), onclick }, text);
  return el("div", { class: "view-switch" },
    button("Web page", asPage, () => { showText = false; openAlert(alert); }),
    button("Plain text", !asPage, () => { showText = true; openAlert(alert); }),
    asPage && !imagesAllowed.has(id)
      ? el("span", { class: "images-note" }, "Pictures from the web are off. ",
          button("Show pictures", false, () => { imagesAllowed.add(id); openAlert(alert); }))
      : null);
}

// The email's own HTML, in a locked frame: the sandbox blocks its scripts, forms and
// any access to this page; the server adds strict rules of its own.
function htmlFrame(id) {
  let src = "/api/email-html/" + encodeURIComponent(id);
  if (imagesAllowed.has(id)) src += "?images=1";
  return el("iframe", {
    class: "html-frame", src, title: "Email as a web page",
    sandbox: "allow-popups allow-popups-to-escape-sandbox", referrerpolicy: "no-referrer",
  });
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
      refreshSoon();
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
      refreshSoon();  // the "N of M emails" count
      break;
    case "notification":  // a new alert (or a web-page notice from a listener)
      toast("🔔 " + message.message, "notify");
      refreshSoon();
      break;
    case "alerts_changed":
      refreshSoon();
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

// ---------- header: demo mail buttons or "check now" ----------

async function setupHeader() {
  const info = await api("/api/info");
  provider = info.provider;
  $("#mode").textContent = info.mailbox === "demo" ? "demo mailbox"
                                                   : "watching " + info.provider_name;
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
      type: "button", class: "small", title: "New mail is checked automatically; this checks now",
      onclick: async (event) => {
        event.target.disabled = true;
        try {
          const result = await api("/api/sync", { method: "POST" });
          toast(`Checked: ${result.added} new email${result.added === 1 ? "" : "s"}`, "info");
          loadAlerts();
        } catch (problem) { toast(problem.message, "error"); }
        event.target.disabled = false;
      },
    }, "Check now"));
  }
}

// ---------- start ----------

document.addEventListener("DOMContentLoaded", () => {
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

  setupHeader().catch((problem) => toast(problem.message, "error"));
  loadAlerts();
  connect();
});
