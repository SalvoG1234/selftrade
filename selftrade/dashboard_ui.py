"""The dashboard's presentation layer: one inline HTML document, no remote assets.

:mod:`selftrade.dashboard` owns the HTTP server, the login and the JSON API; this
module owns **what the operator sees**.  They are split so the server logic stays
readable next to the API it serves, and the page - a design system, a collapsible
sidebar, a hash router and eight views - is one self-contained artifact that can be
reviewed on its own.

Everything is inline on purpose: the project serves no remote script, no CDN, no
webfont and no third-party asset (see ``docs/DASHBOARD.md``).  ``str.replace`` fills
in the boot block and the three refresh windows; nothing else is templated.

The page never invents a number.  A value the backend did not record is rendered as
``N/D`` and any panel built on demo data says so; the charts and metrics are drawn
from the same endpoints the CLI reads.
"""

from __future__ import annotations

HTML_PAGE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>selftrade — operations dashboard</title>
<style>
:root{
  color-scheme: dark;
  --bg:#090d14; --bg-1:#0c121c; --bg-2:#0f1724; --bg-3:#16202f;
  --panel:#0f1724; --panel-2:#0b111b; --raise:#141e2d;
  --bd:#1b2637; --bd-2:#27364b; --bd-3:#3a4d68;
  --tx:#e2e9f3; --tx-2:#9aa8bd; --tx-3:#6b7a91;
  --brand:#3b82f6; --brand-2:#60a5fa;
  --ai:#22d3ee; --ai-2:#a78bfa;
  --pos:#34d399; --neg:#f87171; --warn:#fbbf24; --info:#38bdf8;
  --pos-bg:rgba(52,211,153,.13); --neg-bg:rgba(248,113,113,.13);
  --warn-bg:rgba(251,191,36,.14); --ai-bg:rgba(34,211,238,.11); --brand-bg:rgba(59,130,246,.13);
  --r-xs:5px; --r-sm:7px; --r:10px; --r-lg:14px;
  --sh:0 1px 0 rgba(255,255,255,.02), 0 10px 30px -18px rgba(0,0,0,.9);
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --nav-w: 232px; --ticks: #1a2433;
}
[data-theme="light"]{
  color-scheme: light;
  --bg:#eef1f7; --bg-1:#e8ecf4; --bg-2:#ffffff; --bg-3:#dde3ee;
  --panel:#ffffff; --panel-2:#f6f8fc; --raise:#f2f5fa;
  --bd:#dce2ec; --bd-2:#c6d0de; --bd-3:#a6b5c9;
  --tx:#17202e; --tx-2:#5a6a80; --tx-3:#8492a6;
  --brand:#2563eb; --brand-2:#1d4ed8;
  --ai:#0e7490; --ai-2:#6d28d9;
  --pos:#0f9d64; --neg:#d63a3a; --warn:#a16207; --info:#0369a1;
  --pos-bg:rgba(15,157,100,.12); --neg-bg:rgba(214,58,58,.12);
  --warn-bg:rgba(161,98,7,.13); --ai-bg:rgba(14,116,144,.10); --brand-bg:rgba(37,99,235,.10);
  --sh:0 1px 2px rgba(20,30,50,.05), 0 14px 32px -20px rgba(20,30,50,.28);
  --ticks:#dfe5ee;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--tx);font-family:var(--sans);font-size:13.5px;
     line-height:1.5;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--brand-2);text-decoration:none}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.muted{color:var(--tx-2)} .dim{color:var(--tx-3)}
.ok,.pos{color:var(--pos)} .bad,.neg{color:var(--neg)} .warn{color:var(--warn)} .ai{color:var(--ai)}
.hidden{display:none !important}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
:focus-visible{outline:2px solid var(--brand-2);outline-offset:2px;border-radius:var(--r-xs)}
::selection{background:var(--brand-bg)}

/* ---------- shell ---------- */
.app{display:grid;grid-template-columns:var(--nav-w) 1fr;min-height:100vh}
.app.collapsed{--nav-w:62px}
.sidebar{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;
  background:var(--panel-2);border-right:1px solid var(--bd);overflow:hidden}
.brand{display:flex;align-items:center;gap:10px;padding:16px 16px 12px;font-weight:650;letter-spacing:.2px}
.brand .mark{width:26px;height:26px;flex:none;border-radius:8px;display:grid;place-items:center;
  background:linear-gradient(150deg,var(--brand),var(--ai-2));color:#f8fbff;font-size:13px;font-weight:800}
.brand .name{font-size:14.5px} .brand .name small{display:block;font-size:10px;font-weight:500;color:var(--tx-3);letter-spacing:.08em;text-transform:uppercase}
.nav{display:flex;flex-direction:column;gap:2px;padding:6px 8px;overflow-y:auto;flex:1}
.nav-item{display:flex;align-items:center;gap:11px;padding:8px 10px;border-radius:9px;border:1px solid transparent;
  background:none;color:var(--tx-2);font:inherit;font-size:13px;cursor:pointer;text-align:left;width:100%;
  transition:background .13s ease,color .13s ease,border-color .13s ease}
.nav-item .ico{width:18px;height:18px;flex:none;opacity:.85}
.nav-item:hover{background:var(--bg-3);color:var(--tx)}
.nav-item.active{background:var(--brand-bg);color:var(--tx);border-color:var(--bd-2);font-weight:600}
.nav-item.active .ico{opacity:1;color:var(--brand-2)}
.nav-grp{font-size:9.5px;text-transform:uppercase;letter-spacing:.11em;color:var(--tx-3);
  padding:9px 10px 3px;font-weight:700;white-space:nowrap;overflow:hidden}
.app.collapsed .nav-grp,.app .nav-grp.nav-grp-hide{height:1px;padding:5px 0 0;
  font-size:0;background:var(--bd);margin:4px 10px 0}
.nav-item.ai.active{background:var(--ai-bg);border-color:var(--bd-2)}
.nav-item.ai.active .ico{color:var(--ai)}
.side-foot{padding:8px;border-top:1px solid var(--bd);display:flex;flex-direction:column;gap:6px}
.side-foot .row{display:flex;gap:6px}
.side-foot .ver{font-size:10.5px;color:var(--tx-3);padding:2px 4px;font-family:var(--mono)}
.app.collapsed .brand .name,.app.collapsed .nav-item .lbl,.app.collapsed .side-foot .ver{display:none}
.app.collapsed .nav-item{justify-content:center;padding:9px 0}

.main{display:flex;flex-direction:column;min-width:0}
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  padding:10px 18px;background:color-mix(in srgb, var(--panel-2) 88%, transparent);
  backdrop-filter:blur(8px);border-bottom:1px solid var(--bd)}
.topbar h1{font-size:15px;margin:0;font-weight:640;letter-spacing:.2px}
.topbar .spacer{flex:1}
.statusbar{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.content{padding:16px 18px 40px;display:flex;flex-direction:column;gap:14px;min-width:0}
.view{display:none;flex-direction:column;gap:14px}
.view.active{display:flex}

/* ---------- grid & cards ---------- */
.grid{display:grid;gap:13px;grid-template-columns:repeat(12,minmax(0,1fr))}
.col-2{grid-column:span 2}.col-3{grid-column:span 3}.col-4{grid-column:span 4}
.col-5{grid-column:span 5}.col-6{grid-column:span 6}.col-7{grid-column:span 7}
.col-8{grid-column:span 8}.col-9{grid-column:span 9}.col-12{grid-column:span 12}
@media (max-width:1180px){.col-2,.col-3,.col-4{grid-column:span 6}.col-5,.col-7,.col-8,.col-9{grid-column:span 12}}
@media (max-width:820px){.col-2,.col-3,.col-4,.col-5,.col-6,.col-7,.col-8,.col-9{grid-column:span 12}}
.card{background:var(--panel);border:1px solid var(--bd);border-radius:var(--r);box-shadow:var(--sh);min-width:0}
.card-h{display:flex;align-items:center;gap:10px;padding:12px 15px;border-bottom:1px solid var(--bd)}
.card-h h2{margin:0;font-size:11px;font-weight:650;letter-spacing:.1em;text-transform:uppercase;color:var(--tx-2)}
.card-h .spacer{flex:1}
.card-b{padding:14px 15px}
.card-b.tight{padding:10px 12px}
.card-sub{font-size:11.5px;color:var(--tx-3);margin-top:8px}
.ai-card{border-color:color-mix(in srgb, var(--ai) 32%, var(--bd))}
.ai-card .card-h h2{color:var(--ai)}

/* ---------- stat tiles ---------- */
.tiles{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(168px,1fr))}
.tile{background:var(--panel);border:1px solid var(--bd);border-radius:var(--r);padding:12px 14px;box-shadow:var(--sh)}
.tile .k{font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--tx-3);display:flex;align-items:center;gap:5px}
.tile .v{font-size:21px;font-weight:640;font-family:var(--mono);font-variant-numeric:tabular-nums;margin-top:3px;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .s{font-size:11.5px;color:var(--tx-2);margin-top:2px}
.tile .spark{height:26px;margin-top:6px;width:100%}

/* ---------- chips & badges ---------- */
.chip{display:inline-flex;align-items:center;gap:5px;font-size:11px;line-height:1;padding:3px 8px;border-radius:999px;
  border:1px solid var(--bd-2);color:var(--tx-2);background:var(--bg-2);white-space:nowrap;font-family:var(--mono)}
.chip .dot{width:6px;height:6px;border-radius:50%;background:currentColor;flex:none}
.chip.ok{color:var(--pos);border-color:color-mix(in srgb,var(--pos) 40%,var(--bd-2));background:var(--pos-bg)}
.chip.bad{color:var(--neg);border-color:color-mix(in srgb,var(--neg) 40%,var(--bd-2));background:var(--neg-bg)}
.chip.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 40%,var(--bd-2));background:var(--warn-bg)}
.chip.ai{color:var(--ai);border-color:color-mix(in srgb,var(--ai) 40%,var(--bd-2));background:var(--ai-bg)}
.chip.info{color:var(--info);border-color:color-mix(in srgb,var(--info) 40%,var(--bd-2));background:var(--brand-bg)}
.chip.pulse .dot{animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.tag{display:inline-block;font-size:10.5px;padding:1px 6px;border-radius:var(--r-xs);background:var(--bg-3);color:var(--tx-2);font-family:var(--mono);border:1px solid var(--bd)}
.pill{font-size:11px;font-family:var(--mono);padding:2px 7px;border-radius:var(--r-xs);background:var(--bg-3);color:var(--tx-2)}

/* ---------- buttons & fields ---------- */
button{font:inherit}
.btn{display:inline-flex;align-items:center;gap:6px;background:var(--raise);color:var(--tx);border:1px solid var(--bd-2);
  border-radius:var(--r-sm);padding:6px 12px;font-size:12px;cursor:pointer;transition:background .12s ease,border-color .12s ease,transform .06s ease}
.btn:hover{background:var(--bg-3);border-color:var(--bd-3)}
.btn:active{transform:translateY(1px)}
.btn.primary{background:var(--brand);border-color:var(--brand);color:#f8fbff}
.btn.primary:hover{background:var(--brand-2)}
.btn.danger{color:var(--neg);border-color:color-mix(in srgb,var(--neg) 38%,var(--bd-2))}
.btn.danger:hover{background:var(--neg-bg)}
.btn.ghost{background:none;border-color:transparent;color:var(--tx-2)}
.btn.ghost:hover{background:var(--bg-3);color:var(--tx)}
.btn:disabled{opacity:.42;cursor:not-allowed}
.btn:disabled:hover{background:var(--raise)}
.btn.sm{padding:4px 9px;font-size:11.5px}
.icon-btn{background:none;border:1px solid transparent;color:var(--tx-2);border-radius:var(--r-sm);
  width:30px;height:30px;display:grid;place-items:center;cursor:pointer;font-size:15px}
.icon-btn:hover{background:var(--bg-3);color:var(--tx)}
input,select,textarea{background:var(--bg-1);color:var(--tx);border:1px solid var(--bd-2);border-radius:var(--r-sm);
  padding:6px 9px;font:inherit;font-size:12.5px}
input:focus,select:focus{border-color:var(--brand)}
label.fld{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:var(--tx-2)}
.toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 15px;border-bottom:1px solid var(--bd)}
.toolbar .spacer{flex:1}
.search{position:relative;display:inline-flex;align-items:center}
.search input{padding-left:26px;min-width:180px}
.search .mag{position:absolute;left:8px;color:var(--tx-3);font-size:12px;pointer-events:none}

/* ---------- tables ---------- */
.table-wrap{overflow-x:auto}
table.tbl{width:100%;border-collapse:separate;border-spacing:0;font-size:12.5px}
.tbl th{position:sticky;top:0;background:var(--panel-2);text-align:left;font-weight:600;color:var(--tx-2);
  font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;padding:9px 11px;border-bottom:1px solid var(--bd);
  cursor:pointer;white-space:nowrap;user-select:none}
.tbl th.no-sort{cursor:default}
.tbl th .car{color:var(--brand-2);margin-left:4px}
.tbl td{padding:8px 11px;border-bottom:1px solid var(--bd);font-variant-numeric:tabular-nums;vertical-align:top}
.tbl tbody tr:hover{background:var(--bg-2)}
.tbl tbody tr.exp{border-left:3px solid var(--brand)}
.tbl td .mono{font-size:12px}
.tbl .expand{border:1px solid var(--bd-2);background:none;color:var(--tx-2);border-radius:var(--r-xs);
  width:20px;height:20px;cursor:pointer;line-height:1}
.tbl .expand:hover{color:var(--tx);background:var(--bg-3)}
.tbl tr.detail td{background:var(--panel-2);border-bottom:2px solid var(--bd)}
.pager{display:flex;align-items:center;gap:8px;padding:10px 15px;border-top:1px solid var(--bd);color:var(--tx-2);font-size:11.5px}
.pager .pages{display:flex;gap:4px}
.pager button{background:var(--raise);border:1px solid var(--bd-2);color:var(--tx-2);border-radius:var(--r-xs);
  min-width:26px;height:26px;cursor:pointer;font-size:11.5px;padding:0 6px}
.pager button:hover:not(:disabled){background:var(--bg-3);color:var(--tx)}
.pager button.on{background:var(--brand-bg);color:var(--tx);border-color:var(--bd-2);font-weight:600}
.pager button:disabled{opacity:.4;cursor:not-allowed}

/* ---------- states ---------- */
.empty{padding:22px 16px;text-align:center;color:var(--tx-3);font-size:12.5px}
.empty .big{font-size:20px;opacity:.5;display:block;margin-bottom:6px}
.err{padding:14px 16px;color:var(--neg);font-size:12.5px;background:var(--neg-bg);border-radius:var(--r-sm);border:1px solid color-mix(in srgb,var(--neg) 30%,var(--bd))}
.skel{height:11px;border-radius:6px;margin:7px 0;background:linear-gradient(90deg,var(--bg-3) 25%,var(--bg-2) 50%,var(--bg-3) 75%);
  background-size:220% 100%;animation:sk 1.3s ease infinite}
.skel.w60{width:60%}.skel.w80{width:80%}.skel.w40{width:40%}
@keyframes sk{0%{background-position:120% 0}100%{background-position:-120% 0}}

/* ---------- kv & meters ---------- */
.kv{display:grid;grid-template-columns:auto 1fr;gap:5px 14px;font-size:12.5px;align-items:baseline}
.kv .k{color:var(--tx-2)}
.kv .v{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;min-width:0;overflow-wrap:anywhere}
.meter{height:7px;border-radius:5px;background:var(--bg-3);overflow:hidden;position:relative}
.meter>i{display:block;height:100%;background:var(--pos);border-radius:5px;transition:width .4s ease}
.meter>i.warn{background:var(--warn)} .meter>i.bad{background:var(--neg)}

/* ---------- tooltip ---------- */
.tip{display:inline-grid;place-items:center;width:14px;height:14px;border-radius:50%;border:1px solid var(--bd-3);
  color:var(--tx-3);font-size:9.5px;cursor:help;position:relative;font-family:var(--sans);font-weight:700}
.tip:hover,.tip:focus{color:var(--tx);border-color:var(--tx-3)}
/* One tooltip for the whole page, positioned by the front end (see `tipShow`): a CSS
   ::after cannot escape a card's or a table's scroll box, and this one can.  Any element
   carrying data-tip answers "what is this?" on hover or on keyboard focus. */
.tipbox{position:fixed;left:0;top:0;z-index:95;max-width:320px;pointer-events:none;
  background:var(--raise);color:var(--tx);border:1px solid var(--bd-2);border-radius:var(--r-sm);
  padding:7px 10px;font-size:11.5px;font-weight:400;line-height:1.45;box-shadow:var(--sh);
  opacity:0;transition:opacity .12s ease;white-space:normal;text-transform:none;letter-spacing:0}
.tipbox.on{opacity:1}
[data-tip]:not(button){cursor:help}
@media (hover:none){.tipbox{display:none}}

/* ---------- charts ---------- */
.chart{width:100%;display:block}
.chart-wrap{position:relative}
.price{font-size:28px;font-weight:660;font-family:var(--mono);letter-spacing:.4px;margin:2px 0 6px}
.price small{font-size:13px;font-weight:400;margin-left:9px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--tx-2);margin-top:8px}
.legend i{display:inline-block;width:10px;height:3px;border-radius:2px;margin-right:5px;vertical-align:middle}
.mk-stats{display:flex;gap:24px;flex-wrap:wrap;font-size:11.5px;color:var(--tx-2);margin:4px 0 10px}
.mk-stats span{white-space:nowrap}
.mk-stats b{display:block;font-family:var(--mono);font-size:13.5px;font-weight:620;color:var(--tx);margin-top:3px}

/* ---------- AI cycle ---------- */
.cycle{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.stage{position:relative;background:var(--panel-2);border:1px solid var(--bd);border-radius:var(--r);padding:11px 12px}
.stage .st-h{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:640}
.stage .st-n{width:19px;height:19px;border-radius:50%;display:grid;place-items:center;font-size:10px;font-weight:700;
  background:var(--bg-3);color:var(--tx-2);flex:none;font-family:var(--mono)}
.stage.s-ok .st-n{background:var(--pos-bg);color:var(--pos)}
.stage.s-active .st-n{background:var(--ai-bg);color:var(--ai)}
.stage.s-warn .st-n{background:var(--warn-bg);color:var(--warn)}
.stage.s-bad .st-n{background:var(--neg-bg);color:var(--neg)}
.stage .st-d{margin-top:7px;font-size:11.5px;color:var(--tx-2);display:flex;flex-direction:column;gap:3px}
.stage .st-d b{font-family:var(--mono);color:var(--tx)}
.stage.s-un .st-n{opacity:.6}
.stage.s-un{opacity:.72;border-style:dashed}

/* ---------- details ---------- */
details.tech{border-top:1px solid var(--bd);margin-top:10px}
details.tech>summary{cursor:pointer;padding:8px 0 0;font-size:11.5px;color:var(--tx-2);list-style:none}
details.tech>summary::-webkit-details-marker{display:none}
details.tech>summary::before{content:"+ ";color:var(--tx-3)}
details.tech[open]>summary::before{content:"− "}
details.tech .body{padding-top:8px}
pre.log{margin:0;padding:12px 14px;background:var(--panel-2);border-radius:var(--r-sm);border:1px solid var(--bd);
  font-family:var(--mono);font-size:11.5px;line-height:1.6;max-height:340px;overflow:auto;color:var(--tx-2);white-space:pre-wrap;word-break:break-word}
.banner{display:flex;gap:10px;align-items:flex-start;padding:11px 14px;border-radius:var(--r);border:1px solid var(--bd);
  background:var(--panel-2);font-size:12px;color:var(--tx-2)}
.banner .ico{color:var(--ai);flex:none;font-size:14px;line-height:1.3}
.banner.warn{border-color:color-mix(in srgb,var(--warn) 35%,var(--bd));background:var(--warn-bg)}
.banner.bad{border-color:color-mix(in srgb,var(--neg) 35%,var(--bd));background:var(--neg-bg)}
.rule{height:1px;background:var(--bd);margin:12px 0;border:0}
.list{display:flex;flex-direction:column}
.list .row{display:flex;gap:10px;align-items:baseline;padding:7px 0;border-bottom:1px solid var(--bd);font-size:12.5px}
.list .row:last-child{border-bottom:0}
.list .row .when{color:var(--tx-3);font-family:var(--mono);font-size:11px;white-space:nowrap;min-width:132px}
.list .row .body{flex:1;min-width:0}
.fade{animation:fade .2s ease}
@keyframes fade{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}

/* ---------- login ---------- */
#login{position:fixed;inset:0;background:radial-gradient(1100px 500px at 50% -10%,var(--bg-2),var(--bg));
  display:flex;align-items:center;justify-content:center;z-index:90}
#login .box{background:var(--panel);border:1px solid var(--bd-2);border-radius:var(--r-lg);padding:26px 26px 22px;width:330px;box-shadow:var(--sh)}
#login .box .brand{padding:0 0 4px}
#login p{color:var(--tx-2);font-size:12.5px;margin:2px 0 16px}
#login input{width:100%;margin-bottom:10px}
#login button{width:100%;padding:9px}
#login .err{color:var(--neg);font-size:12px;min-height:16px;margin-top:8px}
@media (prefers-reduced-motion: reduce){*{animation-duration:.001ms !important;transition-duration:.001ms !important}}
/* Narrow screens: keep the sidebar as an icon rail beside the content instead of an
   overlay that would hide what the page is for. */
@media (max-width:900px){
  .app{--nav-w:60px}
  .app .brand .name,.app .nav-item .lbl,.app .side-foot .ver{display:none}
  .app .nav-grp{height:1px;padding:5px 0 0;font-size:0;background:var(--bd);margin:4px 8px 0}
  .app .nav-item{justify-content:center;padding:9px 0}
  .content{padding:14px 12px 34px}
  .topbar{padding:9px 12px}
  .topbar h1{display:none}
}
</style>
</head>
<body>
<div class="tipbox" id="tipbox" role="tooltip" aria-hidden="true"></div>
<div id="login" class="hidden">
  <div class="box">
    <div class="brand"><span class="mark">st</span><span class="name">selftrade<small>operations dashboard</small></span></div>
    <p>Sign in to open the dashboard.</p>
    <input id="pw" type="password" placeholder="password" autocomplete="current-password"
      data-tip="The dashboard password. A fresh install accepts 0000 until someone replaces it in Settings → Login password.">
    <button class="btn primary" id="login-btn" onclick="doLogin()"
      data-tip="Checks the password and, when it matches, sets the signed HttpOnly session cookie that this page then uses.">sign in</button>
    <div class="err" id="login-err"></div>
  </div>
</div>

<div class="app" id="app">
  <aside class="sidebar" id="sidebar">
    <div class="brand"><span class="mark">st</span><span class="name">selftrade<small>operations dashboard</small></span></div>
    <nav class="nav" aria-label="Sections">
      <div class="nav-grp">Market</div>
      <button class="nav-item active" data-view="overview" onclick="setView('overview')"
        data-tip="Overview: the equity curve, the engine status, the latest activity and a one-line reading of the risk state.">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
        <span class="lbl">Overview</span></button>
      <button class="nav-item" data-view="market" onclick="setView('market')"
        data-tip="Market: the live public chart - pair, interval, last price, 24h range, volume and feed status. Keyless and read-only.">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 17l5-5 3 3 4-7 6 6"/><path d="M3 21h18"/></svg>
        <span class="lbl">Market</span></button>
      <div class="nav-grp">Research</div>
      <button class="nav-item ai" data-view="lab" onclick="setView('lab')"
        data-tip="Strategy Lab: the improvement cycle Collect to Learn, the version history with the stored diff for each step, and the reproducibility record.">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 3h6"/><path d="M10 3v6l-4.2 8.2A2 2 0 0 0 7.6 20h8.8a2 2 0 0 0 1.8-2.8L14 9V3"/><path d="M7 15h10"/></svg>
        <span class="lbl">Strategy Lab</span></button>
      <button class="nav-item" data-view="backtest" onclick="setView('backtest')"
        data-tip="Experiments: run one candidate through the same engine and cost model the CLI uses, and read the folds, the out-of-sample comparison and the costs.">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>
        <span class="lbl">Experiments</span></button>
      <div class="nav-grp">Trading & risk</div>
      <button class="nav-item" data-view="trading" onclick="setView('trading')"
        data-tip="Paper Trading: the simulated session - orders, positions, fees, equity - plus the Binance key probe and the capital account this rail would risk.">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 17l5-5 3 3 4-7 6 6"/><path d="M3 21h18"/></svg>
        <span class="lbl">Paper Trading</span></button>
      <button class="nav-item" data-view="capital" onclick="setView('capital')"
        data-tip="Capital: the simulated account and its append-only ledger - credit money, fund a paper session, withdraw, and read every movement with the reason it was made for.">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 2v20"/><path d="M17 6.5c-.9-1.2-2.6-2-4.6-2-2.6 0-4.4 1.3-4.4 3.2 0 2.1 2 2.8 4.6 3.4 2.6.6 4.6 1.3 4.6 3.4 0 1.9-1.9 3.2-4.6 3.2-2.2 0-4-.8-5-2.2"/></svg>
        <span class="lbl">Capital</span></button>
      <button class="nav-item" data-view="risk" onclick="setView('risk')"
        data-tip="Risk Center: the emergency stop, the sealed limits with the room left before each one trips, and what the alert channel actually sent.">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3l7.5 2.8v6.4c0 4-3.2 7.4-7.5 8.8-4.3-1.4-7.5-4.8-7.5-8.8V5.8z"/><path d="M12 8.5v3.2M12 15h.01"/></svg>
        <span class="lbl">Risk Center</span></button>
      <div class="nav-grp">Agent</div>
      <button class="nav-item ai" data-view="agents" onclick="setView('agents')"
        data-tip="Agent and Memory: what the agents are doing right now, the subsystems they depend on, the lessons they kept, the run history and the hall of fame.">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="5" y="8" width="14" height="11" rx="3"/><path d="M12 8V4"/><circle cx="12" cy="3" r="1.4"/><path d="M9 13h.01M15 13h.01"/><path d="M2 12v3M22 12v3"/></svg>
        <span class="lbl">Agent &amp; Memory</span></button>
      <button class="nav-item" data-view="memory" onclick="setView('memory')"
        data-tip="Memory and Knowledge: the lessons with their scope, the run history and the hall of fame.">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>
        <span class="lbl">Memory &amp; Knowledge</span></button>
      <div class="nav-grp">System</div>
      <button class="nav-item" data-view="logs" onclick="setView('logs')"
        data-tip="System Logs: the recorded risk events, the provider calls and the tracked loop's own log tail.">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/></svg>
        <span class="lbl">System Logs</span></button>
      <button class="nav-item" data-view="settings" onclick="setView('settings')"
        data-tip="System: the configuration read-only (environment variable NAMES only, never values), the login password and the sealed risk limits with their verification.">
        <svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 7 19.4a1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H1a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 2.6 7a1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 8 2.6 1.7 1.7 0 0 0 9.1 1V1a2 2 0 1 1 4 0v.1A1.7 1.7 0 0 0 15 2.6a1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9v.1a1.7 1.7 0 0 0 1.6 1.1H23a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1.1z"/></svg>
        <span class="lbl">System</span></button>
      </nav>
    <div class="side-foot">
      <div class="row">
        <button class="btn ghost sm" id="theme-btn" onclick="toggleTheme()" data-tip="Switches between the dark and light theme; the choice is kept in this browser.">theme</button>
        <button class="btn ghost sm" onclick="toggleNav()" data-tip="Collapses the sidebar to icons only, for a wider chart area.">collapse</button>
      </div>
      <div class="ver" id="side-ver" data-tip="The running version and build">…</div>
    </div>
  </aside>

  <div class="main">
    <header class="topbar">
      <button class="icon-btn" onclick="toggleNav()" aria-label="Toggle navigation" data-tip="Collapse or expand the sidebar.">☰</button>
      <h1 id="page-title">Overview</h1>
      <div class="statusbar" id="statusbar"></div>
      <span class="spacer"></span>
      <span class="muted mono" id="health">loading…</span>
      <span class="muted mono" id="session">loop: —</span>
      <span id="ctl-msg" class="muted"></span>
      <button class="btn sm" id="ctl-start" onclick="control('start')" disabled
        data-tip="Launches the tracked research loop (python -m selftrade evolve --session) as a detached child of this dashboard, and points at its pid and log. It evaluates genomes; it never places an order. Refused while one is already running.">start</button>
      <button class="btn sm" id="ctl-pause" onclick="control('pause')" disabled
        data-tip="Freezes the running loop at its next generation boundary: it really blocks there, so no generation is started while it waits. Resume picks it up from that same boundary.">pause</button>
      <button class="btn sm" id="ctl-resume" onclick="control('resume')" disabled
        data-tip="Lets a paused loop continue from the generation boundary where it stopped, without losing the run.">resume</button>
      <button class="btn sm" id="ctl-stop" onclick="control('stop')" disabled
        data-tip="Ends the run at the next generation boundary: the generation in flight finishes and the report is still written. Refused when nothing is running, rather than writing a request no process reads.">stop</button>
      <button class="btn sm ghost" onclick="doLogout()"
        data-tip="Clears the session cookie and returns to the login box. A reload after this asks for the password again.">sign out</button>
    </header>

    <div class="content">
      <!-- ============================ OVERVIEW ============================ -->
      <section class="view active" data-view="overview">
        <div class="toolbar card" style="border:1px solid var(--bd)">
          <button class="btn sm" onclick="refreshOverview(true)" data-tip="Re-reads every panel on this page now; the panels also refresh on their own timer.">refresh</button>
          <label class="fld">Market <span class="search"><span class="mag">⌕</span><input id="ov-symbol" size="11" spellcheck="false" aria-label="Market symbol" data-tip="The pair both charts follow, e.g. BTC/USDT. Prices come from Binance public data: keyless and read-only."></span></label>
          <select id="ov-interval" aria-label="Market interval" data-tip="Candle interval for both charts. Shorter intervals mean noisier candles and more requests.">
            <option>1m</option><option>3m</option><option>5m</option><option>15m</option>
            <option>30m</option><option>1h</option><option>4h</option><option>1d</option>
          </select>
          <button class="btn sm" onclick="applyMarket('ov')" data-tip="Applies this symbol and interval to the charts here and on the Trading page (they share one selection).">apply</button>
          <span class="spacer"></span>
          <span class="muted" id="ov-updated">…</span>
        </div>
        <div class="tiles" id="ov-tiles"></div>
        <div class="tiles" id="ov-cycle"></div>
        <div class="grid">
          <div class="card col-8">
            <div class="card-h"><h2 data-tip="The paper session's recorded equity, point by point — not a simulated curve drawn for the page. A gap means no equity was recorded for that stretch.">Equity curve</h2><span class="spacer"></span><span class="muted" id="ov-eq-meta"></span></div>
            <div class="card-b"><div class="chart-wrap"><canvas class="chart" id="ov-equity" style="height:190px" data-tip="Paper-session equity over time, from the equity rows in the database."></canvas></div></div>
          </div>
          <div class="card col-4">
            <div class="card-h"><h2 data-tip="The engine's own state: execution mode, the sealed-limits digest, whether the model side can move those limits, and the loop's counters.">Risk &amp; engine</h2></div>
            <div class="card-b" id="ov-status"></div>
          </div>
          <div class="card col-8">
            <div class="card-h"><h2 data-tip="Recent candles from Binance public data: keyless, read-only, cached and redrawn on this page's market timer.">Market</h2><span class="spacer"></span><span class="muted" id="ov-mk-meta"></span></div>
            <div class="card-b"><div class="chart-wrap"><canvas class="chart" id="ov-market" style="height:190px" data-tip="Candles for the selected pair and interval, straight from the public market feed."></canvas></div></div>
          </div>
          <div class="card col-4">
            <div class="card-h"><h2 data-tip="The most recent closed paper trades, newest first — fills, direction, size and P&amp;L from the paper rail.">Latest trades</h2><span class="spacer"></span><span class="muted" id="ov-tr-meta"></span></div>
            <div class="card-b tight" id="ov-trades"></div>
          </div>
          <div class="card col-12">
            <div class="card-h"><h2 data-tip="Risk events and provider calls as they were recorded, newest first — the same rows the System Logs page shows in full.">Recent activity &amp; anomalies</h2></div>
            <div class="card-b tight" id="ov-activity"></div>
          </div>
        </div>
      </section>

      <!-- ============================ TRADING ============================ -->
      <section class="view" data-view="trading">
        

        

        

        

        <div class="card" id="bk-card">
          <div class="card-h"><h2 data-tip="A read-only probe of the configured Binance Testnet key: permissions, clock offset, signature, balances, open orders and the rate budget. It runs on an unarmed broker, so nothing can be sent from this browser.">Binance key</h2><span class="spacer"></span><span class="muted" id="bk-status">checking…</span></div>
          <div class="toolbar">
            <span class="muted" id="bk-meta">…</span>
            <span class="spacer"></span>
            <button class="btn sm" onclick="refreshKey(true)" data-tip="Runs the same read-only probe as `binance check` again — including the signature test — right now.">recheck</button>
          </div>
          <div class="card-b" id="bk-body"><span class="muted">…</span></div>
        </div>

                  <div class="grid">
          <div class="card col-4">
            <div class="card-h"><h2 data-tip="The forward paper session: the strategy's signals become orders on the simulated rail, sized at the sealed cap and funded from the capital ledger.">Paper session</h2></div>
            <div class="card-b" id="tr-session"></div>
          </div>
          <div class="card col-8">
            <div class="card-h"><h2 data-tip="What the session is holding right now and how much of the sealed exposure cap that uses — the same numbers the pre-trade gate reads before it lets an order through.">Positions &amp; exposure</h2><span class="spacer"></span><span class="muted" id="tr-pos-meta"></span></div>
            <div class="card-b" id="tr-positions"></div>
          </div>
          <div class="card col-12">
            <div class="card-h"><h2 data-tip="Every closed paper fill: direction, size, entry and exit, the reason it closed and the net P&amp;L after costs.">Paper trades</h2></div>
            <div class="toolbar" id="tr-trades-bar"></div>
            <div class="table-wrap" id="tr-trades"></div>
            <div class="pager" id="tr-trades-pager"></div>
          </div>
        </div>
      </section>

      <!-- ============================ MARKET ============================= -->
      <section class="view" data-view="market">
        <div class="card">
          <div class="card-h"><h2 data-tip="A live chart on public market data: no key, no account, no order. It is here to show what the price is doing, not to place anything.">Live market</h2><span class="spacer"></span><span class="muted" id="mk-status">connecting…</span></div>
          <div class="toolbar">
            <span class="search"><span class="mag">⌕</span><input id="mk-symbol" size="12" spellcheck="false" aria-label="Symbol" data-tip="The pair this chart follows, e.g. BTC/USDT."></span>
            <select id="mk-interval" aria-label="Interval" data-tip="Candle interval: 1m is one candle per minute, 1d one per day.">
              <option>1m</option><option>3m</option><option>5m</option><option>15m</option>
              <option>30m</option><option>1h</option><option>4h</option><option>1d</option>
            </select>
            <button class="btn sm" onclick="applyMarket('mk')" data-tip="Applies the pair and interval, here and on the Overview page (one shared selection).">apply</button>
            <span class="spacer"></span>
            <span class="muted mono" id="mk-meta"></span>
          </div>
          <div class="card-b">
            <div class="price" id="mk-price" data-tip="Last price, with the 24-hour change and range beside it, as the public feed reports them.">…</div>
            <div class="mk-stats" id="mk-stats" data-tip="The 24-hour rolling stats the public feed reports with the price: high, low, traded base volume, quote volume and trade count. Hover a figure for the exact number; nothing here is estimated."></div>
            <div class="chart-wrap"><canvas class="chart" id="chart" style="height:340px" data-tip="Candlesticks: each bar is one interval, with its open, high, low and close."></canvas></div>
          </div>
        </div>
        <div class="card">
          <div class="card-h"><h2 data-tip="What this page is and what it is not: public market data, keyless, read-only. No account, no order, ever.">Read-only market data</h2></div>
          <div class="card-b"><div id="mk-notes" class="muted"></div></div>
        </div>
      </section>

      <!-- ============================= CAPITAL =========================== -->
      <section class="view" data-view="capital">
