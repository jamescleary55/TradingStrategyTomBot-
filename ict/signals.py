"""
Signal generation — combines market structure, liquidity and FVG into the
ICT entry model and emits the required signal columns.

LONG  setup:  bullish liquidity sweep  +  bullish CHoCH  +  bullish FVG
              -> enter at 50% of the FVG, stop below the sweep low, TP at RR.
SHORT setup:  bearish liquidity sweep  +  bearish CHoCH  +  bearish FVG
              -> enter at 50% of the FVG, stop above the sweep high, TP at RR.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from config import StrategyParams, DEFAULT_PARAMS
from .market_structure import market_structure
from .liquidity import equal_levels, liquidity_sweeps
from .fvg import fair_value_gaps


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def _recent(series: np.ndarray, i: int, lookback: int, value: int):
    """Index of the most recent bar in (i-lookback, i] where series == value."""
    start = max(0, i - lookback)
    for j in range(i, start - 1, -1):
        if series[j] == value:
            return j
    return None


def _recent_between(series: np.ndarray, hi: int, lo: int, value: int):
    """Index of the most recent bar in [lo, hi] where series == value (or None)."""
    for j in range(hi, lo - 1, -1):
        if series[j] == value:
            return j
    return None


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    """Simple EMA over a numpy array; returns an array the same length."""
    out = np.full(len(values), np.nan)
    if period <= 1 or len(values) == 0:
        return values.astype(float)
    alpha = 2.0 / (period + 1.0)
    ema = values[0]
    out[0] = ema
    for k in range(1, len(values)):
        ema = alpha * values[k] + (1.0 - alpha) * ema
        out[k] = ema
    return out


def htf_bull_regime(df: pd.DataFrame, ema_period: int = 50,
                    timeframe: str = "1D") -> np.ndarray:
    """
    Higher-timeframe bullish-regime mask aligned to df's (trading-TF) bars.

    Resamples Close to `timeframe`, computes an `ema_period` EMA on the HTF
    closes, and marks a HTF bar bullish when its close > its EMA.  The regime
    is shifted forward one HTF bar before being mapped back onto the trading
    bars, so an intraday bar only ever sees the *last completed* HTF close
    (no look-ahead).  Returns a bool array, length == len(df).
    """
    htf_close = df["Close"].resample(timeframe).last().dropna()
    if len(htf_close) == 0:
        return np.zeros(len(df), dtype=bool)
    ema = _ema(htf_close.to_numpy(dtype=float), ema_period)
    bull = pd.Series(htf_close.to_numpy(dtype=float) > ema, index=htf_close.index)
    bull = bull.shift(1).fillna(False)                 # no look-ahead
    mapped = bull.reindex(df.index.normalize(), method="ffill").fillna(False)
    return mapped.to_numpy(dtype=bool)


def generate_signals(df: pd.DataFrame,
                     params: StrategyParams = DEFAULT_PARAMS) -> pd.DataFrame:
    """
    Run the full ICT pipeline and return an enriched dataframe containing the
    required columns: Signal, Entry, StopLoss, TakeProfit, RiskReward
    (plus all intermediate ICT columns for plotting/debugging).
    """
    out = market_structure(df, params.swing_left, params.swing_right)
    out = equal_levels(out, params.liquidity_lookback, params.equal_tolerance)
    out = liquidity_sweeps(out, params.swing_right, params.sweep_lookback)
    out = fair_value_gaps(out, params.min_fvg_size)
    out["ATR"] = _atr(out, params.atr_period)

    n = len(out)
    sweep = out["Sweep"].to_numpy()
    sweep_ext = out["SweepExtreme"].to_numpy(dtype=float)
    choch = out["CHoCH"].to_numpy()
    fvg = out["FVG"].to_numpy()
    fvg_mid = out["FVGMid"].to_numpy(dtype=float)
    close = out["Close"].to_numpy(dtype=float)

    # Higher-timeframe directional bias (0 disables the filter).
    ema = _ema(close, params.bias_ema) if params.bias_ema > 1 else None

    # ICT killzone gate: a bar is tradable if its UTC hour falls in one of the
    # configured [start, end) session ranges (empty = trade all hours).
    hours = out.index.hour.to_numpy()
    if params.sessions:
        in_session = np.zeros(n, dtype=bool)
        for lo_h, hi_h in params.sessions:
            in_session |= (hours >= lo_h) & (hours < hi_h)
    else:
        in_session = np.ones(n, dtype=bool)

    # Higher-timeframe trend regime: only permit longs while HTF is bullish.
    htf_bull = (htf_bull_regime(out, params.htf_ema_period, params.htf_timeframe)
                if params.use_htf_filter else np.ones(n, dtype=bool))

    # Diagnostics counters (attached to out.attrs for the backtest report).
    diag = {"bull_detected": 0, "bull_blocked_htf": 0, "bull_emitted": 0,
            "bear_detected": 0}

    signal = np.array(["NONE"] * n, dtype=object)
    entry = np.full(n, np.nan)
    stop = np.full(n, np.nan)
    take = np.full(n, np.nan)
    rr = np.full(n, np.nan)

    def _find_sweep(direction: int, i: int, c: int):
        """Locate the sweep that seeds this setup.

        With sequencing on, the stop-hunt must occur on or BEFORE the CHoCH
        (sweep -> CHoCH -> FVG), still within sweep_lookback of the entry bar.
        With it off, fall back to the most recent sweep at or before the FVG.
        """
        lo = max(0, i - params.sweep_lookback)
        if params.require_sequence:
            return _recent_between(sweep, c, lo, direction)
        return _recent_between(sweep, i, lo, direction)

    last_sig = -10**9  # bar index of the previous emitted signal (cooldown)

    for i in range(n):
        # ---- LONG -------------------------------------------------------- #
        if params.allow_long and fvg[i] == 1:
            c = _recent(choch, i, params.choch_lookback, 1)
            s = _find_sweep(1, i, c) if c is not None else None
            if s is not None and c is not None:
                e = fvg_mid[i]
                sl = sweep_ext[s] * (1.0 - params.sl_buffer)
                risk = e - sl
                if risk > 0:
                    diag["bull_detected"] += 1
                    htf_ok = htf_bull[i]                    # HTF regime gate
                    ema_ok = ema is None or close[i] > ema[i]
                    fresh = (i - last_sig) >= params.signal_cooldown
                    if not htf_ok:
                        diag["bull_blocked_htf"] += 1
                    if htf_ok and ema_ok and in_session[i] and fresh:
                        tp = e + params.risk_reward * risk
                        signal[i] = "BUY"
                        entry[i] = e
                        stop[i] = sl
                        take[i] = tp
                        rr[i] = round((tp - e) / risk, 2)
                        last_sig = i
                        diag["bull_emitted"] += 1
                    continue

        # ---- SHORT (detected & labelled for ANALYSIS; never executed when
        #      long_only — see backtest.ICTStrategy.next) ------------------ #
        if params.allow_short and fvg[i] == -1:
            c = _recent(choch, i, params.choch_lookback, -1)
            s = _find_sweep(-1, i, c) if c is not None else None
            if s is not None and c is not None:
                e = fvg_mid[i]
                sl = sweep_ext[s] * (1.0 + params.sl_buffer)
                risk = sl - e
                if risk > 0:
                    diag["bear_detected"] += 1
                    ema_ok = ema is None or close[i] < ema[i]
                    fresh = (i - last_sig) >= params.signal_cooldown
                    if ema_ok and in_session[i] and fresh:
                        tp = e - params.risk_reward * risk
                        signal[i] = "SELL"
                        entry[i] = e
                        stop[i] = sl
                        take[i] = tp
                        rr[i] = round((e - tp) / risk, 2)
                        last_sig = i

    out["Signal"] = signal
    out["Entry"] = entry
    out["StopLoss"] = stop
    out["TakeProfit"] = take
    out["RiskReward"] = rr
    out.attrs["diag"] = diag
    return out


if __name__ == "__main__":
    import sys, os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from data.data_loader import load_data

    data = load_data("BTCUSDT", interval="1h", start="2024-01-01", end="2024-03-01")
    sig = generate_signals(data)
    print(sig[sig["Signal"] != "NONE"][
        ["Signal", "Entry", "StopLoss", "TakeProfit", "RiskReward"]].head(10))
    print(f"\nTotal signals: {(sig['Signal'] != 'NONE').sum()}")
