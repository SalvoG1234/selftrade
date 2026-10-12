"""Stress testing: how much of a result is the draw, and how much the numbers.

Two questions, two axes, both reproducible from a fixed seed.

**Path risk** - the realized trade sequence is one draw out of many.  :func:`monte_carlo`
resamples it (``bootstrap``, ``shuffle``, ``block``) and reports percentiles of final
return, drawdown and losing streak, plus the probabilities that matter against the
sealed limits.  A ``shuffle`` path holds the *same* trades in a different order, so its
final return is identical to the realized one by construction and only the path
changes - which is a check on the sampler, not on the strategy.

**Parameter risk** - :func:`sensitivity` re-runs the engine with every numeric parameter
moved by ±delta (relative for floats, ``max(1, round(|v|·delta))`` for integers, and the
risk knobs moved in magnitude), records the objective before and after and measures the
*degradation*: the worst case, how many perturbations break the evaluation gates, and
which parameter is the fragile one.  A perturbation that the genome's own bounds clamp
is reported as ``clamped``, so the number in the report is the number that was run.

The equity model of a Monte Carlo path is the engine's own: each trade's ``return_pct``
is applied to the *current* equity (``equity *= 1 + return_pct``), which is what the
position-size-from-equity rule does.  :func:`realized_path` therefore reproduces
``backtest.simulate``'s final return and drawdown to floating point, and a test asserts
it.

Reproducibility, stated exactly: ``seed`` seeds one ``numpy.random.Generator`` for the
paths (``default_rng(seed)``) and the sensitivity axis uses ``default_rng(seed + 1)``.
The second stream is unused today - nothing there is random - but the derivation is
written down so that a later change cannot silently renumber it.  Same seed, same
window, same genome: identical percentiles to the last digit on the same numpy.

What this deliberately does **not** do:

* no daily-loss probability from the paths.  Resampling assumes trades are
  exchangeable, and the calendar is not: a bootstrap destroys which trades shared a
  day.  The realized worst day is reported for reference instead, and ``block`` is the
  honest option when the sequence has streaks;
* a perturbation is not a probability.  Sensitivity is a *measured* fragility, not a
  distribution over parameters: nothing here says how likely ±10% is, only what it
  costs;
* no reading of the sealed limits as a forecast.  ``P(drawdown > 15%)`` is the share of
  resampled paths that would have breached it, not a promise about 2027.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from .evaluate import metric_value
from .genome import validate_genome
from .params import FAMILY_PARAMS, BoolParam, ChoiceParam, FloatParam, IntParam
from .risk_rules import LIMITS

#: Resampling methods, in the order the CLI offers them.
METHODS = ("bootstrap", "shuffle", "block")

#: Per-path statistics.  All of them are path properties, not calendar properties.
PATH_METRICS = ("final_return", "max_drawdown", "max_losing_streak", "worst_trade_return")

PERCENTILES = (5, 25, 50, 75, 95)

#: ``evaluation.metric`` names that are not the key ``compute_metrics`` uses.  The
#: objective called ``return`` lives under ``total_return``; without this alias the
#: sensitivity axis would read 0.0 and declare every parameter harmless.
OBJECTIVE_KEYS = {"return": "total_return"}

#: Above this share of the baseline objective, a ±delta move is called fragile.
FRAGILE_SHARE = 0.25


# --------------------------------------------------------------------------
# the trade sequence
# --------------------------------------------------------------------------

def closed_trades(trades: pd.DataFrame | None) -> pd.DataFrame:
    """The trades that were actually closed (``end_of_data`` is a mark, not an exit)."""
    if trades is None:
        return pd.DataFrame(columns=["exit_reason", "return_pct"])
    if not len(trades) or "exit_reason" not in trades.columns:
        return trades.iloc[0:0].copy()
    return trades[trades["exit_reason"] != "end_of_data"].reset_index(drop=True)


def trade_returns(trades: pd.DataFrame | None) -> np.ndarray:
    """``return_pct`` of every closed trade, as a float64 array."""
    closed = closed_trades(trades)
    if not len(closed):
        return np.zeros(0, dtype="float64")
    return closed["return_pct"].to_numpy(dtype="float64")


def _losing_streaks(sample: np.ndarray) -> np.ndarray:
    """Longest run of consecutive losing trades in each row."""
    loss = sample < 0
    idx = np.arange(sample.shape[1])
    # Index of the last non-loss element at or before each position; a loss at i
    # then belongs to a run that started right after it.
    last_ok = np.maximum.accumulate(np.where(~loss, idx, -1), axis=1)
    run = np.where(loss, idx - last_ok, 0)
    return run.max(axis=1)


def _path_stats(sample: np.ndarray, initial_cash: float) -> dict[str, np.ndarray]:
    """Per-row path statistics from a ``(paths, trades)`` matrix of returns."""
    if sample.ndim == 1:
        sample = sample.reshape(1, -1)
    eq = float(initial_cash) * np.cumprod(1.0 + sample, axis=1)
    peak = np.maximum.accumulate(eq, axis=1)
    return {
        "final_return": eq[:, -1] / float(initial_cash) - 1.0,
        "max_drawdown": np.max(1.0 - eq / peak, axis=1),
        "max_losing_streak": _losing_streaks(sample).astype("float64"),
        "worst_trade_return": sample.min(axis=1),
    }


def realized_path(
    trades: pd.DataFrame | None, *, initial_cash: float = 10_000.0
) -> dict[str, float]:
    """The statistics of the sequence as it actually happened."""
    returns = trade_returns(trades)
    if not len(returns):
        return {k: 0.0 for k in PATH_METRICS}
    return {k: float(v[0]) for k, v in _path_stats(returns, initial_cash).items()}


# --------------------------------------------------------------------------
# Monte Carlo over the sequence
# --------------------------------------------------------------------------

def _draw(rng: np.random.Generator, n: int, paths: int, method: str, block: int) -> np.ndarray:
    """An ``(paths, n)`` matrix of positions into the realized sequence."""
    if method == "bootstrap":
        return rng.integers(0, n, size=(int(paths), n))
    if method == "shuffle":
        return np.array([rng.permutation(n) for _ in range(int(paths))], dtype="int64")
    if method == "block":
        b = max(1, min(int(block), n))
        if b >= n:  # a block as long as the window is the window itself
            return np.tile(np.arange(n, dtype="int64"), (int(paths), 1))
        blocks = int(np.ceil(n / b))
        starts = rng.integers(0, n, size=(int(paths), blocks))
        offsets = np.arange(b, dtype="int64")
        idx = (starts[:, :, None] + offsets[None, None, :]) % n
        return idx.reshape(int(paths), blocks * b)[:, :n]
    raise ValueError(f"unknown method {method!r}. Known: {', '.join(METHODS)}")


def percentiles(values: Sequence[float], qs: Sequence[int] = PERCENTILES) -> dict[str, float]:
    """Named percentiles of a sample (``p5``, ``p50``, ...)."""
    arr = np.asarray(values, dtype="float64")
    if not len(arr):
        return {f"p{q}": 0.0 for q in qs}
    return {f"p{q}": float(np.percentile(arr, q)) for q in qs}


def monte_carlo(
    returns: Sequence[float],
    *,
    paths: int = 2000,
    seed: int = 7,
    method: str = "bootstrap",
    block: int = 5,
    initial_cash: float = 10_000.0,
    drawdown_limit: float | None = None,
) -> dict[str, Any]:
    """Resample the trade sequence and report the distribution of path outcomes.

    ``returns`` is the sequence of per-trade returns (``stress.trade_returns``).  The
    same trades are the only input: this measures how much of the realized result is
    the *order* they arrived in (``shuffle``), how much is the sample itself
    (``bootstrap``) and how much is short-run streakiness (``block``).
    """
    r = np.asarray(returns, dtype="float64")
    n = int(len(r))
    limit = LIMITS.MAX_DRAWDOWN_PCT / 100.0 if drawdown_limit is None else float(drawdown_limit)
    if n == 0:
        return {
            "method": method,
            "seed": int(seed),
            "paths": 0,
            "trades": 0,
            "drawdown_limit": limit,
            "percentiles": {},
            "probabilities": {},
            "realized": {k: 0.0 for k in PATH_METRICS},
            "note": "no closed trades to resample: nothing can be said about path risk",
            "path_digest": "",
        }

    rng = np.random.default_rng(int(seed))
    sample = r[_draw(rng, n, paths, method, block)]
    stats = _path_stats(sample, initial_cash)
    realized = {k: float(v[0]) for k, v in _path_stats(r, initial_cash).items()}

    table: dict[str, dict[str, float]] = {}
    for key in PATH_METRICS:
        row = percentiles(stats[key])
        row["realized"] = realized[key]
        row["mean"] = float(np.mean(stats[key]))
        table[key] = row

    dd = stats["max_drawdown"]
    final = stats["final_return"]
    probabilities = {
        "final_negative": float(np.mean(final < 0.0)),
        "drawdown_over_limit": float(np.mean(dd > limit + 1e-12)),
        "drawdown_worse_than_realized": float(np.mean(dd > realized["max_drawdown"] + 1e-12)),
        "final_below_realized": float(np.mean(final < realized["final_return"] - 1e-12)),
        "streak_at_least_ten": float(np.mean(stats["max_losing_streak"] >= 10.0)),
    }

    payload = {
        "method": method,
        "seed": int(seed),
        "paths": int(paths),
        "trades": n,
        "block": int(block),
        "drawdown_limit": limit,
        "initial_cash": float(initial_cash),
        "returns": {
            "mean": float(np.mean(r)),
            "std": float(np.std(r, ddof=0)),
            "min": float(np.min(r)),
            "max": float(np.max(r)),
            "win_share": float(np.mean(r > 0)),
        },
        "percentiles": table,
        "probabilities": probabilities,
        "realized": realized,
    }
    payload["path_digest"] = digest_of_paths(payload)
    return payload


def digest_of_paths(mc: dict[str, Any]) -> str:
    """SHA-256 of the percentile table and the probabilities.

    Two runs of the same seed on the same sequence produce the same digest, so
    "reproducible" is a string to compare rather than a claim to trust.
    """
    payload = {
        "method": mc.get("method"),
        "seed": mc.get("seed"),
        "paths": mc.get("paths"),
        "trades": mc.get("trades"),
        "percentiles": mc.get("percentiles"),
        "probabilities": mc.get("probabilities"),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# parameter sensitivity
# --------------------------------------------------------------------------

def _shift(spec: Any, value: Any, direction: int, delta: float) -> Any:
    """One perturbation of one parameter, in its own currency (int or float)."""
    if isinstance(spec, BoolParam) or isinstance(spec, ChoiceParam):
        return value  # nothing numeric to move
    step = max(1, int(round(abs(float(value)) * float(delta)))) if isinstance(spec, IntParam) else abs(float(value)) * float(delta)
    shifted = float(value) + direction * step
    if isinstance(spec, IntParam):
        return int(round(shifted))
    return float(shifted)


def _shift_risk(key: str, value: Any, direction: int, delta: float) -> Any:
    """Risk knobs move in magnitude: a stop is negative on purpose."""
    v = float(value)
    if key == "stop_loss_pct":
        return -abs(v) * (1.0 + direction * float(delta))
    return v * (1.0 + direction * float(delta))


def perturbations(
    genome: dict[str, Any],
    *,
    deltas: Sequence[float] = (0.10, 0.25),
    include_risk: bool = True,
) -> list[dict[str, Any]]:
    """Every numeric parameter of the genome, moved by ±delta - deterministically.

    Booleans and choices are skipped (there is nothing to shade), and a shift that
    lands back on the base value is dropped rather than reported as a change.
    """
    g = validate_genome(genome)
    specs = FAMILY_PARAMS[g["family"]]
    out: list[dict[str, Any]] = []
    for name in sorted(specs):
        spec = specs[name]
        base = g["params"].get(name, getattr(spec, "default", None))
        for delta in deltas:
            for direction in (1, -1):
                value = _shift(spec, base, direction, float(delta))
                if value == base:
                    continue
                out.append(
                    {
                        "param": name,
                        "path": "params",
                        "base": base,
                        "value": value,
                        "delta": float(delta) * direction,
                    }
                )
    if include_risk:
        for name in ("stop_loss_pct", "take_profit_pct", "trailing_pct"):
            base = g["risk"].get(name)
            if base is None:
                continue
            for delta in deltas:
                for direction in (1, -1):
                    value = _shift_risk(name, base, direction, float(delta))
                    if value == base:
                        continue
                    out.append(
                        {
                            "param": name,
                            "path": "risk",
                            "base": float(base),
                            "value": value,
                            "delta": float(delta) * direction,
                        }
                    )
    return out


def apply_perturbation(genome: dict[str, Any], pert: dict[str, Any]) -> dict[str, Any]:
    """The genome with one parameter moved (then validated, i.e. possibly clamped)."""
    g = validate_genome(genome)
    path = "risk" if pert.get("path") == "risk" else "params"
    out = dict(g)
    out[path] = dict(g[path])
    out[path][pert["param"]] = pert["value"]
    return validate_genome(out)


def objective_value(metrics: dict[str, Any], name: str) -> float:
    """The configured objective, with the metric-name aliases applied."""
    return metric_value(metrics, OBJECTIVE_KEYS.get(str(name), str(name)))


def _gates(metrics: dict[str, Any], *, min_trades: int, max_drawdown: float) -> tuple[bool, str]:
    reasons: list[str] = []
    n = float(metrics.get("n_trades", 0.0))
    if n < int(min_trades):
        reasons.append(f"n_trades={n:.0f} < min_trades={int(min_trades)}")
    dd = float(metrics.get("max_drawdown", 0.0))
    if dd > float(max_drawdown):
        reasons.append(f"max_drawdown={dd:.3f} > {float(max_drawdown):.3f}")
    return (not reasons), "; ".join(reasons)


def sensitivity(
    genome: dict[str, Any],
    run: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    objective: str = "sharpe",
    deltas: Sequence[float] = (0.10, 0.25),
    min_trades: int = 8,
    max_drawdown: float = 0.60,
    include_risk: bool = True,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-run ``run(genome) -> metrics`` for every perturbation and measure the damage.

    ``run`` is injected so the arithmetic of the degradation is testable without the
    engine; :func:`stress_report` passes one that backtests the deployment window.
    """
    g = validate_genome(genome)
    base_metrics = dict(baseline) if baseline is not None else dict(run(g))
    base_value = objective_value(base_metrics, objective)
    base_passed, base_reason = _gates(base_metrics, min_trades=min_trades, max_drawdown=max_drawdown)

    rows: list[dict[str, Any]] = []
    by_param: dict[str, dict[str, Any]] = {}
    for pert in perturbations(g, deltas=deltas, include_risk=include_risk):
        shifted = apply_perturbation(g, pert)
        path = "risk" if pert.get("path") == "risk" else "params"
        used = shifted[path][pert["param"]]
        clamped = used != pert["value"]
        metrics = run(shifted)
        value = objective_value(metrics, objective)
        passed, reason = _gates(metrics, min_trades=min_trades, max_drawdown=max_drawdown)
        row = {
            "param": pert["param"],
            "path": path,
            "base": pert["base"],
            "value": used,
            "requested": pert["value"],
            "clamped": bool(clamped),
            "delta": float(pert["delta"]),
            "actual_change": (float(used) / float(pert["base"]) - 1.0) if float(pert["base"]) else 0.0,
            "objective": value,
            "objective_delta": value - base_value,
            "objective_share": (
                (base_value - value) / abs(base_value) if abs(base_value) > 1e-12 else None
            ),
            "total_return": float(metrics.get("total_return", 0.0)),
            "max_drawdown": float(metrics.get("max_drawdown", 0.0)),
            "n_trades": float(metrics.get("n_trades", 0.0)),
            "passed": bool(passed),
            "gate_reason": reason,
        }
        rows.append(row)
        entry = by_param.setdefault(
            pert["param"],
            {"param": pert["param"], "path": path, "base": pert["base"], "perturbations": []},
        )
        entry["perturbations"].append(row)

    for entry in by_param.values():
        probes = entry["perturbations"]
        worst_row = min(probes, key=lambda r: r["objective"]) if probes else None
        shares = [r["objective_share"] for r in probes if r["objective_share"] is not None]
        entry["worst"] = worst_row
        entry["worst_objective"] = float(worst_row["objective"]) if worst_row else base_value
        entry["worst_share"] = max(shares) if shares else None
        entry["breaks_gate"] = any(not r["passed"] for r in probes)
        entry["fragile"] = bool(entry["worst_share"] is not None and entry["worst_share"] > FRAGILE_SHARE)

    worst = min(rows, key=lambda r: r["objective"]) if rows else None
    breaks = [r for r in rows if not r["passed"]]
    return {
        "objective": str(objective),
        "deltas": [float(d) for d in deltas],
        "baseline": {
            "value": base_value,
            "passed": bool(base_passed),
            "gate_reason": base_reason,
            "total_return": float(base_metrics.get("total_return", 0.0)),
            "max_drawdown": float(base_metrics.get("max_drawdown", 0.0)),
            "n_trades": float(base_metrics.get("n_trades", 0.0)),
        },
        "count": {
            "perturbations": len(rows),
            "clamped": sum(1 for r in rows if r["clamped"]),
            "gate_breaks": len(breaks),
        },
        "worst": worst,
        "gate_breaks": breaks,
        "fragile_params": sorted(e["param"] for e in by_param.values() if e["fragile"]),
        "parameters": sorted(by_param.values(), key=lambda e: str(e["param"])),
    }


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

