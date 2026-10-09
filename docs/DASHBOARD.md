# Dashboard

A local, loopback-only dashboard served by the Python standard library.

```powershell
.\dashboard.ps1
# or: python -m selftrade serve --host 127.0.0.1 --port 8787
```

It binds `127.0.0.1:8787` by default (`dashboard.host` / `dashboard.port`; the
host must be a loopback address). There are **no remote assets and no
telemetry** — the page is a single inline HTML document.

## Page

Panels: overview (run/candidate/hall counts, champion), the paper session, the
paper equity curve drawn on a `<canvas>`, the hall of fame, recent experiments,
paper trades, lessons and run history. Buttons post to `/api/control`.

## JSON API

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/health` | version and database reachability |
| GET | `/api/overview` | counts, champion, last run |
| GET | `/api/equity` | paper equity points |
| GET | `/api/trades` | paper trades |
| GET | `/api/paper` | paper session summary |
| GET | `/api/experiments` | recent candidates (genome, origin, fitness, gate) |
| GET | `/api/hall` | hall of fame |
| GET | `/api/lessons` | lessons (incl. REA evidence) |
| GET | `/api/history` | run history |
| GET | `/api/session` | session control state |
| POST | `/api/control` | `{"action":"start\|pause\|resume\|stop"}` |

`POST /api/control` writes the request into `state/session.json`; the running
loop picks it up at its next generation boundary. The dashboard itself does not
run or stop the process.

Example:

```powershell
curl.exe http://127.0.0.1:8787/api/health
curl.exe -X POST http://127.0.0.1:8787/api/control -H "Content-Type: application/json" -d "{\"action\":\"pause\"}"
```

## Security

Loopback binding only. No secrets are exposed: the API never returns passwords,
tokens or environment values.
