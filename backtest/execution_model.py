"""Realistic execution model.

Replaces the flat 1-tick + touch=fill simulator assumptions. Models:

1. ATR-aware **stop slippage** — wider stops in high-vol bars and during
   news / killzone windows.
2. **Limit-fill probability** — touching a limit is necessary but not
   sufficient. Most touches do not actually fill at the limit price in
   live markets (queue position, partial trades-through).
3. **Session classification** — fill probability and slippage differ
   between London, NY AM, NY PM, and overnight.
4. **News-regime classification** — within ±N minutes of a high-impact
   event the bot is in a stressed (elevated) or catastrophic (blackout)
   regime.
5. **Partial fills** — a touched limit may produce only a fraction of
   requested size, with the rest converting to a market exit at worse
   prices (modeled as the unfilled fraction "missed" entirely).

Three profiles (``OPTIMISTIC``, ``NORMAL``, ``PUNITIVE``) parameterise
the same machinery. They are the inputs to the adversarial sensitivity
analysis: if the strategy holds positive expectancy under PUNITIVE,
that's evidence the edge is real.

This module is **pure** (no IO). Deterministic given the same
``random_state`` seed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

import numpy as np
import pandas as pd

from config import Instrument


VolRegime = Literal["low", "medium", "high"]
NewsRegime = Literal["normal", "elevated", "blackout"]
SessionName = Literal["LONDON", "NY_AM", "NY_PM", "OVERNIGHT"]
ExitType = Literal["stop", "target"]


# ---------------------------------------------------------------------------
@dataclass
class FillResult:
    """Outcome of a limit-order fill attempt."""
    filled: bool                    # was any quantity filled?
    fill_price: Optional[float] = None
    qty_filled_pct: float = 0.0     # 0..1 (1.0 = full fill, 0.5 = partial half, 0.0 = miss)
    slippage_pts: float = 0.0       # adverse points vs intended (≥ 0)
    regime_vol: VolRegime = "medium"
    regime_news: NewsRegime = "normal"
    session: SessionName = "OVERNIGHT"


# ---------------------------------------------------------------------------
@dataclass
class ExecutionProfile:
    """All knobs in one place. Three named profiles below.

    Slippage is expressed as ``max(min_ticks, atr_fraction × ATR(5))``
    in price units; the simulator converts to USD using instrument
    point value.
    """
    name: str = "custom"

    # --- vol classification (% of price) ---
    vol_low_max_pct: float = 0.002          # ATR/price ≤ 0.2% → low
    vol_medium_max_pct: float = 0.006       # ≤ 0.6% → medium, else high

    # --- stop slippage (separate by regime) ---
    stop_slip_min_ticks_low: float = 1.0
    stop_slip_atr_frac_low: float = 0.05
    stop_slip_min_ticks_med: float = 1.0
    stop_slip_atr_frac_med: float = 0.15
    stop_slip_min_ticks_high: float = 2.0
    stop_slip_atr_frac_high: float = 0.25

    # --- stop slippage news multiplier ---
    stop_slip_elevated_mult: float = 1.5
    stop_slip_blackout_mult: float = 3.0

    # --- limit fill probability (entry & target use same model) ---
    limit_fill_prob_low_vol: float = 0.80
    limit_fill_prob_med_vol: float = 0.60
    limit_fill_prob_high_vol: float = 0.35
    limit_fill_prob_elevated: float = 0.40
    limit_fill_prob_blackout: float = 0.05

    # --- partial fill probability (conditional on fill happening) ---
    partial_fill_prob: float = 0.20         # 20% of fills are partials
    partial_fill_qty_pct: float = 0.5       # average filled fraction on partial

    # --- session multipliers on stop slippage ---
    session_slip_mult: dict[str, float] = field(default_factory=lambda: {
        "LONDON":    1.0,
        "NY_AM":     1.0,
        "NY_PM":     1.0,
        "OVERNIGHT": 1.4,
    })

    # --- session multipliers on limit fill probability ---
    session_fill_mult: dict[str, float] = field(default_factory=lambda: {
        "LONDON":    1.0,
        "NY_AM":     1.0,
        "NY_PM":     0.9,
        "OVERNIGHT": 0.7,
    })

    # --- news blackout window (minutes) ---
    elevated_window_min: int = 30
    blackout_window_min: int = 10


# ---------------------------------------------------------------------------
OPTIMISTIC = ExecutionProfile(
    name="OPTIMISTIC",
    vol_low_max_pct=0.003, vol_medium_max_pct=0.010,
    stop_slip_min_ticks_low=0.5, stop_slip_atr_frac_low=0.02,
    stop_slip_min_ticks_med=0.5, stop_slip_atr_frac_med=0.05,
    stop_slip_min_ticks_high=1.0, stop_slip_atr_frac_high=0.10,
    stop_slip_elevated_mult=1.2, stop_slip_blackout_mult=1.5,
    limit_fill_prob_low_vol=0.90,
    limit_fill_prob_med_vol=0.80,
    limit_fill_prob_high_vol=0.65,
    limit_fill_prob_elevated=0.60,
    limit_fill_prob_blackout=0.30,
    partial_fill_prob=0.05,
    partial_fill_qty_pct=0.75,
    session_slip_mult={"LONDON": 1.0, "NY_AM": 1.0, "NY_PM": 1.0, "OVERNIGHT": 1.1},
    session_fill_mult={"LONDON": 1.0, "NY_AM": 1.0, "NY_PM": 1.0, "OVERNIGHT": 0.9},
    elevated_window_min=15, blackout_window_min=5,
)


NORMAL = ExecutionProfile(name="NORMAL")    # defaults above


PUNITIVE = ExecutionProfile(
    name="PUNITIVE",
    vol_low_max_pct=0.001, vol_medium_max_pct=0.004,
    stop_slip_min_ticks_low=2.0, stop_slip_atr_frac_low=0.15,
    stop_slip_min_ticks_med=3.0, stop_slip_atr_frac_med=0.30,
    stop_slip_min_ticks_high=5.0, stop_slip_atr_frac_high=0.60,
    stop_slip_elevated_mult=2.0, stop_slip_blackout_mult=4.0,
    limit_fill_prob_low_vol=0.55,
    limit_fill_prob_med_vol=0.35,
    limit_fill_prob_high_vol=0.15,
    limit_fill_prob_elevated=0.15,
    limit_fill_prob_blackout=0.00,
    partial_fill_prob=0.35,
    partial_fill_qty_pct=0.4,
    session_slip_mult={"LONDON": 1.0, "NY_AM": 1.0, "NY_PM": 1.2, "OVERNIGHT": 1.7},
    session_fill_mult={"LONDON": 1.0, "NY_AM": 1.0, "NY_PM": 0.8, "OVERNIGHT": 0.5},
    elevated_window_min=45, blackout_window_min=15,
)


PROFILES: dict[str, ExecutionProfile] = {
    "OPTIMISTIC": OPTIMISTIC,
    "NORMAL": NORMAL,
    "PUNITIVE": PUNITIVE,
}


# ---------------------------------------------------------------------------
def compute_atr(df: pd.DataFrame, idx: int, period: int = 14) -> float:
    """True-range based ATR. Returns 0 when too few prior bars."""
    if idx <= 0:
        return 0.0
    lo = max(0, idx - period + 1)
    sl = df.iloc[lo: idx + 1]
    high = sl["high"].values
    low = sl["low"].values
    close = sl["close"].values
    prev_close = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    return float(np.mean(tr))


def classify_vol(atr: float, price: float, profile: ExecutionProfile) -> VolRegime:
    if price <= 0 or atr <= 0:
        return "medium"
    pct = atr / price
    if pct <= profile.vol_low_max_pct:
        return "low"
    if pct <= profile.vol_medium_max_pct:
        return "medium"
    return "high"


def classify_session(ts: pd.Timestamp) -> SessionName:
    """ET-aware session name. Falls back to OVERNIGHT outside the three windows."""
    try:
        from utils.time_utils import to_et
        t = to_et(ts).time()
    except Exception:
        return "OVERNIGHT"
    # LONDON 02:00–05:00 ET, NY_AM 07:00–11:00 ET, NY_PM 13:30–16:00 ET
    h = t.hour + t.minute / 60
    if 2 <= h < 5:
        return "LONDON"
    if 7 <= h < 11:
        return "NY_AM"
    if 13.5 <= h < 16:
        return "NY_PM"
    return "OVERNIGHT"


def classify_news(ts: pd.Timestamp, news_events, profile: ExecutionProfile) -> NewsRegime:
    """Map proximity to a high-impact event → normal / elevated / blackout."""
    if not news_events:
        return "normal"
    try:
        from utils.time_utils import to_et
        et = to_et(ts).replace(tzinfo=None)
    except Exception:
        return "normal"
    blackout_mins = profile.blackout_window_min
    elevated_mins = profile.elevated_window_min
    closest_min = None
    for ev in news_events:
        delta = abs((et - ev.ts_et).total_seconds() / 60)
        if closest_min is None or delta < closest_min:
            closest_min = delta
            if delta <= blackout_mins:
                return "blackout"
    if closest_min is not None and closest_min <= elevated_mins:
        return "elevated"
    return "normal"


# ---------------------------------------------------------------------------
def stop_slippage_pts(profile: ExecutionProfile, instrument: Instrument,
                     atr: float, vol: VolRegime, news: NewsRegime,
                     session: SessionName) -> float:
    """Adverse points applied to a stop fill in the trade's losing direction."""
    if vol == "low":
        base = max(profile.stop_slip_min_ticks_low * instrument.tick_size,
                   profile.stop_slip_atr_frac_low * atr)
    elif vol == "high":
        base = max(profile.stop_slip_min_ticks_high * instrument.tick_size,
                   profile.stop_slip_atr_frac_high * atr)
    else:
        base = max(profile.stop_slip_min_ticks_med * instrument.tick_size,
                   profile.stop_slip_atr_frac_med * atr)
    news_mult = (profile.stop_slip_elevated_mult if news == "elevated"
                 else profile.stop_slip_blackout_mult if news == "blackout"
                 else 1.0)
    session_mult = profile.session_slip_mult.get(session, 1.0)
    return base * news_mult * session_mult


