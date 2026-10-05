/* Reelarr setup wizard. Every step saves as you leave it, so closing the tab
   halfway loses nothing; the watcher stays paused until "Finish". */
"use strict";
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const MASK = "••••••";

async function call(method, url, body) {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status === 401) { location.href = "/login"; throw new Error("login required"); }
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, ...data };
}
const get = u => call("GET", u), post = (u, b) => call("POST", u, b || {}), put = (u, b) => call("PUT", u, b);

const STEPS = [
  ["welcome", "Welcome"], ["folders", "Folders"], ["client", "Download client"],
  ["metadata", "Metadata"], ["import", "Start-up"], ["done", "Finish"],
];
let step = 0, st = null, proposedMap = null;

function msg(t, bad) { const m = $("#w-msg"); m.textContent = t || ""; m.className = "msg" + (bad ? " bad" : ""); }

function show(i) {
  step = Math.max(0, Math.min(STEPS.length - 1, i));
  $$(".wiz-step").forEach(s => s.hidden = s.dataset.step !== STEPS[step][0]);
  $("#wiz-steps").innerHTML = STEPS.map(([, label], n) =>
    `<li class="${n === step ? "on" : n < step ? "done" : ""}">${esc(label)}</li>`).join("");
  $("#w-back").disabled = step === 0;
  $("#w-skip").hidden = STEPS[step][0] !== "client" && STEPS[step][0] !== "metadata";
  $("#w-next").textContent = step === STEPS.length - 1 ? "Finish" : "Next";
  msg("");
  if (STEPS[step][0] === "done") renderSummary();
  window.scrollTo(0, 0);
}

/* ── load what's already set (a migrated install arrives prefilled) ── */
(async () => {
  st = await get("/api/setup/state");
  const info = [];
  if (st.in_container) {
    info.push(`Running in Docker as uid ${st.puid}, gid ${st.pgid}.`);
    info.push(`Folders Docker has given Reelarr: ${st.roots.length ? st.roots.join(", ") : "none — add a media mount to the compose file"}.`);
  }
  $("#env-info").textContent = info.join(" ");
  $("#folders-intro").textContent = st.in_container
    ? `Reelarr can only use folders Docker has mounted: ${st.roots.join(", ") || "(none yet)"}. Pick folders inside those, or create new ones.`
    : "Pick any folders on this computer. Reelarr creates artist folders inside the library.";
  $("#mount-help").hidden = !st.media_problem;
  $("#f-watch").value = st.paths.watch_dir || "";
  $("#f-library").value = st.paths.library_dir || "";

  const t = st.torrents;
  $("#c-enabled").checked = !!(t.enabled || t.url);
  $("#client-fields").hidden = !$("#c-enabled").checked;
  $("#c-client").value = t.client || "qbittorrent";
  $("#c-url").value = t.url || "";
  $("#c-username").value = t.username || "";
  $("#c-password").value = t.password || "";
  $("#c-label").value = t.label || "bootlegs";
  $("#c-import").value = t.import_completed ? (t.import_mode || "copy") : (t.url ? "off" : "copy");
  $("#c-torrent-dir").value = t.watch_dir || "";

  $("#m-key").value = st.sources.setlistfm_api_key || "";
  $("#m-ia").checked = st.sources.use_internet_archive !== false;

  const dry = st.safety.dry_run !== false;
  $$('[name="i-mode"]').forEach(r => r.checked = (r.value === "dry") === dry);
  $$('[name="i-conv"]').forEach(r => r.checked = r.value === (st.watcher.convert_target || "preserve"));
  $("#i-keep").checked = !!st.watcher.keep_wav_originals;
  const zones = (Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : [st.timezone || "UTC"]);
  const tz = st.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  $("#i-tz").innerHTML = zones.map(z => `<option ${z === tz ? "selected" : ""}>${esc(z)}</option>`).join("");
  show(0);
})();

$("#c-enabled").addEventListener("change", e => $("#client-fields").hidden = !e.target.checked);
$$('[name="i-mode"]').forEach(r => r.addEventListener("change",
  () => $("#i-live-confirm").hidden = $('[name="i-mode"]:checked').value !== "live"));

