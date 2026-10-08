/* Reelarr GUI logic — vanilla JS, no dependencies. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const jsonOrErr = async (r) => {
  if (r.status === 401) { location.href = "/login"; return { error: "login required" }; }
  try { return await r.json(); }
  catch { return { error: "HTTP " + r.status + " " + r.statusText }; }
};
const api = {
  get: (url) => fetch(url).then(jsonOrErr).catch(e => ({ error: String(e) })),
  post: (url, body) => fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  }).then(jsonOrErr).catch(e => ({ error: String(e) })),
  put: (url, body) => fetch(url, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(jsonOrErr),
};

/* ── Navigation ── */
function bindNav(btn) {
  btn.addEventListener("click", () => {
    $$(".navbtn").forEach(b => b.classList.toggle("active", b === btn));
    $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + btn.dataset.view));
    refreshCurrent();
  });
}
$$(".navbtn").forEach(bindNav);

/* ── Add-ons (providers) ──
   An installed provider ships a script that calls these. Core never names
   a provider; if none is installed, none of this shows. */
const statusHooks = [];
const viewHooks = {};
window.Reelarr = {
  api, $: (s, el) => $(s, el), $$: (s, el) => $$(s, el),
  esc: s => esc(s), fmtTime: t => fmtTime(t),
  /** Add a page: a menu entry plus a <section>. onShow runs each time it opens. */
  addView(id, label, html, onShow) {
    const btn = document.createElement("button");
    btn.className = "navbtn"; btn.dataset.view = id; btn.textContent = label;
    $("#provider-nav").appendChild(btn);
    bindNav(btn);
    const sec = document.createElement("section");
    sec.id = "view-" + id; sec.className = "view"; sec.innerHTML = html;
    $("#view-settings").before(sec);
    if (onShow) viewHooks[id] = onShow;
    return sec;
  },
  /** Called with every /api/status poll; the provider's slice is s.providers[name]. */
  onStatus(fn) { statusHooks.push(fn); },
  refreshStatus: () => refreshStatus(),
};

async function loadProviders() {
  const r = await api.get("/api/providers");
  for (const p of (r.providers || [])) {
    if (p.settings_layout && p.settings_layout.length)
      SETTINGS_LAYOUT.push([p.label, p.name, p.settings_layout]);
    if (p.script) {
      await new Promise(done => {
        const el = document.createElement("script");
        el.src = p.script; el.onload = done; el.onerror = done;
        document.body.appendChild(el);
      });
    }
  }
  settingsCache = null;
}

function currentView() {
  return $(".navbtn.active").dataset.view;
}

/* ── Helpers ── */
const fmtTime = ts => {
  if (!ts) return "–";
  const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  return isNaN(d) ? String(ts) : d.toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
  });
};
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const STATUS_LABELS = { filed: "filed", review: "needs review", error: "error",
  rejected: "rejected", pending: "waiting", processing: "processing", planned: "planned",
  combined: "combined" };
const statusLabel = s => STATUS_LABELS[s] || s;

/* ── Safety: dry-run banner, version ── */
async function refreshSafety() {
  const s = await api.get("/api/safety");
  if (s.error) return;
  $("#safety-banner").hidden = !s.dry_run;
  $("#safety-planned").textContent = s.planned
    ? ` ${s.planned} show${s.planned === 1 ? "" : "s"} planned.` : "";
}

$("#btn-go-live").addEventListener("click", async () => {
  const s = await api.get("/api/safety");
  const n = s.planned || 0;
  const typed = prompt(
    "Going live means Reelarr will start moving, renaming, retagging and trashing files for real.\n\n" +
    (n ? `${n} planned show${n === 1 ? "" : "s"} will be processed straight away.\n\n` : "") +
    "Every change is journalled and can be undone from Activity, and nothing is deleted — " +
    "removed files go to a .reelarr-trash folder for " + (s.trash_days || 30) + " days.\n\n" +
    "Type GO LIVE to confirm.");
  if (typed === null) return;
  const r = await api.post("/api/safety/dry-run", { on: false, confirm: typed });
  if (r.error) { alert(r.error); return; }
  refreshSafety(); refreshStatus(); refreshCurrent();
});

(async () => {
  const sys = await api.get("/api/system");
  if (sys.version) $("#stat-version").textContent = "v" + sys.version;
})();

/* ── Status (rail + counts) ── */
async function refreshStatus() {
  refreshSafety();
  try {
    const s = await api.get("/api/status");
    $("#stat-watch").textContent = s.folders_in_watch ?? "–";
    $("#stat-filed").textContent = (s.counts.filed ?? 0);
    const next = s.next_runs?.watch_scan;
    const scanBtn = $("#btn-scan");
    if (s.scan_running) {
      $("#stat-nextscan").textContent = "scanning the watch folder…";
      scanBtn.textContent = "Stop scanning";
      scanBtn.dataset.mode = "stop";
      scanBtn.classList.remove("brass"); scanBtn.classList.add("quiet");
    } else {
      $("#stat-nextscan").textContent = next ? "next scan " + fmtTime(next) : "watching is off";
      scanBtn.textContent = "Scan now";
      scanBtn.dataset.mode = "sweep";
      scanBtn.classList.remove("quiet"); scanBtn.classList.add("brass");
    }
    const rc = s.counts.review ?? 0;
    const badge = $("#review-count");
    badge.hidden = rc === 0;
    badge.textContent = rc;
    statusHooks.forEach(fn => { try { fn(s); } catch (e) { console.error(e); } });
    const li = s.library_index || {};
    const idxNote = $("#save-note");
    if (idxNote && !idxNote.textContent) {
      idxNote.textContent = li.scanning
        ? `library index: scanning… (${li.folders ?? ""})`
        : `library index: ${li.artists ?? 0} artists · ${li.venues ?? 0} venues · ${li.hosts ?? 0} host mappings`;
    }
    if (!s.watch_dir_ok || !s.library_dir_ok) {
      $("#stat-nextscan").textContent = "⚠ volume not mounted — check docker-compose paths";
    }
  } catch (e) { /* backend restarting; try again next poll */ }
}

/* ── Pagination ── */
const PAGE = 50;
const pages = { shows: 0, review: 0, activity: 0 };

function pager(view, total, refresh) {
  const el = $("#" + view + "-pager");
  if (!el) return;
  const totalPages = Math.max(1, Math.ceil(total / PAGE));
  if (pages[view] >= totalPages) pages[view] = totalPages - 1;   // list shrank
  if (totalPages <= 1) { el.innerHTML = ""; return; }
  const p = pages[view];
  el.innerHTML = `
    <button class="quiet small" data-nav="prev" ${p === 0 ? "disabled" : ""}>&larr; Newer</button>
    <span class="pageinfo mono">page ${p + 1} of ${totalPages} &middot; ${total} entries</span>
    <button class="quiet small" data-nav="next" ${p >= totalPages - 1 ? "disabled" : ""}>Older &rarr;</button>`;
  $$("[data-nav]", el).forEach(b => b.addEventListener("click", () => {
    pages[view] += b.dataset.nav === "next" ? 1 : -1;
    refresh();
  }));
}

/* ── Shows ── */
let showsStatus = "";
$$("#shows-tabs button").forEach(b => b.addEventListener("click", () => {
  showsStatus = b.dataset.status; pages.shows = 0;
  $$("#shows-tabs button").forEach(x => x.classList.toggle("on", x === b));
  refreshManifest();
}));
async function refreshManifest() {
  const data = await api.get(`/api/shows?limit=${PAGE}&offset=${pages.shows * PAGE}` +
    (showsStatus ? `&status=${showsStatus}` : ""));
  const shows = data.items;
  pager("shows", data.total, refreshManifest);
  const el = $("#shows-list");
  if (!data.total) {
    el.innerHTML = showsStatus ? '<p class="empty">Nothing here.</p>'
      : '<p class="empty">No shows yet. Put a show folder in the watch folder, or find some under Discover.</p>';
    return;
  }
  el.innerHTML = shows.map(s => {
    const conf = s.confidence ?? 0;
    const planBtn = s.status === "planned"
      ? ` <button class="quiet small" data-plan="${s.id}">${openPlans.has(s.id) ? "hide plan" : "show plan"}</button>` : "";
    const planBox = s.status === "planned" && openPlans.has(s.id)
      ? `<div class="plan">${renderPlan(planCache[s.id])}</div>` : "";
    const gcls = conf >= 85 ? "high" : conf >= 50 ? "" : "low";
    const detail = s.status === "filed" ? esc(s.filed_path)
      : s.status === "error" ? esc(s.error)
      : esc((s.missing || []).join(", ") && "missing: " + (s.missing || []).join(", ") || s.notes || "");
    return `<div class="entry">
      <span class="when">${fmtTime(s.updated_at)}</span>
      <span class="stamp ${esc(s.status)}">${esc(statusLabel(s.status))}</span>
      <span class="gauge ${gcls}" title="confidence ${conf}"><i style="width:${conf}%"></i></span>
      <span class="what">${esc(s.folder_name)}${detail ? ` <span class="dim">— ${detail}</span>` : ""}${planBtn}${planBox}</span>
    </div>`;
  }).join("");
  $$("#shows-list [data-plan]").forEach(b => b.addEventListener("click", async () => {
    const id = Number(b.dataset.plan);
    if (openPlans.has(id)) openPlans.delete(id);
    else { planCache[id] = await api.get(`/api/show/${id}/plan`); openPlans.add(id); }
    refreshManifest();
  }));
}

const openPlans = new Set(), planCache = {};
const shortPath = p => esc(String(p || "").split("/").slice(-2).join("/"));

function renderPlan(plan) {
  if (!plan || !plan.ops) return '<span class="dim">no plan recorded</span>';
  const ops = plan.ops, lines = [];
  const count = k => ops.filter(o => o.op === k);
  count("extract").forEach(o => lines.push(`extract ${esc(o.file)}`));
  count("flatten").forEach(o => lines.push(`flatten the single subfolder ${esc(o.folder)}/`));
  count("convert").forEach(o => lines.push(`convert ${esc(o.file)} → ${esc(o.to)} (${esc(o.target)})${o.keep_original ? ", keeping the original" : ""}`));
  count("trash").forEach(o => lines.push(`trash ${shortPath(o.src)} <span class="dim">(${esc(o.reason || "")})</span>`));
  const tags = ops.find(o => o.op === "tags");
  if (tags) lines.push(`retag ${tags.files} file(s): ALBUM “${esc(tags.album)}”, ARTIST “${esc(tags.artist)}”` +
    (tags.album_artist && tags.album_artist !== tags.artist ? `, ALBUMARTIST “${esc(tags.album_artist)}”` : "") +
    `, ${tags.titled} track title(s) from the setlist`);
  count("mkdir").forEach(o => lines.push(`create folder ${shortPath(o.dst)}`));
  count("move").forEach(o => lines.push(`move ${shortPath(o.src)} → <strong>${esc(o.dst)}</strong>`));
  return lines.map(l => `<div>· ${l}</div>`).join("") || '<span class="dim">nothing to do</span>';
}

