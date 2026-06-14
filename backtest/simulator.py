"""Walk-forward simulator for ICT setups.

For every :class:`Setup` produced by ``signals.setup``, walk forward bar by
bar and decide what actually happened:

1. **Pending → Filled** — limit order at the FVG mid is considered filled as
   soon as a bar's range touches the entry price. If price hits the stop
   *before* the entry, the setup is voided. If neither happens within
   ``ENTRY_TIMEOUT_BARS`` bars, the setup is cancelled.

2. **Filled → Closed** — first-touch on stop or target. If a single bar
   straddles both levels we apply the pessimistic assumption that the stop
   filled first (a real exchange would settle this via order priority and
   tick sequence, which we cannot reconstruct from OHLC bars alone).

Slippage (``SLIPPAGE_TICKS``) is applied adversely to *both* the entry and
the exit. Commission (``COMMISSION_PER_CONTRACT_USD``) is deducted per
contract per round-trip.

Concurrency is capped at ``RISK.max_concurrent_positions`` (default 1) and
the :class:`DailyLossTracker` halts new entries once the daily loss cap is
breached.

The result is a list of :class:`SimTrade` and a per-bar equity series, plus
a ``stats`` dict suitable for printing or charting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

import numpy as np
import pandas as pd

from backtest.execution_model import (
    ExecutionProfile, NORMAL,
    apply_stop_fill, apply_target_fill, attempt_limit_fill,
)
from config import (
    COMMISSION_PER_CONTRACT_USD,
    ENTRY_TIMEOUT_BARS,
    INSTRUMENTS,
    Instrument,
    RISK,
    SLIPPAGE_TICKS,
)
from risk.sizing import DailyLossTracker, plan_trade, TradePlan
from signals.setup import Setup


Outcome = Literal["target", "stop", "timeout_unfilled", "voided_before_entry", "skipped"]


@dataclass
class SimTrade:
    setup: Setup
    plan: TradePlan
    fill_idx: Optional[int] = None
    fill_timestamp: Optional[pd.Timestamp] = None
    exit_idx: Optional[int] = None
    exit_timestamp: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    outcome: Outcome = "skipped"
    pnl_usd: float = 0.0
    r_multiple: float = 0.0
    skip_reason: str = ""

    def to_row(self) -> dict:
        return {
            "setup_ts": self.setup.timestamp,
            "dir": self.setup.direction,
            "entry": round(self.setup.entry, 2),
            "stop": round(self.setup.stop, 2),
            "target": round(self.setup.target, 2),
            "fill_ts": self.fill_timestamp,
            "exit_ts": self.exit_timestamp,
            "exit_px": round(self.exit_price, 2) if self.exit_price is not None else None,
            "outcome": self.outcome,
            "contracts": self.plan.contracts if self.plan else 0,
            "pnl_usd": round(self.pnl_usd, 2),
            "R": round(self.r_multiple, 2),
        }


@dataclass
class SimResult:
    trades: list[SimTrade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    starting_equity: float = 0.0
    stats: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
def _biggest_winner_share_pct(filled: list) -> float:
    """% of cumulative POSITIVE pnl contributed by the largest single winner."""
    if not filled:
        return 0.0
    pos = [t.pnl_usd for t in filled if t.pnl_usd > 0]
    if not pos:
        return 0.0
    return max(pos) / sum(pos) * 100


def _apply_slippage(price: float, side: str, instrument: Instrument, direction_bias: str) -> float:
    """Adverse fill: buys pay up, sells pay down by SLIPPAGE_TICKS."""
    delta = SLIPPAGE_TICKS * instrument.tick_size
    if direction_bias == "buy":
        return price + delta
    return price - delta


# ---------------------------------------------------------------------------
def simulate(
    df: pd.DataFrame,
    setups: list[Setup],
    starting_equity: float = 10_000.0,
    instrument_symbol: str = "MNQ",
    risk_pct: float | None = None,
    min_rr: float = 1.0,
    timeout_bars: int = ENTRY_TIMEOUT_BARS,
    execution_profile: ExecutionProfile | None = None,
    news_events: list | None = None,
    random_seed: int | None = 42,
) -> SimResult:
    """Run the walk-forward simulation. Returns a :class:`SimResult`."""
    instrument = INSTRUMENTS[instrument_symbol]
    profile = execution_profile or NORMAL
    events = news_events or []
    rng = np.random.default_rng(random_seed)
    equity = starting_equity
    ts = df.index
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    # Counters for execution-realism stats
    n_limit_attempts = 0
    n_limit_filled = 0
    n_limit_partial = 0
    n_limit_missed = 0
    slip_pts_total = 0.0
    slip_pts_n = 0
    slip_pts_samples: list[float] = []

    loss_tracker = DailyLossTracker(starting_equity=starting_equity)
    open_position: Optional[SimTrade] = None
    trades: list[SimTrade] = []
    equity_curve = pd.Series(equity, index=ts, dtype=float).copy()

    # Index setups by their entry-eligible start (bar after CHoCH)
    setup_queue = sorted(setups, key=lambda s: s.choch.idx)
    next_setup_iter = iter(setup_queue)
    pending_setup = next(next_setup_iter, None)
    waiting: list[SimTrade] = []  # setups that have been activated, awaiting fill

    for i in range(n):
        # Activate any setups whose CHoCH was at/before bar i, plan-sized
        while pending_setup is not None and pending_setup.choch.idx <= i:
            if loss_tracker.should_halt(ts[i]):
                trade = SimTrade(setup=pending_setup, plan=None,
                                 outcome="skipped", skip_reason="daily loss halt")
                trades.append(trade)
            elif open_position is not None and RISK.max_concurrent_positions <= 1:
                trade = SimTrade(setup=pending_setup, plan=None,
                                 outcome="skipped", skip_reason="position already open")
                trades.append(trade)
            else:
                plan = plan_trade(
                    equity=equity,
                    entry=pending_setup.entry,
                    stop=pending_setup.stop,
                    target=pending_setup.target,
                    instrument=instrument,
                    risk_pct=risk_pct,
                    min_rr=min_rr,
                )
                if not plan.approved:
                    trades.append(SimTrade(
                        setup=pending_setup, plan=plan,
                        outcome="skipped", skip_reason=plan.reason,
                    ))
                else:
                    waiting.append(SimTrade(setup=pending_setup, plan=plan))
            pending_setup = next(next_setup_iter, None)

        h, l = highs[i], lows[i]

        # ----- Process open position first (stop/target check) -----
        if open_position is not None:
            s = open_position.setup
            stop_hit = (l <= s.stop) if s.direction == "bull" else (h >= s.stop)
            target_hit = (h >= s.target) if s.direction == "bull" else (l <= s.target)
            outcome: Optional[Outcome] = None
            exit_price = None
            if stop_hit and target_hit:
                outcome = "stop"; exit_price = s.stop          # pessimistic
            elif stop_hit:
                outcome = "stop"; exit_price = s.stop
            elif target_hit:
                outcome = "target"; exit_price = s.target

            if outcome is not None:
                bar = df.iloc[i]
                if outcome == "stop":
                    fr = apply_stop_fill(
                        intended_price=exit_price, bar=bar,
                        direction=s.direction, df=df, idx=i,
                        instrument=instrument, profile=profile,
                        news_events=events,
                    )
                    fill_px = fr.fill_price
                else:    # target
                    fr = apply_target_fill(
                        intended_price=exit_price, bar=bar,
                        direction=s.direction, df=df, idx=i,
                        instrument=instrument, profile=profile,
                        news_events=events, rng=rng,
                    )
                    if not fr.filled:
                        # Target NOT actually filled this bar — keep waiting,
                        # the next bar may stop us out. Carry forward.
                        n_limit_attempts += 1
                        n_limit_missed += 1
                        equity_curve.iloc[i] = equity
                        continue
                    fill_px = fr.fill_price
                    n_limit_attempts += 1
                    if fr.qty_filled_pct < 1.0:
                        n_limit_partial += 1
                    else:
                        n_limit_filled += 1
                slip_pts_total += fr.slippage_pts
                slip_pts_n += 1
                slip_pts_samples.append(float(fr.slippage_pts))

                qty = max(1, int(open_position.plan.contracts * fr.qty_filled_pct))
                pnl_price = (fill_px - open_position.plan.entry) if s.direction == "bull" \
                            else (open_position.plan.entry - fill_px)
                pnl_usd = pnl_price * instrument.point_value * qty
                pnl_usd -= COMMISSION_PER_CONTRACT_USD * qty
                risk_price = abs(open_position.plan.entry - s.stop)
                r_mult = (pnl_price / risk_price) if risk_price > 0 else 0.0

                open_position.exit_idx = i
                open_position.exit_timestamp = ts[i]
                open_position.exit_price = float(fill_px)
                open_position.outcome = outcome
                open_position.pnl_usd = float(pnl_usd)
                open_position.r_multiple = float(r_mult)
                trades.append(open_position)

                equity += pnl_usd
                loss_tracker.record(pnl_usd, ts[i])
                open_position = None

        # ----- Process waiting limits (fill or void) -----
        still_waiting: list[SimTrade] = []
        for w in waiting:
            s = w.setup
            bars_since = i - s.choch.idx
            # Void if price hits the stop before reaching entry
            voided = (l <= s.stop) if s.direction == "bull" else (h >= s.stop)
            entry_touched = (l <= s.entry <= h)
            if voided and not entry_touched:
                w.outcome = "voided_before_entry"
                trades.append(w)
                continue
            if entry_touched and open_position is None:
                # Probability-modulated limit fill via the execution model
                bar = df.iloc[i]
                fr = attempt_limit_fill(
                    intended_price=s.entry, bar=bar,
                    direction=s.direction, df=df, idx=i,
                    instrument=instrument, profile=profile,
                    news_events=events, rng=rng,
                )
                n_limit_attempts += 1
                if not fr.filled:
                    n_limit_missed += 1
                    # Stay in waiting until timeout or invalidation
                    still_waiting.append(w)
                    continue
                if fr.qty_filled_pct < 1.0:
                    n_limit_partial += 1
                else:
                    n_limit_filled += 1
                slip_pts_total += fr.slippage_pts
                slip_pts_n += 1
                slip_pts_samples.append(float(fr.slippage_pts))
                qty = max(1, int(w.plan.contracts * fr.qty_filled_pct))
                w.fill_idx = i
                w.fill_timestamp = ts[i]
                w.plan = TradePlan(
                    contracts=qty,
                    entry=float(fr.fill_price),
                    stop=w.plan.stop,
                    target=w.plan.target,
                    risk_per_contract=w.plan.risk_per_contract,
                    total_risk_usd=w.plan.total_risk_usd,
                    potential_reward_usd=w.plan.potential_reward_usd,
                    rr=w.plan.rr,
                    approved=True,
                    reason=w.plan.reason,
                )
                open_position = w
                continue
            if bars_since >= timeout_bars:
                w.outcome = "timeout_unfilled"
                trades.append(w)
                continue
            still_waiting.append(w)
        waiting = still_waiting

        equity_curve.iloc[i] = equity

    # Cancel anything still waiting at the end of the data
    for w in waiting:
        w.outcome = "timeout_unfilled"
        trades.append(w)

    # ----- Stats -----
    filled = [t for t in trades if t.outcome in ("target", "stop")]
    wins = [t for t in filled if t.outcome == "target"]
    losses = [t for t in filled if t.outcome == "stop"]
    skipped = [t for t in trades if t.outcome == "skipped"]
    voided = [t for t in trades if t.outcome == "voided_before_entry"]
    timed_out = [t for t in trades if t.outcome == "timeout_unfilled"]
    total_pnl = sum(t.pnl_usd for t in filled)

    # Max drawdown on the equity curve
    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    max_dd_pct = float(drawdown.min()) if len(drawdown) else 0.0

    avg_r = float(np.mean([t.r_multiple for t in filled])) if filled else 0.0
    expectancy_r = avg_r  # already per-trade
    hit_rate = (len(filled) / len(trades)) if trades else 0.0
    win_rate = (len(wins) / len(filled)) if filled else 0.0

    # Group skip reasons for a "why didn't more trades fill?" view
    skip_breakdown: dict[str, int] = {}
    for t in skipped:
        key = (t.skip_reason or "skipped").split(":")[0].strip()
        skip_breakdown[key] = skip_breakdown.get(key, 0) + 1

    stats = {
        "starting_equity": starting_equity,
        "ending_equity": equity,
        "total_pnl_usd": total_pnl,
        "return_pct": (equity / starting_equity - 1) * 100 if starting_equity else 0,
        "max_drawdown_pct": max_dd_pct * 100,
        "n_setups": len(trades),
        "n_filled": len(filled),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_skipped": len(skipped),
        "n_voided": len(voided),
        "n_timed_out": len(timed_out),
        "hit_rate_pct": hit_rate * 100,
        "win_rate_pct": win_rate * 100,
        "avg_R": avg_r,
        "expectancy_R": expectancy_r,
        "loss_tracker": loss_tracker.summary(),
        "skip_breakdown": skip_breakdown,
        "fill_rate_pct": (len(filled) / len(trades) * 100) if trades else 0,
        # --- execution realism counters ---
        "execution_profile": profile.name,
        "limit_attempts": n_limit_attempts,
        "limit_filled_full": n_limit_filled,
        "limit_filled_partial": n_limit_partial,
        "limit_missed": n_limit_missed,
        "limit_fill_rate_pct": (
            (n_limit_filled + n_limit_partial) / n_limit_attempts * 100
            if n_limit_attempts else 0.0),
        "avg_slippage_pts": (slip_pts_total / slip_pts_n) if slip_pts_n else 0.0,
        "median_slippage_pts": (
            float(np.median(slip_pts_samples)) if slip_pts_samples else 0.0),
        # share of total positive PnL from largest single winner
        "biggest_winner_share_pct": _biggest_winner_share_pct(filled),
    }

    return SimResult(
        trades=trades,
        equity_curve=equity_curve,
        starting_equity=starting_equity,
        stats=stats,
    )
