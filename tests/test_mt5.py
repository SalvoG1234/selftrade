import io
import json
import os
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout

import numpy as np
import pytest

from selftrade import mt5 as mt5mod
from selftrade.config import load_settings
from selftrade.data import load_dataframe
from tests.fake_mt5 import FakeMT5, make_rates


@pytest.fixture(autouse=True)
def reset_override():
    yield
    mt5mod.set_module_override(None)


def settings_for(tmp_path, *, enabled=True, live=False, **overrides):
    s = load_settings("config.yaml")
    s.mt5.enabled = enabled
    s.mt5.live = live
    s.mt5.symbol = "EURUSD"
    s.mt5.suffix = ""
    s.mt5.timeframe = "1h"
    s.mt5.bars = 400
    s.data.min_rows = 50
    s.memory.db = str(tmp_path / "state" / "m.db")
    s.session_file = str(tmp_path / "state" / "session.json")
    for k, v in overrides.items():
        setattr(s.mt5, k, v)
    return s


# ---------------------------------------------------------------- data in


def test_load_bars_produces_valid_ohlcv(tmp_path):
    term = FakeMT5()
    df = mt5mod.load_bars(settings_for(tmp_path), module=term)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "UTC"
    assert (df.dtypes == "float64").all()
    assert (df["high"] >= df["low"]).all()
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()
    assert (df["volume"] > 0).all()


def test_bars_to_frame_maps_tick_volume():
    rates = make_rates(10)
    df = mt5mod.bars_to_frame(rates)
    assert df["volume"].iloc[0] == 1.0
    assert len(df) == 10


def test_load_bars_empty_raises(tmp_path):
    term = FakeMT5(bars=0)
    term.copy_rates_from_pos = lambda *a, **k: None
    with pytest.raises(mt5mod.MT5Error) as exc:
        mt5mod.load_bars(settings_for(tmp_path), module=term)
    assert "market may be closed" in str(exc.value)


def test_bars_to_frame_rejects_bad_structure():
    with pytest.raises(mt5mod.MT5Error):
        mt5mod.bars_to_frame(np.array([1, 2, 3]))


def test_data_source_mt5_uses_override(tmp_path):
    s = settings_for(tmp_path)
    s.data.source = "mt5"
    mt5mod.set_module_override(FakeMT5())
    df = load_dataframe(s)
    assert len(df) == 400


def test_data_source_mt5_without_package_reports_clearly(tmp_path):
    s = settings_for(tmp_path)
    s.data.source = "mt5"
    with pytest.raises(mt5mod.MT5Error) as exc:
        load_dataframe(s)
    assert "MetaTrader5" in str(exc.value)


# ---------------------------------------------------------------- symbols


def test_symbol_resolution_plain():
    from selftrade.config import load_settings

    term = mt5mod.Terminal(FakeMT5(), load_settings("config.yaml"))
    assert term.resolve_symbol("EURUSD") == "EURUSD"


def test_symbol_resolution_with_suffix():
    s = load_settings("config.yaml")
    s.mt5.suffix = ".pro"
    term = mt5mod.Terminal(FakeMT5(symbol="EURUSD.pro"), s)
    assert term.resolve_symbol("EURUSD") == "EURUSD.pro"


def test_symbol_resolution_fallback_lists_candidates():
    s = load_settings("config.yaml")
    term = mt5mod.Terminal(FakeMT5(symbol="EURUSD.raw"), s)
    with pytest.raises(mt5mod.MT5Error) as exc:
        term.resolve_symbol("EURUSD")
    assert "EURUSD.raw" in str(exc.value)


def test_listed_symbols():
    s = load_settings("config.yaml")
    s.mt5.enabled = True
    s.mt5.symbol = "EURUSD"
    syms = mt5mod.listed_symbols(s, module=FakeMT5())
    assert syms == ["EURUSD"]


# ---------------------------------------------------------------- costs