def _fmt_pct(v: Any, digits: int = 2) -> str:
    """A signed percentage (returns keep their sign)."""
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    return f"{float(v) * 100:+.{digits}f}%"


def _fmt_mag(v: Any, digits: int = 2) -> str:
    """An unsigned percentage, for magnitudes like a drawdown."""
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    return f"{abs(float(v)) * 100:.{digits}f}%"


def degradation(mc: dict[str, Any], se: dict[str, Any]) -> dict[str, Any]:
    """The numbers the report is for: how much worse it gets, measured.

    ``path`` is the damage the *order* of the same trades can do; ``parameters`` is the
    damage a ±delta move in the knobs can do.  Both are relative to the realized
    result (the one draw we have).
    """
    base = float(se["baseline"]["value"]) if se["baseline"]["value"] is not None else 0.0
    worst = float(se["worst"]["objective"]) if se["worst"] else base
    table = mc.get("percentiles") or {}
    out: dict[str, Any] = {
        "path": None,
        "parameters": {
            "objective": se["objective"],
            "baseline": base,
            "worst": worst,
            "worst_param": se["worst"]["param"] if se["worst"] else None,
            "worst_delta": se["worst"]["delta"] if se["worst"] else None,
            "objective_share": se["worst"]["objective_share"] if se["worst"] else None,
            "improves_only": bool(
                se["worst"] and (se["worst"]["objective_share"] or 0.0) <= 0.0
            ),
            "gate_breaks": int(se["count"]["gate_breaks"]),
            "perturbations": int(se["count"]["perturbations"]),
            "break_share": (
                se["count"]["gate_breaks"] / se["count"]["perturbations"]
                if se["count"]["perturbations"]
                else 0.0
            ),
        },
    }
    if table:
        realized = mc["realized"]
        out["path"] = {
            "method": mc["method"],
            "seed": mc["seed"],
            "paths": mc["paths"],
            "realized_return": float(realized["final_return"]),
            "p5_return": float(table["final_return"]["p5"]),
            "p50_return": float(table["final_return"]["p50"]),
            "p95_return": float(table["final_return"]["p95"]),
            "return_gap_p5": float(realized["final_return"] - table["final_return"]["p5"]),
            "realized_drawdown": float(realized["max_drawdown"]),
            "p50_drawdown": float(table["max_drawdown"]["p50"]),
            "p95_drawdown": float(table["max_drawdown"]["p95"]),
            "extra_drawdown_p95": float(table["max_drawdown"]["p95"] - realized["max_drawdown"]),
            "drawdown_limit": float(mc.get("drawdown_limit", LIMITS.MAX_DRAWDOWN_PCT / 100.0)),
            "prob_drawdown_over_limit": float(mc["probabilities"]["drawdown_over_limit"]),
            "prob_final_negative": float(mc["probabilities"]["final_negative"]),
            "path_digest": mc.get("path_digest", ""),
        }
    return out


