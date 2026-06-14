"""ICT ``sweep → CHoCH → FVG`` setup builder.

Classic intraday model:

1.  **HTF bias** — direction of the most recent N BOS events
    (``HTF_BIAS_LOOKBACK_BOS``). Bull-leaning → look long; bear → look short.
2.  **Liquidity sweep against the bias** — sell-side raid (sweep low) in a
    bullish bias, buy-side raid (sweep high) in a bearish bias.
3.  **CHoCH in bias direction** — the first opposing Break-Of-Structure that
    confirms the reversal, fired within ``SWEEP_TO_CHOCH_MAX_BARS`` bars
    after the sweep.
4.  **FVG entry** — the first Fair Value Gap in bias direction printed
    within ``CHOCH_TO_FVG_MAX_BARS`` bars after the CHoCH. Entry = mid of
    the gap.
5.  **Stop** — beyond the sweep extreme (small buffer of one ATR-like wick
    isn't applied here; raw extreme).
6.  **Target** — RR-based or next opposing liquidity level
    (``SETUP_TARGET_MODE``).

The output is a list of :class:`Setup` objects ordered chronologically by
the CHoCH timestamp.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd

import config
from config import RISK
from engine.liquidity import LiquidityLevel, Sweep, map_all_liquidity
from engine.market_structure import StructureEvent, summary as ms_summary
from signals.fvg import FVG, find_fvgs


Direction = Literal["bull", "bear"]


@dataclass
class Setup:
    timestamp: pd.Timestamp     # CHoCH timestamp (decision point)
    direction: Direction         # trade direction
    entry: float
    stop: float
    target: float
    rr: float                    # planned reward / risk (price units)
    sweep: Sweep
    choch: StructureEvent
    fvg: FVG
    bias: Direction
    confluence: list[str] = field(default_factory=list)

    def to_row(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "direction": self.direction,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "rr": round(self.rr, 2),
            "bias": self.bias,
            "swept_level": self.sweep.level.label,
            "confluence": ",".join(self.confluence),
        }


# ---------------------------------------------------------------------------
def _bias_at(bos_events: list[StructureEvent], cutoff_idx: int) -> Optional[Direction]:
    """Infer HTF bias from the last N BOS events strictly before ``cutoff_idx``."""
    history = [e for e in bos_events if e.idx < cutoff_idx][-config.HTF_BIAS_LOOKBACK_BOS:]
    if not history:
        return None
    bull = sum(1 for e in history if e.direction == "bull")
    bear = len(history) - bull
    if bull == bear:
        return history[-1].direction  # most recent breaks the tie
    return "bull" if bull > bear else "bear"


def _next_opposing_liquidity(levels: list[LiquidityLevel], price: float,
                             direction: Direction, after_idx_ts: pd.Timestamp) -> Optional[float]:
    """Closest level above (bull) or below (bear) price, established before now."""
    candidates: list[float] = []
    for lvl in levels:
        if lvl.timestamp > after_idx_ts:
            continue
        if direction == "bull" and lvl.side == "high" and lvl.price > price:
            candidates.append(lvl.price)
        elif direction == "bear" and lvl.side == "low" and lvl.price < price:
            candidates.append(lvl.price)
    if not candidates:
        return None
    return min(candidates) if direction == "bull" else max(candidates)


# ---------------------------------------------------------------------------
def find_setups(df: pd.DataFrame, htf_bias_series: Optional[pd.Series] = None,
                require_htf_alignment: bool = False) -> list[Setup]:
    """Run every detector and return the list of valid ICT setups.

    Parameters
    ----------
    df:
        LTF OHLCV DataFrame.
    htf_bias_series:
        Optional Series indexed identically to ``df`` whose values are
        ``"bull"``, ``"bear"`` or ``None``. When provided, replaces the
        same-timeframe BOS-history heuristic and (optionally) filters
        setups whose direction disagrees with the HTF bias.
    require_htf_alignment:
        If True (and ``htf_bias_series`` is provided), reject setups whose
        direction doesn't match the HTF bias at the CHoCH timestamp.
    """
    ms = ms_summary(df)
    liq = map_all_liquidity(df)
    fvgs = find_fvgs(df)

    setups: list[Setup] = []
    bos_events = ms["bos"]
    chochs = ms["choch"]
    sweeps = liq["sweeps"]
    levels = liq["levels"]

    # Index FVGs by direction for quick scanning
    bull_fvgs = sorted([g for g in fvgs if g.direction == "bull"], key=lambda g: g.idx)
    bear_fvgs = sorted([g for g in fvgs if g.direction == "bear"], key=lambda g: g.idx)

    for ch in chochs:
        direction: Direction = ch.direction  # bias direction is the CHoCH direction
        if htf_bias_series is not None:
            try:
                htf_val = htf_bias_series.iloc[ch.idx]
            except Exception:
                htf_val = None
            bias = htf_val if htf_val in ("bull", "bear") else None
        else:
            bias = _bias_at(bos_events, ch.idx)
        if bias is None and require_htf_alignment:
            continue
        if bias is None:
            # Fall back to same-TF heuristic when no HTF available
            bias = _bias_at(bos_events, ch.idx)
            if bias is None:
                continue
        if require_htf_alignment and bias != direction:
            continue
        # Strict reading: CHoCH must align with the prevailing bias trend reversal.
        # (We allow either: bias matches CHoCH direction, OR CHoCH flips a recent counter-trend.)

        # 1) Find the most recent sweep against ``direction`` within window
        wanted_sweep_side = "low" if direction == "bull" else "high"
        candidate_sweeps = [
            s for s in sweeps
            if s.side == wanted_sweep_side
            and 0 < (ch.idx - s.idx) <= config.SWEEP_TO_CHOCH_MAX_BARS
        ]
        if not candidate_sweeps:
            continue
        sweep = max(candidate_sweeps, key=lambda s: s.idx)

        # 2) First FVG in ``direction`` after the CHoCH within window
        pool = bull_fvgs if direction == "bull" else bear_fvgs
        fvg = next(
            (g for g in pool if 0 < (g.idx - ch.idx) <= config.CHOCH_TO_FVG_MAX_BARS),
            None,
        )
        if fvg is None:
            continue

        # 3) Entry / stop / target
        mode = config.SETUP_ENTRY_MODE
        if mode == "closer_edge":
            # The FVG edge nearest the sweep (tighter stop, slightly worse fill price)
            entry = fvg.bottom if direction == "bull" else fvg.top
        elif mode == "farther_edge":
            entry = fvg.top if direction == "bull" else fvg.bottom
        else:  # "mid"
            entry = (fvg.top + fvg.bottom) / 2.0
        stop = sweep.wick_extreme
        if direction == "bull" and stop >= entry:
            continue
        if direction == "bear" and stop <= entry:
            continue

        risk_price = abs(entry - stop)
        if config.SETUP_MAX_STOP_POINTS > 0 and risk_price > config.SETUP_MAX_STOP_POINTS:
            continue
        if config.SETUP_TARGET_MODE == "liquidity":
            target = _next_opposing_liquidity(levels, entry, direction, ch.timestamp)
        else:
            target = None
        if target is None:
            # RR target fallback
            target = entry + RISK.default_rr * risk_price if direction == "bull" else entry - RISK.default_rr * risk_price

        rr = abs(target - entry) / risk_price if risk_price > 0 else 0.0
        if rr < config.SETUP_MIN_RR:
            continue

        confluence = [
            f"bias={bias}",
            f"swept {sweep.level.kind} @ {sweep.level.price:g}",
            f"CHoCH @ {ch.price:g}",
            f"FVG {fvg.direction} [{fvg.bottom:g}-{fvg.top:g}]",
        ]
        if bias == direction:
            confluence.append("aligned with HTF bias")

        setups.append(Setup(
            timestamp=ch.timestamp,
            direction=direction,
            entry=float(entry),
            stop=float(stop),
            target=float(target),
            rr=float(rr),
            sweep=sweep,
            choch=ch,
            fvg=fvg,
            bias=bias,
            confluence=confluence,
        ))

    return setups


# ---------------------------------------------------------------------------
def summary(df: pd.DataFrame, htf_bias_series: Optional[pd.Series] = None,
            require_htf_alignment: bool = False) -> dict:
    """Setup counts + the raw list, for the backtest runner."""
    setups = find_setups(df, htf_bias_series=htf_bias_series,
                         require_htf_alignment=require_htf_alignment)
    return {
        "setups": setups,
        "counts": {
            "total": len(setups),
            "bull":  sum(1 for s in setups if s.direction == "bull"),
            "bear":  sum(1 for s in setups if s.direction == "bear"),
            "aligned_with_bias": sum(1 for s in setups if s.bias == s.direction),
        },
    }
