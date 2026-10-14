"""The dashboard page: one inline HTML document, thirteen pages, no remote assets.

Same rules as the trading dashboard this project sits next to: a single
self-contained file, no CDN, no third-party script, and every number on screen
comes from the API - which reads the database.  Where data does not exist the
page says *not available* or *never published* rather than showing a zero that
looks like a measurement; that distinction is the whole point of the Analytics,
Published Posts and Social Accounts pages.

The JavaScript keeps three things visible at all times, because they are the
three ways an autonomous system goes wrong quietly: the **autonomy mode**, the
**kill switch**, and whether what is on screen is **real, simulated, or
unknown**.

Three decisions in here exist for the operator who uses the panel every day
rather than for the demo that shows it once:

* **the automatic poll never destroys work in progress.**  The 30-second refresh
  redraws ``#content``; while a draft is dirty, or a textarea has focus, it
  updates the chrome and leaves the page alone (``editingNow``), and the top bar
  says *modifiche non salvate* instead of silently throwing them away;
* **a filter is state, not markup.**  The approval queue keeps its filters and
  its multi-selection in ``STATE``, so a redraw does not lose the selection the
  operator just built, and the controls are re-synced from that state rather than
  trusted to still say what the table is doing;
* **the risky action asks first.**  ``Pubblica (live)`` and the global pause go
  through a confirmation that names what is about to happen, because a one-click
  publish is the fastest way to post something nobody meant to post.

The queue also approves or rejects a whole filtered selection in one gesture,
but by looping the single-decision endpoint: each decision keeps its own audit
row and its own note, so a batch is still reviewable one approval at a time.
"""

from __future__ import annotations

import json

PLACEHOLDER = "__DASHBOARD_BOOTSTRAP__"

STYLE = """
:root{
  color-scheme: dark;
  --bg:#090d14; --bg-1:#0c121c; --bg-2:#0f1724; --bg-3:#16202f;
  --panel:#0f1724; --panel-2:#0b111b; --raise:#141e2d;
  --bd:#1b2637; --bd-2:#27364b; --bd-3:#3a4d68;
  --tx:#e2e9f3; --tx-2:#9aa8bd; --tx-3:#6b7a91;
  --brand:#3b82f6; --brand-2:#60a5fa; --ai:#22d3ee; --ai-2:#a78bfa;
  --pos:#34d399; --neg:#f87171; --warn:#fbbf24; --info:#38bdf8;
  --pos-bg:rgba(52,211,153,.13); --neg-bg:rgba(248,113,113,.13);
  --warn-bg:rgba(251,191,36,.14); --ai-bg:rgba(34,211,238,.11); --brand-bg:rgba(59,130,246,.13);
  --r:10px; --r-sm:7px; --sh:0 1px 0 rgba(255,255,255,.02),0 10px 30px -18px rgba(0,0,0,.9);
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --nav-w:236px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font-family:var(--sans);font-size:13.5px;line-height:1.5}
a{color:var(--brand-2);text-decoration:none}
.mono,.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.muted{color:var(--tx-2)} .dim{color:var(--tx-3)}
.pos{color:var(--pos)} .neg{color:var(--neg)} .warn{color:var(--warn)} .ai{color:var(--ai)}
.hidden{display:none !important}
.app{display:grid;grid-template-columns:var(--nav-w) 1fr;min-height:100vh}
.sidebar{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;background:var(--panel-2);
  border-right:1px solid var(--bd);overflow:hidden}
.brand{display:flex;align-items:center;gap:10px;padding:16px 16px 12px;font-weight:650}
.brand .mark{width:26px;height:26px;border-radius:8px;display:grid;place-items:center;
  background:linear-gradient(150deg,var(--brand),var(--ai-2));color:#f8fbff;font-weight:800;font-size:13px}
.brand small{display:block;font-size:10px;color:var(--tx-3);letter-spacing:.08em;text-transform:uppercase}
.nav{display:flex;flex-direction:column;gap:2px;padding:6px 8px;overflow-y:auto;flex:1}
.nav-item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:9px;border:1px solid transparent;
  background:none;color:var(--tx-2);font:inherit;font-size:13px;cursor:pointer;text-align:left;width:100%}
.nav-item:hover{background:var(--bg-3);color:var(--tx)}
.nav-item.active{background:var(--brand-bg);color:var(--tx);border-color:var(--bd-2);font-weight:600}
.nav-item .ico{width:18px;text-align:center;opacity:.9}
.nav-item .pill{margin-left:auto;font-size:10.5px;padding:1px 7px;border-radius:99px;background:var(--warn-bg);color:var(--warn)}
.nav-foot{padding:10px 12px;border-top:1px solid var(--bd);font-size:11.5px;color:var(--tx-3)}
.main{display:flex;flex-direction:column;min-width:0}
.topbar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:12px;padding:12px 20px;
  background:linear-gradient(180deg,var(--bg-1),var(--bg-1) 70%,rgba(12,18,28,.85));border-bottom:1px solid var(--bd)}
.topbar h1{margin:0;font-size:16px;font-weight:640}
.topbar .sub{font-size:11.5px;color:var(--tx-3)}
.spacer{flex:1}
.content{padding:18px 20px 60px;max-width:1500px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.grid.wide{grid-template-columns:repeat(auto-fit,minmax(420px,1fr))}
.card{background:var(--panel);border:1px solid var(--bd);border-radius:var(--r);padding:14px 16px;box-shadow:var(--sh)}
.card h2{margin:0 0 10px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--tx-2);font-weight:600}
.card .big{font-size:22px;font-weight:650;font-family:var(--mono)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:12.5px}
.kv dt{color:var(--tx-3)} .kv dd{margin:0;text-align:right;font-family:var(--mono)}
.badge{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border-radius:99px;font-size:11.5px;
  border:1px solid var(--bd-2);background:var(--bg-3);color:var(--tx-2)}
.badge.ok{background:var(--pos-bg);color:var(--pos);border-color:transparent}
.badge.bad{background:var(--neg-bg);color:var(--neg);border-color:transparent}
.badge.warn{background:var(--warn-bg);color:var(--warn);border-color:transparent}
.badge.ai{background:var(--ai-bg);color:var(--ai);border-color:transparent}
button,.btn{font:inherit;padding:6px 12px;border-radius:var(--r-sm);border:1px solid var(--bd-2);
  background:var(--raise);color:var(--tx);cursor:pointer}
button:hover{background:var(--bg-3)}
button.primary{background:var(--brand-bg);border-color:var(--brand);color:var(--brand-2);font-weight:600}
button.danger{background:var(--neg-bg);border-color:transparent;color:var(--neg)}
button.ghost{background:none}
button:disabled{opacity:.45;cursor:not-allowed}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--tx-3);font-weight:600;text-transform:uppercase;font-size:10.5px;
  letter-spacing:.05em;padding:6px 8px;border-bottom:1px solid var(--bd)}
td{padding:7px 8px;border-bottom:1px solid var(--bd);vertical-align:top}
tr:hover td{background:var(--bg-2)}
pre{background:var(--panel-2);border:1px solid var(--bd);border-radius:var(--r-sm);padding:10px;
  overflow:auto;font-family:var(--mono);font-size:12px;white-space:pre-wrap;margin:0}
textarea,input,select{font:inherit;background:var(--panel-2);color:var(--tx);border:1px solid var(--bd-2);
  border-radius:var(--r-sm);padding:8px}
textarea{width:100%;min-height:170px;font-family:var(--mono);font-size:12.5px}
.empty{padding:18px;text-align:center;color:var(--tx-3);border:1px dashed var(--bd-2);border-radius:var(--r)}
.split{display:grid;gap:12px;grid-template-columns:1fr 1fr}
.cal{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.cal .day{background:var(--panel);border:1px solid var(--bd);border-radius:var(--r);padding:10px}
.cal .day h3{margin:0 0 6px;font-size:12px;color:var(--tx-2)}
.slot{border-left:2px solid var(--ai);padding:5px 7px;margin-bottom:6px;background:var(--bg-2);border-radius:4px;font-size:12px}
.slot.published{border-color:var(--pos)} .slot.failed{border-color:var(--neg)} .slot.leased{border-color:var(--warn)}
.slot .t{font-family:var(--mono);color:var(--tx-3);font-size:11px}
.login{max-width:340px;margin:12vh auto;background:var(--panel);border:1px solid var(--bd);
  border-radius:14px;padding:22px;box-shadow:var(--sh)}
.login h1{font-size:16px;margin:0 0 4px} .login p{color:var(--tx-3);font-size:12px;margin:0 0 14px}
.login input{width:100%;margin-bottom:10px}
.toast{position:fixed;right:18px;bottom:18px;max-width:420px;background:var(--raise);border:1px solid var(--bd-2);
  border-radius:var(--r);padding:12px 14px;box-shadow:var(--sh);z-index:20}
.toast.bad{border-color:var(--neg)} .toast.ok{border-color:var(--pos)}
.bar{height:8px;border-radius:99px;background:var(--bg-3);overflow:hidden}
.bar span{display:block;height:100%;background:linear-gradient(90deg,var(--brand),var(--ai))}
.chip{display:inline-block;padding:2px 8px;border-radius:99px;background:var(--bg-3);font-size:11.5px;
  border:1px solid var(--bd);margin:0 4px 4px 0}
.facts{margin:0;padding-left:16px;color:var(--tx-2);font-size:12px}
.thumb{width:100%;border-radius:var(--r-sm);border:1px solid var(--bd);background:var(--panel-2)}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}
.toolbar input[type=search],.toolbar input[type=text]{min-width:220px}
.toolbar select{padding:6px}
label.row{gap:6px}
.cb{width:15px;height:15px;accent-color:var(--brand);cursor:pointer}
tr.rowsel td{background:var(--brand-bg)}
.kbd{font-family:var(--mono);font-size:11px;border:1px solid var(--bd-2);border-bottom-width:2px;
  border-radius:5px;padding:1px 5px;background:var(--bg-3);color:var(--tx-2);white-space:nowrap}
.freshness{font-size:11.5px;color:var(--tx-3);white-space:nowrap}
.freshness.stale{color:var(--warn)}
.attn{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border-radius:99px;font-size:11.5px;
  border:1px solid transparent;background:var(--warn-bg);color:var(--warn);cursor:pointer}
.attn:hover{filter:brightness(1.15)}
.attn.zero{background:var(--bg-3);color:var(--tx-3);cursor:default}
.overlay{position:fixed;inset:0;background:rgba(4,7,12,.62);z-index:30;display:flex;
  align-items:flex-start;justify-content:center;padding-top:12vh}
.dialog{width:min(620px,92vw);background:var(--panel);border:1px solid var(--bd-2);border-radius:14px;
  box-shadow:var(--sh);overflow:hidden}
.dialog header{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--bd)}
.dialog header h2{margin:0;font-size:13px;color:var(--tx);text-transform:none;letter-spacing:0}
.dialog .body{padding:14px}
.dialog footer{display:flex;gap:8px;justify-content:flex-end;padding:12px 14px;border-top:1px solid var(--bd)}
.dialog input[type=search],.dialog input[type=text]{width:100%}
.cmd-list{max-height:44vh;overflow:auto;margin-top:10px}
.cmd{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;cursor:pointer}
.cmd.active,.cmd:hover{background:var(--brand-bg)}
.cmd .ico{width:18px;text-align:center;color:var(--tx-3)}
.cmd .hint{margin-left:auto;color:var(--tx-3);font-size:11px}
.gnum{width:24px;height:24px;border-radius:7px;display:grid;place-items:center;flex:none;
  background:var(--brand-bg);color:var(--brand-2);font-family:var(--mono);font-size:12px;font-weight:700}
.grid .gstep h2{text-transform:none;letter-spacing:0;font-size:13.5px;color:var(--tx)}
.ghead{display:flex;flex-direction:column;gap:8px}
.gtitle{display:flex;align-items:center;gap:8px;min-width:0}
.gtitle h2{margin:0;min-width:0;overflow-wrap:anywhere}
.gacts{display:flex;align-items:center;gap:8px;margin-left:auto}
"""