def findings(report: dict[str, Any]) -> list[str]:
    """Plain-language lines: what the numbers mean, in the order they matter."""
    out: list[str] = []
    deg = report.get("degradation") or {}
    path = deg.get("path")
    if path:
        out.append(
            f"path risk ({path['method']}, {path['paths']} paths, seed {path['seed']}): the "
            f"realized {_fmt_pct(path['realized_return'])} becomes p5 "
            f"{_fmt_pct(path['p5_return'])} / p50 {_fmt_pct(path['p50_return'])} / p95 "
            f"{_fmt_pct(path['p95_return'])} when the same {report['n_trades']} trades arrive "
            f"in a different order (a gap of {_fmt_pct(path['return_gap_p5'])} at p5)"
        )
        out.append(
            f"drawdown: realized {_fmt_mag(path['realized_drawdown'])}, p50 "
            f"{_fmt_mag(path['p50_drawdown'])}, p95 {_fmt_mag(path['p95_drawdown'])} - "
            f"{_fmt_mag(path['extra_drawdown_p95'], 1)} worse at p95; "
            f"P(drawdown over the sealed {_fmt_mag(path['drawdown_limit'], 1)}) = "
            f"{path['prob_drawdown_over_limit'] * 100:.1f}% and P(ending negative) = "
            f"{path['prob_final_negative'] * 100:.1f}%"
        )
        out.append(f"reproducible: seed {path['seed']}, path digest {path['path_digest'][:16]}...")
    else:
        out.append("path risk: not measurable - the window closed no trades")

    par = deg.get("parameters")
    if par:
        share = par["objective_share"]
        text = (
            f"parameter risk: {par['perturbations']} perturbations around the realized "
            f"{report['objective']} of {par['baseline']:.3f}; the worst leaves it at "
            f"{par['worst']:.3f}"
        )
        if par["worst_param"]:
            text += f" ({par['worst_param']} at {par['worst_delta'] * 100:+.0f}%)"
        if share is None:
            text += ", too close to zero for a share to mean anything"
        elif par.get("improves_only"):
            text += ", and no perturbation is actually worse than the baseline"
        else:
            text += f", a degradation of {share * 100:.1f}%"
        out.append(text)
        if report["sensitivity"]["fragile_params"]:
            out.append(
                "fragile to a small move: " + ", ".join(report["sensitivity"]["fragile_params"])
            )
        breaks = par["gate_breaks"]
        out.append(
            f"gate breaks: {breaks} of {par['perturbations']} perturbations "
            f"({par['break_share'] * 100:.0f}%) fail the evaluation gates that the "
            f"unperturbed genome passes"
            if breaks
            else f"gate breaks: none of {par['perturbations']} perturbations trips "
            "n_trades / max_drawdown - the gates are not sitting on a knife edge"
        )
    return out


