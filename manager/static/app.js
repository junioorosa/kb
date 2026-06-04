/* KB Manager front-end. Talks to the localhost server; the per-launch token is
   read from this page's own URL and sent on every API call. */
"use strict";

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "X-KB-Token": TOKEN, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const msg = data && (data.errors ? data.errors.join("; ") : data.error) || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

function toast(msg, kind = "ok") {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  setTimeout(() => { t.className = "toast"; }, 2600);
}

function setMsg(id, text, kind) {
  const el = $(id);
  el.textContent = text || "";
  el.className = "inline-msg" + (kind ? " " + kind : "");
}

function pill(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  el.className = "pill " + cls;
}

function dot(kind) {
  return `<span class="dot dot-${kind}"></span>`;
}

// --- Status ---------------------------------------------------------------
async function loadStatus() {
  let st;
  try { st = await api("GET", "/api/status"); }
  catch (e) { toast("status: " + e.message, "err"); return; }

  // version
  const ver = st.installed_version || "not stamped";
  $("st-version").textContent = ver + (st.repo_version && st.repo_version !== st.installed_version ? `  (repo ${st.repo_version})` : "");
  pill("pill-version", "v" + (st.installed_version || st.repo_version || "—"), "pill-muted");

  // vault
  const cfg = st.config || {};
  if (cfg.present && cfg.vault) {
    $("st-vault").innerHTML = (cfg.vault_exists ? dot("ok") : dot("bad")) + cfg.vault +
      (cfg.vault_exists ? "" : "  (missing!)");
  } else {
    $("st-vault").innerHTML = dot("warn") + "not configured";
  }

  // daemon
  const d = st.daemon || {};
  let dk = "bad", dtxt = "down" + (d.reason ? ` (${d.reason})` : "");
  if (d.up && d.model_loaded) { dk = "ok"; dtxt = "up · model ready"; }
  else if (d.up) { dk = "warn"; dtxt = "up · loading model"; }
  $("st-daemon").innerHTML = dot(dk) + dtxt;
  pill("pill-daemon", "Embedding " + (dk === "ok" ? "✓" : dk === "warn" ? "~" : "✗"),
       dk === "ok" ? "pill-ok" : dk === "warn" ? "pill-warn" : "pill-bad");

  // scheduler
  const sc = st.scheduler || {};
  const exists = sc.exists;
  $("st-sched").innerHTML = (exists ? dot("ok") : dot("warn")) + (exists ? "registered" : "not registered") +
    (sc.os ? `  ·  ${sc.os}` : "");
  pill("pill-sched", "sync " + (exists ? "✓" : "✗"), exists ? "pill-ok" : "pill-warn");
}

// --- Config (vault + workspaces) ------------------------------------------
let WORKSPACES = [];

async function loadConfig() {
  let r;
  try { r = await api("GET", "/api/config"); }
  catch (e) { toast("config: " + e.message, "err"); return; }
  const cfg = r.config || {};
  $("vault-input").value = cfg.vault || "";
  WORKSPACES = Array.isArray(cfg.workspaces) ? cfg.workspaces.map((w) => ({ name: w.name || "", path: w.path || "" })) : [];
  renderWorkspaces();
}

function renderWorkspaces() {
  const list = $("ws-list");
  list.innerHTML = "";
  if (WORKSPACES.length === 0) {
    list.innerHTML = '<div class="ws-empty">No workspaces yet.</div>';
    return;
  }
  WORKSPACES.forEach((w, i) => {
    const row = document.createElement("div");
    row.className = "ws-row";
    row.innerHTML =
      `<input type="text" placeholder="name" value="${escapeAttr(w.name)}" data-i="${i}" data-k="name" spellcheck="false" />` +
      `<input type="text" placeholder="C:/path/to/repos" value="${escapeAttr(w.path)}" data-i="${i}" data-k="path" spellcheck="false" />` +
      `<button class="ws-del" data-del="${i}" title="remove">×</button>`;
    list.appendChild(row);
  });
  list.querySelectorAll("input").forEach((inp) => {
    inp.addEventListener("input", (e) => {
      const t = e.target;
      WORKSPACES[+t.dataset.i][t.dataset.k] = t.value;
    });
  });
  list.querySelectorAll(".ws-del").forEach((b) => {
    b.addEventListener("click", () => { WORKSPACES.splice(+b.dataset.del, 1); renderWorkspaces(); });
  });
}

function escapeAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

async function saveVault() {
  const vault = $("vault-input").value.trim();
  if (!vault) { setMsg("vault-msg", "enter a path", "err"); return; }
  try {
    await api("PUT", "/api/config", { updates: { vault } });
    setMsg("vault-msg", "saved", "ok");
    loadStatus();
  } catch (e) { setMsg("vault-msg", e.message, "err"); }
}