def test_spread_costs_bps(tmp_path):
    s = settings_for(tmp_path)
    costs = mt5mod.spread_costs(s, module=FakeMT5(bid=1.10000, ask=1.10020, point=0.00001))
    # spread 20 points * 0.00001 = 0.0002 price; /0.110010... mid *10000 = 1.818 bps
    assert costs["spread_points"] == pytest.approx(20.0)
    assert costs["spread_bps"] == pytest.approx(0.0002 / 1.1001 * 10000, rel=1e-6)
    assert costs["suggested_fee_bps"] == pytest.approx(costs["spread_bps"] / 2)
    assert costs["suggested_slippage_bps"] == pytest.approx(costs["spread_bps"] / 2)
    assert costs["round_trip_bps"] == pytest.approx(costs["spread_bps"])


def test_spread_costs_round_trip_is_one_spread(tmp_path):
    s = settings_for(tmp_path)
    c = mt5mod.spread_costs(s, module=FakeMT5())
    assert c["suggested_fee_bps"] + c["suggested_slippage_bps"] == pytest.approx(c["spread_bps"])


def test_spread_costs_no_quote(tmp_path):
    term = FakeMT5()
    term.symbol_info_tick = lambda name: None
    with pytest.raises(mt5mod.MT5Error):
        mt5mod.spread_costs(settings_for(tmp_path), module=term)


# ---------------------------------------------------------------- gating


def test_dry_run_never_calls_order_send(tmp_path):
    term = FakeMT5()
    s = settings_for(tmp_path, live=False)
    out = mt5mod.place_order(s, symbol="EURUSD", side="buy", sl_points=500, module=term)
    assert term.sent == []
    assert out["sent"] is False and out["dry_run"] is True
    req = out["request"]
    for key in ("action", "symbol", "volume", "type", "price", "sl", "tp",
                "deviation", "magic", "comment", "type_time", "type_filling"):
        assert key in req


def test_live_flag_alone_does_not_arm(tmp_path):
    term = FakeMT5()
    s = settings_for(tmp_path, live=False)
    out = mt5mod.place_order(s, symbol="EURUSD", side="buy", sl_points=500, live_flag=True, module=term)
    assert term.sent == []
    assert out["sent"] is False
    assert any("not armed" in r for r in out["reasons"])


def test_config_armed_without_flag_does_not_send(tmp_path):
    term = FakeMT5()
    s = settings_for(tmp_path, live=True)
    out = mt5mod.place_order(s, symbol="EURUSD", side="buy", sl_points=500, live_flag=False, module=term)
    assert term.sent == []
    assert any("--live" in r for r in out["reasons"])


def test_both_keys_send_exactly_one_order(tmp_path):
    term = FakeMT5()
    s = settings_for(tmp_path, live=True)
    out = mt5mod.place_order(s, symbol="EURUSD", side="buy", sl_points=500, live_flag=True, module=term)
    assert len(term.sent) == 1
    assert out["sent"] is True
    assert out["result"]["retcode"] == 10009
    req = term.sent[0]
    assert req["symbol"] == "EURUSD"
    assert req["type"] == term.ORDER_TYPE_BUY
    assert req["sl"] < req["price"]  # buy stop below entry


def test_real_account_is_refused_even_when_armed(tmp_path):
    term = FakeMT5(trade_mode=2)  # REAL
    s = settings_for(tmp_path, live=True)
    with pytest.raises(mt5mod.OrderRejected) as exc:
        mt5mod.place_order(s, symbol="EURUSD", side="buy", sl_points=500, live_flag=True, module=term)
    assert term.sent == []
    assert any("DEMO" in r for r in exc.value.reasons)


def test_require_demo_false_allows_real(tmp_path):
    term = FakeMT5(trade_mode=2)
    s = settings_for(tmp_path, live=True, require_demo=False)
    out = mt5mod.place_order(s, symbol="EURUSD", side="buy", sl_points=500, live_flag=True, module=term)
    assert out["sent"] is True


def test_order_rejected_without_stop(tmp_path):
    term = FakeMT5()
    s = settings_for(tmp_path, live=True, require_stop=True)
    with pytest.raises(mt5mod.OrderRejected) as exc:
        mt5mod.place_order(s, symbol="EURUSD", side="buy", sl_points=0, live_flag=True, module=term)
    assert any("stop" in r for r in exc.value.reasons)
    assert term.sent == []