/* ── folder browser ── */
let browseTarget = null, browsePath = "", browseParent = null;
async function browse(path) {
  const r = await get("/api/setup/browse?path=" + encodeURIComponent(path || ""));
  if (!r.ok) {
    if (path) return browse("");          // a typed path that isn't there: start from the top
    $("#b-list").innerHTML = `<p class="bad">${esc(r.error)}</p>`; return;
  }
  browsePath = r.path; browseParent = r.parent;
  $("#b-path").textContent = r.path || "Folders Reelarr can reach";
  $("#b-up").disabled = r.parent === null && !r.path;
  $("#b-choose").disabled = !r.path;
  $("#b-new").disabled = !r.path;
  $("#b-list").innerHTML = r.dirs.length
    ? r.dirs.map(d => `<button class="b-dir" data-path="${esc(d.path)}">📁 ${esc(d.name)}</button>`).join("")
    : '<p class="hint">No folders in here.</p>';
  $$(".b-dir").forEach(b => b.addEventListener("click", () => browse(b.dataset.path)));
}
$$("[data-browse]").forEach(b => b.addEventListener("click", e => {
  e.preventDefault();
  browseTarget = b.dataset.browse;
  $("#browser").hidden = false;
  browse($("#" + browseTarget).value.trim());
}));
$("#b-up").addEventListener("click", () => browse(browseParent ?? ""));
$("#b-cancel").addEventListener("click", () => $("#browser").hidden = true);
$("#b-choose").addEventListener("click", () => {
  $("#" + browseTarget).value = browsePath;
  $("#browser").hidden = true;
});
$("#b-new").addEventListener("click", async () => {
  const name = prompt("Name for the new folder:");
  if (!name) return;
  const r = await post("/api/setup/mkdir", { path: browsePath.replace(/[\\/]+$/, "") + "/" + name.replace(/[\\/]/g, "") });
  if (!r.ok) return alert(r.error);
  browse(r.path);
});

/* ── download client test + path check ── */
function clientFields() {
  const f = { client: $("#c-client").value, url: $("#c-url").value.trim(),
              username: $("#c-username").value.trim(), password: $("#c-password").value,
              label: $("#c-label").value.trim() };
  if (proposedMap) f.path_map = { ...(st.torrents.path_map || {}), ...proposedMap };
  return f;
}
$("#c-test").addEventListener("click", async () => {
  const m = $("#c-msg");
  m.className = "msg hint mono"; m.textContent = "connecting…";
  const t = await post("/api/torrents/test", clientFields());
  if (!t.ok) { m.className = "msg bad mono"; m.textContent = "✗ " + (t.error || "couldn't connect"); return; }
  m.className = "msg ok mono";
  m.textContent = `✓ connected to ${t.client}${t.version ? " " + t.version : ""} — checking where it saves things…`;
  probe();
});
async function probe() {
  const r = await post("/api/setup/client/probe", clientFields());
  const box = $("#c-paths");
  if (!r.ok) { box.innerHTML = `<p class="bad">${esc(r.error)}</p>`; return; }
  let html = `<p class="hint">${esc(r.message || "")}</p>`;
  if (r.samples && r.samples.length) {
    html += `<div class="paths">${r.samples.map(s => `<div class="${s.visible ? "ok" : "bad"}">
      ${s.visible ? "✓" : "✗"} <span class="mono">${esc(s.client_path)}</span>
      ${s.local_path !== s.client_path ? ` → <span class="mono">${esc(s.local_path)}</span>` : ""}</div>`).join("")}</div>`;
  }
  if (r.suggestion) {
    const g = r.suggestion;
    html += `<div class="panel"><strong>Suggested path mapping</strong>
      <p class="hint">Your client and Reelarr see the same folder under different names. This rule fixes ${g.fixes} of ${g.of}:</p>
      <p class="mono">${esc(g.client)} &nbsp;→&nbsp; ${esc(g.local)}</p>
      <div class="row-actions"><button id="c-apply" class="brass small">Use this mapping</button></div></div>`;
  }
  box.innerHTML = html;
  const apply = $("#c-apply");
  if (apply) apply.addEventListener("click", async () => {
    const v = await post("/api/setup/client/verify-mapping", { client: r.suggestion.client, local: r.suggestion.local });
    if (!v.ok) return alert(v.error);
    proposedMap = { ...(proposedMap || {}), [r.suggestion.client]: r.suggestion.local };
    probe();
  });
}

