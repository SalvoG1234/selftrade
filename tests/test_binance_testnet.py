"""Tests for the Binance Spot Testnet adapter.

No test here touches the network: the transport is injected and records what it
was asked to send.  Four properties are pinned, and they are the ones that would
cost real money if they broke:

* **nothing is sent while unarmed** - the request is signed and recorded as a dry
  run, and the exchange is never asked to open a position;
* **the signature is the documented one** - HMAC-SHA-256, hex, over the exact
  parameter string that is sent, with ``timestamp`` in milliseconds;
* **the testnet is the default host** - a production URL is refused at construction
  unless ``binance.allow_production`` names a funded account on purpose
  and by the config validator;
* **the secrets stay put** - the API key and secret never appear in a recorded
  call, and the signature is redacted.

Plus the mappings that a real client lives or dies by: exchange filters (LOT_SIZE,
NOTIONAL, PRICE_FILTER), error code handling, rate-limit guards, idempotency and
reconciliation against the exchange's own answer.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import pytest

from selftrade.binance import (
    DEFAULT_BASE_URL,
    BinanceError,
    BinanceTestnetBroker,
    BrokerError,
    HostRefusedError,
    HttpResponse,
    SymbolRules,
    key_health,
)
from selftrade.config import ConfigError, load_settings
from selftrade.execution import OrderRequest, OrderStatus, OrderType, Side
from selftrade.memory import Memory

KEY = "key-abc123"
SECRET = "secret-do-not-leak"


class FakeTransport:
    """A transport that answers from a script and records every request."""

    def __init__(self, routes: dict[str, HttpResponse] | None = None):
        self.routes = dict(routes or {})
        self.calls: list[dict] = []

    def __call__(self, method, url, params, body, headers, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params or {}),
                "body": body,
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )
        key = f"{method} {url.split('/api')[-1]}"
        for pattern, response in self.routes.items():
            if pattern in key:
                if callable(response):
                    return response(self.calls[-1])
                return response
        return HttpResponse(status=404, body={"code": -1121, "msg": "Invalid symbol."})


def account_balance(balances: dict[str, tuple[float, float]]) -> HttpResponse:
    return HttpResponse(
        status=200,
        body={
            "canTrade": True,
            "accountType": "SPOT",
            "balances": [
                {"asset": a, "free": f, "locked": l} for a, (f, l) in balances.items()
            ],
        },
    )


def symbol_payload(
    symbol="BTCUSDT",
    *,
    min_qty="0.00001",
    step="0.00001",
    tick="0.01",
    notional="5",
    min_price="0.01",
    max_price="1000000",
    order_types=(
        "LIMIT",
        "LIMIT_MAKER",
        "MARKET",
        "STOP_LOSS",
        "STOP_LOSS_LIMIT",
        "TAKE_PROFIT",
        "TAKE_PROFIT_LIMIT",
    ),
    max_orders=200,
    max_algo_orders=5,
    ask_down="0.8",
    ask_up="2",
):
    """The filters the **real** testnet reports for ``BTCUSDT``, trimmed to what matters."""
    return {
        "symbols": [
            {
                "symbol": symbol,
                "status": "TRADING",
                "baseAsset": symbol.replace("USDT", ""),
                "quoteAsset": "USDT",
                "orderTypes": list(order_types),
                "filters": [
                    {"filterType": "PRICE_FILTER", "minPrice": min_price, "maxPrice": max_price,
                     "tickSize": tick},
                    {"filterType": "LOT_SIZE", "minQty": min_qty, "stepSize": step},
                    {"filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierUp": "1.2",
                     "bidMultiplierDown": "0.5", "askMultiplierUp": ask_up,
                     "askMultiplierDown": ask_down, "avgPriceMins": 5},
                    {"filterType": "NOTIONAL", "minNotional": notional},
                    {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": max_orders},
                    {"filterType": "MAX_NUM_ALGO_ORDERS", "maxNumAlgoOrders": max_algo_orders},
                ],
            }
        ],
        "rateLimits": [
            {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 6000}
        ],
    }


def account_payload(
    *,
    can_trade=True,
    can_withdraw=False,
    can_deposit=True,
    permissions=None,
    account_type="SPOT",
    balances=({"asset": "USDT", "free": "10000", "locked": "0"},),
) -> HttpResponse:
    """A ``/v3/account`` answer with the permission flags a check reads."""
    body = {
        "accountType": account_type,
        "canTrade": can_trade,
        "canWithdraw": can_withdraw,
        "canDeposit": can_deposit,
        "balances": [dict(row) for row in balances],
    }
    if permissions is not None:
        body["permissions"] = list(permissions)
    return HttpResponse(200, body)


def check_routes(**over) -> dict[str, HttpResponse]:
    """Everything a read-only account check needs, and nothing else."""
    routes = {
        "GET /v3/time": HttpResponse(200, {"serverTime": 1_700_000_000_000}),
        "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
        "GET /v3/klines": klines_response([100.0]),
        "GET /v3/account": account_payload(permissions=("SPOT",)),
        "GET /v3/openOrders": HttpResponse(200, []),
    }
    routes.update(over)
    return routes


def cli_check_routes(**over) -> dict[str, HttpResponse]:
    """``check_routes`` with a server time the CLI's **real** clock agrees with.

    The adapter is built with ``now=time.time`` outside the tests, so a scripted
    timestamp from 2023 would make the clock check fail for the wrong reason.
    """
    routes = check_routes(
        **{"GET /v3/time": HttpResponse(200, {"serverTime": int(time.time() * 1000)})}
    )
    routes.update(over)
    return routes


def post_fields(call: dict) -> dict:
    """A recorded POST goes out as an urlencoded body, not as query parameters."""
    return {k: v[0] for k, v in parse_qs(call.get("body") or "").items()}


def filled_entry(**over) -> dict:
    """The ``RESULT`` answer to an armed MARKET BUY that filled: 1 BTC for 101 USDT."""
    row = {
        "symbol": "BTCUSDT",
        "orderId": 5001,
        "clientOrderId": "entry-1",
        "price": "0.00000000",
        "origQty": "1.00000000",
        "executedQty": "1.00000000",
        "cummulativeQuoteQty": "101.00000000",
        "status": "FILLED",
        "timeInForce": "GTC",
        "type": "MARKET",
        "side": "BUY",
        "transactTime": 1_700_000_000_000,
    }
    row.update(over)
    return row


def resting_stop(call: dict) -> HttpResponse:
    """What Binance answers for a resting stop: it echoes the order it accepted."""
    fields = post_fields(call)
    return HttpResponse(
        200,
        {
            "symbol": fields.get("symbol", "BTCUSDT"),
            "orderId": 5002,
            "clientOrderId": fields.get("newClientOrderId", ""),
            "price": fields.get("price", "0"),
            "origQty": fields.get("quantity", "0"),
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "status": "NEW",
            "timeInForce": fields.get("timeInForce", "GTC"),
            "type": fields.get("type", "STOP_LOSS_LIMIT"),
            "side": fields.get("side", "SELL"),
            "transactTime": 1_700_000_000_000,
        },
    )


def exchange_with_protection(*seed_rows):
    """A fake exchange that **remembers** what it accepted, like the real one does.

    ``seed_rows`` are orders that already exist (an entry placed by hand, say).  A
    stateless route cannot model the case this matters for: a stop placed by one
    process and looked up by the next, which is exactly what ``protect`` followed by
    ``unprotect`` in two terminals is.
    """
    state: dict = {"orders": {str(row["clientOrderId"]): dict(row) for row in seed_rows}}

    def on_post(call):
        fields = post_fields(call)
        client_id = str(fields.get("newClientOrderId", ""))
        row = (
            resting_stop(call).body
            if str(fields.get("type", "")).upper().startswith(("STOP", "TAKE"))
            else filled_entry(clientOrderId=client_id, orderId=7001)
        )
        state["orders"][client_id] = row
        return HttpResponse(200, row)

    def on_get(call):
        wanted = str((call.get("params") or {}).get("origClientOrderId"))
        row = state["orders"].get(wanted)
        if row is None:
            return HttpResponse(404, {"code": -2013, "msg": "Order does not exist."})
        return HttpResponse(200, row)

    def on_delete(call):
        wanted = str(post_fields(call).get("orderId"))
        for client_id, row in state["orders"].items():
            if str(row.get("orderId")) == wanted:
                row = {**row, "status": "CANCELED"}
                state["orders"][client_id] = row
                return HttpResponse(200, row)
        return HttpResponse(404, {"code": -2013, "msg": "Order does not exist."})

    routes = protection_routes()
    routes.update(
        {"GET /v3/order": on_get, "POST /v3/order": on_post, "DELETE /v3/order": on_delete}
    )
    return routes, state


def protection_routes(*, free="0.999", stop_response=None, payload=None, **over):
    """An armed entry that fills, plus everything the stop placement then reads."""

    def on_post(call):
        fields = post_fields(call)
        if str(fields.get("type", "")).upper().startswith(("STOP", "TAKE")):
            if stop_response is not None:
                return stop_response
            return resting_stop(call)
        return HttpResponse(200, filled_entry(clientOrderId=fields.get("newClientOrderId", "entry-1")))

    routes = {
        "GET /v3/time": HttpResponse(200, {"serverTime": 1_700_000_000_000}),
        "GET /v3/exchangeInfo": HttpResponse(200, payload or symbol_payload()),
        "GET /v3/klines": klines_response([100.0]),
        "GET /v3/account": account_payload(
            permissions=("SPOT",),
            balances=(
                {"asset": "USDT", "free": "10000", "locked": "0"},
                {"asset": "BTC", "free": free, "locked": "0"},
            ),
        ),
        "GET /v3/openOrders": HttpResponse(200, []),
        "GET /v3/order": HttpResponse(404, {"code": -2013, "msg": "Order does not exist."}),
        "POST /v3/order": on_post,
    }
    routes.update(over)
    return routes


def by_client_id(*rows):
    """The exchange filters ``origClientOrderId`` itself, so the fake transport must too.

    A route that echoed one fixed row would make every lookup look like a hit - which
    is how a second stop gets stacked on the same position.
    """
    table = {str(row.get("clientOrderId")): row for row in rows}

    def on_get(call):
        wanted = (call.get("params") or {}).get("origClientOrderId")
        row = table.get(str(wanted))
        if row is None:
            return HttpResponse(404, {"code": -2013, "msg": "Order does not exist."})
        return HttpResponse(200, row)

    return on_get


def klines_response(closes=(100.0, 101.0, 102.0)) -> HttpResponse:
    rows = []
    for i, close in enumerate(closes):
        rows.append([1_700_000_000_000 + i * 3_600_000, close, close, close, close, 1.0,
                     close, close, 1, close, close, "0"])
    return HttpResponse(status=200, body=rows)


def make_broker(tmp_path, *, routes=None, live=False, enabled=True, risk_gate=None,
                kill_switch=None, base_url=None, api_key=KEY, api_secret=SECRET, **cfg_over):
    s = load_settings(None)
    for key, value in {"enabled": enabled, "live": live, **cfg_over}.items():
        setattr(s.binance, key, value)
    transport = FakeTransport(routes)
    mem = Memory(tmp_path / "m.db")
    broker = BinanceTestnetBroker(
        config=s.binance,
        symbols=["BTC/USDT"],
        transport=transport,
        memory=mem,
        live=live,
        api_key=api_key,
        api_secret=api_secret,
        now=lambda: 1_700_000_000.0,
        risk_gate=risk_gate,
        kill_switch=kill_switch,
        base_url=base_url,
    )
    return broker, transport, mem


def _send_routes(**extra):
    """The routes an armed order needs, including the account the gate reads."""
    routes = {
        "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
        "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
        "GET /v3/klines": klines_response([100.0]),
        "GET /v3/account": account_balance({"USDT": (10_000.0, 0.0)}),
        "GET /v3/order": HttpResponse(404, {"code": -2013, "msg": "Order does not exist."}),
        "POST /v3/order": HttpResponse(
            200,
            {
                "orderId": 55,
                "clientOrderId": "gate-1",
                "symbol": "BTCUSDT",
                "price": "0.00000000",
                "origQty": "1.00000000",
                "executedQty": "1.00000000",
                "cummulativeQuoteQty": "100.00000000",
                "status": "FILLED",
                "type": "MARKET",
                "side": "BUY",
            },
        ),
    }
    routes.update(extra)
    return routes


def _posts(transport) -> list[dict]:
    return [c for c in transport.calls if c["method"] == "POST" and "/v3/order" in c["url"]]


# --------------------------------------------------------------------------
# guard rails: testnet only, two keys, no secrets
# --------------------------------------------------------------------------

def test_base_url_must_be_the_testnet():
    for bad in ("https://api.binance.com/api", "https://api1.binance.com/api"):
        with pytest.raises(HostRefusedError):
            BinanceTestnetBroker(
                config=load_settings(None).binance, symbols=["BTC/USDT"], base_url=bad
            )
    with pytest.raises(HostRefusedError):
        BinanceTestnetBroker(
            config=load_settings(None).binance, symbols=["BTC/USDT"], base_url="https://example.com/api"
        )


def test_config_refuses_a_production_host():
    import tempfile

    text = "binance:\n  enabled: true\n  base_url: https://api.binance.com/api\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError) as exc:
            load_settings(path)
    assert "testnet" in str(exc.value)


def test_arming_needs_both_keys(tmp_path):
    s = load_settings(None)
    s.binance.enabled = False
    s.binance.live = True
    with pytest.raises(Exception) as exc:
        BinanceTestnetBroker(
            config=s.binance, symbols=["BTC/USDT"], transport=FakeTransport(), live=True,
            api_key=KEY, api_secret=SECRET,
        )
    assert "binance.enabled" in str(exc.value)

    s.binance.enabled = True
    with pytest.raises(Exception) as exc:
        BinanceTestnetBroker(
            config=s.binance, symbols=["BTC/USDT"], transport=FakeTransport(), live=True,
            api_key="", api_secret="",
        )
    assert "credentials" in str(exc.value)


def test_it_implements_the_whole_broker_adapter_contract(tmp_path):
    """A stand-in for the exchange has to satisfy the same interface as the simulator."""
    from selftrade.execution import BrokerAdapter

    broker, _, _ = make_broker(tmp_path)
    assert isinstance(broker, BrokerAdapter)
    # the subclass resolves every abstract method (the ABC keeps its own set)
    assert BinanceTestnetBroker.__abstractmethods__ == frozenset()
    assert BrokerAdapter.__abstractmethods__ >= {
        "get_account_balance",
        "get_positions",
        "get_market_data",
        "submit_order",
        "cancel_order",
        "get_order_status",
        "get_trade_history",
    }
    for name in (
        "get_account_balance",
        "get_positions",
        "get_market_data",
        "submit_order",
        "cancel_order",
        "get_order_status",
        "get_trade_history",
    ):
        assert callable(getattr(broker, name))
    assert callable(broker.open_orders) and callable(broker.reconcile)
    assert broker.name == "binance-testnet"


def test_mode_is_never_live(tmp_path):
    broker, _, _ = make_broker(tmp_path)
    assert broker.mode == "paper"
    assert broker.describe()["host_is_testnet"] is True
    assert "dry run" in broker.describe()["mode_note"]

    broker, _, _ = make_broker(tmp_path, live=True)
    assert broker.mode == "testnet"
    assert "orders are sent" in broker.describe()["mode_note"]


def test_secrets_never_appear_in_recorded_calls(tmp_path):
    broker, transport, mem = make_broker(
        tmp_path, routes={"GET /v3/time": HttpResponse(200, {"serverTime": 1})}
    )
    broker.sync_time()
    rows = mem.api_calls()
    blob = json.dumps(rows, default=str)
    assert SECRET not in blob
    assert KEY not in blob
    assert all(c["params"].get("signature") is None for c in broker.recorded_calls())


# --------------------------------------------------------------------------
# the documented wiring
# --------------------------------------------------------------------------

def test_base_url_and_paths_match_the_documentation():
    assert DEFAULT_BASE_URL == "https://testnet.binance.vision/api"
    assert "/sapi" not in DEFAULT_BASE_URL


def test_signature_is_the_documented_hmac_of_the_sent_string(tmp_path):
    broker, transport, _ = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1_700_000_000_500}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
            "GET /v3/account": account_balance({"USDT": (100.0, 0.0)}),
            "GET /v3/klines": klines_response([100.0]),
        },
    )
    broker.sync_time()
    broker.get_account_balance()
    call = [c for c in transport.calls if c["url"].endswith("/api/v3/account")][-1]

    sent = dict(call["params"])
    signature = sent.pop("signature")
    payload = urlencode(sent)
    expected = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    assert len(signature) == 64 and signature == signature.lower()
    assert call["headers"]["X-MBX-APIKEY"] == KEY
    assert sent["recvWindow"] == 5000  # documented default
    # millisecond timestamp, corrected by the server clock offset (500 ms here)
    assert sent["timestamp"] == 1_700_000_000_500


def test_server_clock_offset_is_applied_and_resynced_on_1021(tmp_path):
    state = {"time_calls": 0, "account_calls": 0}

    def time_first(call):
        state["time_calls"] += 1
        return HttpResponse(200, {"serverTime": 2_000_000_000_000 + state["time_calls"] * 1000})

    def account(call):
        state["account_calls"] += 1
        if state["account_calls"] == 1:
            return HttpResponse(
                400,
                {
                    "code": -1021,
                    "msg": "Timestamp for this request is outside of the recvWindow.",
                },
            )
        # the retry must carry the freshly synced server clock
        assert call["params"]["timestamp"] == 2_000_000_002_000
        return account_balance({"USDT": (10.0, 0.0)})

    broker, transport, _ = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": time_first,
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
            "GET /v3/account": account,
            "GET /v3/klines": klines_response([100.0]),
        },
    )
    broker.sync_time()
    snapshot = broker.get_account_balance()
    assert snapshot.cash == 10.0
    # one rejected call, one resync, exactly one retry - never a loop
    paths = [c["url"].split("/api")[-1] for c in transport.calls]
    assert paths.count("/v3/account") == 2
    assert paths.count("/v3/time") == 2
    rejected = [c for c in broker.recorded_calls() if c["code"] == -1021]
    assert len(rejected) == 1 and rejected[0]["ok"] is False


# --------------------------------------------------------------------------
# market rules and error handling
# --------------------------------------------------------------------------

def test_exchange_filters_are_read_and_respected(tmp_path):
    broker, _, mem = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
            "GET /v3/klines": klines_response([100.0]),
        },
    )
    rules = broker.rules_for("BTC/USDT")
    assert isinstance(rules, SymbolRules)
    assert (rules.base_asset, rules.quote_asset) == ("BTC", "USDT")
    assert float(rules.step_size) == pytest.approx(0.00001)
    assert float(rules.min_notional) == pytest.approx(5.0)
    assert json.loads(json.dumps(rules.as_dict()))["step_size"] is not None
    # a second call is served by the cache, not by the API
    broker.rules_for("BTC/USDT")
    assert len([c for c in mem.api_calls() if c["path"] == "/v3/exchangeInfo"]) == 1


def test_quantity_is_rounded_down_to_the_step_and_dust_is_refused(tmp_path):
    broker, _, _ = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload(min_qty="0.001", step="0.001")),
            "GET /v3/klines": klines_response([100.0]),
        },
    )
    below = broker.submit_order(OrderRequest("c1", "BTC/USDT", Side.BUY, 0.0005))
    assert below.status is OrderStatus.REJECTED
    assert "at least 0.001" in below.reject_reason

    tiny = broker.submit_order(OrderRequest("c2", "BTC/USDT", Side.BUY, 0.001))
    assert tiny.status is OrderStatus.REJECTED
    assert "notional" in tiny.reject_reason  # 0.001 * 100 = 0.1 < 5


def test_symbol_status_and_order_type_are_checked(tmp_path):
    payload = symbol_payload()
    payload["symbols"][0]["status"] = "BREAK"
    broker, _, _ = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, payload),
            "GET /v3/klines": klines_response([100.0]),
        },
    )
    order = broker.submit_order(OrderRequest("c1", "BTC/USDT", Side.BUY, 1.0))
    assert "not trading" in order.reject_reason


def test_api_errors_are_raised_parsed_and_recorded(tmp_path):
    broker, _, mem = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/account": HttpResponse(
                400, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}
            ),
        },
    )
    with pytest.raises(BinanceError) as exc:
        broker.get_account_balance()
    assert exc.value.code == -2015
    assert exc.value.status == 400
    row = mem.api_calls()[0]
    assert (row["ok"], row["code"], row["status"]) == (False, "-2015", 400)
    assert "Invalid API-key" in row["msg"]


def test_order_rejection_comes_back_as_a_rejected_order(tmp_path):
    broker, _, _ = make_broker(
        tmp_path,
        live=True,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
            "GET /v3/klines": klines_response([100.0]),
            "GET /v3/order": HttpResponse(404, {"code": -2013, "msg": "Order does not exist."}),
            "POST /v3/order": HttpResponse(
                400,
                {"code": -2010, "msg": "Account has insufficient balance for requested action."},
            ),
        },
    )
    order = broker.submit_order(OrderRequest("c1", "BTC/USDT", Side.BUY, 1.0))
    assert order.status is OrderStatus.REJECTED
    assert "insufficient balance" in order.reject_reason
    assert order.reject_reason.startswith("Binance refused it:")


# --------------------------------------------------------------------------
# dry run: the whole point
# --------------------------------------------------------------------------

def test_dry_run_signs_and_records_but_sends_nothing(tmp_path):
    broker, transport, mem = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
            "GET /v3/klines": klines_response([100.0]),
            "GET /v3/order": HttpResponse(404, {"code": -2013, "msg": "Order does not exist."}),
        },
    )
    order = broker.submit_order(
        OrderRequest("dry-1", "BTC/USDT", Side.BUY, 1.0, note="dry run please")
    )
    assert order.status is OrderStatus.DRY_RUN
    assert order.filled_qty == 0.0
    assert order.reject_reason == ""
    assert not any(
        c["method"] == "POST" and c["url"].endswith("/order") for c in transport.calls
    ), "an unarmed adapter must not POST an order"
    recorded = [c for c in mem.api_calls() if c["dry_run"]]
    assert len(recorded) == 1
    assert recorded[0]["path"] == "/v3/order"
    assert recorded[0]["status"] is None
    assert recorded[0]["params"]["newClientOrderId"] == "dry-1"
    assert recorded[0]["params"]["signature"] == "<hmac-sha256>"


def test_armed_adapter_sends_once_and_maps_the_answer(tmp_path):
    sent: list[dict] = []

    def on_post(call):
        sent.append(call)
        return HttpResponse(
            200,
            {
                "symbol": "BTCUSDT",
                "orderId": 987654,
                "clientOrderId": "client-7",
                "transactTime": 1_700_000_000_000,
                "price": "0.00000000",
                "origQty": "1.00000000",
                "executedQty": "1.00000000",
                "cummulativeQuoteQty": "101.00000000",
                "status": "FILLED",
                "timeInForce": "GTC",
                "type": "MARKET",
                "side": "BUY",
            },
        )

    broker, transport, _ = make_broker(
        tmp_path,
        live=True,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
            "GET /v3/klines": klines_response([100.0]),
            "GET /v3/order": HttpResponse(404, {"code": -2013, "msg": "Order does not exist."}),
            "POST /v3/order": on_post,
        },
    )
    order = broker.submit_order(OrderRequest("client-7", "BTC/USDT", Side.BUY, 1.0, note="entry"))
    assert order.status is OrderStatus.FILLED
    assert order.id == "987654"
    assert order.avg_price == pytest.approx(101.0)  # cummulativeQuoteQty / executedQty
    assert order.note == "entry"
    assert len(sent) == 1
    assert sent[0]["body"] is not None  # POST parameters go in the signed body
    assert "signature=" in sent[0]["body"]
    assert broker.describe()["armed"] is True


def test_a_symbol_without_the_conditional_type_cannot_be_protected(tmp_path):
    """No ``STOP_LOSS_LIMIT`` on the symbol: the entry is refused, nothing is sent."""
    broker, transport, mem = make_broker(
        tmp_path,
        live=True,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(
                200, symbol_payload(order_types=("LIMIT", "MARKET"))
            ),
            "GET /v3/klines": klines_response([100.0]),
        },
    )
    order = broker.submit_order(
        OrderRequest("c1", "BTC/USDT", Side.BUY, 1.0, stop_loss_pct=-0.02)
    )
    assert order.status is OrderStatus.REJECTED
    assert "does not offer STOP_LOSS_LIMIT" in order.reject_reason
    assert _posts(transport) == []
    assert [e["kind"] for e in mem.risk_events()] == ["order_blocked"]


def test_a_plan_this_rail_cannot_honour_is_refused_before_anything_is_sent(tmp_path):
    """A positive stop, a short, a take profit and a trailing stop are all refused."""
    broker, transport, _ = make_broker(tmp_path, live=True, routes=protection_routes())

    positive = broker.submit_order(
        OrderRequest("plan-1", "BTC/USDT", Side.BUY, 1.0, stop_loss_pct=0.02)
    )
    short = broker.submit_order(
        OrderRequest("plan-2", "BTC/USDT", Side.SELL, 1.0, stop_loss_pct=-0.02)
    )
    target = broker.submit_order(
        OrderRequest("plan-3", "BTC/USDT", Side.BUY, 1.0, take_profit_pct=0.05)
    )
    trail = broker.submit_order(
        OrderRequest("plan-4", "BTC/USDT", Side.BUY, 1.0, trailing_pct=0.01)
    )

    assert positive.status is short.status is target.status is trail.status is OrderStatus.REJECTED
    assert "has to be negative" in positive.reject_reason
    assert "only a BUY can be protected" in short.reject_reason
    assert "OCO" in target.reject_reason
    assert "OCO" in trail.reject_reason
    assert _posts(transport) == []  # none of them reached the exchange


def test_retrying_a_client_id_finds_the_order_instead_of_sending_twice(tmp_path):
    posts: list[dict] = []

    def on_get(call):
        return HttpResponse(
            200,
            {
                "symbol": "BTCUSDT",
                "orderId": 5,
                "clientOrderId": call["params"]["origClientOrderId"],
                "price": "100.00",
                "origQty": "1.00000000",
                "executedQty": "1.00000000",
                "cummulativeQuoteQty": "100.00000000",
                "status": "FILLED",
                "side": "BUY",
                "type": "MARKET",
                "time": 1_700_000_000_000,
            },
        )

    def on_post(call):
        posts.append(call)
        return HttpResponse(200, {"orderId": 6, "status": "FILLED", "clientOrderId": "x"})

    broker, _, _ = make_broker(
        tmp_path,
        live=True,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
            "GET /v3/klines": klines_response([100.0]),
            "GET /v3/order": on_get,
            "POST /v3/order": on_post,
        },
    )
    order = broker.submit_order(OrderRequest("same-id", "BTC/USDT", Side.BUY, 1.0))
    assert order.id == "5"
    assert posts == []  # the exchange already knew this client order id


# --------------------------------------------------------------------------
# limits, accounts and reconciliation
# --------------------------------------------------------------------------

def test_weight_header_is_tracked_and_the_budget_stops_us(tmp_path):
    broker, transport, mem = make_broker(
        tmp_path,
        max_weight_per_min=99,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}, {"X-MBX-USED-WEIGHT-1M": "99"}),
            "GET /v3/exchangeInfo": HttpResponse(
                200, symbol_payload(), {"X-MBX-USED-WEIGHT-1M": "99"}
            ),
            "GET /v3/klines": HttpResponse(
                200, klines_response([100.0]).body, {"X-MBX-USED-WEIGHT-1M": "99"}
            ),
        },
    )
    broker.get_market_data("BTC/USDT", limit=2)
    assert broker.describe()["weight_used"] == 99
    with pytest.raises(Exception) as exc:
        broker.get_market_data("BTC/USDT", limit=2)
    assert "weight budget reached" in str(exc.value)
    assert [c for c in mem.api_calls() if c["note"] == "weight"]


def test_429_sets_a_cooldown_from_retry_after(tmp_path):
    responses = [
        HttpResponse(429, {"code": -1003, "msg": "Too many requests."}, {"Retry-After": "30"}),
    ]

    def klines(call):
        return responses.pop(0)

    broker, _, _ = make_broker(
        tmp_path, routes={"GET /v3/time": HttpResponse(200, {"serverTime": 1}), "GET /v3/klines": klines}
    )
    broker.sync_time()
    with pytest.raises(BinanceError):
        broker.get_market_data("BTC/USDT", limit=2)
    assert broker.describe()["cooldown_s"] > 0
    assert broker.describe()["cooldown_s"] <= 30
    with pytest.raises(Exception) as exc:
        broker.get_market_data("BTC/USDT", limit=2)
    assert "cooldown" in str(exc.value)


def test_account_balance_maps_balances_and_prices(tmp_path):
    broker, _, _ = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
            "GET /v3/account": account_balance({"USDT": (1_000.0, 50.0), "BTC": (0.5, 0.0)}),
            "GET /v3/klines": klines_response([100.0, 200.0]),
        },
    )
    snap = broker.get_account_balance()
    assert snap.cash == pytest.approx(1_050.0)  # free + locked
    assert snap.positions_value == pytest.approx(100.0)  # 0.5 BTC at the last close 200
    assert snap.equity == pytest.approx(1_150.0)
    assert snap.open_positions == 1
    assert snap.drawdown == 0.0  # the peak is what this adapter has seen


def test_drawdown_tracks_the_peak_this_adapter_saw(tmp_path):
    prices = {"close": 200.0}

    def klines(call):
        return klines_response([prices["close"]])

    broker, _, _ = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
            "GET /v3/account": account_balance({"USDT": (100.0, 0.0), "BTC": (1.0, 0.0)}),
            "GET /v3/klines": klines,
        },
    )
    assert broker.get_account_balance().equity == pytest.approx(300.0)
    prices["close"] = 100.0
    cached = broker.get_account_balance()  # within the 1s cache window
    assert cached.equity == pytest.approx(300.0)
    later = broker.get_account_balance(refresh=True)
    assert later.equity == pytest.approx(200.0)
    assert later.drawdown == pytest.approx(1 - 200.0 / 300.0)


def test_positions_come_from_balances_and_dust_is_ignored(tmp_path):
    broker, _, _ = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload(notional="5")),
            "GET /v3/account": account_balance({"BTC": (0.000001, 0.0), "USDT": (10.0, 0.0)}),
            "GET /v3/klines": klines_response([100.0]),
        },
    )
    assert broker.get_positions() == []  # 0.0001 USDT of BTC is dust

    broker2, _, _ = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload(notional="5")),
            "GET /v3/account": account_balance({"BTC": (0.5, 0.0), "USDT": (10.0, 0.0)}),
            "GET /v3/klines": klines_response([100.0]),
            "GET /v3/myTrades": HttpResponse(
                200,
                [
                    {"orderId": 1, "qty": "0.25", "price": "80.0", "isBuyer": True, "time": 1_700_000_000_000, "commission": "0.001", "commissionAsset": "BTC"},
                    {"orderId": 2, "qty": "0.25", "price": "100.0", "isBuyer": True, "time": 1_700_000_001_000, "commission": "0.001", "commissionAsset": "BTC"},
                ],
            ),
        },
    )
    positions = broker2.get_positions()
    assert len(positions) == 1
    assert positions[0].qty == pytest.approx(0.5)
    assert positions[0].avg_price == pytest.approx(90.0)  # weighted average of the buys
    assert positions[0].as_dict(price=100.0)["unrealized_pnl"] == pytest.approx(5.0)


def test_trade_history_maps_and_sorts(tmp_path):
    broker, _, _ = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
            "GET /v3/myTrades": HttpResponse(
                200,
                [
                    {"orderId": 2, "qty": "0.5", "price": "101.0", "isBuyer": False, "time": 1_700_000_002_000, "commission": "0.05", "commissionAsset": "USDT"},
                    {"orderId": 1, "qty": "0.5", "price": "100.0", "isBuyer": True, "time": 1_700_000_000_000, "commission": "0.0005", "commissionAsset": "BTC"},
                ],
            ),
        },
    )
    history = broker.get_trade_history(limit=10)
    assert [h["side"] for h in history] == ["buy", "sell"]
    assert history[0]["order_id"] == "1"
    assert history[1]["fee_asset"] == "USDT"
    assert history[0]["ts"] < history[1]["ts"]


def test_reconcile_reports_a_status_that_moved_on_the_exchange(tmp_path):
    def on_order(call):
        return HttpResponse(
            200,
            {
                "orderId": 42,
                "clientOrderId": "c42",
                "symbol": "BTCUSDT",
                "price": "100.00",
                "origQty": "1.00000000",
                "executedQty": "0.00000000",
                "status": "CANCELED",  # it was NEW on our side
                "side": "BUY",
                "type": "LIMIT",
                "time": 1_700_000_000_000,
            },
        )

    broker, _, _ = make_broker(
        tmp_path,
        live=True,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
            "GET /v3/klines": klines_response([100.0]),
            "GET /v3/order": on_order,
        },
    )
    broker._orders["42"] = _new_order("42", "BTC/USDT")
    broker._order_symbol["42"] = "BTC/USDT"
    mismatches = broker.reconcile()
    assert [m.what for m in mismatches] == ["order_status"]
    assert (mismatches[0].internal, mismatches[0].ledger) == ("new", "canceled")
    assert "moved on the exchange" in mismatches[0].as_dict()["detail"]
    # and once our own view has caught up, there is nothing left to report
    assert broker.reconcile() == []


def _new_order(order_id: str, symbol: str):
    from selftrade.execution import Order

    return Order(
        id=order_id,
        client_id="c42",
        symbol=symbol,
        side=Side.BUY,
        qty=1.0,
        type=OrderType.LIMIT,
        status=OrderStatus.NEW,
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_cancel_is_refused_while_unarmed(tmp_path):
    broker, _, _ = make_broker(
        tmp_path, routes={"GET /v3/time": HttpResponse(200, {"serverTime": 1})}
    )
    with pytest.raises(Exception) as exc:
        broker.cancel_order("42")
    assert "not armed" in str(exc.value)


def test_disabled_adapter_never_calls_the_transport(tmp_path):
    broker, transport, _ = make_broker(
        tmp_path, enabled=False, routes={"GET /v3/time": HttpResponse(200, {"serverTime": 1})}
    )
    assert broker.describe()["enabled"] is False
    with pytest.raises(Exception) as exc:
        broker.get_market_data("BTC/USDT")
    assert "disabled" in str(exc.value)
    assert transport.calls == []
    assert broker.mode == "paper"


def test_on_market_data_returns_the_project_ohlcv_contract(tmp_path):
    broker, _, _ = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/klines": klines_response([100.0, 101.0, 102.0]),
        },
    )
    df = broker.get_market_data("BTC/USDT", limit=3)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "UTC"
    assert len(df) == 3


def test_a_market_data_failure_refuses_instead_of_crashing(tmp_path):
    """No klines means no price: the order is refused, not sent half-validated."""
    broker, transport, mem = make_broker(
        tmp_path,
        routes={
            "GET /v3/time": HttpResponse(200, {"serverTime": 1}),
            "GET /v3/exchangeInfo": HttpResponse(200, symbol_payload()),
        },
    )
    order = broker.submit_order(OrderRequest("c1", "BTC/USDT", Side.BUY, 1.0))
    assert order.status is OrderStatus.REJECTED
    assert "cannot price or validate" in order.reject_reason
    assert not any(
        c["method"] == "POST" and c["url"].endswith("/order") for c in transport.calls
    )
    assert mem.risk_events()  # the refusal is on the record


def test_unknown_symbol_is_refused_before_any_request(tmp_path):
    broker, transport, _ = make_broker(
        tmp_path, routes={"GET /v3/time": HttpResponse(200, {"serverTime": 1})}
    )
    order = broker.submit_order(OrderRequest("c1", "ETH/USDT", Side.BUY, 1.0))
    assert order.status is OrderStatus.REJECTED
    assert "not configured here" in order.reject_reason
    assert transport.calls == []


def test_the_network_lives_outside_the_execution_package():
    """The offline contract stays offline; the adapter that sends lives beside it."""
    root = Path(__file__).resolve().parents[1] / "selftrade"
    execution = root / "execution"
    for path in sorted(execution.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = {
            n.names[0].name.split(".")[0]
            for n in ast.walk(tree)
            if isinstance(n, ast.Import)
        } | {
            (n.module or "").split(".")[0]
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom)
        }
        assert not modules & {"requests", "socket", "urllib", "http", "aiohttp"}, path.name

    adapter = ast.parse((root / "binance.py").read_text(encoding="utf-8"))
    assert any(
        isinstance(n, ast.Import) and any(a.name.split(".")[0] == "requests" for a in n.names)
        for n in ast.walk(adapter)
    )


def test_describe_lists_env_var_names_but_no_values(tmp_path):
    broker, _, _ = make_broker(tmp_path)
    info = broker.describe()
    assert info["credentials"] == {
        "SELFTD_BINANCE_API_KEY": True,
        "SELFTD_BINANCE_API_SECRET": True,
    }
    assert SECRET not in json.dumps(info)
    assert KEY not in json.dumps(info)
    assert info["orders_submitted"] == 0


# --------------------------------------------------------------------------
# production is an explicit opt-in, and the sealed gate rides every real order
# --------------------------------------------------------------------------

def test_a_production_host_without_the_opt_in_is_refused(tmp_path):
    s = load_settings(None)
    assert s.binance.allow_production is False
    with pytest.raises(HostRefusedError) as exc:
        make_broker(tmp_path, base_url="https://api.binance.com/api")
    assert "allow_production" in str(exc.value)
    assert "real money" in str(exc.value)


def test_an_unknown_host_is_refused_even_with_the_opt_in(tmp_path):
    with pytest.raises(HostRefusedError):
        make_broker(
            tmp_path,
            allow_production=True,
            base_url="https://api-binance.example.com/api",
        )


def test_the_opt_in_reaches_production_and_reports_live(tmp_path):
    broker, _, _ = make_broker(
        tmp_path,
        live=True,
        allow_production=True,
        base_url="https://api.binance.com/api",
    )
    assert broker.production is True
    assert broker.mode == "live"  # not "testnet", not "paper"
    info = broker.describe()
    assert info["production"] is True
    assert info["allow_production"] is True
    assert info["host_is_testnet"] is False
    assert "PRODUCTION" in info["mode_note"] and "real money" in info["mode_note"]


def test_without_the_opt_in_the_mode_stays_testnet(tmp_path):
    broker, _, _ = make_broker(tmp_path, live=True)
    assert broker.production is False
    assert broker.mode == "testnet"


def test_the_sealed_gate_blocks_a_sent_order_and_records_it(tmp_path):
    """An armed order passes the sealed gate first; a refusal never reaches Binance."""
    from selftrade.execution import RiskGate

    gate = RiskGate(allowed_symbols=["ETH/USDT"])  # BTC/USDT is not on the list
    broker, transport, mem = make_broker(
        tmp_path, live=True, routes=_send_routes(), risk_gate=gate
    )
    order = broker.submit_order(OrderRequest("gate-1", "BTC/USDT", Side.BUY, 1.0))
    assert order.status is OrderStatus.REJECTED
    assert "sealed risk gate" in order.reject_reason
    assert "allowed_symbols" in order.reject_reason
    # the entry order was never sent
    assert _posts(transport) == []
    # and the refusal is on the record, with the checks that produced it
    events = [e for e in mem.risk_events() if "risk gate" in e["reason"]]
    assert events
    assert any((e["detail"] or {}).get("checks") for e in events)


def test_the_gate_refuses_when_the_account_cannot_be_read(tmp_path):
    """A gate that cannot see the book refuses; it does not guess."""
    from selftrade.execution import RiskGate

    routes = _send_routes()
    del routes["GET /v3/account"]  # the account read now fails
    broker, transport, _ = make_broker(
        tmp_path, live=True, routes=routes, risk_gate=RiskGate()
    )
    order = broker.submit_order(OrderRequest("gate-2", "BTC/USDT", Side.BUY, 1.0))
    assert order.status is OrderStatus.REJECTED
    assert "cannot read the account" in order.reject_reason
    assert _posts(transport) == []


def test_the_kill_switch_blocks_a_sent_order(tmp_path):
    from selftrade.execution import KillSwitch, RiskGate

    switch = KillSwitch(tmp_path / "ks.json")
    switch.trigger("daily loss breached", by="test")
    broker, transport, _ = make_broker(
        tmp_path, live=True, routes=_send_routes(), risk_gate=RiskGate(), kill_switch=switch
    )
    order = broker.submit_order(OrderRequest("gate-3", "BTC/USDT", Side.BUY, 1.0))
    assert order.status is OrderStatus.REJECTED
    assert "kill switch" in order.reject_reason
    assert _posts(transport) == []


def test_an_order_that_passes_the_gate_is_actually_sent(tmp_path):
    """The gate is a filter, not a wall: with the gates clear the order goes out."""
    from selftrade.execution import RiskGate

    gate = RiskGate(allowed_symbols=["BTC/USDT"], require_stop=False, min_notional=0.0)
    broker, transport, _ = make_broker(
        tmp_path, live=True, routes=_send_routes(), risk_gate=gate
    )
    order = broker.submit_order(OrderRequest("gate-4", "BTC/USDT", Side.BUY, 1.0))
    assert order.status is OrderStatus.FILLED
    assert len(_posts(transport)) == 1


def test_a_dry_run_does_not_consult_the_gate(tmp_path):
    """Unarmed, nothing is sent, so the gate is not in the path (and not in the way)."""
    from selftrade.execution import RiskGate

    broker, transport, _ = make_broker(
        tmp_path,
        live=False,
        routes=_send_routes(),
        risk_gate=RiskGate(allowed_symbols=["ETH/USDT"]),
    )
    order = broker.submit_order(OrderRequest("gate-5", "BTC/USDT", Side.BUY, 1.0))
    assert order.status is OrderStatus.DRY_RUN
    assert _posts(transport) == []


# --------------------------------------------------------------------------
# the CLI is not a side door: an order typed at the prompt goes through the
# same gate as one submitted through the API, with the errors kept off a
# real exchange.  The transport is patched out, so nothing leaves the test.
# --------------------------------------------------------------------------


def _cli_config(tmp_path, *, live=True, enabled=True, extra_lines=()):
    """A testnet config whose database and session file stay inside ``tmp_path``."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "binance:",
                f"  enabled: {str(enabled).lower()}",
                f"  live: {str(live).lower()}",
                "  base_url: https://testnet.binance.vision/api",
                "memory:",
                f"  db: {(tmp_path / 'state' / 'm.db').as_posix()}",
                f"session_file: {(tmp_path / 'state' / 'session.json').as_posix()}",
                "notify:",
                "  enabled: false",
                "llm:",
                "  enabled: false",
                *extra_lines,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return cfg