def test_order_rejected_symbol_not_allowed(tmp_path):
    term = FakeMT5()
    s = settings_for(tmp_path, live=True, allowed_symbols=["GBPUSD"])
    with pytest.raises(mt5mod.OrderRejected) as exc:
        mt5mod.place_order(s, symbol="EURUSD", side="buy", sl_points=500, live_flag=True, module=term)
    assert any("allowed_symbols" in r for r in exc.value.reasons)


def test_order_rejected_max_lots(tmp_path):
    term = FakeMT5()
    s = settings_for(tmp_path, live=True, max_lots=0.01)
    with pytest.raises(mt5mod.OrderRejected) as exc:
        mt5mod.place_order(s, symbol="EURUSD", side="buy", volume=1.0, sl_points=500, live_flag=True, module=term)
    assert any("max_lots" in r for r in exc.value.reasons)


def test_order_rejected_max_positions(tmp_path):
    from types import SimpleNamespace

    term = FakeMT5(positions=[SimpleNamespace(ticket=1, symbol="EURUSD", volume=0.1)])
    s = settings_for(tmp_path, live=True, max_positions=1)
    with pytest.raises(mt5mod.OrderRejected) as exc:
        mt5mod.place_order(s, symbol="EURUSD", side="buy", volume=0.1, sl_points=500, live_flag=True, module=term)
    assert any("max_positions" in r for r in exc.value.reasons)


def test_order_rejected_wide_spread(tmp_path):
    term = FakeMT5(bid=1.1000, ask=1.1050)  # 500 points
    s = settings_for(tmp_path, live=True, max_spread_points=25)
    with pytest.raises(mt5mod.OrderRejected) as exc:
        mt5mod.place_order(s, symbol="EURUSD", side="buy", volume=0.1, sl_points=500, live_flag=True, module=term)
    assert any("spread" in r for r in exc.value.reasons)


def test_order_rejected_market_closed(tmp_path):
    term = FakeMT5(trade_mode_symbol=2)  # CLOSEONLY
    s = settings_for(tmp_path, live=True)
    with pytest.raises(mt5mod.OrderRejected) as exc:
        mt5mod.place_order(s, symbol="EURUSD", side="buy", volume=0.1, sl_points=500, live_flag=True, module=term)
    assert any("trade_mode" in r for r in exc.value.reasons)


def test_order_rejected_when_disabled(tmp_path):
    term = FakeMT5()
    s = settings_for(tmp_path, enabled=False, live=True)
    with pytest.raises(mt5mod.MT5Error):
        mt5mod.place_order(s, symbol="EURUSD", module=term)


# ---------------------------------------------------------------- sizing


def test_sizing_by_risk(tmp_path):
    s = settings_for(tmp_path, live=True, risk_pct=0.01, max_lots=10.0)
    term = mt5mod.Terminal(FakeMT5(tick_value=1.0, tick_size=0.00001, point=0.00001), s)
    lots, notes = mt5mod.compute_lots(s, term, "EURUSD", sl_points=500, balance=10000.0)
    # risk 100 USD, loss per lot = 1.0 * (1) * 500 = 500 -> 0.2 lots
    assert lots == pytest.approx(0.2)
    assert any("risk_pct" in n for n in notes)


def test_sizing_fallback_fixed_lots(tmp_path):
    s = settings_for(tmp_path, live=True, fixed_lots=0.03)
    term = mt5mod.Terminal(FakeMT5(tick_value=0.0, tick_size=0.0), s)
    lots, notes = mt5mod.compute_lots(s, term, "EURUSD", sl_points=500, balance=10000.0)
    assert lots == pytest.approx(0.03)
    assert any("fixed_lots" in n for n in notes)


def test_volume_snapped_to_step(tmp_path):
    term = FakeMT5(volume_step=0.1, volume_min=0.1)
    s = settings_for(tmp_path, live=True)
    plan = mt5mod.build_order(
        s, mt5mod.Terminal(term, s), symbol="EURUSD", side="buy", volume=0.13, sl_points=500
    )
    assert plan.request["volume"] == pytest.approx(0.1)


# ---------------------------------------------------------------- secrets