/* ── Review queue ── */
const FIELDS = ["artist", "host_artist", "date", "venue", "city", "state",
  "source_type", "official", "shnid", "recorder", "genre"];
const FIELD_LABELS = { host_artist: "Album Artist", source_type: "source", state: "state / country",
  shnid: "SHNID" };
const fieldLabel = f => FIELD_LABELS[f] || f.replace("_", " ");
let artistList = [];

async function refreshReview() {
  const [data, artists] = await Promise.all([
    api.get(`/api/shows?status=review&limit=${PAGE}&offset=${pages.review * PAGE}`),
    api.get("/api/artists")]);
  const shows = data.items;
  artistList = artists;
  pager("review", data.total, refreshReview);
  const el = $("#review-list");
  $("#review-empty").hidden = data.total > 0;
  const isDup = s => /duplicate/i.test(s.notes || "");
  const dupes = shows.filter(isDup), regular = shows.filter(s => !isDup(s));
  $("#dupes-head").hidden = dupes.length === 0;
  $("#dupes-count").textContent = dupes.length ? `(${dupes.length} on this page)` : "";
  const card = s => {
    const m = s.meta || {};
    const missing = s.missing || [];
    const prov = s.provenance || {};
    const provTxt = FIELDS.filter(f => prov[f]).map(f => `${f}←${prov[f]}`).join("  ");
    return `<div class="card" data-id="${s.id}">
      <h3>${esc(s.folder_name)}</h3>
      <p class="why">Confidence ${s.confidence}${missing.length ? " — missing " + esc(missing.join(", ")) : ""}${s.notes ? " — " + esc(s.notes) : ""}</p>
      ${provTxt ? `<p class="prov">sources: ${esc(provTxt)}</p>` : ""}
      ${(m.parts || []).length ? `<p class="hint">Early + late show, filed together: ${m.parts.map(p =>
          `disc ${p.disc} = ${esc(p.dir)}${p.bracket ? ` [${esc(p.bracket)}]` : ""}`).join(" · ")}.
          Album bracket: <span class="mono">[${esc(m.bracket_override || "")}]</span> — editing the source fields below replaces it.</p>` : ""}
      <div class="fields">
        ${FIELDS.map(f => `
          <div class="field">
            <label for="f-${s.id}-${f}">${fieldLabel(f)}${["artist","date","venue","city","state"].includes(f) ? " *" : ""}</label>
            <input id="f-${s.id}-${f}" data-field="${f}" value="${esc(m[f] || "")}"
              ${f === "artist" || f === "host_artist" ? 'list="artist-options"' : ""}
              ${f === "date" ? 'placeholder="YYYY-MM-DD"' : ""}>
          </div>`).join("")}
      </div>
      ${(m.parts || []).length ? "" : `<details class="trk" data-trk ${/Titles need a look/.test(s.notes || "") ? "open" : ""}>
        <summary>Track titles <span class="hint" data-trk-sum></span></summary>
        <p class="hint">Tick <strong>not a song</strong> for tuning, crowd or break tracks — the songs then line up with the rest, in order.</p>
        <div class="tbl-scroll"><table class="tbl trk-tbl"><thead><tr><th class="num">#</th><th>File</th><th class="num">Length</th><th>Title it gets</th><th>Not a song</th></tr></thead><tbody></tbody></table></div>
        <p class="hint mono" data-trk-msg></p>
      </details>`}
      <div class="row-actions">
        ${/duplicate/i.test(s.notes || "") ? '<button class="brass" data-act="replace">Replace the old copy</button>' : ""}
        <button class="${/duplicate/i.test(s.notes || "") ? "quiet" : "brass"}" data-act="approve">File it${/duplicate/i.test(s.notes || "") ? " alongside" : ""}</button>
        <button class="quiet" data-act="setlist">Pull from setlist.fm</button>
        <button class="quiet" data-act="reprocess">Take another look</button>
        <button class="quiet" data-act="reject">Reject</button>
        <span class="hint mono" data-msg></span>
      </div>
    </div>`;
  };
  $("#dupes-list").innerHTML = dupes.map(card).join("");
  el.innerHTML = regular.map(card).join("");

  let dl = $("#artist-options");
  if (!dl) {
    dl = document.createElement("datalist");
    dl.id = "artist-options";
    document.body.appendChild(dl);
  }
  dl.innerHTML = artistList.map(a => `<option value="${esc(a)}">`).join("");

  $$("#review-list [data-trk], #dupes-list [data-trk]").forEach(d => {
    const load = () => { if (!d.dataset.loaded) loadTracks(d.closest(".card")); };
    if (d.open) load();
    d.addEventListener("toggle", () => { if (d.open) load(); });
  });

  $$("#review-list [data-act], #dupes-list [data-act]").forEach(btn => btn.addEventListener("click", async () => {
    const card = btn.closest(".card");
    const id = card.dataset.id;
    const msg = $("[data-msg]", card);
    btn.disabled = true;
    try {
      if (btn.dataset.act === "approve" || btn.dataset.act === "replace") {
        const meta = {};
        $$("input[data-field]", card).forEach(i => meta[i.dataset.field] = i.value.trim());
        if (card.dataset.extras) meta.extras = JSON.parse(card.dataset.extras);
        const r = await api.post(`/api/show/${id}/${btn.dataset.act}`, meta);
        msg.textContent = r.error ? "✗ " + r.error
          : (btn.dataset.act === "replace" ? "✓ replaced the old copy → " : "✓ filed at ") + r.dest;
        if (!r.error) setTimeout(refreshReview, 900);
      } else if (btn.dataset.act === "reprocess") {
        await api.post(`/api/show/${id}/reprocess`);
        msg.textContent = "reprocessing…";
        setTimeout(refreshReview, 2500);
      } else if (btn.dataset.act === "setlist") {
        // read the card's current artist/date (they may have been edited)
        const get = f => (($(`input[data-field="${f}"]`, card) || {}).value || "").trim();
        const artist = get("artist"), date = get("date");
        msg.textContent = "searching setlist.fm…";
        const r = await api.post(`/api/show/${id}/setlist_lookup`,
          { artist, date });
        if (r.error) { msg.textContent = "✗ " + r.error; }
        else if (!r.found) { msg.textContent = r.message || "no match found"; }
        else {
          // fill venue/city/state into the editable fields for review
          const setF = (f, v) => { const el = $(`input[data-field="${f}"]`, card); if (el && v) el.value = v; };
          setF("venue", r.venue); setF("city", r.city); setF("state", r.state);
          const tl = r.track_count
            ? `, ${r.track_count} tracks (approve to write the setlist)` : "";
          msg.textContent = `✓ ${esc(r.venue || "venue found")}, ${esc(r.city || "")}${tl} — check it, then File it`;
          // stash tracks on the card so approve can send them along
          if (r.tracks && r.tracks.length) card.dataset.slfTracks = JSON.stringify(r.tracks);
          const trk = $("[data-trk]", card);
          if (trk) { delete card.dataset.extras; trk.dataset.loaded = ""; if (trk.open) loadTracks(card); }
        }
      } else {
        await api.post(`/api/show/${id}/reject`);
        card.remove();
      }
    } finally { btn.disabled = false; }
  }));
}

// Review → Track titles: which file gets which title, and which aren't songs.
const fmtLen = sec => sec == null ? "?" : `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, "0")}`;
async function loadTracks(card, extras) {
  const d = $("[data-trk]", card);
  const body = extras ? { extras } : {};
  const r = await api.post(`/api/show/${card.dataset.id}/tracks`, body);
  d.dataset.loaded = "1";
  const msg = $("[data-trk-msg]", d);
  if (r.error) { msg.textContent = "✗ " + r.error; return; }
  // File it writes exactly what's shown here
  card.dataset.extras = JSON.stringify(["", ...r.extras]);
  $("tbody", d).innerHTML = r.entries.map((e, i) => `<tr class="${e.kind === "extra" ? "trk-extra" : e.kind === "unmatched" ? "trk-none" : ""}">
      <td class="num mono">${i + 1}</td>
      <td class="mono trk-file" title="${esc(e.name)}">${esc(e.name)}</td>
      <td class="num mono">${fmtLen(e.seconds)}</td>
      <td>${e.title ? esc(e.title) : '<span class="hint">no title</span>'}</td>
      <td><input type="checkbox" data-extra="${esc(e.name)}" aria-label="${esc(e.name)} is not a song" ${e.kind === "extra" ? "checked" : ""}></td></tr>`).join("");
  const nExtra = r.entries.filter(e => e.kind === "extra").length;
  $("[data-trk-sum]", d).textContent = `— ${r.entries.length} files, ${r.songs} songs${nExtra ? `, ${nExtra} not ${nExtra === 1 ? "a song" : "songs"}` : ""}${r.ok ? "" : " · needs a look"}`;
  msg.className = "mono " + (r.ok ? "hint" : "bad");
  msg.textContent = r.ok ? (r.songs ? "✓ every song has a file" : "No setlist yet — Pull from setlist.fm, or file it with the files' own titles.")
    : "✗ " + r.note + (nExtra && r.how !== "yours" ? " — the ticked file is a guess: check it, then File it." : "");
  $$("[data-extra]", d).forEach(c => c.addEventListener("change", () =>
    // "" leads the list so "none ticked" still counts as your choice
    loadTracks(card, ["", ...$$("[data-extra]", d).filter(x => x.checked).map(x => x.dataset.extra)])));
}

$("#btn-toss-dupes").addEventListener("click", async () => {
  $("#btn-toss-dupes").disabled = true;
  $("#toss-result").textContent = "rejecting duplicates…";
  const r = await api.post("/api/review/toss_duplicates");
  $("#btn-toss-dupes").disabled = false;
  $("#toss-result").textContent = `${r.tossed} duplicate(s) tossed to _duplicates`;
  setTimeout(refreshReview, 800);
});

/* ── Activity ── */
const openActions = new Set(), actionOps = {};

async function refreshActions() {
  const r = await api.get("/api/activity/actions?limit=25");
  const el = $("#actions-list");
  if (!r.items || !r.items.length) {
    el.innerHTML = '<p class="empty">Nothing has been changed on disk yet.</p>';
    return;
  }
  el.innerHTML = r.items.map(a => {
    const undone = a.undone_at
      ? `<span class="dim">undone ${fmtTime(a.undone_at)} — ${esc(a.undo_note)}</span>`
      : `<button class="quiet small" data-undo="${a.id}">Undo</button>`;
    const ops = openActions.has(a.id) && actionOps[a.id]
      ? `<div class="plan">${actionOps[a.id].map(o =>
          `<div>· ${esc(o.op)} ${shortPath(o.src)}${o.dst && o.op !== "retag" ? " → " + shortPath(o.dst) : ""}</div>`).join("")}</div>` : "";
    return `<div class="entry">
      <span class="when">${fmtTime(a.ts)}</span>
      <span class="stamp ${a.undone_at ? "rejected" : "filed"}">${esc(a.kind || "action")}</span>
      <span class="what">${esc(a.label)} <span class="dim">— ${a.n_ops} change(s)</span>
        <button class="quiet small" data-ops="${a.id}">${openActions.has(a.id) ? "hide" : "details"}</button>
        ${undone}${ops}</span>
    </div>`;
  }).join("");
  $$("#actions-list [data-ops]").forEach(b => b.addEventListener("click", async () => {
    const id = Number(b.dataset.ops);
    if (openActions.has(id)) openActions.delete(id);
    else { actionOps[id] = (await api.get(`/api/activity/actions/${id}`)).ops || []; openActions.add(id); }
    refreshActions();
  }));
  $$("#actions-list [data-undo]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("Undo this action? Files go back where they were and their old tags are restored.")) return;
    b.disabled = true; b.textContent = "undoing…";
    const res = await api.post(`/api/activity/actions/${b.dataset.undo}/undo`);
    if (res.error) alert(res.error);
    else if (res.conflicts && res.conflicts.length)
      alert(`Restored ${res.restored} item(s), but:\n\n` + res.conflicts.join("\n"));
    refreshActions(); refreshLog(); refreshStatus();
  }));
}