async function saveWorkspaces() {
  const ws = WORKSPACES
    .map((w) => ({ name: (w.name || "").trim(), path: (w.path || "").trim() }))
    .filter((w) => w.name || w.path);
  try {
    await api("PUT", "/api/config", { updates: { workspaces: ws } });
    setMsg("ws-msg", "saved", "ok");
  } catch (e) { setMsg("ws-msg", e.message, "err"); }
}

// --- Schedule -------------------------------------------------------------
async function saveSchedule() {
  const time = $("sched-input").value;
  try {
    const r = await api("POST", "/api/schedule", { time });
    setMsg("sched-msg", r.registered ? "registered" : (r.error || "saved (dry)"), r.registered ? "ok" : "err");
    loadStatus();
  } catch (e) { setMsg("sched-msg", e.message, "err"); }
}

// --- Integration ----------------------------------------------------------
async function toggleIntegration() {
  const enable = $("integ-toggle").checked;
  try {
    const r = await api("POST", "/api/integration", { enable });
    $("integ-label").textContent = enable ? "On" : "Off (muted)";
    setMsg("integ-msg", enable ? "hooks wired" : "muted via kill-switch", "ok");
    toast(enable ? "Integration on" : "Integration muted", "ok");
  } catch (e) {
    $("integ-toggle").checked = !enable;
    setMsg("integ-msg", e.message, "err");
  }
}

async function loadIntegration() {
  // Infer current state from the kill-switch presence via status (best effort):
  // if the daemon/config exist we still can't see the file directly, so default
  // to "on" unless a later /api/status surfaces it. Kept simple for v0.
  $("integ-toggle").checked = true;
  $("integ-label").textContent = "On";
}

// --- Knowledge view -------------------------------------------------------
let KN_LOADED = false;
const KN = { project: "", scope: "", tag: "", q: "" };

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function switchView(view) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.view === view));
  $("view-config").classList.toggle("hidden", view !== "config");
  $("view-knowledge").classList.toggle("hidden", view !== "knowledge");
  if (view === "knowledge" && !KN_LOADED) { KN_LOADED = true; loadKnowledge(); }
}

async function loadKnowledge() {
  loadSyncHistory();
  let ov;
  try { ov = await api("GET", "/api/knowledge/overview"); }
  catch (e) { toast("knowledge: " + e.message, "err"); return; }
  renderOverview(ov);
  loadLearnings();
}

function renderOverview(ov) {
  $("ov-learnings").textContent = ov.totals.learnings;
  $("ov-tickets").textContent = ov.totals.tickets;

  const st = ov.tickets_by_status || {};
  const order = ["resolved", "in-progress", "open", "experimental", "discarded"];
  const keys = Object.keys(st).sort((a, b) => (order.indexOf(a) + 1 || 9) - (order.indexOf(b) + 1 || 9));
  $("ov-status").innerHTML = `<div class="stat-lab">tickets by status</div><div class="status-chips">` +
    keys.map((k) => `<span class="schip schip-${k.replace(/[^a-z]/g, "")}">${st[k]} ${esc(k)}</span>`).join("") + `</div>`;

  const g = ov.growth || [];
  const max = Math.max(1, ...g.map((x) => x.learnings));
  $("ov-growth").innerHTML = g.map((x) =>
    `<div class="grow-row"><span class="grow-m">${esc(x.month)}</span>` +
    `<span class="grow-bar"><i style="width:${Math.round((x.learnings / max) * 100)}%"></i></span>` +
    `<span class="grow-n">${x.learnings}</span></div>`).join("") || `<div class="muted">no dated learnings</div>`;

  const tags = ov.top_tags || [];
  $("ov-tags").innerHTML = tags.map(([t, n]) =>
    `<button class="chip" data-tag="${esc(t)}">${esc(t)}<span class="chip-n">${n}</span></button>`).join("");
  $("ov-tags").querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => {
    KN.tag = (KN.tag === c.dataset.tag) ? "" : c.dataset.tag;
    $("ov-tags").querySelectorAll(".chip").forEach((x) => x.classList.toggle("is-active", x.dataset.tag === KN.tag));
    loadLearnings();
  }));

  const sel = $("kn-project");
  sel.innerHTML = `<option value="">All projects</option>` +
    (ov.by_project || []).map((p) => `<option value="${esc(p.project)}">${esc(p.project)} (${p.learnings})</option>`).join("");
}

