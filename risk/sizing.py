"""Position sizing + daily-loss circuit breaker.

All sizing is per *contract*, accounting for the instrument's point value:

    risk_per_contract_usd = abs(entry - stop) * instrument.point_value
    max_loss_usd          = equity * RISK.max_risk_per_trade_pct
    contracts             = floor(max_loss_usd / risk_per_contract_usd)

The :class:`DailyLossTracker` records realised P&L within a single trading
day (ET) and exposes :meth:`should_halt` for upstream code to bail out of
new entries once ``RISK.max_daily_loss_pct`` is hit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from config import RISK, Instrument
from utils.time_utils import to_et


@dataclass
class TradePlan:
    contracts: int
    entry: float
    stop: float
    target: float
    risk_per_contract: float
    total_risk_usd: float
    potential_reward_usd: float
    rr: float
    approved: bool
    reason: str = ""


# ---------------------------------------------------------------------------
def position_size(
    equity: float,
    entry: float,
    stop: float,
    instrument: Instrument,
    risk_pct: float | None = None,
) -> tuple[int, float]:
    """Return ``(contracts, risk_per_contract_usd)`` for the given setup.

    Rounds *down* (never exceeds the risk cap).
    """
    if entry == stop:
        return 0, 0.0
    risk_pct = risk_pct if risk_pct is not None else RISK.max_risk_per_trade_pct
    risk_per_contract = abs(entry - stop) * instrument.point_value
    if risk_per_contract <= 0:
        return 0, 0.0
    max_loss_usd = equity * risk_pct
    contracts = math.floor(max_loss_usd / risk_per_contract)
    return max(0, contracts), risk_per_contract


def plan_trade(
    equity: float,
    entry: float,
    stop: float,
    target: float,
    instrument: Instrument,
    *,
    risk_pct: float | None = None,
    min_rr: float | None = None,
) -> TradePlan:
    """Build a fully-checked :class:`TradePlan`.

    Sets ``approved=False`` if size rounds to 0, RR is below ``min_rr``,
    or stop is on the wrong side of the entry given the direction inferred
    by ``target``.
    """
    min_rr = min_rr if min_rr is not None else 0.0
    contracts, risk_per_contract = position_size(equity, entry, stop, instrument, risk_pct)
    reward_per_contract = abs(target - entry) * instrument.point_value
    rr = (reward_per_contract / risk_per_contract) if risk_per_contract > 0 else 0.0
    total_risk = contracts * risk_per_contract
    potential_reward = contracts * reward_per_contract

    approved = True
    reason = "ok"
    direction_target = "bull" if target > entry else "bear"
    direction_stop = "bull" if stop < entry else "bear"
    if direction_target != direction_stop:
        approved = False
        reason = "stop is on the wrong side of entry"
    elif contracts <= 0:
        approved = False
        reason = "risk-per-contract exceeds per-trade cap"
    elif rr < min_rr:
        approved = False
        reason = f"RR {rr:.2f} below minimum {min_rr}"

    return TradePlan(
        contracts=contracts,
        entry=entry,
        stop=stop,
        target=target,
        risk_per_contract=risk_per_contract,
        total_risk_usd=total_risk,
        potential_reward_usd=potential_reward,
        rr=rr,
        approved=approved,
        reason=reason,
    )


# ---------------------------------------------------------------------------
@dataclass
class DailyLossTracker:
    """Records realised P&L per ET trading day; halts trading at the cap."""

    starting_equity: float
    max_daily_loss_pct: float = RISK.max_daily_loss_pct
    _by_day: dict[date, float] = field(default_factory=dict)
    _halted_days: set[date] = field(default_factory=set)

    def record(self, pnl_usd: float, timestamp) -> None:
        d = to_et(timestamp).date()
        self._by_day[d] = self._by_day.get(d, 0.0) + pnl_usd
        if self.day_pnl(timestamp) <= -abs(self.starting_equity * self.max_daily_loss_pct):
            self._halted_days.add(d)

    def day_pnl(self, timestamp) -> float:
        return self._by_day.get(to_et(timestamp).date(), 0.0)

    def should_halt(self, timestamp) -> bool:
        return to_et(timestamp).date() in self._halted_days

    def summary(self) -> dict:
        worst = min(self._by_day.values(), default=0.0)
        best = max(self._by_day.values(), default=0.0)
        return {
            "days_traded": len(self._by_day),
            "days_halted": len(self._halted_days),
            "worst_day_pnl": worst,
            "best_day_pnl": best,
            "cumulative_pnl": sum(self._by_day.values()),
        }