async function refreshLog() {
  refreshActions();
  const data = await api.get(`/api/log?limit=${PAGE}&offset=${pages.activity * PAGE}`);
  pager("activity", data.total, refreshLog);
  $("#activity-list").innerHTML = data.items.map(r => `<div class="entry">
    <span class="when">${fmtTime(r.ts)}</span>
    <span class="stamp ${r.level === "error" ? "error" : r.level === "warn" ? "review" : "filed"}">${esc(r.event)}</span>
    <span class="what">${esc(r.detail)}</span>
  </div>`).join("");
}

/* ── Settings ── */
const SETTINGS_LAYOUT = [
  ["Watch folder", "watcher", [
    ["enabled", "checkbox", "Watch folder processing on/off"],
    ["poll_minutes", "number", "Minutes between scans of the watch folder"],
    ["settle_minutes", "number", "A folder must sit unchanged this long before it's touched (lets downloads finish)"],
    ["auto_extract_archives", "checkbox", "Unzip archives found inside show folders"],
    ["convert_shn_to_flac", "checkbox", "Convert .shn to FLAC"],
    ["convert_target", "select:preserve,cd", "preserve = keep the source's bit depth and sample rate (lossless). cd = 16-bit/44.1 kHz with dither"],
    ["pair_wait_minutes", "number", "A folder marked as an early or late show waits up to this many minutes for its other half, so the two can be filed together (0 = don't wait)"],
    ["max_extract_gb", "number", "Refuse to unpack an archive that would come out bigger than this many GB (also refuses zip bombs). Programs and links found inside are removed"],
    ["keep_shn_originals", "checkbox", "Keep the .shn after conversion"],
    ["convert_wav_to_flac", "checkbox", "Convert .wav to lossless FLAC (WAV can't carry tags)"],
    ["keep_wav_originals", "checkbox", "Keep the .wav after conversion"],
    ["audio_source_analysis", "checkbox", "When no text mentions a source, measure crowd noise at track edges to guess AUD vs SBD"],
    ["dedupe_formats", "checkbox", "When a track exists in several formats, keep the best and move the rest to trash: FLAC > WAV > SHN > OGG > MP3 > AFPK. Identically numbered tracks in different disc folders are never treated as duplicates"],
    ["scrub_junk_files", "checkbox", "Move trade debris to trash: .torrent, .json, .json.gz, spectrogram .png (album art is safe)"],
  ]],
  ["Safety", "safety", [
    ["trash_days", "number", "Days removed files stay in .reelarr-trash before they're purged for good (0 = keep forever). Dry run is switched from the banner, not here"],
    ["undo_keep_days", "number", "Days the undo journal is kept"],
  ]],
  ["Filing policy", "filing", [
    ["auto_file_threshold", "number", "Confidence (0–100) required to file without review"],
    ["duplicate_policy", "select:upgrade,review,file_anyway", "upgrade = compare source (SBD > MTX > AUD), tracks, titles & size — keep the better copy, retire the other to _replaced"],
    ["hold_uncertain_titles", "checkbox", "Send shows to Review when their setlist can't be checked against setlist.fm or known songs — keeps junk titles out of your media server"],
    ["new_artist_policy", "select:auto,review", "auto = create new artist folders freely"],
    ["default_source_type", "select:AUD,AUD.FOB,SBD,MTX,SBD.FM", "Bracket used when no source can be detected — album tags always carry [SOURCE]"],
    ["folder_prefix_style", "select:slug,name", "slug → gratefuldead2024-06-01 · name → Grateful Dead 2024-06-01"],
    ["official_labels", "text", "Names that mark an official release (comma-separated) — e.g. the stores or labels you buy from. Official releases are tagged as such and never replaced by fan recordings. “Official” in a [bracket] always counts"],
  ]],
  ["Permissions", "permissions", [
    ["puid", "number", "Owner UID (unRAID nobody = 99)"],
    ["pgid", "number", "Owner GID (unRAID users = 100)"],
    ["dir_mode", "text", "Directory mode, octal"],
    ["file_mode", "text", "File mode, octal"],
  ]],
  ["Places", "places", [
    ["enabled", "checkbox", "Use the built-in world city list (GeoNames) to fill a missing state from the city, and outside the US and Canada to write the country in full in place of the state — Amsterdam, Netherlands"],
  ]],
  ["Metadata sources", "sources", [
    ["use_internet_archive", "checkbox", "Look up etree-named folders on archive.org"],
    ["use_setlistfm", "checkbox", "Confirm venue/city and fetch setlists"],
    ["setlistfm_api_key", "password", "Free key from api.setlist.fm"],
    ["use_musicbrainz_genre", "checkbox", "Fill the genre tag from MusicBrainz"],
  ]],
  ["Torrents", "torrents", [
    ["enabled", "checkbox", "Watch a folder for .torrent files and hand them to your torrent client"],
    ["watch_dir", "text", "Folder Reelarr watches for new .torrent files"],
    ["client", "select:deluge,qbittorrent,transmission", "Which torrent client to hand them to"],
    ["url", "text", "The client's web address — the same one you open in a browser. Deluge :8112, qBittorrent :8080, Transmission :9091"],
    ["username", "text", "Web UI username (qBittorrent and Transmission; leave blank for Deluge)"],
    ["password", "password", "Web UI password (not a daemon password)"],
    ["label", "text", "Label applied to every torrent added — a category in qBittorrent. Deluge forces lowercase with no spaces, so 'My Shows' becomes 'my-shows'"],
    ["download_dir", "text", "Optional: where the client should save them. This is a path as the CLIENT sees it, not Reelarr. Blank = the client's own default"],
    ["add_paused", "checkbox", "Add torrents paused instead of starting straight away"],
    ["poll_minutes", "number", "Minutes between checks of the torrent folder"],
    ["settle_seconds", "number", "Leave a .torrent alone until it's sat unchanged this long (lets the file finish writing)"],
    ["after_add", "select:move,delete", "What to do with the .torrent once the client has it: move it to _added/ or delete it"],
    ["import_completed", "checkbox", "Closes the loop: when a torrent finishes, bring it into the watch folder so Reelarr tags and files it automatically"],
    ["import_mode", "select:copy,move", "copy leaves the original alone so the torrent keeps seeding (recommended). move takes it, which stops seeding"],
    ["min_age_minutes", "number", "Wait this long after a torrent finishes before importing it"],
  ]],
];

let settingsCache = null;

async function refreshSettings() {
  settingsCache = await api.get("/api/settings");
  const form = $("#settings-form");
  form.innerHTML = SETTINGS_LAYOUT.map(([title, group, items]) => `
    <div class="sgroup">
      <h3>${title}</h3>
      <div class="sgrid">
        ${items.map(([key, type, desc]) => {
          const val = settingsCache[group]?.[key];
          const id = `s-${group}-${key}`;
          let input;
          if (type === "checkbox") {
            input = `<input type="checkbox" id="${id}" data-group="${group}" data-key="${key}" ${val ? "checked" : ""}>`;
          } else if (type.startsWith("select:")) {
            const opts = type.split(":")[1].split(",");
            input = `<select id="${id}" data-group="${group}" data-key="${key}">
              ${opts.map(o => `<option value="${o}" ${String(val) === o ? "selected" : ""}>${o}</option>`).join("")}
            </select>`;
          } else {
            input = `<input type="${type}" id="${id}" data-group="${group}" data-key="${key}" value="${esc(val ?? "")}">`;
          }
          return `<div class="sitem">
            <label for="${id}">${key.replaceAll("_", " ")}</label>
            ${input}
            <div class="desc">${desc}</div>
          </div>`;
        }).join("")}
      </div>
    </div>`).join("") + `
    <div class="sgroup">
      <h3>Artist aliases</h3>
      <p class="hint">One per line, <span class="mono">prefix = Full Artist Name</span>. Used when folder names use etree acronyms.</p>
      <textarea id="s-aliases" rows="10" spellcheck="false">${
        Object.entries(settingsCache.artists.aliases)
          .map(([k, v]) => `${k} = ${v}`).join("\n")}</textarea>
      <div class="sitem" style="margin-top:12px">
        <label for="s-cutoff">fuzzy match cutoff (0.5–1.0)</label>
        <input type="text" id="s-cutoff" value="${settingsCache.artists.match_cutoff}">
        <div class="desc">How close a parsed artist must be to an existing library folder to map onto it</div>
      </div>
    </div>
    <div class="sgroup">
      <h3>Country spellings</h3>
      <p class="hint">Outside the US and Canada the country goes where the state would. To write one your own way, one per line, <span class="mono">Country = Your spelling</span> — e.g. <span class="mono">United Kingdom = England</span>.</p>
      <textarea id="s-countries" rows="4" spellcheck="false">${
        Object.entries((settingsCache.places || {}).country_names || {})
          .map(([k, v]) => `${k} = ${v}`).join("\n")}</textarea>
    </div>`;
}

$("#btn-save").addEventListener("click", async (e) => {
  e.preventDefault();
  const patch = {};
  $$("#settings-form [data-group]").forEach(el => {
    const g = el.dataset.group, k = el.dataset.key;
    patch[g] = patch[g] || {};
    let v;
    if (el.type === "checkbox") v = el.checked;
    else if (el.type === "number" || el.tagName === "SELECT" && /^\d+$/.test(el.value)) v = Number(el.value);
    else v = el.value;
    patch[g][k] = v;
  });
  const aliases = {};
  $("#s-aliases").value.split("\n").forEach(line => {
    const m = line.match(/^\s*([^=]+?)\s*=\s*(.+?)\s*$/);
    if (m) aliases[m[1].toLowerCase().replace(/[^a-z0-9]/g, "")] = m[2];
  });
  patch.artists = { aliases, match_cutoff: parseFloat($("#s-cutoff").value) || 0.88 };
  const country_names = {};
  ($("#s-countries")?.value || "").split("\n").forEach(line => {
    const m = line.match(/^\s*([^=]+?)\s*=\s*(.+?)\s*$/);
    if (m) country_names[m[1]] = m[2];
  });
  patch.places = { ...(patch.places || {}), country_names };
  await api.put("/api/settings", patch);
  $("#save-note").textContent = "✓ saved";
  setTimeout(() => $("#save-note").textContent = "", 4000);
});

