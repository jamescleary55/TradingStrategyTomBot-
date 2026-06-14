"""Tradovate REST + WebSocket client.

Two entry points:

- ``get_bars(symbol, timeframe, days)`` — historical OHLCV bars as a pandas
  DataFrame indexed by UTC timestamp. Uses the chart request via WebSocket
  (Tradovate's standard mechanism for both historical and live data).

- ``MarketDataClient`` — long-lived class that can subscribe to live bar
  updates and call a user callback for each completed bar.

When Tradovate credentials are missing from .env, ``get_bars`` falls back to
a deterministic synthetic NQ-like series so the backtest can still run.

Tradovate protocol notes
------------------------
The market data WebSocket uses a simple ``op\\n<id>\\n<query>\\n<body>``
frame format. Server responses come back wrapped in a SockJS-style envelope:
``a[{"s": 200, "i": <id>, "d": <data>}]``. Heartbeats (``[]``) must be sent
every ~2.5 seconds.

Reference: https://api.tradovate.com/#section/Websocket-API
"""
from __future__ import annotations

import json
import logging
import threading
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import numpy as np
import pandas as pd
import requests
import websocket

from config import (
    TRADOVATE_APP_ID,
    TRADOVATE_APP_VERSION,
    TRADOVATE_CID,
    TRADOVATE_PASSWORD,
    TRADOVATE_SECRET,
    TRADOVATE_USERNAME,
    has_tradovate_credentials,
    tradovate_md_ws,
    tradovate_rest_base,
)

log = logging.getLogger(__name__)

TIMEFRAME_TO_CHARTREQ: dict[str, dict] = {
    "1m":  {"underlyingType": "MinuteBar", "elementSize": 1,  "elementSizeUnit": "UnderlyingUnits"},
    "5m":  {"underlyingType": "MinuteBar", "elementSize": 5,  "elementSizeUnit": "UnderlyingUnits"},
    "15m": {"underlyingType": "MinuteBar", "elementSize": 15, "elementSizeUnit": "UnderlyingUnits"},
    "1h":  {"underlyingType": "MinuteBar", "elementSize": 60, "elementSizeUnit": "UnderlyingUnits"},
    "4h":  {"underlyingType": "MinuteBar", "elementSize": 240, "elementSizeUnit": "UnderlyingUnits"},
    "1d":  {"underlyingType": "DailyBar",  "elementSize": 1,  "elementSizeUnit": "UnderlyingUnits"},
}


# ---------------------------------------------------------------------------
# REST: auth
# ---------------------------------------------------------------------------
@dataclass
class AccessToken:
    token: str
    md_token: str
    expires: datetime