<div class="card" id="cap-card">
          <div class="card-h"><h2 data-tip="The simulated account the agents trade, and the only money this program moves. Every movement is a ledger row with a reason; no agent can credit itself.">Capital account</h2><span class="spacer"></span><span class="muted" id="cap-status">loading…</span></div>
          <div class="card-b">
            <div class="tiles" id="cap-tiles"></div>
            <div class="toolbar">
              <label class="fld">Amount <span class="search"><span class="mag">⌕</span><input id="cap-amount" size="10" inputmode="decimal" spellcheck="false" aria-label="Amount" oninput="capButtons()" data-tip="How much simulated capital this movement is for, in the account's currency. The policy caps in funding: decide the largest credit the account will accept."></span></label>
              <label class="fld">Reason <span class="search"><span class="mag">⌕</span><input id="cap-reason" size="26" spellcheck="false" placeholder="why this movement exists — it is kept in the ledger" aria-label="Reason" oninput="capButtons()" data-tip="Why this credit or withdrawal exists. It is stored in the ledger beside the balance it left behind, so the account can be audited later."></span></label>
              <button class="btn sm primary" id="cap-deposit" onclick="capAsk('deposit')" disabled data-tip="Adds simulated capital to the free balance of the account — a ledger row, not a payment. The agents cannot do this; only an operator with a reason can.">credit capital</button>
              <button class="btn sm" id="cap-withdraw" onclick="capAsk('withdraw')" disabled data-tip="Takes simulated capital back out of the free balance, if funding.allow_withdrawal permits it. Recorded as a ledger row with the reason you give.">withdraw</button>
              <button class="btn sm" id="cap-allocate" onclick="capAsk('allocate')" disabled data-tip="Moves capital from the free balance into the paper session, where the agents trade it through the same pre-trade risk gate as everything else.">fund the session</button>
              <button class="btn sm ghost" id="cap-release" onclick="capAsk('release')" disabled data-tip="Takes capital back from the paper session into the free balance when the session is over.">release</button>
              <span class="spacer"></span>
              <button class="btn sm ghost" onclick="refreshCapital()" data-tip="Re-reads the account and its ledger from the local database.">refresh</button>
            </div>
            <div class="toolbar hidden" id="cap-confirm">
              <span class="warn" id="cap-confirm-text"></span>
              <button class="btn sm primary" id="cap-go" onclick="capGo()" data-tip="Writes the movement to the ledger with the reason you gave. Simulated capital only \u2014 no exchange, no key, no order.">confirm</button>
              <button class="btn sm ghost" onclick="capCancel()" data-tip="Abandons this movement: no ledger row is written and the balance is untouched.">cancel</button>
            </div>
            <div class="card-b tight" id="cap-ledger"></div>
            <div class="pager" id="cap-ledger-pager"></div>
            <div class="card-sub">A <b>simulated</b> account: crediting it records a local ledger row and funds the paper session, where the agents trade it through the same risk gate as everything else. No exchange, no key and no order is involved, and <b>no agent can credit itself</b> — a movement needs an operator, a reason and a confirmation. The sealed 5% per position and 15% drawdown still bind on funded capital.</div>
          </div>
        </div>
        <div class="card">
          <div class="card-h"><h2 data-tip="What a credit here is, what it funds, and what it never touches. The ledger is local; the real rails read their own balance.">What a top-up is, and what it is not</h2></div>
          <div class="card-b"><div id="cap-notes" class="muted"></div></div>
        </div>
      </section>

      <!-- =========================== RISK CENTER ========================= -->
      <section class="view" data-view="risk">
        <div class="card" id="ks-card">
          <div class="card-h"><h2 data-tip="The one write action this page offers, and it only ever refuses: arming it makes the risk gate, the paper loop and the Binance adapter reject every order until you disarm. It cannot place one.">Emergency stop</h2><span class="spacer"></span><span class="muted" id="ks-status">loading…</span></div>
          <div class="toolbar">
            <span class="muted" id="ks-meta">…</span>
            <span class="spacer"></span>
            <button class="btn sm danger" id="ks-arm" onclick="ksAsk('arm')" disabled data-tip="Arms the file-backed kill switch: everything that can send an order consults it first and will refuse. Needs a reason and a confirmation; it is not a pause of the loop.">arm emergency stop</button>
            <button class="btn sm" id="ks-reset" onclick="ksAsk('reset')" disabled data-tip="Disarms the kill switch and lets orders be considered again. It also needs a reason and a confirmation.">disarm</button>
          </div>
          <div class="toolbar hidden" id="ks-confirm">
            <span class="warn" id="ks-confirm-text"></span>
            <input id="ks-reason" size="38" placeholder="reason (kept in the audit trail)" data-tip="Why the switch is being armed or disarmed. It is stored in the append-only audit trail next to the moment, so the record explains itself later.">
            <button class="btn sm primary" id="ks-go" onclick="ksGo()" data-tip="Writes the switch now: arming refuses every order on every rail, disarming allows orders to be considered again. It is recorded in the audit trail with your reason.">confirm</button>
            <button class="btn sm ghost" onclick="ksCancel()" data-tip="Abandons this action \u2014 the switch keeps the state it already had.">cancel</button>
          </div>
          <div class="card-b" id="ks-body"><span class="muted">…</span></div>
          <div class="card-b tight" id="ks-msg"></div>        </div>
        <div class="card" id="risk-card">
          <div class="card-h"><h2 data-tip="The active paper session measured against the sealed caps — drawdown, today's P&amp;L and exposure — with the headroom left before each one trips. The arithmetic is the pre-trade gate's own, so this page cannot show a limit the gate enforces differently.">Risk limits</h2><span class="spacer"></span><span class="muted" id="risk-status">loading…</span></div>
          <div class="toolbar">
            <span class="muted" id="risk-meta">…</span>
            <span class="spacer"></span>
            <button class="btn sm" onclick="refreshRisk()" data-tip="Measures the account again, now. Read-only: one database read, no exchange call, no rate budget spent.">refresh</button>
          </div>
          <div class="card-b">
            <div id="risk-headroom" class="muted">…</div>
            <div id="risk-alerts" class="card-sub">…</div>
            <div id="risk-body" class="muted" style="margin-top:10px">…</div>
          </div>        </div>
        <div class="card" id="al-card">
          <div class="card-h"><h2 data-tip="What the optional alert channel actually delivered, read from its own ledger. Only deliveries are recorded there, so nothing on this list is a message that never left the machine.">Alerts</h2><span class="spacer"></span><span class="muted" id="al-status">loading…</span></div>
          <div class="toolbar">
            <span class="muted" id="al-meta">…</span>
            <span class="spacer"></span>
            <button class="btn sm" onclick="refreshAlerts()" data-tip="Re-reads the delivery ledger. No message is sent by looking.">refresh</button>
          </div>
          <div class="card-b" id="al-body"><span class="muted">…</span></div>
        </div>
      </section>

      <section class="view" data-view="lab">
        <div class="card ai-card">
          <div class="card-h"><h2 data-tip="The six stages of one improvement cycle, each with the evidence behind it: the run it came from, the critic's verdict, the folds it was validated on, the paper session that followed and what it stored as a lesson.">Improvement cycle</h2><span class="spacer"></span>
            <span class="muted">Collect → Hypothesize → Backtest → Validate → Paper trade → Learn</span></div>
          <div class="card-b">
            <div class="cycle" id="lab-cycle"></div>
            <div class="card-sub">Every stage is derived from recorded evidence (rows, candidates, gate verdicts, the holdout read, the paper session, stored lessons). A stage with no evidence says <span class="dim">N/D</span>; nothing here is simulated.</div>
          </div>
        </div>
        <div class="toolbar card" style="border:1px solid var(--bd)">
          <label class="fld">Run <select id="lab-runs" onchange="loadLab(this.value)" data-tip="Which recorded run this page is about. Everything below is derived from that run's own rows, report and gate verdicts."></select></label>
          <span class="muted" id="lab-run-meta">…</span>
          <span class="spacer"></span>
          <button class="btn sm" onclick="loadLab($('lab-runs').value)" data-tip="Re-reads the selected run from the database and the report file it wrote.">reload</button>
        </div>
        <div class="card">
          <div class="card-h"><h2 data-tip="Whether this run can be redone: the seed, the dataset and a fingerprint of the code and configuration it was measured with.">Reproducibility</h2><span class="spacer"></span><span class="muted" id="lab-repro-chip"></span></div>
          <div class="card-b" id="lab-repro"></div>
        </div>
        <div class="tiles" id="lab-summary"></div>
        <div class="grid">
          <div class="card col-6">
            <div class="card-h"><h2 data-tip="The deployment verdict for this run's champion: hard checks against the sealed limits, plus whatever warnings were recorded. A warning is not a failure — it is something to answer before deploying.">Critic verdict</h2><span class="spacer"></span><span class="muted" id="lab-critic-chip"></span></div>
            <div class="card-b" id="lab-critic"></div>
          </div>
          <div class="card col-6">
            <div class="card-h"><h2 data-tip="The post-mortem of the losers: the worst losses with the context they were entered in, the failure modes that repeat, and the caveats that keep this from being a promise.">Autopsy</h2><span class="spacer"></span><span class="muted" id="lab-autopsy-chip"></span></div>
            <div class="card-b" id="lab-autopsy"></div>
          </div>
        </div>
        <div class="card">
          <div class="card-h"><h2 data-tip="Every recorded parent &#8594; candidate step, with the parameter diff taken against the genome the candidate was actually derived from. Expand a row for the full diff and both genomes.">Strategy versions</h2><span class="spacer"></span><span class="muted" id="lab-ver-meta"></span></div>
          <div class="card-b tight"><div class="card-sub" id="lab-ver-note">Every recorded parent → candidate step, with the diff taken against the genome it was actually derived from. Expand a row for the full diff and both genomes.</div></div>
          <div class="toolbar" id="lab-ver-bar"></div>
          <div class="table-wrap" id="lab-ver"></div>
          <div class="pager" id="lab-ver-pager"></div>
        </div>
        <div class="card">
          <div class="card-h"><h2 data-tip="Every candidate this run evaluated, with the metric it was scored on, its fold results and whether a hard gate refused it.">Experiments</h2><span class="spacer"></span><span class="muted" id="lab-exp-meta"></span></div>
          <div class="toolbar" id="lab-exp-bar"></div>
          <div class="table-wrap" id="lab-exp"></div>
          <div class="pager" id="lab-exp-pager"></div>
        </div>
        <div class="card">
          <div class="card-h"><h2 data-tip="What the program wrote down at each generation of this run: the best candidate so far, what changed and what it learned from it.">Per-generation lessons</h2></div>
          <div class="card-b tight" id="lab-gen"></div>
        </div>
      </section>

      <!-- ============================ AGENT CENTER ============================ -->
      <section class="view" data-view="agents">
        <div class="banner"><span class="ico">◈</span><div>The Agent Center lists the subsystems this program actually runs. <b>State</b> is derived from live evidence (the loop process, the risk gate, the paper rail, the data feed, the alert channel); <b>resources</b> are shown only where the backend measures them — CPU/RAM are not instrumented here, so they read <span class="dim">N/D</span> rather than a guess. No agent is decorative and no status is simulated.</div></div>
        <div class="tiles" id="ag-tiles"></div>
        <div class="toolbar card" style="border:1px solid var(--bd)">
          <button class="btn sm primary" id="ag-start" onclick="control('start')" data-tip="Launches the tracked research loop, detached, with its pid and its log. It evaluates genomes; it never places an order. Refused while one is already running.">start loop</button>
          <button class="btn sm" id="ag-pause" onclick="control('pause')" data-tip="Freezes the loop at its next generation boundary until you resume it.">pause</button>
          <button class="btn sm" id="ag-resume" onclick="control('resume')" data-tip="Continues a paused loop from the boundary where it stopped.">resume</button>
          <button class="btn sm danger" id="ag-stop" onclick="control('stop')" data-tip="Ends the run at the next boundary; the generation in flight finishes and the report is still written.">stop</button>
          <button class="btn sm ghost" onclick="renderAgents()" data-tip="Re-reads every subsystem's state now, from the process table, the database and the configuration.">refresh</button>
          <span class="spacer"></span>
          <span class="muted" id="ag-ctl-msg"></span>
          <span class="muted" id="ag-loop-meta"></span>
        </div>
        <div class="card">
          <div class="card-h"><h2 data-tip="Each row is a subsystem this program really runs, with the evidence its state came from. Resources appear only where the backend measures them: CPU/RAM are not instrumented, so they read N/D rather than a guess.">Agents &amp; subsystems</h2><span class="spacer"></span><span class="muted" id="ag-meta"></span></div>
          <div class="toolbar" id="ag-bar"></div>
          <div class="table-wrap" id="ag-table"></div>
          <div class="pager" id="ag-pager"></div>
        </div>
        <div class="card">
          <div class="card-h"><h2 data-tip="What each stage of the pipeline needs before it can run, and what it hands to the next stage when it finishes.">Pipeline dependencies</h2></div>
          <div class="card-b" id="ag-deps"></div>
        </div>
      </section>

      <!-- ============================ BACKTESTING ============================ -->
      <section class="view" data-view="backtest">
        <div class="card ai-card">
          <div class="card-h"><h2 data-tip="Run the evaluation engine on one strategy, in this session only. It writes nothing \u2014 no database row, no report, no order \u2014 so it is safe to try a genome you are only curious about.">Run a backtest</h2><span class="spacer"></span><span class="muted" id="bt-run-meta"></span></div>
          <div class="toolbar">
            <label class="fld">Strategy <select id="bt-genome" aria-label="Strategy" data-tip="The genome to evaluate: a champion from the hall of fame, or a strategy family with its default parameters."></select></label>
            <label class="fld">Window <select id="bt-window" aria-label="Window" data-tip="Which slice of the series to measure on. The folds are what the search optimises; the holdout tail is out of sample and is meant to be read once; the whole series is in-sample and therefore optimistic.">
              <option value="folds">walk-forward folds (the objective the search optimises)</option>
              <option value="holdout">holdout tail (out of sample)</option>
              <option value="full">whole series (in-sample)</option>
            </select></label>
            <button class="btn sm primary" id="bt-run" onclick="runBacktest()" data-tip="Runs the evaluation now, with the same engine, fills and cost model the CLI uses. It writes nothing: no database row, no report, no order.">run backtest</button>
            <span class="spacer"></span>
            <span class="muted" id="bt-run-status"></span>
          </div>
          <div class="card-b"><div class="card-sub">Evaluates the chosen genome with the same engine and cost model the CLI uses, on the local dataset. It writes nothing: no row, no report, no order, and it never arms the stop. Every panel below names the window it was measured over.</div></div>
        </div>
        <div class="tiles" id="bt-tiles"></div>
        <div class="card" id="bt-run-card" style="display:none">
          <div class="card-h"><h2 data-tip="The result of the backtest you just asked for, in this session only: nothing here was stored, and reloading the page clears it.">Requested backtest</h2><span class="spacer"></span><span class="muted" id="bt-res-meta"></span></div>
          <div class="card-b"><div class="tiles" id="bt-res-tiles"></div><div class="grid"><div class="col-8"><div class="chart-wrap"><canvas class="chart" id="bt-res-equity" style="height:190px" data-tip="Equity of the backtest you just requested, with its starting cash as the baseline."></canvas></div></div><div class="col-4" id="bt-res-detail"></div></div><div class="card-sub" id="bt-res-src"></div></div>
        </div>
        <div class="grid">
          <div class="card col-7">
            <div class="card-h"><h2 data-tip="The session's equity measured against the initial cash line, so a curve that merely drifts with the price is visible as such.">Equity vs baseline</h2><span class="spacer"></span><span class="muted" id="bt-eq-meta"></span></div>
            <div class="card-b">
              <div class="chart-wrap"><canvas class="chart" id="bt-equity" style="height:210px" data-tip="Equity of the recorded session against the initial cash line, so drift and profit are told apart."></canvas></div>
              <div class="legend"><span data-tip="The session\u2019s equity as the engine recorded it, point by point."><i style="background:var(--brand)"></i>session equity</span><span data-tip="The starting cash line: a curve that only tracks this has not made money, it has merely followed the market."><i style="background:var(--tx-3)"></i>initial cash</span></div>
              <div class="card-sub" id="bt-eq-src"></div>
            </div>
          </div>
          <div class="card col-5">
            <div class="card-h"><h2 data-tip="How far below its own running peak the equity was at each point. It is the shape of the pain, not the profit.">Underwater (drawdown)</h2><span class="spacer"></span><span class="muted" id="bt-dd-meta"></span></div>
            <div class="card-b">
              <div class="chart-wrap"><canvas class="chart" id="bt-dd" style="height:210px" data-tip="Drawdown from the session\u2019s running peak at each recorded equity point: the shape of the pain, not the profit."></canvas></div>
              <div class="card-sub">Drawdown from the session's own running peak, per recorded equity point.</div>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="card-h"><h2 data-tip="Every recorded champion scored on the same out-of-sample window, so the comparison is like for like rather than a mix of in-sample numbers.">Strategy comparison (out-of-sample)</h2><span class="spacer"></span><span class="muted" id="bt-strat-meta"></span></div>
          <div class="toolbar" id="bt-strat-bar"></div>
          <div class="table-wrap" id="bt-strategies"></div>
          <div class="pager" id="bt-strat-pager"></div>
        </div>
        <div class="grid">
          <div class="card col-5">
            <div class="card-h"><h2 data-tip="The fees and slippage every simulated fill pays, taken from the configuration. They are the reason a strategy that looks good on raw prices can lose money.">Cost model</h2></div>
            <div class="card-b" id="bt-costs"></div>
          </div>
          <div class="card col-7">
            <div class="card-h"><h2 data-tip="The optimiser's metric on each out-of-sample fold, next to the single holdout read. Consistency across folds matters more than one good fold.">Walk-forward folds (champion)</h2><span class="spacer"></span><span class="muted" id="bt-folds-meta"></span></div>
            <div class="card-b">
              <div class="chart-wrap"><canvas class="chart" id="bt-folds" style="height:150px" data-tip="The optimiser\u2019s metric on each out-of-sample fold, next to the single holdout read. Consistency across folds is the point."></canvas></div>
              <div class="card-sub">The optimiser's metric on each out-of-sample fold versus the single holdout read.</div>
            </div>
          </div>
        </div>
      </section>

      <!-- ============================ MEMORY ============================ -->
      <section class="view" data-view="memory">
        <div class="tiles" id="mem-stats"></div>
        <div class="card">
          <div class="card-h"><h2 data-tip="What the program wrote down and carries into later runs. Each lesson names the scope it applies to; the button on a run row filters this table to that run.">Lessons &amp; knowledge</h2><span class="spacer"></span><span class="muted" id="mem-les-scope"></span><span class="muted" id="mem-les-meta"></span></div>
          <div class="toolbar" id="mem-les-bar"></div>
          <div class="table-wrap" id="mem-lessons"></div>
          <div class="pager" id="mem-les-pager"></div>
        </div>
        <div class="grid">
          <div class="card col-7">
            <div class="card-h"><h2 data-tip="Every recorded evolution run with its champion, its metric and the gate verdict — the rows the AI Lab reads in depth, one run at a time.">Run history</h2></div>
            <div class="table-wrap" id="mem-runs"></div>
          </div>
          <div class="card col-5">
            <div class="card-h"><h2 data-tip="The best genomes recorded so far, ranked by the evaluation metric. These are what the backtest page's strategy list is built from.">Hall of fame</h2><span class="spacer"></span><span class="muted" id="mem-hall-meta"></span></div>
            <div class="card-b tight" id="mem-hall"></div>
          </div>
        </div>
      </section>

      <!-- ============================ LOGS ============================ -->
      <section class="view" data-view="logs">
        <div class="toolbar card" style="border:1px solid var(--bd)">
          <button class="btn sm" onclick="renderLogs()" data-tip="Re-reads all three logs now from the local database and the loop's log file.">refresh</button>
          <label class="fld">Rows <select id="log-limit" aria-label="Rows per table" onchange="setLogLimit(this.value)" data-tip="How many rows each log table asks for. More rows mean a slower read and a longer page.">
            <option value="50">50</option><option value="100">100</option><option value="200">200</option>
            <option value="500">500</option><option value="1000">1000</option>
          </select></label>
          <span class="spacer"></span>
          <span class="muted" id="log-updated">…</span>
        </div>
        <div class="tiles" id="log-tiles"></div>
        <div class="card">
          <div class="card-h"><h2 data-tip="Everything the sealed risk layer recorded: halts, refusals and the warnings before them, with the numbers that triggered them and who they came from.">Risk events</h2><span class="spacer"></span><span class="muted" id="log-ev-meta"></span></div>
          <div class="toolbar" id="log-ev-bar"></div>
          <div class="table-wrap" id="log-events"></div>
          <div class="pager" id="log-ev-pager"></div>
        </div>
        <div class="card">
          <div class="card-h"><h2 data-tip="Every outbound call to an external provider, with its status, latency and error — the record that explains a slow run or a refused one.">Provider calls</h2><span class="spacer"></span><span class="muted" id="log-call-meta"></span></div>
          <div class="toolbar" id="log-call-bar"></div>
          <div class="table-wrap" id="log-calls"></div>
          <div class="pager" id="log-call-pager"></div>
        </div>
        <div class="card">
          <div class="card-h"><h2 data-tip="The tail of the tracked loop's own log file — the process this dashboard starts — as it was written by the run itself.">Loop log</h2><span class="spacer"></span><span class="muted mono" id="log-loop-meta"></span></div>
          <div class="toolbar">
            <button class="btn sm" id="log-follow-btn" onclick="toggleFollow()" data-tip="Follow re-reads the tail every few seconds and keeps the newest line in view, so you can watch a run in progress.">follow</button>
            <button class="btn sm ghost" onclick="copyLoopLog()" data-tip="Copies the log tail to the clipboard, for pasting into a report or an issue.">copy</button>
            <span class="muted" id="log-follow-msg">follow re-reads the tail every few seconds and keeps the newest line in view</span>
          </div>
          <div class="card-b" id="log-loop"></div>
        </div>
      </section>

      <!-- ============================ SETTINGS ============================ -->
      <section class="view" data-view="settings">
        <div class="banner warn"><span class="ico">⚠</span><div><b>Live trading is disabled.</b> This build sends no real orders and offers no command that could: the Binance rail is a read-only probe, the paper rail fills on a simulator, and the emergency stop can only <i>refuse</i> orders. Environment variable <b>names</b> are shown; values are never read back or displayed.</div></div>
        <div class="tiles" id="set-tiles"></div>
        <!-- The password card is static markup on purpose: the settings body is re-mounted
             on every refresh tick, and a form that wiped itself while being filled in would
             be a bug of the worst kind - the one that loses what you typed. -->
        <div class="grid" id="set-password-grid">
          <div class="card col-12" id="pw-card">
            <div class="card-h"><h2 data-tip="The one credential in the project. A fresh install accepts the bootstrap password 0000; setting your own here stores only a salted PBKDF2 hash, and changing it signs out every other session.">Login password</h2><span class="spacer"></span><span class="muted" id="pw-status">loading…</span></div>
            <div class="card-b">
              <div id="pw-banner"></div>
              <div class="toolbar">
                <label class="fld">Current <span class="search"><span class="mag">⌕</span><input id="pw-current" type="password" size="16" autocomplete="current-password" aria-label="Current password" oninput="pwButtons()" onkeydown="pwEnter(event)" data-tip="The password that is working now. It is required: a session someone copied must not be enough to change the login."></span></label>
                <label class="fld">New <span class="search"><span class="mag">⌕</span><input id="pw-new" type="password" size="16" autocomplete="new-password" aria-label="New password" oninput="pwButtons()" onkeydown="pwEnter(event)" data-tip="The password to use from now on. The minimum length is the one in dashboard.password_min_length (4 unless changed), and it must differ from the current one."></span></label>
                <label class="fld">Repeat <span class="search"><span class="mag">⌕</span><input id="pw-repeat" type="password" size="16" autocomplete="new-password" aria-label="Repeat the new password" oninput="pwButtons()" onkeydown="pwEnter(event)" data-tip="Typed twice on purpose: a password you cannot reproduce is a lockout, and the confirm step is where a typo is caught."></span></label>
                <button class="btn sm primary" id="pw-change" onclick="pwAsk()" disabled data-tip="Asks for a confirmation, then replaces the stored hash and signs out every other session. Only a salted PBKDF2 hash is written — never the password itself.">change password</button>
                <span class="spacer"></span>
                <button class="btn sm ghost" id="pw-reload" onclick="renderSettings()" data-tip="Re-reads where the password comes from now (the stored file, or the environment variable that overrides it).">refresh</button>
              </div>
              <div class="toolbar hidden" id="pw-confirm">
                <span class="warn" id="pw-confirm-text"></span>
                <button class="btn sm primary" id="pw-go" onclick="pwGo()" data-tip="Applies the change: the stored hash is replaced and every other session is signed out. Nothing happens until you press this.">confirm</button>
                <button class="btn sm ghost" onclick="pwCancel()" data-tip="Abandons the change and clears the fields. The current password stays as it is.">cancel</button>
              </div>
              <!-- the outcome of a change lives here, in its own element: the settings body is
                   re-mounted every refresh tick, and a confirmation that vanished after 15 s
                   would take "every other session was signed out" with it. -->
              <div class="card-sub ok" id="pw-msg"></div>
              <div class="card-sub" id="pw-hint"></div>
              <div class="card-sub" id="pw-note">The one credential in the project. Only a <b>salted PBKDF2 hash</b> is stored — never the password — and changing it signs out every other session. When <span class="mono">dashboard.password_env</span> is set, that variable decides the login and this card can only report it.</div>
            </div>
          </div>
        </div>
        <div class="grid" id="set-body"></div>
      </section>
    </div>
  </div>