/* ── Buttons ── */
$("#btn-index").addEventListener("click", async () => {
  await api.post("/api/library/rescan");
  $("#save-note").textContent = "re-reading the library in the background…";
  setTimeout(() => $("#save-note").textContent = "", 5000);
});
$("#btn-scan").addEventListener("click", async () => {
  const btn = $("#btn-scan");
  if (btn.dataset.mode === "stop") {
    btn.disabled = true;
    const r = await api.post("/api/scan/stop");
    $("#stat-nextscan").textContent = r.message || "stopping…";
    setTimeout(() => { btn.disabled = false; refreshStatus(); }, 1500);
    return;
  }
  await api.post("/api/scan?force=true");
  $("#stat-nextscan").textContent = "scanning the watch folder…";
  setTimeout(() => { refreshStatus(); refreshCurrent(); }, 1500);
});
/* ── Maintenance ── */
async function pollJob(statusUrl, onDone, onTick) {
  for (let i = 0; i < 300; i++) {              // up to ~10 min
    const s = await api.get(statusUrl);
    if (s.status === "done") return onDone(s);
    if (s.status === "error") return onDone(s);
    if (onTick) onTick(s);
    await new Promise(r => setTimeout(r, 2000));
  }
  onDone({ status: "error", error: "timed out waiting for the job" });
}

function showExportLink(name) {
  const a = $("#export-link");
  a.hidden = false;
  a.href = "/api/audit/download/" + encodeURIComponent(name);
  a.textContent = "⬇ " + name + " — download, then drop it into Claude";
}

$("#btn-learn").addEventListener("click", async () => {
  $("#btn-learn").disabled = true;
  $("#learn-result").textContent = "comparing records with what's on disk…";
  await api.post("/api/audit/learn");
  pollJob("/api/audit/learn/status", (r) => {
    $("#btn-learn").disabled = false;
    if (r.status === "error") { $("#learn-result").textContent = "✗ " + (r.error || "failed"); return; }
    const na = Object.keys(r.artists_learned || {}).length;
    const nv = Object.keys(r.venues_learned || {}).length;
    $("#learn-result").textContent =
      `${r.shows_checked} shows checked · ${na} artist + ${nv} venue correction(s) learned · ` +
      `${r.reconciled} records reconciled · ${r.total_corrections} standing corrections`;
  });
});

$("#btn-export").addEventListener("click", async () => {
  $("#btn-export").disabled = true;
  const a = $("#export-link");
  a.hidden = false; a.removeAttribute("href");
  a.textContent = "building the report…";
  await api.post("/api/audit/export");
  pollJob("/api/audit/export/status", (r) => {
    $("#btn-export").disabled = false;
    if (r.status === "error") { a.textContent = "✗ export failed: " + (r.error || ""); return; }
    showExportLink(r.name);
  }, () => { a.textContent = "building the report… (still working)"; });
});

// opening Maintenance shows any past exports immediately
$$(".navbtn").forEach(b => b.addEventListener("click", async () => {
  if (b.dataset.view !== "maintenance") return;
  const s = await api.get("/api/audit/export/status");
  if (s.exports && s.exports.length) showExportLink(s.exports[0]);
}));
$("#btn-suspects").addEventListener("click", async () => {
  $("#btn-suspects").disabled = true;
  $("#suspect-count").textContent = "(searching…)";
  await api.post("/api/audit/suspects/run");
  await new Promise(done => pollJob("/api/audit/suspects/status", (r) => {
    $("#btn-suspects").disabled = false;
    if (r.status === "error") { $("#suspect-count").textContent = "✗ " + (r.error || "failed"); return done(); }
    renderSuspects(r.rows || []); done();
  }, (s) => { $("#suspect-count").textContent = `(searching… ${s.scanned || 0} shows checked)`; }));
});
function renderSuspects(rows) {
  $("#suspect-count").textContent = rows.length ? `(${rows.length})` : "(none)";
  $("#suspect-list").innerHTML = rows.length ? rows.map(s => `<div class="entry">
    <span class="stamp review">suspect</span>
    <span class="what">${esc(s.path ? s.path.split("/").slice(-2).join("/") : s.artist_folder)}
      <span class="dim">— ${esc(s.flags.join(" · "))}</span></span>
  </div>`).join("") : '<p class="empty">No suspects. The papers are in order.</p>';
}

$("#btn-return").addEventListener("click", async () => {
  $("#btn-return").disabled = true;
  $("#return-result").textContent = "moving them back to the watch folder…";
  await api.post("/api/audit/return");
  pollJob("/api/audit/return/status", (r) => {
    $("#btn-return").disabled = false;
    $("#return-result").textContent = r.status === "error"
      ? "✗ " + (r.error || "failed")
      : `${r.moved} show(s) back in the watch folder — they'll be reprocessed on the next scan` +
        (r.skipped ? ` (${r.skipped} had no folder)` : "");
    $("#btn-suspects").click();
  });
});

function runBacktag(dryRun) {
  const btns = [$("#btn-backtag-preview"), $("#btn-backtag-run")];
  btns.forEach(b => b.disabled = true);
  $("#backtag-progress").hidden = false;
  $("#backtag-progress").textContent = "starting…";
  $("#backtag-result").textContent = "";
  api.post(`/api/backtag/run?dry_run=${dryRun}`).then(() => {
    pollJob("/api/backtag/status", (r) => {
      btns.forEach(b => b.disabled = false);
      $("#backtag-progress").hidden = true;
      if (r.status === "error") {
        $("#backtag-result").textContent = "✗ " + (r.error || "failed");
        return;
      }
      $("#backtag-result").textContent = dryRun
        ? `previewed ${r.total} shows — see proposed changes below`
        : `done: ${r.applied} fixed in place, ${r.brigged} sent to Review, ${r.skipped} already clean`;
      renderBacktag(r.proposals || [], dryRun);
    }, (s) => {
      $("#backtag-progress").textContent =
        `checking against setlist.fm… ${s.done || 0}/${s.total || "?"}` +
        (s.current ? ` — ${s.current.slice(0, 44)}` : "");
    });
  });
}

function renderBacktag(props, dryRun) {
  const shown = props.filter(p => p.verdict !== "skip");
  $("#backtag-count").textContent = shown.length ? `(${shown.length} with changes)` : "(none)";
  if (!shown.length) {
    $("#backtag-list").innerHTML = '<p class="empty">Nothing to change — every show checks out.</p>';
    return;
  }
  $("#backtag-list").innerHTML = shown.map(p => {
    const stamp = p.verdict === "apply"
      ? '<span class="stamp filed">' + (dryRun ? "would fix" : "fixed") + '</span>'
      : '<span class="stamp review">' + (dryRun ? "would review" : "to review") + '</span>';
    const v = p.changes && p.changes.venue
      ? `<div class="dim">venue: ${esc(p.changes.venue.from)} → ${esc(p.changes.venue.to)}</div>` : "";
    const tf = (p.title_fixes && p.title_fixes.length)
      ? `<div class="dim">${p.title_fixes.length} junk title(s) → ${esc(p.title_fixes.slice(0,2).map(t=>t.to).join(", "))}${p.title_fixes.length>2?"…":""}</div>` : "";
    const why = p.verdict === "brig" ? `<div class="dim">⚠ ${esc(p.reason)}</div>` : "";
    return `<div class="entry"><div>${stamp}
      <span class="what">${esc((p.folder||"").split("/").slice(-1)[0])}</span></div>
      ${v}${tf}${why}</div>`;
  }).join("");
}

$("#btn-backtag-preview").addEventListener("click", () => runBacktag(true));
$("#btn-backtag-run").addEventListener("click", () => {
  if (!confirm("Apply fixes across the whole library? Confident venue and title fixes are retagged in place; uncertain ones go to Review. Preview first if you haven't.")) return;
  runBacktag(false);
});

/* ── Polling ── */
function refreshCurrent() {
  const v = currentView();
  if (viewHooks[v]) { try { viewHooks[v](); } catch (e) { console.error(e); } }
  if (v === "shows") refreshManifest();
  else if (v === "review") refreshReview();
  else if (v === "activity") refreshLog();
  else if (v === "artists") refreshArtists();
  else if (v === "queue") refreshQueue();
  else if (v === "notes") refreshNotes();
  else if (v === "system") refreshSystem();
  else if (v === "discover") initDiscover();
  else if (v === "settings" && !settingsCache) refreshSettings();
  if (v === "settings") { refreshCorrections(); refreshTorrents(); refreshSecurity(); }
}

async function refreshCorrections() {
  const r = await api.get("/api/corrections");
  const list = r.corrections || [];
  $("#corr-count").textContent = list.length ? `(${list.length})` : "";
  if (!list.length) {
    $("#corr-list").innerHTML = '<p class="empty">No learned corrections yet.</p>';
    return;
  }
  $("#corr-list").innerHTML = list.map(c => {
    const to = typeof c.right === "object"
      ? [c.right.venue, c.right.city, c.right.state].filter(Boolean).join(", ")
      : c.right;
    return `<div class="entry">
      <span class="stamp ${c.kind === "artist" ? "review" : "filed"}">${esc(c.kind)}</span>
      <span class="what">${esc(c.wrong)} → ${esc(String(to))}</span>
      <button class="quiet small" data-corr-del="${esc(c.kind)}|${esc(c.wrong)}"
        style="margin-left:auto">remove</button>
    </div>`;
  }).join("");
  $$("#corr-list [data-corr-del]").forEach(b => b.addEventListener("click", async () => {
    const [kind, wrong] = b.dataset.corrDel.split("|");
    await api.post("/api/corrections/delete", { kind, wrong });
    refreshCorrections();
  }));
}

(async () => {
  // first run (or an install that's just been carried over): the
  // wizard decides the folders before anything else happens
  const sys = await api.get("/api/system");
  if (sys && sys.setup_done === false) { location.href = "/setup"; return; }
  await loadProviders();
  refreshStatus(); refreshCurrent();
})();
setInterval(refreshStatus, 6000);
setInterval(() => { if (["shows", "activity"].includes(currentView())) refreshCurrent(); }, 8000);
setInterval(() => { if (currentView() === "queue") refreshQueue(); }, 4000);   // live speeds and ETAs