SCRIPT = r"""
const $ = (sel, root=document) => root.querySelector(sel);
const $$ = (sel, root=document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const BOOT = window.__BOOT__ || {};
let CSRF = BOOT.csrf || "";
let STATE = { page: "overview", status: null };

function toast(msg, kind="ok"){
  const el = $("#toast");
  el.className = "toast " + kind;
  el.innerHTML = `<div class="row"><strong>${kind === "bad" ? "Errore" : "Fatto"}</strong><span class="spacer"></span>
    <button class="ghost" onclick="document.getElementById('toast').classList.add('hidden')">✕</button></div>
    <div class="muted">${esc(msg)}</div>`;
  el.classList.remove("hidden");
  if (kind !== "bad") setTimeout(() => el.classList.add("hidden"), 4200);
}

async function api(path, options){
  const opts = Object.assign({headers:{}}, options || {});
  if(opts.method && opts.method !== "GET"){
    opts.headers["X-CSRF-Token"] = CSRF;
    opts.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, opts);
  if(res.status === 401){ location.reload(); return null; }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch(e) { data = {raw:text}; }
  if(!res.ok){
    toast(`${res.status} ${path}: ${(data && (data.error || data.reason)) || "richiesta fallita"}`, "bad");
    return null;
  }
  return data;
}

async function act(path, body){
  const data = await api(path, {method:"POST", body: JSON.stringify(body||{})});
  if(data) { toast(summarize(data)); await refresh(true); }
  return data;
}
function summarize(data){
  if(data.message) return data.message;
  if(data.results) return data.results.map(r => `${r.platform} ${r.state}`).join(", ") || "nessuna azione";
  if(data.state) return `${data.state}`;
  if(data.ok === false) return data.reason || "rifiutato";
  if(data.items) return `${data.items.length} item generati`;
  return "ok";
}

function badge(text, kind){ return `<span class="badge ${kind||""}">${esc(text)}</span>`; }
function stateBadge(state){
  const map = {published:"ok", dry_run:"ai", pending:"", leased:"warn", failed:"bad",
               blocked:"bad", needs_review:"warn", unknown:"warn", publishing:"warn",
               approved:"ok", rejected:"bad", draft:"", scheduled:"ai"};
  return badge(state, map[state] ?? "");
}
function when(ts){ return ts ? String(ts).replace("T"," ").replace("Z","") : "—"; }
function pct(a,b){ return b ? Math.min(100, Math.round(100*a/b)) : 0; }

/* ------------------------------------------------------------------ pages */
const PAGES = [
  ["overview","Overview","◧"],["guide","Guida operativa","☰"],
  ["calendar","Editorial Calendar","▤"],["studio","Content Studio","✎"],
  ["approvals","Approval Queue","✓"],["published","Published Posts","↗"],["inbox","Community Inbox","✉"],
  ["intelligence","Feedback Intelligence","◇"],["analytics","Analytics","▦"],["accounts","Social Accounts","◎"],
  ["agents","Agent Activity","⚙"],["logs","Logs & Audit","≡"],["settings","Settings","⚒"],
];

function renderNav(){
  const counts = STATE.status?.approvals?.pending || 0;
  const titleEl = $("#page-title");
  if(titleEl){ const found = PAGES.find(p => p[0] === STATE.page); titleEl.textContent = found ? found[1] : "Overview"; }
  $("#nav").innerHTML = PAGES.map(([id,label,ico]) => `
    <button class="nav-item ${STATE.page===id?"active":""}" onclick="go('${id}')">
      <span class="ico">${ico}</span><span>${label}</span>
      ${id==="approvals" && counts ? `<span class="pill">${counts}</span>` : ""}
    </button>`).join("");
}

function go(page){ STATE.page = page; location.hash = page; renderNav(); render(); }
window.go = go;

function editingNow(){
  // A silent poll rebuilds #content.  While a draft is dirty, or a textarea in the
  // page has focus, that would delete work the operator has not saved yet, so the
  // poll updates the chrome and leaves the page alone.
  if(STATE.dirty) return true;
  const el = document.activeElement;
  const content = document.getElementById("content");
  return Boolean(el && content && content.contains(el) && el.tagName === "TEXTAREA");
}

function renderFreshness(){
  const el = $("#freshness");
  if(!el) return;
  if(STATE.statusError){
    el.className = "freshness stale";
    el.textContent = "stato non aggiornato · " + STATE.statusError;
    return;
  }
  const secs = STATE.statusAt ? Math.max(0, Math.round((Date.now() - STATE.statusAt) / 1000)) : null;
  el.className = "freshness" + (secs === null || secs > 90 ? " stale" : "");
  el.textContent = (secs === null ? "mai aggiornato" : `aggiornato ${secs}s fa`)
    + (STATE.dirty ? " · modifiche non salvate" : "");
}

async function refresh(silent){
  const data = await api("/api/status");
  if(data){ STATE.status = data; STATE.statusAt = Date.now(); STATE.statusError = ""; }
  else { STATE.statusError = "il server non ha risposto"; }
  renderNav();
  renderTopbar();
  renderFreshness();
  if(silent && editingNow()) return;
  render();
}

function renderTopbar(){
  const s = STATE.status || {};
  const mode = s.mode_label || "mode unknown";
  const kill = s.kill_switch;
  $("#modebadge").innerHTML = badge(mode, s.mode === "draft" ? "" : "ai");
  $("#killbadge").innerHTML = kill
    ? `<button class="danger" onclick="act('/api/actions/killswitch', {engaged:false})">Kill switch ENGAGED — release</button>`
    : `<button onclick="act('/api/actions/killswitch', {engaged:true})">Pausa globale</button>`;
  const pending = s.approvals?.pending || 0;
  const attn = $("#attention");
  if(attn) attn.innerHTML = pending
    ? `<button class="attn" onclick="go('approvals')" title="Vai alla coda delle approvazioni">${pending} in attesa di approvazione</button>`
    : `<span class="attn zero">nessuna approvazione in attesa</span>`;
  const stats = s.stats || {};
  $("#pipelinesum").textContent =
    `${stats.items ?? 0} items · ${stats.scheduled_pending ?? 0} scheduled · ` +
    `${stats.published ?? 0} published · ${stats.dry_run_posts ?? 0} simulated`;
}

const RENDERERS = {};
function render(){
  const fn = RENDERERS[STATE.page] || RENDERERS.overview;
  $("#content").innerHTML = `<div class="empty">caricamento…</div>`;
  fn().catch(e => { $("#content").innerHTML = `<div class="empty">errore: ${esc(e.message)}</div>`; });
}

/* --------------------------------------------------------------- overview */
RENDERERS.overview = async () => {
  const data = await api("/api/overview");
  const s = data.status, stats = s.stats, health = s.database;
  const cards = [
    ["Items", stats.items, `${JSON.stringify(stats.items_by_state || {})}`],
    ["Da approvare", stats.pending_approvals, "mode A: una persona pubblica"],
    ["In coda", stats.scheduled_pending, "scheduled_post pending"],
    ["Pubblicati", stats.published, `${stats.dry_run_posts} simulazioni`],
    ["Fonti lette", stats.sources, `ultima sync ${when(data.last_sync)}`],
    ["Feedback", stats.feedback, `${stats.feedback_new} nuovi`],
  ].map(([k,v,sub]) => `<div class="card"><h2>${k}</h2><div class="big">${esc(v)}</div>
      <div class="dim">${esc(sub)}</div></div>`).join("");
  const connectors = Object.entries(data.connectors || {}).map(([name, st]) => `
    <tr><td>${esc(name)}</td><td>${st.ready ? badge("ready","ok")
      : badge(st.read_only_endpoints ? "sola lettura" : (st.dry_run ? "dry-run" : "draft"),"warn")}</td>
      <td class="muted">${esc((st.missing||[]).join("; ").slice(0,150) || "—")}</td></tr>`).join("");
  const pipeline = (data.pipeline || []).map(step => `
    <tr><td>${esc(step.step)}</td><td>${stateBadge(step.state)}</td>
      <td class="muted">${esc(step.detail)}</td><td class="mono">${when(step.at)}</td></tr>`).join("");
  const sources = (data.sources || []).map(row => `
    <tr><td>${esc(row.kind)}</td><td>${esc(row.title || row.external_ref).slice(0,70)}</td>
      <td class="mono dim">${when(row.published_at || row.fetched_at)}</td>
      <td>${row.url ? `<a href="${esc(row.url)}" target="_blank" rel="noopener">apri</a>` : ""}</td></tr>`).join("");
  $("#content").innerHTML = `
    <div class="grid">${cards}</div>
    <div class="grid wide" style="margin-top:14px">
      <div class="card"><h2>Pipeline</h2>
        <table><thead><tr><th>Fase</th><th>Stato</th><th>Dettaglio</th><th>Quando</th></tr></thead>
        <tbody>${pipeline || `<tr><td colspan="4" class="dim">nessuna esecuzione registrata</td></tr>`}</tbody></table>
        <div class="row" style="margin-top:10px">
          <button class="primary" onclick="act('/api/actions/sync', {})">Sync repo</button>
          <button onclick="act('/api/actions/produce', {limit:4})">Genera bozze</button>
          <button onclick="act('/api/actions/plan', {})">Pianifica</button>
          <button onclick="act('/api/actions/tick', {simulate:true})">Tick (simulazione)</button>
        </div>
      </div>
      <div class="card"><h2>Connettori &amp; salute</h2>
        <table><thead><tr><th>Connettore</th><th>Stato</th><th>Note / permessi mancanti</th></tr></thead>
        <tbody>${connectors}</tbody></table>
        <dl class="kv" style="margin-top:12px">
          <dt>database</dt><dd>${esc(health.database)}</dd>
          <dt>schema</dt><dd>v${esc(health.schema_version)}</dd>
          <dt>audit chain</dt><dd class="${health.audit_chain.ok ? "pos":"neg"}">
            ${health.audit_chain.ok ? "integra ("+health.audit_chain.checked+" eventi)" : "ROTTA"}</dd>
          <dt>LLM</dt><dd>${esc(s.llm.provider)} ${s.llm.available ? badge("disponibile","ok") : badge("assente","warn")}</dd>
          <dt>notifiche</dt><dd>${s.notifications.would_send ? badge("attive","ok") : badge("disattivate","")}</dd>
        </dl>
      </div>
      <div class="card"><h2>Ultime fonti dal repository</h2>
        <table><thead><tr><th>Tipo</th><th>Titolo</th><th>Data</th><th></th></tr></thead>
        <tbody>${sources || `<tr><td colspan="4" class="dim">nessuna fonte: esegui Sync repo</td></tr>`}</tbody></table>
      </div>
    </div>`;
};

/* ------------------------------------------------------------------ guide */
RENDERERS.guide = async () => {
  const data = await api("/api/guide");
  const kinds = {ok:"ok", todo:"", attention:"warn", blocked:"bad"};
  const steps = (data.steps || []).map(s => `
    <div class="card gstep">
      <div class="ghead">
        <div class="gtitle"><span class="gnum">${esc(s.n)}</span>
          <h2>${esc(s.title)}</h2></div>
        <div class="gacts">${badge(s.state, kinds[s.state] ?? "")}
          <button class="ghost" onclick="go('${esc(s.page)}')">Vai →</button></div>
      </div>
      <p class="muted" style="margin:8px 0 4px">${esc(s.action)}</p>
      <div class="mono dim">${esc(s.detail)}</div>
    </div>`).join("");
  $("#content").innerHTML = `
    <div class="card">
      <h2>Ciclo quotidiano · ${esc(data.mode_label)} (${esc(data.mode)})
        ${data.kill_switch ? badge("kill switch ENGAGED","bad") : badge("kill switch rilasciato","ok")}</h2>
      <p class="muted">Questo è l'ordine in cui le cose hanno senso. Ogni riga porta un numero letto adesso dal
        database: dove non c'è un dato, la riga dice che non c'è invece di mostrare un esempio inventato.</p>
      <p class="dim">Piattaforme consentite: ${esc((data.platforms_allowed || []).join(", ")) || "nessuna"}
        · lingue: ${esc((data.languages || []).join(", ")) || "nessuna"}
        · letto alle ${esc(String(data.generated_at || "").replace("T"," ").replace("Z",""))}</p>
      <div class="row"><button class="primary" onclick="refresh()">Rileggi adesso</button>
        <button onclick="go('approvals')">Coda approvazioni</button>
        <span class="dim">Scorciatoie: <span class="kbd">Ctrl K</span> comandi · <span class="kbd">?</span> aiuto</span></div>
    </div>
    <div class="grid" style="margin-top:14px">${steps || `<div class="empty">la guida non ha passi</div>`}</div>`;
};

/* -------------------------------------------------------------- calendar */
RENDERERS.calendar = async () => {
  const data = await api("/api/calendar?days=7");
  const days = (data.days || []).map(day => `
    <div class="day"><h3>${esc(day.date)}</h3>
      ${(day.slots||[]).map(s => `<div class="slot ${esc(s.state)}">
          <div class="t">${esc(s.local_time)} · ${esc(s.platform)}</div>
          <div>${esc((s.title||"").slice(0,54))}</div>
          <div class="dim">${esc(s.lang)} · ${esc(s.state)} ${s.last_error ? "· "+esc(s.last_error.slice(0,40)) : ""}</div>
        </div>`).join("") || `<div class="dim">—</div>`}
    </div>`).join("");
  const budgets = (data.budget || []).map(b => `<tr><td>${esc(b.platform)}</td><td class="mono">${b.planned}/${b.target}</td>
      <td><div class="bar"><span style="width:${pct(b.planned,b.target)}%"></span></div></td>
      <td>${badge(b.state, b.state==="full"?"ok":b.state==="partial"?"warn":"")}</td></tr>`).join("");
  const proposal = (data.initial_proposal || []).slice(0, 12).map(row => `
    <tr><td class="mono">${esc(row.date)} ${esc(row.time)}</td><td>${esc(row.platform)}</td>
      <td>${esc(row.label)}</td><td class="dim">${esc(row.status)}</td></tr>`).join("");
  $("#content").innerHTML = `
    <div class="card"><h2>Settimana dal ${esc(data.start)} · ${esc(data.timezone)}
      ${data.timezone_available ? "" : badge("timezone db assente: UTC","warn")}</h2>
      <div class="row">
        <button class="primary" onclick="act('/api/actions/plan', {schedule:true})">Pianifica e metti in coda</button>
        <button onclick="act('/api/actions/plan', {})">Solo proposta</button>
        <span class="dim">${esc(data.unscheduled_drafts)} bozze non pianificate</span>
      </div>
      <div class="cal" style="margin-top:12px">${days}</div>
    </div>
    <div class="grid wide" style="margin-top:14px">
      <div class="card"><h2>Budget della settimana</h2>
        <table><thead><tr><th>Piattaforma</th><th>Pianificati / target</th><th></th><th></th></tr></thead>
        <tbody>${budgets}</tbody></table>
        <p class="dim">Se un giorno è vuoto, non c'è abbastanza materiale reale: non viene inventato nulla per riempirlo.</p>
      </div>
      <div class="card"><h2>Calendario iniziale proposto</h2>
        <table><thead><tr><th>Quando</th><th>Piattaforma</th><th>Categoria</th><th>Stato</th></tr></thead>
        <tbody>${proposal}</tbody></table>
        <p class="dim">Proposta di partenza: nessuna riga qui è programmata.</p>
      </div>
    </div>`;
};

/* ---------------------------------------------------------------- studio */
RENDERERS.studio = async () => {
  const list = await api("/api/content?limit=100");
  STATE.studioItems = list.items || [];
  $("#content").innerHTML = `
    <div class="card"><h2>Content Studio</h2>
      <div class="toolbar">
        <input type="search" id="studio-search" placeholder="cerca titolo, stato o #id ( / )"
               value="${esc(STATE.studioQuery || "")}" oninput="studioQuery(this.value)">
        <select id="studio-select" onchange="loadItem(this.value)"></select>
        <button onclick="act('/api/actions/produce', {limit:3})">+ Nuove bozze</button>
        <span class="dim">confronto IT/EN e anteprima per piattaforma</span>
      </div>
    </div>
    <div id="studio-body" style="margin-top:14px"><div class="empty">scegli una bozza…</div></div>`;
  studioOptions();
  const visible = studioFiltered();
  if(visible.length){
    const keep = visible.some(i => i.id === STATE.studioId) ? STATE.studioId : visible[0].id;
    loadItem(keep);
  } else {
    $("#studio-body").innerHTML = `<div class="empty">${
      (STATE.studioItems || []).length ? "nessuna bozza con questa ricerca"
        : "nessuna bozza: esegui <strong>Genera bozze</strong> dalla Overview"}</div>`;
  }
};
function studioFiltered(){
  const q = (STATE.studioQuery || "").toLowerCase();
  return (STATE.studioItems || []).filter(i =>
    !q || `#${i.id} ${i.title} ${i.state} ${i.category || ""}`.toLowerCase().includes(q));
}
function studioOptions(){
  const sel = $("#studio-select");
  if(!sel) return;
  sel.innerHTML = studioFiltered().map(i =>
    `<option value="${i.id}">#${i.id} ${esc(i.title.slice(0,60))} [${esc(i.state)}]</option>`).join("")
    || `<option value="">nessun risultato</option>`;
  if(STATE.studioId) sel.value = String(STATE.studioId);
}
window.studioQuery = (value) => {
  STATE.studioQuery = value;
  studioOptions();
  const visible = studioFiltered();
  if(visible.length && !visible.some(i => i.id === STATE.studioId)) loadItem(visible[0].id);
  else if(!visible.length) $("#studio-body").innerHTML = `<div class="empty">nessuna bozza con questa ricerca</div>`;
};
window.loadItem = async (id) => {
  STATE.studioId = Number(id);
  markDirty(false);
  const item = await api(`/api/content/${id}`);
  const variants = item.variants || [];
  const byPlatform = {};
  variants.forEach(v => { (byPlatform[v.platform] = byPlatform[v.platform] || {})[v.lang] = v; });
  const blocks = Object.entries(byPlatform).map(([platform, langs]) => {
    const it = langs.it, en = langs.en;
    const preview = (v) => v ? `
      <textarea id="edit-${v.id}" data-variant="${v.id}" oninput="markDirty(true)">${esc(v.body)}</textarea>
      <div class="row" style="margin-top:6px">
        ${stateBadge(v.policy_state)} <span class="dim">${v.char_count} caratteri · ${esc(v.generator)}</span>
        <span class="spacer"></span>
        <button onclick="saveVariant(${v.id})">Salva</button>
        <button onclick="act('/api/actions/publish', {variant_id:${v.id}, live:false})">Simula invio</button>
        <button class="primary" onclick="confirmPublish(${v.id}, '${esc(platform)}')">Pubblica (live)</button>
      </div>
      ${v.hashtags ? `<div class="row" style="margin-top:6px"><span class="dim">hashtag:</span> ${esc(v.hashtags)}</div>` : ""}
      ${(v.media||[]).length ? `<div class="row" style="margin-top:6px">${(v.media||[]).map(m =>
        `<a class="chip" href="/media/${esc(m.split(/[\\/]/).pop())}" target="_blank" rel="noopener">${esc(m.split(/[\\/]/).pop())}</a>`).join("")}</div>` : ""}
      ${(v.policy_report?.reasons||[]).length ? `<div class="muted" style="margin-top:6px">policy: ${esc(v.policy_report.reasons.join(" · "))}</div>` : ""}
    ` : `<div class="empty">nessuna variante ${esc(platform)}</div>`;
    return `<div class="card"><h2>${esc(platform)} — italiano / inglese</h2>
      <div class="split"><div><div class="dim">IT</div>${preview(it)}</div>
      <div><div class="dim">EN</div>${preview(en)}</div></div></div>`;
  }).join("");
  $("#studio-body").innerHTML = `
    <div class="card"><h2>#${item.id} ${esc(item.title)}</h2>
      <div class="row">${badge(item.category)} ${stateBadge(item.state)} ${badge("rischio "+item.risk, item.risk==="high"||item.risk==="sensitive"?"bad":"")}
        <span class="dim">previsto: ${esc(item.planned_for || "non pianificato")}</span>
        <span class="spacer"></span>
        <button class="primary" onclick="act('/api/approvals', {item_id:${item.id}, decision:'approved'})">Approva</button>
        <button class="danger" onclick="act('/api/approvals', {item_id:${item.id}, decision:'rejected'})">Rifiuta</button>
      </div>
      <h2 style="margin-top:12px">Fatti verificabili (ogni affermazione viene da qui)</h2>
      <ul class="facts">${(item.facts||[]).map(f => `<li>${esc(f.claim)} — <a href="${esc(f.source_url)}" target="_blank" rel="noopener">${esc(f.source_url)}</a></li>`).join("") || "<li class='dim'>nessun fatto</li>"}</ul>
      ${item.notes ? `<div class="muted" style="margin-top:8px">note del generatore: ${esc(item.notes)}</div>` : ""}
    </div>
    ${blocks || `<div class="empty">questo item non ha varianti</div>`}`;
};
window.saveVariant = async (id) => {
  const el = $(`#edit-${id}`);
  const data = await act(`/api/content/variant/${id}`, {body: el.value});
  if(data) markDirty(false);
};
window.markDirty = (on) => { STATE.dirty = !!on; renderFreshness(); };
window.confirmPublish = (variantId, platform) => confirmDialog("Pubblicare davvero?",
  `Invia ora il contenuto su ${platform} tramite l'API della piattaforma. Se la policy richiede un umano, ` +
  "se manca un'approvazione o il kill switch è attivo, la pubblicazione viene bloccata e te lo dice.",
  () => act('/api/actions/publish', {variant_id: variantId, live: true}), "Pubblica (live)");

/* ------------------------------------------------------------- approvals */
// The filters live in STATE, not in the DOM, so the 30-second poll redraws the
// page without losing the selection the operator set up.
STATE.approvalFilter = {state: "pending", risk: "all", text: ""};
STATE.approvalSel = new Set();
STATE.approvalData = [];

function apOptions(values, current){
  return values.map(v => `<option value="${esc(v)}" ${v === current ? "selected" : ""}>${esc(v)}</option>`).join("");
}
function apFiltered(){
  const f = STATE.approvalFilter, text = (f.text || "").toLowerCase();
  return (STATE.approvalData || []).filter(r =>
    (f.state === "all" || r.state === f.state) &&
    (f.risk === "all" || r.risk === f.risk) &&
    (!text || `${r.item_title || ""} ${r.reason || ""} #${r.id}`.toLowerCase().includes(text)));
}
function apSyncControls(){
  // The rows and the controls must agree.  Changing a filter programmatically (the
  // palette, a test, a restored preference) would otherwise leave a <select> showing
  // one value while the table is filtered by another.
  const f = STATE.approvalFilter;
  const state = $("#ap-state");
  if(state && state.value !== f.state) state.value = f.state;
  const risk = $("#ap-risk");
  if(risk && risk.value !== f.risk) risk.value = f.risk;
  const search = $("#ap-search");
  if(search && search.value !== f.text) search.value = f.text;
}
function apRenderRows(){
  const rowsEl = $("#ap-rows");
  if(!rowsEl) return;
  apSyncControls();
  const rows = apFiltered();
  const pending = rows.filter(r => r.state === "pending");
  rowsEl.innerHTML = rows.map(r => `
    <tr class="${STATE.approvalSel.has(r.id) ? "rowsel" : ""}">
      <td>${r.state === "pending" ? `<input type="checkbox" class="cb"
        ${STATE.approvalSel.has(r.id) ? "checked" : ""} onchange="apToggle(${r.id}, this.checked)">` : ""}</td>
      <td class="mono">#${r.id}</td>
      <td>${esc(r.item_title || r.subject_type+":"+r.subject_id)}</td>
      <td>${badge(r.risk, r.risk==="high"||r.risk==="sensitive"?"bad":"")}</td>
      <td>${stateBadge(r.state)}</td>
      <td class="muted">${esc((r.reason || "").slice(0,90))}</td>
      <td class="dim">${when(r.requested_at)}</td>
      <td class="row">
        ${r.state==="pending" ? `
          <button class="primary" onclick="act('/api/approvals', {approval_id:${r.id}, decision:'approved', note: apNote()})">Approva</button>
          <button class="danger" onclick="act('/api/approvals', {approval_id:${r.id}, decision:'rejected', note: apNote()})">Rifiuta</button>` :
          `<span class="dim">${esc(r.decided_by||"—")} ${when(r.decided_at)}</span>`}
      </td>
    </tr>`).join("") || `<tr><td colspan="8" class="dim">nessuna richiesta con questi filtri</td></tr>`;
  const count = $("#ap-count");
  if(count) count.textContent =
    `· ${rows.length}/${(STATE.approvalData||[]).length} visibili · ${pending.length} in attesa`;
  const sel = $("#ap-sel");
  if(sel) sel.textContent = STATE.approvalSel.size ? `${STATE.approvalSel.size} selezionati` : "";
  const all = $("#ap-all");
  if(all) all.checked = pending.length > 0 && pending.every(r => STATE.approvalSel.has(r.id));
}
RENDERERS.approvals = async () => {
  const data = await api("/api/approvals?limit=200");
  STATE.approvalData = data.approvals || [];
  const f = STATE.approvalFilter;
  $("#content").innerHTML = `
    <div class="card">
      <div class="row"><h2 style="margin:0">Approval Queue <span class="dim" id="ap-count"></span></h2>
        <span class="spacer"></span></div>
      <div class="toolbar">
        <input type="search" id="ap-search" placeholder="cerca titolo, motivo o #id ( / )"
               value="${esc(f.text)}" oninput="apFilter('text', this.value)">
        <select id="ap-state" onchange="apFilter('state', this.value)">${apOptions(["pending","approved","rejected","expired","all"], f.state)}</select>
        <select id="ap-risk" onchange="apFilter('risk', this.value)">${apOptions(["all","low","medium","high","sensitive"], f.risk)}</select>
        <span class="dim">filtri: stato · rischio · testo</span>
      </div>
      <div class="toolbar">
        <label class="row"><input type="checkbox" class="cb" id="ap-all" onchange="apToggleAll(this.checked)"> seleziona tutto il filtro</label>
        <input type="text" id="ap-note" placeholder="nota della decisione (opzionale, finisce nell'audit)">
        <button class="primary" onclick="apBulk('approved')">Approva selezionati</button>
        <button class="danger" onclick="apBulk('rejected')">Rifiuta selezionati</button>
        <span class="spacer"></span>
        <span class="dim" id="ap-sel"></span>
      </div>
      <table><thead><tr><th></th><th>ID</th><th>Contenuto</th><th>Rischio</th><th>Stato</th><th>Motivo</th><th>Richiesto</th><th>Azioni</th></tr></thead>
      <tbody id="ap-rows"></tbody></table>
      <p class="dim">In mode A ogni pubblicazione passa da qui. Sicurezza, critiche, affermazioni finanziarie e modifiche ai permessi richiedono un umano in ogni modalità.</p>
    </div>`;
  apRenderRows();
};
window.apFilter = (key, value) => { STATE.approvalFilter[key] = value; apRenderRows(); };
window.apNote = () => { const el = $("#ap-note"); return el ? el.value : ""; };
window.apToggle = (id, on) => { if(on) STATE.approvalSel.add(id); else STATE.approvalSel.delete(id); apRenderRows(); };
window.apToggleAll = (on) => {
  apFiltered().filter(r => r.state === "pending").forEach(r => {
    if(on) STATE.approvalSel.add(r.id); else STATE.approvalSel.delete(r.id);
  });
  apRenderRows();
};
window.apBulk = async (decision) => {
  const ids = [...STATE.approvalSel];
  if(!ids.length){ toast("seleziona almeno una richiesta in attesa", "bad"); return; }
  const note = apNote();
  const run = async () => {
    let ok = 0;
    const failed = [];
    for(const id of ids){
      const res = await api("/api/approvals", {method:"POST",
        body: JSON.stringify({approval_id:id, decision, note})});
      if(res) ok++; else failed.push(id);
    }
    STATE.approvalSel.clear();
    toast(`${ok}/${ids.length} ${decision === "approved" ? "approvate" : "rifiutate"}`
      + (failed.length ? ` · non riuscite: ${failed.map(i => "#" + i).join(", ")}` : ""),
      failed.length ? "bad" : "ok");
    await refresh(true);
  };
  if(decision === "approved") confirmDialog("Approvare in blocco",
    `Approvare ${ids.length} contenut${ids.length === 1 ? "o" : "i"}? Ogni decisione resta registrata singolarmente nell'audit, con la nota.`,
    run, "Approva");
  else run();
};

/* ------------------------------------------------------------- published */
RENDERERS.published = async () => {
  const data = await api("/api/published?limit=200");
  const rows = (data.posts || []).map(p => `
    <tr><td>${esc(p.platform)}</td><td>${stateBadge(p.state)}</td>
      <td>${esc((p.title || (p.body||"").slice(0,50) || "").slice(0,60))}</td>
      <td class="dim">${when(p.published_at)}</td>
      <td>${p.url ? `<a href="${esc(p.url)}" target="_blank" rel="noopener">apri</a>` : (p.external_id ? `<span class="mono dim">${esc(p.external_id)}</span>` : "—")}</td>
      <td>${p.state==="publishing" ? `<button class="warn" onclick="resolveClaim(${p.id})">Risolvi</button>` : ""}</td>
    </tr>`).join("");
  $("#content").innerHTML = `
    <div class="grid">
      <div class="card"><h2>Pubblicati davvero</h2><div class="big pos">${esc(data.totals.published)}</div>
        <div class="dim">confermati da un ID della piattaforma</div></div>
      <div class="card"><h2>Simulazioni</h2><div class="big ai">${esc(data.totals.dry_run)}</div>
        <div class="dim">nessuna chiamata di scrittura effettuata</div></div>
      <div class="card"><h2>Da verificare</h2><div class="big warn">${esc(data.totals.unknown)}</div>
        <div class="dim">reclamati ma non confermati: serve un controllo umano</div></div>
      <div class="card"><h2>Falliti</h2><div class="big neg">${esc(data.totals.failed)}</div>
        <div class="dim">con il motivo registrato</div></div>
    </div>
    <div class="card" style="margin-top:14px"><h2>Registro pubblicazioni</h2>
      <table><thead><tr><th>Piattaforma</th><th>Stato</th><th>Testo</th><th>Quando</th><th>Link</th><th></th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6" class="dim">nessuna pubblicazione, nemmeno simulata</td></tr>`}</tbody></table>
    </div>`;
};
window.resolveClaim = async (id) => {
  await act(`/api/published/${id}/resolve`, {outcome:"published"});
};

/* -------------------------------------------------------------- inbox */
RENDERERS.inbox = async () => {
  const data = await api("/api/community?limit=100");
  const rows = (data.feedback || []).map(f => `
    <tr><td>${esc(f.platform)}</td><td>${badge(f.category)}</td>
      <td>${badge(f.priority, f.priority==="critical"?"bad":f.priority==="high"?"warn":"")}</td>
      <td>${esc((f.summary||f.body||"").slice(0,80))}</td>
      <td class="dim">${esc(f.author||"—")}</td>
      <td>${f.url ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">apri</a>` : "—"}</td>
      <td><button onclick="draftReply(${f.id})">Bozza risposta</button></td>
    </tr>`).join("");
  $("#content").innerHTML = `
    <div class="card"><h2>Community Inbox</h2>
      <div class="row"><button onclick="act('/api/actions/feedback', {})">Rileggi feedback dalle API</button>
        <span class="dim">sola lettura: nessuna risposta viene inviata automaticamente</span></div>
      <table style="margin-top:10px"><thead><tr><th>Piattaforma</th><th>Categoria</th><th>Priorità</th><th>Riepilogo</th><th>Autore</th><th>Fonte</th><th></th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7" class="dim">nessun feedback: esegui prima sync e poi la rilettura</td></tr>`}</tbody></table>
    </div>
    <div id="reply-box" style="margin-top:14px"></div>`;
};
window.draftReply = async (id) => {
  const data = await api(`/api/community/${id}/reply`, {method:"POST", body:"{}"});
  const box = $("#reply-box");
  if(!data) return;
  if(!data.ok){ box.innerHTML = `<div class="card"><h2>Risposta non proposta</h2><p class="muted">${esc(data.reason)}</p></div>`; return; }
  const d = data.draft;
  box.innerHTML = `<div class="card"><h2>Bozza di risposta · ${esc(d.platform)} / ${esc(d.lang)} ${badge("serve approvazione","warn")}</h2>
    <textarea id="reply-text">${esc(d.body)}</textarea>
    <div class="row" style="margin-top:8px"><span class="muted">${esc(d.reason)}</span></div></div>`;
};

/* -------------------------------------------------------- intelligence */
RENDERERS.intelligence = async () => {
  const data = await api("/api/feedback");
  const cats = Object.entries(data.by_category || {}).map(([cat, prios]) => `
    <tr><td>${badge(cat)}</td><td class="mono">${Object.entries(prios).map(([p,n]) => `${p}: ${n}`).join(" · ")}</td></tr>`).join("");
  const clusters = (data.clusters||[]).map(g => `
    <tr><td>${badge(g.category)}</td><td class="mono">${g.count}</td>
      <td>${esc(g.suggested_action)}</td>
      <td class="dim">${esc((g.items||[]).map(i=>i.summary.slice(0,40)).join(" | "))}</td></tr>`).join("");
  $("#content").innerHTML = `
    <div class="grid">
      <div class="card"><h2>Feedback (${data.window_days} giorni)</h2><div class="big">${esc(data.total)}</div>
        <div class="dim">per piattaforma: ${esc(JSON.stringify(data.by_platform))}</div></div>
      <div class="card"><h2>Critici aperti</h2><div class="big neg">${esc(data.open_critical)}</div>
        <div class="dim">sicurezza e bug bloccanti</div></div>
      <div class="card"><h2>Proposte di issue</h2>
        <div class="big">${(data.proposals?.proposals||[]).length}</div>
        <div class="dim">${esc(data.proposals?.reason || "")}</div></div>
    </div>
    <div class="grid wide" style="margin-top:14px">
      <div class="card"><h2>Per categoria</h2>
        <table><tbody>${cats || `<tr><td class="dim">nessun feedback classificato</td></tr>`}</tbody></table></div>
      <div class="card"><h2>Problemi ricorrenti (cluster)</h2>
        <table><thead><tr><th>Categoria</th><th>N</th><th>Azione suggerita</th><th>Righe</th></tr></thead>
        <tbody>${clusters || `<tr><td colspan="4" class="dim">nessun cluster con almeno 2 elementi</td></tr>`}</tbody></table>
        <p class="dim">${esc(data.note || "")}</p></div>
    </div>`;
};

/* ------------------------------------------------------------- analytics */
RENDERERS.analytics = async () => {
  const data = await api("/api/analytics?days=30");
  const platforms = Object.entries(data.by_platform || {}).map(([platform, b]) => `
    <div class="card"><h2>${esc(platform)}</h2>
      <dl class="kv">
        <dt>pubblicati</dt><dd>${b.posts}</dd>
        <dt>simulati</dt><dd>${b.dry_run}</dd>
        <dt>da verificare</dt><dd>${b.pending_unknown}</dd>
        <dt>metriche disponibili</dt><dd class="pos">${b.metrics_available}</dd>
        <dt>metriche non disponibili</dt><dd class="warn">${b.metrics_unavailable}</dd>
      </dl>
      ${Object.keys(b.metrics||{}).length ? `<table style="margin-top:8px"><tbody>
        ${Object.entries(b.metrics).map(([m,v]) => `<tr><td class="dim">${esc(m)}</td><td class="num">${v}</td></tr>`).join("")}
      </tbody></table>` : `<div class="empty" style="margin-top:8px">nessuna metrica disponibile</div>`}
    </div>`).join("");
  $("#content").innerHTML = `
    <div class="card"><h2>Analytics · finestra ${data.window_days} giorni (dal ${esc(data.since)})</h2>
      <div class="row"><button onclick="act('/api/actions/analytics', {days:30})">Raccogli metriche ora</button>
        <button onclick="window.open('/api/analytics/export?days=30&fmt=csv')">Esporta CSV</button>
        <button onclick="window.open('/api/analytics/export?days=30&fmt=json')">Esporta JSON</button></div>
      <p class="muted" style="margin-top:10px">${esc(data.sources?.unavailable || "")}</p>
      <p class="dim">${esc(data.sources?.trading_performance || "")}</p>
    </div>
    <div class="grid wide" style="margin-top:14px">
      ${platforms || `<div class="empty">nessun dato: non c'è nulla di pubblicato in questa finestra</div>`}
    </div>`;
};

/* ------------------------------------------------------------- accounts */
RENDERERS.accounts = async () => {
  const data = await api("/api/accounts");
  const rows = (data.accounts||[]).map(a => `
    <tr><td>${esc(a.platform)}</td><td>${esc(a.handle)}</td>
      <td>${a.status==="ready" ? badge("ready","ok") : badge(a.status,"warn")}</td>
      <td class="mono dim">${esc(a.token_env || "—")}</td>
      <td class="mono dim">${esc(a.token_fingerprint || "—")}</td>
      <td class="muted">${esc((a.scopes||[]).join(", ").slice(0,90))}</td></tr>`).join("");
  const cards = Object.entries(data.connectors||{}).map(([name, st]) => `
    <div class="card"><h2>${esc(name)}</h2>
      <div class="row">${st.ready ? badge("pronto","ok") : badge("non pronto","warn")}
        ${st.dry_run ? badge("dry-run","ai") : ""}</div>
      <dl class="kv" style="margin-top:8px">
        <dt>auth</dt><dd>${esc(st.auth_flow || st.mode || st.login || "—")}</dd>
        <dt>endpoint base</dt><dd>${esc(st.api_base || "—")}</dd>
        <dt>variabile credenziale</dt><dd>${esc(st.token_env || "—")}</dd>
        <dt>impronta</dt><dd>${esc(st.token_fingerprint || "—")}</dd>
      </dl>
      ${(st.missing||[]).length ? `<div class="muted" style="margin-top:8px">Da fare:<ul class="facts">${st.missing.map(m=>`<li>${esc(m)}</li>`).join("")}</ul></div>` : ""}
    </div>`).join("");
  $("#content").innerHTML = `
    <div class="card"><h2>Account registrati</h2>
      <div class="row"><button onclick="act('/api/actions/accounts', {})">Aggiorna stato</button>
        <span class="dim">solo nomi di variabili e impronte: nessun token è memorizzato o mostrato</span></div>
      <table style="margin-top:10px"><thead><tr><th>Piattaforma</th><th>Handle</th><th>Stato</th><th>Variabile</th><th>Impronta</th><th>Scope</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6" class="dim">nessun account: esegui "Aggiorna stato"</td></tr>`}</tbody></table>
    </div>
    <div class="grid wide" style="margin-top:14px">${cards}</div>`;
};

/* --------------------------------------------------------------- agents */
RENDERERS.agents = async () => {
  const data = await api("/api/agents?limit=60");
  const rows = (data.runs||[]).map(r => `
    <tr><td class="mono dim">${esc(r.id.slice(0,26))}</td><td>${esc(r.agent)}</td><td>${esc(r.command)}</td>
      <td>${stateBadge(r.state==="ok"?"published":r.state)}</td>
      <td class="muted">${esc(r.summary.slice(0,70))}</td>
      <td class="mono dim">${when(r.started_at)}</td>
      <td class="mono dim">${r.finished_at ? when(r.finished_at) : "in corso"}</td></tr>`).join("");
  const apis = (data.api_calls||[]).slice(0,15).map(a => `
    <tr><td>${esc(a.connector)}</td><td class="mono">${esc(a.method)} ${esc(a.endpoint)}</td>
      <td class="${a.ok ? "pos":"neg"}">${a.status || "—"}</td><td class="mono dim">${a.duration_ms} ms</td>
      <td class="dim">${esc((a.error||"").slice(0,50))}</td></tr>`).join("");
  $("#content").innerHTML = `
    <div class="card"><h2>Esecuzioni degli agenti</h2>
      <table><thead><tr><th>Run</th><th>Agente</th><th>Comando</th><th>Stato</th><th>Riepilogo</th><th>Inizio</th><th>Fine</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7" class="dim">nessuna esecuzione</td></tr>`}</tbody></table>
    </div>
    <div class="card" style="margin-top:14px"><h2>Ultime chiamate API (reali)</h2>
      <table><thead><tr><th>Connettore</th><th>Endpoint</th><th>Status</th><th>Durata</th><th>Errore</th></tr></thead>
      <tbody>${apis || `<tr><td colspan="5" class="dim">nessuna chiamata registrata</td></tr>`}</tbody></table>
    </div>`;
};

/* ------------------------------------------------------------ logs/audit */
RENDERERS.logs = async () => {
  const logs = await api("/api/logs?limit=120");
  const audit = await api("/api/audit?limit=60");
  const entries = (logs.entries||[]).map(e => `<div class="mono dim">${esc(JSON.stringify(e))}</div>`).join("");
  const rows = (audit.events||[]).map(e => `
    <tr><td class="mono">${e.id}</td><td class="mono dim">${when(e.ts)}</td><td>${esc(e.actor)}</td>
      <td class="mono">${esc(e.action)}</td><td class="dim">${esc(e.subject)}</td>
      <td class="dim">${esc(JSON.stringify(e.detail).slice(0,80))}</td></tr>`).join("");
  $("#content").innerHTML = `
    <div class="card"><h2>Log strutturati (memoria del processo)</h2>
      <pre style="max-height:280px">${entries || "(vuoto: questo processo non ha ancora scritto log)"}</pre>
    </div>
    <div class="card" style="margin-top:14px"><h2>Audit log (append-only, catena hash)
      ${audit.verify.ok ? badge("catena integra","ok") : badge("CATENA ROTTA","bad")}</h2>
      <div class="row"><button onclick="verifyAudit()">Verifica catena</button>
        <span class="dim">${esc(audit.verify.checked)} eventi verificati · i trigger rifiutano UPDATE e DELETE</span></div>
      <table style="margin-top:10px"><thead><tr><th>ID</th><th>Quando</th><th>Attore</th><th>Azione</th><th>Soggetto</th><th>Dettaglio</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6" class="dim">nessun evento</td></tr>`}</tbody></table>
    </div>`;
};
window.verifyAudit = async () => {
  const data = await api("/api/audit/verify");
  toast(data.ok ? `catena integra su ${data.checked} eventi` : `catena rotta all'evento #${data.at}: ${data.reason}`, data.ok ? "ok":"bad");
};

/* -------------------------------------------------------------- settings */
RENDERERS.settings = async () => {
  const data = await api("/api/settings");
  const stored = Object.entries(data.stored||{}).map(([k,v]) => `
    <tr><td class="mono">${esc(k)}</td><td class="mono dim">${esc(JSON.stringify(v).slice(0,120))}</td></tr>`).join("");
  const envs = Object.entries(data.env_present||{}).map(([k,v]) => `
    <tr><td class="mono">${esc(k)}</td><td>${v ? badge("impostata","ok") : badge("assente","warn")}</td></tr>`).join("");
  $("#content").innerHTML = `
    <div class="grid wide">
      <div class="card"><h2>Autonomia &amp; sicurezza</h2>
        <dl class="kv">
          <dt>modalità</dt><dd>${esc(data.autonomy.mode)}</dd>
          <dt>kill switch</dt><dd>${data.kill_switch ? "ENGAGED" : "released"}</dd>
          <dt>max post / giorno</dt><dd>${esc(JSON.stringify(data.editorial.max_posts_per_day))}</dd>
          <dt>intervallo minimo</dt><dd>${esc(data.editorial.min_gap_minutes)} min</dd>
          <dt>risposte automatiche</dt><dd>${data.autonomy.allow_replies ? "attive" : "disattivate"}</dd>
          <dt>proposta issue</dt><dd>${data.autonomy.allow_issue_proposals ? "attive" : "disattivate"}</dd>
        </dl>
        <div class="row" style="margin-top:10px">
          <button class="danger" onclick="act('/api/actions/killswitch', {engaged:true})">Ferma tutto</button>
          <button onclick="act('/api/actions/killswitch', {engaged:false})">Riabilita</button>
        </div>
        <p class="dim">Cambiare i limiti di autonomia è un'azione umana: viene registrata nell'audit con il valore precedente.</p>
      </div>
      <div class="card"><h2>Impostazioni memorizzate (database)</h2>
        <table><tbody>${stored || `<tr><td class="dim">nessuna</td></tr>`}</tbody></table>
      </div>
      <div class="card"><h2>Variabili d'ambiente citate dalla configurazione</h2>
        <table><tbody>${envs}</tbody></table>
        <p class="dim">Solo presenza: i valori non vengono mai letti né mostrati qui.</p>
      </div>
      <div class="card"><h2>Configurazione effettiva (sola lettura)</h2>
        <pre style="max-height:400px">${esc(data.effective_yaml || "")}</pre>
        <p class="dim">Modificala in <span class="mono">social.yaml</span>; il file è versionato, i segreti no.</p>
      </div>
    </div>`;
};

/* -------------------------------------------------- overlays & shortcuts */
function closeDialog(){ const el = $("#overlay"); if(el) el.remove(); }
window.closeDialog = closeDialog;

function overlay(html){
  closeDialog();
  const el = document.createElement("div");
  el.className = "overlay";
  el.id = "overlay";
  el.innerHTML = `<div class="dialog" role="dialog" aria-modal="true">${html}</div>`;
  document.body.appendChild(el);
  const box = el.firstElementChild;
  box.addEventListener("mousedown", e => { if(e.target === el) closeDialog(); });
  return el;
}

function confirmDialog(title, message, onYes, yesLabel){
  const el = overlay(`
    <header><h2>${esc(title)}</h2></header>
    <div class="body"><p class="muted" style="margin:0">${esc(message)}</p></div>
    <footer>
      <button class="ghost" onclick="closeDialog()">Annulla</button>
      <button class="danger" id="dlg-yes">${esc(yesLabel || "Conferma")}</button>
    </footer>`);
  const yes = $("#dlg-yes", el);
  yes.onclick = async () => { closeDialog(); await onYes(); };
  yes.focus();
}
window.confirmDialog = confirmDialog;

const SHORTCUTS = [
  ["Ctrl / Cmd + K", "palette dei comandi (pagine e azioni)"],
  ["/", "cerca nella pagina corrente"],
  ["r", "aggiorna i dati"],
  ["g poi una lettera", "vai a una pagina (g a = Approval Queue)"],
  ["Esc", "chiudi la finestra aperta"],
  ["?", "questo elenco"],
];
window.showHelp = () => overlay(`
  <header><h2>Scorciatoie da tastiera</h2></header>
  <div class="body"><table><thead><tr><th>Tasto</th><th>Azione</th></tr></thead><tbody>
    ${SHORTCUTS.map(([k, v]) => `<tr><td><span class="kbd">${esc(k)}</span></td><td>${esc(v)}</td></tr>`).join("")}
  </tbody></table></div>
  <footer><button class="ghost" onclick="closeDialog()">Chiudi</button></footer>`);

const COMMANDS = () => [
  ...PAGES.map(([id, label, ico]) => ({label, ico, hint:"pagina", run:() => go(id)})),
  {label:"Sync repo (rileggi le fonti)", ico:"⟳", hint:"azione", run:() => act("/api/actions/sync", {})},
  {label:"Genera bozze", ico:"✎", hint:"azione", run:() => act("/api/actions/produce", {limit:4})},
  {label:"Pianifica e metti in coda", ico:"▤", hint:"azione", run:() => act("/api/actions/plan", {schedule:true})},
  {label:"Tick in simulazione", ico:"⧗", hint:"azione", run:() => act("/api/actions/tick", {simulate:true})},
  {label:"Raccogli metriche (30 giorni)", ico:"▦", hint:"azione", run:() => act("/api/actions/analytics", {days:30})},
  {label:"Rileggi feedback dalle API", ico:"✉", hint:"azione", run:() => act("/api/actions/feedback", {})},
  {label:"Aggiorna stato degli account", ico:"◎", hint:"azione", run:() => act("/api/actions/accounts", {})},
  {label:"Pausa globale (kill switch)", ico:"■", hint:"attenzione", run:() =>
    confirmDialog("Pausa globale", "Fermare ogni pubblicazione finché non la riattivi?",
      () => act("/api/actions/killswitch", {engaged:true}), "Ferma tutto")},
  {label:"Rilascia il kill switch", ico:"▶", hint:"azione", run:() => act("/api/actions/killswitch", {engaged:false})},
  {label:"Scorciatoie da tastiera", ico:"⌘", hint:"aiuto", run: showHelp},
];

STATE.paletteItems = [];
let paletteIndex = 0;
window.openPalette = () => {
  overlay(`
    <header><h2>Comandi</h2><span class="dim">invio esegue · esc chiude</span></header>
    <div class="body">
      <input type="search" id="palette-input" placeholder="cerca una pagina o un'azione…" autocomplete="off">
      <div class="cmd-list" id="palette-list"></div>
    </div>`);
  const input = $("#palette-input");
  input.oninput = renderPalette;
  input.onkeydown = (e) => {
    if(e.key === "ArrowDown"){ e.preventDefault(); paletteIndex = Math.min(paletteIndex + 1, STATE.paletteItems.length - 1); renderPaletteList(); }
    else if(e.key === "ArrowUp"){ e.preventDefault(); paletteIndex = Math.max(paletteIndex - 1, 0); renderPaletteList(); }
    else if(e.key === "Enter"){ e.preventDefault(); runPalette(); }
  };
  renderPalette();
  input.focus();
};
function renderPalette(){
  const q = ($("#palette-input")?.value || "").toLowerCase();
  STATE.paletteItems = COMMANDS().filter(c => !q || c.label.toLowerCase().includes(q));
  paletteIndex = 0;
  renderPaletteList();
}
function renderPaletteList(){
  const list = $("#palette-list");
  if(!list) return;
  list.innerHTML = STATE.paletteItems.map((c, i) => `
    <div class="cmd ${i === paletteIndex ? "active" : ""}" onclick="runPalette(${i})">
      <span class="ico">${c.ico}</span><span>${esc(c.label)}</span>
      <span class="hint">${esc(c.hint)}</span></div>`).join("")
    || `<div class="empty">nessun comando corrisponde</div>`;
}
window.runPalette = (index) => {
  const cmd = STATE.paletteItems[typeof index === "number" ? index : paletteIndex];
  closeDialog();
  if(cmd) cmd.run();
};

function focusSearch(){
  const el = document.querySelector("#content input[type=search]");
  if(el) el.focus();
  else toast("questa pagina non ha un campo di ricerca", "bad");
}

document.addEventListener("keydown", (e) => {
  const active = document.activeElement || {};
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName || "") || active.isContentEditable;
  if((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === "k"){ e.preventDefault(); openPalette(); return; }
  if(e.key === "Escape"){ closeDialog(); return; }
  if(typing) return;
  if(e.key === "?"){ e.preventDefault(); showHelp(); return; }
  if(e.key === "/"){ e.preventDefault(); focusSearch(); return; }
  if(e.key === "r"){ e.preventDefault(); refresh(); return; }
  if(e.key === "g"){
    STATE.pendingGoto = true;
    setTimeout(() => { STATE.pendingGoto = false; }, 1200);
    return;
  }
  if(STATE.pendingGoto){
    STATE.pendingGoto = false;
    const hit = PAGES.find(p => p[1].charAt(0).toLowerCase() === String(e.key).toLowerCase());
    if(hit){ e.preventDefault(); go(hit[0]); }
  }
});

/* ------------------------------------------------------------------ boot */
function routeFromHash(){
  const page = (location.hash || "#overview").slice(1);
  return PAGES.some(p => p[0] === page) ? page : "overview";
}
window.addEventListener("hashchange", () => { STATE.page = routeFromHash(); renderNav(); render(); });
STATE.page = routeFromHash();
renderNav();
refresh();
setInterval(renderFreshness, 1000);
setInterval(() => { if(!document.hidden) refresh(true); }, 30000);
"""