</div>
<script>
'use strict';
/* ============================================================================
   selftrade dashboard - front end
   Sections: constants/state · utils · components · tables · charts · auth ·
   data · per-view renderers · safety panels (stop/limits/alerts/key) · boot.
   ============================================================================ */

const BOOT = __BOOT__;
const REFRESH = __REFRESH__ * 1000;      /* panels */
const MK_REFRESH = __MK_REFRESH__ * 1000; /* live market chart */
const KEY_REFRESH = __KEY_REFRESH__ * 1000;

const VIEWS = ['overview', 'market', 'trading', 'capital', 'lab', 'backtest', 'risk',
  'agents', 'memory', 'logs', 'settings'];
const TITLES = {overview:'Overview', market:'Market', trading:'Paper Trading',
  capital:'Capital', lab:'Strategy Lab', backtest:'Experiments', risk:'Risk Center',
  agents:'Agent & Memory', memory:'Memory & Knowledge', logs:'System Logs', settings:'System'};

const S = {
  authed: false, started: false, view: 'overview',
  theme: localStorage.getItem('st_theme') || 'dark',
  collapsed: localStorage.getItem('st_nav') === '1',
  market: {symbol: BOOT.symbol, interval: BOOT.interval, bars: BOOT.bars},
  marketData: null, lab: null, labRun: null, tables: {}, expanded: {},
  //: The Backtesting page: what the operator asked to evaluate, and the last answer.
  //: Kept so the auto-refresh tick can redraw the view without paying for the compute
  //: again - a backtest runs only when the button is pressed.
  bt: {sel: null, window: 'folds', result: null},
  //: The Memory page: the run whose lessons are being shown (null = all of them).
  memRun: null,
  //: How many rows the System Logs page asks for per table, and whether it is following.
  logLimit: 200,
  logFollow: false,
  //: Re-run results of recorded versions, keyed by version row id (survives a redraw).
  verRuns: {},
  //: The capital account's last answer, and the movement waiting for a confirmation.
  cap: null, capPending: null
};

const $ = id => document.getElementById(id);
function mount(id, html){ const el = $(id); if(el) el.innerHTML = html; }

/* ---------------------------------- utils --------------------------------- */
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function isNum(v){ return v !== null && v !== undefined && v !== '' && !isNaN(v); }
function fmt(v, d){ return isNum(v) ? Number(v).toLocaleString(undefined, {minimumFractionDigits:d||0, maximumFractionDigits:d||0}) : 'N/D'; }
function nf(v, d){ return fmt(v, d); }
function pct(v, d){ return isNum(v) ? (v > 0 ? '+' : '') + Number(v).toFixed(d == null ? 2 : d) + '%' : 'N/D'; }
function sgn(v){ return isNum(v) ? (v > 0 ? 'pos' : (v < 0 ? 'neg' : '')) : ''; }
function money(v, cur){ return isNum(v) ? Number(v).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) + (cur ? ' ' + cur : '') : 'N/D'; }
function dt(ts){
  if(ts === null || ts === undefined || ts === '') return 'N/D';
  let d = (typeof ts === 'number') ? new Date(ts * 1000) : new Date(String(ts).replace(' ', 'T'));
  if(isNaN(d.getTime())) return String(ts);
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}
function ago(ts){
  if(ts === null || ts === undefined || ts === '') return '';
  let t = (typeof ts === 'number') ? ts * 1000 : new Date(String(ts).replace(' ', 'T')).getTime();
  if(isNaN(t)) return '';
  let s = Math.max(0, (Date.now() - t) / 1000);
  if(s < 60) return Math.round(s) + 's ago';
  if(s < 3600) return Math.round(s/60) + 'm ago';
  if(s < 86400) return Math.round(s/3600) + 'h ago';
  return Math.round(s/86400) + 'd ago';
}
function dur(sec){
  if(!isNum(sec)) return 'N/D';
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60;
  if(h) return h + 'h ' + m + 'm';
  if(m) return m + 'm ' + s + 's';
  return s + 's';
}
const cellOf = (row, key) => key.split('.').reduce((o, k) => (o == null ? undefined : o[k]), row);

/* -------------------------------- components ------------------------------ */
function chip(text, kind, dot, tid){ return '<span class="chip ' + (kind||'') + (dot ? ' pulse' : '') + '"' + (tid ? ' data-tip="' + esc(tid) + '"' : '') + '>' + (dot ? '<span class="dot"></span>' : '') + esc(text) + '</span>'; }
function tip(text){ return '<span class="tip" tabindex="0" data-tip="' + esc(text) + '">?</span>'; }
function tile(k, v, sub, cls, tid){
  return '<div class="tile"' + (tid ? ' data-tip="' + esc(tid) + '"' : '') + '><div class="k">' + esc(k) + (tid ? ' ' + tip(tid) : '') + '</div>' +
    '<div class="v ' + (cls||'') + '">' + v + '</div>' +
    (sub ? '<div class="s">' + sub + '</div>' : '') + '</div>';
}

/* ------------------------------- tooltips ---------------------------------
   Hovering (or tabbing to) anything explained tells you what it is.  The listener is
   on the document, so panels that re-mount themselves every refresh tick keep working
   with nothing to re-bind.  One box, clamped to the viewport, over everything. */
let tipOn = null;
function tipHide(){ const b = $('tipbox'); if(b){ b.classList.remove('on'); b.textContent = ''; } tipOn = null; }
function tipPlace(host, ev){
  const b = $('tipbox'); if(!b) return;
  const box = b.getBoundingClientRect();
  const pad = 8, vw = innerWidth, vh = innerHeight;
  const r = host.getBoundingClientRect();
  const anchorX = ev && ev.clientX ? ev.clientX : r.left;
  const anchorY = ev && ev.clientY ? ev.clientY : r.top;
  let x = ev && ev.clientX ? ev.clientX + 12 : r.left;
  let y = ev && ev.clientY ? ev.clientY + 18 : r.bottom + 6;
  /* Prefer below the pointer, but flip above it rather than fall off the bottom; then
     clamp on both axes, so the sentence is always readable where it is shown. */
  if(y + box.height + pad > vh) y = anchorY - box.height - 10;
  const clamp = (v, size, limit) => Math.min(Math.max(pad, v), Math.max(pad, limit - size - pad));
  b.style.left = Math.round(clamp(x, box.width, vw)) + 'px';
  b.style.top = Math.round(clamp(y, box.height, vh)) + 'px';
}
function tipShow(host, ev){
  const b = $('tipbox');
  const text = host && host.getAttribute ? host.getAttribute('data-tip') : '';
  if(!b || !text){ tipHide(); return; }
  if(tipOn !== host){ tipOn = host; b.textContent = text; b.classList.add('on'); }
  tipPlace(host, ev);
}
/* A label and the field it names are one control: hovering the word must answer too, and
   the field it points at is where the sentence already is. */
function tipHostOf(node){
  if(!node || !node.closest) return null;
  const host = node.closest('[data-tip]');
  if(host) return host;
  const lab = node.closest('label');
  const field = lab && lab.querySelector('input, select, textarea');
  if(field && field.getAttribute('data-tip')){ lab.dataset.tip = field.getAttribute('data-tip'); return lab; }
  return null;
}
document.addEventListener('mouseover', e => { const h = tipHostOf(e.target); if(h) tipShow(h, e); else tipHide(); });
document.addEventListener('mouseout', e => {
  const h = tipHostOf(e.target); if(!h || h !== tipOn) return;
  if(e.relatedTarget && h.contains(e.relatedTarget)) return;  /* still inside the same element */
  tipHide();
});
document.addEventListener('focusin', e => { const h = tipHostOf(e.target); if(h) tipShow(h, null); });
document.addEventListener('focusout', () => tipHide());
document.addEventListener('keydown', e => { if(e.key === 'Escape') tipHide(); });
addEventListener('scroll', () => tipHide(), true);
addEventListener('resize', () => tipHide());
function empty(msg, big){ return '<div class="empty"><span class="big">' + (big||'◇') + '</span>' + esc(msg) + '</div>'; }
function errBox(msg){ return '<div class="err">' + esc(msg) + '</div>'; }
function skel(rows){ let h = ''; const w = ['w80','w60','w60','w40']; for(let i=0;i<(rows||3);i++) h += '<div class="skel ' + w[i%4] + '"></div>'; return h; }
/* kv rows: [label, valueHtml, tip?] - the tip goes on the label and on the value, so
   hovering either half of the row answers what this number is. */
function kv(pairs){
  const rows = pairs.filter(Boolean).map(p => {
    const t = p[2] ? ' data-tip="' + esc(p[2]) + '"' : '';
    return '<div class="k"' + t + '>' + esc(p[0]) + '</div><div class="v"' + t + '>' + p[1] + '</div>';
  }).join('');
  return '<div class="kv">' + rows + '</div>';
}
function meter(frac, cls){
  const p = Math.max(0, Math.min(1, isNum(frac) ? frac : 0)) * 100;
  return '<div class="meter"><i class="' + (cls||'') + '" style="width:' + p.toFixed(1) + '%"></i></div>';
}

/* ---------------------------------- tables -------------------------------- */
class DataTable {
  constructor(container, bar, pager, opts){
    this.c = container; this.bar = bar; this.pager = pager; this.o = opts;
    this.q = ''; this.page = 1;
    this.sort = opts.sort || null; this.dir = opts.dir || 'desc';
    this.filter = {};
    this.render(true);
  }
  value(row, key){ return cellOf(row, key); }
  setRows(rows){
    const atTop = this.page === 1;
    this.o.rows = rows || [];
    this.page = atTop ? 1 : this.page;
    this.render(false);
  }
  view(){
    let r = (this.o.rows || []).slice();
    if(this.q){
      const keys = this.o.searchKeys || this.o.columns.map(c => c.key);
      const q = this.q.toLowerCase();
      r = r.filter(x => keys.some(k => String(this.value(x, k) == null ? '' : this.value(x, k)).toLowerCase().includes(q)));
    }
    if(this.o.filterFn) r = r.filter(x => this.o.filterFn(x, this.filter));
    if(this.sort){
      const col = this.o.columns.find(c => c.key === this.sort);
      const f = (col && col.sortValue) ? col.sortValue : (x => this.value(x, this.sort));
      r.sort((a, b) => { const va = f(a), vb = f(b); const d = (va > vb ? 1 : va < vb ? -1 : 0); return this.dir === 'asc' ? d : -d; });
    }
    return r;
  }
  render(withBar){
    if(!this.c || !this.c.isConnected) return;
    const all = this.view();
    const size = this.o.pageSize || 12;
    const pages = Math.max(1, Math.ceil(all.length / size));
    if(this.page > pages) this.page = pages;
    const start = (this.page - 1) * size;
    const slice = all.slice(start, start + size);
    if(!all.length){
      this.c.innerHTML = empty(this.q ? 'No rows match the search.' : (this.o.empty || 'No data recorded.'), this.q ? '⌕' : '◇');
      if(this.pager) this.pager.innerHTML = '';
    } else {
      this.c.innerHTML = '<table class="tbl"><thead><tr>' + this.o.columns.map(c => this.th(c)).join('') + '</tr></thead><tbody>' +
        slice.map(row => this.tr(row)).join('') + '</tbody></table>';
    }
    if(this.bar && withBar !== false) this.renderBar();
    this.renderPager(all.length, pages);
    this.wire();
  }
  th(c){
    const active = this.sort === c.key;
    const car = active ? ' <span class="car">' + (this.dir === 'asc' ? '↑' : '↓') + '</span>' : '';
    return '<th class="' + (c.sortable === false ? 'no-sort' : '') + '" data-sort="' + esc(c.key) + '"' +
      (c.tip ? ' data-tip="' + esc(c.tip) + '"' : '') + '>' +
      esc(c.label) + (c.tip ? ' ' + tip(c.tip) : '') + car + '</th>';
  }
  tr(row){
    const id = this.o.rowId ? this.o.rowId(row) : null;
    const open = id != null && S.expanded[id];
    /* Every cell of an explained column carries the explanation: a bare `0.84` in a wide
       table is exactly the thing a reader has to hover. */
    let cells = this.o.columns.map(c => {
      const raw = this.value(row, c.key);
      const val = c.render ? c.render(raw, row) : esc(raw == null || raw === '' ? 'N/D' : raw);
      return '<td class="' + (c.align === 'right' ? 'num' : '') + '"' +
        (c.tip ? ' data-tip="' + esc(c.tip) + '"' : '') + '>' + val + '</td>';
    }).join('');
    let html = '<tr class="' + (open ? 'exp' : '') + '" data-row="' + esc(id == null ? '' : id) + '">' + cells + '</tr>';
    if(open && this.o.detail) html += '<tr class="detail"><td colspan="' + this.o.columns.length + '">' + this.o.detail(row) + '</td></tr>';
    return html;
  }
  renderBar(){
    const parts = [];
    if(this.o.countable !== false) parts.push('<span class="search"><span class="mag">⌕</span><input data-q placeholder="Search…" value="' + esc(this.q) + '" aria-label="Search table" data-tip="Filters the rows in this table as you type, across the columns that carry text."></span>');
    for(const f of (this.o.filters || [])){
      const opts = ['<option value="">' + esc(f.label) + ': all</option>'].concat(
        (f.options || []).map(o => '<option value="' + esc(o.value) + '"' + (this.filter[f.key] == o.value ? ' selected' : '') + '>' + esc(o.label) + '</option>'));
      parts.push('<select data-f="' + esc(f.key) + '" aria-label="' + esc(f.label) + '" data-tip="Shows only the rows whose ' + esc(f.label) + ' matches, out of the values actually present in this table.">' + opts.join('') + '</select>');
    }
    if(this.o.actions) parts.push('<span class="spacer"></span>' + this.o.actions);
    if(this.bar) this.bar.innerHTML = parts.join('');
  }
  renderPager(total, pages){
    if(!this.pager) return;
    if(total <= (this.o.pageSize || 12)){ this.pager.innerHTML = '<span>' + total + ' row' + (total === 1 ? '' : 's') + '</span>'; return; }
    const start = (this.page - 1) * (this.o.pageSize || 12) + 1;
    const end = Math.min(total, this.page * (this.o.pageSize || 12));
    const nums = [];
    for(let p = 1; p <= pages; p++){
      if(p === 1 || p === pages || Math.abs(p - this.page) <= 1) nums.push(p);
      else if(nums[nums.length-1] !== '…') nums.push('…');
    }
    this.pager.innerHTML = '<span>' + start + '–' + end + ' of ' + total + '</span><span class="spacer" style="flex:1"></span>' +
      '<div class="pages"><button data-p="' + (this.page-1) + '"' + (this.page<=1?' disabled':'') + '>‹</button>' +
      nums.map(p => p === '…' ? '<button disabled>…</button>' : '<button data-p="' + p + '" class="' + (p===this.page?'on':'') + '">' + p + '</button>').join('') +
      '<button data-p="' + (this.page+1) + '"' + (this.page>=pages?' disabled':'') + '>›</button></div>';
  }
  wire(){
    if(this.bar){
      const q = this.bar.querySelector('[data-q]');
      if(q) q.oninput = () => { this.q = q.value; this.page = 1; this.render(false); };
      this.bar.querySelectorAll('[data-f]').forEach(sel => { sel.onchange = () => { this.filter[sel.dataset.f] = sel.value; this.page = 1; this.render(false); }; });
    }
    if(this.c) this.c.querySelectorAll('th[data-sort]:not(.no-sort)').forEach(th => {
      th.onclick = () => { const k = th.dataset.sort; if(this.sort === k) this.dir = this.dir === 'asc' ? 'desc' : 'asc'; else { this.sort = k; this.dir = 'desc'; } this.render(false); };
    });
    if(this.c && this.o.rowId) this.c.querySelectorAll('tr[data-row]').forEach(tr => {
      tr.onclick = () => { const k = tr.dataset.row; if(k === '') return; S.expanded[k] = !S.expanded[k]; this.render(false); };
    });
    if(this.pager) this.pager.querySelectorAll('button[data-p]').forEach(b => {
      b.onclick = () => { const p = +b.dataset.p; if(p >= 1) { this.page = p; this.render(false); } };
    });
  }
}
function table(key, containerId, barId, pagerId, opts){
  const existing = S.tables[key];
  if(existing && existing.c) existing.setRows(opts.rows);
  else S.tables[key] = new DataTable($(containerId), barId ? $(barId) : null, pagerId ? $(pagerId) : null, opts);
}

/* ---------------------------------- router -------------------------------- */
function setView(name){
  if(VIEWS.indexOf(name) < 0) name = 'overview';
  S.view = name;
  if(location.hash !== '#/' + name) history.replaceState(null, '', '#/' + name);
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.dataset.view === name));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === name));
  const t = $('page-title'); if(t) t.textContent = TITLES[name] || name;
  renderActive().catch(() => {});
}
async function renderActive(){
  if(!S.authed) return;
  try {
    if(S.view === 'overview') await renderOverview();
    else if(S.view === 'market') await renderMarketPage();
    else if(S.view === 'trading') await renderTrading();
    else if(S.view === 'capital') await renderCapitalPage();
    else if(S.view === 'risk') await renderRiskCenter();
    else if(S.view === 'lab') await renderLab();
    else if(S.view === 'agents') await renderAgents();
    else if(S.view === 'backtest') await renderBacktest();
    else if(S.view === 'memory') await renderMemory();
    else if(S.view === 'logs') await renderLogs();
    else if(S.view === 'settings') await renderSettings();
  } catch(e){ /* each renderer surfaces its own errors */ }
}
function toggleTheme(){ S.theme = S.theme === 'dark' ? 'light' : 'dark'; localStorage.setItem('st_theme', S.theme); applyTheme(); redrawAll(); }
function applyTheme(){ document.documentElement.dataset.theme = S.theme; }
function toggleNav(){ S.collapsed = !S.collapsed; localStorage.setItem('st_nav', S.collapsed ? '1' : '0'); $('app').classList.toggle('collapsed', S.collapsed); redrawAll(); }

/* ----------------------------------- auth --------------------------------- */
function showLogin(show){ $('login').classList.toggle('hidden', !show); }
/* Any 401 brings the login back, not just the first one.  A session can die while the page
   is open - the TTL runs out, the server restarts with a new signing key, or someone
   changed the password in another tab, which signs every other session out - and panels
   reading "unauthorized" with no way to sign in again is not an acceptable answer. */
function needLogin(){ if(BOOT.auth){ S.authed = false; showLogin(true); } }
async function doLogin(){
  const err = $('login-err'); err.textContent = '';
  let r;
  try { r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: $('pw').value})}); }
  catch(e){ err.textContent = 'network error'; return; }
  if(r.ok){ S.authed = true; showLogin(false); $('pw').value = ''; start(); }
  else if(r.status === 429){ err.textContent = 'too many attempts, wait a few minutes'; }
  else { err.textContent = 'wrong password'; }
}
/* --------------------------- the login password ---------------------------
   The one credential in the project.  The page can read *where* the password comes from
   and ask to change it; the server checks the current one, hashes the new one, stores
   only the hash, and signs every other session out.  Nothing here ever sees the hash. */
function pwState(){ return (S.settings && S.settings.password) || null; }
function pwForm(){
  const val = id => (($(id) || {}).value || '');
  const st = pwState() || {};
  return {cur: val('pw-current'), nw: val('pw-new'), rep: val('pw-repeat'), st: st, min: st.min_length || 4};
}
function pwReady(){
  const f = pwForm();
  return f.st.changeable === true && !!f.cur && f.nw.length >= f.min && f.nw === f.rep && f.nw !== f.cur;
}
function pwButtons(){
  const f = pwForm();
  const b = $('pw-change'); if(b) b.disabled = !pwReady();
  mount('pw-hint', !f.cur ? 'The current password is required: a copied session must not be able to change it.'
    : (f.nw.length < f.min ? ('At least ' + f.min + ' characters (' + f.nw.length + ' so far).')
    : (f.nw !== f.rep ? 'The two new entries differ.'
    : (f.nw === f.cur ? 'The new password is the current one.' : ''))));
}
function pwEnter(e){ if(e.key === 'Enter'){ e.preventDefault(); if($('pw-confirm').classList.contains('hidden')) pwAsk(); else pwGo(); } }
function pwMsg(text, cls){ const el = $('pw-status'); if(el){ el.className = cls || 'muted'; el.textContent = text; } }
function pwRender(){
  const st = pwState();
  const fields = ['pw-current', 'pw-new', 'pw-repeat'];
  const lock = !st || st.changeable !== true;
  for(const id of fields){ const el = $(id); if(el) el.disabled = lock; }
  if(!st){ mount('pw-banner', ''); pwMsg('the password state could not be read', 'bad'); pwButtons(); return; }
  let banner = '';
  const note = s => { const t = String(s || ''); return esc(t.charAt(0).toUpperCase() + t.slice(1)); };
  if(st.source === 'environment'){
    banner = '<div class="banner warn"><span class="ico">⚠</span><div>' + note(st.note) + '</div></div>';
    pwMsg('managed by the environment', 'muted');
  } else if(st.source === 'corrupt'){
    banner = '<div class="banner bad"><span class="ico">✕</span><div>' + note(st.note) + '</div></div>';
    pwMsg('corrupt store: the login refuses every password', 'bad');
  } else if(st.is_default){
    banner = '<div class="banner bad"><span class="ico">⚠</span><div><b>The bootstrap password is still in use.</b> Anyone who can reach this dashboard can sign in with <span class="mono">0000</span>. Set your own below — it is written to <span class="mono">' + esc(st.file || '') + '</span> as a salted hash.</div></div>';
    pwMsg('bootstrap password in use', 'warn');
  } else {
    pwMsg('stored' + (st.changed_at ? ' · changed ' + dt(st.changed_at) : ''), 'muted');
  }
  mount('pw-banner', banner);
  pwButtons();
}
function pwAsk(){
  if(!pwReady()) return;
  const f = pwForm();
  mount('pw-msg', '');
  mount('pw-confirm-text', 'replace the dashboard password (now ' + f.cur.length + ' character(s)) with the ' + f.nw.length + '-character one just typed, and sign out every other session');
  $('pw-confirm').classList.remove('hidden');
}
function pwCancel(){ $('pw-confirm').classList.add('hidden'); mount('pw-confirm-text', ''); mount('pw-msg', ''); }
async function pwGo(){
  const f = pwForm();
  if(!pwReady()) return;
  /* The secrets go in the body, over the same origin, and are never put in the URL or kept. */
  const go = $('pw-go'); const label = go ? go.textContent : '';
  if(go){ go.disabled = true; go.textContent = 'changing…'; }
  let r, obj = {};
  try {
    r = await fetch('/api/password', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({current: f.cur, new: f.nw, confirm: true})});
    obj = await r.json().catch(() => ({}));
  } catch(e){ obj = {error: String(e)}; r = {status: 0}; }
  if(go){ go.disabled = false; go.textContent = label || 'confirm'; }
  if(!r.status || r.status >= 400 || obj.ok === false){
    mount('pw-msg', '');
    mount('pw-confirm-text', 'refused: ' + esc(obj.error || ('HTTP ' + r.status)));
    pwButtons();
    return;
  }
  const changed = (obj.password || {}).changed_at;
  pwCancel();
  for(const id of ['pw-current', 'pw-new', 'pw-repeat']){ const el = $(id); if(el) el.value = ''; }
  if(S.settings && obj.password) S.settings.password = obj.password;
  pwRender();
  mount('pw-msg', '✓ password changed' + (changed ? (' at ' + dt(changed)) : '') +
    ' · every other session was signed out, and the new one is stored as a hash — not as text');
}

async function doLogout(){
  const msg = $('ctl-msg');
  if(!BOOT.auth){ msg.className = 'muted'; msg.textContent = 'login is disabled (loopback only): nothing to sign out of'; return; }
  try { await fetch('/api/logout', {method:'POST'}); } catch(e){ msg.className = 'bad'; msg.textContent = 'sign out failed: ' + e; return; }
  S.authed = false;
  ['start','pause','resume','stop'].forEach(a => { const b = $('ctl-' + a); if(b) b.disabled = true; });
  $('session').textContent = 'loop: —';
  msg.className = 'muted'; msg.textContent = 'signed out';
  showLogin(true);
}

