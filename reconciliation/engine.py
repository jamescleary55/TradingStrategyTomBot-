"""Trade reconciliation engine.

Converts a stream of raw broker execution events into reconciled round-trips.

Design principles (see TRADE_RECONCILIATION_DESIGN.md):

- **Identity, not timestamps.** Duplicate executions are collapsed by
  ``execution_id`` (then ``perm_id``/composite). Entry↔exit matching is driven by
  the running net position per (account, contract), not by time proximity.
- **Net-position lifecycle.** A trade OPENS when net position leaves zero and is
  CLOSED only when it returns to zero. A fill that flips through zero is split:
  the part that flattens closes the current trade; the remainder opens the next.
- **Broker truth.** P&L comes from actual fill prices/quantities, never from the
  intended entry/stop/target. Order intentions (``order_meta``) are used ONLY to
  derive slippage, realized-R, and the exit-reason label.
- **Robust to disorder & partials.** Events are sorted deterministically before
  the walk, so out-of-order input yields the same result. Partial fills and
  scale-in/scale-out are handled by accumulating VWAPs.
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

from reconciliation.model import (
    CANCELLED, CLOSED, EXIT_STOP, EXIT_TARGET, EXIT_UNKNOWN, OPEN, PARTIAL,
    REJECTED, ReconciledTrade,
)

log = logging.getLogger("reconciliation.engine")

_FILL_KINDS = {"fill", "partial"}
_CANCEL_KINDS = {"cancel"}
_REJECT_KINDS = {"reject"}


# ---------------------------------------------------------------------------
# Point-value resolution (USD per 1.0 price move per contract)
# ---------------------------------------------------------------------------
def _default_point_value(symbol: str) -> Optional[float]:
    """Resolve a contract symbol (e.g. 'MESU6') to its instrument point value.

    Matches the longest known root prefix so 'MES' wins over 'ES' for 'MESU6'.
    Returns None if unknown (caller leaves P&L in price points).
    """
    try:
        from config import INSTRUMENTS
    except Exception:  # pragma: no cover
        return None
    sym = (symbol or "").upper()
    best = None
    for root, inst in INSTRUMENTS.items():
        if sym.startswith(root) and (best is None or len(root) > len(best[0])):
            best = (root, inst.point_value)
    return best[1] if best else None


# ---------------------------------------------------------------------------
def _dedup_key(ev) -> str:
    """Identity for dedup. execution_id first; else a composite (still stable)."""
    exid = (getattr(ev, "execution_id", "") or "").strip()
    if exid:
        return f"ex:{exid}"
    perm = (getattr(ev, "perm_id", "") or "")
    return f"cp:{perm}|{ev.order_id}|{ev.timestamp}|{ev.side}|{ev.qty}|{ev.price}"


def _sort_key(ev):
    """Deterministic ordering for the position walk. Time first, then stable
    tiebreakers so equal-timestamp and out-of-order inputs are handled."""
    return (ev.timestamp or "", str(getattr(ev, "execution_id", "")),
            str(ev.order_id or ""))


def _group_key(ev) -> tuple:
    return (str(getattr(ev, "account", "") or ""), str(ev.symbol or ""))


def _is_buy(side: str) -> bool:
    return str(side).lower().startswith("b")


class _Leg:
    """Accumulates one side (entry or exit) of an in-progress round-trip."""
    __slots__ = ("qty", "notional", "commission", "order_ids", "perm_ids",
                 "exec_ids", "first_ts", "last_ts")

    def __init__(self):
        self.qty = 0
        self.notional = 0.0          # sum(price * qty) for VWAP
        self.commission = 0.0
        self.order_ids: list[str] = []
        self.perm_ids: list[str] = []
        self.exec_ids: list[str] = []
        self.first_ts: Optional[str] = None
        self.last_ts: Optional[str] = None

    def add(self, price, qty, commission, order_id, perm_id, exec_id, ts):
        self.qty += qty
        self.notional += price * qty
        self.commission += commission
        if order_id:
            self.order_ids.append(str(order_id))
        if perm_id:
            self.perm_ids.append(str(perm_id))
        if exec_id:
            self.exec_ids.append(str(exec_id))
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts

    @property
    def vwap(self) -> Optional[float]:
        return (self.notional / self.qty) if self.qty else None


class _Trade:
    """An in-progress round-trip for one (account, contract)."""
    def __init__(self, account, symbol):
        self.account = account
        self.symbol = symbol
        self.entry = _Leg()
        self.exit = _Leg()
        self.entry_side: Optional[str] = None   # "Buy" / "Sell"


def _finalize(t: _Trade, *, point_value, order_meta) -> ReconciledTrade:
    direction = 1 if t.entry_side == "Buy" else -1
    entry_px = t.entry.vwap
    exit_px = t.exit.vwap
    closed = t.exit.qty > 0 and t.exit.qty == t.entry.qty

    pv = point_value(t.symbol) if callable(point_value) else point_value
    first_exec = t.entry.exec_ids[0] if t.entry.exec_ids else (t.symbol)
    trade_id = f"{t.account or 'NA'}|{t.symbol}|{first_exec}"

    rt = ReconciledTrade(
        trade_id=trade_id,
        account_id=t.account or None,
        symbol=t.symbol,
        status=CLOSED if closed else (PARTIAL if t.exit.qty > 0 else OPEN),
        entry_time=t.entry.first_ts,
        exit_time=t.exit.last_ts if t.exit.qty > 0 else None,
        entry_price=entry_px,
        exit_price=exit_px,
        entry_qty=t.entry.qty,
        exit_qty=t.exit.qty,
        side=t.entry_side or "",
        commission=round(t.entry.commission + t.exit.commission, 6),
        entry_order_id=t.entry.order_ids[0] if t.entry.order_ids else None,
        exit_order_id=t.exit.order_ids[-1] if t.exit.order_ids else None,
        entry_perm_id=t.entry.perm_ids[0] if t.entry.perm_ids else None,
        exit_perm_id=t.exit.perm_ids[-1] if t.exit.perm_ids else None,
        execution_ids=list(t.entry.exec_ids) + list(t.exit.exec_ids),
        point_value=pv,
    )

    if closed and entry_px is not None and exit_px is not None:
        price_move = (exit_px - entry_px) * direction          # per unit, signed
        gross = price_move * t.entry.qty * (pv if pv is not None else 1.0)
        rt.gross_pnl = round(gross, 6)
        rt.net_pnl = round(gross - rt.commission, 6)

        meta = (order_meta or {}).get(rt.entry_order_id) or {}
        intended_entry = meta.get("intended_entry")
        intended_stop = meta.get("intended_stop")
        intended_target = meta.get("intended_target")
        if intended_entry is not None:
            rt.slippage = round((entry_px - float(intended_entry)) * direction, 6)
        if intended_entry is not None and intended_stop is not None:
            risk = abs(float(intended_entry) - float(intended_stop))
            if risk > 0:
                rt.realized_R = round(price_move / risk, 6)
        # exit-reason label (does NOT affect P&L)
        if intended_target is not None and intended_stop is not None and exit_px is not None:
            rt.exit_reason = (EXIT_TARGET
                              if abs(exit_px - float(intended_target)) <= abs(exit_px - float(intended_stop))
                              else EXIT_STOP)
        elif rt.net_pnl is not None:
            rt.exit_reason = EXIT_TARGET if price_move >= 0 else EXIT_STOP
        else:
            rt.exit_reason = EXIT_UNKNOWN
    return rt


# ---------------------------------------------------------------------------
def reconcile(
    executions: Iterable,
    *,
    order_meta: Optional[dict] = None,
    point_value: Optional[Callable[[str], Optional[float]]] = None,
) -> list[ReconciledTrade]:
    """Reconcile raw execution events into round-trips.

    Args:
        executions: iterable of ExecutionEvent-like objects (execution_id,
            order_id, parent_order_id, perm_id, account, symbol, side, qty,
            price, commission, kind, timestamp).
        order_meta: optional ``{entry_order_id: {intended_entry, intended_stop,
            intended_target, planned_R}}`` — used only for slippage / realized_R /
            exit-reason, never for P&L.
        point_value: optional ``symbol -> point_value`` override (defaults to
            resolving against config.INSTRUMENTS).

    Returns: list of ReconciledTrade (CLOSED, OPEN, PARTIAL, plus CANCELLED /
    REJECTED for zero-fill orders).
    """
    pv = point_value or _default_point_value
    events = list(executions)

    # 1. Split fills from order-level cancel/reject events.
    fills = [e for e in events if (e.kind or "fill") in _FILL_KINDS]
    cancels = [e for e in events if (e.kind or "") in _CANCEL_KINDS]
    rejects = [e for e in events if (e.kind or "") in _REJECT_KINDS]

    # 2. Dedup fills by identity (executionId first).
    seen: set[str] = set()
    unique_fills = []
    for e in fills:
        k = _dedup_key(e)
        if k in seen:
            continue
        seen.add(k)
        unique_fills.append(e)

    # 3. Group by (account, contract), sort each group deterministically.
    groups: dict[tuple, list] = {}
    for e in unique_fills:
        groups.setdefault(_group_key(e), []).append(e)

    trades: list[ReconciledTrade] = []
    filled_order_ids: set[str] = set()
    # Order ids that belong to a filled bracket family (a fill's own order or its
    # parent). An OCA sibling auto-cancel within such a family is NOT a standalone
    # cancelled trade, so it is suppressed below.
    filled_family: set[str] = set()
    for e in unique_fills:
        if e.order_id:
            filled_family.add(str(e.order_id))
        if getattr(e, "parent_order_id", None):
            filled_family.add(str(e.parent_order_id))

    for (account, symbol), group in groups.items():
        group.sort(key=_sort_key)
        pos = 0                      # signed net position
        cur: Optional[_Trade] = None

        for e in group:
            filled_order_ids.add(str(e.order_id or ""))
            sgn = 1 if _is_buy(e.side) else -1
            remaining = int(e.qty)
            unit_comm = (float(e.commission or 0.0) / e.qty) if e.qty else 0.0

            while remaining > 0:
                if pos == 0:
                    cur = _Trade(account, symbol)
                    cur.entry_side = "Buy" if sgn > 0 else "Sell"
                    entry_sign = sgn
                    take = remaining
                    cur.entry.add(e.price, take, unit_comm * take, e.order_id,
                                  e.perm_id, e.execution_id, e.timestamp)
                    pos += sgn * take
                    remaining = 0
                elif (pos > 0) == (sgn > 0):
                    # same direction → scaling into the position (entry fill)
                    take = remaining
                    cur.entry.add(e.price, take, unit_comm * take, e.order_id,
                                  e.perm_id, e.execution_id, e.timestamp)
                    pos += sgn * take
                    remaining = 0
                else:
                    # opposite direction → reducing position (exit fill)
                    closing = min(remaining, abs(pos))
                    cur.exit.add(e.price, closing, unit_comm * closing, e.order_id,
                                 e.perm_id, e.execution_id, e.timestamp)
                    pos += sgn * closing
                    remaining -= closing
                    if pos == 0:
                        trades.append(_finalize(cur, point_value=pv, order_meta=order_meta))
                        cur = None
                        # any 'remaining' now opens a fresh trade on next loop

        if cur is not None and pos != 0:
            # Position still open at end of stream → OPEN or PARTIAL.
            trades.append(_finalize(cur, point_value=pv, order_meta=order_meta))

    # 4. Zero-fill cancel / reject orders become CANCELLED / REJECTED trades.
    def _zero_fill_trade(e, status):
        return ReconciledTrade(
            trade_id=f"{(e.account or 'NA')}|{e.symbol}|{e.order_id}|{status}",
            account_id=(e.account or None), symbol=e.symbol, status=status,
            entry_order_id=str(e.order_id) if e.order_id else None,
            entry_perm_id=str(e.perm_id) if e.perm_id else None,
            execution_ids=[e.execution_id] if e.execution_id else [],
        )

    def _belongs_to_filled_family(e) -> bool:
        return (str(e.order_id or "") in filled_family
                or str(getattr(e, "parent_order_id", "") or "") in filled_family)

    emitted: set = set()
    for e in rejects:
        oid = str(e.order_id or "")
        if oid and not _belongs_to_filled_family(e) and ("R", oid) not in emitted:
            trades.append(_zero_fill_trade(e, REJECTED))
            emitted.add(("R", oid))
    for e in cancels:
        oid = str(e.order_id or "")
        if (oid and not _belongs_to_filled_family(e)
                and ("C", oid) not in emitted and ("R", oid) not in emitted):
            trades.append(_zero_fill_trade(e, CANCELLED))
            emitted.add(("C", oid))

    return trades
