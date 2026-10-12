"""Typed, validated configuration.

Every value in ``config.yaml`` maps to a dataclass field.  A key that is not
known (e.g. a typo like ``backtest.fee_bp``) **stops** execution instead of
silently changing the model.

Every value can be overridden from the CLI with ``--set section.key=value``.
"""

from __future__ import annotations

import dataclasses
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when the configuration is missing, malformed or unknown."""


# --------------------------------------------------------------------------
# Section dataclasses
# --------------------------------------------------------------------------


@dataclass
class DataConfig:
    source: str = "csv"  # csv | freqtrade_json | synthetic | mt5
    csv: str = "data/sample_ohlcv.csv"
    datetime_col: str = "timestamp"
    timeframe: str = "1h"
    symbol: str = "BTC/USDT"
    min_rows: int = 800
    synthetic_rows: int = 12000
    max_rows: int = 0
    fetch_base_url: str = "https://data-api.binance.vision"
    fetch_timeout: float = 20.0


@dataclass
class BacktestConfig:
    initial_cash: float = 10000.0
    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    position_size: float = 0.95
    allow_short: bool = False


@dataclass
class EvaluationConfig:
    folds: int = 4
    train_frac: float = 0.6
    holdout_frac: float = 0.2
    min_trades: int = 8
    max_drawdown: float = 0.60
    metric: str = "sharpe"  # sharpe | sortino | return | calmar
    complexity_penalty: float = 0.01


@dataclass
class EvolutionConfig:
    generations: int = 5
    population: int = 6
    elite: int = 2
    seed: int = 7
    mode: str = "explore"  # params | explore
    mutation_rate: float = 0.35
    crossover: bool = True


@dataclass
class LLMConfig:
    enabled: bool = True
    base_url: str = "http://localhost:11434"
    model: str = "qwen3-coder:30b"
    embed_model: str = "embeddinggemma"
    num_ctx: int = 8192
    num_predict: int = 512
    temperature: float = 0.2
    timeout: float = 180.0


@dataclass
class ReaConfig:
    enabled: bool = False
    dry_run: bool = True
    node_cmd: str = "npx"
    package: str = "rea-agents@latest"
    provider: str = "ghidra"
    ghidra_dir: str = r"C:\tools\ghidra_12.1.4_PUBLIC"
    java_home: str = r"C:\tools\jdk-21"
    targets: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MT5Config:
    enabled: bool = False
    live: bool = False
    require_demo: bool = True
    require_stop: bool = True
    terminal_path: str = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    login: int = 0
    password_env: str = "SELFTD_MT5_PASSWORD"
    server: str = ""
    symbol: str = "EURUSD"
    suffix: str = ""
    timeframe: str = "1h"
    bars: int = 20000
    deviation: int = 20
    magic: int = 20261009
    max_lots: float = 0.50
    max_positions: int = 1
    risk_pct: float = 0.005
    fixed_lots: float = 0.01
    sl_fallback_points: int = 500
    max_spread_points: int = 25
    allowed_symbols: list[str] = field(default_factory=list)


#: Binance hosts that hold real money.  Reachable only with
#: ``binance.allow_production: true`` and, for orders, the two-key arming gate.
BINANCE_PRODUCTION_HOSTS = (
    "api.binance.com",
    "api1.binance.com",
    "api2.binance.com",
    "api3.binance.com",
    "api4.binance.com",
    "api-gcp.binance.com",
    "api.us.binance.com",
    "api.binance.us",
    "data-api.binance.vision",
)


@dataclass
class BinanceConfig:
    """The Binance rail.  **Testnet by default**; a production host is opt-in.

    ``live`` is the first key of the two-key gate (``mt5.py`` uses the same
    pattern): nothing is sent to the exchange unless ``enabled`` and ``live`` are
    both true *and* the CLI passes ``--live``.  Credentials are read from the
    environment variables named here - never from this file, never from the code.
    """

    enabled: bool = False
    live: bool = False
    """ARMS order sending (the second of the two keys, with ``--live``)."""

    allow_production: bool = False
    """Opt in to a **funded** Binance host.  Off by default: a production
    ``base_url`` without this flag is a config error.  This flag on its own does
    not arm anything - ``enabled`` + ``live`` + ``--live`` still gate every order,
    and every order still passes the sealed risk gate and the kill switch."""

    base_url: str = "https://testnet.binance.vision/api"
    api_key_env: str = "SELFTD_BINANCE_API_KEY"
    api_secret_env: str = "SELFTD_BINANCE_API_SECRET"
    symbol: str = "BTC/USDT"
    allowed_symbols: list[str] = field(default_factory=list)
    quote_asset: str = "USDT"
    symbol_suffix: str = ""
    interval: str = "1h"
    kline_bars: int = 500
    recv_window: int = 5000
    timeout: float = 15.0
    max_weight_per_min: int = 1200


@dataclass
class RiskGateConfig:
    """The pre-trade gate's *own* thresholds.

    The portfolio-wide limits (5% per position, 2% daily halt, 15% drawdown) are
    **not** here on purpose: they live in the sealed, read-only
    :mod:`selftrade.risk_rules` layer and no config key can reach them.
    """

    enabled: bool = True
    risk_pct: float = 0.005
    """Share of equity a single trade may risk if its stop is hit."""

    max_positions: int = 1
    max_leverage: float = 1.0
    """Gross exposure as a multiple of equity."""

    min_seconds_between_orders: float = 0.0
    min_notional: float = 1.0
    """Minimum order notional: a stand-in for a liquidity floor."""

    require_stop: bool = True
    allowed_symbols: list[str] = field(default_factory=list)
    """Empty means every symbol this broker serves."""


@dataclass
class PaperConfig:
    window: str = "holdout"  # holdout | recent | full
    window_bars: int = 2000
    step_bars: int = 168
    max_ticks: int = 0


@dataclass
class FundingConfig:
    """The **simulated** capital account: how much may be credited, and in what currency.

    Operator policy, not part of the sealed risk layer.  The sealed caps limit what a
    strategy may risk with whatever capital it holds (5% per position, 15% drawdown)
    and they still bind on funded capital; this section only stops a typo - an extra
    zero - from funding a fantasy account.  What no *agent* can do is stated where it
    is enforced, in :mod:`selftrade.capital`: the research loop, the proposer and the
    evaluators have no code path that credits anything.
    """

    enabled: bool = True
    currency: str = "USDT"
    """The unit every credit is recorded in.  No conversion is done or implied."""

    max_deposit: float = 100_000.0
    """Largest single credit accepted.  ``0`` or negative refuses every credit."""

    max_total_capital: float = 1_000_000.0
    """Ceiling on the total credited (deposits minus withdrawals), ever."""

    allow_withdrawal: bool = True
    """Whether the operator may take capital back out of the account."""


@dataclass
class DashboardConfig:
    """The local dashboard, now with a login and a live Binance chart.

    ``auth`` is on by default and is **required** for any non-loopback bind:
    opening the dashboard on the LAN exposes the loop controls, so a bind that is
    not loopback while ``auth`` is false is refused (in the config and again in
    ``dashboard.make_server``).

    The password itself is never in this file.  Two places can hold it, and the
    environment wins:

    * ``password_env`` - the *name* of an environment variable.  When it is set, it is
      the password, and the Settings page refuses to change it (writing a hash nothing
      would read is worse than saying no);
    * ``password_file`` - where a password set from the dashboard is stored, as a salted
      PBKDF2 hash and nothing else.  Until someone sets one, the login accepts the
      bootstrap password ``0000`` and the dashboard says so (see
      :mod:`selftrade.credentials`).
    """

    host: str = "127.0.0.1"
    port: int = 8787
    auto_refresh_s: int = 15
    auth: bool = True
    """Require a password login.  Mandatory for a non-loopback bind."""

    password_env: str = "SELFTD_DASHBOARD_PASSWORD"
    """Name of the environment variable holding the password (never the value).

    When it is set it *overrides* the stored password, because a machine that provisions
    dashboards from the environment must not be surprised by a change made in a browser.
    """

    password_file: str = "state/dashboard_auth.json"
    """Where a password set from the dashboard (or ``password --set``) is stored.

    One salted PBKDF2 hash, written atomically and owner-only.  Deleting it goes back to
    the bootstrap password; `python -m selftrade password --status` says which one is in
    force.
    """

    password_min_length: int = 4
    """Shortest password the dashboard and the CLI accept (the bootstrap is exactly this)."""

    session_ttl_s: int = 86400
    """How long a signed login cookie stays valid."""

    market_symbol: str = "BTC/USDT"
    """Default symbol of the live chart (Binance public data, no keys)."""

    market_interval: str = "1m"
    """Default candle interval of the live chart."""

    market_bars: int = 180
    """Candles returned per refresh (one public request, max 1000)."""

    market_refresh_s: float = 3.0
    """How often the browser redraws the live chart."""

    key_refresh_s: float = 60.0
    """How often the Binance key panel re-probes the account, read-only.

    The panel runs the same probe as ``binance check``: one signed ``GET`` per
    refresh, never a POST.  One minute is the floor because every probe is a real
    request against the exchange's rate budget; the endpoint also caches for this
    long, so a roomful of tabs still costs one probe per window.
    """


def is_loopback(host: str) -> bool:
    """True for a host that is only reachable from this machine."""
    h = (host or "").strip().lower()
    return h in ("127.0.0.1", "localhost", "::1", "[::1]") or h.startswith("127.")


@dataclass
class MemoryConfig:
    db: str = "state/selftrade.db"


@dataclass
class NotifyConfig:
    enabled: bool = False
    telegram_token_env: str = "SELFTD_TELEGRAM_TOKEN"
    telegram_chat_id_env: str = "SELFTD_TELEGRAM_CHAT_ID"
    warn_below_frac: float = 0.25
    """Share of a halting limit's cap that may be left before a warning is sent.

    A halt is announced when it happens; this is the early-warning half: the drawdown or
    daily-loss limit standing inside this share of its cap is worth one message, because
    that is where something can still be done.  ``0`` turns this band off - but **not**
    the other one: a limit already *past* its cap is a breach, not a threshold, and it
    is always announced (the gate only sees a breach when it checks an order, which may
    be never).  Halts are announced while ``enabled`` is true, whatever this is.
    """

    watch_interval_s: float = 30.0
    """How often ``notify watch`` re-reads the switch and the limits, in seconds."""


@dataclass
class Settings:
    project: str = "selftrade"
    session_file: str = "state/session.json"
    data: DataConfig = field(default_factory=DataConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    evaluation: EvaluationConfig = field(default_factory=EvaluationConfig)
    evolution: EvolutionConfig = field(default_factory=EvolutionConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    rea: ReaConfig = field(default_factory=ReaConfig)
    mt5: MT5Config = field(default_factory=MT5Config)
    risk_gate: RiskGateConfig = field(default_factory=RiskGateConfig)
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    funding: FundingConfig = field(default_factory=FundingConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)


# --------------------------------------------------------------------------
# Loading / building
# --------------------------------------------------------------------------

# Valid values for a few enumerated fields.
_ENUMS = {
    "source": {"csv", "freqtrade_json", "synthetic", "mt5"},
    "metric": {"sharpe", "sortino", "return", "calmar"},
    "mode": {"params", "explore"},
    "window": {"holdout", "recent", "full"},
}


def _coerce(value: Any, hint: Any, path: str) -> Any:
    """Coerce ``value`` to the dataclass field type ``hint``."""
    origin = typing.get_origin(hint)
    if origin is typing.Union:  # Optional[X]
        args = [a for a in typing.get_args(hint) if a is not type(None)]
        if value is None:
            return None
        return _coerce(value, args[0], path) if args else value

    if is_dataclass(hint):
        return _build(hint, value, path)

    if hint is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
        raise ConfigError(f"{path}: expected a boolean, got {value!r}")

    if hint is int:
        if isinstance(value, bool):
            raise ConfigError(f"{path}: expected an integer, got a boolean")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                pass
        raise ConfigError(f"{path}: expected an integer, got {value!r}")

    if hint is float:
        if isinstance(value, bool):
            raise ConfigError(f"{path}: expected a number, got a boolean")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise ConfigError(f"{path}: expected a number, got {value!r}")

    if hint is str:
        if isinstance(value, str):
            return value
        raise ConfigError(f"{path}: expected a string, got {value!r}")

    if origin in (list, typing.List):
        if not isinstance(value, list):
            raise ConfigError(f"{path}: expected a list, got {value!r}")
        args = typing.get_args(hint)
        if args:
            return [_coerce(v, args[0], f"{path}[]") for v in value]
        return value

    if origin in (dict, typing.Dict):
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: expected a mapping, got {value!r}")
        return value

    return value


def _hints(cls: type) -> dict[str, Any]:
    """Resolve field type hints (``__future__`` annotations are strings)."""
    try:
        return typing.get_type_hints(cls)
    except Exception:  # pragma: no cover - fall back to raw field.type
        return {f.name: f.type for f in fields(cls)}


def _build(cls: type, data: Any, path: str = "") -> Any:
    """Build a dataclass of type ``cls`` from ``data``, rejecting unknown keys."""
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"{path or cls.__name__}: expected a mapping, got {data!r}")

    hints = _hints(cls)
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        joined = ", ".join(sorted(unknown))
        where = path or cls.__name__
        raise ConfigError(
            f"unknown config key(s) in '{where}': {joined}. "
            f"Known keys: {', '.join(sorted(known))}"
        )

    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        sub_path = f"{path}.{f.name}" if path else f.name
        kwargs[f.name] = _coerce(data[f.name], hints.get(f.name, f.type), sub_path)
    return cls(**kwargs)


def _apply_enum_checks(settings: Settings) -> None:
    checks = [
        ("data.source", settings.data.source, "source"),
        ("evaluation.metric", settings.evaluation.metric, "metric"),
        ("evolution.mode", settings.evolution.mode, "mode"),
        ("paper.window", settings.paper.window, "window"),
    ]
    for path, value, key in checks:
        allowed = _ENUMS[key]
        if value not in allowed:
            raise ConfigError(
                f"{path}: {value!r} is not valid. Choose one of: {', '.join(sorted(allowed))}"
            )


def load_settings(path: str | Path | None = None, overrides: list[str] | None = None) -> Settings:
    """Load settings from YAML, applying CLI ``section.key=value`` overrides."""
    raw: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise ConfigError(
                f"config file not found: {p}. Create one (see config.yaml) or pass --config."
            )
        try:
            loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:  # pragma: no cover - message passthrough
            raise ConfigError(f"invalid YAML in {p}: {exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{p}: top level must be a mapping")
        raw = loaded

    settings = _build(Settings, raw, "")
    for override in overrides or []:
        apply_override(settings, override)
    _apply_enum_checks(settings)
    _validate_ranges(settings)
    return settings


def _validate_ranges(settings: Settings) -> None:
    e = settings.evaluation
    if not 0.0 < e.train_frac < 1.0:
        raise ConfigError("evaluation.train_frac must be in (0, 1)")
    if not 0.0 <= e.holdout_frac < 1.0:
        raise ConfigError("evaluation.holdout_frac must be in [0, 1)")
    if e.folds < 1:
        raise ConfigError("evaluation.folds must be >= 1")
    if settings.evolution.population < 1:
        raise ConfigError("evolution.population must be >= 1")
    if settings.evolution.elite > settings.evolution.population:
        raise ConfigError("evolution.elite cannot exceed evolution.population")
    if not 0.0 <= settings.evolution.mutation_rate <= 1.0:
        raise ConfigError("evolution.mutation_rate must be in [0, 1]")
    if not 0.0 < settings.backtest.position_size <= 1.0:
        raise ConfigError("backtest.position_size must be in (0, 1]")
    dash = settings.dashboard
    if not is_loopback(dash.host) and not dash.auth:
        raise ConfigError(
            f"dashboard.host is {dash.host!r} (not loopback) but dashboard.auth is "
            "false. Opening the dashboard beyond this machine requires the login: "
            "set dashboard.auth=true (or bind 127.0.0.1)."
        )
    if dash.session_ttl_s < 60:
        raise ConfigError("dashboard.session_ttl_s must be >= 60")
    if not 10 <= dash.market_bars <= 1000:
        raise ConfigError("dashboard.market_bars must be in [10, 1000]")
    if dash.market_refresh_s < 0.5:
        raise ConfigError("dashboard.market_refresh_s must be >= 0.5")
    if dash.key_refresh_s < 5:
        raise ConfigError(
            "dashboard.key_refresh_s must be >= 5 (every probe is a signed request "
            "against the exchange's rate budget)"
        )
    if settings.paper.step_bars < 1:
        raise ConfigError("paper.step_bars must be >= 1")
    nt = settings.notify
    if not 0.0 <= nt.warn_below_frac < 1.0:
        raise ConfigError("notify.warn_below_frac must be in [0, 1)")
    if nt.watch_interval_s < 1.0:
        raise ConfigError("notify.watch_interval_s must be >= 1 (seconds)")
    rg = settings.risk_gate
    if not 0.0 < rg.risk_pct <= 1.0:
        raise ConfigError("risk_gate.risk_pct must be in (0, 1]")
    if rg.max_positions < 0:
        raise ConfigError("risk_gate.max_positions must be >= 0")
    if rg.max_leverage <= 0:
        raise ConfigError("risk_gate.max_leverage must be > 0")
    if rg.min_seconds_between_orders < 0:
        raise ConfigError("risk_gate.min_seconds_between_orders must be >= 0")
    if rg.min_notional < 0:
        raise ConfigError("risk_gate.min_notional must be >= 0")
    bn = settings.binance
    host = bn.base_url.split("//", 1)[-1].split("/", 1)[0].lower()
    if "testnet" not in host:
        if not bn.allow_production:
            raise ConfigError(
                f"binance.base_url must point at a testnet host, got {bn.base_url!r}. "
                "This project ships no production trading rail by default; a funded "
                "account is opt-in with binance.allow_production=true, knowingly."
            )
        if host not in BINANCE_PRODUCTION_HOSTS:
            raise ConfigError(
                f"binance.allow_production is true but {host!r} is not a known Binance "
                f"host ({', '.join(BINANCE_PRODUCTION_HOSTS)}); refusing a look-alike "
                "host that could be pointed anywhere."
            )
    if not 0 < bn.recv_window <= 60000:
        raise ConfigError("binance.recv_window must be in (0, 60000] milliseconds")
    if bn.timeout <= 0:
        raise ConfigError("binance.timeout must be > 0")
    if bn.max_weight_per_min < 1:
        raise ConfigError("binance.max_weight_per_min must be >= 1")
    if not 0 < bn.kline_bars <= 1000:
        raise ConfigError("binance.kline_bars must be in (0, 1000]")


def apply_override(settings: Settings, override: str) -> None:
    """Apply a single ``section.key=value`` (or ``top=value``) override in place."""
    if "=" not in override:
        raise ConfigError(f"invalid override {override!r}: expected section.key=value")
    dotted, _, value = override.partition("=")
    dotted = dotted.strip()
    if not dotted:
        raise ConfigError(f"invalid override {override!r}: empty key")

    parts = dotted.split(".")
    obj: Any = settings
    for part in parts[:-1]:
        if not hasattr(obj, part):
            raise ConfigError(f"unknown config section in override: {part!r}")
        obj = getattr(obj, part)
    leaf = parts[-1]
    if not hasattr(obj, leaf):
        raise ConfigError(f"unknown config key in override: {dotted!r}")

    hint = _hints(type(obj)).get(leaf, {f.name: f.type for f in fields(obj)}[leaf])
    coerced = _coerce(_parse_scalar(value), hint, dotted)
    setattr(obj, leaf, coerced)


def _parse_scalar(value: str) -> Any:
    """Parse a CLI string value into a bool / int / float / list / str."""
    v = value.strip()
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low == "null" or low == "none":
        return None
    # JSON-ish list/dict (used for lists like mt5.allowed_symbols)
    if (v.startswith("[") and v.endswith("]")) or (v.startswith("{") and v.endswith("}")):
        import json

        try:
            return json.loads(v)
        except json.JSONDecodeError:
            pass
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def as_dict(settings: Settings) -> dict[str, Any]:
    """Return a plain dict representation (used for DB / reports)."""
    return dataclasses.asdict(settings)
