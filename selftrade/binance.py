"""Binance **Spot Testnet**: the first adapter that speaks to a real API.

This module lives beside :mod:`selftrade.mt5` and *outside*
:mod:`selftrade.execution` on purpose: the execution package is the offline
contract and its simulation, and a test keeps it free of any network import.  The
adapters that do talk to a provider live here, with their transport injectable.

Every fact in this file comes from the official Binance Spot Testnet
documentation, and the ones that matter are quoted in
``docs/BINANCE-TESTNET.md``:

* base URL ``https://testnet.binance.vision/api``, paths ``/api/v3/...``; the
  testnet serves ``/api`` only, never ``/sapi``;
* authentication: header ``X-MBX-APIKEY`` plus a ``signature`` parameter, which
  is the **HMAC-SHA-256 hex digest** of the request's parameter string, with
  ``timestamp`` (milliseconds) and an optional ``recvWindow`` (default 5000 ms);
* ``GET /api/v3/time`` returns ``{"serverTime": <ms>}`` and is used to keep the
  clock honest; error ``-1021`` ("Timestamp for this request is outside of the
  recvWindow") triggers one re-sync and one retry, never a loop;
* errors come back as ``{"code": ..., "msg": "..."}``; rate-limit usage is
  reported in the ``X-MBX-USED-WEIGHT-...`` headers and a ``429`` carries
  ``Retry-After``;
* a stop loss on Spot is a **separate** conditional order (``stopPrice`` for
  ``STOP_LOSS``, plus ``price``/``quantity``/``timeInForce`` for
  ``STOP_LOSS_LIMIT``) - there is no documented way to attach one to the entry
  order - so this adapter does not pretend to offer one.

Four rules are enforced here rather than documented:

1. **Two keys to send.** Nothing is sent unless ``enabled`` (config) *and* the
   adapter was explicitly armed; without both, an order is built, signed, and
   recorded as a dry run with status ``dry_run``.  A dry run is not a refusal and
   not a fill, and the distinction is visible in the data.
2. **Testnet by default, production on purpose.** A base URL that is not the
   testnet host is refused at construction unless the config carries
   ``binance.allow_production: true`` *and* the host is a known Binance one.  The
   flag is a deliberate opt-in: it is the only way to reach a funded account, and
   it does not arm anything - sending still needs ``enabled`` + ``live`` +
   ``--live``.
2b. **Every real order passes the sealed gate.** When a ``risk_gate`` is wired in,
   an order that is about to be *sent* is checked against the sealed portfolio
   limits (5% per position, 2% daily halt, 15% drawdown), the configured exposure
   caps and the kill switch first; a refusal is recorded as a risk event and the
   order is never sent.  If the account cannot be read, nothing is sent - a gate
   that cannot see the book refuses rather than guesses.
3. **Secrets never leave the environment.** The key and secret are read from the
   variables named in the config and never appear in a recorded call, a log line
   or a ``repr``.
4. **Every call is recorded** - request, status, error code and message, in the
   ``api_calls`` table and in memory - because "Registrazione delle risposte e
   degli errori dell'API" is a requirement, not a nicety.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import urlencode

import pandas as pd
import requests

from .execution.base import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerError,
    Mismatch,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    utc_now_iso,
)
from .config import BINANCE_PRODUCTION_HOSTS
from .execution.risk_gate import ALERT_KIND

TESTNET_HOST = "testnet.binance.vision"
DEFAULT_BASE_URL = f"https://{TESTNET_HOST}/api"
#: Hosts that hold real money.  Reachable only with ``allow_production``, and
#: (for orders) only after the sealed gate and the kill switch have passed.
PRODUCTION_HOSTS = BINANCE_PRODUCTION_HOSTS

#: Binance's own spelling, kept so the response parser reads like the docs.
_REDACTED = "<hmac-sha256>"
_NOT_FOUND_CODES = (-2011, -2013)  # UNKNOWN_ORDER, NO_SUCH_ORDER

#: What Binance's own refusal means for a key that has just been exported, with
#: the next step a human can take.  Codes and messages are the official ones
#: (``errors.md``); this module repeats them, it does not invent them.
KEY_HINTS: dict[int, str] = {
    -2014: "the API key is malformed: check for a truncated or padded value",
    -2015: (
        "Binance rejected the key, its IP restriction or its permissions: make sure "
        "the key was created for THIS host (testnet and production are separate "
        "accounts), that its IP allow-list includes this machine, and that "
        "'Enable Reading' is on"
    ),
    -1022: "the signature was rejected: key and secret must come from the same key pair",
    -1021: "the timestamp fell outside recvWindow: the clock was re-synced, run the check again",
}

#: The signed read that proves a key can sign at all, and changes nothing.
ACCOUNT_CHECK_PATH = "/v3/account"

#: The protective stop of a filled entry.  Binance documents that ``STOP_LOSS_LIMIT``
#: needs ``timeInForce``, ``quantity``, ``price`` and ``stopPrice``, and that
#: ``STOP_LOSS``/``STOP_LOSS_LIMIT`` are the "algo" orders counted against
#: ``MAX_NUM_ALGO_ORDERS``.  The stop is a **SELL** on the spot base asset.
STOP_ORDER_TYPE = "STOP_LOSS_LIMIT"
STOP_CLIENT_ID_SUFFIX = "-stop"

#: Binance documents ``newClientOrderId`` as "A unique id among open orders ...
#: Orders with the same newClientOrderID can be accepted only when the previous one
#: is filled".  The derived stop id is therefore looked up before it is sent, and it is
#: kept short so the entry id plus the suffix stays a reasonable length.
STOP_CLIENT_ID_MAX = 36


class BinanceError(BrokerError):
    """An error answer from the API: ``{"code": ..., "msg": "..."}``."""

    def __init__(self, code: Any, msg: str, *, status: int | None = None, path: str = ""):
        self.code = int(code) if str(code).lstrip("-").isdigit() else code
        self.msg = str(msg)
        self.status = status
        self.path = path
        super().__init__(f"binance error {self.code}: {self.msg}" + (f" [{path}]" if path else ""))


class HostRefusedError(BrokerError):
    """Raised when a caller points this module at a host that holds real money."""


@dataclass
class HttpResponse:
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)
    latency_ms: float = 0.0


class RequestsTransport:
    """The real transport.  The only place in the project that sends an order."""

    def __init__(self, *, session: Any = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "selftrade-binance-testnet/0.1"})

    def __call__(
        self,
        method: str,
        url: str,
        params: dict[str, Any],
        body: str | None,
        headers: dict[str, str],
        timeout: float,
    ) -> HttpResponse:
        started = time.time()
        try:
            if method.upper() == "GET":
                resp = self.session.get(url, params=params, headers=headers, timeout=timeout)
            elif method.upper() == "POST":
                resp = self.session.post(
                    url,
                    params=params,
                    data=body,
                    headers=headers,
                    timeout=timeout,
                )
            else:
                resp = self.session.request(
                    method.upper(), url, params=params, data=body, headers=headers, timeout=timeout
                )
        except requests.RequestException as exc:
            raise BrokerError(f"network error on {url}: {exc}") from exc
        latency = (time.time() - started) * 1000.0
        try:
            parsed = resp.json()
        except ValueError:
            parsed = {"raw": resp.text[:500]}
        return HttpResponse(
            status=int(resp.status_code),
            body=parsed,
            headers={str(k): str(v) for k, v in resp.headers.items()},
            latency_ms=latency,
        )


@dataclass
class SymbolRules:
    """The ``exchangeInfo`` filters that a Spot order has to respect."""

    symbol: str
    base_asset: str = ""
    quote_asset: str = ""
    status: str = "UNKNOWN"
    min_qty: Decimal = Decimal("0")
    step_size: Decimal = Decimal("0")
    tick_size: Decimal = Decimal("0")
    min_price: Decimal = Decimal("0")
    """``PRICE_FILTER.minPrice`` - the filters docs say it governs ``price``/``stopPrice``."""

    max_price: Decimal = Decimal("0")
    """``PRICE_FILTER.maxPrice``."""

    min_notional: Decimal = Decimal("0")
    max_num_orders: int = 0
    """``MAX_NUM_ORDERS`` - normal **and** algo orders count towards it."""

    max_num_algo_orders: int = 0
    """``MAX_NUM_ALGO_ORDERS`` - the cap a resting stop eats into."""

    ask_multiplier_down: Decimal = Decimal("0")
    """``PERCENT_PRICE_BY_SIDE.askMultiplierDown``: the SELL side's lower bound."""

    ask_multiplier_up: Decimal = Decimal("0")
    order_types: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key in (
            "min_qty",
            "step_size",
            "tick_size",
            "min_price",
            "max_price",
            "min_notional",
            "ask_multiplier_down",
            "ask_multiplier_up",
        ):
            out[key] = str(out[key])
        return out


