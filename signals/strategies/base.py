"""Strategy interface.

Every concrete strategy is a self-contained module that knows how to
detect setups on OHLCV data, validate them against contextual filters,
build a sized trade plan, and explain itself in plain English.

The point of the abstraction is *isolation* — the live monitor and
backtest runners depend only on this interface so we can A/B test
variants without touching the execution layer.

Do NOT add a new strategy implementation until the current one has at
least 100 forward-tested signals.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from config import Instrument
from risk.sizing import TradePlan


@dataclass
class StrategyContext:
    """Everything a strategy needs that isn't in the bare OHLCV frame."""
    instrument: Instrument
    timeframe: str
    htf_bias_series: Optional[pd.Series] = None
    htf_timeframe: Optional[str] = None
    news_events: list = field(default_factory=list)
    session: Optional[str] = None       # ASIA / LONDON / NY_AM / NY_PM / None
    spread_estimate: float = 0.0        # in price points, used for slippage logging
    extra: dict = field(default_factory=dict)


@dataclass
class StrategySetup:
    """Normalised representation of a setup produced by any strategy.

    The native Setup dataclass from signals/setup.py is wrapped into this
    on emit so downstream consumers (logger, risk gate, executor) work
    against a stable shape regardless of the underlying strategy.
    """
    strategy_name: str
    strategy_version: str
    timestamp: pd.Timestamp
    symbol: str
    timeframe: str
    direction: str                # "bull" | "bear"
    entry: float
    stop: float
    target: float
    rr: float
    setup_type: str = ""          # e.g. "sweep_choch_fvg"
    setup_subtype: str = ""       # e.g. "EQL_sweep_LDN", "PDH_sweep_NY"
    htf_bias: Optional[str] = None
    setup_score: float = 0.0      # 0–1 confidence, strategy-specific
    invalidation_level: float = 0.0
    sweep_level_price: Optional[float] = None
    sweep_level_kind: Optional[str] = None
    choch_price: Optional[float] = None
    bos_state: Optional[str] = None
    fvg_top: Optional[float] = None
    fvg_bottom: Optional[float] = None
    session: Optional[str] = None
    confluence: list[str] = field(default_factory=list)
    native: Any = None            # the original strategy's setup object


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""


class Strategy(ABC):
    """Interface every concrete strategy implements."""

    name: str = "base"
    version: str = "0.0.0"

    @abstractmethod
    def detect_setups(self, df: pd.DataFrame,
                      context: StrategyContext) -> list[StrategySetup]: ...

    def validate_setup(self, setup: StrategySetup,
                       context: StrategyContext) -> ValidationResult:
        """Optional second-stage filter. Default: trust detect_setups()."""
        return ValidationResult(ok=True)

    @abstractmethod
    def build_trade_plan(self, setup: StrategySetup, equity: float,
                         risk_pct: float, min_rr: float = 1.0) -> TradePlan: ...

    @abstractmethod
    def explain_setup(self, setup: StrategySetup) -> str:
        """One-paragraph human-readable summary used in alerts + logs."""


# ---------------------------------------------------------------------------
_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy(name: str) -> Strategy:
    if name not in _REGISTRY:
        # Lazy-import the default so unrelated tests can import this module
        from signals.strategies.sweep_choch_fvg import SweepChochFvgStrategy  # noqa
    if name not in _REGISTRY:
        raise KeyError(f"Unknown strategy {name!r}. Known: {list(_REGISTRY)}")
    return _REGISTRY[name]()


def list_strategies() -> list[str]:
    # Trigger default import
    from signals.strategies import sweep_choch_fvg  # noqa
    return list(_REGISTRY.keys())