def summary_line(report: dict[str, Any]) -> str:
    """One line, in the register of the autopsy and the critic."""
    deg = report.get("degradation") or {}
    path = deg.get("path")
    par = deg.get("parameters") or {}
    if not path:
        return (
            f"Stress ({report['window']}, {report['family']}): no closed trades on the "
            "window, so neither path risk nor parameter sensitivity could be measured."
        )
    return (
        f"Stress ({report['window']}, {report['family']}, {path['method']}, seed "
        f"{path['seed']}): realized {_fmt_pct(path['realized_return'])} with a "
        f"{_fmt_mag(path['realized_drawdown'])} drawdown; resampling the same "
        f"{report['n_trades']} trades puts the drawdown at p95 "
        f"{_fmt_mag(path['p95_drawdown'])}, and {_share_phrase(par, report['objective'])}"
    )


def _share_phrase(par: dict[str, Any], objective: str) -> str:
    """How to say the parameter-side result, including 'it only gets better'."""
    share = par.get("objective_share")
    if share is None:
        return f"the objective {objective} sits too close to zero for a relative move to mean anything"
    if par.get("improves_only"):
        return f"no ±delta move of the parameters is worse than the baseline {objective}"
    return f"the worst ±delta move of the parameters costs {abs(float(share)) * 100:.1f}% of the {objective}"