/* ── Security ── */
async function refreshSecurity() {
  const r = await api.get("/api/security");
  if (r.error) return;
  $("#sec-mode").textContent = r.mode === "none"
    ? "OFF — anyone who can reach this port has full control"
    : `on — signed in as ${r.username || "?"}`;
  $("#sec-forms").hidden = r.mode === "none";
  $("#sec-none").hidden = r.mode !== "none";
  $("#sec-key").dataset.key = r.api_key;
  if ($("#sec-key").textContent !== "hidden") $("#sec-key").textContent = r.api_key;
}
const secMsg = (t, ok) => { const m = $("#sec-msg"); m.className = "msg mono " + (ok ? "ok" : "bad"); m.textContent = t; };
$("#sec-show-key").addEventListener("click", () => {
  const k = $("#sec-key");
  k.textContent = k.textContent === "hidden" ? (k.dataset.key || "") : "hidden";
});
$("#sec-new-key").addEventListener("click", async () => {
  if (!confirm("Make a new API key? Anything using the old one stops working.")) return;
  const r = await api.post("/api/security/apikey");
  $("#sec-key").dataset.key = r.api_key; $("#sec-key").textContent = r.api_key;
  secMsg("✓ new key — update your scripts", true);
});
$("#sec-pw").addEventListener("click", async () => {
  const r = await api.post("/api/security/password", { current: $("#sec-cur").value, new: $("#sec-new").value });
  if (r.error) return secMsg("✗ " + r.error);
  location.href = "/login";      // every session was signed out, including this one
});
$("#sec-off").addEventListener("click", async () => {
  const pw = prompt("Turning login off means anyone who can reach Reelarr's port can move and retag your files.\n\nEnter your current password to confirm:");
  if (pw === null) return;
  const r = await api.post("/api/security/mode", { mode: "none", password: pw });
  if (r.error) return secMsg("✗ " + r.error);
  secMsg("login is off", true); refreshSecurity();
});
$("#sec-on").addEventListener("click", async () => {
  const r = await api.post("/api/security/mode",
    { mode: "forms", username: $("#sec-user").value, password: $("#sec-pass").value });
  if (r.error) return secMsg("✗ " + r.error);
  secMsg("✓ login is on", true); refreshSecurity(); refreshAuth();
});

async function refreshAuth() {
  const r = await api.get("/api/auth/status");
  $("#btn-logout").hidden = !(r.via === "session");
}
$("#btn-logout").addEventListener("click", async () => {
  await api.post("/api/auth/logout");
  location.href = "/login";
});
refreshAuth();

/* ── Torrent intake ── */
async function refreshTorrents() {
  const s = await api.get("/api/torrents/status");
  if (s.error) return;
  const bits = [];
  if (!s.enabled) bits.push("off");
  else if (!s.configured) bits.push("needs a folder and a client address");
  else bits.push(s.running ? "checking now…" : "watching");
  if (s.enabled && s.watch_dir_ok === false) bits.push("⚠ folder not found");
  if (s.waiting) bits.push(`${s.waiting} waiting`);
  $("#tor-state").textContent = bits.join(" · ");
  $("#tor-last").textContent = s.last_result || "—";
}

$("#tor-test").addEventListener("click", async () => {
  const m = $("#tor-msg");
  m.className = "msg"; m.textContent = "trying the client…";
  const r = await api.post("/api/torrents/test", {});
  if (r.error) { m.className = "msg bad"; m.textContent = "✗ " + r.error; return; }
  let out = `✓ ${r.client}` + (r.version ? ` ${r.version}` : "") + " reachable";
  if (!r.label_plugin) {
    out += ` — but ${r.label_term}s unavailable: ${r.label_error || "not supported"}`;
    m.className = "msg bad";
  } else {
    out += `; ${r.labels.length} ${r.label_term}(s)`;
    out += r.label_exists
      ? `, "${r.label}" already there`
      : `, "${r.label}" will be created on first use`;
    m.className = "msg ok";
  }
  m.textContent = out;
});

$("#tor-import").addEventListener("click", async () => {
  const m = $("#tor-msg");
  m.className = "msg"; m.textContent = "asking the client what's finished…";
  await api.post("/api/torrents/import");
  setTimeout(async () => {
    await refreshTorrents();
    m.className = "msg"; m.textContent = $("#tor-last").textContent || "";
  }, 3000);
});

$("#tor-scan").addEventListener("click", async () => {
  const m = $("#tor-msg");
  m.className = "msg"; m.textContent = "checking the folder…";
  await api.post("/api/torrents/scan");
  setTimeout(async () => { await refreshTorrents(); m.textContent = ""; }, 2500);
});


/* ── Shared bits for the newer pages ── */
const SRC_CLASS = s => !s ? "" : /^SBD/.test(s) ? "good" : /MTX/.test(s) ? "good" : /AUD/.test(s) ? "warn" : "";
const pills = (list, cls = "") => (list || []).map(x => `<span class="pill ${cls}">${esc(x)}</span>`).join("");
function vu(fraction, segments = 12) {
  const on = Math.round(Math.max(0, Math.min(1, fraction || 0)) * segments);
  return `<span class="vu" title="${Math.round((fraction || 0) * 100)}%">` +
    Array.from({ length: segments }, (_, i) =>
      `<i class="${i < on ? "on" : ""}${i < on && fraction >= 1 ? " done" : ""}${i < on && i >= segments - 2 && fraction < 1 ? " hot" : ""}"></i>`).join("") +
    "</span>";
}
function releaseRow(r, actions, opts = {}) {
  const fm = r.formats || [];
  return `<td class="mono">${esc(r.date || "—")}</td>
    <td><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title || r.id)}</a>
      ${r.owned ? '<span class="pill good">in library</span>' : ""}
      ${r.restricted ? `<span class="pill bad" title="${esc(r.restricted)}">can't download</span>` : ""}</td>
    <td>${r.source_type ? `<span class="pill ${SRC_CLASS(r.source_type)}">${esc(r.source_type)}</span>` : '<span class="hint">?</span>'}</td>
    ${opts.formats === false ? "" : `<td class="hide-sm">${pills(fm.map(f => f === "flac24" ? "FLAC 24" : f.toUpperCase()), r.lossless ? "good" : "")}</td>`}
    ${actions}`;
}

/* ── Discover ── */
let disState = { q: "", page: 1, total: 0, rows: 25, indexer: "lma", results: [] };
let disReady = false;
async function initDiscover() {
  if (disReady) return;
  disReady = true;
  const r = await api.get("/api/monitor/artists");
  $("#dis-indexer").innerHTML = (r.indexers || [{ name: "lma", label: "Live Music Archive" }])
    .map(i => `<option value="${esc(i.name)}">${esc(i.label)}</option>`).join("");
}
async function disSearch(page) {
  const q = $("#dis-q").value.trim();
  if (!q) { $("#dis-msg").textContent = "type something first"; return; }
  Object.assign(disState, { q, page: page || 1, indexer: $("#dis-indexer").value || "lma" });
  const m = $("#dis-msg"); m.className = "msg hint mono"; m.textContent = "searching…";
  const r = await api.post("/api/discover/search", { q, page: disState.page, rows: disState.rows, indexer: disState.indexer });
  if (r.error) { m.className = "msg bad mono"; m.textContent = "✗ " + r.error; $("#dis-table").hidden = true; $("#dis-pager").hidden = true; return; }
  disState.total = r.total || 0; disState.results = r.results || [];
  const kind = r.kind === "collection" ? `collection ${r.query}` : r.kind === "item" ? "one show" : "search";
  m.textContent = disState.results.length ? `${kind}: ${disState.total.toLocaleString()} found` : "nothing found";
  $("#dis-table").hidden = !disState.results.length;
  $("#dis-pager").hidden = !disState.results.length;
  $("#dis-prev").disabled = disState.page <= 1;
  $("#dis-next").disabled = disState.page * disState.rows >= disState.total;
  $("#dis-table tbody").innerHTML = disState.results.map(x => `<tr>${releaseRow(x,
    `<td class="hide-sm mono">${(x.downloads || 0).toLocaleString()}</td>
     <td class="actions"><button class="${x.owned ? "quiet" : "brass"} small" data-grab="${esc(x.id)}">${x.owned ? "Grab anyway" : "Grab"}</button></td>`)}</tr>`).join("");
  $$("#dis-table [data-grab]").forEach(b => b.addEventListener("click", () => disGrab([b.dataset.grab], b)));
}
async function disGrab(ids, btn) {
  const m = $("#dis-msg");
  if (btn) { btn.disabled = true; btn.textContent = "grabbing…"; }
  m.className = "msg hint mono"; m.textContent = `checking and fetching ${ids.length}…`;
  const r = await api.post("/api/discover/grab", { indexer: disState.indexer, ids });
  if (r.error) { m.className = "msg bad mono"; m.textContent = "✗ " + r.error; if (btn) { btn.disabled = false; btn.textContent = "Grab"; } return; }
  const blocked = Object.entries(r.blocked || {});
  let out = `✓ ${r.grabbed} sent to the .torrent folder`;
  if (r.skipped) out += `, ${r.skipped} already there`;
  if (r.failed) out += `, ${r.failed} failed`;
  if (blocked.length) out += ` — ${blocked.length} can't be downloaded: ${blocked.map(([i, why]) => `${i} (${why})`).join("; ")}`;
  if (r.handed_on) out += " — handing to your client now";
  m.className = (r.failed || blocked.length) ? "msg bad mono" : "msg ok mono"; m.textContent = out;
  if (btn) btn.textContent = blocked.length ? "can't download" : r.grabbed ? "grabbed" : "already had";
}
$("#dis-search").addEventListener("click", () => disSearch(1));
$("#dis-q").addEventListener("keydown", e => { if (e.key === "Enter") disSearch(1); });
$("#dis-prev").addEventListener("click", () => disSearch(Math.max(1, disState.page - 1)));
$("#dis-next").addEventListener("click", () => disSearch(disState.page + 1));
$("#dis-grab-all").addEventListener("click", () => {
  const ids = disState.results.filter(x => !x.owned).map(x => x.id);
  if (!ids.length) return;
  if (confirm(`Grab ${ids.length} show(s)?`)) disGrab(ids, null);
});

