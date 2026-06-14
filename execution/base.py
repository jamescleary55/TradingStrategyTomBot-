"""Broker adapter interface.

Every concrete adapter (:mod:`execution.tradovate_orders`,
:mod:`execution.topstepx_orders`, future IBKR/Rithmic/...) implements
this small surface. The live monitor + webhook receiver depend only on
the interface, so swapping brokers is a single line in ``.env``.

Resolution order for ``get_adapter()``:

    BROKER env var → "tradovate" (default) | "topstepx" | "dryrun"

The ``dryrun`` adapter never sends anything to a real venue — useful
for smoke tests and offline development.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from config import Instrument
from risk.sizing import TradePlan

log = logging.getLogger(__name__)


@dataclass
class PlacedOrder:
    order_id: int
    raw_response: Any


@dataclass
class OpenPosition:
    symbol: str           # contract/root the broker reports
    side: str             # "Buy" or "Sell"
    qty: int
    avg_entry: float
    unrealised_pnl: Optional[float] = None
    raw: Any = None


@dataclass
class AccountSnapshot:
    account_id: int
    cash: float
    equity: float
    positions: list[OpenPosition]


@dataclass
class ExecutionEvent:
    """One fill — entry, partial, target hit, stop hit, or cancellation.

    Shaped so the reconciler can match it to the trade-attempt row by
    ``order_id`` (or its child IDs for OSO brackets).
    """
    execution_id: str            # broker-side ID
    order_id: str                # parent order id (or this fill's id)
    parent_order_id: Optional[str] = None   # for bracket children
    timestamp: str = ""          # ISO UTC
    symbol: str = ""
    side: str = ""               # "Buy" or "Sell"
    qty: int = 0
    price: float = 0.0
    commission: float = 0.0
    kind: str = "fill"           # fill | partial | reject | cancel
    raw: Any = None


class BrokerAdapter(ABC):
    """Minimal surface every broker adapter must implement."""

    name: str = "base"

    @abstractmethod
    def place_bracket(
        self,
        *,
        instrument: Instrument,
        side: str,                 # "Buy" or "Sell"
        qty: int,
        entry: float,
        stop: float,
        target: float,
        account_id: Optional[int] = None,
        allow_live: bool = False,
        dry_run: bool = False,
    ) -> PlacedOrder: ...

    # Optional — adapters may override; default raises NotImplementedError so
    # the live position-poller can skip cleanly for adapters that haven't
    # implemented it yet.
    def snapshot(self, account_id: Optional[int] = None) -> AccountSnapshot:
        raise NotImplementedError(f"{self.name} adapter has no snapshot() yet")

    def list_executions(self, account_id: Optional[int] = None,
                        since_ts: Optional[str] = None) -> list[ExecutionEvent]:
        """Return every execution / fill event since ``since_ts`` (ISO UTC).

        Implementations must return in chronological order. Returning an
        empty list is fine. Long-poll semantics are not required.
        """
        raise NotImplementedError(f"{self.name} adapter has no list_executions() yet")

    # Higher-level glue used by the live monitor + webhook
    def place_bracket_for_setup(
        self,
        setup,
        plan: TradePlan,
        instrument: Instrument,
        *,
        account_id: Optional[int] = None,
        allow_live: bool = False,
        dry_run: bool = False,
    ) -> PlacedOrder:
        if not plan.approved:
            raise RuntimeError(f"plan not approved: {plan.reason}")
        side = "Buy" if setup.direction == "bull" else "Sell"
        return self.place_bracket(
            instrument=instrument, side=side, qty=plan.contracts,
            entry=plan.entry, stop=plan.stop, target=plan.target,
            account_id=account_id, allow_live=allow_live, dry_run=dry_run,
        )


# ---------------------------------------------------------------------------
# Dry-run adapter — no network, always succeeds. Used for safe local tests.
# ---------------------------------------------------------------------------
class DryRunAdapter(BrokerAdapter):
    name = "dryrun"

    def place_bracket(self, *, instrument, side, qty, entry, stop, target,
                      account_id=None, allow_live=False, dry_run=False) -> PlacedOrder:
        body = {
            "broker": "dryrun", "instrument": instrument.symbol,
            "side": side, "qty": qty, "entry": entry, "stop": stop, "target": target,
            "account_id": account_id,
        }
        log.info("[DRYRUN] would place bracket: %s", body)
        return PlacedOrder(order_id=0, raw_response={"dry_run": True, "body": body})

    def snapshot(self, account_id=None) -> AccountSnapshot:
        return AccountSnapshot(account_id=account_id or 0, cash=0.0, equity=0.0, positions=[])

    def list_executions(self, account_id=None, since_ts=None) -> list[ExecutionEvent]:
        return []


# ---------------------------------------------------------------------------
def get_adapter(name: Optional[str] = None) -> BrokerAdapter:
    """Return the adapter selected by .env (or override).

    Lazy-imports the concrete adapter module so the wrong creds never
    cause an import-time failure for users not on that broker.
    """
    name = (name or os.getenv("BROKER", "tradovate")).strip().lower()
    if name == "dryrun":
        return DryRunAdapter()
    if name == "tradovate":
        from execution.tradovate_orders import TradovateAdapter
        return TradovateAdapter()
    if name in ("topstepx", "projectx"):
        from execution.topstepx_orders import TopstepXAdapter
        return TopstepXAdapter()
    raise ValueError(f"Unknown BROKER: {name!r}")
