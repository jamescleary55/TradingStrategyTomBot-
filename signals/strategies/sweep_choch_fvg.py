"""Sweep → CHoCH → FVG strategy (the current one).

Pure wrapper around :func:`signals.setup.find_setups`. The detection
logic is unchanged — this file exists so the strategy is pluggable.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from risk.sizing import TradePlan, plan_trade
from signals.setup import find_setups as legacy_find_setups
from signals.strategies.base import (
    Strategy,
    StrategyContext,
    StrategySetup,
    ValidationResult,
    register,
)
from utils.time_utils import current_session, trim_incomplete_bar


@register
class SweepChochFvgStrategy(Strategy):
    name = "sweep_choch_fvg"
    version = "1.0.0"

    def detect_setups(self, df: pd.DataFrame,
                      context: StrategyContext) -> list[StrategySetup]:
        # A6 fix — never feed the still-forming current bar into the detector
        df = trim_incomplete_bar(df, context.timeframe)
        # Reuse the legacy detector verbatim
        legacy = legacy_find_setups(
            df,
            htf_bias_series=context.htf_bias_series,
            require_htf_alignment=False,
        )
        out: list[StrategySetup] = []
        for s in legacy:
            try:
                session = current_session(s.timestamp)
            except Exception:
                session = None

            subtype = self._subtype(s, session)
            score = self._score(s, context)

            out.append(StrategySetup(
                strategy_name=self.name,
                strategy_version=self.version,
                timestamp=s.timestamp,
                symbol=context.instrument.symbol,
                timeframe=context.timeframe,
                direction=s.direction,
                entry=s.entry,
                stop=s.stop,
                target=s.target,
                rr=s.rr,
                setup_type="sweep_choch_fvg",
                setup_subtype=subtype,
                htf_bias=s.bias,
                setup_score=score,
                invalidation_level=s.stop,  # invalidation == stop loss line
                sweep_level_price=getattr(s.sweep.level, "price", None),
                sweep_level_kind=getattr(s.sweep.level, "kind", None),
                choch_price=getattr(s.choch, "price", None),
                bos_state=("bull_break" if s.direction == "bull" else "bear_break"),
                fvg_top=getattr(s.fvg, "top", None),
                fvg_bottom=getattr(s.fvg, "bottom", None),
                session=session,
                confluence=list(s.confluence),
                native=s,
            ))
        return out

    def validate_setup(self, setup: StrategySetup,
                       context: StrategyContext) -> ValidationResult:
        # Geometry sanity
        if setup.direction == "bull" and not (setup.stop < setup.entry < setup.target):
            return ValidationResult(False, "invalid_bull_geometry")
        if setup.direction == "bear" and not (setup.target < setup.entry < setup.stop):
            return ValidationResult(False, "invalid_bear_geometry")
        if setup.rr <= 0:
            return ValidationResult(False, "non_positive_rr")
        return ValidationResult(True)

    def build_trade_plan(self, setup: StrategySetup, equity: float,
                         risk_pct: float, min_rr: float = 1.0) -> TradePlan:
        # Resolve instrument lazily so circular imports stay broken
        from config import INSTRUMENTS
        instrument = INSTRUMENTS.get(setup.symbol)
        if instrument is None:
            # Map root → micro proxy for sizing (NQ→MNQ etc.)
            micro_map = {"NQ": "MNQ", "ES": "MES", "GC": "MGC", "CL": "MCL"}
            instrument = INSTRUMENTS[micro_map.get(setup.symbol, "MNQ")]
        return plan_trade(
            equity=equity, entry=setup.entry, stop=setup.stop,
            target=setup.target, instrument=instrument,
            risk_pct=risk_pct, min_rr=min_rr,
        )

    def explain_setup(self, setup: StrategySetup) -> str:
        d = setup.direction.upper()
        bias = f" (HTF bias: {setup.htf_bias})" if setup.htf_bias else ""
        sweep = f" swept {setup.sweep_level_kind} @ {setup.sweep_level_price}" if setup.sweep_level_price else ""
        choch = f" → CHoCH @ {setup.choch_price}" if setup.choch_price else ""
        fvg = (f" → entry in FVG {setup.fvg_bottom:.2f}–{setup.fvg_top:.2f}"
               if setup.fvg_top and setup.fvg_bottom else "")
        return (f"{d} {setup.symbol} on {setup.timeframe} ({setup.session or 'no session'}){bias}.{sweep}{choch}{fvg}. "
                f"Plan: entry {setup.entry:.2f}, stop {setup.stop:.2f}, target {setup.target:.2f} "
                f"(RR {setup.rr:.2f}). Invalidation at stop.")

    # ---------------------------------------------------------------
    def _subtype(self, native_setup, session: Optional[str]) -> str:
        """Encode the setup's flavour for stats slicing (e.g. EQL_LDN)."""
        kind = (getattr(native_setup.sweep.level, "kind", "") or "?").upper()
        sess = (session or "ANY").upper()
        return f"{kind}_{sess}"

    def _score(self, native_setup, context: StrategyContext) -> float:
        """Tiny heuristic score 0–1 for filtering. Will be replaced when
        we have real forward data to calibrate against."""
        score = 0.5
        if native_setup.bias == native_setup.direction:
            score += 0.2
        if native_setup.rr >= 2.0:
            score += 0.15
        if getattr(native_setup.sweep.level, "kind", "") in ("PDH", "PDL", "PWH", "PWL"):
            score += 0.1
        if context.session in ("LONDON", "NY_AM"):
            score += 0.05
        return min(score, 1.0)
