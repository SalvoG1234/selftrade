"""Local dashboard: a stdlib ``http.server`` on loopback with a JSON API.

No remote assets, no telemetry.  Endpoints: ``/api/health``, ``/api/overview``,
``/api/equity``, ``/api/trades``, ``/api/experiments``, ``/api/hall``,
``/api/lessons``, ``/api/history``, ``/api/session`` and ``POST /api/control``.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .memory import Memory
from .session import Session

HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>selftrade dashboard</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0;
         background: #0e1116; color: #d7dde5; }
  header { padding: 14px 20px; border-bottom: 1px solid #222a35; display:flex; gap:16px; align-items:center;}
  h1 { font-size: 16px; margin: 0; font-weight: 600; letter-spacing: .5px; }
  main { padding: 18px 20px; display: grid; gap: 18px; grid-template-columns: repeat(auto-fit, minmax(320px,1fr)); }
  .card { background: #141a22; border: 1px solid #222a35; border-radius: 8px; padding: 14px 16px; }
  .card h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .8px; color: #8b98a8; margin: 0 0 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 4px 6px; border-bottom: 1px solid #1e252f; }
  .ok { color: #5dd39e; } .bad { color: #ef6b6b; } .muted { color: #8b98a8; }
  button { background:#1d2632; color:#d7dde5; border:1px solid #2c3745; border-radius:6px;
           padding:6px 12px; cursor:pointer; font-family:inherit; font-size:12px; }
  button:hover { background:#243040; }
  canvas { width: 100%; height: 180px; }
  .full { grid-column: 1 / -1; }
</style>
</head>
<body>
<header>
  <h1>selftrade</h1>
  <span class="muted" id="health">loading…</span>
  <span style="flex:1"></span>
  <span class="muted" id="session">session: —</span>
  <button onclick="control('start')">start</button>
  <button onclick="control('pause')">pause</button>
  <button onclick="control('resume')">resume</button>
  <button onclick="control('stop')">stop</button>
</header>
<main>
  <div class="card"><h2>Overview</h2><div id="overview" class="muted">…</div></div>
  <div class="card"><h2>Paper session</h2><div id="paper" class="muted">…</div></div>
  <div class="card full"><h2>Paper equity</h2><canvas id="curve" width="900" height="180"></canvas></div>
  <div class="card"><h2>Hall of fame</h2><div id="hall" class="muted">…</div></div>
  <div class="card"><h2>Recent experiments</h2><div id="experiments" class="muted">…</div></div>
  <div class="card"><h2>Paper trades</h2><div id="trades" class="muted">…</div></div>
  <div class="card"><h2>Lessons</h2><div id="lessons" class="muted">…</div></div>
  <div class="card"><h2>Run history</h2><div id="history" class="muted">…</div></div>
</main>
<script>
const REFRESH = __REFRESH__ * 1000;
async function j(u){ const r = await fetch(u); return await r.json(); }
function esc(s){ return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
async function refresh(){
  const h = await j('/api/health');
  document.getElementById('health').textContent = `v${h.version} — db ok=${h.db_ok}`;
  const ov = await j('/api/overview');
  document.getElementById('overview').innerHTML =
    `runs <b>${ov.stats.runs}</b> · candidates <b>${ov.stats.candidates}</b> · hall <b>${ov.stats.hall_of_fame}</b><br>` +
    `champion: <span class="${ov.champion?'ok':'muted'}">${ov.champion?esc(ov.champion.description):'none'}</span><br>` +
    `best fitness: ${ov.stats.best_fitness ?? '—'}`;
  const p = await j('/api/paper');
  document.getElementById('paper').innerHTML = p.session ?
    `#${p.session.session_id} ${esc(p.session.name)} <span class="muted">(${p.session.status})</span><br>` +
    `cursor ${p.session.cursor}/${p.session.total_bars} · trades ${p.session.n_trades} · ` +
    `return <span class="${p.session.total_return>=0?'ok':'bad'}">${(p.session.total_return*100).toFixed(2)}%</span>` : 'no paper session';
  const eq = await j('/api/equity');
  drawCurve(eq.points.map(x=>x.equity));
  const hall = await j('/api/hall');
  document.getElementById('hall').innerHTML = rows(hall.items, r =>
    `#${r.id} fit ${Number(r.fitness).toFixed(3)} <span class="muted">${esc(r.family)}</span>`);
  const ex = await j('/api/experiments');
  document.getElementById('experiments').innerHTML = rows(ex.items.slice(0,12), r =>
    `g${r.generation} <span class="muted">${esc(r.family)}</span> fit ${Number(r.fitness).toFixed(3)} ` +
    `<span class="${r.passed?'ok':'bad'}">${r.passed?'KEEP':'drop'}</span>`);
  const tr = await j('/api/trades');
  document.getElementById('trades').innerHTML = rows(tr.items.slice(-12).reverse(), r =>
    `${esc(r.direction)} ${esc(r.exit_reason)} ` +
    `<span class="${r.pnl>=0?'ok':'bad'}">${Number(r.pnl).toFixed(2)}</span>`);
  const le = await j('/api/lessons');
  document.getElementById('lessons').innerHTML = rows(le.items.slice(0,6), r => esc(r.text));
  const hi = await j('/api/history');
  document.getElementById('history').innerHTML = rows(hi.items.slice(0,8), r =>
    `#${r.id} ${esc(r.outcome)} <span class="muted">${esc(r.note||'')}</span>`);
  const se = await j('/api/session');
  document.getElementById('session').textContent =
    `session: ${se.status} ${se.generations_done}/${se.generations_total}`;
}
function rows(items, fn){
  if(!items || !items.length) return '<span class="muted">none</span>';
  return '<table>' + items.map(x => '<tr><td>' + fn(x) + '</td></tr>').join('') + '</table>';
}
function drawCurve(vals){
  const c = document.getElementById('curve'); const ctx = c.getContext('2d');
  const W = c.width, H = c.height; ctx.clearRect(0,0,W,H);
  if(vals.length < 2){ ctx.fillStyle='#8b98a8'; ctx.fillText('no equity yet', 10, 20); return; }
  const min = Math.min(...vals), max = Math.max(...vals), span = (max-min)||1;
  ctx.strokeStyle = '#3f8cff'; ctx.lineWidth = 1.5; ctx.beginPath();
  vals.forEach((v,i)=>{ const x = i/(vals.length-1)*W; const y = H-(v-min)/span*(H-16)-8;
    i?ctx.lineTo(x,y):ctx.moveTo(x,y); });
  ctx.stroke();
}
async function control(action){
  await fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action})});
  refresh();
}
refresh(); setInterval(refresh, REFRESH);
</script>
</body>
</html>
"""