async function loadSyncHistory() {
  let runs = [];
  try { runs = (await api("GET", "/api/sync-history")).runs || []; } catch (_) {}
  const dot = $("sync-dot"), txt = $("sync-text"), det = $("sync-detail");
  if (!runs.length) {
    dot.className = "sync-dot warn";
    txt.textContent = "No sync recorded yet — the first scheduled run will populate this.";
    det.textContent = ""; return;
  }
  const r = runs[0];
  dot.className = "sync-dot " + (r.errors ? "bad" : "ok");
  const when = (r.ts || "").replace("T", " ").slice(0, 16);
  txt.innerHTML = `Last sync <b>${esc(when)}</b> · ${r.captures} captured` +
    (r.backfills ? ` (${r.backfills} backfill)` : "") + ` · ${r.finalizes} resolved` +
    (r.errors ? ` · <span class="bad-txt">${r.errors} errors</span>` : "");
  const learned = (r.learned_files || []).length;
  det.textContent = learned ? `${learned} file${learned > 1 ? "s" : ""} learned this run` :
    `${runs.length} run${runs.length > 1 ? "s" : ""} on record`;
}

async function loadLearnings() {
  const p = new URLSearchParams();
  if (KN.project) p.set("project", KN.project);
  if (KN.scope) p.set("scope", KN.scope);
  if (KN.tag) p.set("tag", KN.tag);
  if (KN.q) p.set("q", KN.q);
  let items = [];
  try { items = (await api("GET", "/api/knowledge/learnings?" + p.toString())).learnings || []; }
  catch (e) { toast("learnings: " + e.message, "err"); return; }
  renderList(items);
}

function renderList(items) {
  $("kn-count").textContent = `${items.length} learning${items.length === 1 ? "" : "s"}` +
    (KN.tag ? ` · #${KN.tag}` : "");
  const list = $("kn-list");
  if (!items.length) { list.innerHTML = `<div class="muted pad">No matches.</div>`; return; }
  list.innerHTML = items.map((r) =>
    `<button class="kn-item" data-path="${esc(r.rel_path)}">` +
    `<div class="kn-item-top"><span class="kn-item-name">${esc(r.description || r.name)}</span>` +
    `<span class="kn-item-date">${esc(r.date || "")}</span></div>` +
    `<div class="kn-item-meta"><span class="scope-tag scope-${esc(r.scope)}">${esc(r.scope)}</span>` +
    `<span class="kn-item-proj">${esc(r.project || r.workspace)}</span>` +
    (r.tags || []).slice(0, 4).map((t) => `<span class="mini-tag">${esc(t)}</span>`).join("") +
    `</div></button>`).join("");
  list.querySelectorAll(".kn-item").forEach((b) => b.addEventListener("click", () => {
    list.querySelectorAll(".kn-item").forEach((x) => x.classList.remove("is-active"));
    b.classList.add("is-active");
    openItem(b.dataset.path);
  }));
}

async function openItem(rel) {
  const reader = $("kn-reader");
  reader.innerHTML = `<div class="kn-reader-empty">Loading…</div>`;
  let it;
  try { it = await api("GET", "/api/knowledge/item?path=" + encodeURIComponent(rel)); }
  catch (e) { reader.innerHTML = `<div class="kn-reader-empty">Error: ${esc(e.message)}</div>`; return; }
  const fm = it.frontmatter || {};
  const tags = Array.isArray(fm.tags) ? fm.tags : [];
  reader.innerHTML =
    `<div class="kn-read-head">` +
    `<div class="kn-read-title">${esc(it.title)}</div>` +
    `<div class="kn-read-sub"><span class="scope-tag scope-${esc(it.scope)}">${esc(it.scope)}</span>` +
    `<span>${esc(it.project || "")}</span>` +
    (fm.ticket_origin ? `<span class="muted">ticket ${esc(fm.ticket_origin)}</span>` : "") + `</div>` +
    (tags.length ? `<div class="kn-read-tags">` + tags.map((t) => `<span class="mini-tag">${esc(t)}</span>`).join("") + `</div>` : "") +
    `<div class="kn-read-path">${esc(it.rel_path)}</div></div>` +
    `<article class="prose">${it.html}</article>`;  // html is server-rendered + escaped
  reader.scrollTop = 0;
}

let _searchTimer = null;

// --- Wire up --------------------------------------------------------------
function init() {
  if (!TOKEN) { toast("missing token — relaunch from the server URL", "err"); }
  $("save-vault").addEventListener("click", saveVault);
  $("add-ws").addEventListener("click", () => { WORKSPACES.push({ name: "", path: "" }); renderWorkspaces(); });
  $("save-ws").addEventListener("click", saveWorkspaces);
  $("save-sched").addEventListener("click", saveSchedule);
  $("integ-toggle").addEventListener("change", toggleIntegration);

  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));
  $("kn-project").addEventListener("change", (e) => { KN.project = e.target.value; loadLearnings(); });
  $("kn-scope").addEventListener("change", (e) => { KN.scope = e.target.value; loadLearnings(); });
  $("kn-search").addEventListener("input", (e) => {
    KN.q = e.target.value.trim();
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(loadLearnings, 220);
  });

  loadStatus();
  loadConfig();
  loadIntegration();
  switchView("knowledge");  // knowledge is the default landing view
}

document.addEventListener("DOMContentLoaded", init);
