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

import asyncio
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


def _run_bounded(ib, coro_factory, seconds: float):
    """Run an ib_insync async call on the IB event loop, bounded by `seconds`.

    Raises ``asyncio.TimeoutError`` if it doesn't complete in time, so callers
    can degrade to a partial result instead of blocking forever.
    """
    return ib.run(asyncio.wait_for(coro_factory(), seconds))


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

    def snapshot(self, account_id=None, timeout: float = 8.0) -> AccountSnapshot:
        """Timeout-bounded account snapshot — never hangs.

        Uses one-shot, timeout-bounded async requests instead of the streaming
        ``reqAccountUpdates`` (which could wait indefinitely). If account values
        or positions don't arrive within ``timeout`` seconds, returns a PARTIAL
        snapshot (``partial=True``, with ``warnings`` naming the missing fields)
        rather than blocking the monitor/runner forever.
        """
        ib, lib = _connect()
        warnings: list[str] = []
        cash = equity = None
        currency = None
        positions: list[OpenPosition] = []
        try:
            accounts = ib.managedAccounts()
            acct = str(account_id) if account_id is not None else (accounts[0] if accounts else "")

            # Account values (base currency; do NOT filter to USD — paper accts
            # are often EUR, and these tags are reported in the account's base ccy).
            try:
                rows = _run_bounded(ib, lambda: ib.accountSummaryAsync(acct), timeout)
                for v in rows:
                    if v.tag == "TotalCashValue":
                        cash = float(v.value); currency = v.currency or currency
                    elif v.tag == "NetLiquidation":
                        equity = float(v.value); currency = v.currency or currency
            except Exception as e:
                warnings.append(f"account values unavailable ({e.__class__.__name__})")
                log.warning("IBKR snapshot: account values timed out/failed: %s", e)

            # Positions (timeout-bounded).
            try:
                pos = _run_bounded(ib, lambda: ib.reqPositionsAsync(), timeout) or []
                for p in pos:
                    if account_id is not None and str(getattr(p, "account", acct)) != str(account_id):
                        continue
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
                positions_ok = True
            except Exception as e:
                positions_ok = False
                warnings.append(f"positions unavailable ({e.__class__.__name__})")
                log.warning("IBKR snapshot: positions timed out/failed: %s", e)

            partial = bool(warnings)
            return AccountSnapshot(
                # Keep the raw alphanumeric account id (e.g. "DUQ834606") — never
                # coerce to int; that silently produced 0 and broke routing.
                account_id=str(acct),
                cash=cash if cash is not None else 0.0,
                equity=(equity if equity is not None else (cash or 0.0)),
                positions=positions,
                partial=partial,
                warnings=warnings,
                currency=currency,
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
                perm = getattr(ex, "permId", None)
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
                    perm_id=str(perm) if perm not in (None, "") else None,
                    account=str(getattr(ex, "acctNumber", "")) or None,
                    raw=f,
                ))
            events.sort(key=lambda e: e.timestamp)
            return events
        finally:
            ib.disconnect()

    def flatten_and_cancel_all(self, account_id=None, dry_run=False) -> dict:
        """EMERGENCY: cancel every open order and close every position to flat.

        Steps, each logged into the returned report:
          1. Cancel all open orders (global cancel).
          2. Submit an offsetting MARKET order per open position.
          3. Re-snapshot and verify the account is flat (no positions, no orders).

        ``dry_run=True`` reports what WOULD happen without sending anything. This
        is never invoked automatically — only via scripts/flatten_account.py by
        an operator.
        """
        report: dict = {
            "dry_run": dry_run, "cancelled_orders": [], "closed_positions": [],
            "errors": [], "flat": False, "account_id": None,
        }
        ib, lib = _connect()
        try:
            accounts = ib.managedAccounts()
            acct = str(account_id) if account_id is not None else (accounts[0] if accounts else "")
            report["account_id"] = acct

            # 1. Cancel all open orders.
            open_before = []
            try:
                ib.reqAllOpenOrders()
                ib.sleep(0.5)
                open_before = list(ib.openTrades())
            except Exception as e:
                report["errors"].append(f"list open orders failed: {e}")
            for t in open_before:
                oid = getattr(t.order, "orderId", None)
                status = getattr(t.orderStatus, "status", "")
                if status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                    continue
                report["cancelled_orders"].append({"orderId": oid, "status_before": status})
                if not dry_run:
                    try:
                        ib.cancelOrder(t.order)
                    except Exception as e:
                        report["errors"].append(f"cancel {oid} failed: {e}")
            if not dry_run and report["cancelled_orders"]:
                try:
                    ib.reqGlobalCancel()   # belt-and-suspenders
                except Exception as e:
                    report["errors"].append(f"global cancel failed: {e}")
                ib.sleep(1)

            # 2. Close each open position with an offsetting market order.
            try:
                positions = _run_bounded(ib, lambda: ib.reqPositionsAsync(), 8.0) or []
            except Exception as e:
                positions = []
                report["errors"].append(f"list positions failed: {e}")
            for p in positions:
                if account_id is not None and str(getattr(p, "account", acct)) != str(account_id):
                    continue
                qty = int(p.position)
                if qty == 0:
                    continue
                sym = getattr(p.contract, "localSymbol", "") or getattr(p.contract, "symbol", "")
                close_action = "SELL" if qty > 0 else "BUY"
                report["closed_positions"].append(
                    {"symbol": sym, "position": qty, "close_action": close_action, "qty": abs(qty)})
                if not dry_run:
                    try:
                        order = lib.MarketOrder(close_action, abs(qty))
                        order.account = acct or order.account
                        ib.placeOrder(p.contract, order)
                    except Exception as e:
                        report["errors"].append(f"close {sym} failed: {e}")
            if not dry_run and report["closed_positions"]:
                ib.sleep(2)   # let market orders fill

            # 3. Verify flat.
            if dry_run:
                report["flat"] = not report["closed_positions"] and not report["cancelled_orders"]
            else:
                try:
                    pos_after = _run_bounded(ib, lambda: ib.reqPositionsAsync(), 8.0) or []
                    live_pos = [p for p in pos_after if int(p.position) != 0 and (
                        account_id is None or str(getattr(p, "account", acct)) == str(account_id))]
                    ib.reqAllOpenOrders(); ib.sleep(0.5)
                    live_ord = [t for t in ib.openTrades()
                                if getattr(t.orderStatus, "status", "") not in
                                ("Filled", "Cancelled", "ApiCancelled", "Inactive")]
                    report["flat"] = not live_pos and not live_ord
                    report["positions_remaining"] = len(live_pos)
                    report["orders_remaining"] = len(live_ord)
                except Exception as e:
                    report["errors"].append(f"verify flat failed: {e}")
            log.warning("flatten_and_cancel_all (dry_run=%s): cancelled=%d closed=%d flat=%s errors=%d",
                        dry_run, len(report["cancelled_orders"]),
                        len(report["closed_positions"]), report["flat"], len(report["errors"]))
            return report
        finally:
            ib.disconnect()

    def list_open_orders(self, account_id=None) -> list[dict]:
        """Open / pending IBKR orders (non-terminal) for the account.

        Terminal statuses (Filled/Cancelled/ApiCancelled/Inactive) are excluded
        so the execution gate only sees genuinely resting orders.
        """
        ib, lib = _connect()
        _TERMINAL = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
        try:
            ib.reqAllOpenOrders()
            ib.sleep(0.5)
            out: list[dict] = []
            for t in ib.openTrades():
                acct = getattr(t.order, "account", "") or ""
                if account_id is not None and str(acct) and str(acct) != str(account_id):
                    continue
                status = getattr(t.orderStatus, "status", "") or ""
                if status in _TERMINAL:
                    continue
                out.append({
                    "orderId": getattr(t.order, "orderId", None),
                    "status": status,
                    "symbol": getattr(t.contract, "localSymbol", "")
                              or getattr(t.contract, "symbol", ""),
                    "account": str(acct),
                })
            return out
        finally:
            ib.disconnect()