def _cli(monkeypatch, cfg, transport, *args, creds=True):
    """Run the real CLI with the transport replaced and, by default, a key exported."""
    from selftrade import binance as bn_mod
    from selftrade.cli import main

    monkeypatch.setattr(bn_mod, "RequestsTransport", lambda *a, **k: transport)
    if creds:
        monkeypatch.setenv("SELFTD_BINANCE_API_KEY", KEY)
        monkeypatch.setenv("SELFTD_BINANCE_API_SECRET", SECRET)
    else:
        monkeypatch.delenv("SELFTD_BINANCE_API_KEY", raising=False)
        monkeypatch.delenv("SELFTD_BINANCE_API_SECRET", raising=False)
    return main(["--config", str(cfg), "binance", *args])


def test_the_cli_arms_orders_with_the_sealed_gate_and_the_kill_switch(tmp_path, monkeypatch, capsys):  # noqa: E501
    """The switch is wired into the CLI's order path, so a typed order is blocked too."""
    from selftrade.execution import KillSwitch, kill_switch_path

    cfg = _cli_config(tmp_path)
    transport = FakeTransport(_send_routes())
    KillSwitch(kill_switch_path(load_settings(cfg))).trigger("daily loss breached", by="test")

    code = _cli(monkeypatch, cfg, transport, "order", "--qty", "1.0", "--live")

    captured = capsys.readouterr()
    assert code == 1
    assert "kill switch" in captured.out + captured.err
    assert _posts(transport) == []


