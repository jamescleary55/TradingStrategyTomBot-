"""Tradovate paper-only order placement.

Implements the minimum required to push a fully-formed ICT setup as an
**OSO bracket order**: a primary limit at entry, a Stop child for the stop
loss, and a Limit child for the take profit. The two children are linked
OCO automatically by Tradovate when both are present in the OSO body.

Safety rails (intentionally strict; the user must opt out explicitly):

- Hard-fails unless ``TRADOVATE_ENV=demo`` in ``.env``. Pass
  ``allow_live=True`` to override (your responsibility).
- Rejects zero / negative quantity.
- Rejects setups whose stop is on the wrong side of entry for the
  declared direction.
- Sends ``isAutomated=true`` so Tradovate's risk system can tag/throttle.

This module never *decides* to place an order on its own — it's a pure
wrapper around the REST endpoints. The decision lives upstream (live
monitor, manual operator).

Reference: https://api.tradovate.com/#section/Orders
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

from config import Instrument, TRADOVATE_ENV, tradovate_rest_base
from data.tradovate_feed import _resolve_contract_id, authenticate
from execution.base import (
    AccountSnapshot, BrokerAdapter, ExecutionEvent, OpenPosition, PlacedOrder,
)
from risk.sizing import TradePlan
from signals.setup import Setup

log = logging.getLogger(__name__)


class TradovateOrderError(RuntimeError):
    pass


@dataclass
class PlacedBracket:
    """Back-compat alias for ``PlacedOrder``."""
    order_id: int
    raw_response: dict


# ---------------------------------------------------------------------------
def _api_get(path: str, token: str, params: dict | None = None) -> dict:
    r = requests.get(f"{tradovate_rest_base()}{path}",
                     headers={"Authorization": f"Bearer {token}"},
                     params=params or {}, timeout=15)
    r.raise_for_status()
    return r.json()


def _api_post(path: str, token: str, body: dict) -> dict:
    r = requests.post(f"{tradovate_rest_base()}{path}",
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"},
                      json=body, timeout=15)
    if not r.ok:
        raise TradovateOrderError(f"POST {path} → {r.status_code}: {r.text[:300]}")
    return r.json()


def find_account_id(token: str) -> int:
    rows = _api_get("/account/list", token)
    if not rows:
        raise TradovateOrderError("Tradovate returned no accounts for these credentials")
    # Prefer the first active account
    for row in rows:
        if row.get("active", True):
            return int(row["id"])
    return int(rows[0]["id"])


# ---------------------------------------------------------------------------
def place_bracket(
    *,
    contract_id: int,
    side: str,                       # "Buy" or "Sell"
    qty: int,
    entry: float,
    stop: float,
    target: float,
    account_id: Optional[int] = None,
    allow_live: bool = False,
    dry_run: bool = False,
) -> PlacedBracket:
    """Place an OSO bracket order. Returns the primary order id."""
    if qty <= 0:
        raise TradovateOrderError(f"qty must be > 0 (got {qty})")
    if side not in ("Buy", "Sell"):
        raise TradovateOrderError(f"side must be Buy or Sell, got {side!r}")

    is_buy = side == "Buy"
    # Sanity: brackets must wrap the entry on opposite sides
    if is_buy and not (stop < entry < target):
        raise TradovateOrderError(
            f"Buy bracket invalid: need stop({stop}) < entry({entry}) < target({target})")
    if not is_buy and not (target < entry < stop):
        raise TradovateOrderError(
            f"Sell bracket invalid: need target({target}) < entry({entry}) < stop({stop})")

    if TRADOVATE_ENV != "demo" and not allow_live:
        raise TradovateOrderError(
            f"Refusing to place orders in TRADOVATE_ENV={TRADOVATE_ENV} without allow_live=True. "
            f"Set TRADOVATE_ENV=demo in .env (paper) or pass allow_live=True explicitly.")

    token = authenticate().token
    if account_id is None:
        account_id = find_account_id(token)
    accounts = _api_get("/account/list", token)
    account_spec = next((a.get("name", "") for a in accounts if int(a["id"]) == account_id), "")

    opp = "Sell" if is_buy else "Buy"
    body = {
        "accountSpec": account_spec,
        "accountId": account_id,
        "action": side,
        "symbol": str(contract_id),
        "orderQty": int(qty),
        "orderType": "Limit",
        "price": float(entry),
        "isAutomated": True,
        "timeInForce": "GTC",
        "bracket1": {
            "action": opp,
            "orderType": "Stop",
            "stopPrice": float(stop),
            "timeInForce": "GTC",
        },
        "bracket2": {
            "action": opp,
            "orderType": "Limit",
            "price": float(target),
            "timeInForce": "GTC",
        },
    }

    if dry_run:
        log.info("DRY RUN — would POST /order/placeOSO with body: %s", body)
        return PlacedBracket(order_id=0, raw_response={"dry_run": True, "body": body})

    log.info("Placing OSO bracket: %s %s x%d @ %.2f  SL=%.2f  TP=%.2f",
             side, contract_id, qty, entry, stop, target)
    resp = _api_post("/order/placeOSO", token, body)
    if "failureReason" in resp or "errorText" in resp:
        raise TradovateOrderError(f"Tradovate refused order: {resp}")
    order_id = int(resp.get("orderId") or resp.get("id") or 0)
    return PlacedBracket(order_id=order_id, raw_response=resp)


# ---------------------------------------------------------------------------
def place_bracket_for_setup(
    setup: Setup,
    plan: TradePlan,
    instrument: Instrument,
    *,
    account_id: Optional[int] = None,
    allow_live: bool = False,
    dry_run: bool = False,
) -> PlacedBracket:
    """High-level glue: take a Setup + sized TradePlan and place it."""
    if not plan.approved:
        raise TradovateOrderError(f"Refusing un-approved plan: {plan.reason}")
    token = authenticate().token
    contract_id = _resolve_contract_id(token, instrument.symbol)
    side = "Buy" if setup.direction == "bull" else "Sell"
    return place_bracket(
        contract_id=contract_id,
        side=side,
        qty=plan.contracts,
        entry=plan.entry,
        stop=plan.stop,
        target=plan.target,
        account_id=account_id,
        allow_live=allow_live,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# BrokerAdapter implementation (preferred entry point)
# ---------------------------------------------------------------------------
class TradovateAdapter(BrokerAdapter):
    name = "tradovate"

    def place_bracket(self, *, instrument: Instrument, side: str, qty: int,
                      entry: float, stop: float, target: float,
                      account_id=None, allow_live=False, dry_run=False) -> PlacedOrder:
        # Resolve contract on the fly
        token = authenticate().token
        contract_id = _resolve_contract_id(token, instrument.symbol)
        result = place_bracket(
            contract_id=contract_id, side=side, qty=qty,
            entry=entry, stop=stop, target=target,
            account_id=account_id, allow_live=allow_live, dry_run=dry_run,
        )
        return PlacedOrder(order_id=result.order_id, raw_response=result.raw_response)

    def snapshot(self, account_id=None) -> AccountSnapshot:
        token = authenticate().token
        if account_id is None:
            account_id = find_account_id(token)
        accounts = _api_get("/account/list", token)
        acct = next((a for a in accounts if int(a["id"]) == account_id), {})

        # Cash balance
        try:
            bal = _api_get("/cashBalance/getCashBalanceSnapshot", token,
                           params={"accountId": account_id})
        except Exception:
            bal = {}
        cash = float(bal.get("totalCashValue") or bal.get("amount") or 0.0)

        # Positions
        pos_rows = []
        try:
            pos_rows = _api_get("/position/list", token) or []
        except Exception:
            pass

        # Resolve contract names lazily
        positions: list[OpenPosition] = []
        contract_cache: dict[int, str] = {}
        for p in pos_rows:
            if int(p.get("accountId", 0)) != account_id:
                continue
            qty = int(p.get("netPos", 0))
            if qty == 0:
                continue
            cid = int(p.get("contractId", 0))
            sym = contract_cache.get(cid)
            if sym is None:
                try:
                    c = _api_get(f"/contract/item", token, params={"id": cid})
                    sym = (c or {}).get("name", str(cid))
                except Exception:
                    sym = str(cid)
                contract_cache[cid] = sym
            avg = float(p.get("netPrice") or p.get("netPriceAvg") or 0)
            unreal = p.get("openPnl")
            positions.append(OpenPosition(
                symbol=sym, side="Buy" if qty > 0 else "Sell",
                qty=abs(qty), avg_entry=avg,
                unrealised_pnl=float(unreal) if unreal is not None else None,
                raw=p,
            ))
        equity = cash + sum((p.unrealised_pnl or 0) for p in positions)
        return AccountSnapshot(account_id=account_id, cash=cash,
                               equity=equity, positions=positions)

    def list_executions(self, account_id=None, since_ts=None) -> list[ExecutionEvent]:
        token = authenticate().token
        if account_id is None:
            account_id = find_account_id(token)
        # Pull recent execution reports. Tradovate's /executionReport/list
        # supports pagination by id; for simplicity we fetch the latest page
        # and filter by timestamp ourselves.
        try:
            reports = _api_get("/executionReport/list", token) or []
        except Exception:
            return []

        events: list[ExecutionEvent] = []
        for r in reports:
            if int(r.get("accountId", 0)) != account_id:
                continue
            ts = r.get("timestamp") or ""
            if since_ts and ts and ts <= since_ts:
                continue
            kind_raw = (r.get("execType") or r.get("ordStatus") or "").lower()
            if "fill" in kind_raw or kind_raw in ("trade", "filled"):
                kind = "fill"
            elif "partial" in kind_raw:
                kind = "partial"
            elif "cancel" in kind_raw:
                kind = "cancel"
            elif "reject" in kind_raw or "error" in kind_raw:
                kind = "reject"
            else:
                kind = kind_raw or "fill"

            events.append(ExecutionEvent(
                execution_id=str(r.get("id") or r.get("executionId") or ""),
                order_id=str(r.get("orderId") or r.get("origOrderId") or ""),
                parent_order_id=str(r.get("parentOrderId") or "") or None,
                timestamp=str(ts),
                symbol=str(r.get("contract", {}).get("name", "") if isinstance(r.get("contract"), dict) else r.get("symbol", "")),
                side=("Buy" if str(r.get("action") or r.get("side") or "").lower().startswith("b") else "Sell"),
                qty=int(r.get("qty") or r.get("orderQty") or 0),
                price=float(r.get("price") or r.get("avgPrice") or 0.0),
                commission=float(r.get("commission") or 0.0),
                kind=kind,
                raw=r,
            ))
        events.sort(key=lambda e: e.timestamp)
        return events
