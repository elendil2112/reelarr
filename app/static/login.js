"use strict";
const post = (url, body) => fetch(url, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
}).then(async r => ({ ok: r.ok, ...(await r.json().catch(() => ({}))) }));

(async () => {
  const s = await fetch("/api/auth/status").then(r => r.json()).catch(() => ({}));
  document.getElementById("ver").textContent = s.version ? "v" + s.version : "";
  if (s.logged_in) { location.href = "/"; return; }
  document.getElementById(s.setup_required ? "setup" : "login").hidden = false;
  document.querySelector((s.setup_required ? "#setup" : "#login") + " input").focus();
})();

document.getElementById("setup").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target, msg = document.getElementById("setup-msg");
  if (f.password.value !== f.password2.value) { msg.textContent = "The passwords don't match."; return; }
  const r = await post("/api/auth/setup", { username: f.username.value, password: f.password.value });
  if (!r.ok) { msg.textContent = r.error || "Couldn't create the login."; return; }
  location.href = "/";
});

document.getElementById("nologin").addEventListener("click", async () => {
  const msg = document.getElementById("setup-msg");
  const r = await post("/api/auth/setup",
    { mode: "none", confirm: document.getElementById("nologin-confirm").value });
  if (!r.ok) { msg.textContent = r.error || "Couldn't save that."; return; }
  location.href = "/";
});

document.getElementById("login").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target, msg = document.getElementById("login-msg");
  const r = await post("/api/auth/login", { username: f.username.value, password: f.password.value });
  if (!r.ok) { msg.textContent = r.error || "Sign-in failed."; f.password.value = ""; return; }
  location.href = "/";
});