/* ----------------------------------- charts ------------------------------- */
function cssv(n){ return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
function pal(){ return {pos:cssv('--pos'), neg:cssv('--neg'), brand:cssv('--brand'), brand2:cssv('--brand-2'),
  ai:cssv('--ai'), ai2:cssv('--ai-2'), tx2:cssv('--tx-2'), tx3:cssv('--tx-3'), bd:cssv('--bd'), surface:cssv('--bg-2'), panel:cssv('--panel')}; }
function fit(canvas){
  const w = Math.max(80, canvas.clientWidth || canvas.parentElement.clientWidth || 480);
  const h = Math.max(60, canvas.clientHeight || 160);
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
  ctx.font = '10px ' + cssv('--mono'); ctx.textBaseline = 'middle';
  return {ctx, W:w, H:h};
}
function note(ctx, W, H, text){
  ctx.fillStyle = cssv('--tx-3'); ctx.font = '11px ' + cssv('--sans');
  ctx.fillText(text, 12, H/2);
}
function drawEquity(canvas, points, opts){
  opts = opts || {};
  const {ctx, W, H} = fit(canvas);
  const p = pal();
  if(!points || points.length < 2){ note(ctx, W, H, 'no equity points recorded'); return; }
  const vals = points.map(x => +x.equity);
  const lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  const span = (hi - lo) || Math.abs(hi) || 1;
  const L = 4, R = 70, T = 10, B = 18;
  const x = i => L + (i / (vals.length - 1)) * (W - L - R);
  const y = v => T + (hi - v) / span * (H - T - B);
  ctx.strokeStyle = p.bd; ctx.lineWidth = 1;
  for(let g = 0; g <= 4; g++){
    const yy = T + g * (H - T - B) / 4;
    ctx.beginPath(); ctx.moveTo(L, yy); ctx.lineTo(W - R, yy); ctx.stroke();
    ctx.fillStyle = p.tx3; ctx.fillText(fmt(hi - g * span / 4, hi > 100 ? 0 : 2), W - R + 6, yy);
  }
  const base = opts.baseline;
  if(isNum(base)){
    const by = y(base);
    if(by > T && by < H - B){ ctx.strokeStyle = p.tx3; ctx.setLineDash([4, 3]); ctx.beginPath(); ctx.moveTo(L, by); ctx.lineTo(W - R, by); ctx.stroke(); ctx.setLineDash([]); }
  }
  const grad = ctx.createLinearGradient(0, T, 0, H - B);
  grad.addColorStop(0, 'rgba(59,130,246,0.22)'); grad.addColorStop(1, 'rgba(59,130,246,0)');
  ctx.beginPath(); ctx.moveTo(x(0), y(vals[0]));
  for(let i = 1; i < vals.length; i++) ctx.lineTo(x(i), y(vals[i]));
  ctx.lineTo(x(vals.length - 1), H - B); ctx.lineTo(x(0), H - B); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  const up = vals[vals.length - 1] >= vals[0];
  ctx.strokeStyle = up ? p.pos : p.neg; ctx.lineWidth = 1.6; ctx.beginPath();
  vals.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))); ctx.stroke();
  const ly = y(vals[vals.length - 1]);
  ctx.fillStyle = up ? p.pos : p.neg; ctx.beginPath(); ctx.arc(x(vals.length - 1), ly, 2.6, 0, 6.284); ctx.fill();
  if(opts.xlabels && opts.xlabels.length){
    ctx.fillStyle = p.tx3; ctx.font = '9px ' + cssv('--mono');
    const n = Math.min(4, opts.xlabels.length);
    for(let i = 0; i < n; i++){ const idx = Math.round(i * (vals.length - 1) / Math.max(1, n - 1)); ctx.fillText(opts.xlabels[idx], Math.min(x(idx), W - R - 56), H - 8); }
  }
}
function drawUnderwater(canvas, points){
  const {ctx, W, H} = fit(canvas);
  const p = pal();
  if(!points || points.length < 2){ note(ctx, W, H, 'no equity points recorded'); return; }
  let peak = -Infinity; const dd = points.map(x => { peak = Math.max(peak, +x.equity); return peak > 0 ? (1 - (+x.equity) / peak) * 100 : 0; });
  const maxdd = Math.max.apply(null, dd) || 1;
  const L = 4, R = 52, T = 12, B = 16;
  const x = i => L + (i / (dd.length - 1)) * (W - L - R);
  const y = v => T + (v / maxdd) * (H - T - B);
  ctx.strokeStyle = p.bd;
  for(let g = 0; g <= 3; g++){ const yy = T + g * (H - T - B) / 3; ctx.beginPath(); ctx.moveTo(L, yy); ctx.lineTo(W - R, yy); ctx.stroke(); ctx.fillStyle = p.tx3; ctx.fillText('-' + (maxdd * g / 3).toFixed(1) + '%', W - R + 6, yy); }
  ctx.beginPath(); ctx.moveTo(x(0), T); for(let i = 0; i < dd.length; i++) ctx.lineTo(x(i), y(dd[i]));
  ctx.lineTo(x(dd.length - 1), T); ctx.closePath();
  ctx.fillStyle = 'rgba(248,113,113,0.18)'; ctx.fill();
  ctx.strokeStyle = p.neg; ctx.lineWidth = 1.4; ctx.beginPath();
  dd.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))); ctx.stroke();
}
function drawBars(canvas, items, opts){
  opts = opts || {};
  const {ctx, W, H} = fit(canvas);
  const p = pal();
  if(!items || !items.length){ note(ctx, W, H, 'nothing to plot'); return; }
  const vals = items.map(i => +i.value || 0);
  const mx = Math.max(1e-9, Math.max.apply(null, vals.map(Math.abs)));
  const L = 4, R = 46, T = 12, B = 18, n = items.length;
  const bw = (W - L - R) / n, zero = opts.zeroLine ? T + (H - T - B) / 2 : T + (H - T - B);
  ctx.strokeStyle = p.bd; ctx.beginPath(); ctx.moveTo(L, zero); ctx.lineTo(W - R, zero); ctx.stroke();
  items.forEach((it, i) => {
    const v = +it.value || 0;
    const h = Math.abs(v) / mx * (opts.zeroLine ? (H - T - B) / 2 : (H - T - B)) * 0.9;
    const x = L + i * bw + bw * 0.2, w = Math.max(2, bw * 0.6);
    ctx.fillStyle = (opts.color ? opts.color(v) : (v >= 0 ? p.pos : p.neg));
    ctx.fillRect(x, v >= 0 ? zero - h : zero, w, Math.max(1, h));
    if(opts.label){ ctx.fillStyle = p.tx3; ctx.font = '9.5px ' + cssv('--mono'); const lbl = it.label;
      const tw = ctx.measureText(lbl).width; ctx.fillText(lbl, x + w / 2 - tw / 2, H - 8); }
  });
  ctx.fillStyle = p.tx3; ctx.fillText(opts.yfmt ? opts.yfmt(mx) : mx.toFixed(3), W - R + 6, T + 4);
}
function drawSpark(canvas, values, color){
  const {ctx, W, H} = fit(canvas);
  if(!values || values.length < 2) return;
  const lo = Math.min.apply(null, values), hi = Math.max.apply(null, values), span = (hi - lo) || 1;
  ctx.strokeStyle = color || cssv('--brand'); ctx.lineWidth = 1.4; ctx.beginPath();
  values.forEach((v, i) => { const xx = (i / (values.length - 1)) * W, yy = H - 4 - (v - lo) / span * (H - 8); i ? ctx.lineTo(xx, yy) : ctx.moveTo(xx, yy); });
  ctx.stroke();
}
function drawCandles(canvas, c){
  const {ctx, W, H} = fit(canvas);
  const p = pal();
  if(!c || c.length < 2){ note(ctx, W, H, 'waiting for candles…'); return; }
  const PAD = 8, AXIS = 66, volH = Math.round(H * 0.16), priceH = H - volH - 14;
  const plotW = W - AXIS;
  let hi = -Infinity, lo = Infinity, vmax = 0;
  for(const k of c){ hi = Math.max(hi, k.h); lo = Math.min(lo, k.l); vmax = Math.max(vmax, k.v); }
  const span = (hi - lo) || 1;
  const y = v => PAD + (hi - v) / span * (priceH - PAD);
  const bw = Math.max(1, plotW / c.length);
  const x = i => i * bw + bw / 2;
  ctx.strokeStyle = p.bd; ctx.fillStyle = p.tx3; ctx.lineWidth = 1;
  for(let g = 0; g <= 4; g++){
    const yy = PAD + g * (priceH - PAD) / 4;
    ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(plotW, yy); ctx.stroke();
    ctx.fillText(fmt(hi - g * span / 4, hi >= 100 ? 2 : 5), plotW + 6, yy);
  }
  const start = Math.max(0, c.length - Math.floor(plotW / bw));
  for(let i = start; i < c.length; i++){
    const k = c[i], up = k.c >= k.o, col = up ? p.pos : p.neg;
    const cx = x(i - start);
    ctx.strokeStyle = col; ctx.fillStyle = col;
    ctx.beginPath(); ctx.moveTo(cx, y(k.h)); ctx.lineTo(cx, y(k.l)); ctx.stroke();
    const yo = y(k.o), yc = y(k.c), top = Math.min(yo, yc), hgt = Math.max(1, Math.abs(yc - yo));
    ctx.fillRect(cx - Math.max(1, bw * 0.32), top, Math.max(1, bw * 0.64), hgt);
    const vh = vmax ? k.v / vmax * (volH - 4) : 0;
    ctx.globalAlpha = 0.45;
    ctx.fillRect(cx - Math.max(1, bw * 0.32), H - vh, Math.max(1, bw * 0.64), vh);
    ctx.globalAlpha = 1;
  }
  const last = c[c.length - 1].c;
  ctx.strokeStyle = p.brand2; ctx.setLineDash([4, 3]);
  const ly = y(last);
  ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(plotW, ly); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = p.brand2; ctx.fillRect(plotW, ly - 8, AXIS, 16);
  ctx.fillStyle = cssv('--bg'); ctx.fillText(fmt(last, last >= 100 ? 2 : 5), plotW + 6, ly);
}
function redrawAll(){ setTimeout(() => { if(S.marketData) drawMarketCharts(); }, 30); }

/* ------------------------------------ data -------------------------------- */
async function api(path){
  const r = await fetch(path, {headers:{'Accept':'application/json'}});
  if(r.status === 401){ needLogin(); throw new Error('unauthorized'); }
  if(!r.ok){
    /* The backend explains itself (a store it cannot read, a refused action): show that
       sentence instead of a bare status code, which is all a failed fetch would leave. */
    const body = await r.json().catch(() => null);
    throw new Error((body && (body.detail || body.error)) || ('HTTP ' + r.status));
  }
  return await r.json();
}
const j = api; /* kept for the panels below and for anything reading the old name */
function withTimeout(p, ms){ return Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms || 8000))]); }
async function safe(p){ try { return await p; } catch(e){ return null; } }

/* ----------------------------------- boot ---------------------------------- */
async function boot(){
  applyTheme();
  // The sidebar can collapse to icons, which hides the visible label; carry it onto the
  // button as an accessible name so a collapsed rail is still announced correctly.
  document.querySelectorAll('.nav-item').forEach(n => {
    const l = n.querySelector('.lbl');
    if(l && !n.getAttribute('aria-label')) n.setAttribute('aria-label', l.textContent.trim());
  });
  $('app').classList.toggle('collapsed', S.collapsed);
  const hash = (location.hash || '').replace('#/', '');
  if(VIEWS.indexOf(hash) >= 0) S.view = hash;
  let who;
  try { who = await (await fetch('/api/whoami')).json(); }
  catch(e){ $('health').textContent = 'server unreachable'; return; }
  syncMarketInputs();
  const sv = $('side-ver'); if(sv) sv.textContent = 'v' + (who.version || '?');
  if(who.auth_enabled && !who.authenticated){ showLogin(true); return; }
  S.authed = true;
  setView(S.view);
  start();
}
let started = false;
function start(){
  if(started) return; started = true;
  refresh().catch(() => {});
  setInterval(() => refresh().catch(() => {}), REFRESH);
  refreshKey(false).catch(() => {});
  scheduleKey();
  scheduleMarket();
}
function scheduleMarket(){
  setTimeout(async () => { try { await refreshMarket(); } catch(e){} scheduleMarket(); }, MK_REFRESH);
}
function scheduleKey(){
  setTimeout(async () => { try { await refreshKey(false); } catch(e){} scheduleKey(); }, KEY_REFRESH);
}

/* The one refresh tick: the safety state first (a stale stop is worse than none),
   then the top-bar status, then whichever view is on screen. */
async function refresh(){
  await refreshSwitch();   /* stop state first: it must never be stale */
  await refreshRisk();     /* what is left of the limits that would engage it */
  await refreshAlerts();   /* what the channel has actually sent about it */
  await refreshStatus();
  await renderActive();
}
async function loopStatus(){ return await safe(api('/api/session')); }
/* The pid decides, not the file: a state file that still says "running" with no process
   behind it is reported as not running (and called out as stale) - never as a run that
   is actually happening. */
function loopLabel(se){
  const l = (se && se.loop) || {};
  if(l.running) return 'running';
  if(se && se.status === 'running') return 'idle (stale state file)';
  return (se && se.status) || 'idle';
}
async function refreshStatus(){
  const [h, se] = await Promise.all([safe(api('/api/health')), loopStatus()]);
  const health = $('health');
  if(h){
    health.textContent = 'v' + h.version + ' · db ' + (h.db_ok ? 'ok' : 'error');
    health.dataset.tip = 'The running version, and the state database: ok means this page can read and write it, error means the panels will fall back to N/D.';
  }
  const l = (se && se.loop) || {};
  const done = l.generations_done != null ? l.generations_done : 0;
  const tot = l.generations_total || 0;
  // The pid decides, not the file: a state file that still says "running" with no
  // process behind it is reported as not running (and called out as stale), never as a
  // run that is actually happening.
  const sess = $('session');
  sess.textContent = 'loop: ' + loopLabel(se) +
    ' ' + done + '/' + tot + (l.pid ? ' · pid ' + l.pid : '') + (l.pending ? ' · ' + l.pending + ' pending' : '');
  sess.dataset.tip = 'The tracked research loop: its state, how many generations of the requested total are done, the pid when this dashboard started it, and any request waiting for the next boundary. The pid decides the state — a file left saying "running" with no process behind it is reported as stopped.';
  applyButtons(l);
  renderStatusbar(se);
}
function renderStatusbar(se){
  const bits = [];
  const l = (se && se.loop) || {};
  bits.push(chip(l.running ? 'loop running' : 'loop idle', l.running ? 'ok' : 'muted', l.running,
    'The tracked research loop: running means a live process is evaluating genomes, idle means none is. It is the only autonomous process here.'));
  bits.push(chip('mode paper', 'info', false,
    'Every fill is simulated and passes the sealed pre-trade risk gate. There is no live order rail in this build — the Binance panel is a read-only probe.'));
  const ks = S.ks || {};
  bits.push(chip(ks.blocked ? 'stop ENGAGED' : 'stop clear', ks.blocked ? 'bad' : 'ok', !!ks.blocked,
    'The emergency stop (kill switch). ENGAGED means it is refusing every order on every rail until someone disarms it; clear means orders are considered normally. It can only refuse — it never places one.'));
  const risk = S.risk || {};
  if(!risk.ok) bits.push(chip('limits unmeasured', 'muted', false,
    'The risk panel could not measure the account yet (no active paper session, or the database read failed), so no claim is made about the sealed limits.'));
  else if(risk.would_arm) bits.push(chip('limits BREACHED', 'bad', false,
    'At least one halting limit is past its cap: drawdown, today\u2019s P&L or exposure. The gate refuses new orders while this is true.'));
  else bits.push(chip('limits within caps', 'ok', false,
    'Drawdown, today\u2019s P&L and exposure are all inside the sealed caps. The Risk limits panel shows the headroom left on each.'));
  const key = S.key || {};
  if(key.verdict) bits.push(chip('binance ' + key.verdict, key.ok ? 'ok' : (key.verdict === 'not checked' ? 'muted' : 'warn'), false,
    'The last read-only probe of the Binance Testnet key: permissions, signature and clock offset. not checked means the probe has not run (or the rail is switched off).'));
  const al = S.alerts || {};
  const owed = ((risk.pending) || []).length;
  bits.push(chip('alerts ' + ((al.channel && al.channel.reason === 'notify.disabled') ? 'off' : ((al.channel && al.channel.ready) ? 'ready' : 'unknown')) + (owed ? ' · ' + owed + ' owed' : ''), owed ? 'warn' : 'muted', false,
    'The optional risk alert channel (Telegram), which is off unless configured. owed counts alerts that are due and not yet delivered — a halt nobody has been told about is the row worth acting on.'));
  mount('statusbar', bits.join(''));
}

/* ------------------------------ loop controls ----------------------------- */
/* One control path for both places the buttons exist (the top bar and the Agent
   Center): the same POST, the same refusal, the same message. */
async function control(action){
  const msg = $('ctl-msg');
  const ids = ['start','pause','resume','stop'].flatMap(a => ['ctl-' + a, 'ag-' + a]);
  ids.forEach(id => { const b = $(id); if(b) b.disabled = true; });
  const say = (cls, text) => {
    if(msg){ msg.className = cls; msg.textContent = text; }
    const am = $('ag-ctl-msg');
    if(am){ am.className = cls; am.textContent = text; }
  };
  say('muted', action + '…');
  let r, body = {};
  try {
    r = await fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action})});
    body = await r.json().catch(() => ({}));
  } catch(e){
    say('bad', action + ' failed: ' + e + ' (the dashboard is still polling: the state above is unchanged)');
    ids.forEach(id => { const b = $(id); if(b) b.disabled = false; });
    return;
  }
  if(r.ok){ say('ok', action + ': ' + (body.note || (body.command ? 'started (' + body.command + ')' : 'ok'))); }
  else { say('bad', action + ' refused: ' + (body.error || ('HTTP ' + r.status))); }
  try { await refresh(); } catch(e){ ids.forEach(id => { const b = $(id); if(b) b.disabled = false; }); }
}
function applyButtons(l){
  const on = !!(l && l.running);
  const pending = (l && l.pending) || null;
  const paused = (l && l.status) === 'paused' || pending === 'pause';
  const set = (a, disabled) => {
    ['ctl-', 'ag-'].forEach(p => { const b = $(p + a); if(b) b.disabled = !!disabled; });
  };
  set('start', on);
  set('pause', !on || paused || pending === 'stop');
  set('resume', !on || !paused || pending === 'stop');
  set('stop', !on || pending === 'stop');
}

/* -------------------------------- live market ----------------------------- */
/* The Overview toolbar and the Trading card drive the same selection: one symbol and
   one interval feed both charts, so a change in either place arrives in the other. */
function syncMarketInputs(){
  ['mk-symbol', 'ov-symbol'].forEach(id => { const el = $(id); if(el) el.value = S.market.symbol; });
  ['mk-interval', 'ov-interval'].forEach(id => { const el = $(id); if(el) el.value = S.market.interval; });
}
/* The button that was pressed supplies its own fields (`applyMarket('ov')` / `('mk')`):
   the two toolbars share one selection, but neither may silently override the other's
   inputs - pressing apply in the Overview must not read the Trading card's dropdown. */
function applyMarket(from){
  const p = (from === 'mk' || from === 'ov') ? from
    : (S.view === 'trading' || S.view === 'market' ? 'mk' : 'ov');
  const sym = $(p + '-symbol');
  const ivl = $(p + '-interval');
  S.market.symbol = ((sym && sym.value) || '').trim() || S.market.symbol || 'BTC/USDT';
  S.market.interval = (ivl && ivl.value) || S.market.interval;
  syncMarketInputs();
  refreshMarket().catch(() => {});
}
/* A manual refresh of the whole page (the safety state first, like the tick). */
async function refreshOverview(explicit){
  const st = $('ov-updated');
  if(st && explicit) st.textContent = 'refreshing…';
  try { await refresh(); } catch(e){}
  const now = new Date();
  if($('ov-updated')) $('ov-updated').textContent = 'updated ' + now.toLocaleTimeString();
}
async function refreshMarket(){
  const url = '/api/market?symbol=' + encodeURIComponent(S.market.symbol) + '&interval=' + encodeURIComponent(S.market.interval) + '&bars=' + S.market.bars;
  let m; try { m = await j(url); } catch(e){ return; }
  S.marketData = m;
  const st = $('mk-status');
  if(st){
    if(!m.ok){ st.className = 'bad'; st.textContent = 'Binance error'; }
    else { st.className = 'ok'; st.textContent = 'live'; }
  }
  const t = m.ticker || {};
  const up = (t.change_pct || 0) >= 0;
  renderMarketStats(m);
  if($('mk-price')) $('mk-price').innerHTML = m.ok
    ? fmt(t.last, t.last >= 100 ? 2 : 5) + ' <small class="' + (up ? 'ok' : 'bad') + '">' + (up ? '+' : '') + ((t.change_pct || 0) * 100).toFixed(2) + '% 24h</small>'
    : '<span class="muted" style="font-size:15px">' + esc(m.error || 'no data') + '</span>';
  const meta = m.ok ? (m.symbol + ' ' + m.interval + ' · ' + (m.candles || []).length + ' candles · ' + m.latency_ms + ' ms · ' + m.fetched_at + (m.cached ? ' · cached' : '')) : (m.error || '');
  if($('mk-meta')) $('mk-meta').textContent = meta;
  if($('ov-mk-meta')) $('ov-mk-meta').textContent = m.ok ? (m.symbol + ' ' + m.interval + ' · ' + (m.candles || []).length + ' candles') : 'unavailable';
  if(m.ok) syncMarketInputs();
  drawMarketCharts();
}
/* The 24h strip under the price. It exists on the Market page only; the Overview keeps the
   compact price line, so everything here is guarded by the element being present. The
   figures are the feed's own: a missing ticker says so instead of showing zeroes. */
function renderMarketStats(m){
  const el = $('mk-stats'); if(!el) return;
  if(!m.ok){ el.innerHTML = '<span class="muted">24h stats unavailable: ' + esc(m.error || 'no data') + '</span>'; return; }
  const t = m.ticker || {};
  const base = String(m.symbol || '').split('/')[0] || 'base';
  const dp = (v) => (isNum(v) && Math.abs(v) >= 100) ? 2 : 5;
  const cells = [
    ['24h high', fmt(t.high, dp(t.high)), t.high],
    ['24h low', fmt(t.low, dp(t.low)), t.low],
    ['24h volume', compactNum(t.volume) + ' ' + esc(base), t.volume],
    ['24h quote volume', compactNum(t.quote_volume) + ' USDT', t.quote_volume],
    ['24h trades', fmt(t.trades, 0), t.trades],
  ];
  el.innerHTML = cells.map(([k, v, raw]) =>
    '<span title="' + esc(String(raw)) + '">' + k + '<b>' + v + '</b></span>').join('');
}
/* A display convenience for the two figures here that run into the billions. It rounds to
   three significant parts and puts the exact value in the cell's title, so a reader can
   always see the number the feed sent. */
function compactNum(v){
  if(!isNum(v)) return 'N/D';
  const a = Math.abs(v);
  if(a >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if(a >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if(a >= 1e3) return (v / 1e3).toFixed(1) + 'K';
  return fmt(v, a >= 10 ? 0 : 2);
}
function drawMarketCharts(){
  const m = S.marketData; if(!m) return;
  if($('chart')) drawCandles($('chart'), m.candles);
  if($('ov-market')) drawCandles($('ov-market'), m.candles);
}

/* ======================= emergency stop (kill switch) ===================== */
/* The server reads and writes the very file `switch arm|reset` uses, so this stops
   new orders on every rail at once and survives a restart.  It can only *refuse*
   orders - arming places nothing - which is why a page that cannot trade may offer it. */
let ksPending = null;
function ksAsk(action){
  ksPending = action;
  const arming = action === 'arm';
  $('ks-confirm-text').textContent = arming
    ? 'This blocks every new order (paper and Binance) until you disarm. Say why, then confirm:'
    : 'This lets new orders through again. Say why, then confirm:';
  $('ks-reason').value = '';
  ksMsg('', '');
  $('ks-confirm').classList.remove('hidden');
  $('ks-reason').focus();
}
function ksCancel(){ ksPending = null; $('ks-confirm').classList.add('hidden'); }
async function ksGo(){
  if(!ksPending) return;
  const action = ksPending;
  const reason = $('ks-reason').value;
  ksMsg('', '');
  const go = $('ks-go'); go.disabled = true;
  let obj;
  try {
    const r = await fetch('/api/killswitch', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action, reason, confirm: true})});
    obj = await r.json();
  } catch(e){ obj = {ok:false, error:'network error'}; }
  go.disabled = false;
  ksCancel();
  if(obj && obj.ok) ksRender(obj);
  refreshSwitch().catch(() => {});
  if(obj && obj.ok){
    const done = obj.action === 'arm' ? 'armed' : 'disarmed';
    if(obj.event_error) ksMsg(done + ' — but NOT recorded in risk_events: ' + obj.event_error, 'warn');
    else ksMsg(done + ' — recorded as risk event #' + obj.event_id, 'ok');
  } else {
    ksMsg('action not confirmed: ' + ((obj && obj.error) || 'the request failed'), 'bad');
  }
}
function ksMsg(text, cls){ const el = $('ks-msg'); el.className = cls || ''; el.textContent = text; }
async function refreshSwitch(){ let k; try { k = await j('/api/killswitch'); } catch(e){ return; } ksRender(k); }
function ksRender(k){
  S.ks = k;
  const blocked = !!k.blocked;
  const st = $('ks-status'); if(!st) return;
  st.className = blocked ? 'bad' : 'ok';
  st.textContent = blocked ? 'ENGAGED — new orders are refused' : 'clear — orders allowed';
  st.dataset.tip = blocked
    ? 'The kill switch is armed: every rail that could send an order is refusing, and it stays that way until someone disarms it with a reason. Arming never places an order.'
    : 'The kill switch is clear: orders may be considered (and are still checked by the pre-trade gate). Arming it is the one write action this page offers.';
  $('ks-arm').disabled = blocked;
  $('ks-reset').disabled = !blocked;
  const meta = ['by <b>' + esc(k.triggered_by || '—') + '</b>'];
  if(k.triggered_at) meta.push(esc(dt(k.triggered_at)));
  meta.push(esc(k.path || ''));
  meta.push((k.events || 0) + ' event(s) in the file');
  if(k.checked_at) meta.push(esc(k.checked_at));
  $('ks-meta').innerHTML = meta.join(' · ');
  $('ks-meta').dataset.tip = 'Who armed or disarmed the switch, when, where the file that holds it lives, and how many events its audit trail has. The file is the state: it survives restarts, which is the point.';

  const rows = [];
  const row = (left, right, cls, tip) => rows.push('<tr><td class="' + (cls || 'muted') + '"' + (tip ? ' data-tip="' + esc(tip) + '"' : '') + '>' + left + '</td><td colspan="2"' + (tip ? ' data-tip="' + esc(tip) + '"' : '') + '>' + right + '</td></tr>');
  row('blocked', '<b>' + (blocked ? 'yes' : 'no') + '</b>', blocked ? 'bad' : 'ok',
    'Whether the switch is currently refusing orders. It is read from the file on every check, so a restart does not clear it.');
  if(k.reason) row('reason', esc(k.reason), blocked ? 'warn' : 'muted',
    'The reason recorded when the switch was last armed. It is operator-entered: the system never arms itself and never signs its own decision.');
  for(const c of ((k.detail || {}).checks || [])){
    const unit = c.unit === 'x' ? 'x' : '%';
    const roomUnit = c.unit === 'x' ? 'x' : 'pp';
    const num = v => (isFinite(v) ? (+v).toFixed(2) : '?');
    const left = c.ok ? (num(c.headroom) + roomUnit + ' of margin left') : (num(Math.abs(+(c.headroom || 0))) + roomUnit + ' past it');
    row('at arming', esc(c.name || '?') + ' <b>' + num(c.value) + unit + '</b> of the ' + num(c.limit) + unit + ' cap — ' + esc(left), c.ok ? 'muted' : 'bad',
      'The limit as it stood at the moment the switch was armed, kept so the decision can be judged later: ' + esc(c.name || '?') + ' was at ' + num(c.value) + unit + ' against a ' + num(c.limit) + unit + ' cap.');
  }
  for(const h of (k.history || []))
    row(esc(h.event || '?'), esc(dt(h.at || '')) + ' <span class="muted">by ' + esc(h.by || '—') + '</span> · ' + esc(h.reason || ''), h.event === 'reset' ? 'ok' : 'warn',
      'One line of the switch\u2019s audit trail: what happened, when, who did it and why. The trail is append-only \u2014 disarming adds a row, it never erases one.');
  row('note', 'arming can only refuse orders — it never places one', 'muted',
    'The guarantee that makes this button safe to press in a hurry: its failure mode is refusing to trade, not trading.');
  $('ks-body').innerHTML = '<table class="tbl">' + rows.join('') + '</table>';
}

/* =============================== risk limits ============================== */
async function refreshRisk(){ let r; try { r = await j('/api/risk'); } catch(e){ return; } riskRender(r); }
function alertChip(a, pending){
  const owed = (pending || []).length;
  const tail = owed ? ' <span class="warn">— ' + owed + ' waiting to be sent</span>' : '';
  if(!a) return '<span class="muted">alerts: unknown</span>' + tail;
  if(a.ready) return '<span class="ok">alerts: telegram ready</span>' + tail;
  if(a.reason === 'notify.disabled') return '<span class="muted">alerts: off (notify.disabled)</span>' + tail;
  return '<span class="bad">alerts: ENABLED but ' + esc(a.reason) + '</span>' + tail;
}
function riskRender(r){
  S.risk = r;
  const st = $('risk-status'); if(!st) return;
  const head = $('risk-headroom'), alerts = $('risk-alerts'), body = $('risk-body'), meta = $('risk-meta');
  alerts.innerHTML = alertChip(r && r.alerts, r && r.pending);
  if(!r || !r.ok){
    meta.innerHTML = 'sealed digest ' + esc(((r && r.digest) || '').slice(0, 12)) + '… pinned <b>' + ((r && r.pinned) ? 'yes' : 'NO') + '</b>';
    st.className = 'muted'; st.textContent = 'not measurable';
    head.innerHTML = '<span class="warn">' + esc((r && r.error) || 'no measurement') + '</span>' + ((r && r.hint) ? ' <span class="muted">— ' + esc(r.hint) + '</span>' : '');
    body.innerHTML = '<span class="muted">nothing is shown rather than four zeros: a limits panel that cannot see an account must not look like good news</span>';
    return;
  }
  const checks = r.checks || [], nearest = r.nearest || null, breached = r.breached || [], tight = (r.tight_frac === undefined ? 0.25 : r.tight_frac);
  const room = v => (Math.abs(v) < 0.005 ? 0 : v);
  if(r.would_arm){ st.className = 'bad'; st.textContent = 'BREACHED — the next order arms the emergency stop'; }
  else if(breached.length){ st.className = 'bad'; st.textContent = 'breached: ' + breached.join(', ') + ' — orders that break it are refused'; }
  else if(nearest && nearest.headroom_frac <= tight){ st.className = 'warn'; st.textContent = 'tight — ' + nearest.name + ' is nearest'; }
  else { st.className = 'ok'; st.textContent = 'within the sealed limits'; }
  st.dataset.tip = 'The verdict on the sealed caps. tight means the closest cap still has less than ' + Math.round(tight * 100) + '% of its room left; breached means one is past its cap and orders that would break it are refused; BREACHED means the next order arms the emergency stop.';
  const s = r.session || {}, a = r.account || {};
  meta.innerHTML = [s.session_id ? 'paper session #' + s.session_id : 'no session', s.as_of ? 'as of ' + esc(s.as_of) : '',
    'sealed digest ' + esc(String(r.digest || '')).slice(0, 12) + '… pinned <b class="' + (r.pinned ? 'ok' : 'bad') + '">' + (r.pinned ? 'yes' : 'NO') + '</b>'].filter(Boolean).join(' · ');
  head.innerHTML = (r.would_arm
    ? 'breached now: <b>' + esc(breached.join(', ')) + '</b> — the next order is refused and arms the stop until you disarm it'
    : (nearest ? 'room before the gate arms the stop: nearest is <b>' + esc(nearest.name) + '</b> with <b>' + fmt(room(nearest.headroom), 2) + ' ' + esc(nearest.unit === 'x' ? 'x' : 'pp') + '</b> left' : 'no account limits to report')) +
    ' <span class="muted">· equity ' + fmt(a.equity, 2) + ' · cash ' + fmt(a.cash, 2) + ' · exposure ' + fmt(a.positions_value, 2) + ' · drawdown ' + fmt(a.drawdown_pct, 2) + '% · today <span class="' + ((a.day_pnl_pct || 0) >= 0 ? 'ok' : 'bad') + '">' + fmt(a.day_pnl_pct, 2) + '%</span> · ' + fmt(a.open_positions, 0) + ' open position(s)</span>';
  head.dataset.tip = 'What the gate is watching right now: equity, cash, exposure, drawdown and today\u2019s P&L on the active paper session. It hands the account to the gate\u2019s own limit functions, so this panel cannot show a limit the gate enforces differently.';
  alerts.dataset.tip = 'Whether the alert channel is configured, and what it still owes you.';
  const hcell = (label, tip) => '<th class="no-sort" data-tip="' + esc(tip) + '">' + label + '</th>';
  const row = ['<tr>' +
    hcell('limit', 'Which sealed cap this row is: the largest single position, the hard stop per trade, the daily loss, the drawdown from peak, or total exposure.') +
    hcell('now', 'Where the account stands on this limit at this moment, measured on the paper rail.') +
    hcell('cap', 'The sealed ceiling itself, and where it comes from. It is pinned by SHA-256 and no config key or genome can move it.') +
    hcell('room', 'How much is left before this limit trips. A negative number means it is already past its cap.') +
    hcell('if breached', 'What happens when it is passed: a refusal (the order is rejected) or a halt (trading stops and the emergency stop is armed).') +
    '</tr>'];
  for(const c of checks){
    const unit = c.unit === 'x' ? 'x' : '%', unitRoom = c.unit === 'x' ? 'x' : 'pp';
    const cls = c.ok ? (c.headroom_frac <= tight ? 'warn' : 'ok') : 'bad';
    const nameTip = esc(c.name) + (c.arms ? ' is a halting limit: passing it stops trading' : ' is a refusing limit: passing it rejects the order that breaks it') +
      ' (cap source: ' + esc(c.source || 'sealed') + ')';
    row.push('<tr><td class="' + cls + '" data-tip="' + nameTip + '">' + esc(c.name) + (c.arms ? ' <span class="muted">(halts)</span>' : '') + '</td>' +
      '<td class="' + (c.ok ? '' : 'bad') + ' num" data-tip="Current value on the active paper session: ' + fmt(c.value, 2) + unit + '.">' + fmt(c.value, 2) + unit + '</td>' +
      '<td class="num" data-tip="The sealed cap for ' + esc(c.name) + ': ' + fmt(c.limit, 2) + unit + ', from ' + esc(c.source || 'the sealed layer') + '.">' + fmt(c.limit, 2) + unit + ' <span class="muted">' + esc(c.source) + '</span></td>' +
      '<td class="' + cls + ' num" data-tip="Headroom left before it trips: ' + fmt(room(c.headroom), 2) + unitRoom + (c.ok ? '.' : ' \u2014 already past the cap.') + '">' + (c.ok ? '' : 'BREACHED: ') + fmt(room(c.headroom), 2) + unitRoom + '</td>' +
      '<td class="muted" data-tip="' + esc(c.effect) + '">' + esc(c.effect) + '</td></tr>');
  }
  body.innerHTML = checks.length
    ? '<table class="tbl">' + row.join('') + '</table><div class="card-sub">measured on the paper rail and read with it every refresh; the Binance rail is measured by the key panel, which sends no orders. The caps are sealed: neither the config nor the model can widen them.' + (r.would_arm ? ' Disarm the stop from the panel above once the cause is gone.' : '') + '</div>'
    : '<span class="muted">no limits to report</span>';
}

