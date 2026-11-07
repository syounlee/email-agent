// Put the saved color theme on the page before it is drawn (so it never flashes
// white in dark mode). The picker in the header is made by app.js.
"use strict";
try {
  const saved = localStorage.getItem("email-agent-theme");
  if (saved) document.documentElement.dataset.theme = saved;
} catch (problem) { /* private window: just use "auto" */ }