/* ── Artists ── */
const SOURCE_CHOICES = ["SBD", "MTX", "AUD", "FM"];
let artistEditing = null;
async function refreshArtists() {
  const r = await api.get("/api/monitor/artists");
  if (r.error) return;
  $("#art-library").innerHTML = (r.library || []).map(a => `<option value="${esc(a)}">`).join("");
  const st = r.status || {};
  const every = (r.settings || {}).interval_hours || 12;
  $("#art-status").textContent = st.running ? `checking ${st.current || "…"}`
    : `Checked every ${every} hours` + (st.last ? ` — last ${fmtTime(Number(st.last))}` : "") + ".";
  const g = r.gates || [];
  $("#art-gates").hidden = !g.length;
  $("#art-gates").innerHTML = g.map(x => `<p>${esc(x)}</p>`).join("");
  $("#art-empty").hidden = (r.artists || []).length > 0;
  $("#art-table").hidden = !(r.artists || []).length;
  const idx = r.indexers || [];
  $("#art-table tbody").innerHTML = (r.artists || []).map(a => {
    const looks = [a.sources.length ? a.sources.join("/") : "any source",
                   a.require_lossless ? "lossless" : "any format",
                   a.upgrades ? "+ upgrades" : ""].filter(Boolean).join(" · ");
    const row = `<tr data-id="${a.id}">
      <td><strong>${esc(a.name)}</strong>${a.monitored ? "" : ' <span class="pill">paused</span>'}
        ${a.collection ? `<div class="hint mono">collection: ${esc(a.collection)}</div>` : ""}</td>
      <td class="mono">${esc(looks)}</td>
      <td class="hide-sm">${a.action === "grab" ? '<span class="pill warn">grab automatically</span>' : '<span class="pill">ask me</span>'}</td>
      <td class="hide-sm mono">${a.checked_at ? fmtTime(a.checked_at) : "not yet"}${a.last_note ? `<div class="hint" style="white-space:normal">${esc(a.last_note)}</div>` : ""}</td>
      <td>${a.wanted ? `<span class="pill" style="color:var(--violet)">${a.wanted}</span>` : '<span class="hint">0</span>'}</td>
      <td class="actions"><button class="quiet small" data-art="check">Check</button><button class="quiet small" data-art="lookback-open" title="Search this artist's older uploads">Look back</button><button class="quiet small" data-art="edit">Edit</button></td>
    </tr>`;
    if (artistEditing !== a.id) return row;
    return row + `<tr><td colspan="6"><div class="drawer" data-edit="${a.id}">
      <div class="grid">
        <div><label>Monitoring</label><select data-k="monitored"><option value="1" ${a.monitored ? "selected" : ""}>on</option><option value="0" ${a.monitored ? "" : "selected"}>paused</option></select></div>
        <div><label>When something new turns up</label><select data-k="action"><option value="wanted" ${a.action === "wanted" ? "selected" : ""}>put it in the Queue for me</option><option value="grab" ${a.action === "grab" ? "selected" : ""}>grab it automatically</option></select></div>
        <div><label>Sources I'll take</label>${SOURCE_CHOICES.map(sc => `<label class="check" style="margin:4px 0"><input type="checkbox" data-src="${sc}" ${a.sources.includes(sc) ? "checked" : ""}> ${sc}</label>`).join("")}<span class="hint">none ticked = any</span></div>
        <div><label>Formats</label><label class="check"><input type="checkbox" data-k="require_lossless" ${a.require_lossless ? "checked" : ""}> lossless only</label>
             <label class="check"><input type="checkbox" data-k="upgrades" ${a.upgrades ? "checked" : ""}> also better sources of dates I have</label></div>
        <div><label>Indexer</label><select data-k="indexer">${idx.map(i => `<option value="${esc(i.name)}" ${a.indexer === i.name ? "selected" : ""}>${esc(i.label)}</option>`).join("")}</select></div>
        <div><label>Archive collection <span class="hint">(optional)</span></label><input type="text" data-k="collection" value="${esc(a.collection || "")}" placeholder="e.g. GratefulDead"><span class="hint">More exact than matching the artist name.</span></div>
      </div>
      <div class="row-actions"><button class="brass small" data-art="save">Save</button><button class="quiet small" data-art="cancel">Cancel</button><span style="flex:1"></span><button class="quiet small" data-art="remove">Stop monitoring</button></div>
      <div class="row-actions"><label style="margin:0">Look back again:</label>
        <select data-lookback><option value="30">last 30 days</option><option value="365">last year</option><option value="-1" selected>everything on the archive</option></select>
        <button class="quiet small" data-art="lookback">Search</button>
        <span class="hint">Re-judges anything passed over before; your grabs and ignores stay put.</span></div>
      ${a.last_note ? `<p class="hint mono">${esc(a.last_note)}</p>` : ""}
    </div></td></tr>`;
  }).join("");
  $$("#art-table [data-art]").forEach(b => b.addEventListener("click", () => artistAction(b)));
}
async function artistAction(b) {
  const tr = b.closest("tr"), drawer = b.closest(".drawer");
  const id = Number((drawer && drawer.dataset.edit) || (tr && tr.dataset.id));
  const act = b.dataset.art, m = $("#art-msg");
  if (act === "edit") { artistEditing = artistEditing === id ? null : id; return refreshArtists(); }
  if (act === "lookback-open") {
    artistEditing = id;
    await refreshArtists();
    const sel = $(`[data-edit="${id}"] [data-lookback]`);
    if (sel) { sel.scrollIntoView({ block: "center" }); sel.focus(); }
    return;
  }
  if (act === "cancel") { artistEditing = null; return refreshArtists(); }
  if (act === "check") {
    b.disabled = true; b.textContent = "checking…";
    const r = await api.post(`/api/monitor/artists/${id}/check`);
    m.textContent = r.error ? "✗ " + r.error : (r.summary || `${r.artist}: ${r.new} new`);
    refreshArtists(); refreshStatus();
    return;
  }
  if (act === "lookback") {
    const days = $("[data-lookback]", drawer).value;
    const r = await api.post(`/api/monitor/artists/${id}/lookback`, { days: Number(days) });
    m.textContent = r.error ? "✗ " + r.error : `looking back for ${r.name} in the background — results in Queue and Activity`;
    artistEditing = null; setTimeout(refreshArtists, 3000);
    return;
  }
  if (act === "remove") {
    if (!confirm("Stop monitoring this artist? Their shows stay in your library.")) return;
    await fetch(`/api/monitor/artists/${id}`, { method: "DELETE" });
    artistEditing = null; return refreshArtists();
  }
  if (act === "save") {
    const name = $(`#art-table tr[data-id="${id}"] strong`).textContent;
    const body = { name, sources: $$("[data-src]", drawer).filter(x => x.checked).map(x => x.dataset.src) };
    $$("[data-k]", drawer).forEach(x => body[x.dataset.k] = x.type === "checkbox" ? x.checked
      : x.dataset.k === "monitored" ? x.value === "1" : x.value);
    const r = await api.post("/api/monitor/artists", body);
    m.textContent = r.error ? "✗ " + r.error : "✓ saved";
    artistEditing = null; refreshArtists();
  }
}
$("#art-add").addEventListener("click", async () => {
  const name = $("#art-name").value.trim();
  if (!name) return;
  const lookback_days = Number($("#art-lookback").value), action = $("#art-action").value;
  const r = await api.post("/api/monitor/artists", { name, monitored: true, lookback_days, action });
  $("#art-msg").textContent = r.error ? "✗ " + r.error
    : `✓ monitoring ${r.name}` + (r.checking ? " — checking now; results appear in Queue and Activity" : "");
  if (!r.error) { $("#art-name").value = ""; }
  refreshArtists();
  if (r.checking) { setTimeout(refreshArtists, 4000); setTimeout(refreshArtists, 15000); }
});
$("#art-name").addEventListener("keydown", e => { if (e.key === "Enter") $("#art-add").click(); });
$("#art-run").addEventListener("click", async () => {
  const r = await api.post("/api/monitor/run");
  $("#art-msg").textContent = r.error ? "✗ " + r.error : "checking every monitored artist in the background…";
  setTimeout(refreshArtists, 1500);
});

/* ── Queue ── */
// Every table here sorts by any column (click; click again to reverse) and the
// search box above them filters all five at once. Sort choices are remembered.
const STATUS_ORDER = ["downloading", "metadata", "checking", "stalled", "queued", "paused", "error",
                      "import pending", "seeding", "imported"];
const SRC_RANK = ["SBD", "MTX", "MATRIX", "PRE-FM", "FM", "AUD"];
function fmtBytes(n) {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
}
const fmtRate = n => (n ? fmtBytes(n) + "/s" : "—");
function fmtEta(sec) {
  if (sec == null) return "—";
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m` : `${sec}s`;
}
const statusRank = s => { const i = STATUS_ORDER.indexOf(s); return i < 0 ? 99 : i; };
const srcRank = s => { if (!s) return null; const i = SRC_RANK.findIndex(x => s.toUpperCase().startsWith(x)); return i < 0 ? 50 : i; };
const lc = s => (s || "").toLowerCase();
const progressCell = p => p == null ? '<span class="hint">—</span>'
  : `<span style="white-space:nowrap"><span class="hide-sm">${vu(p)}</span> <span class="hint mono">${(p * 100).toFixed(p < 1 ? 1 : 0)}%</span></span>`;
const stStamp = s => s ? `<span class="stamp st-${esc(s.replace(/\s+/g, "-"))}">${esc(s)}</span>` : '<span class="hint">—</span>';

let qQuery = "";
const qTables = {};

/* A sortable, searchable table.
   cols: [{key, label, cls, title, sort(row)→value, cell(row)→html, desc}]  (desc = first click sorts high→low)
   hay(row) → the text the search box looks through. */
function qTable(id, { cols, hay, sort, empty, storeKey, after }) {
  const t = { id, cols, hay, rows: [], empty, after,
              storeKey: storeKey || `reelarr.queue.sort.${id}`, sort };
  try { const saved = JSON.parse(localStorage.getItem(t.storeKey)); if (saved && cols.some(c => c.key === saved.key)) t.sort = saved; } catch { /* private mode */ }
  const table = $("#" + id);
  table.querySelector("thead").innerHTML = "<tr>" + cols.map(c =>
    `<th${c.sort ? ` data-sort="${c.key}" tabindex="0"` : ""}${c.cls ? ` class="${c.cls}"` : ""}${c.title ? ` title="${esc(c.title)}"` : ""}>${c.label}</th>`).join("") + "</tr>";
  const pick = th => {
    const key = th.dataset.sort, col = cols.find(c => c.key === key);
    t.sort = t.sort.key === key ? { key, dir: -t.sort.dir } : { key, dir: col.desc ? -1 : 1 };
    try { localStorage.setItem(t.storeKey, JSON.stringify(t.sort)); } catch { /* private mode */ }
    qRender(id);
  };
  table.querySelectorAll("th[data-sort]").forEach(th => {
    th.addEventListener("click", () => pick(th));
    th.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(th); } });
  });
  qTables[id] = t;
  return t;
}

function qMatches(t, row) {
  if (!qQuery) return true;
  const text = lc(t.hay(row).filter(x => x != null && x !== "").join(" "));
  return qQuery.split(/\s+/).every(w => text.includes(w));
}

function qRender(id) {
  const t = qTables[id];
  const col = t.cols.find(c => c.key === t.sort.key) || t.cols.find(c => c.sort);
  const dir = t.sort.dir;
  const tie = (a, b) => String(t.hay(a)[0] ?? "").localeCompare(String(t.hay(b)[0] ?? ""));
  const rows = t.rows.filter(r => qMatches(t, r));
  rows.sort((a, b) => {
    const va = col.sort(a), vb = col.sort(b);
    const na = va == null || va === "", nb = vb == null || vb === "";
    if (na && nb) return tie(a, b);
    if (na) return 1;                       // unknowns always last, whichever way
    if (nb) return -1;
    if (va < vb) return -dir;
    if (va > vb) return dir;
    return tie(a, b);
  });
  const table = $("#" + id);
  table.querySelectorAll("th[data-sort]").forEach(th => {
    if (th.dataset.sort === col.key) th.setAttribute("aria-sort", dir > 0 ? "ascending" : "descending");
    else th.removeAttribute("aria-sort");
  });
  table.querySelector("tbody").innerHTML = rows.map(r =>
    `<tr${r.id != null ? ` data-rid="${esc(r.id)}"` : ""}>` + t.cols.map(c => `<td${c.cls ? ` class="${c.cls}"` : ""}>${c.cell(r)}</td>`).join("") + "</tr>").join("");
  const wrap = table.closest(".tbl-scroll");
  wrap.hidden = !rows.length;
  const e = $(`#${id}-empty`);
  e.hidden = rows.length > 0;
  e.textContent = t.rows.length ? `Nothing here matches “${qQuery}”.` : t.empty;
  const n = $(`#${id}-n`);
  if (n) n.textContent = !t.rows.length ? "" : rows.length === t.rows.length ? `(${t.rows.length})` : `(${rows.length} of ${t.rows.length})`;
  if (t.after) t.after(rows);
  return rows.length;
}