def test_the_cli_order_passes_the_default_gate_and_is_refused_for_want_of_a_stop(
    tmp_path, monkeypatch, capsys
):
    """``risk_gate.require_stop`` defaults to true, and a Spot entry cannot carry one."""
    cfg = _cli_config(tmp_path)
    transport = FakeTransport(_send_routes())

    code = _cli(monkeypatch, cfg, transport, "order", "--qty", "1.0", "--live")

    captured = capsys.readouterr()
    assert code == 1
    assert "stop loss" in captured.out + captured.err
    assert _posts(transport) == []


def test_a_cli_order_that_clears_the_gate_is_sent_once(tmp_path, monkeypatch):
    """The wiring is a filter, not a wall: with the stop requirement lifted it goes out."""
    cfg = _cli_config(tmp_path, extra_lines=["risk_gate:", "  require_stop: false"])
    transport = FakeTransport(_send_routes())

    code = _cli(monkeypatch, cfg, transport, "order", "--qty", "1.0", "--live")

    assert code == 0
    assert len(_posts(transport)) == 1


def test_the_cli_refuses_live_without_the_config_key(tmp_path, monkeypatch, capsys):
    """Two keys: ``--live`` alone sends nothing and never even builds a client."""
    cfg = _cli_config(tmp_path, live=False)
    transport = FakeTransport(_send_routes())

    code = _cli(monkeypatch, cfg, transport, "order", "--qty", "1.0", "--live")

    assert code == 1
    assert "binance.live" in capsys.readouterr().err
    assert transport.calls == []


