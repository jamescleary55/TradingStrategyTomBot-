"""Reconciliation data model.

A ``ReconciledTrade`` is one round-trip: a position opened and (for CLOSED) fully
returned to flat. It is derived purely from broker execution events, never from
raw order intentions, so the P&L it carries is broker truth.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

# ---- trade statuses -------------------------------------------------------
OPEN = "OPEN"            # entry filled, net position != 0, no exits yet
PARTIAL = "PARTIAL"      # net position != 0 but partially scaled out
CLOSED = "CLOSED"        # net position returned to zero — a complete round-trip
CANCELLED = "CANCELLED"  # order cancelled with zero fills
REJECTED = "REJECTED"    # order rejected with zero fills
VALID_STATUSES = {OPEN, PARTIAL, CLOSED, CANCELLED, REJECTED}

# ---- exit reasons ---------------------------------------------------------
EXIT_TARGET = "target"
EXIT_STOP = "stop"
EXIT_MANUAL = "manual"
EXIT_UNKNOWN = "unknown"


@dataclass
class ReconciledTrade:
    """One reconciled round-trip. Money fields are None until CLOSED."""
    trade_id: str
    account_id: Optional[str]
    symbol: str
    status: str                              # one of VALID_STATUSES

    entry_time: Optional[str] = None         # ISO UTC of first entry fill
    exit_time: Optional[str] = None          # ISO UTC of last exit fill (CLOSED)

    entry_price: Optional[float] = None      # VWAP of entry fills
    exit_price: Optional[float] = None       # VWAP of exit fills

    entry_qty: int = 0                       # total contracts entered
    exit_qty: int = 0                        # total contracts exited

    side: str = ""                           # entry side: "Buy" (long) | "Sell" (short)

    gross_pnl: Optional[float] = None        # price P&L * point_value (pre-commission)
    net_pnl: Optional[float] = None          # gross_pnl - commission
    commission: float = 0.0                  # summed across all fills in the round-trip

    slippage: Optional[float] = None         # adverse entry slippage in PRICE POINTS
    realized_R: Optional[float] = None       # net price move / planned risk-per-unit

    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    entry_perm_id: Optional[str] = None
    exit_perm_id: Optional[str] = None
    execution_ids: list = field(default_factory=list)

    exit_reason: Optional[str] = None        # target | stop | manual | unknown
    point_value: Optional[float] = None      # USD per 1.0 price move per contract

    def to_dict(self) -> dict:
        return asdict(self)