def render_page(bootstrap: dict | None = None) -> str:
    """The whole document: login screen or app shell, plus the JSON API client."""
    boot = json.dumps(bootstrap or {}, ensure_ascii=False)
    html = HTML.replace("__STYLE__", STYLE).replace("__SCRIPT__", SCRIPT)
    return html.replace(PLACEHOLDER, boot)


def render_login(error: str = "", notice: str = "", bootstrap: dict | None = None) -> str:
    boot = json.dumps(bootstrap or {}, ensure_ascii=False)
    return (
        LOGIN.replace("{ERROR}", _esc(error)).replace("{NOTICE}", _esc(notice))
    ).replace(PLACEHOLDER, boot)


def _esc(text: object) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


HTML = """<!doctype html>
<html lang="it"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SelfTrade Social Manager</title>
<style>__STYLE__</style>
</head><body>
<div class="app">
  <aside class="sidebar">
    <div class="brand"><span class="mark">ST</span>
      <span class="name">Social Manager<small>selftrade · admin</small></span></div>
    <nav class="nav" id="nav"></nav>
    <div class="nav-foot">
      <div id="modebadge"></div>
      <div style="margin-top:8px" class="dim">Le scritture passano da policy, approvazioni e kill switch.</div>
      <div style="margin-top:8px"><a href="/logout">Esci</a></div>
    </div>
  </aside>
  <div class="main">
    <header class="topbar">
      <div>
        <h1 id="page-title">Overview</h1>
        <div class="sub" id="pipelinesum"></div>
      </div>
      <span class="spacer"></span>
      <span id="attention"></span>
      <span id="freshness" class="freshness"></span>
      <div class="row" id="killbadge"></div>
      <button class="ghost" onclick="refresh()" title="Aggiorna (r)">Aggiorna</button>
    </header>
    <main class="content" id="content"></main>
  </div>
</div>
<div id="toast" class="toast hidden"></div>
<script>window.__BOOT__ = __DASHBOARD_BOOTSTRAP__;</script>
<script>__SCRIPT__</script>
</body></html>
"""


LOGIN = """<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SelfTrade Social Manager · accesso</title>
<style>__STYLE__</style></head>
<body>
<form class="login" method="post" action="/login">
  <h1>Social Manager</h1>
  <p>Pannello di amministrazione. Le credenziali social non transitano da qui.</p>
  {ERROR}
  {NOTICE}
  <input type="password" name="password" placeholder="password" autofocus autocomplete="current-password">
  <button class="primary" style="width:100%" type="submit">Entra</button>
  <p style="margin-top:12px" class="dim">
    La prima volta la password è quella impostata in <span class="mono">SOCIAL_DASHBOARD_PASSWORD</span>;
    se la variabile non è impostata, impostala e riavvia: nessuna password predefinita è attiva.
  </p>
</form>
<script>window.__BOOT__ = __DASHBOARD_BOOTSTRAP__;</script>
</body></html>
"""