function qRenderAll() {
  let shown = 0, total = 0;
  for (const id of Object.keys(qTables)) { shown += qRender(id); total += qTables[id].rows.length; }
  $("#q-search-n").textContent = qQuery ? `${shown} of ${total} match` : "";
}

const releaseCols = (whyLabel) => [
  { key: "date", label: "Date", cls: "mono", sort: x => x.data.date, cell: x => esc(x.data.date || "—") },
  { key: "show", label: "Show", sort: x => lc(x.data.title || x.release_id), cell: x =>
      `<a href="${esc(x.data.url)}" target="_blank" rel="noopener">${esc(x.data.title || x.release_id)}</a>
       ${x.data.owned ? '<span class="pill good">in library</span>' : ""}
       ${x.data.restricted ? `<span class="pill bad" title="${esc(x.data.restricted)}">can't download</span>` : ""}` },
  { key: "artist", label: "Artist", cls: "hide-sm", sort: x => lc(x.artist_name), cell: x => esc(x.artist_name || "—") },
  { key: "source", label: "Source", sort: x => srcRank(x.data.source_type), cell: x =>
      x.data.source_type ? `<span class="pill ${SRC_CLASS(x.data.source_type)}">${esc(x.data.source_type)}</span>` : '<span class="hint">?</span>' },
  { key: "why", label: whyLabel, cls: "hide-sm hint", sort: x => lc(x.reason), cell: x => esc(x.reason || "") },
  { key: "found", label: "Found", cls: "num mono hide-md", desc: true, sort: x => x.updated_at, cell: x => fmtTime(x.updated_at) },
];
const releaseHay = x => [x.data.title || x.release_id, x.data.date, x.artist_name, x.data.source_type,
                         x.reason, x.release_id, (x.data.formats || []).join(" ")];

qTable("q-wanted", {
  cols: [...releaseCols("Why"), { key: "act", label: "", cls: "actions", cell: () =>
    '<button class="brass small" data-q="grab">Grab</button><button class="quiet small" data-q="ignore">Ignore</button>' }],
  hay: releaseHay, sort: { key: "date", dir: -1 },
  empty: "Nothing waiting. Monitored artists' new uploads appear here.",
});

qTable("q-down", {
  storeKey: "reelarr.queue.sort",          // the sort you picked before 0.1.1 carries over
  sort: { key: "queue", dir: 1 },
  empty: "Nothing downloading.",
  hay: d => [d.name, d.status || d.state, d.label],
  cols: [
    { key: "queue", label: "#", cls: "num mono", title: "Position in the download client's queue", sort: d => d.queue, cell: d => d.queue ?? "—" },
    { key: "name", label: "Name", cls: "tname", sort: d => lc(d.name), cell: d => `<div title="${esc(d.name)}">${esc(d.name)}</div>` },
    { key: "status", label: "Status", cls: "hide-sm", sort: d => statusRank(d.status), cell: d => stStamp(d.status || d.state || "?") },
    { key: "progress", label: "Progress", desc: true, sort: d => d.progress, cell: d => progressCell(d.progress) },
    { key: "size", label: "Size", cls: "num mono hide-sm", desc: true, sort: d => d.size, cell: d => fmtBytes(d.size) },
    { key: "dl_speed", label: "Down", cls: "num mono hide-sm", desc: true, sort: d => d.dl_speed, cell: d => fmtRate(d.dl_speed) },
    { key: "ul_speed", label: "Up", cls: "num mono hide-md", desc: true, sort: d => d.ul_speed, cell: d => fmtRate(d.ul_speed) },
    { key: "eta", label: "ETA", cls: "num mono", sort: d => d.eta, cell: d => fmtEta(d.eta) },
    { key: "seeds", label: "S / P", cls: "num mono hide-sm", title: "Seeds / peers", desc: true,
      sort: d => d.seeds == null ? null : d.seeds * 10000 + (d.peers || 0), cell: d => `${d.seeds ?? "—"} / ${d.peers ?? "—"}` },
    { key: "ratio", label: "Ratio", cls: "num mono hide-md", desc: true, sort: d => d.ratio, cell: d => d.ratio == null ? "—" : d.ratio.toFixed(2) },
    { key: "added", label: "Added", cls: "num mono hide-md", desc: true, sort: d => d.added, cell: d => d.added ? fmtTime(d.added) : "—" },
  ],
  after: rows => {
    const down = rows.reduce((t, d) => t + (d.dl_speed || 0), 0), up = rows.reduce((t, d) => t + (d.ul_speed || 0), 0);
    const left = rows.reduce((t, d) => t + (d.size != null && d.done != null ? Math.max(0, d.size - d.done) : 0), 0);
    $("#q-down-total").textContent = rows.length
      ? `${rows.length} item${rows.length === 1 ? "" : "s"} · ↓ ${fmtRate(down)} · ↑ ${fmtRate(up)} · ${fmtBytes(left)} left`
      : "";
  },
});

qTable("q-intake", {
  sort: { key: "when", dir: -1 },
  empty: "Nothing in the watch folder waiting to be filed.",
  hay: i => [i.folder_name, statusLabel(i.status), i.notes],
  cols: [
    { key: "when", label: "When", cls: "mono nowrap", desc: true, sort: i => i.updated_at, cell: i => fmtTime(i.updated_at) },
    { key: "status", label: "Status", sort: i => lc(statusLabel(i.status)), cell: i => `<span class="stamp ${esc(i.status)}">${esc(statusLabel(i.status))}</span>` },
    { key: "folder", label: "Folder", cls: "tname mono", sort: i => lc(i.folder_name), cell: i => `<div title="${esc(i.folder_name)}">${esc(i.folder_name)}</div>` },
    { key: "notes", label: "Notes", cls: "hide-sm hint", sort: i => lc(i.notes), cell: i => esc(i.notes || "") },
  ],
});

qTable("q-grabbed", {
  sort: { key: "grabbed", dir: -1 },
  empty: "Nothing grabbed yet.",
  hay: g => [g.data.title || g.release_id, g.data.date, g.artist_name, g.data.source_type,
             g.client && g.client.status, g.release_id],
  cols: [
    { key: "grabbed", label: "Grabbed", cls: "mono nowrap", desc: true, sort: g => g.updated_at, cell: g => fmtTime(g.updated_at) },
    { key: "date", label: "Date", cls: "mono hide-sm", sort: g => g.data.date, cell: g => esc(g.data.date || "—") },
    { key: "show", label: "Show", sort: g => lc(g.data.title || g.release_id), cell: g =>
        g.data.url ? `<a href="${esc(g.data.url)}" target="_blank" rel="noopener">${esc(g.data.title || g.release_id)}</a>` : esc(g.data.title || g.release_id) },
    { key: "artist", label: "Artist", cls: "hide-sm", sort: g => lc(g.artist_name), cell: g => esc(g.artist_name || "—") },
    { key: "status", label: "Status", cls: "hide-sm", sort: g => g.client ? statusRank(g.client.status) : null, cell: g => g.client ? stStamp(g.client.status) :
        '<span class="hint" title="Not in the download client (yet) — or it was removed there">not in client</span>' },
    { key: "progress", label: "Progress", desc: true, sort: g => g.client && g.client.progress, cell: g => progressCell(g.client && g.client.progress) },
    { key: "eta", label: "ETA", cls: "num mono", sort: g => !g.client ? null : g.client.progress >= 1 ? 0 : g.client.eta, cell: g =>
        !g.client ? "—" : g.client.progress >= 1 ? "done" : fmtEta(g.client.eta) },
  ],
});

qTable("q-skipped", {
  cols: [...releaseCols("Why not"), { key: "act", label: "", cls: "actions", cell: () =>
    '<button class="quiet small" data-q="grab">Grab anyway</button>' }],
  hay: releaseHay, sort: { key: "found", dir: -1 },
  empty: "Nothing passed over.",
});

// Buttons in Wanted / Passed over: one listener each, so a 4-second refresh can't drop clicks.
for (const id of ["q-wanted", "q-skipped"]) {
  $(`#${id} tbody`).addEventListener("click", async e => {
    const b = e.target.closest("button[data-q]");
    if (!b) return;
    b.disabled = true;
    const res = await api.post(`/api/releases/${b.closest("tr").dataset.rid}/${b.dataset.q}`);
    if (res.error) { alert(res.error); b.disabled = false; return; }
    refreshQueue(); refreshStatus();
  });
}

let qTyping;
$("#q-search").addEventListener("input", e => {
  clearTimeout(qTyping);
  qTyping = setTimeout(() => { qQuery = lc(e.target.value.trim()); qRenderAll(); }, 120);
});
$("#q-search").addEventListener("keydown", e => {
  if (e.key === "Escape") { e.target.value = ""; qQuery = ""; qRenderAll(); }
});

async function refreshQueue() {
  const r = await api.get("/api/queue");
  if (r.error) return;
  const gates = [];
  if (!r.torrent_folder) gates.push("No .torrent folder is set, so nothing can be grabbed — choose one in Settings → Torrents.");
  else if (!r.client && !r.client_error) gates.push("No download client is set, so grabbed .torrent files will sit in the torrent folder — set one in Settings → Torrents.");
  if (r.dry_run) gates.push("Dry run is on: artists set to grab automatically leave new finds here instead. Grab buttons still work.");
  $("#q-gates").hidden = !gates.length;
  $("#q-gates").innerHTML = gates.map(x => `<p>${esc(x)}</p>`).join("");
  $("#q-client").textContent = r.client ? `· ${r.client}` : r.client_error ? `— ${r.client_error}` : "— no download client set";
  qTables["q-wanted"].rows = r.wanted || [];
  qTables["q-down"].rows = r.downloading || [];
  qTables["q-intake"].rows = r.intake || [];
  qTables["q-grabbed"].rows = r.grabbed || [];
  qTables["q-skipped"].rows = r.skipped || [];
  // don't re-draw under someone mid-click on a button
  if (document.activeElement && document.activeElement.matches("#view-queue tbody button")) return;
  qRenderAll();
}

/* ── Notes ── */
// Saved as you type (a moment after you stop), when you switch notes, and when
// you leave the page. Each save carries the revision this page last saw, so a
// second tab can't silently overwrite the first.
const NOTE_KEY = "reelarr.notes.open";
const nt = { list: [], cur: null, dirty: false, saving: false, again: false, timer: null, retry: null, loaded: false };

function ntStatus(text, bad = false) {
  const s = $("#nt-status");
  s.textContent = text;
  s.classList.toggle("bad", bad);
}
const ntClock = t => new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const ntTitle = n => (n.title || "").trim() || (n.preview || (n.body || "").trim().split("\n")[0] || "").slice(0, 80) || "Untitled";

function ntRenderList() {
  const q = lc($("#nt-search").value.trim());
  const words = q ? q.split(/\s+/) : [];
  const rows = nt.list.filter(n => !words.length || words.every(w => lc(n.title + " " + n.body).includes(w)));
  $("#nt-list").innerHTML = rows.map(n => `<li data-id="${n.id}" class="${nt.cur && nt.cur.id === n.id ? "active" : ""}">
      <button type="button"><span class="nt-t">${n.pinned ? '<span class="nt-pin" title="Pinned">◆</span>' : ""}${esc(ntTitle(n))}</span>
      ${n.title && n.preview ? `<span class="nt-p">${esc(n.preview)}</span>` : ""}
      <span class="nt-d">${fmtTime(n.updated_at)}</span></button></li>`).join("");
  const e = $("#nt-list-empty");
  e.hidden = rows.length > 0;
  e.textContent = nt.list.length ? "No notes match." : "No notes yet.";
}

// Keep the sidebar's copy of the open note in step with what's on screen.
function ntSyncListEntry(n) {
  const i = nt.list.findIndex(x => x.id === n.id);
  const first = (n.body || "").split("\n").map(s => s.trim()).find(Boolean) || "";
  const entry = { ...n, preview: first.slice(0, 140) };
  if (i < 0) nt.list.unshift(entry); else nt.list[i] = entry;
  nt.list.sort((a, b) => (b.pinned - a.pinned) || (b.updated_at - a.updated_at));
  ntRenderList();
}

async function ntLoadList() {
  const r = await api.get("/api/notes");
  if (r.error) return;
  nt.list = r.notes || [];
  ntRenderList();
  if (!nt.loaded) {
    nt.loaded = true;
    let want = null;
    try { want = Number(localStorage.getItem(NOTE_KEY)) || null; } catch { /* private mode */ }
    const pick = nt.list.find(n => n.id === want) || nt.list[0];
    if (pick) ntOpen(pick.id, false);
  }
}

function ntShow(n) {
  nt.cur = { id: n.id, rev: n.rev, title: n.title, body: n.body, pinned: n.pinned, updated_at: n.updated_at };
  nt.dirty = false;
  $("#nt-editor").hidden = false;
  $("#nt-none").hidden = true;
  $("#nt-conflict").hidden = true;
  $("#nt-title").value = n.title;
  $("#nt-body").value = n.body;
  $("#nt-pin").setAttribute("aria-pressed", String(!!n.pinned));
  $("#nt-pin").textContent = n.pinned ? "Pinned" : "Pin";
  ntStatus(`Saved · ${ntClock(n.updated_at)}`);
  try { localStorage.setItem(NOTE_KEY, String(n.id)); } catch { /* private mode */ }
  ntRenderList();
}

async function ntOpen(id, focus = true) {
  if (nt.cur && nt.cur.id === id) return;
  await ntLeave();
  const n = await api.get(`/api/notes/${id}`);
  if (n.error) { ntStatus(n.error, true); ntLoadList(); return; }
  ntShow(n);
  if (focus) $("#nt-body").focus();
}

// Before switching away: save what's unsaved; drop a note that was never written in.
async function ntLeave() {
  if (!nt.cur) return;
  clearTimeout(nt.timer);
  if (nt.dirty) await ntSave();
  const c = nt.cur;
  if (!c.title.trim() && !c.body.trim() && !nt.dirty) {
    await fetch(`/api/notes/${c.id}`, { method: "DELETE" }).catch(() => {});
    nt.list = nt.list.filter(x => x.id !== c.id);
  }
}

function ntChanged() {
  if (!nt.cur) return;
  nt.cur.title = $("#nt-title").value;
  nt.cur.body = $("#nt-body").value;
  nt.dirty = true;
  ntStatus("Editing…");
  clearTimeout(nt.timer);
  nt.timer = setTimeout(ntSave, 700);
}

async function ntSave(extra = {}, keepalive = false) {
  if (!nt.cur) return;
  if (nt.saving) { nt.again = true; return; }
  clearTimeout(nt.timer);
  clearTimeout(nt.retry);
  const c = nt.cur;
  const sent = { title: c.title, body: c.body, rev: c.rev, ...extra };
  nt.saving = true;
  ntStatus("Saving…");
  let r, status = 0;
  try {
    const res = await fetch(`/api/notes/${c.id}`, { method: "PUT", keepalive,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(sent) });
    status = res.status;
    if (status === 401) { location.href = "/login"; return; }
    r = await res.json();
  } catch (e) {
    r = { error: String(e) };
  }
  nt.saving = false;
  if (nt.cur !== c) return;                         // switched notes meanwhile; that save still landed
  if (status === 409 && r.conflict) {
    nt.theirs = r.current;
    $("#nt-conflict").hidden = false;
    ntStatus("Not saved — changed elsewhere", true);
    return;
  }
  if (r.error || status >= 400) {
    ntStatus(status === 404 ? "This note was deleted elsewhere." : "Not saved — retrying…", true);
    if (status !== 404 && status !== 400) nt.retry = setTimeout(() => ntSave(), 5000);
    return;
  }
  c.rev = r.rev; c.updated_at = r.updated_at; c.pinned = r.pinned;
  // typed more while that was in flight? then there's still something to save
  nt.dirty = c.title !== sent.title || c.body !== sent.body;
  ntSyncListEntry({ ...r, title: c.title, body: c.body });
  if (nt.dirty || nt.again) { nt.again = false; ntSave(); }
  else ntStatus(`Saved · ${ntClock(r.updated_at)}`);
}

async function ntNew() {
  await ntLeave();
  const n = await api.post("/api/notes", {});
  if (n.error) { ntStatus(n.error, true); return; }
  ntSyncListEntry(n);
  ntShow(n);
  $("#nt-title").focus();
}

$("#nt-title").addEventListener("input", ntChanged);
$("#nt-body").addEventListener("input", ntChanged);
$("#nt-title").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); $("#nt-body").focus(); } });
$("#nt-new").addEventListener("click", ntNew);
$("#nt-search").addEventListener("input", ntRenderList);
$("#nt-search").addEventListener("keydown", e => { if (e.key === "Escape") { e.target.value = ""; ntRenderList(); } });
$("#nt-list").addEventListener("click", e => {
  const li = e.target.closest("li[data-id]");
  if (li) ntOpen(Number(li.dataset.id));
});
$("#nt-pin").addEventListener("click", () => {
  if (!nt.cur) return;
  const pinned = !nt.cur.pinned;
  $("#nt-pin").setAttribute("aria-pressed", String(pinned));
  $("#nt-pin").textContent = pinned ? "Pinned" : "Pin";
  ntSave({ pinned });
});
$("#nt-delete").addEventListener("click", async () => {
  if (!nt.cur) return;
  const c = nt.cur;
  clearTimeout(nt.timer);
  if (nt.dirty) await ntSave();
  const r = await fetch(`/api/notes/${c.id}`, { method: "DELETE" }).then(jsonOrErr).catch(e => ({ error: String(e) }));
  if (r.error) { ntStatus(r.error, true); return; }
  nt.list = nt.list.filter(x => x.id !== c.id);
  nt.cur = null;
  $("#nt-editor").hidden = true;
  $("#nt-none").hidden = false;
  ntRenderList();
  const u = $("#nt-undo");
  u.hidden = false;
  u.innerHTML = `Deleted “${esc(ntTitle(c))}”. <button class="quiet small" id="nt-undo-btn">Undo</button>`;
  $("#nt-undo-btn").addEventListener("click", async () => {
    const n = await api.post(`/api/notes/${c.id}/restore`);
    u.hidden = true;
    if (!n.error) { ntSyncListEntry(n); ntShow(n); }
  });
  setTimeout(() => { u.hidden = true; }, 15000);
});
$("#nt-keep-mine").addEventListener("click", () => {
  if (!nt.cur || !nt.theirs) return;
  nt.cur.rev = nt.theirs.rev;
  $("#nt-conflict").hidden = true;
  nt.dirty = true;
  ntSave();
});
$("#nt-take-theirs").addEventListener("click", () => {
  if (nt.theirs) ntShow(nt.theirs);
});
document.addEventListener("keydown", e => {
  if (currentView() !== "notes") return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (nt.dirty) ntSave(); }
  if (e.altKey && e.key.toLowerCase() === "n") { e.preventDefault(); ntNew(); }
});
// Leaving the tab or closing it: send what's unsaved straight away.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden" && nt.dirty && !nt.saving) ntSave({}, true);
});
window.addEventListener("pagehide", () => { if (nt.dirty && !nt.saving) ntSave({}, true); });

