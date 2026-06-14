"""Liquidity mapping & sweep detection.

Reference levels (the things smart money "raids"):
- Equal Highs / Equal Lows clustered within ``EQUAL_LEVEL_TOLERANCE`` (0.25%)
- Previous Day High / Low (PDH / PDL)
- Previous Week High / Low (PWH / PWL)
- Session Highs / Lows for each ICT killzone (London, NY AM)

A *sweep* is a bar whose wick prints beyond a reference level and whose close
returns back inside (within ``SWEEP_REENTRY_BARS``, default = same bar).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from config import EQUAL_LEVEL_TOLERANCE, KILLZONES, SESSIONS, SWEEP_REENTRY_BARS
from utils.time_utils import in_session, to_et


Side = Literal["high", "low"]
LevelKind = Literal["EQH", "EQL", "PDH", "PDL", "PWH", "PWL", "SH", "SL"]


@dataclass
class LiquidityLevel:
    kind: LevelKind
    side: Side               # "high" or "low"
    price: float
    timestamp: pd.Timestamp  # when the level was established (or end-of-period)
    label: str = ""          # e.g. "PDH 2026-06-03" or "LONDON-SH"
    bar_indices: list[int] = field(default_factory=list)


@dataclass
class Sweep:
    idx: int
    timestamp: pd.Timestamp
    level: LiquidityLevel
    side: Side               # which side was swept (matches level.side)
    wick_extreme: float      # the high/low of the sweep bar
    close: float


# ---------------------------------------------------------------------------
# Daily / weekly references
# ---------------------------------------------------------------------------
def previous_day_levels(df: pd.DataFrame) -> list[LiquidityLevel]:
    """One PDH + one PDL for each prior session day represented in df."""
    et_index = df.index.tz_convert("America/New_York") if df.index.tz else df.index
    by_day = df.groupby(et_index.date)
    levels: list[LiquidityLevel] = []
    days = sorted(by_day.groups.keys())
    for d in days[:-1]:  # exclude the in-progress day
        sl = by_day.get_group(d)
        high_idx = int(sl["high"].values.argmax())
        low_idx = int(sl["low"].values.argmin())
        levels.append(LiquidityLevel(
            kind="PDH", side="high",
            price=float(sl["high"].max()),
            timestamp=sl.index[high_idx],
            label=f"PDH {d.isoformat()}",
        ))
        levels.append(LiquidityLevel(
            kind="PDL", side="low",
            price=float(sl["low"].min()),
            timestamp=sl.index[low_idx],
            label=f"PDL {d.isoformat()}",
        ))
    return levels


def previous_week_levels(df: pd.DataFrame) -> list[LiquidityLevel]:
    """PWH / PWL for each ISO week represented in df (excluding the in-progress one)."""
    et_index = df.index.tz_convert("America/New_York") if df.index.tz else df.index
    week_keys = pd.Series(et_index, index=df.index).dt.isocalendar()
    keys = list(zip(week_keys.year, week_keys.week))
    df2 = df.copy()
    df2["_wk"] = keys
    levels: list[LiquidityLevel] = []
    grouped = df2.groupby("_wk", sort=True)
    week_ids = sorted(grouped.groups.keys())
    for wk in week_ids[:-1]:
        sl = grouped.get_group(wk).drop(columns="_wk")
        high_idx = int(sl["high"].values.argmax())
        low_idx = int(sl["low"].values.argmin())
        levels.append(LiquidityLevel(
            kind="PWH", side="high",
            price=float(sl["high"].max()),
            timestamp=sl.index[high_idx],
            label=f"PWH {wk[0]}-W{wk[1]:02d}",
        ))
        levels.append(LiquidityLevel(
            kind="PWL", side="low",
            price=float(sl["low"].min()),
            timestamp=sl.index[low_idx],
            label=f"PWL {wk[0]}-W{wk[1]:02d}",
        ))
    return levels


# ---------------------------------------------------------------------------
# Session highs / lows (killzones)
# ---------------------------------------------------------------------------
def session_levels(df: pd.DataFrame) -> list[LiquidityLevel]:
    """High + low of each completed killzone session for each day in df."""
    levels: list[LiquidityLevel] = []
    et_index = df.index.tz_convert("America/New_York") if df.index.tz else df.index
    days = sorted(set(et_index.date))
    for d in days:
        for kz in KILLZONES:
            sess = SESSIONS[kz]
            mask = [(to_et(ts).date() == d) and in_session(ts, sess) for ts in df.index]
            if not any(mask):
                continue
            sl = df[mask]
            high_idx = int(sl["high"].values.argmax())
            low_idx = int(sl["low"].values.argmin())
            levels.append(LiquidityLevel(
                kind="SH", side="high",
                price=float(sl["high"].max()),
                timestamp=sl.index[high_idx],
                label=f"{kz}-SH {d.isoformat()}",
            ))
            levels.append(LiquidityLevel(
                kind="SL", side="low",
                price=float(sl["low"].min()),
                timestamp=sl.index[low_idx],
                label=f"{kz}-SL {d.isoformat()}",
            ))
    return levels


# ---------------------------------------------------------------------------
# Equal highs / lows
# ---------------------------------------------------------------------------
def equal_levels(df: pd.DataFrame, tolerance: float = EQUAL_LEVEL_TOLERANCE) -> list[LiquidityLevel]:
    """Cluster bar highs and lows by relative price proximity.

    Returns one LiquidityLevel per cluster containing >= 2 touches. The
    ``bar_indices`` list records each contributing bar.
    """
    levels: list[LiquidityLevel] = []
    for side in ("high", "low"):
        prices = df[side].values
        ts_list = list(df.index)
        clusters: list[dict] = []  # each {"price": float, "indices": list[int]}
        for i, p in enumerate(prices):
            placed = False
            for c in clusters:
                if abs(p - c["price"]) / c["price"] <= tolerance:
                    c["indices"].append(i)
                    # Update cluster price to the running extreme (most extreme touch)
                    c["price"] = max(c["price"], p) if side == "high" else min(c["price"], p)
                    placed = True
                    break
            if not placed:
                clusters.append({"price": float(p), "indices": [i]})
        for c in clusters:
            if len(c["indices"]) >= 2:
                last_idx = c["indices"][-1]
                levels.append(LiquidityLevel(
                    kind="EQH" if side == "high" else "EQL",
                    side=side,
                    price=c["price"],
                    timestamp=ts_list[last_idx],
                    label=f"{'EQH' if side == 'high' else 'EQL'} x{len(c['indices'])}",
                    bar_indices=c["indices"],
                ))
    return levels


# ---------------------------------------------------------------------------
# Sweep detection
# ---------------------------------------------------------------------------
def find_sweeps(df: pd.DataFrame, levels: list[LiquidityLevel],
                reentry_bars: int = SWEEP_REENTRY_BARS) -> list[Sweep]:
    """For each level, find bars whose wick exceeds it then close re-enters.

    A level can only be swept on a bar strictly after it was established.
    """
    sweeps: list[Sweep] = []
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    ts = df.index
    n = len(df)
    ts_index = {t: i for i, t in enumerate(ts)}

    for lvl in levels:
        lvl_idx = ts_index.get(lvl.timestamp, -1)
        start = max(lvl_idx + 1, 0)
        for i in range(start, n):
            if lvl.side == "high":
                if highs[i] > lvl.price:
                    # close back inside within reentry_bars
                    end = min(i + reentry_bars, n - 1)
                    if any(closes[j] < lvl.price for j in range(i, end + 1)):
                        sweeps.append(Sweep(i, ts[i], lvl, "high",
                                            float(highs[i]), float(closes[i])))
                        break
            else:
                if lows[i] < lvl.price:
                    end = min(i + reentry_bars, n - 1)
                    if any(closes[j] > lvl.price for j in range(i, end + 1)):
                        sweeps.append(Sweep(i, ts[i], lvl, "low",
                                            float(lows[i]), float(closes[i])))
                        break
    return sweeps


# ---------------------------------------------------------------------------
def map_all_liquidity(df: pd.DataFrame) -> dict:
    """Run every liquidity detector + sweep search in one call."""
    eq = equal_levels(df)
    pd_lv = previous_day_levels(df)
    pw_lv = previous_week_levels(df)
    sess = session_levels(df)
    all_levels = eq + pd_lv + pw_lv + sess
    sweeps = find_sweeps(df, all_levels)
    return {
        "equal":      eq,
        "prev_day":   pd_lv,
        "prev_week":  pw_lv,
        "session":    sess,
        "levels":     all_levels,
        "sweeps":     sweeps,
        "counts": {
            "EQH": sum(1 for l in eq if l.side == "high"),
            "EQL": sum(1 for l in eq if l.side == "low"),
            "PDH": sum(1 for l in pd_lv if l.kind == "PDH"),
            "PDL": sum(1 for l in pd_lv if l.kind == "PDL"),
            "PWH": sum(1 for l in pw_lv if l.kind == "PWH"),
            "PWL": sum(1 for l in pw_lv if l.kind == "PWL"),
            "session_levels": len(sess),
            "sweeps": len(sweeps),
            "sweeps_high": sum(1 for s in sweeps if s.side == "high"),
            "sweeps_low":  sum(1 for s in sweeps if s.side == "low"),
        },
    }
