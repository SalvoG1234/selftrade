"""What if we had really invested: a money simulation on real prices.

This module answers one question with numbers and charts: *if we had put a fixed
amount of cash behind this genome on this price series, what would the account
have looked like?*

It deliberately reuses :func:`selftrade.backtest.simulate`, so the simulation is
the same engine that the search optimised - there is no second, flattering
accounting path.

What it reports, and why each number is here:

* **net** equity, trades and returns, with ``fee_bps + slippage_bps`` charged on
  every side;
* a **buy & hold** benchmark on the same cash and the same entry cost, because
  that is the alternative a real investor would actually have;
* the **measured cost drag**: the same genome is also run with costs set to zero
  and the difference in final equity is what fees and slippage took away;
* a **cost sensitivity** table (0x, 1x, 2x, 3x the configured costs) so the
  fragility of the edge to trading costs is visible instead of implied;
* a per-calendar-year table and a drawdown series;
* charts as **inline SVG** (stdlib only, no remote assets, no image files).

Honesty rules baked in: the selection bias of the champion is stated in the
report, a negative result is printed as a negative result, and the ``full``
window is labelled in-sample because the genome was chosen on part of it.
"""

from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from .backtest import BacktestResult, compute_metrics, drawdown_series, simulate
from .data import TIMEFRAME_BARS_PER_YEAR, split_holdout
from .genome import describe, validate_genome

WINDOWS = ("holdout", "full", "recent")
DEFAULT_CHART_POINTS = 480
CURRENCY = "USDT"


class InvestError(Exception):
    """Raised when the simulation cannot be built from the given inputs."""


# --------------------------------------------------------------------------
# Window and single simulation
# --------------------------------------------------------------------------


def select_window(df: pd.DataFrame, settings: Any, window: str = "holdout") -> pd.DataFrame:
    """Pick the slice of history the simulation runs on.

    * ``holdout`` - the tail the evolution was forbidden to look at (the honest
      out-of-sample window; equals the whole series when ``holdout_frac`` is 0);
    * ``full`` - every bar available (in-sample for a selected genome);
    * ``recent`` - the last ``paper.window_bars`` bars.
    """
    w = (window or "holdout").lower()
    if w not in WINDOWS:
        raise InvestError(f"unknown window {window!r}; use one of {', '.join(WINDOWS)}")
    if w == "full":
        return df
    if w == "recent":
        n = int(getattr(settings.paper, "window_bars", 0) or 0)
        if n <= 0 or n >= len(df):
            return df
        return df.iloc[-n:]
    _, hold = split_holdout(df, settings.evaluation.holdout_frac)
    return hold if len(hold) else df


def _one_run(
    df: pd.DataFrame,
    genome: dict[str, Any],
    settings: Any,
    *,
    initial_cash: float,
    cost_multiplier: float = 1.0,
) -> BacktestResult:
    bt = settings.backtest
    return simulate(
        df,
        genome,
        initial_cash=float(initial_cash),
        fee_bps=float(bt.fee_bps) * cost_multiplier,
        slippage_bps=float(bt.slippage_bps) * cost_multiplier,
        position_size=float(bt.position_size),
        allow_short=bool(bt.allow_short),
        timeframe=str(settings.data.timeframe),
    )


def buy_hold_series(df: pd.DataFrame, initial_cash: float, cost: float) -> pd.Series:
    """Cash parked in the asset: buy at the first open, hold to the last close.

    The entry pays the same per-side cost as the strategy; no exit cost, because
    nothing is sold.  This is the benchmark a real investor had available.
    """
    entry = float(df["open"].iloc[0]) * (1.0 + float(cost))
    if entry <= 0:
        raise InvestError("first open price is not positive; cannot build buy & hold")
    units = float(initial_cash) / entry
    return (units * df["close"].astype("float64")).astype("float64")


def _cagr(initial: float, final: float, years: float) -> float:
    if years <= 0 or initial <= 0 or final <= 0:
        return 0.0
    return float((final / initial) ** (1.0 / years) - 1.0)


def _series_stats(equity: pd.Series, initial_cash: float, timeframe: str) -> dict[str, float]:
    bpy = float(TIMEFRAME_BARS_PER_YEAR.get(timeframe, 8760.0))
    n = int(len(equity))
    return {
        "final_equity": float(equity.iloc[-1]) if n else float(initial_cash),
        "total_return": (float(equity.iloc[-1]) / initial_cash - 1.0) if n else 0.0,
        "cagr": _cagr(initial_cash, float(equity.iloc[-1]) if n else initial_cash, n / bpy if bpy else 0.0),
        "max_drawdown": float(max(0.0, -drawdown_series(equity).min())) if n else 0.0,
    }


def _exposure(trades: pd.DataFrame, n_bars: int) -> float:
    if not len(trades) or n_bars <= 0:
        return 0.0
    return float(min(1.0, trades["bars_held"].sum() / n_bars))