# --------------------------------------------------------------------------
# the read-only account check: what a freshly exported key meets first.  It
# reads the clock, the signed account, the permissions, the balance and the
# open orders - and the report carries the POST count, so "it never sends" is
# a number in the output rather than a promise in a docstring.
# --------------------------------------------------------------------------


def test_account_check_reads_the_clock_the_key_and_the_book_without_sending(tmp_path):
    broker, transport, mem = make_broker(tmp_path, routes=check_routes())

    report = broker.account_check()

    assert report["ok"] is True
    assert report["verdict"] == "ok"
    assert report["host_is_testnet"] is True
    assert report["production"] is False
    assert report["armed"] is False
    assert report["clock"] == {
        "server_time_ms": 1_700_000_000_000,
        "local_time_ms": 1_700_000_000_000,
        "offset_ms": 0.0,
        "recv_window_ms": 5000,
        "ok": True,
    }
    assert report["signature"] == {"ok": True, "path": "/v3/account", "error": None}
    assert report["permissions"]["canTrade"] is True
    assert report["permissions"]["canWithdraw"] is False
    assert report["permissions"]["accountType"] == "SPOT"
    assert report["permissions"]["permissions"] == ["SPOT"]
    assert report["trade_ready"] is True
    assert report["account"]["cash"] == pytest.approx(10_000.0)
    assert report["account"]["equity"] == pytest.approx(10_000.0)
    assert [row["asset"] for row in report["account"]["balances"]] == ["USDT"]
    assert [entry["name"] for entry in report["steps"]] == [
        "credentials",
        "clock",
        "signature",
        "permissions",
        "balance",
        "open_orders",
        "read_only",
    ]
    assert all(entry["ok"] for entry in report["steps"])

    # the claim is checkable, not asserted
    assert report["http"]["calls"] == 4
    assert report["http"]["posts"] == 0
    assert report["http"]["methods"] == ["GET"]
    assert report["http"]["read_only"] is True
    # no ``/v3/klines``: the account holds only the quote asset, so nothing needed
    # a price - the check reads what it must and no more
    assert report["http"]["paths"] == [
        "/v3/account",
        "/v3/exchangeInfo",
        "/v3/openOrders",
        "/v3/time",
    ]
    assert _posts(transport) == []
    assert broker.orders_submitted == 0
    assert {c["method"] for c in mem.api_calls(provider="binance-testnet")} == {"GET"}
    # and no secret leaks into the report
    text = json.dumps(report)
    assert KEY not in text and SECRET not in text


