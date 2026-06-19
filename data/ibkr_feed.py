"""Interactive Brokers OHLCV feed.

Pulls historical bars for CME/NYMEX/COMEX index/commodity futures from a
running TWS or IB Gateway via ``ib_async`` (falls back to ``ib_insync``).

This module satisfies the same ``get_bars(symbol, timeframe, days)`` contract as
:mod:`data.tradovate_feed` and :mod:`data.yfinance_feed`, so it drops straight
into :func:`data.loader.load_bars`:

    columns ``[open, high, low, close, volume]``
    a tz-aware (UTC) DatetimeIndex named ``timestamp``, sorted ascending.

IBKR is the one source that gives BOTH deep intraday futures history AND live
execution (see :mod:`execution.ibkr_orders`), which is why the bot uses it for
the NQ/ES focus.

PREREQUISITES (do once — see ``python -m data.ibkr_feed --help-setup``):
  1. An IBKR account (paper is fine to start).
  2. TWS or IB Gateway running and logged in.
  3. API enabled: TWS > Global Config > API > Settings > "Enable ActiveX and
     Socket Clients", and add 127.0.0.1 to Trusted IPs.
  4. A CME real-time/historical market-data subscription (~$10-15/mo) so NQ/ES
     historical bars are permitted.

Connection comes from .env (see config.py): IB_HOST, IB_PORT, IB_CLIENT_ID.
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from config import IB_CLIENT_ID, IB_EXCHANGE, IB_HOST, IB_PORT

log = logging.getLogger(__name__)

_EMPTY_COLS = ["open", "high", "low", "close", "volume"]

# our timeframe -> (IBKR barSizeSetting, per-request chunk duration)
_BARSIZE: dict[str, tuple[str, str]] = {
    "1m":  ("1 min",   "2 D"),
    "5m":  ("5 mins",  "1 W"),
    "15m": ("15 mins", "2 W"),
    "1h":  ("1 hour",  "1 M"),
    "4h":  ("4 hours", "2 M"),
    "1d":  ("1 day",   "1 Y"),
}

SETUP_HELP = """\
IBKR SETUP CHECKLIST
--------------------
1. Install + log in to TWS or IB Gateway (paper account is fine to start).
2. Enable the API:  TWS > File > Global Config > API > Settings
     [x] Enable ActiveX and Socket Clients
     Socket port:  7497 (TWS paper) / 4002 (Gateway paper)
     Add 127.0.0.1 to "Trusted IPs".
3. Subscribe to CME data:  Account > Market Data Subscriptions >
     "CME Real-Time (NP,L1)"  (~$10-15/mo).  Without it, historical NQ/ES
     requests return 'No market data permissions'.
4. Point the bot at your session in .env:
     BROKER=ibkr
     IB_PORT=7497        # or 4002 for Gateway paper
   Then:  python -m data.ibkr_feed --symbol NQ --timeframe 15m --days 30
"""


def _ib_lib():
    """Use the maintained ``ib_async`` if present (py3.10+), else ``ib_insync``."""
    try:
        import ib_async as lib
    except ImportError:
        try:
            import ib_insync as lib
        except ImportError as e:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "IBKR feed needs 'ib_async' (preferred) or 'ib_insync'. "
                "Install with: pip install ib_async"
            ) from e
    return lib


def _connect():
    lib = _ib_lib()
    ib = lib.IB()
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=20)
    return ib, lib


def get_bars(symbol: str, timeframe: str, days: int = 30) -> pd.DataFrame:
    """Download historical bars for a futures root from a running TWS/Gateway.

    Returns the bot-standard OHLCV frame (lowercase columns, UTC index named
    ``timestamp``). Paginates backwards until ``days`` of history is covered.
    """
    sym = symbol.upper()
    if timeframe not in _BARSIZE:
        raise ValueError(f"IBKR feed supports {list(_BARSIZE)}, not {timeframe!r}.")
    exch = IB_EXCHANGE.get(sym)
    if exch is None:
        raise ValueError(
            f"{sym} is not an IBKR-routable futures root "
            f"(known: {sorted(IB_EXCHANGE)}). Use source='yfinance' for this symbol."
        )

    bar_size, chunk = _BARSIZE[timeframe]
    start_ts = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=days)
    end_ts = pd.Timestamp.utcnow().tz_localize(None)

    ib, lib = _connect()
    try:
        contract = lib.ContFuture(sym, exch)          # continuous front-month
        ib.qualifyContracts(contract)

        frames: list[pd.DataFrame] = []
        cursor = end_ts
        while cursor > start_ts:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=cursor.strftime("%Y%m%d %H:%M:%S"),
                durationStr=chunk,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
            )
            if not bars:
                break
            df = lib.util.df(bars)
            frames.append(df)
            earliest = pd.Timestamp(df["date"].iloc[0])
            if earliest <= start_ts or earliest >= cursor:
                break
            cursor = earliest
            time.sleep(0.4)                            # respect IBKR pacing limits
    finally:
        ib.disconnect()

    if not frames:
        log.error("IBKR returned no data for %s %s — check TWS/Gateway is running "
                  "and CME data is subscribed.", sym, timeframe)
        return pd.DataFrame(columns=_EMPTY_COLS)

    out = pd.concat(frames).drop_duplicates(subset="date").sort_values("date")
    idx = pd.to_datetime(out["date"], utc=True)
    out = out[["open", "high", "low", "close", "volume"]].astype(float)
    out.index = idx
    out.index.name = "timestamp"
    out = out[(out.index >= start_ts.tz_localize("UTC")) &
              (out.index <= end_ts.tz_localize("UTC"))]
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="IBKR futures OHLCV feed")
    ap.add_argument("--symbol", default="NQ")
    ap.add_argument("--timeframe", default="15m")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--help-setup", action="store_true")
    args = ap.parse_args()
    if args.help_setup:
        print(SETUP_HELP)
        raise SystemExit
    logging.basicConfig(level=logging.INFO)
    frame = get_bars(args.symbol, args.timeframe, args.days)
    if frame.empty:
        print(f"{args.symbol} {args.timeframe}: no bars")
    else:
        print(f"{args.symbol} {args.timeframe}: {len(frame)} bars  "
              f"{frame.index.min()} -> {frame.index.max()}")