function refreshNotes() {
  ntLoadList();
}

/* ── System ── */
async function refreshSystem() {
  const r = await api.get("/api/system/health");
  if (r.error) return;
  $("#sys-health").innerHTML = r.checks.map(c => `<div class="h ${esc(c.level)}"><strong>${esc(c.name)}</strong><p>${esc(c.detail)}</p></div>`).join("");
  const sc = r.schema || {};
  $("#sys-about").innerHTML = `
    <div class="kv"><span>Version</span><strong>${esc(r.version)}</strong></div>
    <div class="kv"><span>Database schema</span><strong>reelarr.db v${esc(sc["reelarr.db"])} · library_index.db v${esc(sc["library_index.db"])} · settings v${esc(sc["settings.json"] ?? "—")}</strong></div>
    <div class="kv"><span>Config folder</span><strong>${esc(r.config_dir)}</strong></div>
    <div class="kv"><span>Add-ons</span><strong>${r.providers.length ? esc(r.providers.join(", ")) : "none"}</strong></div>
    <div class="kv"><span>What's new</span><strong>CHANGELOG.md, beside the app</strong></div>`;
  $("#sys-backups").innerHTML = (r.backups || []).length ? r.backups.map(b => `<div class="entry">
      <span class="when">${fmtTime(b.mtime)}</span><span class="what"><a href="/api/system/backup/${encodeURIComponent(b.name)}">${esc(b.name)}</a>
      <span class="dim">— ${(b.size / 1048576).toFixed(1)} MB</span></span></div>`).join("")
    : '<p class="empty">No backups yet.</p>';
}
$("#sys-backup").addEventListener("click", async () => {
  $("#sys-backup-msg").textContent = "making a backup…";
  const r = await api.post("/api/system/backup");
  $("#sys-backup-msg").textContent = r.error ? "✗ " + r.error : `✓ ${r.name}`;
  refreshSystem();
});

/* ── Rail extras: wanted badge, spinning reel while working ── */
statusHooks.push(s => {
  const n = s.wanted || 0, b = $("#wanted-count");
  b.hidden = !n; b.textContent = n;
  $("#mark").classList.toggle("spinning", !!(s.scan_running || s.monitor_running));
});
