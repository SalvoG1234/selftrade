"""Order-level execution: one interface, a simulated broker and a risk gate.

See :mod:`selftrade.execution.base` for the contract, :mod:`selftrade.execution.paper_broker`
for the default simulation, :mod:`selftrade.execution.risk_gate` for the pre-trade
checks, :mod:`selftrade.execution.kill_switch` for the stop-everything switch and
:mod:`selftrade.execution.runner` for the forward session that drives a genome
through the broker - the path ``paper`` takes.
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
from .kill_switch import KillSwitch, KillSwitchError, arming_detail, kill_switch_path
from .paper_broker import PaperBroker
from .risk_gate import (
    ALERT_KIND,
    Decision,
    RiskGate,
    account_limits,
    describe_decision,
    exposure_limits,
)
from .runner import OrderRunResult, run_orders

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
    "OrderRunResult",
    "OrderStatus",
    "OrderType",
    "PaperBroker",
    "Position",
    "RiskBlockedError",
    "RiskGate",
    "Side",
    "account_limits",
    "arming_detail",
    "describe_decision",
    "exposure_limits",
    "run_orders",
    "utc_now_iso",
]
