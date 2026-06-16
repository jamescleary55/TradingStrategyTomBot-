"""
Central configuration for the ICT trading system.

All tunable parameters live here so the strategy, backtester and optimiser
can share a single source of truth.
"""
from dataclasses import dataclass, field
from typing import Dict


# --------------------------------------------------------------------------- #
#  Symbol routing
# --------------------------------------------------------------------------- #
# Maps the user-facing symbol to the ticker used by each data source.
SYMBOL_MAP: Dict[str, Dict[str, str]] = {
    "BTCUSDT": {"binance": "BTCUSDT", "yfinance": "BTC-USD", "fmp": "BTCUSD"},
    "ETHUSDT": {"binance": "ETHUSDT", "yfinance": "ETH-USD", "fmp": "ETHUSD"},
    "EURUSD":  {"binance": None,      "yfinance": "EURUSD=X", "fmp": "EURUSD"},
    "SPY":     {"binance": None,      "yfinance": "SPY",      "fmp": "SPY"},
    # Index futures (continuous front-month via Yahoo). FMP free tier blocks
    # NQ; ES is available as ESUSD on FMP if you ever need a daily cross-check.
    # "ibkr" entries route to data/ibkr.py (real CME futures, data + execution).
    "NQ":      {"binance": None, "yfinance": "NQ=F", "fmp": None,    "ibkr": "NQ"},
    "ES":      {"binance": None, "yfinance": "ES=F", "fmp": "ESUSD", "ibkr": "ES"},
}

# Futures contract specs (CME index futures) -> dollar P&L + realistic sizing.
# point_value = $ per 1.0 index point.  Margins are approximate and change;
# treat as a sanity bound, not gospel.
CONTRACT_SPECS = {
    "NQ":  {"point_value": 20.0, "tick_size": 0.25, "init_margin": 23000.0, "exchange": "CME"},
    "ES":  {"point_value": 50.0, "tick_size": 0.25, "init_margin": 16000.0, "exchange": "CME"},
    "MNQ": {"point_value": 2.0,  "tick_size": 0.25, "init_margin": 2300.0,  "exchange": "CME"},
    "MES": {"point_value": 5.0,  "tick_size": 0.25, "init_margin": 1600.0,  "exchange": "CME"},
}


def contracts_for_risk(symbol: str, equity: float, risk_per_trade: float,
                       stop_distance_points: float) -> int:
    """Whole-contract size so a stop-out loses ~risk_per_trade of equity.

    contracts = (equity * risk%) / (stop_distance_in_points * point_value)
    Returns 0 when the risk budget can't afford even one contract.
    """
    spec = CONTRACT_SPECS.get(symbol.upper())
    if spec is None or stop_distance_points <= 0:
        return 0
    risk_dollars = equity * risk_per_trade
    risk_per_contract = stop_distance_points * spec["point_value"]
    return max(0, int(risk_dollars // risk_per_contract))

# Symbols that are FX/equity/futures only -> force yfinance.
DEFAULT_SOURCE = {
    "BTCUSDT": "binance",
    "ETHUSDT": "binance",
    "EURUSD":  "yfinance",
    "SPY":     "yfinance",
    "NQ":      "yfinance",
    "ES":      "yfinance",
}


@dataclass
class StrategyParams:
    """Tunable parameters for ICT signal generation and risk management."""

    # --- Market structure --------------------------------------------------
    swing_left: int = 3          # bars to the left of a swing pivot
    swing_right: int = 3         # bars to the right of a swing pivot

    # --- Liquidity ---------------------------------------------------------
    liquidity_lookback: int = 20     # how far back to search for equal H/L
    equal_tolerance: float = 0.0010  # max % distance to call two levels "equal"
    sweep_lookback: int = 30         # validity window of a sweep (bars)

    # --- Fair Value Gaps ---------------------------------------------------
    min_fvg_size: float = 0.0008     # min gap height as a fraction of price
    fvg_lookback: int = 20           # how recent a FVG must be to trade

    # --- Confluence windows ------------------------------------------------
    choch_lookback: int = 20         # CHoCH must be this recent to trade

    # --- Signal quality filters -------------------------------------------
    # NOTE: defaults below were chosen from an ablation on BTC/ETH 1h
    # (2023-06 -> 2024-06).  The cooldown reduced overtrading and improved
    # BTC PF 1.38 -> 1.50 with no real ETH cost, so it is ON.  Strict event
    # sequencing and the EMA bias filter *reduced* returns on both assets in
    # that test, so they default OFF — keep them as opt-in knobs to explore
    # per asset/timeframe (e.g. via the optimiser) rather than as a blanket on.
    require_sequence: bool = False   # enforce ICT order: sweep -> CHoCH -> FVG
    bias_ema: int = 0                # HTF trend bias EMA period (0 = disabled)
    signal_cooldown: int = 6         # min bars between consecutive signals
    allow_long: bool = True          # take BUY setups
    allow_short: bool = True         # emit SELL setups (kept on for ANALYSIS;
                                     # execution is governed by `long_only`)
    long_only: bool = True           # INDEX MODE (NQ/ES): block ALL short
                                     # EXECUTION. Bearish setups are still
                                     # detected/labelled for analysis, but the
                                     # backtester never submits a sell order.
    news_window_min: int = 0         # block entries +/- this many minutes around
                                     # a high-impact news event (0 = no filter)
    # ICT killzones: only take signals whose bar (UTC hour) falls in one of
    # these [start, end) ranges.  Empty = trade all hours (DEFAULT).
    # Tested honestly: restricting to the canonical London/NY killzones REDUCED
    # out-of-sample returns on 3 of 4 assets (BTC/NQ/ES), and asset-specific
    # "best" windows (e.g. NY 12-20) were in-sample overfit that didn't hold on
    # a holdout.  So we trade all hours by default; `sessions` stays an opt-in
    # knob for per-asset experimentation, not a blanket-on filter.
    sessions: tuple = ()

    # --- Higher-timeframe trend filter (index long-only model) -------------
    # Only permit LONG entries while the higher timeframe is in a bullish
    # regime (HTF close > HTF EMA).  This is the proper multi-timeframe filter
    # (resampled daily), distinct from `bias_ema` which works on the trading
    # timeframe only.
    use_htf_filter: bool = True      # gate longs by the HTF regime
    htf_ema_period: int = 50         # EMA period computed on the HTF series
    htf_timeframe: str = "1D"        # resample rule for the HTF ('1D','4H',...)

    # --- Risk management ---------------------------------------------------
    risk_per_trade: float = 0.01     # 1% of equity per trade
    risk_reward: float = 3.0         # minimum 1:3 RR
    sl_buffer: float = 0.0005        # extra buffer beyond the sweep extreme
    entry_valid_bars: int = 10       # how long a pending limit order stays live
    breakeven_r: float = 0.0         # move stop to entry once price reaches this
                                     # many R in profit (0 = disabled)
    use_trailing_stop: bool = False  # optional trailing stop
    trail_atr_mult: float = 2.0      # trailing distance in ATR multiples
    atr_period: int = 14


# A single default instance for convenience.
DEFAULT_PARAMS = StrategyParams()
