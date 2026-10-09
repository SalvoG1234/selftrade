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


@dataclass
class PaperConfig:
    window: str = "holdout"  # holdout | recent | full
    window_bars: int = 2000
    step_bars: int = 168
    max_ticks: int = 0


@dataclass
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    auto_refresh_s: int = 15


@dataclass
class MemoryConfig:
    db: str = "state/selftrade.db"


@dataclass
class NotifyConfig:
    enabled: bool = False
    telegram_token_env: str = "SELFTD_TELEGRAM_TOKEN"
    telegram_chat_id_env: str = "SELFTD_TELEGRAM_CHAT_ID"


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
    paper: PaperConfig = field(default_factory=PaperConfig)
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
    if settings.dashboard.host not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("dashboard.host must be loopback (127.0.0.1 / localhost / ::1)")
    if settings.paper.step_bars < 1:
        raise ConfigError("paper.step_bars must be >= 1")


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