# ---------------------------------------------------------------------------
def limit_fill_probability(profile: ExecutionProfile,
                           vol: VolRegime, news: NewsRegime,
                           session: SessionName) -> float:
    """0..1 probability that a touched limit actually fills (any qty)."""
    if vol == "low":
        base = profile.limit_fill_prob_low_vol
    elif vol == "high":
        base = profile.limit_fill_prob_high_vol
    else:
        base = profile.limit_fill_prob_med_vol
    if news == "blackout":
        base = min(base, profile.limit_fill_prob_blackout)
    elif news == "elevated":
        base = min(base, profile.limit_fill_prob_elevated)
    base *= profile.session_fill_mult.get(session, 1.0)
    return max(0.0, min(1.0, base))


# ---------------------------------------------------------------------------
def attempt_limit_fill(*,
                       intended_price: float,
                       bar: pd.Series,
                       direction: str,                # "bull" or "bear"
                       df: pd.DataFrame,
                       idx: int,
                       instrument: Instrument,
                       profile: ExecutionProfile,
                       news_events: list,
                       rng: np.random.Generator) -> FillResult:
    """Decide whether a limit at ``intended_price`` would fill given this bar.

    Required: the bar's range touches ``intended_price``.
    Probability-modulated fill, conditional on touch.
    """
    h, l = float(bar["high"]), float(bar["low"])
    touched = l <= intended_price <= h
    if not touched:
        return FillResult(filled=False, qty_filled_pct=0.0)

    atr = compute_atr(df, idx)
    price = (h + l) / 2.0
    vol = classify_vol(atr, price, profile)
    news = classify_news(df.index[idx], news_events, profile)
    session = classify_session(df.index[idx])

    p_fill = limit_fill_probability(profile, vol, news, session)
    if rng.random() > p_fill:
        return FillResult(filled=False, qty_filled_pct=0.0,
                          regime_vol=vol, regime_news=news, session=session)

    # Filled (full or partial)
    if rng.random() < profile.partial_fill_prob:
        qty_pct = max(0.05, min(1.0,
            rng.normal(profile.partial_fill_qty_pct, 0.1)))
    else:
        qty_pct = 1.0

    # Limit-order fills typically AT the limit price (no positive slippage).
    # Small adverse for queue position: 0 ticks median, occasional 1 tick.
    queue_slip_ticks = 1 if rng.random() < 0.3 else 0
    slip = queue_slip_ticks * instrument.tick_size
    return FillResult(
        filled=True,
        fill_price=intended_price,    # we record the limit price; slippage tracked separately
        qty_filled_pct=qty_pct,
        slippage_pts=slip,
        regime_vol=vol, regime_news=news, session=session,
    )