def _overview(memory: Memory) -> dict[str, Any]:
    champion = memory.champion()
    stats = memory.stats()
    last = memory.last_run()
    return {
        "stats": stats,
        "champion": (
            {"family": champion["family"], "fitness": champion["fitness"], "description": _describe(champion["genome"])}
            if champion
            else None
        ),
        "last_run": {"id": last["id"], "outcome": last["outcome"], "note": last["note"]} if last else None,
    }


def _describe(genome: dict[str, Any]) -> str:
    from .genome import describe

    try:
        return describe(genome)
    except Exception:  # pragma: no cover
        return str(genome.get("family"))


def _paper_block(memory: Memory) -> dict[str, Any]:
    session = memory.active_paper_session()
    if session is None:
        return {"session": None}
    trades = memory.paper_trades(session["id"])
    equity = memory.paper_equity(session["id"])
    initial = float(session["initial_cash"]) or 1.0
    final = float(session["cash"])
    return {
        "session": {
            "session_id": session["id"],
            "name": session["name"],
            "status": session["status"],
            "cursor": session["cursor"],
            "total_bars": session["total_bars"],
            "n_trades": len([t for t in trades]),
            "initial_cash": session["initial_cash"],
            "cash": session["cash"],
            "total_return": final / initial - 1.0,
            "genome": session["genome"],
        }
    }


def api_get(settings: Any, path: str) -> tuple[int, Any]:
    memory = Memory(settings.memory.db)
    if path == "/api/health":
        try:
            Memory(settings.memory.db).stats()
            db_ok = True
        except Exception:  # pragma: no cover
            db_ok = False
        return 200, {"status": "ok", "version": __version__, "db": settings.memory.db, "db_ok": db_ok}
    if path == "/api/overview":
        return 200, _overview(memory)
    if path == "/api/equity":
        session = memory.active_paper_session()
        pts = memory.paper_equity(session["id"]) if session else []
        return 200, {"session_id": session["id"] if session else None, "points": pts}
    if path == "/api/trades":
        session = memory.active_paper_session()
        items = memory.paper_trades(session["id"]) if session else []
        return 200, {"items": items}
    if path == "/api/paper":
        return 200, _paper_block(memory)
    if path == "/api/experiments":
        return 200, {"items": memory.recent_candidates(100)}
    if path == "/api/hall":
        return 200, {"items": memory.hall_of_fame(50)}
    if path == "/api/lessons":
        return 200, {"items": memory.lessons(limit=100)}
    if path == "/api/history":
        return 200, {"items": memory.history(50)}
    if path == "/api/session":
        return 200, Session(settings.session_file).status()
    return 404, {"error": "not found", "path": path}


def api_control(settings: Any, body: dict[str, Any]) -> tuple[int, Any]:
    action = (body or {}).get("action")
    if action not in ("start", "pause", "resume", "stop"):
        return 400, {"error": f"invalid action {action!r}; use start|pause|resume|stop"}
    session = Session(settings.session_file)
    if action == "start":
        generations = body.get("generations") or settings.evolution.generations
        state = session.request_start(int(generations))
    else:
        state = session.request(action)
    return 200, {"ok": True, "action": action, "session": state}


class DashboardHandler(BaseHTTPRequestHandler):
    settings: Any = None
    server_version = "selftrade-dashboard"

    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, code: int, obj: Any, content_type: str = "application/json") -> None:
        if isinstance(obj, str):
            payload = obj.encode("utf-8")
        else:
            payload = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            page = HTML_PAGE.replace("__REFRESH__", str(int(self.settings.dashboard.auto_refresh_s)))
            self._send(200, page, "text/html")
            return
        code, obj = api_get(self.settings, path)
        self._send(code, obj)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid JSON body"})
            return
        if path == "/api/control":
            code, obj = api_control(self.settings, body if isinstance(body, dict) else {})
            self._send(code, obj)
            return
        self._send(404, {"error": "not found", "path": path})


def make_server(settings: Any, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (DashboardHandler,), {"settings": settings})
    return ThreadingHTTPServer((host or settings.dashboard.host, port or settings.dashboard.port), handler)


def serve(settings: Any, host: str | None = None, port: int | None = None) -> None:
    httpd = make_server(settings, host, port)
    addr = httpd.server_address
    print(f"selftrade dashboard on http://{addr[0]}:{addr[1]}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping dashboard")
    finally:
        httpd.server_close()
