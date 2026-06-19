"""Central configuration: session times, instruments, risk parameters.

All session times are in US Eastern Time (ET) per ICT convention.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Sessions (US Eastern Time)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Session:
    name: str
    start: time
    end: time


SESSIONS: dict[str, Session] = {
    "ASIA":   Session("Asia",   time(20, 0), time(0, 0)),   # 20:00–24:00 ET
    "LONDON": Session("London", time(2, 0),  time(5, 0)),   # 02:00–05:00 ET
    "NY_AM":  Session("NY AM",  time(7, 0),  time(11, 0)),  # 07:00–11:00 ET
    "NY_PM":  Session("NY PM",  time(13, 30), time(16, 0)),
}

KILLZONES = ("LONDON", "NY_AM")
SESSION_TZ = "America/New_York"


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Instrument:
    symbol: str          # Tradovate root symbol (front-month resolved separately)
    description: str
    tick_size: float
    tick_value: float    # USD per tick per contract
    point_value: float   # USD per 1.0 price move per contract


INSTRUMENTS: dict[str, Instrument] = {
    "NQ":  Instrument("NQ",  "E-mini Nasdaq-100",    0.25,  5.00,  20.00),
    "MNQ": Instrument("MNQ", "Micro E-mini Nasdaq",  0.25,  0.50,   2.00),
    "ES":  Instrument("ES",  "E-mini S&P 500",       0.25, 12.50,  50.00),
    "MES": Instrument("MES", "Micro E-mini S&P 500", 0.25,  1.25,   5.00),
    "CL":  Instrument("CL",  "Crude Oil",            0.01, 10.00, 1000.00),
    "MCL": Instrument("MCL", "Micro Crude Oil",      0.01,  1.00,  100.00),
    "GC":  Instrument("GC",  "Gold",                 0.10, 10.00,  100.00),
    "MGC": Instrument("MGC", "Micro Gold",           0.10,  1.00,   10.00),
    # --- Crypto (perp-style; sizing here is for R-multiple math only) ---
    # Treat 1 contract = 0.01 base coin. point_value = 0.01 USD per 1 USD price move.
    # The simulator uses these to derive USD figures; R-multiple metrics are
    # invariant to this choice. asset_class is mapped to 'stock' so the existing
    # CHECK constraint passes — the bot is crypto-agnostic structurally.
    "BTCUSDT": Instrument("BTCUSDT", "Bitcoin perp (synthetic)",  0.10, 0.001,  0.01),
    "ETHUSDT": Instrument("ETHUSDT", "Ether perp (synthetic)",    0.01, 0.0001, 0.01),
    "SOLUSDT": Instrument("SOLUSDT", "Solana perp (synthetic)",   0.01, 0.0001, 0.01),
    "BNBUSDT": Instrument("BNBUSDT", "BNB perp (synthetic)",      0.01, 0.0001, 0.01),
}

DEFAULT_SYMBOL = "NQ"
DEFAULT_TIMEFRAME = "15m"


# ---------------------------------------------------------------------------
# Detector parameters
# ---------------------------------------------------------------------------
SWING_LOOKBACK = 5           # bars on each side for swing high/low
EQUAL_LEVEL_TOLERANCE = 0.0025  # 0.25% — for equal highs/lows
SWEEP_REENTRY_BARS = 1       # bars allowed for close-back-inside on sweep


# ---------------------------------------------------------------------------
# Risk parameters
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RiskConfig:
    max_risk_per_trade_pct: float    # e.g. 0.005 = 0.5% of equity
    max_daily_loss_pct: float
    max_concurrent_positions: int
    default_rr: float                # R-multiple target


RISK = RiskConfig(
    max_risk_per_trade_pct=0.005,
    max_daily_loss_pct=0.02,
    max_concurrent_positions=1,
    default_rr=2.0,
)


# ---------------------------------------------------------------------------
# Setup detector parameters
# ---------------------------------------------------------------------------
HTF_BIAS_LOOKBACK_BOS = 4   # last N BOS events used to infer HTF bias
SWEEP_TO_CHOCH_MAX_BARS = 10
CHOCH_TO_FVG_MAX_BARS = 6
SETUP_MIN_RR = 1.5
SETUP_TARGET_MODE = "rr"    # "rr" → entry +/- RR * risk; "liquidity" → next opposing liquidity
SETUP_ENTRY_MODE = "mid"    # "mid" / "closer_edge" / "farther_edge" — where in the FVG to enter
SETUP_MAX_STOP_POINTS = 0   # 0 = disabled; otherwise reject setups whose |entry-stop| exceeds N points

# Walk-forward simulator
ENTRY_TIMEOUT_BARS = 12     # cancel pending limit if not filled within N bars
SLIPPAGE_TICKS = 1          # adverse fill assumption on entry + exit
COMMISSION_PER_CONTRACT_USD = 4.0  # round-trip per contract (entry + exit)


# ---------------------------------------------------------------------------
# Tradovate credentials (from .env)
# ---------------------------------------------------------------------------
TRADOVATE_USERNAME = os.getenv("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD = os.getenv("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID = os.getenv("TRADOVATE_APP_ID", "ict-futures-bot")
TRADOVATE_APP_VERSION = os.getenv("TRADOVATE_APP_VERSION", "0.1.0")
TRADOVATE_CID = os.getenv("TRADOVATE_CID", "")
TRADOVATE_SECRET = os.getenv("TRADOVATE_SECRET", "")
TRADOVATE_ENV = os.getenv("TRADOVATE_ENV", "demo").lower()


def tradovate_rest_base() -> str:
    return "https://demo.tradovateapi.com/v1" if TRADOVATE_ENV == "demo" else "https://live.tradovateapi.com/v1"


def tradovate_md_ws() -> str:
    # Market data WebSocket. Same host for demo/live.
    return "wss://md.tradovateapi.com/v1/websocket"


def has_tradovate_credentials() -> bool:
    return bool(TRADOVATE_USERNAME and TRADOVATE_PASSWORD and TRADOVATE_CID and TRADOVATE_SECRET)


# ---------------------------------------------------------------------------
# Interactive Brokers (IBKR) connection (from .env)
# ---------------------------------------------------------------------------
# IBKR is reached over a local socket to a running TWS or IB Gateway — there is
# no API username/secret in .env (you log in inside TWS/Gateway). "Credentials"
# here therefore means: the user has opted into IBKR and a socket port is set.
# Common ports: 7497 TWS paper | 7496 TWS live | 4002 Gateway paper | 4001 Gateway live
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "7497"))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "17"))

# Paper vs live is inferred from the port (paper = 7497 TWS / 4002 Gateway).
# Set IB_ENV=live explicitly to bypass the paper-only rail in execution/ibkr_orders.py.
IB_ENV = os.getenv("IB_ENV", "").strip().lower() or (
    "paper" if IB_PORT in (7497, 4002) else "live"
)

# IBKR exchange per instrument root. Index futures = CME; energy = NYMEX;
# metals = COMEX. Crypto roots are not IBKR-routable and stay on yfinance/synthetic.
IB_EXCHANGE: dict[str, str] = {
    "NQ": "CME", "MNQ": "CME", "ES": "CME", "MES": "CME",
    "CL": "NYMEX", "MCL": "NYMEX", "GC": "COMEX", "MGC": "COMEX",
}


def has_ibkr_credentials() -> bool:
    """IBKR has no API secret, so 'configured' means the user opted in via .env.

    True when ``BROKER=ibkr`` or an ``IB_PORT`` is explicitly set. We can't prove
    the socket is actually live without connecting; the loader and order adapter
    surface the real connection error if TWS/Gateway isn't running.
    """
    return os.getenv("BROKER", "").strip().lower() == "ibkr" or bool(os.getenv("IB_PORT"))
