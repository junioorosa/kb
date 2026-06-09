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

// --- Updates (remote-aware, deliberate) -----------------------------------
// Detect a newer KB on the remote (read-only VERSION compare) and offer a
// one-click pull + re-deploy. The apply is fast-forward-only + reversible
// (server side); here we only render state and trigger it on an explicit click.
function renderUpdate(u) {
  const el = $("st-update"), pill = $("pill-update"), applyBtn = $("apply-update");
  el.title = (u && u.reason) ? u.reason : "";
  if (!u || (!u.checked && u.reason)) {
    el.innerHTML = dot("warn") + "couldn't check";   // offline / no remote VERSION
    pill.classList.add("hidden");
    applyBtn.classList.add("hidden");
    return;
  }
  if (u.update_available) {
    el.innerHTML = dot("warn") + `update available: v${esc(u.local_version)} → v${esc(u.remote_version)}`;
    pill.textContent = `update v${esc(u.remote_version)}`;
    pill.classList.remove("hidden");
    applyBtn.classList.remove("hidden");
  } else {
    el.innerHTML = dot("ok") + `up to date (v${esc(u.local_version)})`;
    pill.classList.add("hidden");
    applyBtn.classList.add("hidden");
  }
}

async function checkUpdate(opts = {}) {
  if (opts.manual) setMsg("update-msg", "checking…", "");
  let u;
  try { u = await api("GET", "/api/update-check"); }
  catch (e) { renderUpdate({ reason: e.message, checked: false }); if (opts.manual) setMsg("update-msg", e.message, "err"); return; }
  renderUpdate(u);
  if (opts.manual) {
    const txt = u.checked ? (u.update_available ? "update available" : "up to date") : (u.reason || "couldn't check");
    setMsg("update-msg", txt, u.checked ? "ok" : "err");
  }
}

async function applyUpdate() {
  if (!confirm("Pull the latest version and re-deploy?\n\n" +
    "Fast-forwards the source tree and re-installs the hooks/engine. Reversible — " +
    "every overwritten file is backed up. Refuses if you have uncommitted or diverging local changes.")) return;
  const btn = $("apply-update");
  btn.disabled = true;
  setMsg("update-msg", "updating…", "");
  let r;
  try { r = await api("POST", "/api/update"); }
  catch (e) { setMsg("update-msg", e.message, "err"); btn.disabled = false; return; }
  btn.disabled = false;
  if (!r.updated) { setMsg("update-msg", r.reason || "update did not apply", "err"); return; }
  setMsg("update-msg", `updated v${esc(r.from)} → v${esc(r.to)}. ${esc(r.note || "")}`, "ok");
  toast(`Updated to v${r.to} — restart the manager to load UI changes`, "ok");
  loadStatus();
  checkUpdate();
}

// --- Vault remote (one-time connect; never auto-pushes) -------------------
// The vault is local-only by default. This lets the user deliberately connect it
// to a private remote they own (backup, or a shared team KB). The nightly sync
// never pushes; this is the explicit "make it a team repo" gesture.
function renderVaultRemote(v) {
  const state = $("vr-state"), form = $("vr-form"), btn = $("vr-connect"), pullRow = $("vr-pull-row");
  const hide = (el, h) => el.classList.toggle("hidden", h);
  if (!v || !v.is_git) {
    state.innerHTML = dot("warn") + esc((v && v.reason) || "vault is not a git repo");
    hide(form, true); hide(btn, true); hide(pullRow, true);
    return;
  }
  if (v.has_remote) {
    // Connected: hide the connect form, show the team-read "Pull" control.
    state.innerHTML = dot("ok") + `connected → ${esc(v.url || v.remote)}`;
    hide(form, true); hide(btn, true); hide(pullRow, false);
  } else {
    state.innerHTML = dot("warn") + "local only — not connected to any remote";
    hide(form, false); hide(btn, false); hide(pullRow, true);
  }
}

async function loadVaultRemote() {
  let v;
  try { v = await api("GET", "/api/vault-remote"); }
  catch (e) { $("vr-state").innerHTML = dot("warn") + "couldn't read remote state"; return; }
  renderVaultRemote(v);
}

