"""The critic: the agent that refuses to deploy a strategy that breaks a limit.

Modelled on the line the demo puts on screen - *"That fails: -66.5% drawdown
breaks your -20% limit. I won't deploy it."* - a refusal is a **first-class
result** here: it carries the numbers that produced it, it is recorded, and it
hands the strategy back instead of quietly shipping it.

The critic judges the **out-of-sample** behaviour of a genome against the sealed
limits in :mod:`selftrade.risk_rules`, and it is wired into the deployment rail
(``paper --create``), so the refusal has consequences rather than being a print
statement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .backtest import BacktestResult, simulate
from .genome import validate_genome
from .risk_rules import LIMITS, RiskLimits, is_sealed, verify_seal


@dataclass
class Verdict:
    """What the critic decided, with the evidence attached."""

    approved: bool
    text: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "text": self.text,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "checks": [dict(c) for c in self.checks],
        }


def worst_daily_loss(equity: pd.Series) -> float:
    """Worst single calendar-day equity move (negative or 0).

    Needs a datetime index (what the engine always produces); anything else is
    reported as 0 rather than raising, because a diagnosis must never be the
    thing that breaks a run.
    """
    if equity is None or len(equity) < 3:
        return 0.0
    if not isinstance(equity.index, pd.DatetimeIndex):
        return 0.0
    daily = equity.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    if not len(rets):
        return 0.0
    return float(min(0.0, rets.min()))


def _check(name: str, value: float, limit: float, ok: bool, detail: str) -> dict[str, Any]:
    return {
        "name": name,
        "value": float(value),
        "limit": float(limit),
        "ok": bool(ok),
        "detail": detail,
    }


def judge(
    metrics: dict[str, Any],
    *,
    genome: dict[str, Any],
    equity: pd.Series | None = None,
    trades: pd.DataFrame | None = None,
    limits: RiskLimits = LIMITS,
    settings: Any = None,
    apply_seal: bool = True,
    min_trades: int = 8,
) -> Verdict:
    """Decide whether this strategy may be deployed.

    Hard checks refuse; warnings are recorded without blocking.
    """
    g = validate_genome(genome) if genome else {"risk": {"stop_loss_pct": 0.0}}
    risk = g.get("risk", {})
    reasons: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    # --- 1. the sealed layer must itself be intact -------------------------
    if not is_sealed(limits):
        reasons.append("the sealed risk layer is not in its sealed configuration")
    if apply_seal:
        seal = verify_seal(settings, limits)
        checks.append(
            _check("seal", 0.0, 0.0, seal["ok"], f"{seal['reason']} [{seal['path']}]")
        )
        if not seal["ok"]:
            reasons.append(f"seal broken: {seal['reason']}")
    else:
        checks.append(_check("seal", 0.0, 0.0, True, "seal check skipped by the caller"))

    # --- 2. drawdown -------------------------------------------------------
    dd_pct = float(metrics.get("max_drawdown", 0.0)) * 100.0
    dd_ok = dd_pct <= limits.MAX_DRAWDOWN_PCT + 1e-9
    checks.append(
        _check(
            "max_drawdown",
            dd_pct,
            limits.MAX_DRAWDOWN_PCT,
            dd_ok,
            f"{dd_pct:.1f}% out-of-sample drawdown vs the sealed {limits.MAX_DRAWDOWN_PCT:.1f}% limit",
        )
    )
    if not dd_ok:
        reasons.append(
            f"{dd_pct:.1f}% drawdown breaks the sealed {limits.MAX_DRAWDOWN_PCT:.1f}% limit"
        )

    # --- 3. the stop must be tight enough to be honoured -------------------
    stop_pct = abs(float(risk.get("stop_loss_pct") or 0.0)) * 100.0
    stop_ok = 0.0 < stop_pct <= limits.HARD_STOP_LOSS_PCT + 1e-9
    checks.append(
        _check(
            "hard_stop",
            stop_pct,
            limits.HARD_STOP_LOSS_PCT,
            stop_ok,
            f"stop at {stop_pct:.2f}% vs the sealed {limits.HARD_STOP_LOSS_PCT:.1f}% hard stop",
        )
    )
    if stop_pct <= 0:
        reasons.append("the genome has no stop loss; the sealed layer requires one")
    elif not stop_ok:
        reasons.append(
            f"a {stop_pct:.1f}% stop is looser than the sealed "
            f"{limits.HARD_STOP_LOSS_PCT:.1f}% hard stop"
        )

    # --- 4. worst day ------------------------------------------------------
    day_loss = abs(worst_daily_loss(equity) if equity is not None else 0.0) * 100.0
    day_ok = day_loss <= limits.MAX_DAILY_LOSS_PCT + 1e-9
    checks.append(
        _check(
            "max_daily_loss",
            day_loss,
            limits.MAX_DAILY_LOSS_PCT,
            day_ok,
            f"worst day {day_loss:.2f}% vs the sealed {limits.MAX_DAILY_LOSS_PCT:.1f}% daily halt",
        )
    )
    if not day_ok:
        reasons.append(
            f"the worst day lost {day_loss:.2f}%, past the sealed "
            f"{limits.MAX_DAILY_LOSS_PCT:.1f}% daily halt"
        )

    # --- 5. it has to actually trade --------------------------------------
    n_trades = int(metrics.get("n_trades", 0) or 0)
    trades_ok = n_trades > 0
    checks.append(
        _check(
            "trades",
            float(n_trades),
            float(min_trades),
            trades_ok,
            f"{n_trades} closed trade(s) out-of-sample",
        )
    )
    if not trades_ok:
        reasons.append("the strategy never traded out-of-sample, so there is nothing to deploy")

    # --- warnings: not blocking, but they belong in the verdict ------------
    configured = float(getattr(getattr(settings, "backtest", None), "position_size", 0.0) or 0.0)
    if configured * 100.0 > limits.MAX_POSITION_PCT + 1e-9:
        warnings.append(
            f"research sizing is {configured * 100:.0f}% of equity; the live rail will be "
            f"clamped to the sealed {limits.MAX_POSITION_PCT:.1f}% per position "
            f"(research, paper and backtest stay comparable on purpose)"
        )
    if 0 < n_trades < max(min_trades * 2, 12):
        warnings.append(
            f"only {n_trades} trades: too small a sample to separate skill from luck"
        )
    top3 = float(metrics.get("_top3_share", 0.0) or 0.0)
    if top3 <= 0.0 and trades is not None and len(trades):
        top3 = _top3_share(trades)
    if top3 > 0.6:
        warnings.append(
            f"the 3 best trades produced {top3 * 100:.0f}% of gross profit: the result "
            "rests on a handful of trades"
        )

    approved = not reasons
    if approved:
        text = (
            f"Approved: {dd_pct:.1f}% drawdown and a {stop_pct:.2f}% stop are inside the "
            f"sealed limits ({limits.MAX_DRAWDOWN_PCT:.0f}% / "
            f"{limits.HARD_STOP_LOSS_PCT:.0f}%)."
        )
    else:
        text = "Refused: " + "; ".join(reasons) + ". I will not deploy it."
    return Verdict(approved=approved, text=text, reasons=reasons, warnings=warnings, checks=checks)


def verdict_for_genome(
    settings: Any,
    genome: dict[str, Any],
    df: pd.DataFrame,
    *,
    window: str = "holdout",
    limits: RiskLimits = LIMITS,
    apply_seal: bool = True,
) -> tuple[Verdict, BacktestResult, pd.DataFrame]:
    """Run the genome on the out-of-sample window and let the critic judge it."""
    from .invest import select_window  # local import: invest already imports backtest

    cut = select_window(df, settings, window)
    bt = settings.backtest
    res = simulate(
        cut,
        validate_genome(genome),
        initial_cash=bt.initial_cash,
        fee_bps=bt.fee_bps,
        slippage_bps=bt.slippage_bps,
        position_size=bt.position_size,
        allow_short=bt.allow_short,
        timeframe=settings.data.timeframe,
    )
    metrics = dict(res.metrics)
    metrics["_top3_share"] = _top3_share(res.trades)
    verdict = judge(
        metrics,
        genome=genome,
        equity=res.equity,
        limits=limits,
        settings=settings,
        apply_seal=apply_seal,
        min_trades=int(getattr(settings.evaluation, "min_trades", 8)),
    )
    verdict.checks.append(
        _check(
            "window",
            float(len(cut)),
            0.0,
            True,
            f"judged on the '{window}' window ({len(cut)} bars)",
        )
    )
    return verdict, res, cut


def _top3_share(trades: pd.DataFrame) -> float:
    closed = trades[trades["exit_reason"] != "end_of_data"] if len(trades) else trades
    if not len(closed):
        return 0.0
    wins = closed[closed["pnl"] > 0]["pnl"].sort_values(ascending=False)
    gross = float(closed[closed["pnl"] > 0]["pnl"].sum())
    if gross <= 0 or not len(wins):
        return 0.0
    return float(wins.head(3).sum() / gross)


def render_markdown(verdict: Verdict | dict[str, Any]) -> str:
    """Markdown for the verdict; accepts the dataclass or its ``as_dict()`` form."""
    if not isinstance(verdict, Verdict):
        verdict = Verdict(
            approved=bool(verdict.get("approved")),
            text=str(verdict.get("text", "")),
            reasons=list(verdict.get("reasons", [])),
            warnings=list(verdict.get("warnings", [])),
            checks=list(verdict.get("checks", [])),
        )
    lines = [f"**{verdict.text}**", "", "| check | value | limit | ok | detail |", "| --- | ---: | ---: | --- | --- |"]
    for c in verdict.checks:
        mark = "yes" if c["ok"] else "**NO**"
        lines.append(
            f"| {c['name']} | {c['value']:.2f} | {c['limit']:.2f} | {mark} | {c['detail']} |"
        )
    if verdict.warnings:
        lines += ["", "Warnings (recorded, not blocking):", ""]
        lines += [f"* {w}" for w in verdict.warnings]
    return "\n".join(lines)