def apply_stop_fill(*,
                    intended_price: float,
                    bar: pd.Series,
                    direction: str,           # trade direction
                    df: pd.DataFrame,
                    idx: int,
                    instrument: Instrument,
                    profile: ExecutionProfile,
                    news_events: list) -> FillResult:
    """Stop hit → market fill at intended ± slippage (adverse).

    Stops always fill (no probability gate) but the worse-than-intended
    price is the punishment.
    """
    atr = compute_atr(df, idx)
    price = (float(bar["high"]) + float(bar["low"])) / 2.0
    vol = classify_vol(atr, price, profile)
    news = classify_news(df.index[idx], news_events, profile)
    session = classify_session(df.index[idx])
    slip = stop_slippage_pts(profile, instrument, atr, vol, news, session)
    sign = -1 if direction == "bull" else +1   # adverse for the trade
    return FillResult(
        filled=True,
        fill_price=intended_price + sign * slip,
        qty_filled_pct=1.0,
        slippage_pts=slip,
        regime_vol=vol, regime_news=news, session=session,
    )


def apply_target_fill(*,
                      intended_price: float,
                      bar: pd.Series,
                      direction: str,
                      df: pd.DataFrame,
                      idx: int,
                      instrument: Instrument,
                      profile: ExecutionProfile,
                      news_events: list,
                      rng: np.random.Generator) -> FillResult:
    """Target = limit order. Same probability-modulated path as entry limits."""
    return attempt_limit_fill(
        intended_price=intended_price, bar=bar, direction=direction,
        df=df, idx=idx, instrument=instrument, profile=profile,
        news_events=news_events, rng=rng,
    )
