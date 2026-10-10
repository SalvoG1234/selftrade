"""Tests for the autopsy: causal context, worst losses, clusters, caveats."""

import numpy as np
import pandas as pd
import pytest

from selftrade.autopsy import (
    autopsy,
    cluster_modes,
    console_lines,
    context_table,
    lesson_text,
    overfit_warnings,
    render_markdown,
    summary_line,
    trade_stats,
)
from selftrade.data import make_synthetic
from selftrade.genome import sample_genome

GENOME = sample_genome("trend_momentum", np.random.default_rng(5))


@pytest.fixture(scope="module")
def df():
    return make_synthetic(420, seed=23)


@pytest.fixture(scope="module")
def ctx(df):
    return context_table(df)


def make_trades(rows) -> pd.DataFrame:
    """rows: list of (entry_i, bars_held, pnl, exit_reason, direction)."""
    recs = []
    for entry_i, bars, pnl, reason, direction in rows:
        recs.append(
            {
                "direction": direction,
                "entry_time": _IDX[entry_i],
                "exit_time": _IDX[min(entry_i + bars, len(_IDX) - 1)],
                "entry_price": 100.0,
                "exit_price": 100.0 + pnl / 10.0,
                "size": 10.0,
                "pnl": float(pnl),
                "return_pct": float(pnl) / 1000.0,
                "bars_held": int(bars),
                "exit_reason": reason,
            }
        )
    return pd.DataFrame(recs)


_IDX: pd.DatetimeIndex = None  # filled by the fixture below


@pytest.fixture(autouse=True)
def _bind_index(df):
    global _IDX
    _IDX = df.index


# --------------------------------------------------------------- causality


def test_context_uses_only_past_bars(df):
    full = context_table(df)
    cut = context_table(df.iloc[:300])
    for col in ("rsi", "atr_pct", "dist_sma200_pct"):
        pd.testing.assert_series_equal(
            full.loc[cut.index, col], cut[col], check_names=False
        )


def test_context_has_no_lookahead_under_a_future_shock(df):
    shocked = df.copy()
    shocked.iloc[300:, shocked.columns.get_loc("close")] *= 3.0
    a = context_table(df).iloc[:300]
    b = context_table(shocked).iloc[:300]
    pd.testing.assert_series_equal(a["rsi"], b["rsi"], check_names=False)
    pd.testing.assert_series_equal(a["atr_pct"], b["atr_pct"], check_names=False)


# ------------------------------------------------------------ the report