/* ── saving each step ── */
async function saveStep(name) {
  if (name === "folders") {
    const r = await post("/api/setup/folders", { watch: $("#f-watch").value.trim(), library: $("#f-library").value.trim() });
    const box = $("#folders-result");
    box.innerHTML = (r.problems || []).map(p => `<p class="bad">✗ ${esc(p)}</p>`).join("")
                  + (r.notes || []).map(p => `<p class="hint">• ${esc(p)}</p>`).join("");
    if (!r.ok) throw new Error("fix the folders above first");
    return;
  }
  if (name === "client") {
    if (!$("#c-enabled").checked) { await put("/api/settings", { torrents: { enabled: false, import_completed: false } }); return; }
    const f = clientFields();
    if (!f.url) throw new Error("enter the client's web address, or untick it");
    const tdir = $("#c-torrent-dir").value.trim();
    if (tdir) {
      const c = await post("/api/setup/check-folders", { watch: $("#f-watch").value.trim(), library: $("#f-library").value.trim(), torrent_dir: tdir });
      const bad = (c.problems || []).filter(p => p.includes(".torrent folder"));
      if (bad.length) throw new Error(bad[0]);
    }
    const imp = $("#c-import").value;
    const patch = { enabled: true, client: f.client, url: f.url, username: f.username,
      label: f.label, watch_dir: tdir, import_completed: imp !== "off",
      import_mode: imp === "move" ? "move" : "copy" };
    if (f.password && f.password !== MASK) patch.password = f.password;
    if (f.path_map) patch.path_map = f.path_map;
    const r = await put("/api/settings", { torrents: patch });
    if (!r.ok) throw new Error(r.error || "couldn't save");
    return;
  }
  if (name === "metadata") {
    const key = $("#m-key").value.trim();
    const patch = { use_internet_archive: $("#m-ia").checked, use_setlistfm: !!key };
    if (key !== MASK) patch.setlistfm_api_key = key;
    await put("/api/settings", { sources: patch });
    return;
  }
  if (name === "import") {
    const live = $('[name="i-mode"]:checked').value === "live";
    const keep = $("#i-keep").checked;
    await put("/api/settings", { timezone: $("#i-tz").value,
      watcher: { convert_target: $('[name="i-conv"]:checked').value,
                 keep_wav_originals: keep, keep_shn_originals: keep } });
    const r = await post("/api/safety/dry-run", live ? { on: false, confirm: $("#i-confirm").value } : { on: true });
    if (!r.ok) throw new Error(r.error || "couldn't set the start-up mode");
  }
}

async function renderSummary() {
  const s = await get("/api/setup/state");
  const t = s.torrents;
  const rows = [
    ["Watch folder", s.paths.watch_dir],
    ["Library", s.paths.library_dir],
    ["Download client", t.enabled ? `${t.client} at ${t.url}${t.import_completed ? `, finished downloads ${t.import_mode === "move" ? "moved" : "copied"} in` : ""}` : "none"],
    ["Path mapping", Object.keys(t.path_map || {}).length ? Object.entries(t.path_map).map(([a, b]) => `${a} → ${b}`).join("; ") : "none needed"],
    [".torrent folder", t.watch_dir || "—"],
    ["setlist.fm", s.sources.setlistfm_api_key ? "key saved" : "not set"],
    ["Start-up", s.safety.dry_run ? "dry run (plans only)" : "live"],
    ["Conversion", s.watcher.convert_target === "cd" ? "CD quality" : "original quality"],
    ["Time zone", s.timezone],
  ];
  $("#summary").innerHTML = rows.map(([k, v]) => `<div><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join("");
}

$("#w-back").addEventListener("click", () => show(step - 1));
$("#w-skip").addEventListener("click", async () => {
  if (STEPS[step][0] === "client") await put("/api/settings", { torrents: { enabled: false, import_completed: false } });
  show(step + 1);
});
$("#w-next").addEventListener("click", async () => {
  const btn = $("#w-next");
  btn.disabled = true; msg("saving…");
  try {
    if (step === STEPS.length - 1) {
      const r = await post("/api/setup/finish", { index_library: $("#d-index").checked, scan_now: $("#d-scan").checked });
      if (!r.ok) throw new Error((r.problems && r.problems[0]) || r.error || "couldn't finish");
      location.href = "/";
      return;
    }
    await saveStep(STEPS[step][0]);
    show(step + 1);
  } catch (e) {
    msg(String(e.message || e), true);
  } finally {
    btn.disabled = false;
  }
});

/* ── "Reelarr can't see your music" help ── */
$$("#mh-tabs button").forEach(b => b.addEventListener("click", e => {
  e.preventDefault();
  $$("#mh-tabs button").forEach(x => x.classList.toggle("on", x === b));
  $$("[data-mh-body]").forEach(d => d.hidden = d.dataset.mhBody !== b.dataset.mh);
}));
$("#mh-check").addEventListener("click", async e => {
  e.preventDefault();
  const s2 = await get("/api/setup/state");
  st.roots = s2.roots;
  if (!s2.media_problem) {
    $("#mount-help").hidden = true;
    $("#folders-intro").textContent = `Reelarr can see: ${s2.roots.join(", ")}. Pick folders inside those.`;
  } else {
    $("#mh-msg").textContent = "still empty — did you recreate the container (docker compose up -d)?";
  }
});
