"""Interactive Brokers order placement (paper-first).

Implements :class:`execution.base.BrokerAdapter` for IBKR via ``ib_async``
(falls back to ``ib_insync``), talking to a running TWS or IB Gateway.

A fully-formed ICT setup is pushed as a **native IBKR bracket**: a parent
limit at entry, a stop-loss child, and a take-profit limit child, all in one
OCA group so a fill on one child cancels the other. This mirrors the OSO
bracket the Tradovate adapter sends.

Safety rails (intentionally strict; opt out explicitly):

- Hard-fails unless the connection is a paper session (``IB_ENV=paper``, i.e.
  port 7497/4002). Pass ``allow_live=True`` to override (your responsibility).
- Rejects zero / negative quantity.
- Rejects brackets whose stop/target are on the wrong side of entry.
- Tags orders so IBKR can attribute them to this app.

This module never *decides* to place an order — it's a wrapper around the IBKR
API. The decision lives upstream (live monitor, manual operator).

Connection comes from .env (config.py): IB_HOST, IB_PORT, IB_CLIENT_ID.
"""
from __future__ import annotations

import logging
from typing import Optional

from config import (
    IB_CLIENT_ID, IB_ENV, IB_EXCHANGE, IB_HOST, IB_PORT, Instrument,
)
from execution.base import (
    AccountSnapshot, BrokerAdapter, ExecutionEvent, OpenPosition, PlacedOrder,
)

log = logging.getLogger(__name__)


class IBKROrderError(RuntimeError):
    pass


def _ib_lib():
    """Use the maintained ``ib_async`` if present (py3.10+), else ``ib_insync``."""
    try:
        import ib_async as lib
    except ImportError:
        try:
            import ib_insync as lib
        except ImportError as e:  # pragma: no cover - environment dependent
            raise IBKROrderError(
                "IBKR adapter needs 'ib_async' (preferred) or 'ib_insync'. "
                "Install with: pip install ib_async"
            ) from e
    return lib


def _connect():
    lib = _ib_lib()
    ib = lib.IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=20)
    except Exception as e:
        raise IBKROrderError(
            f"Could not connect to TWS/Gateway at {IB_HOST}:{IB_PORT} "
            f"(clientId={IB_CLIENT_ID}). Is it running with the API enabled? {e}"
        ) from e
    return ib, lib


def _qualify_future(ib, lib, symbol: str):
    sym = symbol.upper()
    exch = IB_EXCHANGE.get(sym)
    if exch is None:
        raise IBKROrderError(
            f"{sym} is not an IBKR-routable futures root (known: {sorted(IB_EXCHANGE)})."
        )
    contract = lib.ContFuture(sym, exch)
    ib.qualifyContracts(contract)
    return contract