def _yearly(equity: pd.Series, bh: pd.Series) -> list[dict[str, Any]]:
    """Calendar-year returns for the strategy and for buy & hold."""
    if not len(equity):
        return []
    start_value = float(equity.iloc[0])
    start_bh = float(bh.iloc[0])
    out: list[dict[str, Any]] = []
    for year, grp in equity.groupby(equity.index.year):
        end = float(grp.iloc[-1])
        end_bh = float(bh[bh.index.year == year].iloc[-1])
        out.append(
            {
                "year": int(year),
                "strategy_return": end / start_value - 1.0 if start_value else 0.0,
                "buy_hold_return": end_bh / start_bh - 1.0 if start_bh else 0.0,
                "bars": int(len(grp)),
            }
        )
        start_value, start_bh = end, end_bh
    return out


def sha256_file(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def simulate_investment(
    df: pd.DataFrame,
    genome: dict[str, Any],
    settings: Any,
    *,
    window: str = "holdout",
    initial_cash: float | None = None,
    origin: str = "seed",
    data_path: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Run the money simulation and return a JSON-serialisable report dict."""
    g = validate_genome(genome)
    cut = select_window(df, settings, window)
    if len(cut) < 3:
        raise InvestError(f"window {window!r} has only {len(cut)} bar(s); fetch more data")

    bt = settings.backtest
    cash = float(initial_cash if initial_cash is not None else bt.initial_cash)
    if cash <= 0:
        raise InvestError(f"initial cash must be positive, got {cash}")
    cost = (float(bt.fee_bps) + float(bt.slippage_bps)) / 10000.0
    timeframe = str(settings.data.timeframe)

    result = _one_run(cut, g, settings, initial_cash=cash)
    zeros = _one_run(cut, g, settings, initial_cash=cash, cost_multiplier=0.0)
    bh = buy_hold_series(cut, cash, cost)

    metrics = dict(result.metrics)
    bh_stats = _series_stats(bh, cash, timeframe)
    strat_stats = _series_stats(result.equity, cash, timeframe)
    gross_final = float(zeros.equity.iloc[-1])
    net_final = float(result.equity.iloc[-1])

    sensitivity = []
    for mult in (0.0, 1.0, 2.0, 3.0):
        run = _one_run(cut, g, settings, initial_cash=cash, cost_multiplier=mult)
        sensitivity.append(
            {
                "cost_multiplier": mult,
                "cost_per_side_bps": (float(bt.fee_bps) + float(bt.slippage_bps)) * mult,
                "final_equity": float(run.equity.iloc[-1]),
                "total_return": float(run.equity.iloc[-1]) / cash - 1.0,
                "max_drawdown": float(run.metrics.get("max_drawdown", 0.0)),
                "n_trades": int(run.metrics.get("n_trades", 0)),
            }
        )

    years = len(cut) / float(TIMEFRAME_BARS_PER_YEAR.get(timeframe, 8760.0))

    # Is this window touched by the data the genome was selected on?  The
    # selection region ends where the holdout begins; anything starting before
    # that boundary is (partly) in-sample.
    _, hold = split_holdout(df, settings.evaluation.holdout_frac)
    in_sample = bool(len(hold) == 0 or (len(cut) and cut.index[0] < hold.index[0]))
    sampled = origin in ("sampled", "seed")
    notes = [
        "Simulation on historical prices with the same engine the search used; "
        "no order was sent anywhere.",
        f"Costs: {float(bt.fee_bps) + float(bt.slippage_bps):.1f} bps per side "
        f"({2 * (float(bt.fee_bps) + float(bt.slippage_bps)):.1f} bps round trip), "
        "charged on entry and exit.",
        "Past performance on historical data is not a forecast. Synthetic-data "
        "numbers are labelled as such; these are real market prices.",
    ]
    if in_sample:
        why = {
            "full": "the full window contains the bars the champion was selected on",
            "recent": "this window reaches back into the bars the champion was selected on",
        }.get(window, f"the '{window}' window overlaps the bars the champion was selected on")
        notes.append(f"IN-SAMPLE: {why}, so these numbers are optimistically biased.")
        if sampled:
            notes.append(
                "This genome itself was sampled, not evolved, so the bias comes from "
                "the window rather than from a selection made here."
            )
    else:
        notes.append(
            f"OUT-OF-SAMPLE window ({window}): the evolution read these bars only "
            "once, at the end of the run."
        )
    if not len(result.trades):
        notes.append("The genome never opened a trade on this window: the account stayed in cash.")

    report = {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "currency": CURRENCY,
        "window": window,
        "in_sample": in_sample,
        "origin": origin,
        "genome": g,
        "description": describe(g),
        "params": {
            "initial_cash": cash,
            "fee_bps": float(bt.fee_bps),
            "slippage_bps": float(bt.slippage_bps),
            "cost_per_side_bps": float(bt.fee_bps) + float(bt.slippage_bps),
            "round_trip_bps": 2 * (float(bt.fee_bps) + float(bt.slippage_bps)),
            "position_size": float(bt.position_size),
            "allow_short": bool(bt.allow_short),
        },
        "data": {
            "source": str(settings.data.source),
            "path": data_path or str(settings.data.csv),
            "sha256": sha256_file(data_path or settings.data.csv),
            "symbol": str(settings.data.symbol),
            "timeframe": timeframe,
            "rows_total": int(len(df)),
            "rows_window": int(len(cut)),
            "start": cut.index[0].isoformat(),
            "end": cut.index[-1].isoformat(),
            "years": round(float(years), 3),
            "first_price": float(cut["open"].iloc[0]),
            "last_price": float(cut["close"].iloc[-1]),
        },
        "strategy": {
            **strat_stats,
            "sharpe": float(metrics.get("sharpe", 0.0)),
            "sortino": float(metrics.get("sortino", 0.0)),
            "calmar": float(metrics.get("calmar", 0.0)),
            "n_trades": int(metrics.get("n_trades", 0)),
            "win_rate": float(metrics.get("win_rate", 0.0)),
            "profit_factor": float(metrics.get("profit_factor", 0.0)),
            "avg_trade_return": float(metrics.get("avg_trade_return", 0.0)),
            "exposure": _exposure(result.trades, len(cut)),
            "open_at_end": int(metrics.get("open_at_end", 0.0)),
        },
        "buy_hold": bh_stats,
        "cost_drag": {
            "gross_final_equity": gross_final,
            "net_final_equity": net_final,
            "cost_paid": gross_final - net_final,
            "pct_of_initial": (gross_final - net_final) / cash,
            "gross_return": gross_final / cash - 1.0,
            "net_return": net_final / cash - 1.0,
        },
        "cost_sensitivity": sensitivity,
        "years": _yearly(result.equity, bh),
        "trades": _trades_json(result.trades),
        "equity_curve": _curve_json(result.equity),
        "buy_hold_curve": _curve_json(bh),
        "drawdown_curve": _curve_json(drawdown_series(result.equity)),
        "price_curve": _curve_json(cut["close"]),
        "trade_markers": [
            {
                "entry_time": r["entry_time"].isoformat(),
                "exit_time": r["exit_time"].isoformat(),
                "entry_price": float(r["entry_price"]),
                "exit_price": float(r["exit_price"]),
                "pnl": float(r["pnl"]),
                "direction": r["direction"],
            }
            for _, r in result.trades.iterrows()
        ],
        "notes": notes,
    }
    report["verdict"] = _verdict(report)
    return report


def _verdict(report: dict[str, Any]) -> dict[str, Any]:
    """One-line honest read of the simulation, derived from the numbers."""
    strat = report["strategy"]
    bh = report["buy_hold"]
    diff = strat["total_return"] - bh["total_return"]
    if strat["n_trades"] == 0:
        text = "No trades: cash did nothing."
    elif diff < 0:
        text = (
            f"Underperformed buy & hold by {abs(diff) * 100:.1f} percentage points "
            f"while paying {report['cost_drag']['cost_paid']:,.0f} {report['currency']} in costs."
        )
    else:
        text = f"Beat buy & hold by {diff * 100:.1f} percentage points."
    return {
        "beat_buy_hold": bool(diff > 0),
        "delta_vs_buy_hold": float(diff),
        "text": text,
    }


def _trades_json(trades: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for _, r in trades.iterrows():
        rows.append(
            {
                "direction": str(r["direction"]),
                "entry_time": r["entry_time"].isoformat(),
                "exit_time": r["exit_time"].isoformat(),
                "entry_price": float(r["entry_price"]),
                "exit_price": float(r["exit_price"]),
                "size": float(r["size"]),
                "pnl": float(r["pnl"]),
                "return_pct": float(r["return_pct"]),
                "bars_held": int(r["bars_held"]),
                "exit_reason": str(r["exit_reason"]),
            }
        )
    return rows


def _curve_json(series: pd.Series) -> list[list[Any]]:
    return [[ts.isoformat(), float(v)] for ts, v in series.items()]


# --------------------------------------------------------------------------
# Inline SVG charts (stdlib only)
# --------------------------------------------------------------------------


def _sample_points(series: pd.Series, points: int = DEFAULT_CHART_POINTS) -> list[tuple[float, float]]:
    """Downsample to at most ``points`` (x = fraction of the window, y = value)."""
    n = len(series)
    if n == 0:
        return []
    vals = series.to_numpy(dtype="float64")
    if n <= points:
        xs = np.linspace(0.0, 1.0, n) if n > 1 else np.array([0.0])
        return list(zip(xs.tolist(), vals.tolist()))
    idx = np.linspace(0, n - 1, points).round().astype(int)
    xs = np.linspace(0.0, 1.0, points)
    return list(zip(xs.tolist(), vals[idx].tolist()))


def _money(v: float) -> str:
    return f"{v:,.0f}"


def _pct(v: float, digits: int = 1) -> str:
    return f"{v * 100:+.{digits}f}%"


def svg_line_chart(
    series: Sequence[tuple[str, pd.Series, str]],
    *,
    width: int = 940,
    height: int = 300,
    title: str = "",
    y_label: str = "",
    height_ratio: float = 0.78,
) -> str:
    """Multi-series line chart. ``series`` is a list of (label, data, colour)."""
    pad_l, pad_r, pad_t, pad_b = 78, 16, 30 if title else 12, 26
    plot_w = max(10.0, width - pad_l - pad_r)
    plot_h = max(10.0, height * height_ratio - pad_t - pad_b)
    all_vals: list[float] = []
    prepared: list[tuple[str, list[tuple[float, float]], str]] = []
    for label, data, colour in series:
        pts = _sample_points(data)
        if not pts:
            continue
        prepared.append((label, pts, colour))
        all_vals.extend(v for _, v in pts)
    if not all_vals:
        return ""
    lo, hi = min(all_vals), max(all_vals)
    if hi - lo < 1e-9:
        lo, hi = lo - 1.0, hi + 1.0
    span = hi - lo
    lo -= span * 0.06
    hi += span * 0.06

    def x_of(fx: float) -> float:
        return pad_l + fx * plot_w

    def y_of(v: float) -> float:
        return pad_t + (1.0 - (v - lo) / (hi - lo)) * plot_h

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
        f'aria-label="{html.escape(title or y_label)}" preserveAspectRatio="none">'
    ]
    if title:
        parts.append(
            f'<text x="{pad_l}" y="18" class="chart-title">{html.escape(title)}</text>'
        )
    # horizontal gridlines + y ticks
    for k in range(5):
        v = lo + (hi - lo) * k / 4.0
        y = y_of(v)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w:.1f}" y2="{y:.1f}" '
            'class="grid"/>'
        )
        parts.append(
            f'<text x="{pad_l - 6:.1f}" y="{y + 3.5:.1f}" class="tick tick-y">'
            f"{html.escape(_money(v))}</text>"
        )
    # date ticks (start / middle / end) taken from the longest series
    longest = max((d for _, d, _ in series if len(d)), key=len, default=None)
    if longest is not None and len(longest) > 1:
        for fx, pos in ((0.0, "start"), (0.5, "middle"), (1.0, "end")):
            i = int(round(fx * (len(longest) - 1)))
            label = longest.index[i].strftime("%Y-%m-%d")
            anchor = {"start": "start", "middle": "middle", "end": "end"}[pos]
            parts.append(
                f'<text x="{x_of(fx):.1f}" y="{pad_t + plot_h + 16:.1f}" '
                f'class="tick" text-anchor="{anchor}">{label}</text>'
            )
    for i, (label, pts, colour) in enumerate(prepared):
        d = " ".join(f"{x_of(x):.2f},{y_of(v):.2f}" for x, v in pts)
        parts.append(f'<polyline points="{d}" fill="none" stroke="{colour}" stroke-width="1.8"/>')
        lx = pad_l + 8 + i * 190
        parts.append(
            f'<line x1="{lx}" y1="{pad_t + plot_h + 22:.1f}" x2="{lx + 18}" '
            f'y2="{pad_t + plot_h + 22:.1f}" stroke="{colour}" stroke-width="2.4"/>'
        )
        parts.append(
            f'<text x="{lx + 23}" y="{pad_t + plot_h + 25.5:.1f}" class="legend">'
            f"{html.escape(label)}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_drawdown_chart(
    dd: pd.Series, *, width: int = 940, height: int = 170, title: str = "Drawdown (underwater)"
) -> str:
    pad_l, pad_r, pad_t, pad_b = 78, 16, 30, 20
    plot_w = max(10.0, width - pad_l - pad_r)
    plot_h = max(10.0, height - pad_t - pad_b)
    pts = _sample_points(dd)
    if not pts:
        return ""
    lo, hi = min(v for _, v in pts), max(0.0, max(v for _, v in pts))
    span = max(1e-6, hi - lo)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" preserveAspectRatio="none">',
        f'<text x="{pad_l}" y="18" class="chart-title">{html.escape(title)}</text>',
    ]

    def y_of(v: float) -> float:
        return pad_t + (v - lo) / span * plot_h

    for frac in (0.0, 1.0):
        v = lo + (hi - lo) * frac
        y = y_of(v)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w:.1f}" y2="{y:.1f}" class="grid"/>'
        )
        parts.append(
            f'<text x="{pad_l - 6:.1f}" y="{y + 3.5:.1f}" class="tick tick-y">'
            f"{v * 100:.0f}%</text>"
        )
    line = " ".join(f"{pad_l + x * plot_w:.2f},{y_of(v):.2f}" for x, v in pts)
    area = f"{pad_l + pts[0][0] * plot_w:.2f},{y_of(0.0):.2f} " + line
    area += f" {pad_l + pts[-1][0] * plot_w:.2f},{y_of(0.0):.2f}"
    parts.append(f'<polygon points="{area}" class="dd-area"/>')
    parts.append(f'<polyline points="{line}" fill="none" stroke="#f2695c" stroke-width="1.5"/>')
    parts.append("</svg>")
    return "".join(parts)


def svg_price_chart(
    price: pd.Series,
    markers: Iterable[dict[str, Any]],
    *,
    width: int = 940,
    height: int = 260,
    title: str = "Price with entries and exits",
) -> str:
    """Log-ish price line (raw scale) with entry/exit markers."""
    pad_l, pad_r, pad_t, pad_b = 78, 16, 30, 20
    plot_w = max(10.0, width - pad_l - pad_r)
    plot_h = max(10.0, height - pad_t - pad_b)
    pts = _sample_points(price)
    if not pts:
        return ""
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    lo, hi = lo * 0.96, hi * 1.04
    t0, t1 = price.index[0], price.index[-1]
    total_s = max(1e-9, (t1 - t0).total_seconds())

    def x_of(v: float) -> float:
        return pad_l + v * plot_w

    def y_of(v: float) -> float:
        return pad_t + (1.0 - (v - lo) / (hi - lo)) * plot_h

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" preserveAspectRatio="none">',
        f'<text x="{pad_l}" y="18" class="chart-title">{html.escape(title)}</text>',
    ]
    for k in range(4):
        v = lo + (hi - lo) * k / 3.0
        y = y_of(v)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w:.1f}" y2="{y:.1f}" class="grid"/>'
        )
        parts.append(
            f'<text x="{pad_l - 6:.1f}" y="{y + 3.5:.1f}" class="tick tick-y">{_money(v)}</text>'
        )
    line = " ".join(f"{x_of(x):.2f},{y_of(v):.2f}" for x, v in pts)
    parts.append(f'<polyline points="{line}" fill="none" stroke="#8ab4f8" stroke-width="1.6"/>')
    for m in markers:
        for key, colour, shape in (
            ("entry_time", "#42d392", "up"),
            ("exit_time", "#f2695c", "down"),
        ):
            ts = pd.Timestamp(m[key])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            if ts < t0 or ts > t1:
                continue
            fx = (ts - t0).total_seconds() / total_s
            y = y_of(float(m["entry_price"] if key == "entry_time" else m["exit_price"]))
            x = x_of(fx)
            if shape == "up":
                path = f"M {x:.2f} {y + 7:.2f} L {x - 4.5:.2f} {y + 15:.2f} L {x + 4.5:.2f} {y + 15:.2f} Z"
            else:
                path = f"M {x:.2f} {y - 7:.2f} L {x - 4.5:.2f} {y - 15:.2f} L {x + 4.5:.2f} {y - 15:.2f} Z"
            parts.append(f'<path d="{path}" fill="{colour}" opacity="0.85"/>')
    parts.append(
        f'<circle cx="{pad_l + 8}" cy="{pad_t + 4}" r="4" fill="#42d392"/>'
        f'<text x="{pad_l + 18}" y="{pad_t + 8}" class="legend">entry</text>'
        f'<circle cx="{pad_l + 88}" cy="{pad_t + 4}" r="4" fill="#f2695c"/>'
        f'<text x="{pad_l + 98}" y="{pad_t + 8}" class="legend">exit</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def svg_year_bars(years: list[dict[str, Any]], *, width: int = 940, height: int = 230) -> str:
    """Grouped bars: strategy vs buy & hold, per calendar year."""
    if not years:
        return ""
    pad_l, pad_r, pad_t, pad_b = 78, 16, 30, 30
    plot_w = max(10.0, width - pad_l - pad_r)
    plot_h = max(10.0, height - pad_t - pad_b)
    vals = [y["strategy_return"] for y in years] + [y["buy_hold_return"] for y in years]
    hi = max(0.0, max(vals))
    lo = min(0.0, min(vals))
    span = max(1e-9, hi - lo)

    def y_of(v: float) -> float:
        return pad_t + (hi - v) / span * plot_h

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" preserveAspectRatio="none">',
        f'<text x="{pad_l}" y="18" class="chart-title">Calendar year returns</text>',
    ]
    for v in (hi, 0.0, lo):
        y = y_of(v)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w:.1f}" y2="{y:.1f}" class="grid"/>'
        )
        parts.append(
            f'<text x="{pad_l - 6:.1f}" y="{y + 3.5:.1f}" class="tick tick-y">{v * 100:.0f}%</text>'
        )
    slot = plot_w / len(years)
    bw = min(22.0, slot * 0.32)
    zero_y = y_of(0.0)
    for i, y in enumerate(years):
        cx = pad_l + slot * (i + 0.5)
        for off, key, colour in ((-1, "strategy_return", "#42d392"), (1, "buy_hold_return", "#8ab4f8")):
            v = y[key]
            x = cx + off * (bw * 0.6) - bw / 2
            top = min(y_of(v), zero_y)
            h = abs(y_of(v) - zero_y)
            parts.append(f'<rect x="{x:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(0.6, h):.1f}" fill="{colour}" opacity="0.85"/>')
        parts.append(
            f'<text x="{cx:.1f}" y="{pad_t + plot_h + 18:.1f}" class="tick" text-anchor="middle">'
            f'{y["year"]}</text>'
        )
    parts.append(
        f'<rect x="{pad_l + 8}" y="{pad_t}" width="10" height="10" fill="#42d392"/>'
        f'<text x="{pad_l + 24}" y="{pad_t + 9}" class="legend">strategy</text>'
        f'<rect x="{pad_l + 128}" y="{pad_t}" width="10" height="10" fill="#8ab4f8"/>'
        f'<text x="{pad_l + 144}" y="{pad_t + 9}" class="legend">buy &amp; hold</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------
# Markdown / HTML rendering
# --------------------------------------------------------------------------


def render_markdown(report: dict[str, Any]) -> str:
    cur = report["currency"]
    d, s, b, cd = report["data"], report["strategy"], report["buy_hold"], report["cost_drag"]
    lines = [
        f"# Investment simulation - {report['description']}",
        "",
        f"* generated: {report['generated_at']} (UTC)",
        f"* window: **{report['window']}**"
        + (" **in-sample (optimistically biased)**" if report["in_sample"] else " (out-of-sample)"),
        f"* genome origin: {report['origin']}",
        f"* data: {d['symbol']} {d['timeframe']} - {d['rows_window']:,} of {d['rows_total']:,} bars "
        f"({d['start'][:10]} .. {d['end'][:10]}, {d['years']:.2f} years)",
        f"* data file: `{d['path']}` sha256 `{d['sha256'][:16]}...`",
        f"* price: {d['first_price']:,.2f} -> {d['last_price']:,.2f} {cur}",
        f"* cash: {report['params']['initial_cash']:,.0f} {cur}; costs "
        f"{report['params']['cost_per_side_bps']:.1f} bps/side "
        f"({report['params']['round_trip_bps']:.1f} bps round trip)",
        "",
        "## Result",
        "",
        f"| | strategy | buy & hold |",
        f"| --- | ---: | ---: |",
        f"| final equity ({cur}) | {s['final_equity']:,.2f} | {b['final_equity']:,.2f} |",
        f"| total return | {_pct(s['total_return'])} | {_pct(b['total_return'])} |",
        f"| CAGR | {_pct(s['cagr'], 2)} | {_pct(b['cagr'], 2)} |",
        f"| max drawdown | {s['max_drawdown'] * 100:.1f}% | {b['max_drawdown'] * 100:.1f}% |",
        "",
        f"**{report['verdict']['text']}**",
        "",
        f"* Sharpe {s['sharpe']:.2f}, Sortino {s['sortino']:.2f}, Calmar {s['calmar']:.2f}",
        f"* trades {s['n_trades']}, win rate {s['win_rate'] * 100:.1f}%, "
        f"profit factor {s['profit_factor']:.2f}, avg trade {_pct(s['avg_trade_return'], 2)}",
        f"* time in market {s['exposure'] * 100:.1f}% of bars",
        f"* costs paid: {cd['cost_paid']:,.2f} {cur} "
        f"({cd['pct_of_initial'] * 100:.1f}% of the starting cash) - "
        f"gross {cd['gross_final_equity']:,.2f} vs net {cd['net_final_equity']:,.2f}",
        "",
        "## Cost sensitivity",
        "",
        "| cost x | bps/side | final equity | return | max DD | trades |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report["cost_sensitivity"]:
        lines.append(
            f"| {row['cost_multiplier']:.0f}x | {row['cost_per_side_bps']:.1f} | "
            f"{row['final_equity']:,.2f} | {_pct(row['total_return'])} | "
            f"{row['max_drawdown'] * 100:.1f}% | {row['n_trades']} |"
        )
    lines += ["", "## By calendar year", "", "| year | strategy | buy & hold |", "| --- | ---: | ---: |"]
    for y in report["years"]:
        lines.append(f"| {y['year']} | {_pct(y['strategy_return'])} | {_pct(y['buy_hold_return'])} |")
    lines += ["", "## Notes", ""]
    lines += [f"* {n}" for n in report["notes"]]
    lines += [
        "",
        "Charts: `" + Path(report.get("html_path", "report.html")).name + "` "
        "(equity vs buy & hold, drawdown, price with trades, yearly bars).",
        "",
    ]
    return "\n".join(lines)


CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 28px 32px 60px; background: #12151c; color: #e6e9ef;
       font: 14px/1.55 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .08em; color: #9aa4b2;
     margin: 34px 0 10px; }
.sub { color: #9aa4b2; margin: 0 0 18px; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 8px; }
.card { background: #1a1f2a; border: 1px solid #262c38; border-radius: 10px; padding: 12px 16px; min-width: 170px; }
.card .k { color: #9aa4b2; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }
.card .v { font-size: 20px; font-variant-numeric: tabular-nums; margin-top: 4px; }
.card .s { color: #9aa4b2; font-size: 12px; }
.pos { color: #42d392; } .neg { color: #f2695c; } .muted { color: #9aa4b2; }
canvas { display: none; }
.chart { width: 100%; height: auto; background: #161b24; border: 1px solid #242a36; border-radius: 10px; }
.grid { stroke: #262c38; stroke-width: 1; }
.tick { fill: #8d97a6; font-size: 11px; font-variant-numeric: tabular-nums; }
.tick-y { text-anchor: end; }
.chart-title { fill: #cfd6e2; font-size: 12px; letter-spacing: .04em; }
.legend { fill: #9aa4b2; font-size: 11px; }
.dd-area { fill: #f2695c; opacity: .22; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid #232936; }
th:first-child, td:first-child { text-align: left; }
th { color: #9aa4b2; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
tbody tr:hover { background: #191e28; }
.banner { border-left: 3px solid #e0b74a; background: #241f13; padding: 10px 14px; border-radius: 6px; margin: 14px 0; }
.banner.bad { border-color: #f2695c; background: #241518; }
.banner.good { border-color: #42d392; background: #13241b; }
ul.notes { color: #b7c0cd; }
code { background: #1a1f2a; padding: 1px 5px; border-radius: 4px; }
"""


def _fmt_money(v: float) -> str:
    return f"{v:,.2f}"


def _cls(v: float) -> str:
    return "pos" if v >= 0 else "neg"


def render_html(report: dict[str, Any], *, include_charts: bool = True) -> str:
    cur = report["currency"]
    d, s, b, cd = report["data"], report["strategy"], report["buy_hold"], report["cost_drag"]
    esc = html.escape
    charts = ""
    if include_charts:
        ec = report["equity_curve"]
        bh = report["buy_hold_curve"]
        dd = report["dd_curve"] if "dd_curve" in report else report["drawdown_curve"]
        price = report["price_curve"]
        eq_s = pd.Series(
            [v for _, v in ec], index=pd.to_datetime([t for t, _ in ec], utc=True)
        )
        bh_s = pd.Series([v for _, v in bh], index=pd.to_datetime([t for t, _ in bh], utc=True))
        dd_s = pd.Series([v for _, v in dd], index=pd.to_datetime([t for t, _ in dd], utc=True))
        px_s = pd.Series(
            [v for _, v in price], index=pd.to_datetime([t for t, _ in price], utc=True)
        )
        charts = "".join(
            [
                '<h2>Equity curve</h2>',
                svg_line_chart(
                    [
                        ("strategy", eq_s, "#42d392"),
                        ("buy & hold", bh_s, "#8ab4f8"),
                    ],
                    title=f"Account value in {cur} ({report['window']} window)",
                ),
                '<h2>Drawdown</h2>',
                svg_drawdown_chart(dd_s),
                '<h2>Price and trades</h2>',
                svg_price_chart(px_s, report["trade_markers"], title=f"{d['symbol']} {d['timeframe']}"),
                '<h2>Calendar years</h2>',
                svg_year_bars(report["years"]),
            ]
        )

    sens_rows = "".join(
        f"<tr><td>{r['cost_multiplier']:.0f}x</td><td>{r['cost_per_side_bps']:.1f}</td>"
        f"<td>{_fmt_money(r['final_equity'])}</td>"
        f"<td class=\"{_cls(r['total_return'])}\">{_pct(r['total_return'])}</td>"
        f"<td>{r['max_drawdown'] * 100:.1f}%</td><td>{r['n_trades']}</td></tr>"
        for r in report["cost_sensitivity"]
    )
    year_rows = "".join(
        f"<tr><td>{y['year']}</td>"
        f"<td class=\"{_cls(y['strategy_return'])}\">{_pct(y['strategy_return'])}</td>"
        f"<td class=\"{_cls(y['buy_hold_return'])}\">{_pct(y['buy_hold_return'])}</td></tr>"
        for y in report["years"]
    )
    trade_rows = "".join(
        f"<tr><td>{t['entry_time'][:16].replace('T', ' ')}</td>"
        f"<td>{t['exit_time'][:16].replace('T', ' ')}</td>"
        f"<td>{t['direction']}</td>"
        f"<td>{t['entry_price']:,.2f}</td><td>{t['exit_price']:,.2f}</td>"
        f"<td class=\"{_cls(t['pnl'])}\">{t['pnl']:,.2f}</td>"
        f"<td class=\"{_cls(t['return_pct'])}\">{_pct(t['return_pct'], 2)}</td>"
        f"<td>{t['exit_reason']}</td></tr>"
        for t in report["trades"][:200]
    )
    notes = "".join(f"<li>{esc(n)}</li>" for n in report["notes"])
    verdict_cls = "good" if report["verdict"]["beat_buy_hold"] else "bad"
    bias = (
        '<div class="banner bad">IN-SAMPLE window: the genome was selected on part of '
        "these bars, so this result is optimistically biased.</div>"
        if report["in_sample"]
        else '<div class="banner">Out-of-sample window: the evolution read these bars only '
        "once, at the very end of the run.</div>"
    )

    return "".join(
        [
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width, initial-scale=1'>",
            f"<title>Investment simulation - {esc(report['genome']['family'])}</title>",
            f"<style>{CSS}</style></head><body>",
            f"<h1>Investment simulation &middot; {esc(report['genome']['family'])}</h1>",
            f"<p class='sub'>{esc(d['symbol'])} {esc(d['timeframe'])} &middot; "
            f"{d['rows_window']:,} bars ({d['start'][:10]} &rarr; {d['end'][:10]}, "
            f"{d['years']:.2f} years) &middot; window <b>{esc(report['window'])}</b> &middot; "
            f"generated {esc(report['generated_at'])}</p>",
            bias,
            "<div class='cards'>",
            _card(f"Starting cash", f"{report['params']['initial_cash']:,.0f} {cur}"),
            _card(
                "Strategy final",
                f"{s['final_equity']:,.0f} {cur}",
                f"{_pct(s['total_return'])} &middot; CAGR {_pct(s['cagr'], 2)}",
                cls=_cls(s['total_return']),
            ),
            _card(
                "Buy &amp; hold final",
                f"{b['final_equity']:,.0f} {cur}",
                f"{_pct(b['total_return'])} &middot; CAGR {_pct(b['cagr'], 2)}",
                cls=_cls(b['total_return']),
            ),
            _card("Max drawdown", f"{s['max_drawdown'] * 100:.1f}%", f"buy &amp; hold {b['max_drawdown'] * 100:.1f}%"),
            _card("Costs paid", f"{cd['cost_paid']:,.0f} {cur}", f"{cd['pct_of_initial'] * 100:.1f}% of starting cash"),
            _card(
                "Trades",
                f"{s['n_trades']}",
                f"win {s['win_rate'] * 100:.1f}% &middot; PF {s['profit_factor']:.2f}",
            ),
            "</div>",
            f"<div class='banner {verdict_cls}'><b>{esc(report['verdict']['text'])}</b></div>",
            charts,
            "<h2>Cost sensitivity</h2>",
            "<table><thead><tr><th>cost</th><th>bps/side</th><th>final equity</th>"
            "<th>return</th><th>max DD</th><th>trades</th></tr></thead><tbody>",
            sens_rows,
            "</tbody></table>",
            "<h2>Calendar years</h2>",
            "<table><thead><tr><th>year</th><th>strategy</th><th>buy &amp; hold</th></tr></thead>"
            f"<tbody>{year_rows}</tbody></table>",
            "<h2>Trades</h2>",
            "<table><thead><tr><th>entry</th><th>exit</th><th>dir</th><th>entry px</th>"
            "<th>exit px</th><th>P&amp;L</th><th>return</th><th>reason</th></tr></thead>"
            f"<tbody>{trade_rows}</tbody></table>",
            f"<p class='muted'>{len(report['trades'])} trade(s) total"
            f"{' (first 200 shown)' if len(report['trades']) > 200 else ''}.</p>",
            "<h2>Numbers and provenance</h2>",
            "<ul class='notes'>",
            f"<li>data file <code>{esc(d['path'])}</code>, sha256 "
            f"<code>{esc(d['sha256'][:32])}</code>",
            f"<li>price {d['first_price']:,.2f} &rarr; {d['last_price']:,.2f} {cur}; "
            f"costs {report['params']['cost_per_side_bps']:.1f} bps/side "
            f"({report['params']['round_trip_bps']:.1f} bps round trip), "
            f"position size {report['params']['position_size'] * 100:.0f}% of equity",
            f"<li>selection: origin <code>{esc(report['origin'])}</code>; genome "
            f"<code>{esc(json.dumps(report['genome']['params']))}</code> "
            f"risk <code>{esc(json.dumps(report['genome']['risk']))}</code>",
            "</ul>",
            f"<ul class='notes'>{notes}</ul>",
            "</body></html>",
        ]
    )


def _card(key: str, value: str, sub: str = "", cls: str = "") -> str:
    return (
        f"<div class='card'><div class='k'>{key}</div>"
        f"<div class='v {cls}'>{value}</div><div class='s'>{sub}</div></div>"
    )


def write_reports(
    report: dict[str, Any],
    *,
    out_md: str | Path,
    out_html: str | Path,
    out_json: str | Path | None = None,
) -> dict[str, Path]:
    """Write markdown + HTML (+ optional JSON) and return the paths."""
    md_path = Path(out_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    report["html_path"] = str(out_html)
    md_path.write_text(render_markdown(report), encoding="utf-8")

    html_path = Path(out_html)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(report), encoding="utf-8")

    written = {"markdown": md_path, "html": html_path}
    if out_json is not None:
        json_path = Path(out_json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        written["json"] = json_path
    return written
