"""Order-level execution: one interface, a simulated broker and a risk gate.

See :mod:`selftrade.execution.base` for the contract, :mod:`selftrade.execution.paper_broker`
for the default simulation, :mod:`selftrade.execution.risk_gate` for the pre-trade
checks and :mod:`selftrade.execution.kill_switch` for the stop-everything switch.
"""

from .base import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerError,
    Fill,
    Mismatch,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    RiskBlockedError,
    Side,
    utc_now_iso,
)
from .kill_switch import DEFAULT_PATH as KILL_SWITCH_PATH
from .kill_switch import KillSwitch, KillSwitchError, kill_switch_path
from .paper_broker import PaperBroker
from .risk_gate import ALERT_KIND, Decision, RiskGate, describe_decision

__all__ = [
    "ALERT_KIND",
    "AccountSnapshot",
    "BrokerAdapter",
    "BrokerError",
    "Decision",
    "Fill",
    "KILL_SWITCH_PATH",
    "KillSwitch",
    "KillSwitchError",
    "kill_switch_path",
    "Mismatch",
    "Order",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
    "PaperBroker",
    "Position",
    "RiskBlockedError",
    "RiskGate",
    "Side",
    "describe_decision",
    "utc_now_iso",
]