/* ================================== alerts ================================ */
async function refreshAlerts(){ let a; try { a = await j('/api/alerts'); } catch(e){ return; } alRender(a); }
function alRender(a){
  S.alerts = a;
  const st = $('al-status'); if(!st) return;
  const meta = $('al-meta'), body = $('al-body'), ch = (a && a.channel) || {};
  if(ch.ready){ st.className = 'ok'; st.textContent = 'channel ready'; }
  else if(ch.reason === 'notify.disabled'){ st.className = 'muted'; st.textContent = 'channel off (notify.disabled)'; }
  else { st.className = 'bad'; st.textContent = 'channel ' + esc(ch.reason || 'unknown'); }
  st.dataset.tip = 'Whether the optional alert channel is configured: ready means it can deliver, off means notify.enabled is false (or its credentials are unset), anything else is a reason it could not.';
  const items = (a && a.items) || [];
  meta.innerHTML = items.length + ' sent' + (a && a.limit ? ' <span class="muted">(kept up to ' + a.limit + ')</span>' : '') + ' · <span class="muted mono">' + esc((a && a.state_file) || '') + '</span>';
  if(!items.length){
    body.innerHTML = '<span class="muted">nothing has been sent yet: this list is what actually left the machine, so a decision that was never delivered does not appear here</span>';
    return;
  }
  const rows = items.map(it => {
    const halt = it.kind === 'halt';
    const kind = halt ? (it.automatic ? 'HALT (automatic)' : 'HALT (by you)') : 'warning';
    const why = it.band ? (esc(it.reason) + ' <span class="muted">[' + esc(it.band) + ']</span>') : esc(it.reason);
    return '<tr><td class="muted mono">' + esc(dt(it.at || '')) + '</td><td class="' + (halt ? 'bad' : 'warn') + '">' + kind + '</td><td>' + why + '</td><td class="muted">' + esc(it.headline || '') + '</td></tr>';
  }).join('');
  body.innerHTML = '<table class="tbl"><thead><tr>' +
    '<th class="no-sort" data-tip="When the message left this machine, from the channel\u2019s own delivery ledger.">when</th>' +
    '<th class="no-sort" data-tip="A HALT is the kill switch being armed \u2014 marked automatic when the program armed it itself; a warning is the notice sent before a halting limit is reached.">kind</th>' +
    '<th class="no-sort" data-tip="Which limit triggered it and which band it was in (close to, or already past, its cap).">why</th>' +
    '<th class="no-sort" data-tip="The first line of what was actually sent, so the message can be recognised without opening the channel.">what was said</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>';
}

/* ============================== binance key =============================== */
async function refreshKey(force){
  let k; try { k = await j('/api/binance' + (force ? '?force=1' : '')); } catch(e){ return; }
  S.key = k;
  const verdict = k.verdict || 'unknown';
  const calm = !k.ok && verdict === 'not checked';
  const st = $('bk-status'); if(!st) return;
  st.className = k.ok ? 'ok' : (calm ? 'muted' : 'bad');
  st.textContent = verdict + (k.cached ? ' · cached' : '');
  const meta = [];
  if(k.base_url) meta.push(esc(String(k.base_url).replace(/^https?:[/][/]/, '')));
  if(k.host_is_testnet) meta.push('testnet');
  else if(k.production) meta.push('<span class="bad">PRODUCTION</span>');
  meta.push('mode ' + esc(k.mode || '?') + ' · armed <b>' + (k.armed ? 'yes' : 'no') + '</b>');
  const creds = Object.entries(k.credentials || {}).map(([n, p]) => '<span class="' + (p ? 'ok' : 'bad') + '">' + esc(n) + ' ' + (p ? 'set' : 'MISSING') + '</span>');
  if(creds.length) meta.push(creds.join(' '));
  if(k.checked_at) meta.push(esc(k.checked_at));
  if(typeof k.latency_ms === 'number') meta.push(k.latency_ms + ' ms');
  $('bk-meta').innerHTML = meta.join(' · ');
  const rows = [];
  const row = (left, right, cls, tip) => rows.push('<tr><td class="' + (cls || 'muted') + '"' + (tip ? ' data-tip="' + esc(tip) + '"' : '') + '>' + left + '</td><td colspan="2"' + (tip ? ' data-tip="' + esc(tip) + '"' : '') + '>' + right + '</td></tr>');
  for(const s of (k.steps || [])) row(s.ok ? 'ok' : 'FAIL', esc(s.name) + ' <span class="muted">' + esc(s.detail) + '</span>', s.ok ? 'ok' : 'bad');
  const clock = k.clock || {};
  if(Object.keys(clock).length)
    row('clock', (clock.server_time_ms === null || clock.server_time_ms === undefined) ? 'not read' : 'server ' + esc(clock.server_time_ms) + ' ms · offset <b>' + esc(clock.offset_ms == null ? '—' : clock.offset_ms) + ' ms</b> (recvWindow ' + esc(clock.recv_window_ms) + ' ms)', clock.ok ? 'muted' : 'warn');
  const perms = Object.entries(k.permissions || {}).filter(([, v]) => v !== null && v !== undefined && !(Array.isArray(v) && !v.length));
  if(perms.length) row('permissions', perms.map(([n, v]) => esc(n) + '=<b>' + esc(v) + '</b>').join(' '));
  const sig = k.signature || {};
  if(Object.keys(sig).length) row('signature', esc(sig.path || '') + ' ok=<b>' + (sig.ok ? 'true' : 'false') + '</b>' + (sig.error ? ' · ' + esc(sig.error) : ''), sig.ok ? 'muted' : 'warn');
  const acc = k.account || {};
  if(acc.balances && acc.balances.length)
    row('balances', acc.balances.map(b => esc(b.asset) + ' <b>' + Number(b.free).toFixed(8) + '</b>' + (Number(b.locked) ? ' <span class="muted">(locked ' + Number(b.locked).toFixed(8) + ')</span>' : '')).join('<br>') + '<div class="muted">' + acc.balances_total + ' non-zero asset(s), quote ' + esc(acc.quote_asset || '—') + '</div>',
      'The Testnet account\u2019s non-zero balances as the key itself reports them: what is free and what is locked in an order. Read-only \u2014 this panel never moves any of it.');
  const open = k.open_orders || {};
  if(open.orders && open.orders.length) row('open orders', open.orders.map(o => esc(o.symbol) + ' ' + esc(o.side) + ' ' + esc(o.qty) + ' ' + esc(o.type) + ' → ' + esc(o.status) + ' <span class="muted">(id ' + esc(o.id) + ')</span>').join('<br>'),
    'Orders that exist on the Testnet account right now. This dashboard cannot create or cancel one: the probe only reads.');
  const lim = k.limits || {};
  if(lim.max_weight_per_min) row('rate budget', esc(lim.weight_used) + '/' + esc(lim.max_weight_per_min) + ' weight · cooldown ' + esc(lim.cooldown_s) + ' s',
    'How much of Binance\u2019s request-weight allowance the probe has used this minute, and the cooldown the client keeps before it asks again \u2014 the guard against being rate-limited.');
  const http = k.http || {};
  if(http.calls !== undefined) row('requests', http.calls + ' call(s) <span class="muted">(' + esc((http.methods || []).join(', ') || 'none') + ')</span> · <span class="' + (http.read_only ? 'ok' : 'bad') + '">' + esc(http.posts) + ' POST</span> · read-only <b>' + (http.read_only ? 'yes' : 'no') + '</b>',
    'What the probe actually sent, by HTTP method. read-only yes means no call in this probe could change anything on the exchange \u2014 which is the property worth checking.');
  const notices = (k.warnings || []).map(t => ['warning', t]).concat((k.problems || []).map(t => ['problem', t])).concat((k.hints || []).map(t => ['hint', t]));
  for(const [kind, text] of notices) row(kind, esc(text), kind === 'hint' ? 'muted' : (calm ? 'muted' : (kind === 'warning' ? 'warn' : 'bad')),
    'What the probe reported about the key or the rail: a warning is a caveat, a problem is something that will make a call fail, a hint is what to do about it.');
  if(k.trade_ready !== undefined && k.trade_ready !== null) row('ready to trade', '<b>' + k.trade_ready + '</b>',
    'Whether the configured key has everything the rail needs to place an order. It is a statement about the key, not permission to trade: the rail stays off until it is switched on deliberately.');
  $('bk-body').innerHTML = rows.length ? '<table class="tbl">' + rows.join('') + '</table>' : '<span class="muted">no detail</span>';
}

/* ============================= CAPITAL ACCOUNT ============================
   The one place simulated money enters the system.  The page can read the account and
   ask for a movement; the server moves it, and only with a confirmation and a reason.
   Nothing here can trade and nothing here is real money - see the card's own footer. */