def _pct_or_na(v: Any, digits: int = 1) -> str:
    """A share of the objective, unsigned (the direction is said in words)."""
    return "n/a" if v is None else f"{abs(float(v)) * 100:.{digits}f}%"


def lesson_text(report: dict[str, Any]) -> str:
    """What goes into the memory so the next generation inherits it."""
    if not (report.get("degradation") or {}).get("path"):
        return (
            f"Stress ({report['window'] or 'window'}, {report['family']}): no closed "
            "trades, so no path risk or sensitivity was measured."
        )
    return " ".join([summary_line(report)] + findings(report)[1:3])


def stress_report(
    df: pd.DataFrame,
    genome: dict[str, Any],
    settings: Any,
    *,
    window: str = "holdout",
    paths: int = 2000,
    seed: int = 7,
    method: str = "bootstrap",
    block: int = 5,
    deltas: Sequence[float] = (0.10, 0.25),
    include_risk: bool = True,
    origin: str = "",
) -> dict[str, Any]:
    """Both axes on one deployment window, plus the degradation summary.

    The window is the same one ``critic`` and ``autopsy`` use, and for the same reason:
    this is *evidence* about the genome that was selected elsewhere, not a new search.
    Nothing here re-selects, and nothing here is fed back into ``evolve``.
    """
    from .backtest import simulate
    from .critic import worst_daily_loss
    from .invest import select_window

    g = validate_genome(genome)
    cut = select_window(df, settings, window)
    bt = settings.backtest
    ev = settings.evaluation

    def run(candidate: dict[str, Any]) -> dict[str, Any]:
        res = simulate(
            cut,
            candidate,
            initial_cash=bt.initial_cash,
            fee_bps=bt.fee_bps,
            slippage_bps=bt.slippage_bps,
            position_size=bt.position_size,
            allow_short=bt.allow_short,
            timeframe=settings.data.timeframe,
        )
        return dict(res.metrics)

    baseline_metrics = run(g)
    res = simulate(
        cut,
        g,
        initial_cash=bt.initial_cash,
        fee_bps=bt.fee_bps,
        slippage_bps=bt.slippage_bps,
        position_size=bt.position_size,
        allow_short=bt.allow_short,
        timeframe=settings.data.timeframe,
    )
    returns = trade_returns(res.trades)
    mc = monte_carlo(
        returns,
        paths=paths,
        seed=seed,
        method=method,
        block=block,
        initial_cash=bt.initial_cash,
    )
    se = sensitivity(
        g,
        run,
        objective=ev.metric,
        deltas=deltas,
        min_trades=int(ev.min_trades),
        max_drawdown=float(ev.max_drawdown),
        include_risk=include_risk,
        baseline=baseline_metrics,
    )

    from .genome import describe

    report: dict[str, Any] = {
        "window": window,
        "bars": int(len(cut)),
        "origin": origin,
        "family": g["family"],
        "description": describe(g),
        "genome": g,
        "objective": ev.metric,
        "n_trades": int(len(returns)),
        "open_at_end": int(len(res.trades) - len(returns)),
        "baseline": {
            **se["baseline"],
            "total_return": float(baseline_metrics.get("total_return", 0.0)),
            "max_drawdown": float(baseline_metrics.get("max_drawdown", 0.0)),
            "n_trades": float(baseline_metrics.get("n_trades", 0.0)),
            "worst_daily_loss": float(worst_daily_loss(res.equity)),
            "final_equity": float(baseline_metrics.get("final_equity", bt.initial_cash)),
        },
        "monte_carlo": mc,
        "sensitivity": se,
    }
    report["degradation"] = degradation(mc, se)
    report["findings"] = findings(report)
    report["summary"] = summary_line(report)
    report["lesson"] = lesson_text(report)
    return report


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def console_lines(report: dict[str, Any]) -> list[str]:
    out = [summary_line(report)]
    if not report["n_trades"]:
        return out
    mc = report["monte_carlo"]
    out.append(
        f"  trades: {report['n_trades']} closed, {report['open_at_end']} open at the end; "
        f"mean {_fmt_pct(mc['returns']['mean'])}, win share "
        f"{mc['returns']['win_share'] * 100:.0f}%, worst {_fmt_pct(mc['returns']['min'])}"
    )
    out.append("  path percentiles (resampled, realized in the last column):")
    out.append(
        "    metric                p5       p25       p50       p75       p95   realized"
    )
    for key in PATH_METRICS:
        row = mc["percentiles"][key]
        cells = "".join(f"{row[f'p{q}']:>9.4f}" for q in PERCENTILES)
        out.append(f"    {key:<18}{cells}{row['realized']:>11.4f}")
    probs = mc["probabilities"]
    out.append(
        f"  probabilities: P(final<0)={probs['final_negative'] * 100:.1f}% "
        f"P(drawdown>{mc['drawdown_limit'] * 100:.1f}%)={probs['drawdown_over_limit'] * 100:.1f}% "
        f"P(streak>=10)={probs['streak_at_least_ten'] * 100:.1f}%"
    )
    out.append(
        f"  realized worst day: {_fmt_pct(report['baseline']['worst_daily_loss'])} "
        "(the calendar cannot be resampled; the realized day is the reference)"
    )
    se = report["sensitivity"]
    out.append(
        f"  sensitivity: objective {se['objective']} = {se['baseline']['value']:.4f}, "
        f"{se['count']['perturbations']} perturbations "
        f"({se['count']['clamped']} clamped by the genome bounds), "
        f"{se['count']['gate_breaks']} gate breaks"
    )
    for param in report["sensitivity"]["parameters"]:
        worst = min(param["perturbations"], key=lambda p: p["objective"])
        share = worst["objective_share"]
        out.append(
            f"    - {param['param']:<18} worst {worst['objective']:>9.4f} "
            f"({worst['delta'] * 100:+.0f}%"
            + (
                ", no perturbation worse than the baseline"
                if share is not None and share <= 0.0
                else (f", {_pct_or_na(share)} of the objective" if share is not None else "")
            )
            + f", dd {_fmt_mag(worst['max_drawdown'])}, "
            + ("breaks the gates" if not worst["passed"] else "gates hold")
            + (", clamped" if worst["clamped"] else "")
            + ")"
        )
    out.append("  findings:")
    for line in report["findings"]:
        out.append(f"    * {line}")
    return out