async function connectVaultRemote() {
  const url = $("vr-input").value.trim();
  if (!url) { setMsg("vr-msg", "enter a remote URL", "err"); return; }
  if (!confirm("Connect the vault to:\n\n" + url + "\n\n" +
    "This publishes your vault's contents to that remote. Make sure it's a PRIVATE repo you own. Continue?")) return;
  const btn = $("vr-connect");
  btn.disabled = true;
  setMsg("vr-msg", "connecting…", "");
  let r;
  try { r = await api("POST", "/api/vault-remote/connect", { url }); }
  catch (e) { setMsg("vr-msg", e.message, "err"); btn.disabled = false; return; }
  btn.disabled = false;
  if (r.pushed) {
    setMsg("vr-msg", "connected + pushed", "ok");
    toast("Vault published to remote", "ok");
    loadVaultRemote();
  } else if (r.connected) {
    // Remote added but the push failed (auth not set up / guard hook). The remote
    // is set — surface the reason so the user can finish the push by hand.
    setMsg("vr-msg", r.reason || "remote added; push pending", "err");
    loadVaultRemote();
  } else {
    setMsg("vr-msg", r.reason || "could not connect", "err");
  }
}

async function pullVaultRemote() {
  const btn = $("vr-pull");
  btn.disabled = true;
  setMsg("vr-pull-msg", "pulling…", "");
  let r;
  try { r = await api("POST", "/api/vault-remote/pull"); }
  catch (e) { setMsg("vr-pull-msg", e.message, "err"); btn.disabled = false; return; }
  btn.disabled = false;
  if (r.pulled) {
    const txt = r.already_up_to_date ? "already up to date"
      : `pulled ${r.merged_commits} commit${r.merged_commits === 1 ? "" : "s"}`;
    setMsg("vr-pull-msg", txt, "ok");
    if (!r.already_up_to_date) toast("Pulled team updates — reindex on next sync", "ok");
  } else {
    setMsg("vr-pull-msg", r.reason || "pull failed", "err");
  }
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
  const repos = r.repos ? ` · ${r.repos.fetched}/${r.repos.discovered} repos` : "";
  txt.innerHTML = `Last sync <b>${esc(when)}</b>${repos} · ${r.captures} captured` +
    (r.backfills ? ` (${r.backfills} backfill)` : "") + ` · ${r.finalizes} resolved` +
    (r.errors ? ` · <span class="bad-txt">${r.errors} error${r.errors > 1 ? "s" : ""}</span>` : "");
  const L = r.learned || {};
  const nl = (L.learnings || []).length, nt = (L.tickets || []).length;
  const base = (nl || nt)
    ? `${nl} learning${nl !== 1 ? "s" : ""}, ${nt} ticket${nt !== 1 ? "s" : ""} this run`
    : `${runs.length} run${runs.length > 1 ? "s" : ""} on record`;
  const dups = r.duplicates || [];
  if (dups.length) {
    if (dot.className.indexOf("bad") === -1) dot.className = "sync-dot warn";
    const twins = dups.filter((d) => d.kind === "twin").length;
    const top = dups.slice(0, 6).map((d) =>
      `${d.kind === "twin" ? "twin " : ""}${d.score} · ${shortPair(d.a)} ⇄ ${shortPair(d.b)}`).join("\n");
    const label = twins
      ? `⚠ ${twins} same-run twin${twins > 1 ? "s" : ""}` +
        (dups.length > twins ? ` (+${dups.length - twins} to review)` : "")
      : `⚠ ${dups.length} possible duplicate${dups.length > 1 ? "s" : ""}`;
    det.innerHTML = `${esc(base)} · <span class="warn-txt" title="${esc(top)}">${esc(label)}</span>`;
  } else {
    det.textContent = base;
  }
}

function shortPair(rel) {
  // ".../<folder>/Learnings/<name>.md" -> "<folder>/<name>"
  const parts = (rel || "").split("/");
  const name = (parts.pop() || "").replace(/\.md$/, "");
  const i = parts.lastIndexOf("Learnings");
  const folder = i > 0 ? parts[i - 1] : (parts.pop() || "");
  return folder ? `${folder}/${name}` : name;
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
  $("check-update").addEventListener("click", () => checkUpdate({ manual: true }));
  $("apply-update").addEventListener("click", applyUpdate);
  $("vr-connect").addEventListener("click", connectVaultRemote);
  $("vr-pull").addEventListener("click", pullVaultRemote);

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
  loadVaultRemote();         // is the vault connected to a remote? (read-only)
  checkUpdate();             // fail-soft remote probe; never blocks the page render
  switchView("knowledge");  // knowledge is the default landing view
}

document.addEventListener("DOMContentLoaded", init);