function capFormReady(action){
  const amount = ($('cap-amount') && $('cap-amount').value || '').trim();
  const reason = ($('cap-reason') && $('cap-reason').value || '').trim();
  const n = parseFloat(amount);
  const hasAmount = isFinite(n) && n > 0;
  if(!reason) return false;
  if(action === 'release') return true;   /* amount blank = the whole allocation */
  return hasAmount;
}
function capButtons(){
  const sess = (S.cap && S.cap.session) || null;
  /* A movement needs a reason, and an amount - except `release`, whose amount may be
     blank, meaning "the whole allocation".  `allocate`/`release` also need a session. */
  for(const [action, possible] of [['deposit', true], ['withdraw', true], ['allocate', !!sess], ['release', !!(sess && sess.funded > 0)]]){
    const b = $('cap-' + action); if(!b) continue;
    b.disabled = !(possible && capFormReady(action));
  }
}
function capMsg(text, cls){ const el = $('cap-status'); if(el){ el.className = cls || 'muted'; el.textContent = text; } }
async function refreshCapital(){
  let c; try { c = await j('/api/capital'); } catch(e){ capMsg('unavailable: ' + ((e && e.message) || e), 'bad'); return; }
  S.cap = c; capRender();
}
function capRender(){
  const c = S.cap; if(!c) return;
  const cur = c.currency || '';
  const sess = c.session || null;
  capMsg(c.simulated ? 'simulated account' : 'account', 'muted');
  mount('cap-tiles', [
    tile('Free balance', money(c.balance) + ' ' + esc(cur), 'credited but not allocated', c.balance > 0 ? '' : 'muted',
      'Capital that is in the account and not yet handed to a session. Only an operator can put it there: no strategy, genome or config key credits money.'),
    tile('Credited', money(c.credited) + ' ' + esc(cur), c.withdrawn ? ('withdrawn ' + money(c.withdrawn)) : 'never withdrawn', '',
      'The sum of every deposit in the ledger, minus withdrawals: the capital this account has ever held.'),
    tile('Allocated', money(c.allocated) + ' ' + esc(cur), 'working in a paper session', c.allocated > 0 ? 'ai' : '',
      'Capital handed to a paper session, where the agents trade it through the risk gate. It returns to the free balance only through a release.'),
    tile('Session capital', sess ? (money(sess.capital) + ' ' + esc(cur)) : 'N/D',
      sess ? ('session #' + sess.session_id + ' · ' + (sess.funded > 0 ? 'from the ledger' : 'config initial_cash')) : 'no session',
      sess && sess.funded > 0 ? 'ai' : '',
      'What the active paper session trades with. A funded session is marked as coming from the ledger; an unfunded one uses `backtest.initial_cash`.'),
    tile('Per-credit cap', money(c.policy.max_deposit) + ' ' + esc(cur), 'total ceiling ' + money(c.policy.max_total_capital), '',
      'Operator policy from `funding.max_deposit` / `funding.max_total_capital`, there to stop a typo from funding a fantasy account. It is not the sealed layer: the 5% per position and 15% drawdown caps bind on funded capital too.')
  ].join(''));
  capButtons();
  const rows = c.ledger || [];
  table('cap-ledger', 'cap-ledger', null, 'cap-ledger-pager', {
    rows: rows, pageSize: 8, sort: 'id', dir: 'desc', empty: 'the ledger is empty: no capital has ever been credited',
    searchKeys: ['kind', 'reason', 'source'],
    columns: [
      {key:'id', label:'#', tip:'The row\u2019s id in the local database \u2014 stable, and the thing to quote when you ask about this record.', render:v => '<span class="mono muted">' + esc(v) + '</span>'},
      {key:'ts', label:'when', render:v => '<span class="mono" style="font-size:11.5px">' + dt(v) + '</span>'},
      {key:'kind', label:'movement', tip:'What the operator did: deposit credits the account, withdraw takes capital back, allocation hands it to a paper session, release takes it back from one.', render:v => chip(v || 'N/D', {deposit:'ok', allocation:'ai', release:'info'}[v] || 'warn')},
      {key:'amount', label:'amount', tip:'The signed size of this movement, in the account\u2019s currency. A signed amount is what makes a credit auditable rather than a claim.', align:'right', render:v => '<span class="' + sgn(v) + '">' + (v > 0 ? '+' : '') + money(v) + '</span>'},
      {key:'balance_after', label:'balance after', tip:'The free balance immediately after this movement, recorded at the time so the ledger can be replayed row by row.', align:'right', render:v => '<span class="mono">' + money(v) + '</span>'},
      {key:'session_id', label:'session', tip:'The paper session this capital was allocated to or released from. A movement with no session is a deposit or a withdrawal.', render:v => v != null ? ('#' + esc(v)) : '<span class="muted">—</span>'},
      {key:'reason', label:'reason', tip:'The reason the operator gave for this movement. It is required: it is what makes the row auditable instead of an unexplained number.', render:v => esc(v || 'N/D')},
      {key:'source', label:'from', tip:'Which front end wrote the row (dashboard, CLI, or the program itself). Agents can never be the source of a credit.', render:v => '<span class="tag">' + esc(v || '?') + '</span>'}
    ]
  });
}
/* One movement at a time, and never without a sentence saying what it will do. */
function capAsk(action){
  if(!capFormReady(action)) return;
  const amount = (($('cap-amount') && $('cap-amount').value) || '').trim();
  const reason = (($('cap-reason') && $('cap-reason').value) || '').trim();
  const c = S.cap || {};
  const sess = c.session || null;
  const who = sess ? ('session #' + sess.session_id) : 'the active session (none exists)'; 
  const text = {
    deposit: 'credit <b>' + esc(amount) + ' ' + esc(c.currency || '') + '</b> into the account\u2019s free balance',
    withdraw: 'withdraw <b>' + esc(amount) + ' ' + esc(c.currency || '') + '</b> from the free balance (now ' + money(c.balance) + ')',
    allocate: 'allocate <b>' + esc(amount) + ' ' + esc(c.currency || '') + '</b> to ' + esc(who) + ', where the agents trade it',
    release: 'return ' + esc(who) + '\u2019s allocated capital (<b>' + money(sess ? sess.funded : null) + '</b>) to the free balance'
  }[action];
  S.capPending = {action};
  mount('cap-confirm-text', text + ' — reason: “' + esc(reason) + '”');
  $('cap-confirm').classList.remove('hidden');
}
function capCancel(){ S.capPending = null; $('cap-confirm').classList.add('hidden'); mount('cap-confirm-text', ''); }
async function capGo(){
  const pending = S.capPending;
  if(!pending) return;
  const body = {
    action: pending.action,
    reason: (($('cap-reason') && $('cap-reason').value) || '').trim(),
    confirm: true
  };
  const amount = (($('cap-amount') && $('cap-amount').value) || '').trim();
  if(amount) body.amount = parseFloat(amount);
  const sess = (S.cap && S.cap.session) || null;
  if(sess && (pending.action === 'allocate' || pending.action === 'release')) body.session_id = sess.session_id;
  const go = $('cap-go'); const label = go ? go.textContent : '';
  if(go){ go.disabled = true; go.textContent = pending.action + '…'; }
  let r, obj = {};
  try {
    r = await fetch('/api/capital', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    obj = await r.json().catch(() => ({}));
  } catch(e){ obj = {error: String(e)}; r = {status: 0}; }
  if(go){ go.disabled = false; go.textContent = label || 'confirm'; }
  if(!r.status || r.status >= 400 || obj.ok === false){
    mount('cap-confirm-text', 'refused: ' + esc(obj.error || ('HTTP ' + r.status)));
    const box = $('cap-go'); if(box) box.disabled = false;
    return;
  }
  capCancel();
  S.cap = obj.account || S.cap;
  if($('cap-amount')) $('cap-amount').value = '';
  capMsg(pending.action + ': ledger row #' + ((obj.entry || {}).id || '?') + ' written', 'ok');
  capRender();
  renderActive().catch(() => {});
}

/* ================================= OVERVIEW =============================== */
function maxDrawdown(points){
  let peak = -Infinity, mdd = 0;
  for(const p of (points || [])){ const e = +p.equity; peak = Math.max(peak, e); if(peak > 0) mdd = Math.max(mdd, 1 - e / peak); }
  return mdd * 100;
}
async function renderOverview(){
  const [ov, paper, eq, risk, se, tr, logs] = await Promise.all([
    safe(api('/api/overview')), safe(api('/api/paper')), safe(api('/api/equity')), safe(api('/api/risk')),
    loopStatus(), safe(api('/api/trades')), safe(api('/api/logs?limit=40'))
  ]);
  const pts = (eq && eq.points) || [];
  const a = (risk && risk.account) || {};
  const sess = (paper && paper.session) || null;
  const l = (se && se.loop) || {};
  const cur = ov && ov.stats ? ov.stats : {};

  /* --- the numbers that matter, all from the backend --- */
  const equity = a.equity != null ? a.equity : (sess ? sess.equity : null);
  const cash = a.cash != null ? a.cash : (sess ? sess.cash : null);
  const initial = sess ? sess.initial_cash : (pts.length ? +pts[0].equity : null);
  const pnlAbs = (isNum(equity) && isNum(initial)) ? equity - initial : null;
  const pnlPct = (isNum(pnlAbs) && initial) ? pnlAbs / initial * 100 : null;
  const mdd = pts.length ? maxDrawdown(pts) : null;
  const nTrades = sess ? sess.n_trades : (tr && tr.items ? tr.items.length : null);

  mount('ov-tiles', [
    tile('Account equity', money(equity), cash != null ? 'cash ' + money(cash) : 'paper session ' + (sess ? '#' + sess.session_id : 'N/D'), '', 'Simulated paper account: equity = cash + the open position marked at the last recorded bar.'),
    tile('P&L', '<span class="' + sgn(pnlAbs) + '">' + (pnlAbs != null ? (pnlAbs >= 0 ? '+' : '') + money(pnlAbs) : 'N/D') + '</span>', pnlPct != null ? pct(pnlPct, 2) + ' vs initial cash' : 'no baseline', sgn(pnlAbs), 'Absolute and percentage change of the simulated account against its initial cash.'),
    tile('Drawdown', '<span class="' + (a.drawdown_pct > 0 ? 'neg' : '') + '">' + fmt(a.drawdown_pct, 2) + '%</span>', mdd != null ? 'max ' + fmt(mdd, 2) + '% from the curve' : 'current 0%', a.drawdown_pct > 0 ? 'neg' : '', 'Current drawdown from the session peak, and the maximum over the recorded equity curve. The sealed cap is 15%.'),
    tile('Trades', fmt(nTrades, 0), fmt(a.open_positions, 0) + ' open position(s)', '', 'Closed paper trades recorded for the active session, and open positions derived from equity − cash.'),
    tile('Market', esc(S.market.symbol), esc(S.market.interval) + ' · public Binance feed', '', 'The symbol the live chart and the backtest data are configured around. The live chart reads the keyless public Binance mirror.'),
    tile('Engine', chip('paper', 'info'), (S.ks && S.ks.blocked) ? chip('stop engaged', 'bad') : chip('orders allowed', 'ok'), '', 'The execution mode is paper: orders fill on the simulator and pass the sealed risk gate. The stop blocks new orders on every rail.'),
    tile('AI agents', l.running ? 'running' : 'idle', 'loop ' + ((l.generations_done != null ? l.generations_done : 0) + '/' + (l.generations_total || 0)), l.running ? 'ai' : '', 'The research loop (the Evolver) is the autonomous process here; the other subsystems are listed in the Agent Center.'),
    tile('Last cycle', cur.runs != null ? ('run #' + (cur.runs)) : 'N/D', cur.best_fitness != null ? 'best fitness ' + fmt(cur.best_fitness, 3) : 'no hall entry', '', 'The most recent recorded improvement run and the best fitness in the hall of fame.')
  ].join(''));

  /* --- the improvement cycle, from /api/lab (fetched lazily to keep the tick light) --- */
  safe(api('/api/lab')).then(lab => { S.lab = lab; renderCycleStrip(lab); }).catch(() => {});

  /* --- equity + market --- */
  if($('ov-equity')) drawEquity($('ov-equity'), pts, {baseline: initial, xlabels: pts.map(p => dt(p.ts).slice(5, 10))});
  mount('ov-eq-meta', pts.length ? (pts.length + ' points · ' + dt(pts[0].ts) + ' → ' + dt(pts[pts.length - 1].ts)) : 'no session');

  /* --- risk & engine status card --- */
  const nearest = risk && risk.nearest;
  mount('ov-status', kv([
    ['engine', chip('paper', 'info'), 'The active order rail. Paper means every fill is simulated on the local broker, in front of which the sealed pre-trade gate sits. No real order rail exists in this build.'],
    ['data source', esc('data/' + (cur.csv || '')) || 'configured csv', 'The local price file the backtest, the paper rail and this page all read through the same validating loader.'],
    ['loop', l.running ? chip('running · pid ' + (l.pid || '?'), 'ok', true) : chip('idle', 'muted'), 'The tracked research loop: whether a live process is evaluating genomes, and its pid. The pid decides the state, never the file alone.'],
    ['risk limits', !risk || !risk.ok ? chip('not measurable', 'muted') : (risk.would_arm ? chip('breached — stop imminent', 'bad') : (nearest ? chip('ok · nearest ' + esc(nearest.name), 'ok') : chip('ok', 'ok'))), 'The sealed caps measured on the active session, with the nearest one named. breached means a halting limit is past its cap and the next order is refused.'],
    ['drawdown', fmt(a.drawdown_pct, 2) + '% of the sealed 15%', 'How far the session is below its own peak, against the sealed 15% cap. Past it, the gate refuses new orders and the emergency stop can be armed.'],
    ['alert channel', alertChip(risk && risk.alerts, risk && risk.pending), 'The optional Telegram alert channel and what it still owes you: a halt nobody has been told about is the row worth acting on.'],
    ['db', (ov ? 'ok' : 'unreachable'), 'The local state database this page reads. unreachable means the panels fall back to N/D rather than inventing numbers.']
  ]) + '<details class="tech"><summary data-tip="More identifiers than a glance needs: which paper session the numbers come from, which window it walks, how far its cursor has got, and the exposure in absolute terms.">technical details</summary><div class="body">' + kv([
    ['paper session', sess ? ('#' + sess.session_id + ' ' + esc(sess.name)) : 'none', 'The forward paper session the account numbers come from, if one is active.'],
    ['window / status', sess ? (esc(sess.window || '') + ' · ' + esc(sess.status || '')) : 'N/D', 'Which slice of the data the session walks, and whether it is running, paused or finished.'],
    ['cursor', sess ? (sess.cursor + '/' + sess.total_bars) : 'N/D', 'How far through the window the session has walked: bars consumed of the total.'],
    ['exposure', money(a.positions_value), 'The market value of what is currently held — measured against the sealed exposure cap.']
  ]) + '</div></details>');

  /* --- latest trades --- */
  const items = (tr && tr.items) ? tr.items.slice(-8).reverse() : [];
  mount('ov-tr-meta', items.length ? (items.length + ' shown') : '');
  mount('ov-trades', items.length ? '<table class="tbl"><tbody>' + items.map(t =>
    '<tr><td class="mono">' + esc(t.direction) + '</td><td class="muted">' + esc(t.exit_reason) + '</td><td class="num ' + sgn(t.pnl) + '">' + (t.pnl >= 0 ? '+' : '') + fmt(t.pnl, 2) + '</td><td class="muted mono">' + esc(dt(t.exit_time).slice(5)) + '</td></tr>').join('') + '</tbody></table>'
    : empty('no paper trades recorded', '◇'));

  /* --- recent activity & anomalies --- */
  mount('ov-activity', renderActivity(logs));
  mount('ov-updated', 'updated ' + new Date().toLocaleTimeString() + ' · auto-refresh every ' + (REFRESH / 1000) + ' s');
}
/* What each stage of the improvement cycle means, in the order a reader meets them. */
const STAGE_TIPS = {
  collect: 'Gathering data and reading what previous runs stored: the dataset, its validation, and the lessons already learned.',
  hypothesize: 'Proposing the next move: bounded parameter changes to a strategy, from the local model when it is available and deterministically when it is not.',
  backtest: 'Scoring candidates on the walk-forward folds with the real cost model and hard gates \u2014 the objective the search maximises.',
  validate: 'Reading the single untouched holdout window, out of sample. It is read once, precisely so it cannot be tuned against.',
  paper: 'Forward paper trading: the champion\u2019s signals become simulated orders through the pre-trade risk gate, on a persistent cursor.',
  learn: 'Writing down what the run taught: the lesson, its scope and its embedding, so a later run starts from it instead of from nothing.'
};
function renderCycleStrip(lab){
  if(!lab || !lab.ok){ mount('ov-cycle', ''); return; }
  const st = {};
  for(const s of (lab.cycle || [])) st[s.key] = s;
  const order = ['collect','hypothesize','backtest','validate','paper','learn'];
  const html = order.map(k => { const s = st[k]; if(!s) return '';
    const stageTip = esc(s.label) + ' \u2014 ' + (STAGE_TIPS[s.key] || 'one stage of the improvement cycle') +
      ((s.detail || []).length ? ('\n\nNow: ' + (s.detail || []).slice(0, 3).map(d => d.label + ' ' + d.value).join(' \u00b7 ')) : '');
    return '<div class="stage s-' + stageClass(s.status) + '" data-tip="' + stageTip + '"><div class="st-h"><span class="st-n">' + (s.ord || '') + '</span>' + esc(s.label) + '</div>' +
      '<div class="st-d">' + (s.detail || []).slice(0, 2).map(d => '<span>' + esc(d.label) + ' <b>' + esc(d.value) + '</b></span>').join('') + '</div></div>';
  }).join('');
  mount('ov-cycle', '<div class="card ai-card" style="grid-column:1/-1;width:100%"><div class="card-h"><h2 data-tip="The six stages of the last recorded run, each labelled with the evidence behind it: what was collected, what was hypothesised, how it scored on the folds, whether it was validated out of sample, what the paper session did with it and what was stored as a lesson. A stage with no evidence reads N/D rather than a guess.">Last improvement cycle</h2><span class="spacer"></span><span class="muted">run #' + (lab.selected || '—') + (lab.metric ? ' · metric ' + esc(lab.metric) : '') + '</span></div><div class="card-b"><div class="cycle">' + (html || empty('no run recorded yet', '◈')) + '</div></div></div>');
  const el = $('ov-cycle'); if(el) el.style.display = 'block';
}
function renderActivity(logs){
  if(!logs) return '<span class="muted">activity log unavailable</span>';
  const rows = [];
  for(const e of (logs.risk_events || []).slice(0, 8)) rows.push(['risk', e]);
  for(const c of (logs.api_calls || []).filter(c => !c.ok).slice(0, 6)) rows.push(['api', c]);
  if(!rows.length) return empty('no risk events or failed provider calls recorded', '✓');
  rows.sort((a, b) => (b[1].created_at || 0) - (a[1].created_at || 0));
  return '<div class="list">' + rows.slice(0, 12).map(([kind, x]) => {
    const bad = kind === 'risk' || !x.ok;
    const label = kind === 'risk' ? esc(x.kind) : (esc(x.provider) + ' ' + esc(x.method) + ' ' + esc(x.path));
    const extra = kind === 'risk' ? esc(x.reason) : (esc(x.code || '') + ' ' + esc(x.msg || ''));
    return '<div class="row"><span class="when">' + dt(x.created_at) + '</span><span class="chip ' + (bad ? 'bad' : 'muted') + '">' + (kind === 'risk' ? 'risk' : 'api') + '</span><span class="body">' + label + ' <span class="muted">— ' + extra + '</span></span></div>';
  }).join('') + '</div>';
}

/* ================================= TRADING ================================ */
async function renderMarketPage(){
  /* The Market page draws the same public series the Overview does, from the same
     keyless endpoint: one selection, two places it is shown. Nothing here writes. */
  await refreshMarket();
  const notes = $('mk-notes');
  if(notes) notes.textContent = 'Candles and 24h stats come from the public Binance '
    + 'feed - keyless, read-only, cached server-side. No account is involved and this '
    + 'page cannot place, change or cancel anything.';
}
async function renderCapitalPage(){
  /* The account and its ledger, with the same confirm-gated movements the CLI's `capital`
     command makes. The note states what a credit is: a local row that funds a paper
     session. The real rails do not read this ledger and this page does not pretend
     otherwise - a Binance balance comes from Binance. */
  await refreshCapital();
  const notes = $('cap-notes');
  if(notes) notes.innerHTML = 'A credit here is a row in a local ledger, not a payment: '
    + '<b>selftrade/capital.py</b> imports no HTTP client, no socket and no exchange module, '
    + 'and a test parses the file to keep it that way. What it changes is the capital a '
    + '<b>paper session</b> trades, and therefore how large its positions are. The real rails '
    + 'do not read it: a Binance balance comes from Binance, and no credit here changes what '
    + 'an exchange will let you trade. Every movement needs an amount, a reason and a '
    + 'confirmation, and no agent can credit itself.';
}
async function renderRiskCenter(){
  /* The stop first, because it must never be shown stale, then the room left under the
     limits that would engage it, then what the alert channel actually sent about either.
     All three also refresh on the global timer, so this is a first paint, not the only. */
  await refreshSwitch();
  await refreshRisk();
  await refreshAlerts();
}
async function renderTrading(){
  const [paper, risk, tr] = await Promise.all([safe(api('/api/paper')), safe(api('/api/risk')), safe(api('/api/trades'))]);
  if(!S.key) refreshKey(false).catch(() => {});
  const sess = (paper && paper.session) || null;
  mount('tr-session', sess ? kv([
    ['session', '#' + sess.session_id + ' <span class="muted">' + esc(sess.name) + '</span>'],
    ['status', chip(sess.status || '?', sess.status === 'active' ? 'ok' : 'muted')],
    ['strategy', '<span class="mono">' + esc((sess.genome || {}).family || 'N/D') + '</span>'],
    ['cursor', sess.cursor + ' / ' + sess.total_bars],
    ['trades', fmt(sess.n_trades, 0)],
    ['initial cash', money(sess.initial_cash) +
      (sess.capital_source === 'ledger'
        ? ' ' + chip('from the ledger', 'ai')
        : ' <span class="muted">(config initial_cash — nobody has credited this account)</span>')],
    ['equity', money(sess.equity)],
    ['return', '<span class="' + sgn(sess.total_return) + '">' + pct(sess.total_return * 100, 2) + '</span>']
  ]) : empty('no paper session', '◇'));
  const a = (risk && risk.account) || {};
  mount('tr-pos-meta', risk && risk.ok ? ('measured on session #' + ((risk.session || {}).session_id)) : '');
  mount('tr-positions', risk && risk.ok ? kv([
    ['open positions', fmt(a.open_positions, 0) + ' <span class="muted">(equity − cash)</span>', 'How many positions are open, derived as equity − cash. The paper session keeps no positions table of its own: the fill ledger is the truth.'],
    ['exposure', money(a.positions_value), 'The market value currently at risk, measured against the sealed exposure cap — the number the gate reads before it lets another order through.'],
    ['cash', money(a.cash), 'Free cash in the simulated account: what is not in a position.'],
    ['equity', money(a.equity), 'Cash plus the open position marked at the last recorded bar. This is the number the drawdown and daily-loss caps are measured on.'],
    ['peak equity', money(a.peak_equity), 'The highest equity reached so far. Drawdown is measured from this peak, not from the starting cash.'],
    ['day P&L', '<span class="' + sgn(a.day_pnl_pct) + '">' + fmt(a.day_pnl_pct, 2) + '%</span>', 'Today\u2019s change against the sealed 2% daily-loss cap.'],
  ]) + '<div class="card-sub">The paper session keeps no positions table: exposure is derived as equity − cash and pinned by the fill ledger. The Binance rail is not measured here.</div>' : empty('no paper session to measure', '◇'));
  const items = (tr && tr.items) ? tr.items : [];
  table('tr-trades', 'tr-trades', 'tr-trades-bar', 'tr-trades-pager', {
    rows: items, pageSize: 12, sort: 'id', dir: 'desc', empty: 'no paper trades recorded',
    searchKeys: ['direction', 'exit_reason', 'entry_time', 'exit_time'],
    filters: [{key:'dir', label:'direction', options:[{value:'long',label:'long'},{value:'short',label:'short'}]},
              {key:'reason', label:'exit', options: Array.from(new Set(items.map(t => t.exit_reason).filter(Boolean))).map(r => ({value:r, label:r}))}],
    filterFn: (t, f) => (!f.dir || t.direction === f.dir) && (!f.reason || t.exit_reason === f.reason),
    columns: [
      {key:'id', label:'#', tip:'The row\u2019s id in the local database \u2014 stable, and the thing to quote when you ask about this record.', render:v => '<span class="mono muted">' + esc(v) + '</span>'},
      {key:'direction', label:'side', tip:'Long means the position profits when the price rises; short means it profits when the price falls.', render:v => chip(v, v === 'long' ? 'info' : 'warn')},
      {key:'entry_time', label:'entry', tip:'When the trade was opened: the bar at which the order filled, in the data\u2019s own clock.', render:v => '<span class="mono">' + esc(dt(v)) + '</span>'},
      {key:'exit_time', label:'exit', tip:'When the trade was closed, and therefore the bar whose price the P&L is settled at.', render:v => '<span class="mono">' + esc(dt(v)) + '</span>'},
      {key:'entry_price', label:'entry px', tip:'The fill price on entry, including the modelled slippage rather than the raw close.', align:'right', render:v => fmt(v, 2)},
      {key:'exit_price', label:'exit px', tip:'The fill price on exit, after costs. The difference from entry is the trade\u2019s gross move, before fees.', align:'right', render:v => fmt(v, 2)},
      {key:'bars_held', label:'bars', tip:'How many candles the trade was held for \u2014 a short hold means costs weigh more heavily on the result.', align:'right'},
      {key:'exit_reason', label:'reason', tip:'Why it closed: a signal reversal, the armed stop, the target, the trailing stop, or the end of the data window.', render:v => '<span class="tag">' + esc(v || 'N/D') + '</span>'},
      {key:'pnl', label:'P&L', tip:'Net profit or loss for the trade after fees and slippage, in the account\u2019s currency.', align:'right', render:v => '<span class="' + sgn(v) + '">' + (v >= 0 ? '+' : '') + fmt(v, 2) + '</span>'},
      {key:'return_pct', label:'return', align:'right', render:v => '<span class="' + sgn(v) + '">' + (isNum(v) ? (v * 100).toFixed(3) + '%' : 'N/D') + '</span>'}
    ]
  });
  refreshCapital().catch(() => {});
}

/* ================================== AI LAB ================================ */
function stageClass(s){ return {ok:'ok', active:'active', warn:'warn', bad:'bad', failed:'bad', unavailable:'un'}[s] || 'un'; }
/* diff helpers: one diff rendering, used by the version table and its detail row */
function diffVal(v){
  if(v === null || v === undefined) return 'unset';
  if(v === true) return 'true';
  if(v === false) return 'false';
  if(isNum(v)) return (Number.isInteger(v) ? String(v) : v.toFixed(4));
  return String(v);
}
function diffText(diff, max){
  const d = diff || [];
  if(!d.length) return '<span class="muted">carried over unchanged</span>';
  const n = max || 2;
  return d.slice(0, n).map(p => '<span class="mono">' + esc(p.key) + '</span> <span class="muted">' + esc(diffVal(p.from)) + '</span>→<b>' + esc(diffVal(p.to)) + '</b>').join(' <span class="dim">|</span> ') +
    (d.length > n ? ' <span class="muted">+' + (d.length - n) + ' more</span>' : '');
}
function outcomeChip(o){
  const kind = {champion:'ai', kept:'ok', dropped:'bad'}[o] || 'muted';
  return chip(o || 'N/D', kind);
}
function renderRepro(rp){
  rp = rp || {};
  const commit = rp.commit ? ('<span class="mono">' + esc(rp.commit_short || String(rp.commit).slice(0, 12)) + '</span>' +
    (rp.branch ? ' <span class="muted">' + esc(rp.branch) + '</span>' : '') +
    (rp.dirty ? ' ' + chip('worktree dirty', 'warn') : '')) : 'N/D';
  const data = rp.dataset ? ('<span class="mono">' + esc(rp.dataset_short || String(rp.dataset).slice(0, 12)) + '</span>' +
    (isNum(rp.dataset_rows) ? ' <span class="muted">' + fmt(rp.dataset_rows, 0) + ' rows</span>' : '')) : 'N/D';
  const src = rp.dataset_path ? '<span class="mono">' + esc(rp.dataset_path) + '</span>'
    : (rp.dataset_method ? esc(rp.dataset_method) + ' hash' : 'N/D');
  const known = !!(rp.repro_id || rp.commit || rp.dataset);
  const reproTip = 'Whether this run can be redone: the recorded seed, dataset fingerprint and commit. no record means the run predates that provenance, not that it was irreproducible.';
  mount('lab-repro-chip', rp.repro_id
    ? (rp.reproducible === true ? chip('reproducible', 'ok', false, reproTip) : chip('not reproducible from the commit', 'warn', false, reproTip))
    : chip('no record', 'muted', false, reproTip));
  const notes = (rp.notes || []).map(n => '<li>' + esc(n) + '</li>').join('');
  mount('lab-repro', (known ? kv([
    ['reproducibility id', rp.repro_id ? '<span class="mono" style="font-size:12.5px">' + esc(rp.repro_id) + '</span>' : 'N/D'],
    ['code (commit)', commit],
    ['dataset', data],
    ['dataset source', src],
    ['versions recorded', fmt(rp.versions_recorded, 0) + ' <span class="muted">in this run</span>']
  ]) : empty('this run has no recorded provenance', '◌')) +
    (notes ? '<div class="card-sub warn"><b>Notes</b><ul style="margin:6px 0 0 16px;padding:0">' + notes + '</ul></div>' : '') +
    '<div class="card-sub">The id is a hash of the commit and the dataset digest: two runs of the same code over the same prices share it, which is what makes a stored fitness traceable. It is <b>not</b> a promise of identical results - a run from a dirty worktree is flagged instead of hidden.</div>');
}
/* Re-run a recorded version: the same genome through the same engine on the folds
   window, which is the window the stored fitness came from.  This is the check the
   provenance exists for, so it says "reproduces" or "does not" and never guesses. */
async function rerunVersion(id){
  const box = $('verrun-' + id);
  if(box) box.innerHTML = '<span class="muted">re-running the recorded genome on the folds window…</span>';
  try {
    const res = await j('/api/backtest?version_id=' + encodeURIComponent(id) + '&window=folds');
    S.verRuns[id] = res;
  } catch(e){
    S.verRuns[id] = {ok: false, error: (e && e.message) || 'request failed'};
  }
  verRunBox(id);
}
function verRunBox(id){
  const box = $('verrun-' + id);
  if(!box) return;
  const res = S.verRuns[id];
  if(!res){ box.innerHTML = ''; return; }
  if(!res.ok){ box.innerHTML = errBox('re-run failed: ' + (res.error || 'unknown error')); return; }
  const cmp = res.comparison || {};
  const ok = cmp.matches === true;
  box.innerHTML = '<div class="banner ' + (ok ? '' : 'bad') + '"><span class="ico">' + (ok ? '✓' : '✕') + '</span><div><b>' +
    (ok ? 'Reproduces' : 'Does not reproduce') + '</b> — ' + esc(cmp.note || '') + '<br>' +
    'stored <span class="mono">' + fmt(cmp.stored_fitness, 6) + '</span> · re-run <span class="mono">' + fmt(res.fitness, 6) + '</span>' +
    (isNum(cmp.delta) ? ' · Δ <span class="mono">' + (cmp.delta >= 0 ? '+' : '') + cmp.delta.toExponential(2) + '</span>' : '') +
    ' · ' + fmt(res.n_trades, 0) + ' trades · ' + fmt(res.runtime_ms, 0) + ' ms' +
    '<br>dataset ' + (cmp.dataset_matches == null ? 'unknown' : (cmp.dataset_matches ? 'identical to that run' : '<span class="warn">different from that run</span>')) +
    ' · repro id ' + (cmp.current_repro_id ? '<span class="mono">' + esc(cmp.current_repro_id) + '</span>' : 'N/D') +
    (cmp.stored_repro_id && cmp.stored_repro_id !== cmp.current_repro_id ? ' (that run: <span class="mono">' + esc(cmp.stored_repro_id) + '</span>)' : '') +
    '</div></div>';
}
function renderVersions(lab, labVersions){
  const v = labVersions || [];
  const champ = v.filter(x => x.outcome === 'champion').pop();
  mount('lab-ver-meta', v.length ? (v.length + ' recorded' + (champ ? ' · champion v' + champ.version : '') + (lab.versions_truncated ? ' (truncated)' : '')) : '');
  table('lab-ver', 'lab-ver', 'lab-ver-bar', 'lab-ver-pager', {
    rows: v, pageSize: 14, sort: 'version', dir: 'desc', rowId: x => 'ver' + x.id,
    empty: 'no strategy version recorded for this run — it ran before versions were recorded',
    searchKeys: ['family', 'origin', 'reason', 'rationale', 'outcome', 'diff_keys'],
    filters: [
      {key:'outcome', label:'outcome', options: Array.from(new Set(v.map(x => x.outcome).filter(Boolean))).map(o => ({value:o, label:o}))},
      {key:'origin', label:'origin', options: Array.from(new Set(v.map(x => x.origin).filter(Boolean))).map(o => ({value:o, label:o}))},
      {key:'gate', label:'gate', options:[{value:'keep',label:'passed'},{value:'drop',label:'failed'}]},
      {key:'change', label:'change', options:[{value:'changed',label:'changed params'},{value:'same',label:'unchanged'}]}
    ],
    filterFn: (x, f) => (!f.outcome || x.outcome === f.outcome) && (!f.origin || x.origin === f.origin) &&
      (!f.gate || (f.gate === 'keep' ? x.passed : !x.passed)) &&
      (!f.change || (f.change === 'changed' ? x.changed > 0 : x.changed === 0)),
    columns: [
      {key:'version', label:'v', tip:'The strategy version number: one recorded version per candidate, in the order the run produced them.', render:v2 => '<span class="mono">v' + esc(v2 == null ? '?' : v2) + '</span>'},
      {key:'generation', label:'gen', tip:'The generation of the evolution run this version came from. Later generations are descendants of earlier ones.', render:v2 => '<span class="mono muted">g' + esc(v2) + '</span>'},
      {key:'family', label:'strategy', tip:'The strategy family the genome belongs to \u2014 the shape of the logic, whose parameters the search then tunes.', render:v2 => '<span class="mono">' + esc(v2 || 'N/D') + '</span>'},
      {key:'origin', label:'origin', tip:'Where the candidate came from: seed is a starting genome, llm is a proposal from the local model, and the rest are mutations or crossovers the search produced.', render:v2 => chip(v2 || 'N/D', v2 === 'llm' ? 'ai' : (v2 === 'seed' ? 'muted' : ''))},
      {key:'fitness', label:'fitness', tip:'The score this candidate got on the walk-forward folds \u2014 the number the search maximises, not an out-of-sample result.', align:'right', render:v2 => '<b class="' + (v2 >= 0 ? 'ok' : 'bad') + '">' + fmt(v2, 3) + '</b>'},
      {key:'delta_fitness', label:'vs parent', align:'right', render:v2 => isNum(v2) ? ('<span class="' + sgn(v2) + '">' + (v2 >= 0 ? '+' : '') + fmt(v2, 3) + '</span>') : 'N/D',
        tip:'Fitness change against the genome this version was actually derived from (mutation parent or carried-over elite). N/D when the version is a fresh seed.'},
      {key:'changed', label:'changes', tip:'How many parameters differ from the parent genome. Zero with a parent means the candidate was carried over unchanged; seed means there is nothing to compare against.', align:'right', render:(v2, row) => row.has_parent === false ? '<span class="muted">seed</span>' : (v2 || 0)},
      {key:'outcome', label:'outcome', render:v2 => outcomeChip(v2)},
      {key:'diff_keys', label:'diff', render:(v2, row) => row.has_parent === false ? '<span class="muted">root genome — nothing to diff</span>' : diffText(row.diff, 2)}
    ],
    detail: x => '<div class="grid"><div class="col-7">' +
      ((x.diff || []).length
        ? '<table class="tbl"><thead><tr><th class="no-sort">parameter</th><th class="no-sort">group</th><th class="no-sort">from</th><th class="no-sort">to</th></tr></thead><tbody>' + x.diff.map(p => '<tr><td class="mono">' + esc(p.key) + '</td><td><span class="tag">' + esc(p.group || '') + '</span></td><td class="num muted">' + esc(diffVal(p.from)) + '</td><td class="num"><b>' + esc(diffVal(p.to)) + '</b></td></tr>').join('') + '</tbody></table>'
        : '<span class="muted">' + (x.has_parent ? 'no parameter changed: the elite was carried over as it is.' : 'fresh seed genome: it has no parent to diff against.') + '</span>') +
      '</div><div class="col-5">' + kv([
        ['version', 'v' + esc(x.version) + ' <span class="muted">run #' + esc(x.run_id == null ? '?' : x.run_id) + '</span>'],
        ['parent', x.has_parent ? ('<span class="mono">' + esc(x.parent_family || '?') + '</span> <span class="muted">' + esc(String(x.parent_hash || '').slice(0, 10)) + '</span>') : 'none (seed)'],
        ['this genome', '<span class="mono">' + esc(String(x.genome_hash || '').slice(0, 10)) + '</span>'],
        ['parent fitness', fmt(x.parent_fitness, 3)],
        ['gate reason', esc(x.reason || 'ok')],
        ['recorded', esc(dt(x.created_at))]
      ]) + '</div></div>' +
      (x.rationale ? '<div class="card-sub"><b>Hypothesis</b> ' + esc(x.rationale) + '</div>' : '') +
      '<div class="toolbar"><button class="btn sm" onclick="rerunVersion(' + esc(x.id) + ')">re-run \u0026 compare</button>' +
      '<span class="muted">re-evaluates this exact genome on the folds window, which is where its stored fitness came from</span></div>' +
      '<div id="verrun-' + esc(x.id) + '"></div>' +
      '<details class="tech"><summary>genomes (JSON)</summary><div class="body"><div class="col-6" style="display:inline-block;vertical-align:top;width:49%"><div class="muted">parent</div><pre class="log">' + esc(JSON.stringify(x.parent_genome || {}, null, 1)) + '</pre></div><div style="display:inline-block;vertical-align:top;width:49%"><div class="muted">this version</div><pre class="log">' + esc(JSON.stringify(x.genome || {}, null, 1)) + '</pre></div></div></details>' +
      (x.outcome === 'champion' ? '<div class="card-sub ai">★ champion of this run - the version whose holdout read is the out-of-sample row</div>' : '')
  });
}
async function loadLab(runId){
  const q = (runId && runId !== 'latest') ? ('?run_id=' + encodeURIComponent(runId)) : '';
  let lab; try { lab = await j('/api/lab' + q); } catch(e){ mount('lab-cycle', errBox('could not load the lab: ' + e.message)); return; }
  S.lab = lab; S.labRun = lab.selected;
  renderLab(lab);
}
async function renderLab(){
  if(!S.lab){ mount('lab-cycle', skel(4)); await loadLab(S.labRun); return; }
  const lab = S.lab;

  /* run selector */
  const sel = $('lab-runs');
  if(sel){
    sel.innerHTML = (lab.runs || []).map(r => '<option value="' + r.id + '"' + (r.id === lab.selected ? ' selected' : '') + '>#' + r.id + ' · ' + (r.outcome || '?') + ' · ' + esc(dt(r.ts)) + ' · ' + (r.repro_id ? ('<' + esc(String(r.repro_id).slice(0, 6)) + '>') : 'no id') + '</option>').join('') || '<option value="latest">no runs</option>';
  }
  const run = (lab.runs || []).find(r => r.id === lab.selected) || {};
  mount('lab-run-meta', lab.selected ? ('run #' + lab.selected + ' · ' + esc(run.outcome || '?') + (run.note ? ' · ' + esc(run.note) : '') + (lab.report_available ? '' : ' · report file missing: only the stored subset is shown')) : 'no run recorded');

  /* provenance and the recorded version lineage (the audit trail, straight from the DB) */
  renderRepro(lab.repro);
  renderVersions(lab, lab.versions);
  /* the boxes are rebuilt with every redraw, so the stored answers are repainted */
  for(const id of Object.keys(S.verRuns || {})) verRunBox(id);

  /* cycle */
  mount('lab-cycle', (lab.cycle || []).map(s =>
    '<div class="stage s-' + stageClass(s.status) + '" data-tip="' + esc(s.label + ' \u2014 ' + (STAGE_TIPS[s.key] || 'one stage of the improvement cycle') +
      ((s.detail || []).length ? ('\n\nNow: ' + s.detail.map(d => d.label + ' ' + d.value).join(' \u00b7 ')) : '') +
      (s.evidence ? ('\n\nEvidence: ' + s.evidence) : '')) + '"><div class="st-h"><span class="st-n">' + esc(s.ord || '') + '</span>' + esc(s.label) + '</div>' +
    '<div class="st-d">' + (s.detail || []).map(d => '<span>' + esc(d.label) + ' <b>' + esc(d.value) + '</b></span>').join('') + '</div>' +
    '<span class="muted mono" style="font-size:10px;margin-top:6px;display:block">' + esc(s.evidence || '') + '</span></div>').join('') || empty('no cycle recorded', '◈'));

  /* summary tiles */
  const c = (lab.champion || {});
  const h = lab.holdout || {};
  mount('lab-summary', [
    tile('Metric', esc(lab.metric || 'N/D'), 'optimiser objective', '', 'The evaluation metric the search maximises. There is deliberately no single accuracy figure here: success is the recorded fitness and the out-of-sample metric, both defined by the backtester and reproducible from the report.'),
    tile('Generations', fmt(run.generations, 0), fmt(run.candidates, 0) + ' candidates evaluated', '', 'How many generations this run was asked for, and how many candidate genomes were built and evaluated in total.'),
    tile('Passed the gate', fmt(run.passed, 0), run.candidates ? (Math.round((run.passed / run.candidates) * 100) + '% of candidates') : 'N/D', '', 'A candidate passes only if n_trades ≥ min_trades and max_drawdown ≤ the configured cap.'),
    tile('Champion', c.family ? esc(c.family) : 'none', c.fitness != null ? 'fitness ' + fmt(c.fitness, 3) : 'no candidate passed', c.family ? 'ai' : '', 'The best candidate of this run: the strategy family that won, and the fitness it was scored with on the walk-forward folds.'),
    tile('Holdout (OOS)', h.metric_value != null ? fmt(h.metric_value, 3) : 'N/D', h.fold_mean != null ? 'fold mean ' + fmt(h.fold_mean, 3) : 'read once, out of sample', h.metric_value != null && h.fold_mean != null ? sgn(h.metric_value - h.fold_mean) : '', 'The tail of the series read exactly once, at the end. A holdout far better than the folds is flagged as regime luck.'),
    /* The benchmark beside the out-of-sample number, because that number means nothing
       without it: a run recorded before the benchmark existed reads N/D rather than a
       comparison against a figure that was never measured. */
    tile('vs market', h.excess != null ? ((h.excess >= 0 ? '+' : '') + fmt(h.excess, 3)) : 'N/D',
      h.baseline_value != null ? ('buy & hold ' + fmt(h.baseline_value, 3)) : 'no benchmark recorded',
      h.excess != null ? (h.excess >= 0 ? 'ok' : 'bad') : '',
      'The champion held against simply holding the asset over the same out-of-sample slice, with the same cash and the same entry cost. excess = the strategy metric minus the benchmark metric; a positive holdout on a rising asset is not an edge by itself.'),
    tile('LLM proposals', lab.llm_available ? 'on' : 'off', lab.proposals ? ('llm ' + (lab.proposals.llm || 0) + ' · det ' + (lab.proposals.deterministic || 0) + ' · seed ' + (lab.proposals.seed || 0)) : 'N/D', '', 'Whether Ollama was reachable this run and where the proposals came from. The LLM never writes trading logic - it proposes bounded genome changes only.')
  ].join(''));

  /* critic */
  const cr = lab.critic;
  const criticTip = 'The deployment verdict for this run\u2019s champion: hard checks against the sealed limits plus the warnings that were recorded. not run means the critic was never invoked for this run.';
  mount('lab-critic-chip', cr ? chip(cr.approved ? 'approved' : 'refused', cr.approved ? 'ok' : 'bad', false, criticTip) : chip('not run', 'muted', false, criticTip));
  mount('lab-critic', cr ? '<div class="banner ' + (cr.approved ? '' : 'bad') + '"><span class="ico">' + (cr.approved ? '✓' : '✕') + '</span><div>' + esc(cr.text || '') + '</div></div>' +
    ((cr.reasons || []).length ? '<div class="card-sub"><b>Refusals</b><ul style="margin:6px 0 0 16px;padding:0">' + cr.reasons.map(r => '<li>' + esc(r) + '</li>').join('') + '</ul></div>' : '') +
    ((cr.warnings || []).length ? '<div class="card-sub"><b>Warnings</b><ul style="margin:6px 0 0 16px;padding:0">' + cr.warnings.map(r => '<li>' + esc(r) + '</li>').join('') + '</ul></div>' : '') +
    ((cr.checks || []).length ? '<details class="tech"><summary>sealed checks (' + cr.checks.length + ')</summary><div class="body"><table class="tbl"><thead><tr><th class="no-sort">check</th><th class="no-sort">value</th><th class="no-sort">limit</th><th class="no-sort">ok</th></tr></thead><tbody>' + cr.checks.map(k => '<tr><td>' + esc(k.name) + '</td><td class="num">' + fmt(k.value, 3) + '</td><td class="num">' + fmt(k.limit, 3) + '</td><td class="' + (k.ok ? 'ok' : 'bad') + '">' + (k.ok ? 'ok' : 'FAIL') + '</td></tr>').join('') + '</tbody></table></div></details>' : '') : empty('no critic verdict recorded for this run', '◇'));

  /* autopsy */
  const au = lab.autopsy;
  const autopsyTip = 'The post-mortem of this run\u2019s losers: the worst losses in their entry context, the failure modes that repeat, and the caveats. not run means no autopsy was recorded.';
  mount('lab-autopsy-chip', au ? chip(au.n_losers != null ? (au.n_losers + ' losers') : 'summary', au.n_losers ? 'warn' : 'muted', false, autopsyTip) : chip('not run', 'muted', false, autopsyTip));
  mount('lab-autopsy', au ? (au.summary ? '<div class="banner"><span class="ico">◍</span><div>' + esc(au.summary) + '</div></div>' : '') +
    ((au.worst || []).length ? '<details class="tech"><summary>worst losses with entry context</summary><div class="body"><table class="tbl"><thead><tr><th class="no-sort">entry</th><th class="no-sort">return</th><th class="no-sort">context</th></tr></thead><tbody>' + au.worst.map(w => '<tr><td class="mono">' + esc(w.entry_time || w.time || '') + '</td><td class="neg num">' + (isNum(w.return_pct) ? (w.return_pct * 100).toFixed(2) + '%' : fmt(w.pnl, 2)) + '</td><td class="muted">' + esc(w.context || w.note || '') + '</td></tr>').join('') + '</tbody></table></div></details>' : '') +
    ((au.modes || []).length ? '<div class="card-sub"><b>Failure modes</b> ' + au.modes.slice(0, 4).map(m => '<span class="tag">' + esc(m.label || m.name) + ' ×' + esc(m.count) + '</span>').join(' ') + '</div>' : '') +
    (au.overfit ? '<div class="card-sub warn">overfit caveat: ' + esc(au.overfit) + '</div>' : '') : empty('no autopsy recorded for this run', '◇'));

  /* experiments */
  const exps = lab.experiments || [];
  mount('lab-exp-meta', exps.length ? (exps.length + ' experiments recorded') : '');
  table('lab-exp', 'lab-exp', 'lab-exp-bar', 'lab-exp-pager', {
    rows: exps, pageSize: 14, sort: 'fitness', dir: 'desc', rowId: r => r.key, empty: 'no experiments recorded for this run',
    searchKeys: ['family', 'origin', 'reason', 'rationale'],
    filters: [{key:'origin', label:'origin', options: Array.from(new Set(exps.map(e => e.origin).filter(Boolean))).map(o => ({value:o, label:o}))},
              {key:'gate', label:'gate', options:[{value:'keep',label:'KEEP'},{value:'drop',label:'drop'}]}],
    filterFn: (e, f) => (!f.origin || e.origin === f.origin) && (!f.gate || (f.gate === 'keep' ? e.passed : !e.passed)),
    columns: [
      {key:'generation', label:'gen', tip:'Which generation of this run evaluated the candidate.', render:v => '<span class="mono muted">g' + esc(v) + '</span>'},
      {key:'family', label:'strategy', tip:'The strategy family the candidate belongs to.', render:v => '<span class="mono">' + esc(v || 'N/D') + '</span>'},
      {key:'origin', label:'origin', tip:'Where the candidate came from: seed, a mutation, a crossover, or a proposal from the local LLM.', render:v => chip(v || 'N/D', v === 'llm' ? 'ai' : (v === 'seed' ? 'muted' : ''))},
      {key:'fitness', label:'fitness', tip:'The optimiser\u2019s score for the candidate over the walk-forward folds. The champion is the highest one that also passed the gate.', align:'right', render:v => '<b class="' + (v >= 0 ? 'ok' : 'bad') + '">' + fmt(v, 3) + '</b>'},
      {key:'delta', label:'Δ baseline', align:'right', render:v => isNum(v) ? ('<span class="' + sgn(v) + '">' + (v >= 0 ? '+' : '') + fmt(v, 3) + '</span>') : 'N/D', tip:'Fitness change against the first candidate of the same family in this run.'},
      {key:'n_trades', label:'trades', tip:'How many trades the candidate took in the evaluation window. Too few and the score is noise; the configured min_trades gate is what makes that explicit.', align:'right', render:v => fmt(v, 0)},
      {key:'max_drawdown', label:'maxDD', tip:'The worst peak-to-trough fall of the candidate\u2019s equity. It must stay under the configured cap to pass the gate.', align:'right', render:v => isNum(v) ? (v * 100).toFixed(1) + '%' : 'N/D'},
      {key:'passed', label:'gate', tip:'KEEP means the candidate cleared the hard gates (enough trades, drawdown inside the cap); drop means a gate refused it, whatever its fitness was.', render:v => v ? chip('KEEP', 'ok') : chip('drop', 'bad')},
      {key:'rationale', label:'hypothesis', tip:'What this candidate was trying to test \u2014 written by the search or the model before it ran, not after seeing the result.', render:v => '<span class="muted">' + esc((v || 'N/D').slice(0, 90)) + '</span>'}
    ],
    detail: e => '<div class="grid"><div class="col-6">' + kv([
      ['fitness', fmt(e.fitness, 4)],
      ['metric mean / std', fmt(e.metric_mean, 4) + ' / ' + fmt(e.metric_std, 4)],
      ['gate reason', esc(e.reason || 'ok')],
      ['complexity', fmt(e.complexity, 0) + ' active knobs']
    ]) + '</div><div class="col-6">' + ((e.params_changed || []).length
      ? '<div class="mono" style="font-size:11.5px">' + e.params_changed.map(p => '<div>' + esc(p.key) + ': <span class="muted">' + esc(p.from == null ? 'unset' : (typeof p.from === 'number' ? p.from.toFixed(4) : p.from)) + '</span> → <b>' + esc(typeof p.to === 'number' ? p.to.toFixed(4) : p.to) + '</b></div>').join('') + '</div><div class="card-sub">changed since the first ' + esc(e.family) + ' candidate in this run</div>'
      : '<span class="muted">no baseline to diff against</span>') + '</div></div>' + (e.is_champion ? '<div class="card-sub ai">★ champion of this run — its holdout read is the out-of-sample row in the comparison</div>' : '')
  });

  /* per-generation lessons */
  const gens = lab.generations || [];
  mount('lab-gen', gens.length ? '<div class="list">' + gens.slice().reverse().map(g => '<div class="row"><span class="when">generation ' + esc(g.generation) + '</span><span class="chip ' + (g.kept ? 'info' : 'muted') + '">' + (g.kept || 0) + ' kept</span><span class="body">' + esc(g.lesson || '') + ' <span class="muted">best ' + fmt(g.best_fitness, 3) + '</span></span></div>').join('') + '</div>' : empty('no per-generation lessons recorded', '◇'));
}

/* =============================== AGENT CENTER ============================= */
const AGENT_STATES = {running:'Running', waiting:'Waiting', completed:'Completed', failed:'Failed', unavailable:'Unavailable'};
/* Where each row's state actually comes from.  An expansion that only repeated the row
   would be decoration; this says which endpoint was read to decide it. */
const AGENT_SOURCES = {
  'Evolver (research loop)': '/api/session (the loop pid, status and generation counters) plus the tail of state/loop.log - the pid decides, not the state file',
  'Proposer': '/api/lab (the run\u2019s proposal origins: llm / deterministic / seed)',
  'Backtest evaluator': '/api/lab (the run\u2019s candidates and per-candidate gate verdicts)',
  'Critic': '/api/lab (the critic report stored with the run)',
  'Risk gate': '/api/risk (the sealed caps against the live session) and /api/logs (risk_events)',
  'Paper rail (broker)': '/api/risk (the paper session) and /api/trades (its fills)',
  'Market data feed': '/api/market (keyless public Binance klines)',
  'Binance rail (key probe)': '/api/binance (the read-only key report; it never sends an order)',
  'Alert channel': '/api/alerts (the channel state and what it actually sent)'
};
function agentState(kind){ return chip(AGENT_STATES[kind] || kind, {running:'ok', waiting:'warn', completed:'info', failed:'bad', unavailable:'muted'}[kind] || 'muted'); }
async function renderAgents(){
  const [se, risk, alerts, key, mkt, lab, logs, h] = await Promise.all([
    loopStatus(), safe(api('/api/risk')), safe(api('/api/alerts')), S.key ? null : safe(api('/api/binance')),
    S.marketData ? null : safe(api('/api/market?symbol=' + encodeURIComponent(S.market.symbol) + '&interval=' + encodeURIComponent(S.market.interval) + '&bars=60')),
    safe(api('/api/lab')), safe(api('/api/logs?limit=100')), safe(api('/api/health'))
  ]);
  if(key) S.key = key; if(mkt) S.marketData = mkt;
  const l = (se && se.loop) || {};
  const keyr = S.key || {};
  const al = alerts || S.alerts || {};
  const riskr = risk || S.risk || {};
  const riskEvents = (logs && logs.risk_events) || [];
  const calls = (logs && logs.api_calls) || [];
  const blocked = riskEvents.filter(e => e.kind === 'order_blocked').length;
  const apiErr = calls.filter(c => !c.ok).length;
  const run = lab && lab.runs && lab.runs[0];

  const agents = [];
  agents.push({name:'Evolver (research loop)', role:'Seed → evaluate → elite → propose; writes the report', dep:'Data feed + evaluator',
    state: l.running ? 'running' : (l.status === 'paused' ? 'waiting' : (l.pid ? 'failed' : 'waiting')),
    detail: l.running ? ('pid ' + (l.pid || '?') + ' · ' + ((l.generations_done != null ? l.generations_done : 0) + '/' + (l.generations_total || 0)) + ' generations') : 'no tracked loop running',
    last: l.started_at ? ('started ' + dt(l.started_at)) : 'idle', runtime: l.running ? dur(Date.now()/1000 - (l.started_at || Date.now()/1000)) : 'N/D', errors: 0, res:'N/D'});
  if(run && lab.proposals){
    agents.push({name:'Proposer', role:'Bounded genome proposals (LLM or deterministic)', dep:'Evolver',
      state: 'completed', detail: 'llm ' + (lab.proposals.llm||0) + ' · deterministic ' + (lab.proposals.deterministic||0) + ' · seed ' + (lab.proposals.seed||0) + (lab.llm_available ? ' · Ollama available' : ' · Ollama off'),
      last: 'run #' + run.id, runtime: 'N/D', errors: 0, res:'N/D'});
  } else agents.push({name:'Proposer', role:'Bounded genome proposals (LLM or deterministic)', dep:'Evolver', state:'unavailable', detail:'no run recorded', last:'—', runtime:'N/D', errors:0, res:'N/D'});
  agents.push({name:'Backtest evaluator', role:'Walk-forward folds, gate verdict per candidate', dep:'Data feed',
    state: run ? 'completed' : 'unavailable', detail: run ? (run.candidates + ' candidates, ' + run.passed + ' passed the gate') : 'no candidate evaluated',
    last: run ? ('run #' + run.id) : '—', runtime:'N/D', errors:0, res:'N/D'});
  agents.push({name:'Critic', role:'Judges deployment against the sealed limits', dep:'Holdout read',
    state: (lab && lab.critic) ? 'completed' : 'unavailable', detail: (lab && lab.critic) ? ((lab.critic.approved ? 'approved' : 'refused') + ' — ' + String(lab.critic.text || '').slice(0, 80)) : 'not run',
    last: run ? ('run #' + run.id) : '—', runtime:'N/D', errors:0, res:'N/D'});
  agents.push({name:'Risk gate', role:'Refuses orders over the sealed caps', dep:'Kill switch + account',
    state: riskr.ok ? 'completed' : 'unavailable', detail: blocked + ' blocked order(s) on record' + (riskr.would_arm ? ' · a limit is breached now' : ''),
    last: riskEvents[0] ? dt(riskEvents[0].created_at) : '—', runtime:'N/D', errors: blocked, res:'N/D'});
  agents.push({name:'Paper rail (broker)', role:'Fills champion orders on the simulator', dep:'Risk gate + market data',
    state: (riskr.session && riskr.session.status === 'active') ? 'running' : (riskr.session ? 'completed' : 'unavailable'),
    detail: riskr.session ? ('session #' + riskr.session.session_id + ' · ' + (riskr.session.points || 0) + ' equity points') : 'no session',
    last: riskr.session && riskr.session.as_of ? dt(riskr.session.as_of) : '—', runtime:'N/D', errors:0, res:'N/D'});
  agents.push({name:'Market data feed', role:'Public Binance klines for the live chart', dep:'data-api.binance.vision',
    state: (S.marketData && S.marketData.ok) ? 'running' : 'failed', detail: (S.marketData && S.marketData.ok) ? (S.marketData.symbol + ' ' + S.marketData.interval + ' · ' + (S.marketData.candles||[]).length + ' candles · ' + S.marketData.latency_ms + ' ms') : ((S.marketData && S.marketData.error) || 'not fetched'),
    last: (S.marketData && S.marketData.fetched_at) ? esc(S.marketData.fetched_at) : '—', runtime:'N/D', errors:0, res:'N/D'});
  agents.push({name:'Binance rail (key probe)', role:'Read-only key health; never sends an order', dep:'API key',
    state: keyr.ok ? 'running' : (keyr.verdict === 'not checked' ? 'unavailable' : (keyr.verdict ? 'failed' : 'unavailable')), detail: (keyr.verdict || 'not checked') + ' · ' + (keyr.http ? (keyr.http.calls + ' call(s), ' + keyr.http.posts + ' POST') : 'no probe'),
    last: keyr.checked_at ? esc(keyr.checked_at) : '—', runtime:'N/D', errors: (keyr.problems || []).length, res:'N/D'});
  const ch = (al && al.channel) || {};
  agents.push({name:'Alert channel', role:'Carries halts and warnings to Telegram', dep:'Environment variables',
    state: ch.ready ? 'running' : (ch.reason === 'notify.disabled' ? 'waiting' : 'failed'), detail: ch.ready ? 'ready' : (ch.reason || 'unknown') + (ch.channel ? ' (' + ch.channel + ')' : ''),
    last: al && al.items && al.items[0] ? esc(al.items[0].at) : '—', runtime:'N/D', errors:0, res:'N/D'});

  const running = agents.filter(a => a.state === 'running').length;
  const totalErrors = agents.reduce((n, a) => n + (a.errors || 0), 0) + apiErr;
  mount('ag-tiles', [
    tile('Agents running', running + ' / ' + agents.length, 'from live evidence', '', 'How many listed subsystems report a live state right now. Each one\u2019s evidence is in the table below: a process, a database row or a configuration flag, never a guess.'),
    tile('Loop', esc(loopLabel(se)), l.pid ? ('pid ' + l.pid) : 'no tracked process', l.running ? 'ai' : '', 'The tracked research loop: whether a process is running, and its pid when this dashboard started it. Pause and stop are refused when nothing is running.'),
    tile('Last run', run ? ('#' + run.id) : 'N/D', run ? (run.candidates + ' candidates · ' + run.passed + ' passed') : 'no run recorded', '', 'The most recent recorded evolution run, with how many candidates it evaluated and how many passed the hard gates.'),
    tile('Errors on record', fmt(totalErrors, 0), blocked + ' blocked order(s) · ' + apiErr + ' failed call(s)', totalErrors ? 'warn' : 'ok', 'Blocked orders (risk gate) and failed provider calls (api_calls), read from the state database.')
  ].join(''));
  mount('ag-meta', dt(Date.now()/1000) + ' · resources are not instrumented (N/D)');
  mount('ag-loop-meta', l.running
    ? ('loop running · pid ' + (l.pid || '?') + ' · ' + ((l.generations_done || 0) + '/' + (l.generations_total || 0)) + ' generations')
    : ('no tracked loop running' + (l.pid ? ' · the state file still names pid ' + l.pid : '') +
       ' · `start` launches it, `pause`/`resume`/`stop` are refused while none is running'));

  table('agents', 'ag-table', 'ag-bar', 'ag-pager', {
    rows: agents, pageSize: 20, sort: null, empty: 'no agents',
    rowId: a => 'agent-' + a.name,
    detail: a => kv([
      ['role', esc(a.role)],
      ['depends on', esc(a.dep)],
      ['state', agentState(a.state)],
      ['what it is doing', esc(a.detail)],
      ['last activity', esc(a.last)],
      ['runtime', esc(a.runtime)],
      ['errors', fmt(a.errors, 0)],
      ['resources', esc(a.res)]
    ]) + '<div class="card-sub"><b>Derived from</b> ' + esc(AGENT_SOURCES[a.name] || 'no instrumented source') +
      '. CPU, RAM and per-agent timings are not measured by this backend, which is why those cells read <span class="mono">N/D</span> instead of a guess.</div>',
    searchKeys: ['name', 'role', 'dep', 'detail'],
    filters: [{key:'state', label:'state', options: Object.keys(AGENT_STATES).map(k => ({value:k, label:AGENT_STATES[k]}))}],
    filterFn: (a, f) => !f.state || a.state === f.state,
    columns: [
      {key:'name', label:'agent', tip:'The subsystem, with the role it plays in the pipeline underneath it.', sortable:false, render:(v, r) => '<b>' + esc(v) + '</b><div class="muted" style="font-size:11px">' + esc(r.role) + '</div>'},
      {key:'state', label:'state', tip:'Derived from live evidence, never a fixed label: running means a process or a fresh heartbeat, waiting means it is idle but configured, unavailable means it cannot run here.', sortable:false, render:v => agentState(v)},
      {key:'detail', label:'what it is doing', tip:'The one line that explains the state: which process, which file, which verdict the state was read from.', sortable:false, render:v => '<span class="muted">' + esc(v) + '</span>'},
      {key:'dep', label:'depends on', tip:'What must be present for this subsystem to work \u2014 the earlier stage whose output it consumes.', sortable:false, render:v => '<span class="tag">' + esc(v) + '</span>'},
      {key:'last', label:'last activity', tip:'When this subsystem last did something observable, from the database or the loop log.', sortable:false, render:v => '<span class="mono" style="font-size:11.5px">' + esc(v) + '</span>'},
      {key:'runtime', label:'runtime', tip:'Time the last run of this subsystem took, where the backend measures it. N/D means it is not instrumented \u2014 a dash, not zero.', sortable:false, align:'right', render:v => '<span class="mono">' + esc(v) + '</span>'},
      {key:'errors', label:'errors', tip:'Recorded failures for this subsystem: blocked orders for the risk gate, failed calls for the data feed.', sortable:false, align:'right', render:v => v ? '<span class="bad">' + v + '</span>' : '<span class="muted">0</span>'},
      {key:'res', label:'resources', tip:'CPU and RAM are not instrumented by this backend, so this reads N/D rather than a guess.', sortable:false, align:'right', render:v => '<span class="muted">' + esc(v) + '</span>'}
    ]
  });
  const depTip = 'What this stage needs before it can run, and what it hands to the next one. A stage is only available when the stage it depends on is available \u2014 that is the whole point of listing them.';
  mount('ag-deps', kv([
    ['Data feed', 'feeds the evaluator, the live chart and (read-only) the key probe', depTip],
    ['Evolver', 'depends on the data feed and the backtest evaluator', depTip],
    ['Proposer / evaluator', 'only run inside the Evolver', depTip],
    ['Critic', 'reads the single holdout and the sealed limits', depTip],
    ['Paper rail', 'depends on the risk gate and the kill switch', depTip],
    ['Key probe / alert channel', 'read-only / outbound-only; neither can place an order', depTip]
  ]));
}

/* =============================== BACKTESTING ============================== */
function tradeStats(trades){
  const t = trades || [];
  const wins = t.filter(x => x.pnl > 0), losses = t.filter(x => x.pnl <= 0);
  const gp = wins.reduce((s, x) => s + x.pnl, 0), gl = Math.abs(losses.reduce((s, x) => s + x.pnl, 0));
  return {n: t.length, wins: wins.length, losses: losses.length,
    winRate: t.length ? wins.length / t.length : null,
    profitFactor: gl > 0 ? gp / gl : (gp > 0 ? Infinity : null),
    avgWin: wins.length ? gp / wins.length : null, avgLoss: losses.length ? -(gl / losses.length) : null,
    expectancy: t.length ? t.reduce((s, x) => s + x.pnl, 0) / t.length : null};
}
/* "Run a backtest" is the Backtesting page's own function: it asks the server to
   evaluate one genome on one window with the real engine and cost model.  The answer is
   cached in S.bt so the 15 s tick redraws the view without re-running anything, and the
   page never claims a number it did not receive. */
function btChoiceLabel(kind, id, label){
  return '<option value="' + esc(kind + ':' + id) + '">' + esc(label) + '</option>';
}
function btChoices(hallItems, versions){
  let out = '';
  for(const h of (hallItems || [])){
    out += btChoiceLabel('hall', h.id, 'hall #' + h.id + ' · ' + (h.family || '?') + ' · fit ' + fmt(h.fitness, 3));
  }
  for(const v of (versions || []).slice().reverse()){
    out += btChoiceLabel('version', v.id, 'run #' + v.run_id + ' v' + v.version + ' · ' + (v.family || '?') +
      ' · fit ' + fmt(v.fitness, 3) + (v.outcome === 'champion' ? ' ★ champion' : ''));
  }
  return out;
}
function btQuery(){
  const sel = $('bt-genome');
  const kind = String((sel && sel.value) || '').split(':')[0];
  const id = String((sel && sel.value) || '').split(':')[1];
  const window = ($('bt-window') && $('bt-window').value) || 'folds';
  if(!sel || !sel.value || id == null) return null;
  const key = kind === 'hall' ? 'hall_id=' : (kind === 'version' ? 'version_id=' : null);
  if(!key) return null;
  return key + encodeURIComponent(id) + '&window=' + encodeURIComponent(window);
}
async function runBacktest(){
  const q = btQuery();
  const status = $('bt-run-status');
  if(!q){ if(status) status.textContent = 'pick a strategy first'; return; }
  const btn = $('bt-run');
  if(btn){ btn.disabled = true; btn.textContent = 'running…'; }
  if(status) status.textContent = 'computing on the local dataset…';
  const t0 = Date.now();
  try {
    S.bt.result = await j('/api/backtest?' + q);
    if(status) status.textContent = 'done in ' + ((Date.now() - t0) / 1000).toFixed(1) + ' s';
  } catch(e){
    S.bt.result = {ok: false, error: (e && e.message) || 'request failed'};
    if(status) status.textContent = 'failed';
  }
  if(btn){ btn.disabled = false; btn.textContent = 'run backtest'; }
  renderBacktestResult();
}
function renderBacktestResult(){
  const card = $('bt-run-card');
  const res = S.bt.result;
  if(!card) return;
  if(!res){ card.style.display = 'none'; return; }
  card.style.display = '';
  if(!res.ok){
    mount('bt-res-meta', '');
    mount('bt-res-tiles', '');
    mount('bt-res-detail', '');
    mount('bt-res-src', '');
    const box = $('bt-res-tiles');
    if(box) box.innerHTML = errBox('the backtest could not be run: ' + (res.error || 'unknown error'));
    return;
  }
  const w = res.window || {}, cmp = res.comparison || null, prov = res.provenance || {}, ds = prov.dataset || {};
  const metric = res.metric || 'metric';
  const vals = (res.folds || []).map(f => f[metric]);
  mount('bt-res-meta', esc(res.description || '') + ' · ' + esc(w.name || '') + ' · ' + fmt(w.bars, 0) + ' bars');
  mount('bt-res-tiles', [
    tile(w.name === 'folds' ? 'Fitness (folds)' : (metric + ' (' + esc(w.name || '') + ')'), fmt(res.fitness, 4),
      (res.folds || []).length ? ((res.folds || []).length + ' folds') : 'single window',
      isNum(res.fitness) ? sgn(res.fitness) : '',
      'fitness = mean(metric on the test slice) - complexity penalty - 0.5 * std. On the folds window this is exactly the number the search optimises.'),
    tile('Trades', fmt(res.n_trades, 0), 'closed in the window'),
    tile('Max drawdown', isNum(res.max_drawdown) ? (res.max_drawdown * 100).toFixed(2) + '%' : 'N/D', 'worst fold / whole window'),
    tile('Gate', res.passed == null ? 'n/a' : (res.passed ? chip('pass', 'ok') : chip('fail', 'bad')),
      esc(res.gate_reason || 'ok'), res.passed === false ? 'neg' : '',
      'The gate (min trades, drawdown cap) is a walk-forward verdict; on a single holdout window it is not applied and reads n/a.'),
    tile('Runtime', fmt(res.runtime_ms, 0) + ' ms', 'computed on this machine'),
    tile('Window', esc(w.name || 'N/D'), fmt(w.bars, 0) + ' bars · ' + esc((w.first || '').slice(0, 10)) + ' → ' + esc((w.last || '').slice(0, 10)))
  ].join(''));
  const pts = (res.equity || []).map(v => ({equity: v}));
  if($('bt-res-equity')){
    if(pts.length > 1) drawEquity($('bt-res-equity'), pts, {baseline: (res.costs || {}).initial_cash});
    else note($('bt-res-equity').getContext('2d'), $('bt-res-equity').width, $('bt-res-equity').height, 'no equity curve for this window');
  }
  const c = res.costs || {};
  mount('bt-res-detail', kv([
    ['folds', vals.length ? vals.map(v => fmt(v, 3)).join(' · ') : 'N/D', 'The metric on each walk-forward fold. Reading them together is the point: one great fold and three poor ones is not a strategy.'],
    ['equity points', fmt(pts.length, 0) + (res.equity_step > 1 ? ' (every ' + res.equity_step + 'th bar)' : ''), 'How many points the equity curve was recorded at. A larger step means a coarser curve.'],
    ['fee / slippage', fmt(c.fee_bps, 1) + ' / ' + fmt(c.slippage_bps, 1) + ' bps per side', 'The cost model this evaluation ran with: fee and slippage per side, in basis points of the notional.'],
    ['round trip', fmt(c.round_trip_bps, 1) + ' bps', 'What one complete trade pays in costs before it can profit.'],
    ['position size', isNum(c.position_size) ? (c.position_size * 100).toFixed(0) + '% of equity' : 'N/D', 'The share of equity each position used in this evaluation, subject to the sealed 5% cap.'],
    ['provenance', prov.repro_id ? ('<span class="mono">' + esc(prov.repro_id) + '</span> · commit ' + esc(prov.commit_short || 'N/D') + (prov.dirty ? ' ' + chip('dirty tree', 'warn') : '')) : 'N/D', 'The code state the result belongs to: the reproduction id and the commit. A dirty tree means the working copy had uncommitted changes when it ran.'],
    ['dataset', ds.sha256 ? ('<span class="mono">' + esc(String(ds.short || ds.sha256).slice(0, 12)) + '</span> · ' + esc(ds.method || '') + (isNum(ds.rows) ? ' · ' + fmt(ds.rows, 0) + ' rows' : '')) : 'N/D', 'Which price data was measured on, identified by a hash of the file \u2014 so a different dataset is visible rather than assumed.']
  ]) + (cmp ? ('<div class="banner ' + (cmp.matches ? '' : 'bad') + '"><span class="ico">' + (cmp.matches ? '✓' : '✕') + '</span><div><b>' +
      (cmp.matches ? 'The recorded fitness reproduces' : 'It does not reproduce') + '</b> — ' + esc(cmp.note || '') + '<br>stored <span class="mono">' + fmt(cmp.stored_fitness, 6) + '</span> vs computed <span class="mono">' + fmt(res.fitness, 6) + '</span>' +
      (isNum(cmp.delta) ? (' (Δ ' + (cmp.delta >= 0 ? '+' : '') + cmp.delta.toExponential(2) + ')') : '') +
      '<br>dataset: ' + (cmp.dataset_matches == null ? 'unknown' : (cmp.dataset_matches ? 'same digest as that run' : chip('different from that run', 'warn'))) +
      ' · repro id ' + (cmp.current_repro_id ? esc(cmp.current_repro_id) : 'N/D') + (cmp.stored_repro_id && cmp.stored_repro_id !== cmp.current_repro_id ? ' (that run: ' + esc(cmp.stored_repro_id) + ')' : '') +
      '</div></div>') : ''));
  mount('bt-res-src', 'Measured on ' + esc((w.source || 'N/D')) + ' ' + esc((w.symbol || '')) + ' ' + esc((w.timeframe || '')) +
    ' · window “' + esc(w.name || '') + '” (' + esc(w.label || '') + ') · ' + fmt(w.bars, 0) + ' bars · ' +
    (w.in_sample ? 'in-sample: the search also saw this window' : 'out of sample for the search') +
    '. Nothing was written: no row, no report, no order.');
}
async function renderBacktest(){
  const [hall, eq, tr, paper, lab] = await Promise.all([safe(api('/api/hall')), safe(api('/api/equity')), safe(api('/api/trades')), safe(api('/api/paper')), safe(api('/api/lab'))]);
  /* the run selector: the hall's genomes and the recorded versions of the selected run */
  const sel = $('bt-genome');
  if(sel){
    const items = ((hall && hall.items) || []);
    const versions = (lab && lab.versions) || [];
    const keep = S.bt.sel || sel.value;
    const html = btChoices(items, versions);
    if(sel.innerHTML !== html){ sel.innerHTML = html || '<option value="">nothing recorded to evaluate</option>'; }
    if(keep && Array.from(sel.options).some(o => o.value === keep)) sel.value = keep;
    S.bt.sel = sel.value;
  }
  if($('bt-window') && S.bt.window) $('bt-window').value = S.bt.window;
  mount('bt-run-meta', (lab && lab.repro && lab.repro.repro_id) ? ('current dataset ' + esc(String(lab.repro.dataset_short || '').slice(0, 12)) + ' · commit ' + esc(lab.repro.commit_short || 'N/D')) : '');
  const btn = $('bt-run');
  if(btn) btn.disabled = !($('bt-genome') && $('bt-genome').value);
  renderBacktestResult();
  const pts = (eq && eq.points) || [];
  const trades = (tr && tr.items) || [];
  const st = tradeStats(trades);
  const sess = (paper && paper.session) || null;
  const mdd = pts.length ? maxDrawdown(pts) : null;
  const ret = sess ? sess.total_return : null;
  const champ = ((hall && hall.items) || [])[0];
  const oos = champ ? (champ.metrics_oos || {}).metrics || {} : {};

  mount('bt-tiles', [
    tile('Out-of-sample metric', oos.sharpe != null ? fmt(oos.sharpe, 3) : 'N/D', champ ? ('champion ' + esc(champ.family)) : 'no hall entry', oos.sharpe != null ? sgn(oos.sharpe) : '', 'The champion\u2019s Sharpe measured on the single untouched holdout window, not on the folds it was selected on.'),
    tile('Total return', ret != null ? pct(ret * 100, 2) : (oos.total_return != null ? pct(oos.total_return * 100, 2) : 'N/D'), 'paper session vs initial cash', sgn(ret != null ? ret : oos.total_return), 'Change of the recorded session against its starting cash, after fees and slippage. It is a result, not a forecast.'),
    tile('Max drawdown', mdd != null ? fmt(mdd, 2) + '%' : (oos.max_drawdown != null ? fmt(oos.max_drawdown * 100, 2) + '%' : 'N/D'), 'from the recorded equity curve', mdd > 15 ? 'neg' : '', 'Peak-to-trough on the recorded equity curve. The sealed cap is 15%; the champion is refused for exceeding it.'),
    tile('Profit factor', st.profitFactor != null ? (isFinite(st.profitFactor) ? fmt(st.profitFactor, 3) : '∞') : (oos.profit_factor != null ? fmt(oos.profit_factor, 3) : 'N/D'), 'gross win / gross loss', st.profitFactor != null && st.profitFactor < 1 ? 'neg' : '', 'Sum of winning trades divided by the absolute sum of losing trades.'),
    tile('Win rate', st.winRate != null ? (st.winRate * 100).toFixed(1) + '%' : (oos.win_rate != null ? (oos.win_rate * 100).toFixed(1) + '%' : 'N/D'), st.wins + ' wins / ' + st.losses + ' losses', '', 'How often the trades closed in profit. On its own it says nothing: a high win rate with a poor profit factor still loses money.'),
    tile('Trades', fmt(st.n || oos.n_trades, 0), 'closed in the session', '', 'How many trades the number above is based on, out of the session that was recorded. Few trades mean a score that is mostly noise.')
  ].join(''));

  if($('bt-equity')) drawEquity($('bt-equity'), pts, {baseline: sess ? sess.initial_cash : null, xlabels: pts.map(p => dt(p.ts).slice(5, 10))});
  mount('bt-eq-meta', pts.length ? (dt(pts[0].ts) + ' → ' + dt(pts[pts.length - 1].ts)) : 'no session');
  const win = sess ? (sess.window || '') : '';
  mount('bt-eq-src', sess ? ('Source: paper session #' + sess.session_id + ' (' + esc(sess.name) + '), window “' + esc(win || 'N/D') + '” on ' + esc(S.market.symbol) + ' · ' + esc((paper.session && paper.session.timeframe) || 'configured timeframe') + '. Data nature: simulated fills on recorded market bars — not a live account.') : 'Source: N/D — no paper session recorded.');
  if($('bt-dd')) drawUnderwater($('bt-dd'), pts);
  mount('bt-dd-meta', mdd != null ? ('max ' + fmt(mdd, 2) + '%') : 'no session');

  const items = (hall && hall.items) || [];
  mount('bt-strat-meta', items.length ? (items.length + ' hall champion(s), ranked by fitness') : '');
  table('bt-strat', 'bt-strategies', 'bt-strat-bar', 'bt-strat-pager', {
    rows: items, pageSize: 10, sort: 'fitness', dir: 'desc', empty: 'no hall-of-fame entry recorded',
    searchKeys: ['family'],
    columns: [
      {key:'id', label:'#', tip:'The row\u2019s id in the local database \u2014 stable, and the thing to quote when you ask about this record.', render:v => '<span class="mono muted">' + esc(v) + '</span>'},
      {key:'family', label:'strategy', render:v => '<span class="mono">' + esc(v || 'N/D') + '</span>'},
      {key:'fitness', label:'fitness', align:'right', tip:'The optimiser objective on the walk-forward folds.', render:v => fmt(v, 3)},
      {key:'oos', label:'OOS ' + (lab && lab.metric ? esc(lab.metric) : 'metric'), align:'right', tip:'Measured on the single holdout read.', sortValue:r => ((r.metrics_oos||{}).metrics||{}).sharpe, render:(v, r) => { const m = ((r.metrics_oos||{}).metrics||{}).sharpe; return m != null ? '<b class="' + (m>=0?'ok':'bad') + '">' + fmt(m, 3) + '</b>' : 'N/D'; }},
      {key:'oos_ret', label:'OOS return', align:'right', render:(v, r) => { const x = ((r.metrics_oos||{}).metrics||{}).total_return; return x != null ? '<span class="' + sgn(x) + '">' + pct(x * 100, 2) + '</span>' : 'N/D'; }},
      {key:'oos_dd', label:'OOS maxDD', align:'right', render:(v, r) => { const x = ((r.metrics_oos||{}).metrics||{}).max_drawdown; return x != null ? fmt(x * 100, 2) + '%' : 'N/D'; }},
      {key:'oos_pf', label:'profit factor', align:'right', tip:'Gross wins over gross losses on the holdout.', render:(v, r) => { const x = ((r.metrics_oos||{}).metrics||{}).profit_factor; return x != null ? fmt(x, 3) : 'N/D'; }},
      {key:'oos_wr', label:'win rate', align:'right', render:(v, r) => { const x = ((r.metrics_oos||{}).metrics||{}).win_rate; return x != null ? (x * 100).toFixed(1) + '%' : 'N/D'; }},
      {key:'oos_n', label:'OOS trades', align:'right', render:(v, r) => { const x = ((r.metrics_oos||{}).metrics||{}).n_trades; return x != null ? fmt(x, 0) : 'N/D'; }}
    ]
  });

  const settings = (sess && sess.settings) || {};
  mount('bt-costs', kv([
    ['fee (per side)', isNum(settings.fee_bps) ? (settings.fee_bps + ' bps') : 'N/D', 'Taker fee charged on each fill, in basis points of the traded notional. It is paid twice per round trip.'],
    ['slippage (per side)', isNum(settings.slippage_bps) ? (settings.slippage_bps + ' bps') : 'N/D', 'How much worse than the quoted price a fill is assumed to be, per side. It is the model\u2019s estimate of the cost of actually getting filled.'],
    ['round trip', (isNum(settings.fee_bps) && isNum(settings.slippage_bps)) ? ((settings.fee_bps + settings.slippage_bps) * 2 + ' bps') : 'N/D', 'Fee plus slippage, both sides: what one complete trade pays before it has made anything.'],
    ['position size', isNum(settings.position_size) ? (settings.position_size * 100 + '% of equity') : 'N/D', 'The fraction of equity the research engine puts into a position, before the sealed 5% per-position cap clamps it on the paper rail.'],
    ['shorts allowed', settings.allow_short ? 'yes' : 'no', 'Whether the strategy may take short positions at all. Off means every trade is long.'],
    ['timeframe', esc(settings.timeframe || 'N/D'), 'The candle interval the session was measured on, from the data it read.']
  ]) + '<div class="card-sub">Costs are the model the session was run with (config-derived, not re-measured here). The paper rail is clamped to the sealed 5% per position, so the effective size can differ from the research size above.</div>');

  const folds = (lab && lab.holdout && lab.holdout.fold_values) || [];
  const hold = lab && lab.holdout ? lab.holdout.metric_value : null;
  mount('bt-folds-meta', folds.length ? (folds.length + ' folds') : 'no folds recorded');
  if($('bt-folds')){
    const bars = folds.map((v, i) => ({label: 'f' + (i + 1), value: v}));
    if(hold != null) bars.push({label: 'hold', value: hold});
    drawBars($('bt-folds'), bars, {zeroLine: true, label: true, yfmt: v => fmt(v, 2)});
  }
}

/* ================================== MEMORY ================================ */
/* The two cards on this page are about the same records, so they drive each other:
   clicking a run scopes the lessons table to that run's lessons (null = all of them). */
function setRunFilter(runId){
  S.memRun = (runId == null || runId === '') ? null : Number(runId);
  renderMemory().catch(() => {});
}
/* Hand a hall genome to the Backtesting page's own function instead of re-implementing
   an evaluation here: one backtest path, chosen from two places. */
function backtestHall(id){
  S.bt.sel = 'hall:' + id;
  S.bt.result = null;
  setView('backtest');
}
async function renderMemory(){
  const [ov, les, hist, hall] = await Promise.all([safe(api('/api/overview')), safe(api('/api/lessons')), safe(api('/api/history')), safe(api('/api/hall'))]);
  const s = (ov && ov.stats) || {};
  const lessons = (les && les.items) || [];
  const withEmb = lessons.filter(l => l.embedding && l.embedding.length).length;
  mount('mem-stats', [
    tile('Runs', fmt(s.runs, 0), 'evolution cycles recorded', '', 'How many complete evolution runs the database holds. Each one is a cycle the AI Lab can be read in depth from.'),
    tile('Candidates', fmt(s.candidates, 0), 'genomes evaluated', '', 'Every genome the search has built and scored across all runs, passing or not.'),
    tile('Lessons', fmt(s.lessons, 0), withEmb + ' with embeddings', '', 'What the program wrote down for later runs; the count with embeddings is how many are also searchable by similarity.'),
    tile('Hall of fame', fmt(s.hall_of_fame, 0), s.best_fitness != null ? ('best fitness ' + fmt(s.best_fitness, 3)) : 'no entry', '', 'The recorded best genomes, ranked by the evaluation metric. These feed the backtest page\u2019s strategy list.'),
    tile('Paper trades', fmt(s.paper_trades, 0), 'across ' + fmt(s.paper_sessions, 0) + ' session(s)', '', 'Closed simulated fills across every forward paper session, and how many sessions produced them.')
  ].join(''));

  const tags = Array.from(new Set(lessons.flatMap(l => (l.tags || []).map(t => String(t).split(':')[0])))).sort();
  const scoped = S.memRun ? lessons.filter(l => l.run_id === S.memRun) : lessons;
  /* Clicking a run scopes this table to that run's lessons: the two cards on this page
     are about the same thing, so they are wired to each other instead of sitting apart. */
  mount('mem-les-scope', S.memRun
    ? (chip('run #' + S.memRun + ' only', 'info') + ' <button class="btn sm ghost" onclick="setRunFilter(null)">show all runs</button>')
    : '<span class="muted">all runs</span>');
  mount('mem-les-meta', lessons.length
    ? (scoped.length + (S.memRun ? ' lesson(s) of run #' + S.memRun : ' lesson(s)') + ' · ' + withEmb + ' with embeddings')
    : '');
  table('mem-lessons', 'mem-lessons', 'mem-les-bar', 'mem-les-pager', {
    rows: lessons, pageSize: 12, sort: 'id', dir: 'desc', rowId: r => 'les' + r.id, empty: 'no lessons recorded',
    searchKeys: ['text', 'tags'],
    filters: [{key:'tag', label:'tag', options: tags.map(t => ({value:t, label:t}))}],
    /* the scope lives in S so a redraw (or the 15 s tick) cannot silently drop it */
    filterFn: (l, f) => (!f.tag || (l.tags || []).some(t => String(t).split(':')[0] === f.tag)) &&
      (!S.memRun || l.run_id === S.memRun),
    columns: [
      {key:'id', label:'#', tip:'The row\u2019s id in the local database \u2014 stable, and the thing to quote when you ask about this record.', render:v => '<span class="mono muted">' + esc(v) + '</span>'},
      {key:'text', label:'lesson', tip:'The sentence the program stored to carry into later runs \u2014 what it learned, written at the moment it learned it.', render:v => esc(v)},
      {key:'tags', label:'tags', tip:'The labels that decide when this lesson is recalled.', render:v => (v || []).map(t => '<span class="tag">' + esc(t) + '</span>').join(' ') || '<span class="muted">N/D</span>'},
      {key:'run_id', label:'run', tip:'The evolution run this lesson came out of, so a claim can always be traced back to the run that produced it.', render:v => v != null ? ('#' + esc(v)) : '<span class="muted">—</span>'},
      {key:'embedding', label:'emb', tip:'Whether the lesson has an embedding vector and how many dimensions it has \u2014 that is what makes it searchable by similarity later.', render:v => (v && v.length) ? chip(String(v.length) + 'd', 'ai') : '<span class="muted">none</span>'},
      {key:'created_at', label:'when', tip:'When the row was written to the memory database.', render:v => '<span class="mono" style="font-size:11.5px">' + dt(v) + '</span>'}
    ],
    detail: l => '<div class="muted" style="white-space:pre-wrap">' + esc(l.text) + '</div>' + (l.question ? '<div class="card-sub">question: ' + esc(l.question) + ' · target: ' + esc(l.target || 'N/D') + '</div>' : '')
  });

  table('mem-runs', 'mem-runs', null, null, {
    rows: (hist && hist.items) || [], pageSize: 8, sort: 'id', dir: 'desc', empty: 'no runs recorded',
    columns: [
      {key:'id', label:'#', render:v => '<span class="mono">' + esc(v) + '</span>'},
      {key:'outcome', label:'outcome', tip:'How the run ended: ok means it completed its generations, stopped means an operator stopped it at a boundary, error means it failed.', render:v => chip(v || 'N/D', v === 'ok' ? 'ok' : (v === 'stopped' ? 'warn' : 'muted'))},
      {key:'note', label:'note', tip:'The run\u2019s own closing note \u2014 what it was progressing towards when it ended.', render:v => '<span class="muted">' + esc(v || '') + '</span>'},
      {key:'ts', label:'when', tip:'When the run was recorded.', render:v => '<span class="mono" style="font-size:11.5px">' + dt(v) + '</span>'},
      {key:'scope', label:'', sortable:false, render:(v, r) => '<button class="btn sm ' + (S.memRun === r.id ? 'primary' : 'ghost') + '" onclick="event.stopPropagation();setRunFilter(' + esc(r.id) + ')" data-tip="Filters the lessons table above to this run only, so you can see what it specifically learned.">' + (S.memRun === r.id ? 'showing' : 'lessons') + '</button>'}
    ],
    detail: r => kv([
      ['run', '#' + esc(r.id)],
      ['outcome', esc(r.outcome || 'N/D')],
      ['note', esc(r.note || '—')],
      ['started', esc(dt(r.ts))]
    ]) + (r.repro_id ? ('<div class="card-sub">reproducibility id <span class="mono">' + esc(r.repro_id) + '</span></div>') : '') +
      '<div class="toolbar"><button class="btn sm" onclick="event.stopPropagation();setRunFilter(' + esc(r.id) + ')">scope the lessons above to this run</button></div>'
  });

  const items = (hall && hall.items) || [];
  mount('mem-hall-meta', items.length ? (items.length + ' entries · expand a row for its genome') : '');
  table('mem-hall', 'mem-hall', null, null, {
    rows: items, pageSize: 8, sort: 'fitness', dir: 'desc', rowId: h => 'hall' + h.id,
    empty: 'no hall entry recorded',
    columns: [
      {key:'id', label:'#', tip:'The row\u2019s id in the local database \u2014 stable, and the thing to quote when you ask about this record.', render:v => '<span class="mono muted">' + esc(v) + '</span>'},
      {key:'family', label:'strategy', tip:'The strategy family of the recorded champion.', render:v => '<span class="mono">' + esc(v || 'N/D') + '</span>'},
      {key:'fitness', label:'fit', tip:'The fitness the champion was recorded with on the walk-forward folds.', align:'right', render:v => '<b class="' + (v >= 0 ? 'ok' : 'bad') + '">' + fmt(v, 3) + '</b>'},
      {key:'created_at', label:'when', tip:'When this genome was recorded in the hall of fame.', render:v => '<span class="mono" style="font-size:11px">' + esc(dt(v)) + '</span>'}
    ],
    detail: h => '<div class="grid"><div class="col-5">' + kv([
      ['run', h.run_id != null ? ('#' + esc(h.run_id)) : 'N/D'],
      ['fitness (folds)', fmt(h.fitness, 4)],
      ['holdout sharpe', ((h.metrics_oos || {}).metrics || {}).sharpe != null ? fmt(((h.metrics_oos || {}).metrics || {}).sharpe, 3) : 'N/D'],
      ['knobs', (h.genome && h.genome.params) ? (Object.keys(h.genome.params).length + ' active') : 'N/D'],
      ['recorded', esc(dt(h.created_at))]
    ]) + '</div><div class="col-7"><pre class="log">' + esc(JSON.stringify(h.genome || {}, null, 1)) + '</pre></div></div>' +
      '<div class="toolbar"><button class="btn sm" onclick="event.stopPropagation();backtestHall(' + esc(h.id) + ')">evaluate on the Backtesting page</button>' +
      '<span class="muted">opens this genome in the Backtesting view\u2019s own run control</span></div>'
  });
}

/* ================================== LOGS ================================== */
/* The row budget is a page setting, not a constant: a busy state file can hold thousands
   of calls, and the operator decides how much to pull.  The follow toggle re-reads the
   tail on its own while this view is open. */
function setLogLimit(v){
  const n = parseInt(v, 10);
  S.logLimit = (isFinite(n) && n >= 10) ? n : 200;
  const sel = $('log-limit'); if(sel) sel.value = String(S.logLimit);
  renderLogs().catch(() => {});
}
function toggleFollow(){
  S.logFollow = !S.logFollow;
  const b = $('log-follow-btn');
  if(b){ b.className = 'btn sm' + (S.logFollow ? ' primary' : ''); b.textContent = S.logFollow ? 'following' : 'follow'; }
  const m = $('log-follow-msg');
  if(m) m.textContent = S.logFollow ? 'following: the tail re-reads every 5 s and scrolls to the newest line' : 'follow re-reads the tail every few seconds and keeps the newest line in view';
  if(S.logFollow) scheduleFollow(0);
}
function scheduleFollow(delay){
  if(!S.logFollow) return;
  setTimeout(async () => {
    if(!S.logFollow || S.view !== 'logs') return;
    try { await renderLogs(); } catch(e){}
    scheduleFollow(5000);
  }, delay || 5000);
}
function copyLoopLog(){
  const pre = $('log-loop') && $('log-loop').querySelector('pre');
  const text = pre ? pre.textContent : '';
  const msg = $('log-follow-msg');
  const done = ok => { if(msg) msg.textContent = ok ? ('loop log copied (' + text.split('\n').length + ' lines)') : 'nothing to copy — no loop log yet, or the clipboard is unavailable'; };
  if(!text){ done(false); return; }
  if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(text).then(() => done(true), () => done(false)); }
  else done(false);
}
async function renderLogs(){
  const sel = $('log-limit'); if(sel) sel.value = String(S.logLimit);
  let lg; try { lg = await j('/api/logs?limit=' + S.logLimit); } catch(e){ mount('log-events', errBox('could not load logs: ' + e.message)); return; }
  const ev = lg.risk_events || [], calls = lg.api_calls || [], loop = lg.loop_log || {};
  const blocked = ev.filter(e => e.kind === 'order_blocked').length;
  const failed = calls.filter(c => !c.ok).length;
  mount('log-updated', 'updated ' + new Date().toLocaleTimeString() + ' · up to ' + S.logLimit + ' rows per table');
  mount('log-tiles', [
    tile('Risk events', fmt(ev.length, 0), blocked + ' blocked order(s)', blocked ? 'warn' : '', 'Every refusal the risk gate recorded, plus kill-switch actions — the same rows the CLI\u2019s `risk`/`switch` read.'),
    tile('Provider calls', fmt(calls.length, 0), failed + ' failed', failed ? 'bad' : 'ok', 'Every request the Binance rail made (or the MT5 rail, when used), with status, code and latency. No key or signature is stored here.'),
    tile('Loop log', fmt((loop.tail || []).length, 0), loop.exists ? 'lines shown' : 'no log file', '', 'The tail of the tracked loop\u2019s output (state/loop.log).')
  ].join(''));

  mount('log-ev-meta', ev.length ? (ev.length + ' newest events') : '');
  table('log-ev', 'log-events', 'log-ev-bar', 'log-ev-pager', {
    rows: ev, pageSize: 12, sort: 'id', dir: 'desc', rowId: r => 'ev' + r.id, empty: 'no risk events recorded',
    searchKeys: ['kind', 'reason'],
    filters: [{key:'kind', label:'kind', options: Array.from(new Set(ev.map(e => e.kind).filter(Boolean))).map(k => ({value:k, label:k}))}],
    filterFn: (e, f) => !f.kind || e.kind === f.kind,
    columns: [
      {key:'id', label:'#', tip:'The row\u2019s id in the local database \u2014 stable, and the thing to quote when you ask about this record.', render:v => '<span class="mono muted">' + esc(v) + '</span>'},
      {key:'created_at', label:'when', render:v => '<span class="mono" style="font-size:11.5px">' + dt(v) + '</span>'},
      {key:'kind', label:'kind', tip:'What happened: a blocked order (the pre-trade gate refusing), a kill-switch action, or a halt/warning from the limits watcher.', render:v => chip(v || 'N/D', String(v).indexOf('kill_switch') === 0 ? 'bad' : (String(v).indexOf('blocked') >= 0 ? 'warn' : 'info'))},
      {key:'reason', label:'reason', tip:'The gate\u2019s own words for the refusal or the halt \u2014 which limit was read, what it saw and what it therefore refused.', render:v => esc(v)},
      {key:'run_id', label:'run', tip:'The evolution run this event belongs to, when it came from one.', render:v => v != null ? ('#' + esc(v)) : '<span class="muted">—</span>'}
    ],
    detail: e => '<pre class="log">' + esc(JSON.stringify(e.detail, null, 2) || 'null') + '</pre>'
  });

  mount('log-call-meta', calls.length ? (calls.length + ' newest calls') : '');
  table('log-call', 'log-calls', 'log-call-bar', 'log-call-pager', {
    rows: calls, pageSize: 12, sort: 'id', dir: 'desc', rowId: r => 'call' + r.id, empty: 'no provider calls recorded',
    searchKeys: ['provider', 'method', 'path', 'msg', 'code'],
    filters: [{key:'ok', label:'result', options:[{value:'ok',label:'ok'},{value:'fail',label:'failed'}]},
              {key:'provider', label:'provider', options: Array.from(new Set(calls.map(c => c.provider).filter(Boolean))).map(p => ({value:p, label:p}))}],
    filterFn: (c, f) => (!f.ok || (f.ok === 'ok' ? c.ok : !c.ok)) && (!f.provider || c.provider === f.provider),
    columns: [
      {key:'id', label:'#', tip:'The row\u2019s id in the local database \u2014 stable, and the thing to quote when you ask about this record.', render:v => '<span class="mono muted">' + esc(v) + '</span>'},
      {key:'created_at', label:'when', render:v => '<span class="mono" style="font-size:11.5px">' + dt(v) + '</span>'},
      {key:'provider', label:'provider', tip:'Which external service was called \u2014 the data feed, the Binance rail or the MT5 bridge.', render:v => esc(v)},
      {key:'method', label:'method', tip:'The HTTP method of the call: GET for reads, POST/DELETE for the few write paths an exchange exposes.', render:v => '<span class="mono">' + esc(v) + '</span>'},
      {key:'path', label:'path', tip:'The endpoint that was called. Query strings are recorded as they were sent; no key or signature is stored here.', render:v => '<span class="mono">' + esc(v) + '</span>'},
      {key:'status', label:'status', tip:'The HTTP status the provider answered with. A dash means the call never got an answer.', align:'right', render:v => v == null ? '<span class="muted">—</span>' : esc(v)},
      {key:'ok', label:'result', tip:'Whether the call succeeded. Failures are retried where the client considers them retryable, and each attempt is a row.', render:v => v ? chip('ok', 'ok') : chip('failed', 'bad')},
      {key:'code', label:'code', tip:'The provider\u2019s own error code, when it sent one \u2014 the number to quote when the API rejects a request.', render:v => v ? ('<span class="mono">' + esc(v) + '</span>') : '<span class="muted">—</span>'},
      {key:'latency_ms', label:'latency', tip:'Round-trip time of the call in milliseconds. N/D means it was not measured.', align:'right', render:v => v == null ? 'N/D' : (fmt(v, 0) + ' ms')}
    ],
    detail: c => '<div class="grid"><div class="col-6"><div class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:.05em">request params</div><pre class="log">' + esc(JSON.stringify(c.params, null, 2) || '{}') + '</pre></div><div class="col-6"><div class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:.05em">response</div><pre class="log">' + esc(typeof c.response === 'string' ? c.response : JSON.stringify(c.response, null, 2) || 'null') + '</pre></div></div>' + (c.msg ? '<div class="card-sub">' + esc(c.msg) + '</div>' : '')
  });

  mount('log-loop-meta', loop.path ? (esc(loop.path) + (loop.truncated ? ' · tail' : '')) : '');
  mount('log-loop', loop.exists && (loop.tail || []).length ? '<pre class="log">' + esc(loop.tail.join('\n')) + '</pre>' : empty(loop.exists ? 'the loop log is empty' : 'no loop log yet — start the loop from the header', '▤'));
  if(S.logFollow){
    const pre = $('log-loop') && $('log-loop').querySelector('pre');
    if(pre) pre.scrollTop = pre.scrollHeight;
  }
}