class BinanceTestnetBroker(BrokerAdapter):
    """A ``BrokerAdapter`` for the Spot Testnet, dry-run until explicitly armed.

    The paths below are **relative to the base URL**, which already ends in
    ``/api``: the documented endpoint ``GET /api/v3/time`` is reached as
    ``base_url`` + ``/v3/time``.
    """

    name = "binance-testnet"

    def __init__(
        self,
        *,
        config: Any = None,
        symbols: list[str] | None = None,
        transport: Callable[..., HttpResponse] | None = None,
        memory: Any = None,
        run_id: int | None = None,
        live: bool = False,
        base_url: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        now: Callable[[], float] | None = None,
        risk_gate: Any = None,
        kill_switch: Any = None,
    ):
        cfg = config
        self.enabled = bool(getattr(cfg, "enabled", False))
        self.base_url = str(
            base_url or getattr(cfg, "base_url", None) or DEFAULT_BASE_URL
        ).rstrip("/")
        self.allow_production = bool(getattr(cfg, "allow_production", False))
        self._check_host(self.base_url, self.allow_production)
        self.timeout = float(getattr(cfg, "timeout", 15.0))
        self.recv_window = int(getattr(cfg, "recv_window", 5000))
        self.quote_asset = str(getattr(cfg, "quote_asset", "USDT"))
        self.symbol_suffix = str(getattr(cfg, "symbol_suffix", "") or "")
        self.kline_bars = int(getattr(cfg, "kline_bars", 500))
        self.interval = str(getattr(cfg, "interval", "1h"))
        self.max_weight_per_min = int(getattr(cfg, "max_weight_per_min", 1200))
        self.allowed_symbols = [str(s) for s in (getattr(cfg, "allowed_symbols", None) or [])]
        self.symbols = [str(s) for s in (symbols or self.allowed_symbols)]
        if not self.symbols:
            raise BrokerError(
                "the binance adapter needs at least one symbol: add it to "
                "binance.allowed_symbols in config.yaml"
            )

        key_env = str(getattr(cfg, "api_key_env", "SELFTD_BINANCE_API_KEY"))
        secret_env = str(getattr(cfg, "api_secret_env", "SELFTD_BINANCE_API_SECRET"))
        self._key_env, self._secret_env = key_env, secret_env
        self._api_key = api_key if api_key is not None else os.environ.get(key_env, "")
        self._api_secret = api_secret if api_secret is not None else os.environ.get(secret_env, "")

        if live and not self.enabled:
            raise BrokerError(
                "refusing to arm the binance adapter: set `binance.enabled: true` in "
                "config.yaml first (two keys, like the MT5 rail)"
            )
        if live and not (self._api_key and self._api_secret):
            raise BrokerError(
                "refusing to arm the binance adapter without credentials: export "
                f"{key_env} and {secret_env}, never put them in the repository"
            )
        self._armed = bool(live)

        self.transport = transport or (RequestsTransport() if self.enabled else None)
        self.memory = memory
        self.run_id = run_id
        self.risk_gate = risk_gate
        self.kill_switch = kill_switch
        self._now = now or time.time
        self._time_offset_ms = 0.0
        self._synced = False
        self._weight_used = 0
        self._cooldown_until = 0.0
        self._exchange_info: dict[str, Any] | None = None
        self._rules: dict[str, SymbolRules] = {}
        self._orders: dict[str, Order] = {}
        #: entry client id -> the protective stop this adapter placed for it
        self._protections: dict[str, Order] = {}
        self._order_symbol: dict[str, str] = {}
        self._peak_equity: float | None = None
        self._day: Any = None
        self._day_start_equity: float | None = None
        self._account_cache: tuple[float, AccountSnapshot] | None = None
        self._calls: list[dict[str, Any]] = []
        self._last_order_at: float | None = None
        self.orders_submitted = 0

    # ------------------------------------------------------------- guard rails
    @staticmethod
    def _check_host(base_url: str, allow_production: bool = False) -> None:
        """Testnet, or a known production host explicitly opted into."""
        host = base_url.split("//", 1)[-1].split("/", 1)[0].lower()
        if host in PRODUCTION_HOSTS:
            if allow_production:
                return
            raise HostRefusedError(
                f"{host} holds real money: refusing it. Set `binance.allow_production: "
                "true` only if you really mean to trade a funded account - and expect "
                "the sealed risk gate and the kill switch on every order."
            )
        if TESTNET_HOST not in host:
            raise HostRefusedError(
                f"refusing {host!r}: the base URL must be the Spot Testnet host "
                f"({TESTNET_HOST}), or a known Binance host opted into with "
                "`binance.allow_production: true`."
            )

    @property
    def production(self) -> bool:
        host = self.base_url.split("//", 1)[-1].split("/", 1)[0].lower()
        return host in PRODUCTION_HOSTS

    @property
    def mode(self) -> str:  # type: ignore[override]
        """``paper`` until armed, then ``testnet`` or ``live``."""
        if not self._armed:
            return "paper"
        return "live" if self.production else "testnet"

    @property
    def armed(self) -> bool:
        return self._armed

    def arm(self) -> bool:
        """The second key, given in code.  Idempotent."""
        if not (self._api_key and self._api_secret):
            raise BrokerError(
                f"no credentials: export {self._key_env} and {self._secret_env} first"
            )
        if not self.enabled:
            raise BrokerError("binance.enabled is false: the first key is missing")
        self._armed = True
        return True

    def disarm(self) -> bool:
        self._armed = False
        return True

    # -------------------------------------------------------------- recording
    def _record(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, Any],
        response: HttpResponse | None,
        error: Any = None,
        dry_run: bool = False,
        note: str = "",
    ) -> dict[str, Any]:
        """Keep the request, the answer and the error - never the secret."""
        safe_params = {
            k: (_REDACTED if k == "signature" else v) for k, v in (params or {}).items()
        }
        body = None if response is None else response.body
        entry = {
            "ts": utc_now_iso(),
            "method": method,
            "path": path,
            "params": safe_params,
            "status": None if response is None else int(response.status),
            "latency_ms": None if response is None else round(float(response.latency_ms), 1),
            "weight": None if response is None else response.headers.get("X-MBX-USED-WEIGHT-1M"),
            "ok": bool(response is not None and 200 <= int(response.status) < 300 and error is None),
            "dry_run": bool(dry_run),
            "code": None,
            "msg": "",
            "response": _slim(body),
            "note": note,
        }
        if isinstance(error, BinanceError):
            entry["code"], entry["msg"] = error.code, error.msg
        elif error is not None:
            entry["code"], entry["msg"] = None, str(error)
        self._calls.append(entry)
        if self.memory is not None:
            try:
                self.memory.add_api_call(
                    self.run_id,
                    provider=self.name,
                    method=entry["method"],
                    path=entry["path"],
                    params=entry["params"],
                    status=entry["status"],
                    ok=entry["ok"],
                    code=entry["code"],
                    msg=entry["msg"],
                    dry_run=entry["dry_run"],
                    latency_ms=entry["latency_ms"],
                    response=entry["response"],
                    note=entry["note"],
                )
            except Exception:  # pragma: no cover - auditing must never break execution
                pass
        return entry

    def recorded_calls(self, limit: int = 50) -> list[dict[str, Any]]:
        """This process's API traffic, newest last."""
        return [dict(c) for c in self._calls[-int(limit) :]]

    def _alert(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        if self.memory is None:
            return
        try:
            self.memory.add_risk_event(self.run_id, ALERT_KIND, reason, detail)
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------- clock
    def sync_time(self) -> int:
        """``GET /api/v3/time`` - keep our clock inside ``recvWindow``."""
        local_ms = int(self._now() * 1000)
        payload, _ = self._http("GET", "/v3/time", {}, signed=False)
        server_ms = int(payload.get("serverTime", local_ms))
        self._time_offset_ms = float(server_ms - local_ms)
        self._synced = True
        return server_ms

    def _timestamp_ms(self) -> int:
        if not self._synced:
            self.sync_time()
        return int(self._now() * 1000 + self._time_offset_ms)

    # -------------------------------------------------------------------- HTTP
    def _http(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        signed: bool = True,
        dry_run: bool = False,
        retry_on_clock: bool = True,
        body_style: str = "query",
    ) -> tuple[Any, HttpResponse | None]:
        """Sign, record and send one call.  Returns ``(body, response)``."""
        if not self.enabled:
            raise BrokerError(
                "the binance adapter is disabled: set `binance.enabled: true` in config.yaml"
            )
        if self.transport is None:
            raise BrokerError("no transport configured for the binance adapter")
        remaining = self._cooldown_until - self._now()
        if remaining > 0:
            reason = (
                f"rate limit cooldown: {remaining:.0f}s left after a Binance 429/418 answer"
            )
            self._record(
                method=method,
                path=path,
                params=params,
                response=None,
                error=BrokerError(reason),
                note="cooldown",
            )
            raise BrokerError(reason)
        if self._weight_used >= self.max_weight_per_min:
            reason = (
                f"request weight budget reached ({self._weight_used}/"
                f"{self.max_weight_per_min} per minute reported by Binance)"
            )
            self._record(
                method=method,
                path=path,
                params=params,
                response=None,
                error=BrokerError(reason),
                note="weight",
            )
            raise BrokerError(reason)

        query = dict(params or {})
        if signed:
            if not (self._api_key and self._api_secret):
                raise BrokerError(
                    f"missing credentials: export {self._key_env} and {self._secret_env}"
                )
            query["timestamp"] = self._timestamp_ms()
            query.setdefault("recvWindow", self.recv_window)
            payload = urlencode(query)
            query["signature"] = hmac.new(
                self._api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
            ).hexdigest()

        headers = {"X-MBX-APIKEY": self._api_key} if self._api_key else {}
        url = f"{self.base_url}{path}"
        if dry_run:
            self._record(
                method=method,
                path=path,
                params=query,
                response=None,
                dry_run=True,
                note="dry run: signed but not sent",
            )
            return {}, None

        sent = dict(query)
        body = urlencode(sent) if (method.upper() in ("POST", "DELETE") and body_style == "body") else None
        if body is None:
            send_params: dict[str, Any] = sent
            send_body: str | None = None
        else:
            send_params = {}
            send_body = body
        try:
            response = self.transport(
                method.upper(), url, send_params, send_body, headers, self.timeout
            )
        except BrokerError as exc:
            self._record(method=method, path=path, params=query, response=None, error=exc)
            raise

        self._note_limits(response)

        if not 200 <= response.status < 300:
            error = _error_from(response, path)
            if (
                isinstance(error, BinanceError)
                and error.code == -1021
                and retry_on_clock
                and signed
            ):
                # The documented recovery: resync and try exactly once more.  The
                # rejected attempt stays in the record - a retry that hides the
                # first answer is not an audit trail.
                self._record(
                    method=method,
                    path=path,
                    params=query,
                    response=response,
                    error=error,
                    note="clock outside recvWindow: resynced and retried once",
                )
                self.sync_time()
                return self._http(
                    method,
                    path,
                    params,
                    signed=signed,
                    dry_run=dry_run,
                    retry_on_clock=False,
                    body_style=body_style,
                )
            if response.status == 429 or response.status == 418:
                retry_after = _retry_after(response)
                self._cooldown_until = self._now() + retry_after
                self._alert(
                    f"binance rate limit hit ({response.status}); paused for {retry_after:.0f}s",
                    {"path": path, "status": response.status},
                )
            self._record(method=method, path=path, params=query, response=response, error=error)
            raise error
        self._record(method=method, path=path, params=query, response=response)
        return response.body, response

    def _note_limits(self, response: HttpResponse) -> None:
        """Track the weight Binance reports in its own headers, and nothing else."""
        used = response.headers.get("X-MBX-USED-WEIGHT-1M")
        if used is not None and str(used).isdigit():
            self._weight_used = int(used)

    # ------------------------------------------------------------ market rules
    def exchange_info(self, symbol: str | None = None, *, refresh: bool = False) -> dict[str, Any]:
        """``GET /api/v3/exchangeInfo``, cached (weight 20, so it is fetched rarely)."""
        if self._exchange_info is None or refresh:
            params = {"symbol": self.symbol_for(symbol)} if symbol else {}
            body, _ = self._http("GET", "/v3/exchangeInfo", params, signed=False)
            self._exchange_info = body or {}
            self._rules.clear()
        return self._exchange_info

    def symbol_for(self, symbol: str) -> str:
        """``BTC/USDT`` -> ``BTCUSDT`` (+ any configured suffix)."""
        return str(symbol).replace("/", "").replace("-", "").upper() + self.symbol_suffix

    def rules_for(self, symbol: str) -> SymbolRules:
        """The ``LOT_SIZE`` / ``NOTIONAL`` / ``PRICE_FILTER`` numbers for one symbol."""
        binance_symbol = self.symbol_for(symbol)
        if binance_symbol in self._rules:
            return self._rules[binance_symbol]
        info = self.exchange_info(symbol)
        entry = _find_symbol(info, binance_symbol)
        if entry is None:
            raise BrokerError(f"{binance_symbol} is not listed on this exchange")
        rules = SymbolRules(
            symbol=binance_symbol,
            base_asset=str(entry.get("baseAsset", "")),
            quote_asset=str(entry.get("quoteAsset", "")),
            status=str(entry.get("status", "UNKNOWN")),
            order_types=tuple(str(t) for t in entry.get("orderTypes", [])),
        )
        for flt in entry.get("filters", []):
            kind = str(flt.get("filterType", ""))
            if kind == "LOT_SIZE":
                rules.min_qty = _dec(flt.get("minQty"))
                rules.step_size = _dec(flt.get("stepSize"))
            elif kind == "PRICE_FILTER":
                rules.tick_size = _dec(flt.get("tickSize"))
                rules.min_price = _dec(flt.get("minPrice"))
                rules.max_price = _dec(flt.get("maxPrice"))
            elif kind in ("MIN_NOTIONAL", "NOTIONAL"):
                rules.min_notional = _dec(flt.get("minNotional") or flt.get("notional"))
            elif kind == "MAX_NUM_ORDERS":
                rules.max_num_orders = int(flt.get("maxNumOrders") or 0)
            elif kind == "MAX_NUM_ALGO_ORDERS":
                rules.max_num_algo_orders = int(flt.get("maxNumAlgoOrders") or 0)
            elif kind == "PERCENT_PRICE_BY_SIDE":
                rules.ask_multiplier_down = _dec(flt.get("askMultiplierDown"))
                rules.ask_multiplier_up = _dec(flt.get("askMultiplierUp"))
        self._rules[binance_symbol] = rules
        return rules

    def _round_down(self, value: float, step: Decimal) -> Decimal:
        if step <= 0:
            return _dec(value)
        try:
            return (Decimal(str(value)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        except InvalidOperation:  # pragma: no cover - defensive
            return _dec(value)

    def _validate_against_filters(
        self,
        symbol: str,
        qty: float,
        price: float | None,
        order_type: OrderType = OrderType.MARKET,
    ) -> tuple[Decimal, str]:
        """Round to the documented step sizes and refuse what the exchange would."""
        rules = self.rules_for(symbol)
        if rules.status not in ("", "TRADING"):
            return Decimal("0"), f"{rules.symbol} is not trading (status {rules.status})"
        quantity = self._round_down(qty, rules.step_size)
        if quantity <= 0 or (rules.min_qty > 0 and quantity < rules.min_qty):
            return (
                Decimal("0"),
                f"{rules.symbol} needs at least {rules.min_qty} (step {rules.step_size}); "
                f"{qty} rounds to {quantity}",
            )
        if price and price > 0 and rules.min_notional > 0:
            notional = Decimal(str(price)) * quantity
            if notional < rules.min_notional:
                return (
                    Decimal("0"),
                    f"notional {notional:.8f} is under the {rules.min_notional} minimum "
                    f"for {rules.symbol}",
                )
        wanted = order_type.value.upper()
        if rules.order_types and wanted not in rules.order_types:
            return Decimal("0"), (
                f"{rules.symbol} does not offer {wanted} orders "
                f"(exchangeInfo lists {', '.join(rules.order_types)})"
            )
        return quantity, ""

    # ------------------------------------------------------------------- calls
    def _account(self) -> dict[str, Any]:
        """``GET /api/v3/account``.  Signing is mandatory; the answer is cached by
        the callers, not here, so a fresh read is always available on request."""
        body, _ = self._http("GET", "/v3/account", {}, signed=True)
        return body or {}

    def balances(self) -> dict[str, dict[str, float]]:
        """``{asset: {"free": x, "locked": y}}`` straight from ``/api/v3/account``."""
        account = self._account()
        out: dict[str, dict[str, float]] = {}
        for row in account.get("balances", []) or []:
            asset = str(row.get("asset", ""))
            free, locked = float(row.get("free", 0.0) or 0.0), float(row.get("locked", 0.0) or 0.0)
            if asset and (free or locked):
                out[asset] = {"free": free, "locked": locked}
        return out

    def last_price(self, symbol: str) -> float:
        """The last close of the newest kline - no extra endpoint, no invention."""
        df = self.get_market_data(symbol, limit=2)
        if df is None or len(df) == 0:
            raise BrokerError(f"no market data for {symbol}")
        return float(df["close"].iloc[-1])

    def get_market_data(self, symbol: str | None = None, limit: int = 100) -> pd.DataFrame:
        """``GET /api/v3/klines`` (public).  Testnet history is thin by design."""
        target = symbol or self.symbols[0]
        body, _ = self._http(
            "GET",
            "/v3/klines",
            {
                "symbol": self.symbol_for(target),
                "interval": self.interval,
                "limit": max(1, min(int(limit), 1000)),
            },
            signed=False,
        )
        rows = body or []
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        frame = pd.DataFrame(
            [
                {
                    "timestamp": pd.to_datetime(int(r[0]), unit="ms", utc=True),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]),
                }
                for r in rows
            ]
        ).set_index("timestamp")
        return frame

    def get_account_balance(self, refresh: bool = False) -> AccountSnapshot:
        """Cash, equity and drawdown from the real balances plus last prices.

        The drawdown is measured against the peak this adapter has itself seen
        since it started, not the account's all-time high: the API does not
        publish one, and inventing it would be worse than saying so.
        """
        if (
            not refresh
            and self._account_cache is not None
            and self._now() - self._account_cache[0] < 1.0
        ):
            return self._account_cache[1]
        snapshot = self._snapshot_from(self._account())
        self._account_cache = (self._now(), snapshot)
        return snapshot

    def _snapshot_from(self, account: dict[str, Any]) -> AccountSnapshot:
        """The equity maths, on a ``/v3/account`` payload that was already read.

        Split out of :meth:`get_account_balance` so that :meth:`account_check` can
        price the same answer without asking for the account twice: one read, one
        set of numbers.
        """
        held: dict[str, float] = {}
        for row in account.get("balances", []) or []:
            asset = str(row.get("asset", ""))
            qty = float(row.get("free", 0.0) or 0.0) + float(row.get("locked", 0.0) or 0.0)
            if asset and qty:
                held[asset] = qty
        cash = float(held.get(self.quote_asset, 0.0))
        positions_value = 0.0
        open_positions = 0
        for symbol in self.symbols:
            base = self.rules_for(symbol).base_asset
            qty = float(held.get(base, 0.0))
            if not qty:
                continue
            price = self.last_price(symbol)
            notional = qty * price
            if notional < float(self.rules_for(symbol).min_notional or 0):
                continue  # dust: the testnet hands out small balances of everything
            positions_value += notional
            open_positions += 1
        equity = cash + positions_value
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity
        peak = self._peak_equity or equity
        drawdown = max(0.0, 1.0 - equity / peak) if peak > 0 else 0.0
        day = pd.Timestamp(self._now(), unit="s", tz="UTC").date()
        if self._day != day or self._day_start_equity is None:
            self._day, self._day_start_equity = day, equity
        day_pnl = equity / self._day_start_equity - 1.0 if self._day_start_equity else 0.0
        return AccountSnapshot(
            ts=utc_now_iso(),
            cash=cash,
            positions_value=positions_value,
            equity=equity,
            realized_pnl=0.0,  # spot balances carry no cost basis; see positions
            unrealized_pnl=0.0,
            day_pnl_pct=day_pnl,
            drawdown=drawdown,
            open_positions=open_positions,
            fees_paid=0.0,
        )

    def _avg_cost(self, symbol: str, fallback: float) -> float:
        """Average buy price from ``GET /api/v3/myTrades``, falling back to the close.

        A spot balance carries no cost basis, so this is the **weighted average of
        the buys** (which lot a sell consumed is not knowable from the account).
        When there are no trades, or the answer is unusable, the last close is used
        and the caller can see it, because no invented number is better than a
        stated approximation.
        """
        try:
            trades = self.my_trades(symbol, limit=500)
        except BrokerError:
            return fallback
        bought_qty = bought_value = 0.0
        for t in trades:
            if not t.get("isBuyer"):
                continue
            qty, price = _f(t.get("qty")), _f(t.get("price"))
            bought_qty += qty
            bought_value += qty * price
        if bought_qty <= 0:
            return fallback
        return bought_value / bought_qty

    def get_positions(self) -> list[Position]:
        """Spot has balances, not positions: only the configured symbols are reported."""
        held = self.balances()
        out: list[Position] = []
        for symbol in self.symbols:
            rules = self.rules_for(symbol)
            qty = float(held.get(rules.base_asset, {}).get("free", 0.0)) + float(
                held.get(rules.base_asset, {}).get("locked", 0.0)
            )
            if not qty:
                continue
            price = self.last_price(symbol)
            if rules.min_notional > 0 and Decimal(str(qty)) * Decimal(str(price)) < rules.min_notional:
                continue
            out.append(
                Position(symbol, qty, self._avg_cost(symbol, price), utc_now_iso())
            )
        return out

    def my_trades(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        """``GET /api/v3/myTrades`` - requires a symbol, signed."""
        body, _ = self._http(
            "GET",
            "/v3/myTrades",
            {"symbol": self.symbol_for(symbol), "limit": max(1, min(int(limit), 1000))},
            signed=True,
        )
        return list(body or [])

    def get_trade_history(self, limit: int = 100) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for symbol in self.symbols:
            for t in self.my_trades(symbol, limit=limit):
                out.append(
                    {
                        "ts": _ms_to_iso(t.get("time")),
                        "order_id": str(t.get("orderId", "")),
                        "symbol": symbol,
                        "side": "buy" if t.get("isBuyer") else "sell",
                        "qty": float(t.get("qty", 0.0)),
                        "price": float(t.get("price", 0.0)),
                        "fee": float(t.get("commission", 0.0)),
                        "fee_asset": t.get("commissionAsset", ""),
                        "reason": "fill",
                    }
                )
        out.sort(key=lambda r: r["ts"])
        return out[-int(limit) :]

    def get_order_status(self, order_id: str, symbol: str | None = None) -> Order | None:
        target = symbol or self._order_symbol.get(order_id) or self._single_symbol()
        try:
            body, _ = self._http(
                "GET",
                "/v3/order",
                {"symbol": self.symbol_for(target), "orderId": order_id},
                signed=True,
            )
        except BinanceError as exc:
            if exc.code in _NOT_FOUND_CODES:
                return None
            raise
        return self._to_order(target, body or {})

    def open_orders(self, *, strict: bool = False) -> list[Order]:
        """Open orders on the configured symbols.

        ``strict`` re-raises instead of skipping a symbol whose read failed.  The
        tolerant default is what a trading loop wants; a check wants the failure,
        because "no open orders" and "the open orders could not be read" must not
        look the same.
        """
        out: list[Order] = []
        for symbol in self.symbols:
            try:
                body, _ = self._http(
                    "GET", "/v3/openOrders", {"symbol": self.symbol_for(symbol)}, signed=True
                )
            except BinanceError:
                if strict:
                    raise
                continue
            for row in body or []:
                out.append(self._to_order(symbol, row))
        return out

    # ------------------------------------------------------------------ check
    def account_check(self) -> dict[str, Any]:
        """Probe a real account **read-only** and report what the exchange answered.

        This is meant to be the first thing a freshly exported key touches: the
        clock (``GET /v3/time``), the signature (the signed ``GET /v3/account``),
        the permissions, the balance and the open orders.  There is no path from
        here to :meth:`submit_order`, nothing is armed, and the report carries how
        many ``POST`` requests the transport actually saw - so "it never sends an
        order" is checkable in the output rather than asserted in a docstring.

        Failures are *reported*, not raised: a wrong secret, a restricted key or an
        unreadable account all come back as a verdict, with the step that failed,
        the code Binance returned and, for the codes Binance documents, a hint
        naming the next step.
        """
        steps: list[dict[str, Any]] = []
        problems: list[str] = []
        warnings: list[str] = []
        hints: list[str] = []

        def step(name: str, ok: bool, detail: str) -> None:
            steps.append({"name": name, "ok": bool(ok), "detail": detail})

        credentials = {
            self._key_env: bool(self._api_key),
            self._secret_env: bool(self._api_secret),
        }
        report: dict[str, Any] = {
            "provider": self.name,
            "base_url": self.base_url,
            "host_is_testnet": TESTNET_HOST in self.base_url,
            "production": self.production,
            "mode": self.mode,
            "armed": self._armed,
            "credentials": credentials,
            "steps": steps,
            "problems": problems,
            "warnings": warnings,
            "hints": hints,
            "clock": {
                "server_time_ms": None,
                "local_time_ms": int(self._now() * 1000),
                "offset_ms": 0.0,
                "recv_window_ms": self.recv_window,
                "ok": False,
            },
            "signature": {"ok": False, "path": ACCOUNT_CHECK_PATH, "error": None},
            "permissions": {
                "accountType": None,
                "canTrade": None,
                "canWithdraw": None,
                "canDeposit": None,
                "permissions": [],
            },
            "trade_ready": None,
            "account": {
                "quote_asset": self.quote_asset,
                "cash": None,
                "positions_value": None,
                "equity": None,
                "drawdown": None,
                "day_pnl_pct": None,
                "open_positions": None,
                "balances": [],
                "balances_total": 0,
            },
            "open_orders": {"count": 0, "orders": []},
            "limits": {},
            "http": {},
        }

        account: dict[str, Any] | None = None
        missing = [name for name, present in credentials.items() if not present]
        if missing:
            problems.append("no credentials: export " + " and ".join(missing))
            hints.append(
                "set them for the process that runs the CLI (or for your user); never in "
                "config.yaml and never in the repository"
            )
            step("credentials", False, "missing " + ", ".join(missing))
        else:
            step(
                "credentials",
                True,
                "both environment variables are set (their values are never read back "
                "or printed)",
            )
            self._check_clock(report, step, problems)
            account = self._check_signature(report, step, problems, hints)
            if account is not None:
                self._check_permissions(report, account, step, warnings)
                self._check_balance(report, account, step, problems)
                self._check_open_orders(report, step, problems)

        posts = [c for c in self._calls if str(c.get("method", "")).upper() == "POST"]
        methods = sorted({str(c.get("method", "")).upper() for c in self._calls})
        report["http"] = {
            "calls": len(self._calls),
            "posts": len(posts),
            "methods": methods,
            "paths": sorted({str(c.get("path", "")) for c in self._calls}),
            "read_only": not posts,
        }
        step(
            "read_only",
            not posts,
            f"{len(self._calls)} call(s) this run "
            f"({', '.join(methods) if methods else 'none'}): {len(posts)} POST",
        )
        if posts:  # pragma: no cover - a read-only check must never get here
            problems.append(f"{len(posts)} POST request(s) were made by a read-only check")
        report["limits"] = {
            "weight_used": self._weight_used,
            "max_weight_per_min": self.max_weight_per_min,
            "cooldown_s": round(max(0.0, self._cooldown_until - self._now()), 1),
        }
        can_trade = report["permissions"]["canTrade"]
        # Unknown is not the same as false: a payload that does not say stays ``None``.
        report["trade_ready"] = (
            None if (account is None or can_trade is None) else bool(can_trade)
        )
        report["ok"] = not problems
        report["verdict"] = (
            "problems found" if problems else ("ok, with warnings" if warnings else "ok")
        )
        return report

    def _check_clock(
        self, report: dict[str, Any], step: Any, problems: list[str]
    ) -> None:
        """``GET /v3/time``: a clock outside ``recvWindow`` refuses every signature."""
        try:
            server_ms = self.sync_time()
            offset = float(self._time_offset_ms)
            ok = abs(offset) < float(self.recv_window)
            report["clock"].update(
                {"server_time_ms": int(server_ms), "offset_ms": round(offset, 1), "ok": ok}
            )
            step(
                "clock",
                ok,
                f"server time - local time = {offset:+.0f} ms "
                f"(recvWindow {self.recv_window} ms)",
            )
            if not ok:
                problems.append(
                    f"the local clock is {offset:+.0f} ms away from Binance, past the "
                    f"{self.recv_window} ms recvWindow: signed calls would be refused"
                )
        except BrokerError as exc:  # a check reports, it does not crash
            step("clock", False, str(exc))
            problems.append(f"server time unavailable: {exc}")

    def _check_signature(
        self,
        report: dict[str, Any],
        step: Any,
        problems: list[str],
        hints: list[str],
    ) -> dict[str, Any] | None:
        """The signed read that proves the key: ``GET /v3/account``, which changes nothing."""
        try:
            account = self._account()
            report["signature"].update({"ok": True, "error": None})
            step("signature", True, f"the signed GET {ACCOUNT_CHECK_PATH} was accepted")
            return account
        except BinanceError as exc:
            report["signature"]["error"] = {
                "code": exc.code,
                "msg": exc.msg,
                "status": exc.status,
            }
            step("signature", False, f"Binance refused the signed call: {exc.code} {exc.msg}")
            problems.append(f"the key was not accepted ({exc.code} {exc.msg})")
            hint = KEY_HINTS.get(exc.code)
            if hint and hint not in hints:
                hints.append(hint)
            return None
        except BrokerError as exc:
            step("signature", False, str(exc))
            problems.append(f"the account could not be read: {exc}")
            return None

    def _check_permissions(
        self,
        report: dict[str, Any],
        account: dict[str, Any],
        step: Any,
        warnings: list[str],
    ) -> None:
        """What the key is allowed to do, straight from the account payload."""
        perms = [str(p) for p in (account.get("permissions") or [])]
        info = report["permissions"]
        info.update(
            {
                "accountType": account.get("accountType"),
                "canTrade": account.get("canTrade"),
                "canWithdraw": account.get("canWithdraw"),
                "canDeposit": account.get("canDeposit"),
                "permissions": perms,
            }
        )
        step(
            "permissions",
            True,
            f"accountType={info['accountType']} canTrade={info['canTrade']} "
            f"canWithdraw={info['canWithdraw']} permissions={perms or 'not reported'}",
        )
        if info["canTrade"] is False:
            warnings.append(
                "the key cannot trade (canTrade=false): reading works, but Binance would "
                "refuse every order sent with it"
            )
        if info["canWithdraw"]:
            warnings.append(
                "the key can WITHDRAW (canWithdraw=true): revoke that permission on "
                "Binance - this project never needs it"
            )
        if perms and "SPOT" not in perms:
            warnings.append(f"the key's permissions are {perms}: this adapter speaks Spot only")

    def _check_balance(
        self,
        report: dict[str, Any],
        account: dict[str, Any],
        step: Any,
        problems: list[str],
    ) -> None:
        """The balance table and the equity it implies, from the payload already read."""
        rows, total = _balance_rows(account)
        try:
            snapshot = self._snapshot_from(account)
        except BrokerError as exc:
            # The raw balances are still worth reporting when the pricing cannot be
            # done (no ``exchangeInfo``, no klines): a check says what it could and
            # could not read, it does not throw.
            report["account"].update({"balances": rows, "balances_total": total})
            step("balance", False, f"the balance could not be priced: {exc}")
            problems.append(f"the balance could not be priced: {exc}")
            return
        report["account"].update(
            {
                "cash": round(snapshot.cash, 8),
                "positions_value": round(snapshot.positions_value, 8),
                "equity": round(snapshot.equity, 8),
                "drawdown": round(snapshot.drawdown, 6),
                "day_pnl_pct": round(snapshot.day_pnl_pct, 6),
                "open_positions": snapshot.open_positions,
                "balances": rows,
                "balances_total": total,
            }
        )
        step(
            "balance",
            True,
            f"equity {snapshot.equity:,.4f} {self.quote_asset} "
            f"(cash {snapshot.cash:,.2f} + positions {snapshot.positions_value:,.2f}), "
            f"drawdown {snapshot.drawdown * 100:.2f}%, {total} non-zero asset(s)",
        )

    def _check_open_orders(
        self, report: dict[str, Any], step: Any, problems: list[str]
    ) -> None:
        """What is already working at the exchange - read strictly, so a failure shows."""
        try:
            orders = self.open_orders(strict=True)
            report["open_orders"] = {
                "count": len(orders),
                "orders": [o.as_dict() for o in orders],
            }
            step(
                "open_orders",
                True,
                f"{len(orders)} open order(s) on {', '.join(self.symbols)}",
            )
        except BrokerError as exc:
            step("open_orders", False, str(exc))
            problems.append(f"open orders could not be read: {exc}")

    def _single_symbol(self) -> str:
        if len(self.symbols) == 1:
            return self.symbols[0]
        raise BrokerError(
            "cannot tell which symbol this order belongs to; pass symbol=... "
            "(this adapter was configured with " + ", ".join(self.symbols) + ")"
        )

    def _find_by_client_id(self, symbol: str, client_id: str) -> Order | None:
        """A retry must find the order, not place a second one."""
        try:
            body, _ = self._http(
                "GET",
                "/v3/order",
                {"symbol": self.symbol_for(symbol), "origClientOrderId": client_id},
                signed=True,
            )
        except BinanceError as exc:
            if exc.code in _NOT_FOUND_CODES:
                return None
            raise
        return self._to_order(symbol, body or {})

    def _to_order(self, symbol: str, row: dict[str, Any]) -> Order:
        status = {
            "NEW": OrderStatus.NEW,
            "PARTIALLY_FILLED": OrderStatus.NEW,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELED,
            "REJECTED": OrderStatus.REJECTED,
            "EXPIRED": OrderStatus.CANCELED,
            "EXPIRED_IN_MATCH": OrderStatus.CANCELED,
        }.get(str(row.get("status", "NEW")).upper(), OrderStatus.NEW)
        executed = _f(row.get("executedQty"))
        quote = _f(row.get("cummulativeQuoteQty"))
        avg = (quote / executed) if executed else None
        if avg is None:
            avg = _f(row.get("price")) or None
        order = Order(
            id=str(row.get("orderId", "")),
            client_id=str(row.get("clientOrderId", "")),
            symbol=symbol,
            side=Side.BUY if str(row.get("side", "")).upper() == "BUY" else Side.SELL,
            qty=_f(row.get("origQty")),
            type=_order_type(str(row.get("type", ""))),
            status=status,
            created_at=_ms_to_iso(row.get("time") or row.get("transactTime")),
            limit_price=_f(row.get("price")) or None,
            filled_qty=executed,
            avg_price=avg,
            updated_at=_ms_to_iso(row.get("updateTime")),
        )
        if order.id:
            self._orders[order.id] = order
            self._order_symbol[order.id] = symbol
        return order

    # ----------------------------------------------------------- the contract
    def submit_order(self, request: OrderRequest) -> Order:
        """Validate against the exchange filters, then send - if armed.

        Not armed: the order is built, signed and recorded as ``dry_run``.  The
        exchange is never asked to open a position in that mode.
        """
        symbol = str(request.symbol)
        if request.symbol not in self.symbols:
            return self._reject(
                request,
                f"{symbol} is not configured here (binance.allowed_symbols has "
                f"{', '.join(self.symbols)})",
            )
        if float(request.qty) <= 0:
            return self._reject(request, f"qty must be positive, got {request.qty}")
        if request.type is OrderType.LIMIT and not request.limit_price:
            return self._reject(request, "a limit order needs limit_price")
        plan = self._plan_refusal(request)
        if plan:
            return self._reject(request, plan)

        try:
            price = (
                float(request.limit_price)
                if request.type is OrderType.LIMIT
                else self.last_price(symbol)
            )
            quantity, why = self._validate_against_filters(
                symbol, request.qty, price, request.type
            )
        except BrokerError as exc:
            # ``exchangeInfo`` and a price are prerequisites: without them we cannot
            # know the step size or the notional, so nothing is guessed and nothing
            # is sent.  A market-data hiccup becomes a refusal with a reason, not a
            # crash halfway through an order.
            return self._reject(request, f"cannot price or validate this order: {exc}")
        if why:
            return self._reject(request, why)

        # The protective stop is validated **before** the entry goes out: a position
        # this rail cannot protect is a position it does not open.  What it cannot
        # promise in advance is the fill price, so the stop is recomputed on the real
        # average fill once the order is filled (see ``_protect_after_fill``).
        stop_price = Decimal("0")
        if request.stop_loss_pct is not None:
            stop_price, why = self._stop_plan(request, symbol, quantity, price)
            if why:
                return self._reject(
                    request,
                    f"the protective stop this order needs would be refused: {why}",
                )
        self.orders_submitted += 1

        try:
            existing = self._find_by_client_id(symbol, request.client_id)
        except BinanceError as exc:
            # Protection from duplicate orders needs the answer to be trustworthy:
            # if we cannot ask whether this client id already exists, we do not send.
            return self._reject(
                request,
                f"cannot check whether {request.client_id} was already sent ({exc.msg}), "
                "so nothing was sent",
            )
        if existing is not None:
            return existing

        # The sealed gate speaks for an order that is about to be *sent*: the
        # sealed limits, the configured exposure caps and the kill switch, with the
        # account as it is right now. A refusal is recorded and nothing goes out.
        if self._armed and self.risk_gate is not None:
            refusal = self._sealed_gate_refusal(request, price)
            if refusal:
                return self._reject(request, refusal)

        params: dict[str, Any] = {
            "symbol": self.symbol_for(symbol),
            "side": request.side.value.upper(),
            "type": request.type.value.upper(),
            "newClientOrderId": request.client_id,
            "newOrderRespType": "RESULT",
        }
        if request.type is OrderType.LIMIT:
            params["timeInForce"] = "GTC"
            params["quantity"] = str(quantity)
            params["price"] = str(self._round_down(price, self.rules_for(symbol).tick_size))
        else:
            params["quantity"] = str(quantity)

        if not self._armed:
            self._http("POST", "/v3/order", params, signed=True, dry_run=True)
            note = "dry run: signed and recorded, not sent"
            if request.stop_loss_pct is not None:
                # Both legs are signed and recorded, so a dry run shows the whole plan
                # (and covers the stop's signature) without anything reaching Binance.
                self._http(
                    "POST",
                    "/v3/order",
                    self._stop_params(
                        symbol, quantity, stop_price, self._stop_client_id(request.client_id)
                    ),
                    signed=True,
                    dry_run=True,
                )
                note = (
                    "dry run: entry + protective stop signed and recorded, not sent "
                    f"(the stop would be SELL {STOP_ORDER_TYPE} {quantity} @ {stop_price})"
                )
            order = Order(
                id=f"dry-{request.client_id}",
                client_id=request.client_id,
                symbol=symbol,
                side=request.side,
                qty=float(quantity),
                type=request.type,
                status=OrderStatus.DRY_RUN,
                created_at=utc_now_iso(),
                limit_price=request.limit_price,
                note=note,
            )
            self._orders[order.id] = order
            self._order_symbol[order.id] = symbol
            return order

        self._last_order_at = self._now()
        try:
            body, _ = self._http(
                "POST", "/v3/order", params, signed=True, body_style="body"
            )
        except BinanceError as exc:
            return self._reject(request, f"Binance refused it: {exc.msg} (code {exc.code})")
        order = self._to_order(symbol, body or {})
        order.note = request.note
        if request.stop_loss_pct is not None:
            order.note = self._protect_after_fill(
                symbol, order, float(request.stop_loss_pct), price
            )
        return order

    # -------------------------------------------------------- protective stop
    def _plan_refusal(self, request: OrderRequest) -> str:
        """What a plan this rail cannot honour is refused for, before anything is sent."""
        if request.take_profit_pct is not None or request.trailing_pct is not None:
            return (
                "a take profit and a trailing stop need an OCO (whose legs cancel each "
                "other) or a watcher that moves the order as the price moves; this "
                "adapter automates neither yet, so it will not pretend they are in place"
            )
        if request.stop_loss_pct is None:
            return ""
        if float(request.stop_loss_pct) >= 0:
            return (
                f"stop_loss_pct is {request.stop_loss_pct}: a long's stop sits *below* the "
                "entry, so the value has to be negative"
            )
        if request.side is not Side.BUY:
            return (
                "only a BUY can be protected this way: a spot stop is a SELL of the base "
                "asset, and a short side has no position to sell here"
            )
        return ""

    def _stop_price(self, reference: float, stop_loss_pct: float, symbol: str) -> Decimal:
        """The stop level the plan asks for, rounded **down** to the documented tick."""
        raw = float(reference) * (1.0 + float(stop_loss_pct))
        return self._round_down(raw, self.rules_for(symbol).tick_size)

    def _stop_plan(
        self, request: OrderRequest, symbol: str, quantity: Decimal, reference: float
    ) -> tuple[Decimal, str]:
        """The stop this entry needs, checked against the exchange before the entry.

        This is the difference between "the position will be protected" and "I hope it
        will": if the stop the plan implies is one Binance would refuse, the entry is
        not sent at all, so no position is ever opened without a place to stop out.
        """
        stop_price = self._stop_price(reference, float(request.stop_loss_pct), symbol)
        why = self._validate_stop(
            symbol, quantity=quantity, stop_price=stop_price, reference=reference
        )
        return stop_price, why

    def _validate_stop(
        self,
        symbol: str,
        *,
        quantity: Decimal,
        stop_price: Decimal,
        reference: float,
    ) -> str:
        """The documented rules a resting SELL stop-limit has to respect.

        Returns ``""`` when Binance should accept the order, or the reason it is
        refused.  Only documented rules are enforced here: the type has to be offered
        by the symbol, ``PRICE_FILTER`` governs ``price``/``stopPrice`` (tick, min and
        max), ``LOT_SIZE``/``NOTIONAL`` govern the size, and the SELL side's
        ``PERCENT_PRICE_BY_SIDE`` bounds the limit leg.  The documented reference for
        that last filter is a volume-weighted average over ``avgPriceMins`` minutes,
        which this process does not compute: it uses the fill price instead, which is
        the *stricter* reference, and only refuses what is clearly outside.
        """
        rules = self.rules_for(symbol)
        if STOP_ORDER_TYPE not in rules.order_types:
            return (
                f"{rules.symbol} does not offer {STOP_ORDER_TYPE} orders "
                f"(exchangeInfo lists {', '.join(rules.order_types)})"
            )
        if stop_price <= 0:
            return f"the stop price {stop_price} is not positive"
        if rules.min_price > 0 and stop_price < rules.min_price:
            return (
                f"the stop price {stop_price} is under the PRICE_FILTER minimum "
                f"{rules.min_price}"
            )
        if rules.max_price > 0 and stop_price > rules.max_price:
            return (
                f"the stop price {stop_price} is over the PRICE_FILTER maximum "
                f"{rules.max_price}"
            )
        if quantity <= 0 or (rules.min_qty > 0 and quantity < rules.min_qty):
            return (
                f"the stop size {quantity} is under the LOT_SIZE minimum {rules.min_qty} "
                f"(step {rules.step_size})"
            )
        if rules.min_notional > 0 and stop_price * quantity < rules.min_notional:
            return (
                f"the stop's notional {stop_price * quantity:.8f} is under the NOTIONAL "
                f"minimum {rules.min_notional}"
            )
        market = Decimal(str(reference))
        if rules.ask_multiplier_down > 0 and stop_price < market * rules.ask_multiplier_down:
            return (
                f"the stop {stop_price} is further below the market ({reference}) than the "
                f"documented askMultiplierDown {rules.ask_multiplier_down} allows"
            )
        if rules.ask_multiplier_up > 0 and stop_price > market * rules.ask_multiplier_up:
            return (
                f"the stop {stop_price} is further above the market ({reference}) than the "
                f"documented askMultiplierUp {rules.ask_multiplier_up} allows"
            )
        return ""

    @staticmethod
    def _stop_client_id(client_id: str) -> str:
        """``<entry>-stop``: deterministic, so a retry finds the stop instead of stacking one.

        Binance documents ``newClientOrderId`` as "A unique id among open orders ...
        Orders with the same newClientOrderID can be accepted only when the previous
        one is filled": the adapter does not lean on that, it looks the id up before
        sending.  No maximum length is documented, so the derived id is kept short
        anyway (the entry id is cut to leave room for the suffix).
        """
        room = max(1, STOP_CLIENT_ID_MAX - len(STOP_CLIENT_ID_SUFFIX))
        return f"{str(client_id)[:room]}{STOP_CLIENT_ID_SUFFIX}"

    def _stop_params(
        self, symbol: str, quantity: Decimal, stop_price: Decimal, client_id: str
    ) -> dict[str, Any]:
        """The documented parameters of a resting SELL ``STOP_LOSS_LIMIT``.

        ``newOrderRespType=RESULT`` is explicit: the docs say a non-``MARKET``/``LIMIT``
        order defaults to ``ACK``, which carries no status, price or quantity back -
        and this adapter wants the exchange's own answer for the stop, not a guess.
        The limit leg sits **at** the stop price, which bounds the worst fill; a
        gap that skips the level can leave the limit unfilled, and that trade-off is
        the documented difference between ``STOP_LOSS`` (market) and this type.
        """
        return {
            "symbol": self.symbol_for(symbol),
            "side": Side.SELL.value.upper(),
            "type": STOP_ORDER_TYPE,
            "timeInForce": "GTC",
            "quantity": str(quantity),
            "price": str(stop_price),
            "stopPrice": str(stop_price),
            "newClientOrderId": client_id,
            "newOrderRespType": "RESULT",
        }

    def _algo_budget(self, symbol: str) -> str:
        """``MAX_NUM_ALGO_ORDERS``: a resting stop is an "algo" order and the cap is real.

        Returns ``""`` when there is room for one more, or the reason there is not.
        The count comes from the exchange's own open orders, never from a local guess.
        """
        rules = self.rules_for(symbol)
        if not (rules.max_num_algo_orders or rules.max_num_orders):
            return ""
        try:
            open_orders = self.open_orders(strict=True)
        except BrokerError as exc:
            return f"the open orders could not be counted before adding a stop ({exc})"
        same_symbol = [o for o in open_orders if o.symbol == symbol]
        resting = [
            o for o in same_symbol if o.type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_LIMIT)
        ]
        if rules.max_num_algo_orders and len(resting) >= rules.max_num_algo_orders:
            return (
                f"{rules.symbol} already carries {len(resting)} resting algo order(s) and "
                f"the documented MAX_NUM_ALGO_ORDERS cap is {rules.max_num_algo_orders}"
            )
        if rules.max_num_orders and len(same_symbol) >= rules.max_num_orders:
            return (
                f"{rules.symbol} already carries {len(same_symbol)} open order(s) and the "
                f"documented MAX_NUM_ORDERS cap is {rules.max_num_orders}"
            )
        return ""

    def _place_stop(
        self, symbol: str, quantity: Decimal, stop_price: Decimal, client_id: str
    ) -> tuple[Order | None, str]:
        """Send the resting stop.  Returns ``(order, "")`` or ``(None, why)``."""
        try:
            body, _ = self._http(
                "POST",
                "/v3/order",
                self._stop_params(symbol, quantity, stop_price, client_id),
                signed=True,
                body_style="body",
            )
        except BrokerError as exc:
            return None, str(exc)
        order = self._to_order(symbol, body or {})
        order.note = f"protective stop: SELL {STOP_ORDER_TYPE} @ {stop_price}"
        return order, ""

    def _unprotected(self, entry: Order, why: str) -> BrokerError:
        """A filled entry that could not be protected: alerted, recorded, never silent."""
        self._alert(
            f"order {entry.client_id} is UNPROTECTED: {why}",
            {
                "order_id": entry.id,
                "symbol": entry.symbol,
                "filled_qty": float(entry.filled_qty),
                "provider": self.name,
            },
        )
        return BrokerError(
            f"the entry filled but its protective stop could not be placed: {why}. "
            "The position is open and unprotected"
        )

    def _attach_stop(
        self,
        symbol: str,
        entry: Order,
        stop_loss_pct: float,
        reference: float,
        filled: float,
    ) -> Order:
        """Place (or find) the protective stop of a filled entry, or raise why not.

        The size comes from the **free** balance rather than from the filled quantity:
        Binance takes its commission out of the received asset, so the account holds a
        little less than ``executedQty`` and a stop for the full size would be refused
        for insufficient balance.
        """
        if not self._armed:
            raise BrokerError("the adapter is not armed: a stop cannot be placed in a dry run")
        stop_cid = self._stop_client_id(entry.client_id)
        existing = self._find_by_client_id(symbol, stop_cid)
        if existing is not None:
            # Already placed (resting, filled or cancelled): saying so is the whole
            # point of a deterministic id - a second stop on the same position would
            # sell it twice.
            existing.note = existing.note or f"protective stop for {entry.client_id}"
            self._protections[entry.client_id] = existing
            return existing
        rules = self.rules_for(symbol)
        free = float((self.balances().get(rules.base_asset) or {}).get("free", 0.0))
        quantity = self._round_down(min(float(filled), free), rules.step_size)
        stop_price = self._stop_price(reference, stop_loss_pct, symbol)
        why = self._validate_stop(
            symbol, quantity=quantity, stop_price=stop_price, reference=reference
        )
        if not why:
            why = self._algo_budget(symbol)
        if why:
            raise self._unprotected(entry, why)
        order, error = self._place_stop(symbol, quantity, stop_price, stop_cid)
        if order is None:
            raise self._unprotected(entry, error)
        self._protections[entry.client_id] = order
        return order

    def _protect_after_fill(
        self, symbol: str, entry: Order, stop_loss_pct: float, reference: float
    ) -> str:
        """Place the stop the plan asked for, right after the entry filled.

        Returns the note of the entry order.  A position that filled and could not be
        protected is not a silent detail: the alert is recorded and the note says
        ``UNPROTECTED`` so a caller sees it without reading a log.  The stop level is
        recomputed from the **average fill price**, because that is where the risk
        actually started, not from the reference the entry was priced with.
        """
        note = entry.note
        if entry.filled_qty <= 0:
            return " ".join(part for part in (note, "nothing filled: no position to protect") if part)
        try:
            stop = self._attach_stop(
                symbol, entry, stop_loss_pct, float(entry.avg_price or reference), entry.filled_qty
            )
        except BrokerError as exc:
            return " ".join(part for part in (note, f"UNPROTECTED: {exc}") if part)
        return " ".join(
            part
            for part in (
                note,
                f"protected: SELL {STOP_ORDER_TYPE} {stop.qty} @ {stop.limit_price} "
                f"(client {stop.client_id}, id {stop.id}, status {stop.status.value})",
            )
            if part
        )

    def protect(
        self,
        client_id: str,
        stop_loss_pct: float,
        *,
        symbol: str | None = None,
        price: float | None = None,
    ) -> Order:
        """Place the protective stop for an entry that is already filled.

        This is the way back from an ``UNPROTECTED`` note, and the way to protect a
        position this rail did not open itself.  It raises ``BrokerError`` when the
        stop cannot be placed, so a caller can never mistake a refusal for protection.
        """
        target = symbol or self._single_symbol()
        entry = self._find_by_client_id(target, client_id)
        if entry is None:
            raise BrokerError(
                f"no order with client id {client_id} on {target}: there is nothing to protect"
            )
        if entry.side is not Side.BUY:
            raise BrokerError(
                f"order {client_id} is a {entry.side.value.upper()}: a spot stop protects a "
                "long position only"
            )
        if float(entry.filled_qty) <= 0:
            raise BrokerError(
                f"order {client_id} has filled {entry.filled_qty}: there is no position to protect"
            )
        reference = float(price or entry.avg_price or self.last_price(target))
        return self._attach_stop(
            target, entry, float(stop_loss_pct), reference, float(entry.filled_qty)
        )

    def protection_of(
        self, entry_client_id: str, *, symbol: str | None = None
    ) -> Order | None:
        """The stop guarding an entry, as the exchange sees it right now.

        Memory first, then the exchange: the stop's client id is derived from the
        entry's, so a stop placed by another process (or by an earlier run of this one)
        is still findable.  That matters because ``protect`` in one terminal and
        ``unprotect`` in another are two processes, not one session.
        """
        stop = self._protections.get(entry_client_id)
        if stop is not None:
            if not self._armed:
                return stop
            return self._find_by_client_id(stop.symbol, stop.client_id) or stop
        target = symbol
        if target is None:
            try:
                target = self._single_symbol()
            except BrokerError:
                return None
        return self._find_by_client_id(target, self._stop_client_id(entry_client_id))

    def cancel_protection(
        self, entry_client_id: str, *, symbol: str | None = None
    ) -> Order | None:
        """Cancel the stop of an entry whose position is being closed by hand.

        ``None`` means there is no stop to cancel; anything else that goes wrong is
        raised, so "no protection" and "could not ask" stay distinguishable.
        """
        stop = self.protection_of(entry_client_id, symbol=symbol)
        if stop is None:
            return None
        if not self._armed:
            raise BrokerError(
                "refusing to cancel on the exchange: the adapter is not armed (dry run)"
            )
        if stop.status is not OrderStatus.NEW:
            return stop  # it already fired or was cancelled: nothing to undo
        return self.cancel_order(stop.id)

    def _sealed_gate_refusal(self, request: OrderRequest, price: float) -> str:
        """The sealed pre-trade gate for an order that is about to be sent.

        Returns ``""`` when the order may go out, or the reason it may not.  If the
        account cannot be read the answer is a refusal: a gate that cannot see the
        book must not guess.
        """
        try:
            account = self.get_account_balance(refresh=True)
            positions = self.get_positions()
        except Exception as exc:  # a read that fails is a refusal, not a crash
            reason = f"cannot read the account for the sealed risk gate ({exc})"
            self._alert(f"order {request.client_id} refused: {reason}", {"error": str(exc)[:200]})
            return f"{reason}, so nothing was sent"
        try:
            decision = self.risk_gate.check(
                request,
                account,
                positions,
                price=float(price) if price else None,
                last_order_at=self._last_order_at,
                now=self._now(),
                kill_switch=self.kill_switch,
            )
        except Exception as exc:  # a gate that breaks refuses
            reason = f"the sealed risk gate could not decide ({exc})"
            self._alert(f"order {request.client_id} refused: {reason}", {"error": str(exc)[:200]})
            return f"{reason}, so nothing was sent"
        if decision.ok:
            return ""
        reason = "; ".join(decision.reasons) or "refused without a reason"
        self._alert(
            f"order {request.client_id} refused by the sealed risk gate: {reason}",
            {"checks": decision.checks, "alerts": decision.alerts},
        )
        return f"the sealed risk gate refused it: {reason}"

    def _reject(self, request: OrderRequest, reason: str) -> Order:
        order = Order(
            id=f"rejected-{request.client_id}",
            client_id=request.client_id,
            symbol=str(request.symbol),
            side=request.side,
            qty=float(request.qty),
            type=request.type,
            status=OrderStatus.REJECTED,
            created_at=utc_now_iso(),
            limit_price=request.limit_price,
            reject_reason=reason,
            note=request.note,
        )
        self._orders[order.id] = order
        self._alert(reason, {"request": order.as_dict(), "provider": self.name})
        return order

    def cancel_order(self, order_id: str) -> Order:
        if not self._armed:
            raise BrokerError(
                "refusing to cancel on the exchange: the adapter is not armed (dry run)"
            )
        symbol = self._order_symbol.get(order_id) or self._single_symbol()
        body, _ = self._http(
            "DELETE",
            "/v3/order",
            {"symbol": self.symbol_for(symbol), "orderId": order_id},
            signed=True,
            body_style="body",
        )
        return self._to_order(symbol, body or {})

    # ------------------------------------------------------------- integrity
    def reconcile(self) -> list[Mismatch]:
        """Ask the exchange about our own orders and report what disagrees."""
        out: list[Mismatch] = []
        for order_id, local in self._orders.items():
            if order_id.startswith(("dry-", "rejected-")):
                continue
            symbol = self._order_symbol.get(order_id)
            if not symbol:
                continue
            try:
                remote = self.get_order_status(order_id, symbol=symbol)
            except BrokerError as exc:
                out.append(Mismatch("order_lookup", local.status.value, "error", str(exc)))
                continue
            if remote is None:
                out.append(
                    Mismatch(
                        "order_status",
                        local.status.value,
                        "not found",
                        f"order {order_id} is not on the exchange",
                    )
                )
            elif remote.status is not local.status:
                out.append(
                    Mismatch(
                        "order_status",
                        local.status.value,
                        remote.status.value,
                        f"order {order_id} moved on the exchange",
                    )
                )
        return out

    def describe(self) -> dict[str, Any]:
        out = super().describe()
        out.update(
            {
                "base_url": self.base_url,
                "enabled": self.enabled,
                "armed": self._armed,
                "host_is_testnet": TESTNET_HOST in self.base_url,
                "production": self.production,
                "allow_production": self.allow_production,
                "risk_gate": self.risk_gate is not None,
                "kill_switch": self.kill_switch is not None,
                "symbols": list(self.symbols),
                "quote_asset": self.quote_asset,
                "interval": self.interval,
                "kline_bars": self.kline_bars,
                "mode_note": (
                    (
                        "orders are sent to the PRODUCTION host (real money)"
                        if self.production
                        else "orders are sent to the Spot Testnet"
                    )
                    if self._armed
                    else "dry run: requests are signed and recorded, never sent"
                ),
                "credentials": {
                    self._key_env: bool(self._api_key),
                    self._secret_env: bool(self._api_secret),
                },
                "weight_used": self._weight_used,
                "max_weight_per_min": self.max_weight_per_min,
                "cooldown_s": round(max(0.0, self._cooldown_until - self._now()), 1),
                "calls_recorded": len(self._calls),
                "fill_rule": "whatever the exchange reports (executedQty, cummulativeQuoteQty)",
                "protective_stop": (
                    f"SELL {STOP_ORDER_TYPE} at the plan's stop price, placed by the rail "
                    "right after a fill; no OCO, so a take profit or a trailing stop is "
                    "refused instead of faked"
                ),
            }
        )
        return out


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# the read-only key probe, shared by ``binance check`` and the dashboard
# --------------------------------------------------------------------------


def key_health(
    settings: Any,
    *,
    memory: Any = None,
    symbols: list[str] | None = None,
    transport: Callable[..., HttpResponse] | None = None,
) -> dict[str, Any]:
    """The ``binance check`` report for a whole settings object, and nothing else.

    The broker is built **unarmed**, so no order can come out of this path, and the
    same report is what the CLI prints and what the dashboard panel renders.  A rail
    that is switched off - or a host the config refuses - comes back with
    ``verdict: "not checked"`` and the reason, in the same shape as a real probe, so
    a caller can render either case without guessing.

    ``transport`` is injectable for the same reason it is everywhere else in this
    module: a test has to be able to exercise the whole report without a network.
    """
    cfg = getattr(settings, "binance", settings)
    base_url = str(getattr(cfg, "base_url", None) or DEFAULT_BASE_URL)
    host = base_url.split("//", 1)[-1].split("/", 1)[0].lower()
    key_env = str(getattr(cfg, "api_key_env", "SELFTD_BINANCE_API_KEY"))
    secret_env = str(getattr(cfg, "api_secret_env", "SELFTD_BINANCE_API_SECRET"))
    report: dict[str, Any] = {
        "provider": "binance-testnet",
        "base_url": base_url,
        "enabled": bool(getattr(cfg, "enabled", False)),
        "host_is_testnet": TESTNET_HOST in base_url,
        "production": host in PRODUCTION_HOSTS,
        "mode": "paper",
        "armed": False,  # a probe never arms anything
        "credentials": {
            key_env: bool(os.environ.get(key_env)),
            secret_env: bool(os.environ.get(secret_env)),
        },
        "steps": [],
        "problems": [],
        "warnings": [],
        "hints": [],
        "clock": {},
        "signature": {},
        "permissions": {},
        "trade_ready": None,
        "account": {"balances": [], "balances_total": 0},
        "open_orders": {"count": 0, "orders": []},
        "limits": {},
        # No broker was ever built, so this is not a claim about a probe: it is the
        # count of requests this report cost, and it is zero either way.
        "http": {"calls": 0, "posts": 0, "methods": [], "paths": [], "read_only": True},
        "checked_at": utc_now_iso(),
    }
    if not report["enabled"]:
        report.update(
            ok=False,
            verdict="not checked",
            problems=["disabled in config: binance.enabled=false, so nothing was sent"],
            hints=[
                "set binance.enabled=true (or pass --set binance.enabled=true) to probe the account"
            ],
        )
        return report
    wanted = [str(s) for s in (symbols or getattr(cfg, "allowed_symbols", None) or [])]
    if not wanted:
        wanted = [str(getattr(cfg, "symbol", "BTC/USDT"))]
    try:
        broker = BinanceTestnetBroker(
            config=cfg, symbols=wanted, transport=transport, memory=memory, live=False
        )
    except BrokerError as exc:
        report.update(ok=False, verdict="not checked", problems=[str(exc)])
        return report
    checked = broker.account_check()
    # ``enabled`` is a property of the rail, and this report was only reached because
    # it is on; the failure reports carry their own value, so both shapes agree.
    checked.setdefault("enabled", True)
    checked["checked_at"] = utc_now_iso()
    return checked


def _order_type(value: str) -> OrderType:
    """Binance's type string as the shared enum knows it: a stop stays a stop.

    Only the types this rail can meet are modelled; take-profit legs (which belong
    to an OCO this adapter does not place) keep the old neutral mapping.
    """
    kind = str(value).upper()
    if kind == "LIMIT":
        return OrderType.LIMIT
    if kind == "STOP_LOSS_LIMIT":
        return OrderType.STOP_LOSS_LIMIT
    if kind == "STOP_LOSS":
        return OrderType.STOP_LOSS
    return OrderType.MARKET


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation:  # pragma: no cover - defensive
        return Decimal("0")


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _balance_rows(
    account: dict[str, Any], limit: int = 25
) -> tuple[list[dict[str, Any]], int]:
    """The non-zero balances, largest first, plus how many there were."""
    rows: list[dict[str, Any]] = []
    for row in account.get("balances", []) or []:
        asset = str(row.get("asset", ""))
        free, locked = _f(row.get("free")), _f(row.get("locked"))
        if asset and (free or locked):
            rows.append({"asset": asset, "free": free, "locked": locked})
    rows.sort(key=lambda r: (r["free"] + r["locked"]), reverse=True)
    return rows[:limit], len(rows)


def _ms_to_iso(value: Any) -> str:
    if value in (None, "", 0):
        return utc_now_iso()
    try:
        return pd.Timestamp(int(value), unit="ms", tz="UTC").isoformat()
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return utc_now_iso()


def _retry_after(response: HttpResponse) -> float:
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after") or "60"
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 60.0


def _slim(body: Any, limit: int = 800) -> Any:
    """Keep a recording small enough for a database row."""
    if body is None:
        return None
    if isinstance(body, dict):
        out: dict[str, Any] = {}
        for k, v in list(body.items())[:20]:
            out[str(k)] = v if isinstance(v, (int, float, bool, type(None))) else _slim(v, limit // 2)
        return out
    if isinstance(body, list):
        return [_slim(v, limit // 2) for v in body[:5]] + ([f"... {len(body)} rows"] if len(body) > 5 else [])
    text = str(body)
    return text[:limit]


def _find_symbol(info: dict[str, Any], binance_symbol: str) -> dict[str, Any] | None:
    for entry in info.get("symbols", []) or []:
        if str(entry.get("symbol", "")).upper() == binance_symbol.upper():
            return entry
    return None


def _error_from(response: HttpResponse, path: str) -> BrokerError:
    body = response.body if isinstance(response.body, dict) else {}
    if "code" in body:
        return BinanceError(body.get("code"), body.get("msg", ""), status=response.status, path=path)
    return BrokerError(
        f"HTTP {response.status} from {path}: {str(response.body)[:200]}"
    )