def authenticate() -> AccessToken:
    """POST credentials to Tradovate and return access + market-data tokens."""
    if not has_tradovate_credentials():
        raise RuntimeError("Missing Tradovate credentials in .env")

    body = {
        "name": TRADOVATE_USERNAME,
        "password": TRADOVATE_PASSWORD,
        "appId": TRADOVATE_APP_ID,
        "appVersion": TRADOVATE_APP_VERSION,
        "cid": int(TRADOVATE_CID) if TRADOVATE_CID.isdigit() else TRADOVATE_CID,
        "sec": TRADOVATE_SECRET,
    }
    r = requests.post(f"{tradovate_rest_base()}/auth/accessTokenRequest", json=body, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "errorText" in data:
        raise RuntimeError(f"Tradovate auth failed: {data['errorText']}")
    expires = datetime.fromisoformat(data["expirationTime"].replace("Z", "+00:00"))
    return AccessToken(
        token=data["accessToken"],
        md_token=data.get("mdAccessToken", data["accessToken"]),
        expires=expires,
    )


def _resolve_contract_id(token: str, root_symbol: str) -> int:
    """Look up the front-month contract id for a root (e.g. 'NQ' → NQU5 → 12345)."""
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(
        f"{tradovate_rest_base()}/contract/suggest",
        params={"t": root_symbol, "l": 10},
        headers=headers,
        timeout=15,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise RuntimeError(f"No contracts returned for symbol '{root_symbol}'")
    # First active contract for the requested root
    for row in rows:
        if row.get("name", "").startswith(root_symbol):
            return row["id"]
    return rows[0]["id"]


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------
class MarketDataClient:
    """Lightweight Tradovate market-data WebSocket client.

    Usage:
        client = MarketDataClient()
        client.connect()
        client.subscribe_bars("NQ", "15m", on_bar=lambda bar: print(bar))
        client.run_forever()
    """

    def __init__(self):
        self.ws: Optional[websocket.WebSocketApp] = None
        self.token: Optional[AccessToken] = None
        self._req_id = 0
        self._handlers: dict[int, Callable] = {}
        self._lock = threading.Lock()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ----- connection lifecycle -----
    def connect(self) -> None:
        self.token = authenticate()
        url = tradovate_md_ws()
        self.ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=lambda ws, e: log.error("WS error: %s", e),
            on_close=lambda ws, code, msg: log.info("WS closed: %s %s", code, msg),
        )

    def run_forever(self) -> None:
        if not self.ws:
            raise RuntimeError("Call connect() first")
        try:
            self.ws.run_forever(ping_interval=30, ping_timeout=10)
        finally:
            self._stop.set()

    def close(self) -> None:
        self._stop.set()
        if self.ws:
            self.ws.close()

    # ----- framing helpers -----
    def _next_id(self) -> int:
        with self._lock:
            self._req_id += 1
            return self._req_id

    def _send_op(self, op: str, body: dict, on_response: Optional[Callable] = None) -> int:
        rid = self._next_id()
        frame = f"{op}\n{rid}\n\n{json.dumps(body)}"
        if on_response:
            self._handlers[rid] = on_response
        assert self.ws and self.ws.sock
        self.ws.send(frame)
        return rid

    def _on_open(self, ws):
        log.info("Tradovate WS open; authorizing")
        ws.send(f"authorize\n{self._next_id()}\n\n{self.token.md_token}")
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            try:
                if self.ws and self.ws.sock and self.ws.sock.connected:
                    self.ws.send("[]")
            except Exception:
                break
            self._stop.wait(2.5)

    def _on_message(self, ws, msg: str):
        if not msg:
            return
        first = msg[0]
        if first == "o":         # connection open
            return
        if first == "h":         # heartbeat
            return
        if first == "c":         # closed
            log.info("Server close frame: %s", msg)
            return
        if first == "a":         # array of messages
            try:
                payload = json.loads(msg[1:])
            except json.JSONDecodeError:
                return
            for m in payload:
                self._dispatch(m)

    def _dispatch(self, m: dict):
        # Response to a numbered request
        if "i" in m and m["i"] in self._handlers:
            try:
                self._handlers[m["i"]](m)
            except Exception:
                log.exception("response handler error")
            return
        # Event / push (e.g. live bar update)
        if "e" in m:
            ev = m["e"]
            for h in list(self._handlers.values()):
                try:
                    h(m)
                except Exception:
                    log.exception("event handler error for %s", ev)

    # ----- subscriptions -----
    def subscribe_bars(self, symbol: str, timeframe: str, on_bar: Callable[[dict], None],
                       history_bars: int = 200) -> None:
        """Subscribe to historical + live bars for ``symbol`` at ``timeframe``."""
        cid = _resolve_contract_id(self.token.token, symbol)
        if timeframe not in TIMEFRAME_TO_CHARTREQ:
            raise ValueError(f"Unsupported timeframe '{timeframe}'")
        chart_desc = TIMEFRAME_TO_CHARTREQ[timeframe]
        body = {
            "symbol": str(cid),
            "chartDescription": chart_desc,
            "timeRange": {"asMuchAsElements": history_bars},
        }
        self._send_op("md/getChart", body, on_response=on_bar)


# ---------------------------------------------------------------------------
# get_bars: blocking historical fetch
# ---------------------------------------------------------------------------
def get_bars(symbol: str, timeframe: str, days: int = 30) -> pd.DataFrame:
    """Return ``days`` worth of OHLCV bars for ``symbol`` at ``timeframe``.

    Falls back to a deterministic synthetic series when no credentials are
    available so the rest of the pipeline can be developed offline.
    """
    if not has_tradovate_credentials():
        log.warning("Tradovate credentials missing — generating synthetic %s %s data", symbol, timeframe)
        return _synthetic_bars(symbol, timeframe, days)

    # Approx total bars to request
    minutes_per_bar = _minutes_per_bar(timeframe)
    elements = max(1, int((days * 24 * 60) / minutes_per_bar))

    client = MarketDataClient()
    client.connect()

    result_holder: dict = {"bars": [], "done": False, "error": None}

    def on_msg(m: dict):
        d = m.get("d") or {}
        bars = d.get("bars") or []
        for b in bars:
            result_holder["bars"].append(b)
        # End-of-history flag
        if d.get("eoh") or d.get("eoH") or m.get("s") == 200 and "bars" in d:
            result_holder["done"] = True

    def runner():
        try:
            client.subscribe_bars(symbol, timeframe, on_bar=on_msg, history_bars=elements)
            client.run_forever()
        except Exception as e:
            result_holder["error"] = e

    t = threading.Thread(target=runner, daemon=True)
    t.start()

    waited = 0.0
    while not result_holder["done"] and waited < 30 and not result_holder["error"]:
        time_mod.sleep(0.25)
        waited += 0.25
    client.close()

    if result_holder["error"]:
        raise result_holder["error"]
    return _bars_to_df(result_holder["bars"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _minutes_per_bar(tf: str) -> int:
    return {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 60 * 24}[tf]


def _bars_to_df(bars: list[dict]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    rows = []
    for b in bars:
        # Tradovate uses fields: timestamp (ISO), open, high, low, close, upVolume, downVolume, upTicks, downTicks
        ts = b.get("timestamp") or b.get("t")
        try:
            ts = pd.to_datetime(ts, utc=True)
        except Exception:
            continue
        rows.append({
            "open":   float(b.get("open",  b.get("o", 0))),
            "high":   float(b.get("high",  b.get("h", 0))),
            "low":    float(b.get("low",   b.get("l", 0))),
            "close":  float(b.get("close", b.get("c", 0))),
            "volume": float((b.get("upVolume", 0) or 0) + (b.get("downVolume", 0) or 0)) or float(b.get("v", 0)),
        })
        rows[-1]["_ts"] = ts
    df = pd.DataFrame(rows).set_index("_ts").sort_index()
    df.index.name = "timestamp"
    return df


def _synthetic_bars(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    """Generate a synthetic NQ-like OHLCV series for offline development."""
    minutes = _minutes_per_bar(timeframe)
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % minutes)
    n = int((days * 24 * 60) / minutes)
    idx = pd.date_range(end=end, periods=n, freq=f"{minutes}min", tz="UTC")

    rng = np.random.default_rng(seed=hash(f"{symbol}_{timeframe}_{days}") & 0xffffffff)
    # Random walk in log-returns with intraday vol clustering
    vol = 0.0015
    drift = 0.00002
    returns = rng.normal(loc=drift, scale=vol, size=n)
    # Inject occasional impulses to create FVGs and sweeps
    impulse_mask = rng.random(n) < 0.02
    returns[impulse_mask] *= rng.choice([-5, 5], size=impulse_mask.sum())
    log_prices = 17500 * np.exp(np.cumsum(returns))
    close = log_prices

    # Build OHLC around close
    open_ = np.concatenate(([close[0]], close[:-1]))
    spread = np.abs(rng.normal(0, vol * 0.6, size=n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    # Occasional wicks beyond high/low (good for sweep detection)
    wick_mask = rng.random(n) < 0.05
    high[wick_mask] += spread[wick_mask] * 2
    low[wick_mask] -= spread[wick_mask] * 2
    volume = rng.integers(500, 8000, size=n).astype(float)

    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )
    df.index.name = "timestamp"
    # Snap to tick size (0.25 for NQ)
    df[["open", "high", "low", "close"]] = (df[["open", "high", "low", "close"]] / 0.25).round() * 0.25
    return df
