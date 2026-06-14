"""Validate ``simulate_controlled`` against ``simulate`` on isolated
single-setup inputs.

Both simulators run the same setup, same data, same profile. Only
RNG keying differs:
- ``simulate`` seeds a single stream rng from ``random_seed``.
- ``simulate_controlled`` seeds a per-setup rng from
  ``hash(master_seed, profile.name, setup_identity)``.

Statistical claim under test: the DISTRIBUTIONS over many seeds
should be statistically indistinguishable. If they differ, the
controlled simulator has a bug and every conclusion that depends
on it is suspect.

Method: pick ~10 representative ES setups, run both sims for each
across N seeds, compare per-setup distributions on:
- fill rate, partial rate, miss rate
- target hit rate, stop hit rate
- mean / median R-multiple
- slip distributions

Outputs a verdict per setup and an overall pass/fail.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.execution_model import PROFILES
from backtest.simulator import simulate
from backtest.simulator_controlled import simulate_controlled
from backtest.tier1_montecarlo import MICRO_MAP
from data.loader import load_bars
from signals.htf_bias import compute_bias_series, htf_timeframe_for
from signals.setup import find_setups

console = Console()
log = logging.getLogger("validate_sim")


def _run(simulator, df, setups, sim_symbol, profile, seed, equity, risk_pct):
    if simulator is simulate_controlled:
        sim = simulator(df=df, setups=setups,
                        starting_equity=equity, instrument_symbol=sim_symbol,
                        risk_pct=risk_pct, min_rr=1.0,
                        execution_profile=profile, master_seed=seed)
    else:
        sim = simulator(df=df, setups=setups,
                        starting_equity=equity, instrument_symbol=sim_symbol,
                        risk_pct=risk_pct, min_rr=1.0,
                        execution_profile=profile, random_seed=seed)
    closed = [t for t in sim.trades if t.outcome in ("target", "stop")]
    return sim, closed


def _summarise(runs):
    """Collapse N simulator runs of a single setup into outcome stats."""
    outcomes = Counter()
    rs = []
    slips = []
    for sim, closed in runs:
        st = sim.stats
        out_counter = Counter(t.outcome for t in sim.trades)
        outcomes["filled_full"] += st["limit_filled_full"]
        outcomes["filled_partial"] += st["limit_filled_partial"]
        outcomes["limit_attempts"] += st["limit_attempts"]
        outcomes["limit_missed"] += st["limit_missed"]
        outcomes["target"] += out_counter.get("target", 0)
        outcomes["stop"] += out_counter.get("stop", 0)
        outcomes["voided"] += out_counter.get("voided_before_entry", 0)
        outcomes["timeout"] += out_counter.get("timeout_unfilled", 0)
        outcomes["skipped"] += out_counter.get("skipped", 0)
        for t in closed:
            rs.append(t.r_multiple)
        slips.append(st["avg_slippage_pts"])
    return {
        "n_runs": len(runs),
        "limit_attempts": outcomes["limit_attempts"],
        "fill_rate_pct": (outcomes["filled_full"] + outcomes["filled_partial"])
                         / max(1, outcomes["limit_attempts"]) * 100,
        "partial_rate_pct": outcomes["filled_partial"]
                            / max(1, outcomes["filled_full"] + outcomes["filled_partial"]) * 100,
        "target_pct": outcomes["target"] / max(1, len(runs)) * 100,
        "stop_pct": outcomes["stop"] / max(1, len(runs)) * 100,
        "voided_pct": outcomes["voided"] / max(1, len(runs)) * 100,
        "timeout_pct": outcomes["timeout"] / max(1, len(runs)) * 100,
        "n_closed": len(rs),
        "mean_R": float(np.mean(rs)) if rs else 0.0,
        "median_R": float(np.median(rs)) if rs else 0.0,
        "mean_avg_slip": float(np.mean(slips)) if slips else 0.0,
    }


def _ks_close(a: float, b: float, tol: float = 0.05) -> bool:
    """Absolute difference test."""
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b)) + 0.02


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="ES")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--source", default="yfinance")
    parser.add_argument("--seeds", type=int, default=300)
    parser.add_argument("--n-setups", type=int, default=10)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    sym = args.symbol.upper()
    df = load_bars(sym, "1h", days=args.days, source=args.source)
    df_htf = load_bars(sym, htf_timeframe_for("1h"), days=args.days, source=args.source)
    bias = compute_bias_series(df, df_htf) if not df_htf.empty else None
    setups = find_setups(df, htf_bias_series=bias)
    log.info("Loaded %d %s setups", len(setups), sym)

    # Spread the chosen setups across the timeline so we sample different regimes
    if len(setups) <= args.n_setups:
        test_setups = setups
    else:
        step = len(setups) // args.n_setups
        test_setups = setups[::step][:args.n_setups]
    log.info("Testing %d setups", len(test_setups))

    profile = PROFILES["NORMAL"]
    sim_sym = MICRO_MAP.get(sym, sym)
    rows = []
    failures = []
    for i, setup in enumerate(test_setups):
        # Wrap single setup as the only one in the run
        orig_runs = [
            _run(simulate, df, [setup], sim_sym, profile, s, 100_000, 0.005)
            for s in range(args.seeds)
        ]
        ctrl_runs = [
            _run(simulate_controlled, df, [setup], sim_sym, profile, s, 100_000, 0.005)
            for s in range(args.seeds)
        ]
        orig = _summarise(orig_runs)
        ctrl = _summarise(ctrl_runs)
        # Check distributions for closeness
        ok_fill = _ks_close(orig["fill_rate_pct"], ctrl["fill_rate_pct"])
        ok_tgt = _ks_close(orig["target_pct"], ctrl["target_pct"])
        ok_stp = _ks_close(orig["stop_pct"], ctrl["stop_pct"])
        ok_mr = _ks_close(orig["mean_R"], ctrl["mean_R"], tol=0.1)
        all_ok = ok_fill and ok_tgt and ok_stp and ok_mr
        rows.append({
            "setup_idx": i, "timestamp": str(setup.timestamp),
            "direction": setup.direction,
            "orig": orig, "ctrl": ctrl,
            "pass": all_ok,
            "fail_fields": [k for k, v in
                            [("fill_rate", ok_fill), ("target", ok_tgt),
                             ("stop", ok_stp), ("mean_R", ok_mr)]
                            if not v],
        })
        if not all_ok:
            failures.append(rows[-1])
        log.info("[setup %d] orig fill %.0f%% / target %.0f%% / mean R %+.2f  "
                 "vs ctrl fill %.0f%% / target %.0f%% / mean R %+.2f  → %s",
                 i, orig["fill_rate_pct"], orig["target_pct"], orig["mean_R"],
                 ctrl["fill_rate_pct"], ctrl["target_pct"], ctrl["mean_R"],
                 "PASS" if all_ok else f"FAIL ({rows[-1]['fail_fields']})")

    # ---- render ----
    tbl = Table(title=f"{sym} — single-setup distribution comparison "
                      f"(n={args.seeds} seeds per cell)", header_style="bold")
    for c in ("Idx", "Timestamp", "Dir",
              "Orig fill%", "Ctrl fill%", "Orig tgt%", "Ctrl tgt%",
              "Orig stop%", "Ctrl stop%", "Orig mean R", "Ctrl mean R",
              "Verdict"):
        tbl.add_column(c, justify=("left" if c in ("Idx", "Timestamp", "Dir") else "right"))
    for r in rows:
        v = "[green]PASS[/green]" if r["pass"] else f"[red]FAIL ({','.join(r['fail_fields'])})[/red]"
        tbl.add_row(
            str(r["setup_idx"]), r["timestamp"][:16], r["direction"],
            f"{r['orig']['fill_rate_pct']:.0f}%",
            f"{r['ctrl']['fill_rate_pct']:.0f}%",
            f"{r['orig']['target_pct']:.0f}%",
            f"{r['ctrl']['target_pct']:.0f}%",
            f"{r['orig']['stop_pct']:.0f}%",
            f"{r['ctrl']['stop_pct']:.0f}%",
            f"{r['orig']['mean_R']:+.2f}",
            f"{r['ctrl']['mean_R']:+.2f}",
            v,
        )
    console.print(tbl)

    total = len(rows); passed = sum(1 for r in rows if r["pass"])
    overall = "PASS" if passed == total else "FAIL"
    color = "green" if overall == "PASS" else "red"
    console.print(Panel(
        f"{passed}/{total} setups passed within tolerance.\n"
        + ("Controlled simulator validated — distributions match the original."
           if overall == "PASS" else
           f"FAILED setups: {[r['setup_idx'] for r in failures]}. "
           f"Investigate simulator_controlled before trusting any prior result that used it."),
        title=f"Controlled-simulator validation: {overall}",
        border_style=color, title_align="left",
    ))

    if args.out:
        Path(args.out).write_text(json.dumps({
            "symbol": sym, "n_seeds": args.seeds, "rows": rows,
            "overall": overall,
        }, indent=2, default=str))
        console.print(f"\n[dim]Wrote {args.out}[/dim]")


if __name__ == "__main__":
    main()