def test_account_check_reports_a_refused_key_with_the_next_step(tmp_path):
    routes = check_routes(
        **{
            "GET /v3/account": HttpResponse(
                401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}
            )
        }
    )
    broker, transport, _ = make_broker(tmp_path, routes=routes)

    report = broker.account_check()

    assert report["ok"] is False
    assert report["verdict"] == "problems found"
    assert report["signature"]["ok"] is False
    assert report["signature"]["error"] == {
        "code": -2015,
        "msg": "Invalid API-key, IP, or permissions for action.",
        "status": 401,
    }
    assert report["trade_ready"] is None
    assert any("separate accounts" in hint for hint in report["hints"])
    failed = [entry["name"] for entry in report["steps"] if not entry["ok"]]
    assert failed == ["signature"]
    # a refused key stops the signed reads: the clock was asked, then the account
    assert [c["url"].split("/api")[-1] for c in transport.calls] == ["/v3/time", "/v3/account"]
    assert _posts(transport) == []


def test_account_check_without_credentials_asks_nothing_of_binance(tmp_path):
    broker, transport, _ = make_broker(tmp_path, routes=check_routes(), api_key="", api_secret="")

    report = broker.account_check()

    assert report["ok"] is False
    assert report["credentials"] == {
        "SELFTD_BINANCE_API_KEY": False,
        "SELFTD_BINANCE_API_SECRET": False,
    }
    assert "SELFTD_BINANCE_API_KEY and SELFTD_BINANCE_API_SECRET" in report["problems"][0]
    assert [entry["name"] for entry in report["steps"]] == ["credentials", "read_only"]
    assert transport.calls == []
    assert report["http"] == {
        "calls": 0,
        "posts": 0,
        "methods": [],
        "paths": [],
        "read_only": True,
    }


