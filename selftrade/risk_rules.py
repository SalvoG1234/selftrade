"""Sealed risk limits: frozen constants the model can never touch.

The demo this project mirrors is blunt about it: the agent may rewrite its own
strategy code, but the risk limits live in a **separate, read-only layer**, sealed
with SHA-256, with ``MODEL_CAN_OVERRIDE = False``.  Nothing an LLM, a genome, a
config key or a CLI flag can express is able to move them.

How the seal is enforced rather than decorative:

1. `RiskLimits` is a ``frozen`` dataclass - the running process cannot mutate it;
2. the limits are **deliberately not part of `config.yaml`**, so
   ``--set risk_rules.MAX_DRAWDOWN_PCT=99`` fails as an unknown config key exactly
   like any other typo, and no genome key can reach them either;
3. the digest of the shipped values is pinned in :data:`SEALED_DIGEST`.  The first
   deployment writes it to ``<state>/risk_rules.seal``; every later deployment
   re-verifies it, and editing the numbers in this file makes the next
   deployment **refuse** until the operator re-seals on purpose
   (``python -m selftrade risk seal``).

Scope, stated plainly: these limits guard **deployment** - the critic verdict, the
paper session and the live sizing rail.  The research backtester is *not* clamped
by them, because exploration has to be free; `docs/AUTOPSY-AND-CRITIC.md` records
that boundary and why it exists.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SEAL_FILENAME = "risk_rules.seal"


class RiskRulesError(Exception):
    """Raised when the sealed risk layer was tampered with or is violated."""


@dataclass(frozen=True)
class RiskLimits:
    """The hard limits.  Frozen on purpose: there is no setter and no override."""

    MAX_POSITION_PCT: float = 5.0
    """Share of equity a single live position may take."""

    HARD_STOP_LOSS_PCT: float = 5.0
    """No strategy is deployed with a stop looser than this."""

    MAX_DAILY_LOSS_PCT: float = 2.0
    """A worse single day halts the strategy (and refuses it here)."""

    MAX_DRAWDOWN_PCT: float = 15.0
    """A deeper out-of-sample drawdown breaks the strategy."""

    MODEL_CAN_OVERRIDE: bool = False
    """Documented, checked and asserted: the model cannot relax any of the above."""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def canonical(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()


LIMITS = RiskLimits()

#: Digest of the limits as shipped.  Regenerate with `python -m selftrade risk seal`.
SEALED_DIGEST = "f145ca89d49d48eafeb689f59b9248cccff0696ecb3047d75a9d4a80afe86e3e"


def digest_of(limits: RiskLimits = LIMITS) -> str:
    return limits.digest()


def assert_pinned(expected: str = SEALED_DIGEST, limits: RiskLimits = LIMITS) -> str:
    """Raise if the shipped limits no longer match the pinned digest."""
    got = digest_of(limits)
    if expected and got != expected:
        raise RiskRulesError(
            "the sealed risk limits were edited: digest "
            f"{got[:12]}... does not match the pin {expected[:12]}.... "
            "Re-seal on purpose with `python -m selftrade risk seal` if the change "
            "was intentional."
        )
    return got


def assert_no_levers(limits: RiskLimits = LIMITS) -> None:
    """The model-side levers must all be closed."""
    if limits.MODEL_CAN_OVERRIDE:
        raise RiskRulesError(
            "MODEL_CAN_OVERRIDE is True: the risk layer is no longer sealed; "
            "this project does not allow that configuration."
        )
    if limits.MAX_POSITION_PCT <= 0 or limits.HARD_STOP_LOSS_PCT <= 0:
        raise RiskRulesError("position and stop limits must be positive")


def seal_path(settings: Any = None) -> Path:
    """Where the seal lives: next to the session file (``state/`` by default)."""
    if settings is not None and getattr(settings, "session_file", None):
        return Path(settings.session_file).parent / SEAL_FILENAME
    return Path("state") / SEAL_FILENAME


def write_seal(settings: Any = None, limits: RiskLimits = LIMITS) -> Path:
    """Write (or re-write) the seal file.  Explicit operator action."""
    path = seal_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "digest": digest_of(limits),
        "limits": limits.as_dict(),
        "sealed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "delete this file to re-seal on the next run; editing the limits in "
        "the source without re-sealing makes deployments refuse",
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def verify_seal(settings: Any = None, limits: RiskLimits = LIMITS) -> dict[str, Any]:
    """Check the seal.  Missing file -> seal it now; mismatched digest -> refuse."""
    path = seal_path(settings)
    digest = digest_of(limits)
    pinned = digest == SEALED_DIGEST
    if not path.exists():
        try:
            write_seal(settings, limits)
        except OSError as exc:  # pragma: no cover - read-only filesystem
            return {
                "ok": False,
                "reason": f"cannot write the seal file {path}: {exc}",
                "digest": digest,
                "path": str(path),
                "pinned": pinned,
                "sealed_at": None,
            }
        return {
            "ok": True,
            "reason": "sealed now (first run)",
            "digest": digest,
            "path": str(path),
            "pinned": pinned,
            "sealed_at": None,
        }

    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "reason": f"the seal file {path} is unreadable or corrupt: {exc}",
            "digest": digest,
            "path": str(path),
            "pinned": pinned,
            "sealed_at": None,
        }

    stored_digest = str(stored.get("digest", ""))
    if stored_digest != digest:
        return {
            "ok": False,
            "reason": (
                f"the risk limits changed after they were sealed "
                f"({stored_digest[:12]}... -> {digest[:12]}...): re-seal with "
                f"`python -m selftrade risk seal` if that was intentional"
            ),
            "digest": digest,
            "path": str(path),
            "pinned": pinned,
            "sealed_at": stored.get("sealed_at"),
        }
    return {
        "ok": True,
        "reason": "seal verified",
        "digest": digest,
        "path": str(path),
        "pinned": pinned,
        "sealed_at": stored.get("sealed_at"),
    }


def effective_position_size(settings: Any, limits: RiskLimits = LIMITS) -> float:
    """Live sizing: the smaller of the configured size and the sealed cap.

    Used by the live rail.  The research backtester keeps the configured size so
    that exploration, paper and backtest stay comparable by construction.
    """
    configured = float(getattr(settings.backtest, "position_size", 1.0))
    return max(0.0, min(configured, limits.MAX_POSITION_PCT / 100.0))


def sealed_block(limits: RiskLimits = LIMITS) -> str:
    """The read-only panel: how the limits look printed for a human."""
    lines = [f"{k:<22} = {v}" for k, v in limits.as_dict().items()]
    lines.append(f"{'SHA-256':<22} = {digest_of(limits)[:32]}...")
    return "\n".join(lines)


def is_sealed(limits: RiskLimits = LIMITS) -> bool:
    """Read-only test used by the interface layer."""
    try:
        assert_no_levers(limits)
    except RiskRulesError:
        return False
    if os.environ.get("SELFTD_ALLOW_RISK_OVERRIDE"):
        raise RiskRulesError(
            "SELFTD_ALLOW_RISK_OVERRIDE is set: this project does not provide a way "
            "to widen the sealed risk limits from the environment."
        )
    return True