/* ================================ SETTINGS ================================ */
function card(id, title, body, tip){ return '<div class="card col-6" id="' + id + '"><div class="card-h"><h2' + (tip ? ' data-tip="' + esc(tip) + '"' : '') + '>' + esc(title) + '</h2></div><div class="card-b">' + body + '</div></div>'; }
function fmtVal(v){ if(v == null) return 'N/D'; if(typeof v === 'boolean') return v ? 'true' : 'false'; if(Array.isArray(v)) return v.length ? esc(v.join(', ')) : 'none'; if(typeof v === 'object') return esc(JSON.stringify(v)); return esc(v); }
/* The seal verification: three independent checks, each reported with the evidence it
   was decided on.  A page that only echoed the digest would be telling the operator what
   the number is, not whether it still holds - so this reads /api/settings' verification
   block (a pure read: unlike the CLI it never writes the seal file). */
function sealCheck(name, ok, evidence){
  const verdict = ok === true ? 'ok' : (ok === false ? 'FAIL' : 'unknown');
  const tip = 'Check: ' + name + ' \u2014 result ' + verdict + '. Evidence: ' + evidence.replace(/<[^>]+>/g, '');
  return '<tr data-tip="' + esc(tip) + '"><td>' + esc(name) + '</td><td class="' + (ok === true ? 'ok' : (ok === false ? 'bad' : 'muted')) + '">' +
    verdict + '</td><td class="muted">' + evidence + '</td></tr>';
}
function renderSeal(v){
  if(!v) return '<span class="muted">the seal was not verified by this server</span>';
  const disk = v.on_disk || {};
  const rows = [
    sealCheck('shipped values match the pinned digest', v.pinned,
      'digest <span class="mono">' + esc(String(v.digest || '').slice(0, 16)) + '…</span> vs pin <span class="mono">' + esc(String(v.pin || '').slice(0, 16)) + '…</span>'),
    sealCheck('model-side levers are closed', v.levers_closed,
      v.levers_closed ? 'no config key, genome or environment variable can reach these caps' : esc(v.levers_error || 'a lever is open')),
    sealCheck('matches the seal already on disk', v.matches_disk,
      disk.exists ? ('<span class="mono">' + esc(v.file || 'seal') + '</span> sealed ' + esc(disk.sealed_at || 'N/D')) : 'no seal file yet — the CLI writes it on the first `risk verify`')
  ].join('');
  return '<div class="banner ' + (v.ok ? '' : 'bad') + '"><span class="ico">' + (v.ok ? '✓' : '✕') + '</span><div>' + esc(v.note || '') + '</div></div>' +
    '<table class="tbl" style="margin-top:8px"><thead><tr>' +
      '<th class="no-sort" data-tip="One of the three read-only checks over the sealed limits.">check</th>' +
      '<th class="no-sort" data-tip="ok means the check passed, FAIL means it did not and the page says so, unknown means this server could not decide \u2014 which is not the same as a pass.">result</th>' +
      '<th class="no-sort" data-tip="What the verdict was decided on: the digests compared, the levers probed, the seal file on disk.">evidence</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table>' +
    '<div class="card-sub">Verified server-side and read-only: unlike <span class="mono">python -m selftrade risk verify</span>, this GET never writes the seal file. Path <span class="mono">' + esc(v.path || 'N/D') + '</span>.</div>';
}
async function renderSettings(){
  let s; try { s = await j('/api/settings'); } catch(e){ mount('set-body', errBox('could not load settings: ' + e.message)); pwMsg('unavailable: ' + e.message, 'bad'); return; }
  S.settings = s;
  pwRender();
  const sealed = s.sealed || {};
  const ver = sealed.verification || {};
  mount('set-tiles', [
    tile('Version', esc(s.version || 'N/D'), esc(s.project || 'selftrade'), '', 'The version of the program answering this page, from the package itself.'),
    tile('Execution mode', chip('paper', 'info'), 'no real orders anywhere', '', 'Which order rail is active. Paper means fills happen on the simulator; the sealed risk gate stands in front of it either way.'),
    tile('Live trading', chip('disabled', 'ok'), 'opt-in and gated, off by default', '', 'Whether the build can send a real order. It cannot: this is an explicit, gated switch rather than an absence of code.'),
    tile('Sealed digest', '<span class="mono" style="font-size:14px">' + esc(String(sealed.digest || '').slice(0, 16)) + '…</span>', sealed.pinned ? chip('pinned', 'ok') : chip('NOT pinned', 'bad'), '', 'A SHA-256 over the shipped risk limits. It is compared against the pin on every verification, so a changed cap cannot pass unnoticed.'),
    tile('Seal verification', ver.ok ? chip('verifies', 'ok') : chip('does NOT verify', 'bad'),
      'pinned ' + (ver.pinned ? 'yes' : 'no') + ' · levers ' + (ver.levers_closed ? 'closed' : 'OPEN') + ' · disk ' + (ver.matches_disk == null ? 'no file' : (ver.matches_disk ? 'matches' : 'differs')),
      ver.ok ? '' : 'bad', 'Three read-only checks: the shipped caps against the pinned SHA-256, the model-side levers, and the seal file already on disk. A failure here means do not deploy until it is explained.')
  ].join(''));
  /* Every settings row explains itself: the dotted path it lives under, and the fact that
     this page reports the configuration rather than writing it. A bare `strategy: rsi`
     is exactly the kind of row a reader has to hover. */
  const SEC_TIPS = {
    paths: 'Where this installation keeps its state: the database, the session file, the loop log, the reports directory and the seal file.',
    data: 'Where prices come from and what shape they must have. The same loader feeds the backtest, the paper rail and the live chart, so a bad frame is refused in one place.',
    backtest: 'The engine\u2019s assumptions: starting cash, fees and slippage per side, the fraction of equity a position may use, and whether shorting is allowed at all.',
    evaluation: 'How a candidate is scored and what it must survive: the metric, the walk-forward folds, the untouched holdout, the minimum number of trades and the drawdown cap.',
    evolution: 'The search itself: how many generations and candidates, how many elites carry over, the seed that makes the run reproducible, and how far it may mutate.',
    paper: 'The forward paper session: which window it walks, how many bars, and how often it advances a step.',
    funding: 'The policy on simulated capital: whether crediting is allowed, in which currency, and the largest single credit and total the account will accept.',
    risk_gate: 'The pre-trade gate: the risk fraction per order, the maximum positions and leverage, whether a stop is required, and the minimum spacing between orders.',
    dashboard: 'This page itself: where it binds, whether a login is required, the name of the password environment variable, the stored-password file and the refresh cadences.',
    llm: 'The optional local model (Ollama). It proposes bounded genome changes only: it never writes trading logic and never places an order.',
    notify: 'The optional risk alert channel. Only the NAMES of its environment variables are shown here \u2014 the values are never read back.',
    binance: 'The Binance Testnet rail: switched off by default, with the read-only probe above as the only thing this build does with a key.',
    mt5: 'The MetaTrader 5 bridge: disabled by default, and it refuses to attach to anything that is not a demo account.',
    rea: 'The optional reverse-engineering evidence tooling. Dry run by default; it is not part of any trading path.'
  };
  const secTip = (name, k) => (SEC_TIPS[name] || 'A configuration group read from config.yaml.') +
    '\n\nKey: ' + name + '.' + k + ' \u2014 reported here as it is configured; change it in config.yaml (or with a --set override), never on this page.';
  const sec = (name, obj) => card('set-' + name, name,
    obj ? kv(Object.keys(obj).map(k => [k, fmtVal(obj[k]), secTip(name, k)])) : '<span class="muted">N/D</span>',
    SEC_TIPS[name]);
  const limits = sealed.limits || {};
  mount('set-body',
    sec('paths', s.paths) + sec('data', s.data) + sec('backtest', s.backtest) + sec('evaluation', s.evaluation) +
    sec('evolution', s.evolution) + sec('paper', s.paper) + sec('funding', s.funding) +
    sec('risk_gate', s.risk_gate) +
    '<div class="card col-12" id="set-sealed"><div class="card-h"><h2 data-tip="The caps that cannot be moved: a 5% position, a 5% hard stop, a 2% daily loss and a 15% drawdown. They live in risk_rules.py, are SHA-256 pinned, and no config key, genome or environment variable can reach them.">sealed limits (read-only)</h2><span class="spacer"></span><span class="muted" id="set-sealed-chip"></span></div><div class="card-b">' +
      kv(Object.keys(limits).map(k => [k, fmtVal(limits[k]),
        'Sealed: ' + k + ' in risk_rules.py. SHA-256 pinned and reachable by no config key, genome or environment variable \u2014 the model side cannot widen it.' + (k === 'MODEL_CAN_OVERRIDE' ? ' This is the flag itself: false means exactly that.' : '')])) +
      '<div class="card-sub">These caps live in <span class="mono">risk_rules.py</span>, are SHA-256 pinned and reachable by no config key, genome or environment variable. Digest <span class="mono">' + esc(String(sealed.digest || '').slice(0, 12)) + '…</span>.</div>' +
      '<div class="card-h" style="padding-left:0;padding-right:0"><h2 style="font-size:12.5px" data-tip="Three independent read-only checks: the shipped caps against the pinned SHA-256, the model-side levers, and the seal file already on disk. A failure here means do not deploy until it is explained.">verification</h2></div>' + renderSeal(ver) +
    '</div></div>' +
    sec('dashboard', s.dashboard) + sec('llm', s.llm) + sec('notify', s.notify) + sec('binance', s.binance) + sec('mt5', s.mt5) + sec('rea', s.rea)
  );
  mount('set-sealed-chip', ver.ok
    ? chip('verified', 'ok', false, 'All three checks passed: the shipped caps match the pin, no model-side lever reaches them, and the seal on disk agrees. Re-run `python -m selftrade risk verify` to write or refresh that seal file.')
    : chip('NOT verified', 'bad', false, 'At least one check failed. Do not deploy until it is explained: it means the limits could have moved, or the seal file disagrees with the shipped ones.'));
}

/* ================================== boot ================================== */
window.addEventListener('hashchange', () => { const v = (location.hash || '').replace('#/', ''); if(VIEWS.indexOf(v) >= 0) setView(v); });
window.addEventListener('resize', () => redrawAll());
$('pw').addEventListener('keydown', e => { if(e.key === 'Enter') doLogin(); });
boot();
</script>
</body>
</html>
"""