def test_account_check_warns_about_a_withdrawable_key_without_failing(tmp_path):
    routes = check_routes(
        **{"GET /v3/account": account_payload(can_withdraw=True, permissions=("SPOT",))}
    )
    broker, _, _ = make_broker(tmp_path, routes=routes)

    report = broker.account_check()

    assert report["ok"] is True
    assert report["verdict"] == "ok, with warnings"
    assert any("WITHDRAW" in warning for warning in report["warnings"])


def test_account_check_says_when_the_key_cannot_trade(tmp_path):
    routes = check_routes(
        **{"GET /v3/account": account_payload(can_trade=False, permissions=("SPOT",))}
    )
    broker, _, _ = make_broker(tmp_path, routes=routes)

    report = broker.account_check()

    assert report["ok"] is True  # the probe worked; the key simply cannot trade
    assert report["trade_ready"] is False
    assert any("cannot trade" in warning for warning in report["warnings"])


def test_account_check_leaves_trade_ready_unknown_when_the_payload_is_silent(tmp_path):
    routes = check_routes(
        **{"GET /v3/account": HttpResponse(200, {"accountType": "SPOT", "balances": []})}
    )
    broker, _, _ = make_broker(tmp_path, routes=routes)

    report = broker.account_check()

    assert report["permissions"]["canTrade"] is None
    assert report["trade_ready"] is None  # unknown is not the same as false
    assert report["ok"] is True


def test_account_check_lists_the_open_orders(tmp_path):
    row = {
        "orderId": 77,
        "clientOrderId": "selftd-1",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "status": "NEW",
        "price": "100.00",
        "origQty": "0.5",
        "executedQty": "0",
        "cummulativeQuoteQty": "0",
        "time": 1_700_000_000_000,
    }
    routes = check_routes(**{"GET /v3/openOrders": HttpResponse(200, [row])})
    broker, transport, _ = make_broker(tmp_path, routes=routes)

    report = broker.account_check()

    assert report["open_orders"]["count"] == 1
    order = report["open_orders"]["orders"][0]
    assert order["id"] == "77"
    assert order["symbol"] == "BTC/USDT"
    assert order["side"] == "buy"
    assert order["status"] == "new"
    assert order["limit_price"] == pytest.approx(100.0)
    assert _posts(transport) == []


def test_account_check_still_reports_the_balance_when_the_pricing_fails(tmp_path):
    routes = check_routes(
        **{"GET /v3/exchangeInfo": HttpResponse(500, {"code": -1001, "msg": "DISCONNECTED"})}
    )
    broker, transport, _ = make_broker(tmp_path, routes=routes)

    report = broker.account_check()

    assert report["ok"] is False
    assert report["account"]["equity"] is None
    assert [row["asset"] for row in report["account"]["balances"]] == ["USDT"]
    assert any("could not be priced" in problem for problem in report["problems"])
    assert any(entry["name"] == "balance" and not entry["ok"] for entry in report["steps"])
    assert _posts(transport) == []


def test_account_check_flags_a_clock_that_is_past_the_recv_window(tmp_path):
    routes = check_routes(**{"GET /v3/time": HttpResponse(200, {"serverTime": 1_700_000_010_000})})
    broker, _, _ = make_broker(tmp_path, routes=routes)

    report = broker.account_check()

    assert report["clock"]["ok"] is False
    assert report["clock"]["offset_ms"] == pytest.approx(10_000.0)
    assert report["ok"] is False
    assert any("recvWindow" in problem for problem in report["problems"])


def test_a_strict_open_orders_read_surfaces_the_failure(tmp_path):
    """``open_orders`` skips a symbol it cannot read; the check must not."""
    routes = check_routes()
    del routes["GET /v3/openOrders"]
    broker, _, _ = make_broker(tmp_path, routes=routes)

    with pytest.raises(BinanceError):
        broker.open_orders(strict=True)

    assert broker.open_orders() == []  # the tolerant path is unchanged


def test_the_cli_check_prints_a_readable_report_and_sends_nothing(tmp_path, monkeypatch, capsys):
    cfg = _cli_config(tmp_path)
    transport = FakeTransport(cli_check_routes())

    code = _cli(monkeypatch, cfg, transport, "check")

    out = capsys.readouterr().out
    assert code == 0
    assert "account check" in out
    assert "verdict: ok" in out
    assert "no order can come out of this command" in out
    assert "0 POST" in out
    assert _posts(transport) == []


def test_the_cli_check_exits_one_when_binance_refuses_the_key(tmp_path, monkeypatch, capsys):
    cfg = _cli_config(tmp_path)
    routes = cli_check_routes(
        **{
            "GET /v3/account": HttpResponse(
                401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}
            )
        }
    )
    transport = FakeTransport(routes)

    code = _cli(monkeypatch, cfg, transport, "check")

    out = capsys.readouterr().out
    assert code == 1
    assert "problem:" in out
    assert "hint:" in out
    assert _posts(transport) == []


def test_the_cli_check_exits_one_without_credentials_and_touches_nothing(
    tmp_path, monkeypatch, capsys
):
    cfg = _cli_config(tmp_path)
    transport = FakeTransport(cli_check_routes())

    code = _cli(monkeypatch, cfg, transport, "check", creds=False)

    out = capsys.readouterr().out
    assert code == 1
    assert "no credentials" in out
    assert "SELFTD_BINANCE_API_KEY" in out
    assert transport.calls == []


def test_the_cli_check_cannot_be_armed_by_live(tmp_path, monkeypatch, capsys):
    """``--live`` must not arm a read-only check, and must not trip the key guard."""
    cfg = _cli_config(tmp_path, live=False)
    transport = FakeTransport(cli_check_routes())

    code = _cli(monkeypatch, cfg, transport, "check", "--live")

    captured = capsys.readouterr()
    assert code == 0
    assert "read-only" in captured.err
    assert "armed=False" in captured.out
    assert _posts(transport) == []


def test_the_cli_check_needs_the_rail_enabled(tmp_path, monkeypatch, capsys):
    cfg = _cli_config(tmp_path, enabled=False)
    transport = FakeTransport(cli_check_routes())

    code = _cli(monkeypatch, cfg, transport, "check")

    captured = capsys.readouterr()
    assert code == 1
    assert "binance.enabled=true" in captured.out + captured.err
    assert transport.calls == []


def test_the_cli_check_json_is_the_whole_report(tmp_path, monkeypatch, capsys):
    cfg = _cli_config(tmp_path)
    transport = FakeTransport(cli_check_routes())

    code = _cli(monkeypatch, cfg, transport, "check", "--json")

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["signature"]["ok"] is True
    assert payload["http"]["posts"] == 0
    assert payload["account"]["equity"] == pytest.approx(10_000.0)


