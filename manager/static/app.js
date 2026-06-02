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

// --- Wire up --------------------------------------------------------------
function init() {
  if (!TOKEN) { toast("missing token — relaunch from the server URL", "err"); }
  $("save-vault").addEventListener("click", saveVault);
  $("add-ws").addEventListener("click", () => { WORKSPACES.push({ name: "", path: "" }); renderWorkspaces(); });
  $("save-ws").addEventListener("click", saveWorkspaces);
  $("save-sched").addEventListener("click", saveSchedule);
  $("integ-toggle").addEventListener("change", toggleIntegration);
  loadStatus();
  loadConfig();
  loadIntegration();
}

document.addEventListener("DOMContentLoaded", init);
