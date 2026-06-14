"""Baseline signal generators for adversarial comparison vs ICT.

Three baselines, each producing :class:`Setup`-compatible objects so
they pass through the same simulator, same execution profiles, same
risk model. Differences are confined to *how the entry is chosen*.

1. **RANDOM_ENTRY_BASELINE** — pick K random bars during allowed
   sessions; random direction; ATR-sized stop / 2:1 target.
2. **SIMPLE_TREND_BASELINE** — EMA-trend filter (8 > 21 = bull) + 4h
   trend agreement; pullback to EMA8 triggers entry; ATR stop, 2:1
   target.
3. **DESTROYED_SIGNAL_BASELINE** — take real ICT setups, randomize
   their timestamps (within session bucket) but preserve direction,
   distribution of RR, and ATR-sized geometry at the new timestamp.

All baselines obey the same `allowed_sessions` filter (LONDON,
NY_AM by default — match the bot's personal_rules).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from utils.time_utils import current_session


# ---------------------------------------------------------------------------
@dataclass
class _Choch:
    """Minimal duck-type for the simulator's `s.choch.idx` access."""
    idx: int


@dataclass
class BaselineSetup:
    """Setup look-alike that satisfies the simulator's contract.

    Fields the simulator touches: choch.idx, entry, stop, target,
    direction, timestamp. The rest exist so the trade-row writer
    doesn't crash.
    """
    timestamp: pd.Timestamp
    direction: str                  # "bull" or "bear"
    entry: float
    stop: float
    target: float
    rr: float
    choch: _Choch
    bias: str = "neutral"
    # The four below are unused by the simulator but referenced by some
    # debug/logging code paths. Keep as Nones.
    sweep: Optional[object] = None
    fvg: Optional[object] = None
    confluence: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
def _atr(df: pd.DataFrame, idx: int, period: int = 14) -> float:
    lo = max(0, idx - period + 1)
    sl = df.iloc[lo: idx + 1]
    high = sl["high"].values; low = sl["low"].values
    close = sl["close"].values
    prev_close = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum.reduce([
        high - low, np.abs(high - prev_close), np.abs(low - prev_close),
    ])
    return float(np.mean(tr))


def _allowed(ts: pd.Timestamp, sessions: set[str]) -> bool:
    try:
        return current_session(ts) in sessions
    except Exception:
        return False


def _build(df: pd.DataFrame, idx: int, direction: str,
           stop_atr_mult: float, rr: float) -> BaselineSetup:
    """Build a setup at bar `idx` using ATR-scaled stop and a target at `rr`R."""
    bar = df.iloc[idx]
    atr = _atr(df, idx)
    entry = float(bar["close"])
    if atr <= 0:
        atr = max(entry * 0.001, 0.01)
    stop_dist = stop_atr_mult * atr
    if direction == "bull":
        stop = entry - stop_dist
        target = entry + rr * stop_dist
    else:
        stop = entry + stop_dist
        target = entry - rr * stop_dist
    return BaselineSetup(
        timestamp=df.index[idx], direction=direction,
        entry=entry, stop=stop, target=target,
        rr=rr, choch=_Choch(idx=idx),
    )


# ---------------------------------------------------------------------------
def random_entry_baseline(df: pd.DataFrame, n_signals: int,
                          allowed_sessions=("LONDON", "NY_AM", "NY_PM"),
                          rng_seed: int = 42,
                          stop_atr_mult: float = 1.0,
                          rr: float = 2.0) -> list[BaselineSetup]:
    """Uniform-random entries during allowed sessions. Direction 50/50."""
    sessions = set(allowed_sessions)
    rng = np.random.default_rng(rng_seed)
    candidates = [i for i in range(20, len(df) - 30)
                  if _allowed(df.index[i], sessions)]
    if not candidates:
        return []
    n = min(n_signals, len(candidates))
    chosen = rng.choice(candidates, size=n, replace=False)
    chosen.sort()
    setups = []
    for i in chosen:
        direction = "bull" if rng.random() < 0.5 else "bear"
        setups.append(_build(df, int(i), direction, stop_atr_mult, rr))
    return setups