# --------------------------------------------------------------------------
# the protective stop: the plan becomes a resting order the moment the fill
# lands, and a position that could not be protected says so out loud.  The
# rules enforced here are the documented ones - STOP_LOSS_LIMIT needs
# timeInForce + quantity + price + stopPrice, PRICE_FILTER governs
# price/stopPrice, and a resting stop is an "algo" order against
# MAX_NUM_ALGO_ORDERS.
# --------------------------------------------------------------------------


def test_the_stop_is_placed_right_after_the_entry_fills(tmp_path):
    broker, transport, mem = make_broker(tmp_path, live=True, routes=protection_routes())

    order = broker.submit_order(
        OrderRequest("entry-1", "BTC/USDT", Side.BUY, 1.0, stop_loss_pct=-0.02)
    )

    assert order.status is OrderStatus.FILLED
    posts = _posts(transport)
    assert len(posts) == 2  # the entry, then the stop
    entry_fields, stop_fields = post_fields(posts[0]), post_fields(posts[1])
    assert (entry_fields["type"], entry_fields["side"]) == ("MARKET", "BUY")
    assert stop_fields["type"] == "STOP_LOSS_LIMIT"
    assert stop_fields["side"] == "SELL"
    assert stop_fields["timeInForce"] == "GTC"
    assert stop_fields["newClientOrderId"] == "entry-1-stop"
    # a non-MARKET/LIMIT order answers ACK by default, which carries no status back
    assert stop_fields["newOrderRespType"] == "RESULT"
    # 101 * 0.98, rounded down to the 0.01 tick; sized from the *free* balance
    assert Decimal(stop_fields["stopPrice"]) == Decimal("98.98")
    assert Decimal(stop_fields["price"]) == Decimal("98.98")
    assert Decimal(stop_fields["quantity"]) == Decimal("0.999")

    stop = broker.protection_of("entry-1")
    assert stop is not None
    assert stop.client_id == "entry-1-stop"
    assert stop.type is OrderType.STOP_LOSS_LIMIT
    assert stop.status is OrderStatus.NEW
    assert stop.limit_price == pytest.approx(98.98)
    assert "protected: SELL STOP_LOSS_LIMIT 0.999 @ 98.98" in order.note

    # and the whole plan is on the record, not in a log line
    sent = [
        c
        for c in mem.api_calls()
        if c["path"] == "/v3/order" and c["method"] == "POST" and not c["dry_run"]
    ]
    assert len(sent) == 2
    assert {row["params"]["type"] for row in sent} == {"MARKET", "STOP_LOSS_LIMIT"}


def test_the_stop_is_sized_from_the_free_balance_not_the_filled_quantity(tmp_path):
    """The commission comes out of the received asset, so ``executedQty`` is not sellable."""
    broker, transport, _ = make_broker(
        tmp_path, live=True, routes=protection_routes(free="0.5")
    )

    order = broker.submit_order(
        OrderRequest("entry-2", "BTC/USDT", Side.BUY, 1.0, stop_loss_pct=-0.02)
    )

    assert order.status is OrderStatus.FILLED
    assert Decimal(post_fields(_posts(transport)[1])["quantity"]) == Decimal("0.5")


def test_the_stop_level_follows_the_average_fill_price_not_the_reference(tmp_path):
    broker, transport, _ = make_broker(tmp_path, live=True, routes=protection_routes())

    broker.submit_order(OrderRequest("entry-3", "BTC/USDT", Side.BUY, 1.0, stop_loss_pct=-0.10))

    # the klines answer says 100, the fill says 101: the stop follows the fill
    fields = post_fields(_posts(transport)[1])
    assert Decimal(fields["stopPrice"]) == Decimal("90.9")


def test_a_dry_run_records_both_legs_and_sends_neither(tmp_path):
    broker, transport, mem = make_broker(tmp_path, live=False, routes=protection_routes())

    order = broker.submit_order(
        OrderRequest("dry-1", "BTC/USDT", Side.BUY, 1.0, stop_loss_pct=-0.02)
    )

    assert order.status is OrderStatus.DRY_RUN
    assert _posts(transport) == []
    recorded = [
        c for c in mem.api_calls() if c["path"] == "/v3/order" and c["dry_run"]
    ]
    assert len(recorded) == 2
    assert {row["params"]["type"] for row in recorded} == {"MARKET", "STOP_LOSS_LIMIT"}
    assert "STOP_LOSS_LIMIT" in order.note and "not sent" in order.note


def test_a_filled_entry_that_cannot_be_protected_is_alerted_not_silent(tmp_path):
    refused = HttpResponse(
        400, {"code": -2010, "msg": "Account has insufficient balance for requested action."}
    )
    broker, transport, mem = make_broker(
        tmp_path, live=True, routes=protection_routes(stop_response=refused)
    )

    order = broker.submit_order(
        OrderRequest("entry-4", "BTC/USDT", Side.BUY, 1.0, stop_loss_pct=-0.02)
    )

    assert order.status is OrderStatus.FILLED  # the entry really did fill
    assert "UNPROTECTED" in order.note
    assert "insufficient balance" in order.note
    assert broker.protection_of("entry-4") is None
    assert len(_posts(transport)) == 2  # the stop was attempted, and refused
    alerts = [e for e in mem.risk_events() if "UNPROTECTED" in e["reason"]]
    assert alerts and alerts[0]["detail"]["order_id"] == order.id
    assert alerts[0]["kind"] == "order_blocked"


def test_an_existing_stop_is_found_instead_of_placed_twice(tmp_path):
    existing = {
        "symbol": "BTCUSDT",
        "orderId": 4242,
        "clientOrderId": "entry-5-stop",
        "price": "98.98",
        "origQty": "0.49950",
        "executedQty": "0",
        "cummulativeQuoteQty": "0",
        "status": "NEW",
        "type": "STOP_LOSS_LIMIT",
        "side": "SELL",
        "timeInForce": "GTC",
        "transactTime": 1_700_000_000_000,
    }
    routes = protection_routes()
    routes["GET /v3/order"] = by_client_id(existing)
    broker, transport, _ = make_broker(tmp_path, live=True, routes=routes)

    order = broker.submit_order(
        OrderRequest("entry-5", "BTC/USDT", Side.BUY, 1.0, stop_loss_pct=-0.02)
    )

    assert len(_posts(transport)) == 1  # the entry only: no second stop on one position
    assert "protected" in order.note and "4242" in order.note
    assert broker.protection_of("entry-5").id == "4242"


def test_the_algo_order_budget_is_counted_from_the_exchange(tmp_path):
    """``MAX_NUM_ALGO_ORDERS`` is real: a resting stop is an algo order."""
    someone_else = {
        "symbol": "BTCUSDT",
        "orderId": 9,
        "clientOrderId": "someone-else-stop",
        "price": "1.0",
        "origQty": "1.0",
        "executedQty": "0",
        "cummulativeQuoteQty": "0",
        "status": "NEW",
        "type": "STOP_LOSS_LIMIT",
        "side": "SELL",
    }
    routes = protection_routes(payload=symbol_payload(max_algo_orders=1))
    routes["GET /v3/openOrders"] = HttpResponse(200, [someone_else])
    broker, transport, _ = make_broker(tmp_path, live=True, routes=routes)

    order = broker.submit_order(
        OrderRequest("entry-6", "BTC/USDT", Side.BUY, 1.0, stop_loss_pct=-0.02)
    )

    assert len(_posts(transport)) == 1  # the entry went out, the stop was not attempted
    assert "UNPROTECTED" in order.note
    assert "MAX_NUM_ALGO_ORDERS" in order.note


def test_protect_places_the_stop_for_a_position_that_is_already_open(tmp_path):
    routes = protection_routes()
    routes["GET /v3/order"] = by_client_id(
        filled_entry(clientOrderId="late-1", orderId=7001)
    )
    broker, transport, _ = make_broker(tmp_path, live=True, routes=routes)

    stop = broker.protect("late-1", -0.03)

    assert stop.type is OrderType.STOP_LOSS_LIMIT
    assert stop.client_id == "late-1-stop"
    fields = post_fields(_posts(transport)[0])
    assert fields["side"] == "SELL" and fields["type"] == "STOP_LOSS_LIMIT"
    assert Decimal(fields["stopPrice"]) == Decimal("97.97")  # 101 * 0.97

    with pytest.raises(BrokerError, match="nothing to protect"):
        broker.protect("nobody", -0.03)


def test_protect_refuses_a_short_and_an_unfilled_entry(tmp_path):
    routes = protection_routes()
    routes["GET /v3/order"] = by_client_id(
        filled_entry(clientOrderId="sell-1", side="SELL"),
        filled_entry(clientOrderId="open-1", status="NEW", executedQty="0"),
    )
    broker, transport, _ = make_broker(tmp_path, live=True, routes=routes)

    with pytest.raises(BrokerError, match="long position only"):
        broker.protect("sell-1", -0.02)
    with pytest.raises(BrokerError, match="no position to protect"):
        broker.protect("open-1", -0.02)
    assert _posts(transport) == []


def test_an_unarmed_adapter_cannot_place_a_stop(tmp_path):
    routes = protection_routes()
    routes["GET /v3/order"] = by_client_id(filled_entry(clientOrderId="late-2"))
    broker, transport, _ = make_broker(tmp_path, live=False, routes=routes)

    with pytest.raises(BrokerError, match="not armed"):
        broker.protect("late-2", -0.02)
    assert _posts(transport) == []


