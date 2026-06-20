"""TopstepX (ProjectX) broker adapter — stub.

TopstepX uses ProjectX's API, which is similar in spirit to Tradovate's
but distinct. This is a **stub** that implements the auth + place-order
calls following ProjectX's published format; it has *not* been smoke
tested against a live demo account because that requires you to drop
your TopstepX credentials in ``.env``.

To wire it up:

1. Add to .env::

       BROKER=topstepx
       PROJECTX_USERNAME=...
       PROJECTX_API_KEY=...
       PROJECTX_ACCOUNT_ID=...
       PROJECTX_BASE=https://api.topstepx.com   # demo same as live for this API

2. Run ``python -m live.monitor --symbols MNQ --auto-execute --execute-dry-run``
   — the dry-run flag will print the order body without sending so you
   can verify the payload shape before going live.

ProjectX API ref: https://api.docs.projectx.com/
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from config import Instrument
from execution.base import (
    AccountSnapshot, BrokerAdapter, ExecutionEvent, OpenPosition, PlacedOrder,
)

log = logging.getLogger(__name__)


class TopstepXError(RuntimeError):
    pass


def _api_url() -> str:
    return os.getenv("PROJECTX_BASE", "https://api.topstepx.com").rstrip("/")


def _authenticate() -> str:
    """POST /api/Auth/loginKey → return Bearer token."""
    username = os.getenv("PROJECTX_USERNAME", "")
    api_key = os.getenv("PROJECTX_API_KEY", "")
    if not username or not api_key:
        raise TopstepXError("Missing PROJECTX_USERNAME / PROJECTX_API_KEY in .env")
    r = requests.post(
        f"{_api_url()}/api/Auth/loginKey",
        json={"userName": username, "apiKey": api_key},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("success") or not data.get("token"):
        raise TopstepXError(f"Auth failed: {data}")
    return data["token"]


def _account_id() -> int:
    aid = os.getenv("PROJECTX_ACCOUNT_ID", "")
    if not aid:
        raise TopstepXError("Set PROJECTX_ACCOUNT_ID in .env")
    return int(aid)


def _resolve_contract(token: str, root: str) -> int:
    """POST /api/Contract/search → first active contract for the root."""
    r = requests.post(
        f"{_api_url()}/api/Contract/search",
        json={"searchText": root, "live": False},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    r.raise_for_status()
    contracts = r.json().get("contracts") or []
    if not contracts:
        raise TopstepXError(f"No contracts for {root!r}")
    for c in contracts:
        if c.get("activeContract"):
            return int(c["id"])
    return int(contracts[0]["id"])


class TopstepXAdapter(BrokerAdapter):
    """ProjectX-style broker (TopstepX, Tradovate API isn't used here)."""

    name = "topstepx"

    def place_bracket(self, *, instrument: Instrument, side: str, qty: int,
                      entry: float, stop: float, target: float,
                      account_id: Optional[int] = None, allow_live: bool = False,
                      dry_run: bool = False) -> PlacedOrder:
        if qty <= 0:
            raise TopstepXError(f"qty must be > 0 (got {qty})")
        if side not in ("Buy", "Sell"):
            raise TopstepXError(f"side must be Buy or Sell, got {side!r}")

        is_buy = side == "Buy"
        if is_buy and not (stop < entry < target):
            raise TopstepXError(
                f"Buy bracket invalid: stop({stop}) < entry({entry}) < target({target})")
        if not is_buy and not (target < entry < stop):
            raise TopstepXError(
                f"Sell bracket invalid: target({target}) < entry({entry}) < stop({stop})")

        # ProjectX type codes: 1=Limit, 4=Stop. Side: 0=Bid (Buy), 1=Ask (Sell)
        type_limit, type_stop = 1, 4
        px_side = 0 if is_buy else 1
        body = {
            "accountId": account_id or _account_id(),
            "type": type_limit,
            "side": px_side,
            "size": int(qty),
            "limitPrice": float(entry),
            "stopPrice": None,
            "trailPrice": None,
            "customTag": "ict-futures-bot",
            "linkedOrderId": None,
            "bracket": {
                "stopLoss": {"type": type_stop, "price": float(stop)},
                "takeProfit": {"type": type_limit, "price": float(target)},
            },
        }

        if dry_run:
            log.info("DRY RUN — would POST /api/Order/place with body: %s", body)
            return PlacedOrder(order_id=0, raw_response={"dry_run": True, "body": body})

        token = _authenticate()
        body["contractId"] = _resolve_contract(token, instrument.symbol)

        log.info("Placing TopstepX bracket: %s %s x%d @ %.2f  SL=%.2f  TP=%.2f",
                 side, instrument.symbol, qty, entry, stop, target)
        r = requests.post(
            f"{_api_url()}/api/Order/place",
            json=body,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            timeout=15,
        )
        if not r.ok:
            raise TopstepXError(f"placeOrder → {r.status_code}: {r.text[:300]}")
        data = r.json()
        order_id = int(data.get("orderId") or data.get("id") or 0)
        return PlacedOrder(order_id=order_id, raw_response=data)

    # ----- Read-only methods (paper-validation phase) --------------------
    def _api_post(self, path: str, token: str, body: Optional[dict] = None) -> dict:
        r = requests.post(
            f"{_api_url()}{path}",
            json=body or {},
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            timeout=15,
        )
        if not r.ok:
            raise TopstepXError(f"POST {path} → {r.status_code}: {r.text[:300]}")
        return r.json() or {}

    def snapshot(self, account_id: Optional[int] = None) -> AccountSnapshot:
        token = _authenticate()
        aid = account_id or _account_id()
        # POST /api/Account/search returns accounts the key can see.
        accts = self._api_post("/api/Account/search", token, {"onlyActiveAccounts": True})
        acct = next((a for a in (accts.get("accounts") or [])
                     if int(a.get("id", 0)) == aid),
                    None)
        if acct is None:
            raise TopstepXError(f"account id {aid} not visible to this key")
        cash = float(acct.get("balance") or acct.get("equity") or 0.0)

        # Open positions
        pos_payload = self._api_post("/api/Position/searchOpen", token,
                                     {"accountId": aid})
        positions: list[OpenPosition] = []
        for p in (pos_payload.get("positions") or []):
            qty = int(p.get("size") or p.get("netPos") or 0)
            if qty == 0:
                continue
            positions.append(OpenPosition(
                symbol=str(p.get("contractName") or p.get("symbolName") or p.get("contractId", "")),
                side="Buy" if qty > 0 else "Sell",
                qty=abs(qty),
                avg_entry=float(p.get("averagePrice") or p.get("avgPrice") or 0.0),
                unrealised_pnl=float(p["unrealizedPnL"]) if "unrealizedPnL" in p else None,
                raw=p,
            ))
        equity = cash + sum(p.unrealised_pnl or 0 for p in positions)
        # account_id is a string in AccountSnapshot; ProjectX/TopstepX's native
        # id is numeric, so stringify it at the boundary (no coercion of ids).
        return AccountSnapshot(account_id=str(aid), cash=cash,
                               equity=equity, positions=positions)

    def list_executions(self, account_id: Optional[int] = None,
                        since_ts: Optional[str] = None) -> list[ExecutionEvent]:
        """Returns fill / partial / reject / cancel events since `since_ts`.

        ProjectX exposes /api/Trade/search for filled trades and
        /api/Order/search for order status. We pull from /api/Trade/search.
        """
        token = _authenticate()
        aid = account_id or _account_id()
        body: dict = {"accountId": aid}
        if since_ts:
            body["startTimestamp"] = since_ts
        payload = self._api_post("/api/Trade/search", token, body)
        events: list[ExecutionEvent] = []
        for t in (payload.get("trades") or []):
            ts = t.get("creationTimestamp") or t.get("timestamp") or ""
            if since_ts and ts and ts <= since_ts:
                continue
            side_raw = (t.get("side") or "").lower()
            # ProjectX: 0/Bid = Buy, 1/Ask = Sell
            side = "Buy" if side_raw in ("0", "bid", "buy", "b") else "Sell"
            events.append(ExecutionEvent(
                execution_id=str(t.get("id") or t.get("tradeId") or ""),
                order_id=str(t.get("orderId") or ""),
                parent_order_id=str(t.get("linkedOrderId") or "") or None,
                timestamp=str(ts),
                symbol=str(t.get("contractName") or t.get("contractId", "")),
                side=side,
                qty=int(t.get("size") or t.get("qty") or 0),
                price=float(t.get("price") or 0.0),
                commission=float(t.get("commission") or t.get("fee") or 0.0),
                kind="fill",
                raw=t,
            ))
        events.sort(key=lambda e: e.timestamp)
        return events

    def list_orders(self, account_id: Optional[int] = None,
                    open_only: bool = False) -> list[dict]:
        """Open / historical orders. Returns raw ProjectX rows (not normalised).
        Useful for the operator's smoke test."""
        token = _authenticate()
        aid = account_id or _account_id()
        path = "/api/Order/searchOpen" if open_only else "/api/Order/search"
        payload = self._api_post(path, token, {"accountId": aid})
        return list(payload.get("orders") or [])

    def list_positions(self, account_id: Optional[int] = None) -> list[dict]:
        """Raw open positions. The :meth:`snapshot` method normalises them."""
        token = _authenticate()
        aid = account_id or _account_id()
        payload = self._api_post("/api/Position/searchOpen", token,
                                 {"accountId": aid})
        return list(payload.get("positions") or [])
