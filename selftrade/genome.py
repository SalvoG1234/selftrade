"""The genome: pure JSON data describing a strategy.  Never code.

Layout::

    {"family": "rsi_reversion",
     "params": {"rsi_period": 14, ...},
     "risk":   {"stop_loss_pct": -0.05, "take_profit_pct": null,
                "trailing_pct": null, "trade_shorts": false}}

:func:`validate_genome` clamps every value and applies the per-family
constraints (``ema_fast < ema_slow``, ``rsi_buy < rsi_exit``, ...), discarding
unknown keys.  The output of an LLM therefore cannot produce an illegal genome.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

from .params import (
    BoolParam,
    ChoiceParam,
    FAMILY_ORDER,
    FAMILY_PARAMS,
    FloatParam,
    IntParam,
    RISK_DEFAULTS,
    RISK_STOP_HIGH,
    RISK_STOP_LOW,
    RISK_TARGET_HIGH,
    RISK_TARGET_LOW,
    clamp_param,
)


class GenomeError(Exception):
    """Raised when a genome cannot be validated at all (e.g. bad family)."""


def _clamp_risk(risk: Any) -> dict[str, Any]:
    if not isinstance(risk, dict):
        risk = {}
    out: dict[str, Any] = {}

    sl = risk.get("stop_loss_pct", RISK_DEFAULTS["stop_loss_pct"])
    try:
        sl = float(sl)
    except (TypeError, ValueError):
        sl = RISK_DEFAULTS["stop_loss_pct"]
    if sl != sl:
        sl = RISK_DEFAULTS["stop_loss_pct"]
    out["stop_loss_pct"] = max(RISK_STOP_LOW, min(RISK_STOP_HIGH, sl))

    for key in ("take_profit_pct", "trailing_pct"):
        val = risk.get(key, RISK_DEFAULTS[key])
        if val is None or (isinstance(val, str) and val.strip().lower() in ("null", "none", "")):
            out[key] = None
        else:
            try:
                f = float(val)
            except (TypeError, ValueError):
                out[key] = None
            else:
                if f != f or f <= 0:
                    out[key] = None
                else:
                    out[key] = max(RISK_TARGET_LOW, min(RISK_TARGET_HIGH, f))

    ts = risk.get("trade_shorts", RISK_DEFAULTS["trade_shorts"])
    if isinstance(ts, str):
        ts = ts.strip().lower() in ("true", "1", "yes", "on")
    out["trade_shorts"] = bool(ts)
    return out


def _fix_constraints(family: str, params: dict[str, Any]) -> dict[str, Any]:
    specs = FAMILY_PARAMS[family]
    if family == "trend_momentum":
        if params["ema_fast"] >= params["ema_slow"]:
            # Keep fast strictly below slow, both inside their own range.
            params["ema_fast"] = min(params["ema_fast"], params["ema_slow"] - 1)
            if params["ema_fast"] < specs["ema_fast"].low:
                params["ema_fast"] = specs["ema_fast"].low
                params["ema_slow"] = max(params["ema_fast"] + 1, specs["ema_slow"].low)
                params["ema_slow"] = min(params["ema_slow"], specs["ema_slow"].high)
    elif family == "rsi_reversion":
        if params["rsi_buy"] >= params["rsi_exit"]:
            params["rsi_exit"] = min(specs["rsi_exit"].high, params["rsi_buy"] + 5.0)
            if params["rsi_exit"] <= params["rsi_buy"]:
                params["rsi_buy"] = max(specs["rsi_buy"].low, params["rsi_exit"] - 5.0)
    elif family == "donchian_breakout":
        if params["exit_period"] > params["entry_period"]:
            params["exit_period"] = max(specs["exit_period"].low, min(params["exit_period"], params["entry_period"]))
    return params


def validate_genome(genome: Any) -> dict[str, Any]:
    """Return a canonical, clamped, legal genome.  Unknown keys are dropped."""
    if not isinstance(genome, dict):
        raise GenomeError(f"genome must be a mapping, got {type(genome).__name__}")
    family = genome.get("family")
    if family not in FAMILY_PARAMS:
        raise GenomeError(
            f"unknown family {family!r}. Choose one of: {', '.join(FAMILY_ORDER)}"
        )
    specs = FAMILY_PARAMS[family]
    raw_params = genome.get("params") or {}
    if not isinstance(raw_params, dict):
        raw_params = {}

    params: dict[str, Any] = {}
    for name, spec in specs.items():
        value = raw_params.get(name, spec.default)
        clamped = clamp_param(spec, value)
        if isinstance(spec, IntParam) or isinstance(spec, FloatParam):
            clamped = clamp_param(spec, clamped)  # second pass after constraint fix
        params[name] = clamped

    params = _fix_constraints(family, params)
    # Re-clamp in case a constraint fix pushed a value out of range.
    for name, spec in specs.items():
        params[name] = clamp_param(spec, params[name])

    return {"family": family, "params": params, "risk": _clamp_risk(genome.get("risk"))}


# --------------------------------------------------------------------------
# Sampling / mutation
# --------------------------------------------------------------------------


def _sample_param(spec: Any, rng: np.random.Generator) -> Any:
    if isinstance(spec, IntParam):
        return int(rng.integers(spec.low, spec.high + 1))
    if isinstance(spec, FloatParam):
        return float(rng.uniform(spec.low, spec.high))
    if isinstance(spec, BoolParam):
        return bool(rng.random() < 0.5)
    if isinstance(spec, ChoiceParam):
        return str(rng.choice(list(spec.choices)))
    return spec.default  # pragma: no cover


def sample_genome(
    family: str | None = None, rng: np.random.Generator | None = None
) -> dict[str, Any]:
    rng = rng or np.random.default_rng()
    if family is None:
        family = str(rng.choice(FAMILY_ORDER))
    if family not in FAMILY_PARAMS:
        raise GenomeError(f"unknown family {family!r}")
    specs = FAMILY_PARAMS[family]
    params = {name: _sample_param(spec, rng) for name, spec in specs.items()}
    risk = {
        "stop_loss_pct": float(rng.uniform(-0.12, -0.015)),
        "take_profit_pct": (None if rng.random() < 0.5 else float(rng.uniform(0.03, 0.25))),
        "trailing_pct": (None if rng.random() < 0.7 else float(rng.uniform(0.03, 0.25))),
        "trade_shorts": bool(rng.random() < 0.3),
    }
    return validate_genome({"family": family, "params": params, "risk": risk})


def _mutate_param(name: str, spec: Any, value: Any, rng: np.random.Generator) -> Any:
    if isinstance(spec, IntParam):
        span = spec.high - spec.low
        step = max(1, int(round(span * rng.uniform(0.05, 0.25))))
        return int(value) + int(rng.integers(-step, step + 1))
    if isinstance(spec, FloatParam):
        span = spec.high - spec.low
        return float(value) + float(rng.normal(0.0, span * 0.15))
    if isinstance(spec, BoolParam):
        return not bool(value)
    if isinstance(spec, ChoiceParam):
        return str(rng.choice(list(spec.choices)))
    return value  # pragma: no cover


def mutate_genome(
    genome: dict[str, Any],
    rng: np.random.Generator | None = None,
    rate: float = 0.35,
    *,
    allow_family_change: bool = False,
    family_change_prob: float = 0.08,
) -> dict[str, Any]:
    """Return a mutated copy of ``genome`` (validated)."""
    rng = rng or np.random.default_rng()
    g = validate_genome(genome)

    if allow_family_change and rng.random() < family_change_prob:
        return sample_genome(str(rng.choice(FAMILY_ORDER)), rng)

    family = g["family"]
    specs = FAMILY_PARAMS[family]
    params = dict(g["params"])
    for name, spec in specs.items():
        if rng.random() < rate:
            params[name] = _mutate_param(name, spec, params[name], rng)

    risk = dict(g["risk"])
    if rng.random() < rate:
        risk["stop_loss_pct"] = float(risk["stop_loss_pct"]) + float(rng.normal(0.0, 0.02))
    if rng.random() < rate:
        if risk["take_profit_pct"] is None:
            risk["take_profit_pct"] = float(rng.uniform(0.03, 0.25)) if rng.random() < 0.5 else None
        else:
            risk["take_profit_pct"] = float(risk["take_profit_pct"]) + float(rng.normal(0.0, 0.04))
    if rng.random() < rate:
        if risk["trailing_pct"] is None:
            risk["trailing_pct"] = float(rng.uniform(0.03, 0.25)) if rng.random() < 0.4 else None
        else:
            risk["trailing_pct"] = float(risk["trailing_pct"]) + float(rng.normal(0.0, 0.04))
    if rng.random() < rate:
        risk["trade_shorts"] = not risk["trade_shorts"]

    return validate_genome({"family": family, "params": params, "risk": risk})


def crossover_genomes(
    a: dict[str, Any],
    b: dict[str, Any],
    rng: np.random.Generator | None = None,
) -> dict[str, Any]:
    """Uniform crossover.  If families differ, pick one parent's family."""
    rng = rng or np.random.default_rng()
    ga = validate_genome(a)
    gb = validate_genome(b)
    family = ga["family"] if rng.random() < 0.5 else gb["family"]
    specs = FAMILY_PARAMS[family]

    src_a = ga["params"] if ga["family"] == family else {}
    src_b = gb["params"] if gb["family"] == family else {}
    params = {}
    for name in specs:
        candidates = [d[name] for d in (src_a, src_b) if name in d]
        if not candidates:
            params[name] = specs[name].default
        else:
            params[name] = candidates[int(rng.integers(0, len(candidates)))]

    risk = {}
    for key in ("stop_loss_pct", "take_profit_pct", "trailing_pct", "trade_shorts"):
        pick = ga["risk"] if rng.random() < 0.5 else gb["risk"]
        risk[key] = pick[key]

    return validate_genome({"family": family, "params": params, "risk": risk})