def test_cancel_protection_cancels_the_resting_stop(tmp_path):
    routes = protection_routes()
    routes["DELETE /v3/order"] = HttpResponse(
        200, {"symbol": "BTCUSDT", "orderId": 5002, "clientOrderId": "entry-7-stop",
              "status": "CANCELED", "price": "98.98", "origQty": "0.999",
              "executedQty": "0", "cummulativeQuoteQty": "0", "type": "STOP_LOSS_LIMIT",
              "side": "SELL", "timeInForce": "GTC"}
    )
    broker, transport, _ = make_broker(tmp_path, live=True, routes=routes)
    broker.submit_order(OrderRequest("entry-7", "BTC/USDT", Side.BUY, 1.0, stop_loss_pct=-0.02))

    canceled = broker.cancel_protection("entry-7")

    deletes = [c for c in transport.calls if c["method"] == "DELETE"]
    assert len(deletes) == 1
    assert post_fields(deletes[0])["orderId"] == "5002"
    assert canceled.status is OrderStatus.CANCELED


def test_a_stop_that_already_fired_is_not_cancelled(tmp_path):
    """The stop is re-read at the exchange: a stop that sold the position has nothing to undo."""
    state = {"placed": False}

    def on_get(call):
        wanted = (call.get("params") or {}).get("origClientOrderId")
        if wanted == "entry-8-stop" and state["placed"]:
            return HttpResponse(
                200,
                {"symbol": "BTCUSDT", "orderId": 5002, "clientOrderId": "entry-8-stop",
                 "status": "FILLED", "price": "98.98", "origQty": "0.999",
                 "executedQty": "0.999", "cummulativeQuoteQty": "97.9",
                 "type": "STOP_LOSS_LIMIT", "side": "SELL"},
            )
        return HttpResponse(404, {"code": -2013, "msg": "Order does not exist."})

    def on_post(call):
        if post_fields(call).get("type") == "STOP_LOSS_LIMIT":
            state["placed"] = True
            return resting_stop(call)
        return HttpResponse(200, filled_entry(clientOrderId="entry-8"))

    routes = protection_routes()
    routes["GET /v3/order"] = on_get
    routes["POST /v3/order"] = on_post
    routes["DELETE /v3/order"] = HttpResponse(200, {"orderId": 5002, "status": "CANCELED"})
    broker, transport, _ = make_broker(tmp_path, live=True, routes=routes)
    broker.submit_order(OrderRequest("entry-8", "BTC/USDT", Side.BUY, 1.0, stop_loss_pct=-0.02))

    stop = broker.cancel_protection("entry-8")

    assert stop.status is OrderStatus.FILLED  # it did its job: it is not "cancelled"
    assert [c for c in transport.calls if c["method"] == "DELETE"] == []


def test_the_default_gate_accepts_an_entry_that_carries_its_stop(tmp_path):
    """``require_stop`` defaults to true: before this, such an order could never be sent."""
    from selftrade.execution import RiskGate

    gate = RiskGate(require_stop=True, min_notional=0.0, max_positions=1)
    broker, transport, _ = make_broker(
        tmp_path, live=True, routes=protection_routes(), risk_gate=gate
    )

    order = broker.submit_order(
        OrderRequest("gate-stop", "BTC/USDT", Side.BUY, 1.0, stop_loss_pct=-0.02)
    )

    assert order.status is OrderStatus.FILLED
    assert len(_posts(transport)) == 2
    assert "protected" in order.note


def test_the_cli_orders_with_a_stop_and_prints_the_protection(tmp_path, monkeypatch, capsys):
    cfg = _cli_config(tmp_path)
    transport = FakeTransport(protection_routes())

    code = _cli(
        monkeypatch, cfg, transport, "order", "--qty", "1.0", "--live", "--stop-pct", "-0.02"
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "protected: SELL STOP_LOSS_LIMIT" in out
    posts = _posts(transport)
    assert len(posts) == 2
    assert post_fields(posts[1])["newClientOrderId"].endswith("-stop")


def test_the_cli_protect_and_unprotect_actions(tmp_path, monkeypatch, capsys):
    cfg = _cli_config(tmp_path)
    routes, state = exchange_with_protection(
        filled_entry(clientOrderId="late-9", orderId=7001)
    )
    transport = FakeTransport(routes)

    assert _cli(monkeypatch, cfg, transport, "protect", "--client-id", "late-9",
                "--stop-pct", "-0.02", "--live") == 0
    out = capsys.readouterr().out
    assert "binance protect:" in out and "stop_loss_limit" in out
    assert "guarding the entry late-9" in out

    # a *second process*: the in-memory map is gone, and the stop is still found from
    # its deterministic client id - protect in one terminal, unprotect in another
    assert _cli(monkeypatch, cfg, transport, "unprotect", "--client-id", "late-9",
                "--live") == 0
    assert "canceled" in capsys.readouterr().out
    assert state["orders"]["late-9-stop"]["status"] == "CANCELED"

    # the second key is required: touching real protection has no rehearsal
    assert _cli(monkeypatch, cfg, transport, "protect", "--client-id", "late-9",
                "--stop-pct", "-0.02") == 1
    assert "without both keys" in capsys.readouterr().err

    assert _cli(monkeypatch, cfg, transport, "protect", "--client-id", "late-9",
                "--live") == 1
    assert "--stop-pct is required" in capsys.readouterr().err
    assert _cli(monkeypatch, cfg, transport, "protect", "--stop-pct", "-0.02",
                "--live") == 1
    assert "--client-id is required" in capsys.readouterr().err
    assert _cli(monkeypatch, cfg, transport, "unprotect", "--client-id", "nope",
                "--live") == 1
    assert "no protective stop" in capsys.readouterr().err


# --------------------------------------------------------------------------
# `key_health`: the one probe behind `binance check` and the dashboard panel.
# It is read-only *and* unarmed by construction, so the two callers cannot drift
# apart, and it always answers - a rail that is off is reported, not skipped.
# --------------------------------------------------------------------------

def test_key_health_reports_a_disabled_rail_without_a_single_request(tmp_path, monkeypatch):
    settings = load_settings(_cli_config(tmp_path, enabled=False))
    monkeypatch.setenv("SELFTD_BINANCE_API_KEY", KEY)
    monkeypatch.setenv("SELFTD_BINANCE_API_SECRET", SECRET)
    transport = FakeTransport(cli_check_routes())

    report = key_health(settings, transport=transport)

    assert report["ok"] is False
    assert report["verdict"] == "not checked"
    assert report["enabled"] is False
    assert report["armed"] is False
    assert report["mode"] == "paper"
    assert report["host_is_testnet"] is True and report["production"] is False
    assert "disabled in config" in report["problems"][0]
    assert any("binance.enabled=true" in hint for hint in report["hints"])
    assert report["credentials"] == {
        "SELFTD_BINANCE_API_KEY": True,
        "SELFTD_BINANCE_API_SECRET": True,
    }
    assert report["steps"] == [] and report["open_orders"]["count"] == 0
    assert report["http"]["calls"] == 0 and report["http"]["posts"] == 0
    assert report["http"]["read_only"] is True
    assert report["checked_at"]
    assert transport.calls == []


def test_key_health_probes_the_account_read_only_and_unarmed(tmp_path, monkeypatch):
    settings = load_settings(_cli_config(tmp_path))
    monkeypatch.setenv("SELFTD_BINANCE_API_KEY", KEY)
    monkeypatch.setenv("SELFTD_BINANCE_API_SECRET", SECRET)
    transport = FakeTransport(cli_check_routes())

    report = key_health(settings, transport=transport)

    assert report["ok"] is True
    assert report["verdict"] in ("ok", "ok, with warnings")
    # the two properties that make this safe to run from a browser panel
    assert report["armed"] is False and report["mode"] == "paper"
    assert report["http"]["posts"] == 0 and report["http"]["read_only"] is True
    assert report["http"]["methods"] == ["GET"]
    assert _posts(transport) == [] and all(c["method"] == "GET" for c in transport.calls)
    # and it really read the account: clock, signature, permissions, balance
    assert report["signature"]["ok"] is True
    assert report["permissions"]["canTrade"] is True
    assert report["trade_ready"] is True
    assert report["account"]["balances"][0]["asset"] == "USDT"
    assert report["limits"]["max_weight_per_min"] == 1200
    assert SECRET not in json.dumps(report)


def test_key_health_without_credentials_touches_nothing(tmp_path, monkeypatch):
    settings = load_settings(_cli_config(tmp_path))
    monkeypatch.delenv("SELFTD_BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("SELFTD_BINANCE_API_SECRET", raising=False)
    transport = FakeTransport(cli_check_routes())

    report = key_health(settings, transport=transport)

    # The probe *is* built and it reports the missing credentials - as a verdict, not
    # as an exception, and without a single request leaving the process.
    assert report["ok"] is False and report["verdict"] == "problems found"
    assert "no credentials" in report["problems"][0]
    assert report["credentials"]["SELFTD_BINANCE_API_KEY"] is False
    assert [s["name"] for s in report["steps"]] == ["credentials", "read_only"]
    assert report["http"]["calls"] == 0 and report["http"]["posts"] == 0
    assert transport.calls == []


def test_key_health_reports_a_refused_host_instead_of_raising(tmp_path, monkeypatch):
    """A production URL is still refused here: the probe does not bypass the guard."""
    settings = load_settings(_cli_config(tmp_path))
    settings.binance.base_url = "https://api.binance.com/api"  # allow_production stays false
    monkeypatch.setenv("SELFTD_BINANCE_API_KEY", KEY)
    monkeypatch.setenv("SELFTD_BINANCE_API_SECRET", SECRET)
    transport = FakeTransport(cli_check_routes())

    report = key_health(settings, transport=transport)

    assert report["ok"] is False and report["verdict"] == "not checked"
    assert "holds real money" in report["problems"][0]
    assert report["production"] is True and report["host_is_testnet"] is False
    assert transport.calls == []
