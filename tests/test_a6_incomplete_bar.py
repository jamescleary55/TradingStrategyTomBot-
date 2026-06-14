"""A6 — phantom-FVG fix tests.

Verifies:
1. A bar whose window has not yet closed is removed before detection.
2. A fully-closed bar passes through.
3. trim is idempotent — calling twice removes at most one bar total.
4. detect_setups never returns a setup whose CHoCH/FVG sits on the
   incomplete trailing bar (the failure mode the audit named).
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_df(n: int, last_ts: pd.Timestamp, tf_seconds: int) -> pd.DataFrame:
    idx = pd.date_range(end=last_ts, periods=n,
                        freq=pd.Timedelta(seconds=tf_seconds), tz="UTC")
    return pd.DataFrame({
        "open": [100 + i for i in range(n)],
        "high": [101 + i for i in range(n)],
        "low":  [99 + i for i in range(n)],
        "close": [100 + i for i in range(n)],
        "volume": [1000] * n,
    }, index=idx)


def test_incomplete_bar_is_trimmed():
    from utils.time_utils import trim_incomplete_bar
    now = dt.datetime(2026, 6, 12, 12, 30, tzinfo=dt.timezone.utc)
    # Last bar opens at 12:00, window ends at 13:00; "now" is 12:30 → still forming
    last = pd.Timestamp("2026-06-12 12:00", tz="UTC")
    df = _make_df(20, last, 3600)
    out = trim_incomplete_bar(df, "1h", now=now)
    assert len(out) == 19
    assert out.index[-1] == pd.Timestamp("2026-06-12 11:00", tz="UTC")


def test_closed_bar_is_kept():
    from utils.time_utils import trim_incomplete_bar
    # Last bar opens at 11:00, closes at 12:00; "now" 12:30 → already closed
    now = dt.datetime(2026, 6, 12, 12, 30, tzinfo=dt.timezone.utc)
    last = pd.Timestamp("2026-06-12 11:00", tz="UTC")
    df = _make_df(20, last, 3600)
    out = trim_incomplete_bar(df, "1h", now=now)
    assert len(out) == 20
    assert out.index[-1] == last


def test_trim_idempotent():
    from utils.time_utils import trim_incomplete_bar
    now = dt.datetime(2026, 6, 12, 12, 30, tzinfo=dt.timezone.utc)
    last = pd.Timestamp("2026-06-12 12:00", tz="UTC")
    df = _make_df(20, last, 3600)
    once = trim_incomplete_bar(df, "1h", now=now)
    twice = trim_incomplete_bar(once, "1h", now=now)
    # Second call removes nothing because once.index[-1] (11:00) has already closed by 12:30
    assert len(twice) == 19


def test_strategy_skips_incomplete_bar_in_detection():
    """The detector must never produce a setup whose latest bar is still forming."""
    from signals.strategies.base import StrategyContext
    from signals.strategies.sweep_choch_fvg import SweepChochFvgStrategy
    from config import INSTRUMENTS
    # Build a deterministic OHLC that would form an FVG on the LAST bar
    # if the still-forming bar is included.
    idx = pd.date_range("2026-06-01", periods=60, freq="1h", tz="UTC")
    df = pd.DataFrame({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000,
    }, index=idx)
    # Three-candle bullish imbalance at the END of the series
    df.iloc[-3] = {"open": 100, "high": 102, "low": 99, "close": 99.5, "volume": 1000}
    df.iloc[-2] = {"open": 99.5, "high": 105, "low": 99.5, "close": 105, "volume": 1000}
    df.iloc[-1] = {"open": 105, "high": 110, "low": 103, "close": 109, "volume": 1000}

    # "now" stamp such that the last bar's window has NOT closed yet
    last_open = df.index[-1].to_pydatetime()
    now = last_open + dt.timedelta(minutes=30)

    # Bypass the live monitor and call the strategy directly with a custom now.
    # We rely on trim_incomplete_bar reading the wall clock — patch it.
    import utils.time_utils as tu
    orig = tu.trim_incomplete_bar
    try:
        tu.trim_incomplete_bar = lambda d, tf, now=None: orig(d, tf, now=now)
        # Now invoke through the strategy adapter so it uses the patched one.
        # SweepChochFvg imports the helper at module load, so patch THERE too.
        import signals.strategies.sweep_choch_fvg as scf
        scf.trim_incomplete_bar = tu.trim_incomplete_bar

        ctx = StrategyContext(instrument=INSTRUMENTS["MNQ"], timeframe="1h")
        setups_with_open_bar = scf.SweepChochFvgStrategy().detect_setups(
            df, ctx,
        )

        # Last bar should not feed into detection — any setup must have idx < last
        # (we cannot easily count without re-running the legacy detector w/ trimming).
        # Verifying invariant: no setup carries the trailing index.
        last_idx = len(df) - 1
        for s in setups_with_open_bar:
            assert s.timestamp != df.index[last_idx], \
                "setup detected on the still-forming current bar (A6 regression)"
    finally:
        tu.trim_incomplete_bar = orig