# --------------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------------


def complexity(genome: dict[str, Any]) -> int:
    """Number of 'active' knobs: non-default params + enabled risk rules.

    Used as a fitness penalty so the search prefers simpler strategies.
    """
    g = validate_genome(genome)
    specs = FAMILY_PARAMS[g["family"]]
    count = 0
    for name, spec in specs.items():
        default = spec.default
        value = g["params"][name]
        if isinstance(spec, FloatParam):
            if abs(float(value) - float(default)) > 1e-9:
                count += 1
        elif value != default:
            count += 1
    if g["risk"]["take_profit_pct"] is not None:
        count += 1
    if g["risk"]["trailing_pct"] is not None:
        count += 1
    if g["risk"]["trade_shorts"]:
        count += 1
    return count


def genome_diff(parent: Any, child: Any) -> list[dict[str, Any]]:
    """Every value that changed from ``parent`` to ``child``, in a stable order.

    The single diff primitive behind strategy versions: the search records it when a
    candidate is evaluated, so "what changed" is a stored fact rather than something
    reconstructed later from a report.  Returns ``[]`` when a side is unusable - a
    missing diff must never break a run or a panel.
    """
    try:
        c = validate_genome(child)
        p = validate_genome(parent)
    except (GenomeError, TypeError, ValueError):
        return []
    changes: list[dict[str, Any]] = []
    if c["family"] != p["family"]:
        changes.append({"key": "family", "group": "strategy", "from": p["family"], "to": c["family"]})
    for key, value in c["params"].items():
        old = p["params"].get(key)
        if old != value:
            changes.append({"key": key, "group": "params", "from": old, "to": value})
    for key, value in c["risk"].items():
        old = p["risk"].get(key)
        if old != value:
            changes.append({"key": "risk." + key, "group": "risk", "from": old, "to": value})
    return changes


def genome_key(genome: dict[str, Any]) -> str:
    """Stable hash of a canonical genome (dedup / reproducibility)."""
    g = validate_genome(genome)
    payload = json.dumps(g, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def genome_json(genome: dict[str, Any]) -> str:
    return json.dumps(validate_genome(genome), sort_keys=True)


def describe(genome: dict[str, Any]) -> str:
    """One-line human description (for reports and logs)."""
    g = validate_genome(genome)
    parts = [f"{k}={v}" for k, v in sorted(g["params"].items())]
    risk = g["risk"]
    r = f"sl={risk['stop_loss_pct']:.3f}"
    if risk["take_profit_pct"] is not None:
        r += f",tp={risk['take_profit_pct']:.3f}"
    if risk["trailing_pct"] is not None:
        r += f",trail={risk['trailing_pct']:.3f}"
    if risk["trade_shorts"]:
        r += ",shorts"
    return f"{g['family']} [{', '.join(parts)}] ({r})"
