"""The autopsy: what the trades lost on, clustered so it is not repeated twice.

This is the screen the demo lingers on longest:

    Loaded 214 trades - 126 losers
    Found the 5 worst losses
    Clustering failure modes across all 126 losers...

The point is not to admire the winners.  It is to look the losers in the eye,
name the *recurring* reason they lost, and turn that into a concrete rule change
that the next generation inherits.  The lesson written here goes into the SQLite
memory (:class:`selftrade.memory.Memory`), which is what makes the loop
self-improving rather than merely repetitive.

The context of every trade is read at its **entry bar, from information available
at that moment** (RSI, ATR%, distance from the 200-period average), so the autopsy
cannot smuggle in hindsight.  The one hindsight value used is the window median of
ATR%, and only as a *label* ("above-median volatility") for the diagnosis - never
as an input to a trading decision.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from . import indicators
from .genome import validate_genome

EXIT_REASONS = ("stop", "target", "signal", "end_of_data")


# --------------------------------------------------------------------------
# Market context at the entry bar
# --------------------------------------------------------------------------


def context_table(
    df: pd.DataFrame,
    *,
    rsi_period: int = 14,
    atr_period: int = 14,
    sma_period: int = 200,
    vol_period: int = 20,
) -> pd.DataFrame:
    """Causal context columns for every bar (no future information)."""
    close = df["close"].astype("float64")
    atr = indicators.atr(df, atr_period)
    sma = indicators.sma(close, sma_period)
    tbl = pd.DataFrame(
        {
            "rsi": indicators.rsi(close, rsi_period),
            "atr_pct": atr / close * 100.0,
            "sma200": sma,
            "dist_sma200_pct": (close / sma - 1.0) * 100.0,
            "vol20": indicators.realized_vol(close, vol_period),
            "above_200d": close > sma,
        },
        index=df.index,
    )
    return tbl


def _row_at(tbl: pd.DataFrame, ts: Any) -> pd.Series:
    if ts in tbl.index:
        return tbl.loc[ts]
    pos = int(tbl.index.searchsorted(ts)) - 1
    pos = max(0, min(pos, len(tbl) - 1))
    return tbl.iloc[pos]


def _fmt(v: Any, unit: str = "", digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    return f"{float(v):.{digits}f}{unit}"


# --------------------------------------------------------------------------
# Clustering
# --------------------------------------------------------------------------


MODE_SPECS: tuple[tuple[str, str, str, str], ...] = (
    (
        "counter_trend",
        "entry against the 200-period trend",
        "require the trend to agree (price above its 200-period average for longs) "
        "or run the regime filter",
        "long with price below the 200-period average (or short above it) at entry",
    ),
    (
        "high_volatility",
        "entry in above-median volatility",
        "skip entries when ATR% is above the window median, or size the position down",
        "ATR% at entry above the median of the window",
    ),
    (
        "fast_stop",
        "stopped out within 3 bars",
        "the entry is too early: wait for a confirmation close, or widen the stop",
        "stopped out after at most 3 bars",
    ),
    (
        "slow_exit",
        "bled out on a late signal exit",
        "the exit is too slow: tighten the trailing stop or add a time stop",
        "held 20+ bars and left on a signal",
    ),
    (
        "stop_heavy",
        "the stop did all the work",
        "nearly all exits are stops: the entries are not selective enough",
        "exit reason was the stop",
    ),
    (
        "chased_extension",
        "chased an extension",
        "do not buy an overbought reading (or short an oversold one) without a pullback",
        "long entry with RSI above 70, or short entry with RSI below 30",
    ),
)


def _enrich(trades: pd.DataFrame, ctx: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """Closed trades plus the market context at their entry bar."""
    if not len(trades):
        # Typed empties: a strategy that never traded must not blow up the
        # autopsy with object-dtype columns.
        floats = (
            "entry_price",
            "exit_price",
            "pnl",
            "return_pct",
            "rsi",
            "atr_pct",
            "dist_sma200_pct",
            "vol20",
        )
        empty = pd.DataFrame({c: pd.Series(dtype="float64") for c in floats})
        empty["direction"] = pd.Series(dtype="object")
        empty["exit_reason"] = pd.Series(dtype="object")
        empty["entry_time"] = pd.Series(dtype="datetime64[ns, UTC]")
        empty["exit_time"] = pd.Series(dtype="datetime64[ns, UTC]")
        empty["bars_held"] = pd.Series(dtype="int64")
        empty["above_200d"] = pd.Series(dtype="bool")
        for extra in ("trend_aligned", "high_vol", "is_long"):
            empty[extra] = pd.Series(dtype="bool")
        return empty
    recs: list[dict[str, Any]] = []
    median_atr = float(ctx["atr_pct"].median())
    for _, t in trades.iterrows():
        row = _row_at(ctx, t["entry_time"])
        is_long = str(t["direction"]) == "long"
        above = bool(row["above_200d"]) if pd.notna(row["above_200d"]) else True
        recs.append(
            {
                "direction": str(t["direction"]),
                "entry_time": t["entry_time"],
                "exit_time": t["exit_time"],
                "entry_price": float(t["entry_price"]),
                "exit_price": float(t["exit_price"]),
                "pnl": float(t["pnl"]),
                "return_pct": float(t["return_pct"]),
                "bars_held": int(t["bars_held"]),
                "exit_reason": str(t["exit_reason"]),
                "rsi": float(row["rsi"]) if pd.notna(row["rsi"]) else float("nan"),
                "atr_pct": float(row["atr_pct"]) if pd.notna(row["atr_pct"]) else float("nan"),
                "dist_sma200_pct": (
                    float(row["dist_sma200_pct"]) if pd.notna(row["dist_sma200_pct"]) else float("nan")
                ),
                "above_200d": above,
                "trend_aligned": bool(above == is_long),
                "vol20": float(row["vol20"]) if pd.notna(row["vol20"]) else float("nan"),
                "high_vol": bool(
                    pd.notna(row["atr_pct"]) and float(row["atr_pct"]) > median_atr
                ),
                "is_long": is_long,
            }
        )
    return pd.DataFrame(recs).sort_values("entry_time").reset_index(drop=True)


def _mode_hit(key: str, r: pd.Series) -> bool:
    if key == "counter_trend":
        return not bool(r["trend_aligned"])
    if key == "high_volatility":
        return bool(r["high_vol"])
    if key == "fast_stop":
        return r["bars_held"] <= 3 and r["exit_reason"] == "stop"
    if key == "slow_exit":
        return r["bars_held"] >= 20 and r["exit_reason"] == "signal"
    if key == "stop_heavy":
        return r["exit_reason"] == "stop"
    if key == "chased_extension":
        if pd.isna(r["rsi"]):
            return False
        return (bool(r["is_long"]) and float(r["rsi"]) > 70) or (
            not bool(r["is_long"]) and float(r["rsi"]) < 30
        )
    raise KeyError(key)


def cluster_modes(losers: pd.DataFrame, winners: pd.DataFrame) -> list[dict[str, Any]]:
    """Count each failure mode among the losers, with the winner contrast."""
    if not len(losers):
        return []
    median_atr = float(losers["atr_pct"].median())
    out: list[dict[str, Any]] = []
    for key, label, fix, when in MODE_SPECS:
        hits = losers[[_mode_hit(key, r) for _, r in losers.iterrows()]]
        if not len(hits):
            continue
        win_hits = (
            sum(1 for _, r in winners.iterrows() if _mode_hit(key, r)) if len(winners) else 0
        )
        out.append(
            {
                "key": key,
                "label": label,
                "when": when,
                "count": int(len(hits)),
                "share_of_losers": float(len(hits) / len(losers)),
                "winner_count": int(win_hits),
                "winner_share": float(win_hits / len(winners)) if len(winners) else 0.0,
                "lost_pnl": float(hits["pnl"].sum()),
                "median_atr_pct": median_atr,
                "examples": [
                    {
                        "entry_time": t.isoformat(),
                        "pnl": float(p),
                        "return_pct": float(rp),
                        "bars_held": int(bh),
                        "exit_reason": str(er),
                    }
                    for t, p, rp, bh, er in zip(
                        hits["entry_time"].head(3),
                        hits["pnl"].head(3),
                        hits["return_pct"].head(3),
                        hits["bars_held"].head(3),
                        hits["exit_reason"].head(3),
                    )
                ],
                "fix": fix,
            }
        )
    out.sort(key=lambda m: (m["count"], abs(m["lost_pnl"])), reverse=True)
    return out


def overfit_warnings(
    closed: pd.DataFrame, *, min_trades: int = 8, winners: pd.DataFrame | None = None
) -> list[str]:
    """The demo's "sample of only 12 trades" family of warnings."""
    out: list[str] = []
    n = len(closed)
    if n == 0:
        return ["no closed trades at all"]
    if n < max(min_trades * 2, 12):
        out.append(
            f"only {n} closed trades: too small a sample to separate skill from luck "
            f"(the gate wants at least {min_trades})"
        )
    wins = winners if winners is not None else closed[closed["pnl"] > 0]
    gross_win = float(wins["pnl"].sum()) if len(wins) else 0.0
    if gross_win > 0 and len(wins):
        top3 = float(wins["pnl"].sort_values(ascending=False).head(3).sum())
        share = top3 / gross_win
        if share > 0.6:
            out.append(
                f"the 3 best trades produced {share * 100:.0f}% of gross profit: "
                "the result rests on a handful of trades"
            )
    if len(closed) >= 10:
        worst = float(closed["pnl"].min())
        median_loss = float(closed[closed["pnl"] < 0]["pnl"].median()) if (closed["pnl"] < 0).any() else 0.0
        if median_loss < 0 and abs(worst) > 3 * abs(median_loss):
            out.append(
                f"the worst trade ({worst:.2f}) is more than 3x the median loss "
                f"({median_loss:.2f}): tail risk dominates"
            )
    win_rate = float((closed["pnl"] > 0).mean())
    if win_rate < 0.35:
        out.append(
            f"win rate {win_rate * 100:.1f}%: the strategy relies on large winners to "
            "pay for many small losses"
        )
    return out


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def autopsy(
    trades: pd.DataFrame,
    df: pd.DataFrame,
    genome: dict[str, Any],
    *,
    worst_n: int = 5,
    min_trades: int = 8,
    window_label: str = "",
    origin: str = "",
    context: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Post-mortem of a run's trades: worst losses, clusters, rules to change."""
    g = validate_genome(genome)
    ctx = context if context is not None else context_table(df)
    all_trades = trades if len(trades) else trades
    closed = (
        all_trades[all_trades["exit_reason"] != "end_of_data"] if len(all_trades) else all_trades
    )
    open_at_end = int(len(all_trades) - len(closed))
    enriched = _enrich(closed, ctx, df)
    losers = enriched[enriched["pnl"] < 0]
    winners = enriched[enriched["pnl"] >= 0]
    # sort_values/head instead of nsmallest: nsmallest breaks on an empty frame.
    worst = losers.sort_values("pnl").head(max(1, int(worst_n)))

    worst_rows: list[dict[str, Any]] = []
    for rank, (_, r) in enumerate(worst.iterrows(), start=1):
        side = "long" if r["is_long"] else "short"
        below = "below" if not bool(r["above_200d"]) else "above"
        sentence = (
            f"{side} at {r['entry_time'].strftime('%Y-%m-%d %H:%M')} with RSI "
            f"{_fmt(r['rsi'])}, price {_fmt(abs(r['dist_sma200_pct']), '%')} {below} the "
            f"200-period average, ATR {_fmt(r['atr_pct'], '%')}, held {int(r['bars_held'])} "
            f"bars, out on {r['exit_reason']}"
        )
        worst_rows.append(
            {
                "rank": rank,
                "direction": "long" if r["is_long"] else "short",
                "entry_time": r["entry_time"].isoformat(),
                "exit_time": r["exit_time"].isoformat(),
                "pnl": float(r["pnl"]),
                "return_pct": float(r["return_pct"]),
                "bars_held": int(r["bars_held"]),
                "exit_reason": str(r["exit_reason"]),
                "rsi": None if pd.isna(r["rsi"]) else float(r["rsi"]),
                "atr_pct": None if pd.isna(r["atr_pct"]) else float(r["atr_pct"]),
                "dist_sma200_pct": (
                    None if pd.isna(r["dist_sma200_pct"]) else float(r["dist_sma200_pct"])
                ),
                "context": sentence,
            }
        )

    comparison: dict[str, Any] = {}
    for col, unit in (("rsi", ""), ("atr_pct", "%"), ("dist_sma200_pct", "%"), ("bars_held", "")):
        comparison[col] = {
            "losers": _median(losers[col]) if len(losers) else None,
            "winners": _median(winners[col]) if len(winners) else None,
            "unit": unit,
        }

    exit_mix = (
        {k: int(v) for k, v in closed["exit_reason"].value_counts().items()} if len(closed) else {}
    )
    report = {
        "window": window_label,
        "origin": origin,
        "family": g["family"],
        "description": _describe(g),
        "genome": g,
        "n_trades": int(len(closed)),
        "n_losers": int(len(losers)),
        "loser_share": float(len(losers) / len(closed)) if len(closed) else 0.0,
        "open_at_end": open_at_end,
        "net_pnl": float(closed["pnl"].sum()) if len(closed) else 0.0,
        "gross_loss": float(losers["pnl"].sum()) if len(losers) else 0.0,
        "worst_total": float(worst["pnl"].sum()) if len(worst) else 0.0,
        "worst_avg_return_pct": float(worst["return_pct"].mean()) if len(worst) else 0.0,
        "worst": worst_rows,
        "modes": cluster_modes(losers, winners),
        "losers_vs_winners": comparison,
        "exit_mix": exit_mix,
        "overfit": overfit_warnings(closed, min_trades=min_trades, winners=winners),
        "trades": [
            {
                "entry_time": r["entry_time"].isoformat(),
                "exit_time": r["exit_time"].isoformat(),
                "direction": "long" if r["is_long"] else "short",
                "pnl": float(r["pnl"]),
                "return_pct": float(r["return_pct"]),
                "bars_held": int(r["bars_held"]),
                "exit_reason": str(r["exit_reason"]),
                "rsi": None if pd.isna(r["rsi"]) else float(r["rsi"]),
                "atr_pct": None if pd.isna(r["atr_pct"]) else float(r["atr_pct"]),
                "dist_sma200_pct": (
                    None if pd.isna(r["dist_sma200_pct"]) else float(r["dist_sma200_pct"])
                ),
            }
            for _, r in enriched.iterrows()
        ],
    }
    report["summary"] = summary_line(report)
    return report


def _median(s: pd.Series) -> float | None:
    if s is None or not len(s):
        return None
    v = s.median()
    return None if pd.isna(v) else float(v)


def _describe(genome: dict[str, Any]) -> str:
    from .genome import describe

    return describe(genome)


def summary_line(report: dict[str, Any]) -> str:
    """One line that says what happened, in the demo's register."""
    if report["n_trades"] == 0:
        return "Loaded 0 closed trades: nothing to autopsy."
    top = report["modes"][0] if report["modes"] else None
    text = (
        f"Loaded {report['n_trades']} trades - {report['n_losers']} losers "
        f"({report['loser_share'] * 100:.0f}%); the worst {len(report['worst'])} took "
        f"{abs(report['worst_total']):,.2f} out of the account"
    )
    if top:
        text += (
            f". Dominant failure mode: {top['label']} "
            f"({top['count']} of {report['n_losers']} losers, {top['share_of_losers'] * 100:.0f}%)"
        )
    text += "."
    return text


def lesson_text(report: dict[str, Any]) -> str:
    """What goes into the memory so the same mistake is not made twice."""
    if report["n_trades"] == 0:
        return (
            f"Autopsy ({report['window'] or 'window'}, {report['family']}): the strategy "
            "never traded, so nothing was learned about its entries."
        )
    parts = [summary_line(report)]
    if report["modes"]:
        described = "; ".join(
            f"{m['label']} -> {m['fix']}" for m in report["modes"][:3]
        )
        parts.append(f"Rules to change next generation: {described}.")
    if report["overfit"]:
        parts.append("Caveats: " + "; ".join(report["overfit"][:2]) + ".")
    return " ".join(parts)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def console_lines(report: dict[str, Any]) -> list[str]:
    out = [summary_line(report)]
    if report["n_trades"] == 0:
        return out
    out.append(
        f"  exit mix: "
        + ", ".join(f"{k}={v}" for k, v in sorted(report["exit_mix"].items()))
    )
    for w in report["worst"]:
        out.append(
            f"  #{w['rank']} {w['pnl']:>9,.2f} ({w['return_pct'] * 100:+.1f}%) "
            f"{w['entry_time'][:16]} -> {w['exit_time'][:16]} {w['context']}"
        )
    if report["modes"]:
        out.append("  failure modes:")
        for m in report["modes"]:
            out.append(
                f"    - {m['label']}: {m['count']}/{report['n_losers']} losers "
                f"({m['share_of_losers'] * 100:.0f}%), winners {m['winner_share'] * 100:.0f}%"
            )
            out.append(f"      fix: {m['fix']}")
    for w in report["overfit"]:
        out.append(f"  caveat: {w}")
    return out


def render_markdown(report: dict[str, Any]) -> str:
    L: list[str] = [f"## Autopsy ({report['window'] or 'window'})", ""]
    L.append(summary_line(report))
    L.append("")
    if report["n_trades"] == 0:
        return "\n".join(L)
    L += [
        f"- closed trades: {report['n_trades']} ({report['n_losers']} losers, "
        f"{report['loser_share'] * 100:.1f}%), net P&L {report['net_pnl']:,.2f}",
        f"- exit mix: " + ", ".join(f"`{k}`={v}" for k, v in sorted(report["exit_mix"].items())),
        "",
        "### The worst losses",
        "",
        "| # | P&L | return | bars | exit | what the market looked like at entry |",
        "| ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for w in report["worst"]:
        L.append(
            f"| {w['rank']} | {w['pnl']:,.2f} | {w['return_pct'] * 100:+.2f}% | "
            f"{w['bars_held']} | {w['exit_reason']} | {w['context']} |"
        )
    if report["modes"]:
        L += [
            "",
            "### Failure modes across all losers",
            "",
            "| failure mode | losers | share | winners | fix |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        for m in report["modes"]:
            L.append(
                f"| {m['label']} | {m['count']} | {m['share_of_losers'] * 100:.0f}% | "
                f"{m['winner_share'] * 100:.0f}% | {m['fix']} |"
            )
    L += ["", "### Losers vs winners", "", "| context at entry | losers | winners |", "| --- | ---: | ---: |"]
    for col, vals in report["losers_vs_winners"].items():
        def fmt(v: Any) -> str:
            return "n/a" if v is None else f"{float(v):.2f}{vals['unit']}"

        L.append(f"| {col} | {fmt(vals['losers'])} | {fmt(vals['winners'])} |")
    if report["overfit"]:
        L += ["", "### Caveats", ""]
        L += [f"* {w}" for w in report["overfit"]]
    L.append("")
    return "\n".join(L)


def autopsy_for_result(
    trades: pd.DataFrame,
    df: pd.DataFrame,
    genome: dict[str, Any],
    *,
    window_label: str,
    origin: str = "",
    min_trades: int = 8,
    worst_n: int = 5,
) -> dict[str, Any]:
    """Convenience wrapper used by the CLI, the Evolver and the tests."""
    return autopsy(
        trades,
        df,
        genome,
        worst_n=worst_n,
        min_trades=min_trades,
        window_label=window_label,
        origin=origin,
    )


def trade_stats(trades: pd.DataFrame) -> dict[str, Any]:
    """Small helper for callers that want the counts without the full report."""
    closed = trades[trades["exit_reason"] != "end_of_data"] if len(trades) else trades
    return {
        "n_trades": int(len(closed)),
        "n_losers": int((closed["pnl"] < 0).sum()) if len(closed) else 0,
        "reasons": sorted({str(x) for x in closed["exit_reason"]}) if len(closed) else [],
    }