def render_markdown(report: dict[str, Any]) -> str:
    L: list[str] = [f"## Stress ({report['window'] or 'window'})", ""]
    L += [summary_line(report), ""]
    if not report["n_trades"]:
        return "\n".join(L)
    mc = report["monte_carlo"]
    deg = report["degradation"]
    L += [
        f"- genome: `{report['description']}`",
        f"- closed trades: {report['n_trades']} ({report['open_at_end']} open at the end), "
        f"objective `{report['objective']}` = {report['baseline']['value']:.4f}",
        f"- resampling: {mc['method']}, {mc['paths']} paths, seed {mc['seed']}, "
        f"path digest `{mc['path_digest'][:16]}`",
        "",
        "### Path risk (Monte Carlo over the trade sequence)",
        "",
        "| statistic | p5 | p25 | p50 | p75 | p95 | realized |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key in PATH_METRICS:
        row = mc["percentiles"][key]
        L.append(
            f"| {key} | " + " | ".join(f"{row[f'p{q}']:.4f}" for q in PERCENTILES)
            + f" | {row['realized']:.4f} |"
        )
    probs = mc["probabilities"]
    L += [
        "",
        f"- P(ending negative): {probs['final_negative'] * 100:.1f}%",
        f"- P(drawdown over the sealed {mc['drawdown_limit'] * 100:.1f}%): "
        f"{probs['drawdown_over_limit'] * 100:.1f}%",
        f"- P(drawdown worse than the realized one): "
        f"{probs['drawdown_worse_than_realized'] * 100:.1f}%",
        f"- P(at least 10 losses in a row): {probs['streak_at_least_ten'] * 100:.1f}%",
        f"- realized worst calendar day: {_fmt_pct(report['baseline']['worst_daily_loss'])} "
        "(not resampled: a bootstrap destroys which trades shared a day)",
        "",
        "### Parameter sensitivity",
        "",
        f"Objective `{report['objective']}`, baseline {report['baseline']['value']:.4f} "
        f"({'passes' if report['baseline']['passed'] else 'fails'} the evaluation gates).",
        "",
        "| parameter | base | value | change | objective | degradation | drawdown | gates |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for param in report["sensitivity"]["parameters"]:
        for p in param["perturbations"]:
            L.append(
                f"| {p['param']}{' (risk)' if p['path'] == 'risk' else ''} | {p['base']:.4g} | "
                f"{p['value']:.4g} | {p['delta'] * 100:+.0f}% | {p['objective']:.4f} | "
                f"{_pct_or_na(p['objective_share'])} | {_fmt_mag(p['max_drawdown'])} | "
                + ("ok" if p["passed"] else "**breaks**")
                + (" *clamped*" if p["clamped"] else "")
                + " |"
            )
    L += [
        "",
        f"- perturbations: {report['sensitivity']['count']['perturbations']} "
        f"({report['sensitivity']['count']['clamped']} clamped by the genome bounds, so the "
        "value column is what actually ran)",
        f"- gate breaks: {report['sensitivity']['count']['gate_breaks']}",
        f"- fragile to a ±delta move (> {FRAGILE_SHARE * 100:.0f}% of the objective): "
        f"{', '.join(report['sensitivity']['fragile_params']) or 'nothing above the threshold'}",
        "",
        "### Measured degradation",
        "",
        f"- path: return p5 {_fmt_pct(deg['path']['p5_return'])} vs realized "
        f"{_fmt_pct(deg['path']['realized_return'])} (gap "
        f"{_fmt_pct(deg['path']['return_gap_p5'])}); drawdown p95 "
        f"{_fmt_mag(deg['path']['p95_drawdown'])} vs realized "
        f"{_fmt_mag(deg['path']['realized_drawdown'])} ("
        f"{_fmt_mag(deg['path']['extra_drawdown_p95'], 1)} worse)",
        f"- parameters: worst case {deg['parameters']['worst']:.4f} vs baseline "
        f"{deg['parameters']['baseline']:.4f}"
        + (
            f" ({deg['parameters']['worst_param']} at "
            f"{deg['parameters']['worst_delta'] * 100:+.0f}%)"
            if deg["parameters"]["worst_param"]
            else ""
        )
        + ", "
        + _share_phrase(deg["parameters"], report["objective"]),
        f"- gate breaks from parameter risk: {deg['parameters']['gate_breaks']} of "
        f"{deg['parameters']['perturbations']}",
        "",
        "### Findings",
        "",
    ]
    L += [f"* {line}" for line in report["findings"]]
    L += [
        "",
        "Resampling assumes trades are exchangeable and sensitivity is a measured "
        "fragility, not a probability: neither is a forecast.",
        "",
    ]
    return "\n".join(L)