def test_password_never_in_output(tmp_path, monkeypatch):
    password = "SuperSecret123!"
    monkeypatch.setenv("SELFTD_MT5_PASSWORD", password)
    seen = []
    term = FakeMT5(password_seen=seen)
    s = settings_for(tmp_path, live=True)

    # Run through place_order with the fake module directly:
    mt5mod.place_order(s, symbol="EURUSD", side="buy", volume=0.1, sl_points=500, live_flag=True, module=term)
    assert seen, "initialize() should have received the password from the env"
    assert seen[0] == password

    # CLI output (doctor) must not contain the password.
    buf_out = io.StringIO()
    with redirect_stdout(buf_out):
        lines = mt5mod.doctor_lines(s, module=term)
    text = "\n".join(lines) + buf_out.getvalue()
    assert password not in text
    assert "SELFTD_MT5_PASSWORD" in text


def test_cli_dry_run_output_has_no_password(tmp_path, monkeypatch):
    password = "AnotherSecret456!"
    monkeypatch.setenv("SELFTD_MT5_PASSWORD", password)
    s = settings_for(tmp_path, live=False)
    term = FakeMT5()
    mt5mod.set_module_override(term)
    args = Namespace(
        action="order", symbol="EURUSD", side="buy", volume=0.1, sl_points=500,
        tp_points=None, ticket=None, out=None, live=False, json=False,
    )
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        code = mt5mod.cli(s, args)
    assert code == 0
    assert password not in buf.getvalue()
    assert password not in err.getvalue()
    assert term.sent == []
    assert "DRY RUN" in buf.getvalue()


def test_cli_rejection_exit_code_nonzero(tmp_path):
    s = settings_for(tmp_path, live=True)
    term = FakeMT5(trade_mode=2)  # real
    mt5mod.set_module_override(term)
    args = Namespace(
        action="order", symbol="EURUSD", side="buy", volume=0.1, sl_points=500,
        tp_points=None, ticket=None, out=None, live=True, json=False,
    )
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        code = mt5mod.cli(s, args)
    assert code == 1
    assert term.sent == []
    assert "REJECTED" in err.getvalue()


def test_cli_costs_prints_suggestion(tmp_path):
    s = settings_for(tmp_path)
    mt5mod.set_module_override(FakeMT5())
    args = Namespace(
        action="costs", symbol=None, side="buy", volume=None, sl_points=None,
        tp_points=None, ticket=None, out=None, live=False, json=False,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = mt5mod.cli(s, args)
    assert code == 0
    text = buf.getvalue()
    assert "spread" in text and "suggested" in text


def test_cli_bars_writes_snapshot(tmp_path):
    s = settings_for(tmp_path)
    mt5mod.set_module_override(FakeMT5())
    out = tmp_path / "snap.csv"
    args = Namespace(
        action="bars", symbol=None, side="buy", volume=None, sl_points=None,
        tp_points=None, ticket=None, out=str(out), live=False, json=False,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = mt5mod.cli(s, args)
    assert code == 0
    assert out.exists()


# ---------------------------------------------------------------- doctor


def test_doctor_disabled_when_not_enabled(tmp_path):
    s = settings_for(tmp_path, enabled=False)
    lines = mt5mod.doctor_lines(s, module=FakeMT5())
    assert "disabled" in lines[0]


def test_doctor_connected_with_fake(tmp_path):
    s = settings_for(tmp_path)
    lines = mt5mod.doctor_lines(s, module=FakeMT5())
    joined = "\n".join(lines)
    assert "connected" in joined
    assert "DEMO" in joined
    assert "password source" in joined


def test_doctor_reports_connect_failure(tmp_path):
    s = settings_for(tmp_path)
    lines = mt5mod.doctor_lines(s, module=FakeMT5(init_ok=False))
    assert "cannot connect" in lines[0]


def test_account_info_shape(tmp_path):
    info = mt5mod.account_info(settings_for(tmp_path), module=FakeMT5())
    assert info["login"] == 1234567
    assert info["is_demo"] is True


def test_positions_list(tmp_path):
    from types import SimpleNamespace

    term = FakeMT5(positions=[SimpleNamespace(ticket=7, symbol="EURUSD", volume=0.1, type=0)])
    pos = mt5mod.open_positions(settings_for(tmp_path), module=term)
    assert pos[0]["ticket"] == 7
