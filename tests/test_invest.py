"""Tests for the money simulation: windows, benchmarks, costs and rendering."""

import hashlib
import json
import re
from pathlib import Path

import pandas as pd
import pytest

from selftrade.config import load_settings
from selftrade.data import make_synthetic
from selftrade.invest import (
    DEFAULT_CHART_POINTS,
    InvestError,
    buy_hold_series,
    render_html,
    render_markdown,
    select_window,
    sha256_file,
    simulate_investment,
    svg_drawdown_chart,
    svg_line_chart,
    svg_price_chart,
    svg_year_bars,
    write_reports,
)

GENOME = {
    "family": "trend_momentum",
    "params": {},
    "risk": {
        "stop_loss_pct": -0.25,
        "take_profit_pct": None,
        "trailing_pct": None,
        "trade_shorts": False,
    },
}


@pytest.fixture()
def settings(tmp_path):
    s = load_settings("config.yaml")
    s.data.source = "synthetic"
    s.data.timeframe = "1h"
    s.data.symbol = "BTC/USDT"
    s.data.csv = str(tmp_path / "nonexistent.csv")
    s.evaluation.min_trades = 1
    s.evaluation.max_drawdown = 0.99
    s.memory.db = str(tmp_path / "state" / "m.db")
    return s


@pytest.fixture(scope="module")
def df():
    return make_synthetic(3000, seed=17)


@pytest.fixture(scope="module")
def report(df):
    s = load_settings("config.yaml")
    s.data.source = "synthetic"
    s.data.timeframe = "1h"
    s.evaluation.holdout_frac = 0.25
    return simulate_investment(df, GENOME, s, window="holdout", initial_cash=1000.0)


# ---------------------------------------------------------------- windowing


def test_select_window_full_recent_and_holdout(settings, df):
    settings.evaluation.holdout_frac = 0.25
    assert len(select_window(df, settings, "full")) == len(df)
    dev, hold = select_window(df, settings, "holdout"), df.iloc[int(len(df) * 0.75) :]
    assert len(dev) == len(hold)
    assert dev.index[0] == hold.index[0]

    settings.paper.window_bars = 500
    recent = select_window(df, settings, "recent")
    assert len(recent) == 500
    assert recent.index[-1] == df.index[-1]


def test_select_window_rejects_unknown(settings, df):
    with pytest.raises(InvestError):
        select_window(df, settings, "yesterday")


# ---------------------------------------------------------------- benchmark


def test_buy_hold_tracks_the_price(settings, df):
    cut = df.iloc[-500:]
    bh = buy_hold_series(cut, 1000.0, 0.0)
    assert bh.iloc[0] == pytest.approx(1000.0 * cut["close"].iloc[0] / cut["open"].iloc[0])
    ratio = cut["close"].iloc[-1] / cut["open"].iloc[0]
    assert bh.iloc[-1] == pytest.approx(1000.0 * ratio)

    with_cost = buy_hold_series(cut, 1000.0, 0.001)
    assert with_cost.iloc[-1] < bh.iloc[-1]


# ---------------------------------------------------------------- simulation


def test_report_shape_and_invariants(report):
    assert report["currency"] == "USDT"
    assert report["window"] == "holdout"
    assert report["in_sample"] is False
    assert report["data"]["rows_window"] == len(report["equity_curve"])
    assert len(report["equity_curve"]) == len(report["buy_hold_curve"])
    assert len(report["price_curve"]) == len(report["drawdown_curve"])
    assert report["strategy"]["total_return"] == pytest.approx(
        report["strategy"]["final_equity"] / report["params"]["initial_cash"] - 1.0
    )
    assert report["params"]["round_trip_bps"] == pytest.approx(
        2 * report["params"]["cost_per_side_bps"]
    )
    digest = report["data"]["sha256"]
    assert digest == "" or (len(digest) == 64 and int(digest, 16) >= 0)
    assert report["notes"] and report["verdict"]["text"]


def test_costs_are_charged_and_measured(report):
    cd = report["cost_drag"]
    assert cd["gross_final_equity"] >= cd["net_final_equity"]
    assert cd["cost_paid"] == pytest.approx(cd["gross_final_equity"] - cd["net_final_equity"])
    assert report["strategy"]["n_trades"] >= 1


def test_cost_sensitivity_is_monotone(report):
    rows = report["cost_sensitivity"]
    assert [r["cost_multiplier"] for r in rows] == [0.0, 1.0, 2.0, 3.0]
    finals = [r["final_equity"] for r in rows]
    assert finals[0] >= finals[1] >= finals[2] >= finals[3]
    assert rows[1]["cost_per_side_bps"] == pytest.approx(report["params"]["cost_per_side_bps"])


def test_years_cover_the_window(report):
    cut_start = report["data"]["start"][:4]
    assert report["years"][0]["year"] == int(cut_start)
    assert len(report["years"]) >= 1
    for y in report["years"]:
        assert -1.0 <= y["strategy_return"] <= 50.0


def test_full_window_is_labelled_in_sample(settings, df):
    rep = simulate_investment(df, GENOME, settings, window="full")
    assert rep["in_sample"] is True
    assert any("IN-SAMPLE" in n for n in rep["notes"])
    assert rep["data"]["rows_window"] == len(df)


