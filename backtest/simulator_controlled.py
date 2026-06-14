"""Deterministic-per-trade-RNG simulator.

Same logic as :func:`backtest.simulator.simulate` but the execution
randomness is keyed per-setup, not per-stream. The motivation:

When account size changes, more setups pass risk-sizing and enter
the queue. With a single stream RNG, the random draws for ANY
later setup shift, because earlier ones consumed more rng calls.
So the "ES drop" from $50k to $100k could be selection effect
*or* RNG path divergence — they're entangled.

This simulator gives each setup its own RNG, seeded from
``(master_seed, profile.name, setup_identity)``. The identity is
the setup's timestamp + direction + price tuple, which is stable
across runs.

Result: a setup that fires in run A and run B receives the same
random fill outcomes in both runs. Only "did this setup get
accepted" depends on account size — the per-trade randomness does
not. We can now isolate the selection effect.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from backtest.execution_model import (
    ExecutionProfile, NORMAL,
    apply_stop_fill, apply_target_fill, attempt_limit_fill,
)
from backtest.simulator import SimResult, SimTrade, _biggest_winner_share_pct
from config import (
    COMMISSION_PER_CONTRACT_USD, ENTRY_TIMEOUT_BARS, INSTRUMENTS, RISK,
)
from risk.sizing import DailyLossTracker, plan_trade


def _setup_identity(setup) -> str:
    """Stable key for a setup across runs."""
    ts = setup.timestamp.value if hasattr(setup.timestamp, "value") else int(pd.Timestamp(setup.timestamp).value)
    parts = (ts, setup.direction, round(float(setup.entry), 4),
             round(float(setup.stop), 4), round(float(setup.target), 4))
    return repr(parts)


def _seed_for(master_seed: int, profile_name: str, setup_id: str) -> int:
    """Derive a deterministic per-setup seed."""
    h = hashlib.blake2b(
        f"{master_seed}|{profile_name}|{setup_id}".encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(h, "little", signed=False) % (2**32)


def simulate_controlled(
    df: pd.DataFrame,
    setups: list,
    starting_equity: float = 10_000.0,
    instrument_symbol: str = "MNQ",
    risk_pct: float | None = None,
    min_rr: float = 1.0,
    timeout_bars: int = ENTRY_TIMEOUT_BARS,
    execution_profile: ExecutionProfile | None = None,
    news_events: list | None = None,
    master_seed: int = 42,
) -> SimResult:
    instrument = INSTRUMENTS[instrument_symbol]
    profile = execution_profile or NORMAL
    events = news_events or []
    equity = starting_equity
    ts = df.index
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    # Per-setup deterministic RNG store keyed by setup identity.
    setup_rngs: dict[str, np.random.Generator] = {}

    def _rng_for(setup):
        ident = _setup_identity(setup)
        rng = setup_rngs.get(ident)
        if rng is None:
            rng = np.random.default_rng(_seed_for(master_seed, profile.name, ident))
            setup_rngs[ident] = rng
        return rng

    n_limit_attempts = 0; n_limit_filled = 0
    n_limit_partial = 0; n_limit_missed = 0
    slip_pts_total = 0.0; slip_pts_n = 0
    slip_pts_samples: list[float] = []
    entry_slip_samples: list[float] = []
    target_slip_samples: list[float] = []
    stop_slip_samples: list[float] = []

    loss_tracker = DailyLossTracker(starting_equity=starting_equity)
    open_position: Optional[SimTrade] = None
    trades: list[SimTrade] = []
    equity_curve = pd.Series(equity, index=ts, dtype=float).copy()

    setup_queue = sorted(setups, key=lambda s: s.choch.idx)
    it = iter(setup_queue); pending = next(it, None)
    waiting: list[SimTrade] = []

    for i in range(n):
        while pending is not None and pending.choch.idx <= i:
            if loss_tracker.should_halt(ts[i]):
                trades.append(SimTrade(setup=pending, plan=None,
                                       outcome="skipped",
                                       skip_reason="daily loss halt"))
            elif open_position is not None and RISK.max_concurrent_positions <= 1:
                trades.append(SimTrade(setup=pending, plan=None,
                                       outcome="skipped",
                                       skip_reason="position already open"))
            else:
                plan = plan_trade(
                    equity=equity, entry=pending.entry, stop=pending.stop,
                    target=pending.target, instrument=instrument,
                    risk_pct=risk_pct, min_rr=min_rr,
                )
                if not plan.approved:
                    trades.append(SimTrade(setup=pending, plan=plan,
                                           outcome="skipped",
                                           skip_reason=plan.reason))
                else:
                    waiting.append(SimTrade(setup=pending, plan=plan))
            pending = next(it, None)

        h, l = highs[i], lows[i]

        if open_position is not None:
            s = open_position.setup
            stop_hit = (l <= s.stop) if s.direction == "bull" else (h >= s.stop)
            target_hit = (h >= s.target) if s.direction == "bull" else (l <= s.target)
            outcome = None; exit_price = None
            if stop_hit and target_hit:
                outcome = "stop"; exit_price = s.stop
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
                    stop_slip_samples.append(float(fr.slippage_pts))
                else:
                    fr = apply_target_fill(
                        intended_price=exit_price, bar=bar,
                        direction=s.direction, df=df, idx=i,
                        instrument=instrument, profile=profile,
                        news_events=events, rng=_rng_for(s),
                    )
                    if not fr.filled:
                        n_limit_attempts += 1; n_limit_missed += 1
                        equity_curve.iloc[i] = equity
                        continue
                    fill_px = fr.fill_price
                    n_limit_attempts += 1
                    if fr.qty_filled_pct < 1.0:
                        n_limit_partial += 1
                    else:
                        n_limit_filled += 1
                    target_slip_samples.append(float(fr.slippage_pts))

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

        still_waiting: list[SimTrade] = []
        for w in waiting:
            s = w.setup
            bars_since = i - s.choch.idx
            voided = (l <= s.stop) if s.direction == "bull" else (h >= s.stop)
            entry_touched = (l <= s.entry <= h)
            if voided and not entry_touched:
                w.outcome = "voided_before_entry"
                trades.append(w); continue
            if entry_touched and open_position is None:
                bar = df.iloc[i]
                fr = attempt_limit_fill(
                    intended_price=s.entry, bar=bar,
                    direction=s.direction, df=df, idx=i,
                    instrument=instrument, profile=profile,
                    news_events=events, rng=_rng_for(s),
                )
                n_limit_attempts += 1
                if not fr.filled:
                    n_limit_missed += 1
                    still_waiting.append(w); continue
                if fr.qty_filled_pct < 1.0:
                    n_limit_partial += 1
                else:
                    n_limit_filled += 1
                entry_slip_samples.append(float(fr.slippage_pts))
                slip_pts_total += fr.slippage_pts; slip_pts_n += 1
                slip_pts_samples.append(float(fr.slippage_pts))
                qty = max(1, int(w.plan.contracts * fr.qty_filled_pct))
                w.fill_idx = i; w.fill_timestamp = ts[i]
                from risk.sizing import TradePlan
                w.plan = TradePlan(
                    contracts=qty, entry=float(fr.fill_price), stop=w.plan.stop,
                    target=w.plan.target, risk_per_contract=w.plan.risk_per_contract,
                    total_risk_usd=w.plan.total_risk_usd,
                    potential_reward_usd=w.plan.potential_reward_usd,
                    rr=w.plan.rr, approved=True, reason=w.plan.reason,
                )
                open_position = w; continue
            if bars_since >= timeout_bars:
                w.outcome = "timeout_unfilled"
                trades.append(w); continue
            still_waiting.append(w)
        waiting = still_waiting
        equity_curve.iloc[i] = equity

    for w in waiting:
        w.outcome = "timeout_unfilled"
        trades.append(w)

    filled = [t for t in trades if t.outcome in ("target", "stop")]
    wins = [t for t in filled if t.outcome == "target"]
    losses = [t for t in filled if t.outcome == "stop"]
    skipped = [t for t in trades if t.outcome == "skipped"]
    voided = [t for t in trades if t.outcome == "voided_before_entry"]
    timed_out = [t for t in trades if t.outcome == "timeout_unfilled"]
    total_pnl = sum(t.pnl_usd for t in filled)

    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    max_dd_pct = float(drawdown.min()) if len(drawdown) else 0.0
    avg_r = float(np.mean([t.r_multiple for t in filled])) if filled else 0.0
    hit_rate = (len(filled) / len(trades)) if trades else 0.0
    win_rate = (len(wins) / len(filled)) if filled else 0.0

    from collections import Counter
    skip_breakdown = Counter()
    for t in skipped:
        skip_breakdown[(t.skip_reason or "skipped").split(":")[0].strip()] += 1

    stats = {
        "starting_equity": starting_equity, "ending_equity": equity,
        "total_pnl_usd": total_pnl,
        "return_pct": (equity / starting_equity - 1) * 100 if starting_equity else 0,
        "max_drawdown_pct": max_dd_pct * 100,
        "n_setups": len(trades), "n_filled": len(filled),
        "n_wins": len(wins), "n_losses": len(losses),
        "n_skipped": len(skipped), "n_voided": len(voided),
        "n_timed_out": len(timed_out),
        "hit_rate_pct": hit_rate * 100, "win_rate_pct": win_rate * 100,
        "avg_R": avg_r, "expectancy_R": avg_r,
        "loss_tracker": loss_tracker.summary(),
        "skip_breakdown": dict(skip_breakdown),
        "fill_rate_pct": (len(filled) / len(trades) * 100) if trades else 0,
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
        "median_stop_slip_pts": (
            float(np.median(stop_slip_samples)) if stop_slip_samples else 0.0),
        "median_entry_slip_pts": (
            float(np.median(entry_slip_samples)) if entry_slip_samples else 0.0),
        "median_target_slip_pts": (
            float(np.median(target_slip_samples)) if target_slip_samples else 0.0),
        "n_stop_slip_samples": len(stop_slip_samples),
        "biggest_winner_share_pct": _biggest_winner_share_pct(filled),
    }
    return SimResult(trades=trades, equity_curve=equity_curve,
                     starting_equity=starting_equity, stats=stats)