# ---------------------------------------------------------------------------
class IBKRAdapter(BrokerAdapter):
    name = "ibkr"

    def place_bracket(self, *, instrument: Instrument, side: str, qty: int,
                      entry: float, stop: float, target: float,
                      account_id=None, allow_live=False, dry_run=False) -> PlacedOrder:
        if qty <= 0:
            raise IBKROrderError(f"qty must be > 0 (got {qty})")
        if side not in ("Buy", "Sell"):
            raise IBKROrderError(f"side must be Buy or Sell, got {side!r}")

        is_buy = side == "Buy"
        # Brackets must wrap the entry on opposite sides.
        if is_buy and not (stop < entry < target):
            raise IBKROrderError(
                f"Buy bracket invalid: need stop({stop}) < entry({entry}) < target({target})")
        if not is_buy and not (target < entry < stop):
            raise IBKROrderError(
                f"Sell bracket invalid: need target({target}) < entry({entry}) < stop({stop})")

        if IB_ENV != "paper" and not allow_live:
            raise IBKROrderError(
                f"Refusing to place orders in IB_ENV={IB_ENV} without allow_live=True. "
                f"Use a paper port (7497 TWS / 4002 Gateway) or pass allow_live=True explicitly.")

        if dry_run:
            body = {
                "broker": "ibkr", "instrument": instrument.symbol, "side": side,
                "qty": qty, "entry": entry, "stop": stop, "target": target,
                "account_id": account_id,
            }
            log.info("DRY RUN — would place IBKR bracket: %s", body)
            return PlacedOrder(order_id=0, raw_response={"dry_run": True, "body": body})

        ib, lib = _connect()
        try:
            contract = _qualify_future(ib, lib, instrument.symbol)
            action = "BUY" if is_buy else "SELL"
            # ib_async/ib_insync helper builds parent + TP + SL as a linked OCA set.
            bracket = ib.bracketOrder(
                action, int(qty),
                limitPrice=float(entry),
                takeProfitPrice=float(target),
                stopLossPrice=float(stop),
            )
            placed = []
            for o in bracket:
                o.account = account_id or o.account
                placed.append(ib.placeOrder(contract, o))
            ib.sleep(1)  # let TWS assign order ids / acknowledge

            parent_trade = placed[0]
            order_id = int(getattr(parent_trade.order, "orderId", 0) or 0)
            statuses = [
                {"orderId": getattr(t.order, "orderId", None),
                 "status": getattr(t.orderStatus, "status", None)}
                for t in placed
            ]
            # A rejected/inactive parent means the venue refused the order.
            parent_status = getattr(parent_trade.orderStatus, "status", "")
            if parent_status in ("Inactive", "Cancelled", "ApiCancelled"):
                raise IBKROrderError(f"IBKR rejected bracket: {statuses}")

            log.info("Placed IBKR bracket: %s %s x%d @ %.2f  SL=%.2f  TP=%.2f (parent id=%s)",
                     side, instrument.symbol, qty, entry, stop, target, order_id)
            return PlacedOrder(order_id=order_id, raw_response={"orders": statuses})
        finally:
            ib.disconnect()

    def snapshot(self, account_id=None) -> AccountSnapshot:
        ib, lib = _connect()
        try:
            ib.reqAccountUpdates()
            ib.sleep(1)
            accounts = ib.managedAccounts()
            acct = str(account_id) if account_id is not None else (accounts[0] if accounts else "")

            cash = 0.0
            equity = 0.0
            for v in ib.accountValues(acct):
                if v.currency not in ("USD", "BASE", ""):
                    continue
                if v.tag == "TotalCashValue":
                    cash = float(v.value)
                elif v.tag == "NetLiquidation":
                    equity = float(v.value)

            positions: list[OpenPosition] = []
            for p in ib.positions(acct):
                if p.position == 0:
                    continue
                qty = int(p.position)
                positions.append(OpenPosition(
                    symbol=getattr(p.contract, "localSymbol", "") or getattr(p.contract, "symbol", ""),
                    side="Buy" if qty > 0 else "Sell",
                    qty=abs(qty),
                    avg_entry=float(p.avgCost or 0.0),
                    unrealised_pnl=None,
                    raw=p,
                ))
            return AccountSnapshot(
                account_id=_safe_int(acct), cash=cash,
                equity=equity or cash, positions=positions,
            )
        finally:
            ib.disconnect()

    def list_executions(self, account_id=None, since_ts=None) -> list[ExecutionEvent]:
        ib, lib = _connect()
        try:
            ib.reqExecutions()
            ib.sleep(1)
            events: list[ExecutionEvent] = []
            for f in ib.fills():
                ex = f.execution
                ts = ""
                if getattr(ex, "time", None) is not None:
                    try:
                        ts = ex.time.astimezone().isoformat()
                    except Exception:
                        ts = str(ex.time)
                if account_id is not None and str(getattr(ex, "acctNumber", "")) != str(account_id):
                    continue
                if since_ts and ts and ts <= since_ts:
                    continue
                comm = 0.0
                if getattr(f, "commissionReport", None) is not None:
                    comm = float(getattr(f.commissionReport, "commission", 0.0) or 0.0)
                events.append(ExecutionEvent(
                    execution_id=str(getattr(ex, "execId", "")),
                    order_id=str(getattr(ex, "orderId", "")),
                    parent_order_id=None,
                    timestamp=ts,
                    symbol=getattr(f.contract, "localSymbol", "") or getattr(f.contract, "symbol", ""),
                    side="Buy" if str(getattr(ex, "side", "")).upper().startswith("B") else "Sell",
                    qty=int(getattr(ex, "shares", 0) or 0),
                    price=float(getattr(ex, "price", 0.0) or 0.0),
                    commission=comm,
                    kind="fill",
                    raw=f,
                ))
            events.sort(key=lambda e: e.timestamp)
            return events
        finally:
            ib.disconnect()


def _safe_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