# ---------------------------------------------------------------------------
def simple_trend_baseline(df: pd.DataFrame, df_htf: pd.DataFrame | None = None,
                          ema_fast: int = 8, ema_slow: int = 21,
                          allowed_sessions=("LONDON", "NY_AM", "NY_PM"),
                          target_signals: Optional[int] = None,
                          rng_seed: int = 42,
                          stop_atr_mult: float = 1.5,
                          rr: float = 2.0) -> list[BaselineSetup]:
    """EMA-pullback trend follower.

    Bull rule: EMA_fast > EMA_slow AND HTF trend bull (4h close > 4h EMA21)
    AND current bar's low touches EMA_fast (the pullback).
    Mirror for bear. Entry at next bar's open (we approximate as the
    pullback bar's close).
    """
    sessions = set(allowed_sessions)
    close = df["close"]
    low = df["low"]; high = df["high"]
    ef = close.ewm(span=ema_fast, adjust=False).mean()
    es = close.ewm(span=ema_slow, adjust=False).mean()
    # HTF trend
    if df_htf is not None and not df_htf.empty:
        htf_ef = df_htf["close"].ewm(span=8, adjust=False).mean()
        htf_es = df_htf["close"].ewm(span=21, adjust=False).mean()
        htf_dir = pd.Series(np.where(htf_ef > htf_es, "bull", "bear"),
                            index=df_htf.index)
        htf_dir = htf_dir.reindex(df.index, method="ffill")
    else:
        # No HTF: use a much longer LTF EMA pair as a proxy
        long_ef = close.ewm(span=50, adjust=False).mean()
        long_es = close.ewm(span=200, adjust=False).mean()
        htf_dir = pd.Series(np.where(long_ef > long_es, "bull", "bear"),
                            index=df.index)

    raw: list[BaselineSetup] = []
    last_emit_idx = -10  # prevent back-to-back same-side entries
    cooldown = 4
    for i in range(50, len(df) - 30):
        ts = df.index[i]
        if not _allowed(ts, sessions):
            continue
        if i - last_emit_idx < cooldown:
            continue
        ef_i = ef.iloc[i]; es_i = es.iloc[i]
        htf_i = htf_dir.iloc[i] if i < len(htf_dir) else None
        if ef_i > es_i and htf_i == "bull":
            # bull pullback: low of this bar within 0.3*ATR of EMA_fast
            atr = _atr(df, i)
            if low.iloc[i] <= ef_i + 0.3 * atr and close.iloc[i] > ef_i:
                raw.append(_build(df, i, "bull", stop_atr_mult, rr))
                last_emit_idx = i
        elif ef_i < es_i and htf_i == "bear":
            atr = _atr(df, i)
            if high.iloc[i] >= ef_i - 0.3 * atr and close.iloc[i] < ef_i:
                raw.append(_build(df, i, "bear", stop_atr_mult, rr))
                last_emit_idx = i

    if target_signals is not None and len(raw) > target_signals:
        rng = np.random.default_rng(rng_seed)
        idxs = sorted(rng.choice(len(raw), size=target_signals, replace=False))
        raw = [raw[i] for i in idxs]
    return raw


# ---------------------------------------------------------------------------
def destroyed_signal_baseline(df: pd.DataFrame, ict_setups: list,
                              allowed_sessions=("LONDON", "NY_AM", "NY_PM"),
                              rng_seed: int = 42,
                              stop_atr_mult: float = 1.0) -> list[BaselineSetup]:
    """Shuffle the *timing* of ICT setups.

    Preserves: count, direction distribution, RR distribution, session
    composition.

    Destroys: the specific bar where the sweep+CHoCH+FVG actually
    formed. If ICT's information content is *when* the strategy fires
    (and not just direction × session × RR), this baseline must
    underperform real ICT.
    """
    sessions = set(allowed_sessions)
    rng = np.random.default_rng(rng_seed)
    # Bucket allowed bars by session
    pools: dict[str, list[int]] = {s: [] for s in sessions}
    for i in range(20, len(df) - 30):
        ts = df.index[i]
        try:
            sess = current_session(ts)
        except Exception:
            sess = None
        if sess in pools:
            pools[sess].append(i)

    out: list[BaselineSetup] = []
    for s in ict_setups:
        try:
            sess = current_session(s.timestamp)
        except Exception:
            sess = None
        pool = pools.get(sess) or [i for v in pools.values() for i in v]
        if not pool:
            continue
        new_idx = int(rng.choice(pool))
        out.append(_build(df, new_idx, s.direction, stop_atr_mult,
                          rr=max(1.0, float(s.rr) if s.rr else 2.0)))
    out.sort(key=lambda x: x.choch.idx)
    return out