def test_worst_losses_ranked_with_entry_context(df, ctx):
    below = ctx.index[(ctx["above_200d"] == False)].tolist()  # noqa: E712
    above = ctx.index[(ctx["above_200d"] == True)].tolist()  # noqa: E712
    assert below and above, "synthetic frame must contain both regimes"
    i_below = df.index.get_loc(below[len(below) // 2])
    i_above = df.index.get_loc(above[len(above) // 2])
    trades = make_trades(
        [
            (i_below, 2, -120.0, "stop", "long"),
            (i_above, 9, -20.0, "signal", "long"),
            (i_above, 30, 45.0, "target", "long"),
        ]
    )
    rep = autopsy(trades, df, GENOME, window_label="holdout", origin="champion")

    assert rep["n_trades"] == 3 and rep["n_losers"] == 2
    assert rep["loser_share"] == pytest.approx(2 / 3)
    assert [w["rank"] for w in rep["worst"]] == [1, 2]
    assert rep["worst"][0]["pnl"] == pytest.approx(-120.0)
    assert "below the 200-period average" in rep["worst"][0]["context"]
    assert "held 2 bars" in rep["worst"][0]["context"]
    assert rep["worst_total"] == pytest.approx(-140.0)
    assert rep["exit_mix"] == {"stop": 1, "signal": 1, "target": 1}


def test_modes_cluster_losers_and_contrast_with_winners(df, ctx):
    below = ctx.index[(ctx["above_200d"] == False)].tolist()  # noqa: E712
    above = ctx.index[(ctx["above_200d"] == True)].tolist()  # noqa: E712
    i_below = df.index.get_loc(below[len(below) // 2])
    i_above = df.index.get_loc(above[len(above) // 2])
    trades = make_trades(
        [
            (i_below, 2, -100.0, "stop", "long"),
            (i_below, 2, -60.0, "stop", "long"),
            (i_above, 25, -30.0, "signal", "long"),
            (i_above, 40, 90.0, "target", "long"),
        ]
    )
    rep = autopsy(trades, df, GENOME, window_label="holdout")
    modes = {m["key"]: m for m in rep["modes"]}
    assert "counter_trend" in modes
    assert modes["counter_trend"]["count"] == 2
    assert modes["counter_trend"]["winner_count"] == 0
    assert modes["fast_stop"]["count"] == 2
    assert modes["slow_exit"]["count"] == 1
    assert modes["stop_heavy"]["count"] == 2
    assert all(m["fix"] for m in rep["modes"])
    assert rep["modes"][0]["count"] >= rep["modes"][-1]["count"]  # sorted by weight


def test_losers_vs_winners_comparison(df, ctx):
    above = ctx.index[(ctx["above_200d"] == True)].tolist()  # noqa: E712
    i_above = df.index.get_loc(above[len(above) // 2])
    trades = make_trades([(i_above, 5, -50.0, "stop", "long"), (i_above, 50, 80.0, "signal", "long")])
    rep = autopsy(trades, df, GENOME)
    comp = rep["losers_vs_winners"]
    assert comp["bars_held"]["losers"] == pytest.approx(5)
    assert comp["bars_held"]["winners"] == pytest.approx(50)
    assert comp["atr_pct"]["unit"] == "%"


def test_open_position_is_not_a_closed_trade_and_never_the_worst(df, ctx):
    above = ctx.index[(ctx["above_200d"] == True)].tolist()  # noqa: E712
    i = df.index.get_loc(above[len(above) // 2])
    trades = make_trades(
        [
            (i, 4, -40.0, "stop", "long"),
            (i, 6, -999.0, "end_of_data", "long"),
        ]
    )
    rep = autopsy(trades, df, GENOME)
    assert rep["n_trades"] == 1
    assert rep["open_at_end"] == 1
    assert rep["worst"][0]["pnl"] == pytest.approx(-40.0)
    assert rep["n_losers"] == 1


def test_summary_and_lesson_are_actionable(df, ctx):
    above = ctx.index[(ctx["above_200d"] == True)].tolist()  # noqa: E712
    i = df.index.get_loc(above[len(above) // 2])
    trades = make_trades([(i, 2, -50.0, "stop", "long"), (i, 40, 10.0, "signal", "long")])
    rep = autopsy(trades, df, GENOME, window_label="holdout")
    assert rep["summary"].startswith("Loaded 2 trades - 1 losers")
    assert "Dominant failure mode" in rep["summary"]
    lesson = lesson_text(rep)
    assert "Rules to change next generation" in lesson
    assert "fix" not in lesson.lower() or "->" in lesson  # fixes are inlined
    lines = console_lines(rep)
    assert lines[0] == rep["summary"]
    assert any("failure modes:" in line for line in lines)


def test_no_trades_is_reported_honestly(df):
    empty = pd.DataFrame(
        columns=[
            "direction",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "size",
            "pnl",
            "return_pct",
            "bars_held",
            "exit_reason",
        ]
    )
    rep = autopsy(empty, df, GENOME, window_label="holdout")
    assert rep["n_trades"] == 0
    assert rep["worst"] == [] and rep["modes"] == []
    assert rep["summary"] == "Loaded 0 closed trades: nothing to autopsy."
    assert console_lines(rep) == [rep["summary"]]
    assert "nothing to autopsy" in render_markdown(rep)
    assert trade_stats(empty)["n_trades"] == 0


def test_overfit_warnings_fire(df, ctx):
    above = ctx.index[(ctx["above_200d"] == True)].tolist()  # noqa: E712
    i = df.index.get_loc(above[len(above) // 2])
    thin = make_trades([(i, 3, -10.0, "stop", "long"), (i, 3, 10.0, "stop", "long")])
    warns = overfit_warnings(thin, min_trades=8)
    assert any("too small a sample" in w for w in warns)

    concentrated = make_trades(
        [(i, 3, 1000.0, "target", "long"), (i, 3, 10.0, "target", "long")]
        + [(i, 3, -600.0, "stop", "long")]
        + [(i, 3, -50.0, "stop", "long")] * 7  # 10 closed trades, so tail risk is checked
    )
    warns = overfit_warnings(concentrated, min_trades=4)
    assert any("3 best trades" in w for w in warns)
    assert any("tail risk" in w for w in warns)
    assert overfit_warnings(make_trades([])) == ["no closed trades at all"]


def test_render_markdown_has_the_tables(df, ctx):
    above = ctx.index[(ctx["above_200d"] == True)].tolist()  # noqa: E712
    i = df.index.get_loc(above[len(above) // 2])
    trades = make_trades([(i, 2, -50.0, "stop", "long"), (i, 20, -10.0, "signal", "long")])
    md = render_markdown(autopsy(trades, df, GENOME, window_label="holdout"))
    assert "## Autopsy (holdout)" in md
    assert "### The worst losses" in md
    assert "### Failure modes across all losers" in md
    assert "### Losers vs winners" in md
    assert md.count("| ---") >= 3


def test_cluster_modes_is_empty_without_losers(df, ctx):
    above = ctx.index[(ctx["above_200d"] == True)].tolist()  # noqa: E712
    i = df.index.get_loc(above[len(above) // 2])
    trades = make_trades([(i, 20, 30.0, "target", "long")])
    enriched_winners = trades
    assert cluster_modes(make_trades([]), enriched_winners) == []
    assert summary_line(autopsy(trades, df, GENOME)) .startswith("Loaded 1 trades - 0 losers")