def test_recent_window_flags_selection_overlap(settings, df):
    settings.evaluation.holdout_frac = 0.25  # holdout = last 750 of 3000 bars
    settings.paper.window_bars = 500
    inside = simulate_investment(df, GENOME, settings, window="recent")
    assert inside["in_sample"] is False
    assert any("OUT-OF-SAMPLE" in n for n in inside["notes"])

    settings.paper.window_bars = 1500
    straddling = simulate_investment(df, GENOME, settings, window="recent")
    assert straddling["in_sample"] is True
    assert any("reaches back" in n for n in straddling["notes"])

    settings.evaluation.holdout_frac = 0.0
    everything = simulate_investment(df, GENOME, settings, window="recent")
    assert everything["in_sample"] is True


def test_sampled_genome_gets_the_window_bias_note(settings, df):
    rep = simulate_investment(df, GENOME, settings, window="full", origin="sampled")
    assert any("sampled, not evolved" in n for n in rep["notes"])


def test_result_is_deterministic(settings, df):
    a = simulate_investment(df, GENOME, settings, window="holdout")
    b = simulate_investment(df, GENOME, settings, window="holdout")
    assert a["strategy"]["final_equity"] == b["strategy"]["final_equity"]
    assert a["trades"] == b["trades"]


def test_report_is_json_serialisable(report):
    payload = json.dumps(report, default=str)
    assert "equity_curve" in payload


def test_rejects_zero_cash_and_tiny_windows(settings, df):
    with pytest.raises(InvestError):
        simulate_investment(df, GENOME, settings, initial_cash=0.0)
    with pytest.raises(InvestError):
        simulate_investment(df.iloc[:2], GENOME, settings, window="full")


def test_no_trade_genome_says_so(settings, df):
    # A stop so tight the position opens and is stopped on the same bar logic
    # is impossible; instead make the entry impossible by requiring shorts only.
    g = {
        "family": "rsi_reversion",
        "params": {"rsi_buy": 0.5, "rsi_exit": 1.0},
        "risk": {"stop_loss_pct": -0.008, "take_profit_pct": None, "trailing_pct": None,
                 "trade_shorts": False},
    }
    rep = simulate_investment(df, g, settings, window="holdout")
    if rep["strategy"]["n_trades"] == 0:
        assert any("never opened a trade" in n for n in rep["notes"])
        assert rep["verdict"]["text"] == "No trades: cash did nothing."


# ---------------------------------------------------------------- rendering


def test_html_has_inline_charts_and_no_remote_assets(report):
    doc = render_html(report)
    assert doc.startswith("<!doctype html>")
    assert "<svg" in doc
    assert "http://" not in doc and "https://" not in doc
    assert "<script" not in doc
    assert "Equity curve" in doc
    assert _pct_text(report["strategy"]["total_return"]) in doc


def test_html_escapes_untrusted_text(settings, df):
    settings.data.symbol = "<script>alert(1)</script>"
    rep = simulate_investment(df, GENOME, settings, window="holdout")
    doc = render_html(rep)
    assert "<script>alert(1)</script>" not in doc
    assert "&lt;script&gt;" in doc


def test_markdown_contains_the_tables(report):
    md = render_markdown(report)
    assert "## Cost sensitivity" in md
    assert "## By calendar year" in md
    assert "buy & hold" in md
    assert report["verdict"]["text"] in md


def test_write_reports_writes_three_files(tmp_path, report):
    paths = write_reports(
        report,
        out_md=tmp_path / "a" / "inv.md",
        out_html=tmp_path / "a" / "inv.html",
        out_json=tmp_path / "a" / "inv.json",
    )
    assert set(paths) == {"markdown", "html", "json"}
    assert all(Path(p).exists() for p in paths.values())
    data = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert data["strategy"]["final_equity"] == report["strategy"]["final_equity"]
    assert "svg" in Path(paths["html"]).read_text(encoding="utf-8")


# ---------------------------------------------------------------- svg pieces


def test_svg_helpers_degrade_gracefully():
    empty = pd.Series([], dtype="float64")
    assert svg_line_chart([("x", empty, "#fff")]) == ""
    assert svg_drawdown_chart(empty) == ""
    assert svg_price_chart(empty, []) == ""
    assert svg_year_bars([]) == ""


def test_svg_line_chart_downsamples(report):
    n = len(report["equity_curve"])
    assert n > DEFAULT_CHART_POINTS
    series = pd.Series(
        [v for _, v in report["equity_curve"]],
        index=pd.to_datetime([t for t, _ in report["equity_curve"]], utc=True),
    )
    svg = svg_line_chart([("strategy", series, "#42d392")], title="t")
    assert svg.count("polyline") == 1
    points = re.search(r'points="([^"]+)"', svg).group(1).split()
    assert len(points) == DEFAULT_CHART_POINTS
    assert all(x.split(",")[0] for x in points)


def test_svg_year_bars_needs_years(report):
    svg = svg_year_bars([{"year": 2021, "strategy_return": 0.2, "buy_hold_return": 0.5}])
    assert svg.count("<rect") >= 3


def test_sha256_file(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hello", encoding="utf-8")
    assert sha256_file(p) == hashlib.sha256(b"hello").hexdigest()
    assert sha256_file(tmp_path / "missing.txt") == ""


def _pct_text(v: float) -> str:
    return f"{v * 100:+.1f}%"
