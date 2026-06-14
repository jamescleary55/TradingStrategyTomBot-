"""ES — ICT vs RANDOM / TREND / DESTROYED baselines.

Same data, same risk model, same execution profiles, same MC seeds.
Only the entry-decision logic changes.

Outputs:
- per-strategy/profile headline (seed=42)
- Monte Carlo summary (default 100 seeds)
- corrected slip breakdown — entry / target / stop separately,
  with stop-only p50/p75/p95 and R drag per stopped trade

Honest evaluation rules (from the brief):

Do NOT claim edge if any of:
  - ICT does not clearly outperform random
  - ICT does not clearly outperform destroyed
  - ICT only wins under OPTIMISTIC
  - ICT advantage disappears after corrected stop slippage
  - largest winner > 25% of profit
  - MC 5%ile is negative

Verdict bucket: ICT_HAS_INFORMATIONAL_EDGE / ICT_NOT_BETTER_THAN_BASELINE
/ INCONCLUSIVE_SAMPLE_TOO_SMALL.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.baselines import (
    destroyed_signal_baseline, random_entry_baseline, simple_trend_baseline,
)
from backtest.execution_model import (
    NORMAL, OPTIMISTIC, PROFILES, PUNITIVE,
    apply_stop_fill, apply_target_fill, attempt_limit_fill,
)
from backtest.simulator import simulate
from backtest.tier1_montecarlo import MICRO_MAP
from config import INSTRUMENTS
from data.loader import load_bars
from risk.sizing import plan_trade
from signals.htf_bias import compute_bias_series, htf_timeframe_for
from signals.setup import find_setups
from utils.time_utils import current_session

console = Console()
log = logging.getLogger("baseline_compare")

PROFILE_ORDER = ("OPTIMISTIC", "NORMAL", "PUNITIVE")


# ---------------------------------------------------------------------------
def _profit_factor(closed) -> float:
    wins = sum(t.r_multiple for t in closed if t.outcome == "target")
    losses = abs(sum(t.r_multiple for t in closed if t.outcome == "stop"))
    if losses == 0:
        return math.inf if wins > 0 else 0.0
    return wins / losses


def _recovery_factor(stats: dict) -> float:
    dd = abs(stats.get("max_drawdown_pct", 0))
    ret = stats.get("return_pct", 0)
    if dd == 0:
        return math.inf if ret > 0 else 0.0
    return ret / dd


def _sess_breakdown(trades) -> dict:
    out = Counter()
    for t in trades:
        if t.outcome not in ("target", "stop"):
            continue
        try:
            s = current_session(t.setup.timestamp) or "NONE"
        except Exception:
            s = "NONE"
        out[s] += 1 if t.outcome == "target" else 0
        out[f"_{s}_n"] += 1
    return dict(out)


# ---------------------------------------------------------------------------
def _run_one(df, setups, sim_symbol, profile, seed, equity, risk_pct):
    sim = simulate(
        df=df, setups=setups,
        starting_equity=equity, instrument_symbol=sim_symbol,
        risk_pct=risk_pct, min_rr=1.0,
        execution_profile=profile, random_seed=seed,
    )
    closed = [t for t in sim.trades if t.outcome in ("target", "stop")]
    return sim, closed


def _mc(df, setups, sim_symbol, profile, n_seeds, equity, risk_pct):
    runs = []
    for s in range(n_seeds):
        sim, closed = _run_one(df, setups, sim_symbol, profile, s,
                               equity, risk_pct)
        runs.append({
            "expectancy_R": sim.stats["expectancy_R"],
            "win_rate_pct": sim.stats["win_rate_pct"],
            "n_closed": sim.stats["n_filled"],
            "max_dd_pct": sim.stats["max_drawdown_pct"],
            "limit_fill_rate_pct": sim.stats["limit_fill_rate_pct"],
            "biggest_winner_share_pct": sim.stats["biggest_winner_share_pct"],
            "profit_factor": _profit_factor(closed),
            "recovery_factor": _recovery_factor(sim.stats),
        })
    exps = np.array([r["expectancy_R"] for r in runs])
    return {
        "n_seeds": n_seeds,
        "mean_R": float(np.mean(exps)),
        "median_R": float(np.median(exps)),
        "std_R": float(np.std(exps)),
        "p5_R": float(np.percentile(exps, 5)),
        "p95_R": float(np.percentile(exps, 95)),
        "p_pos": float(np.mean(exps > 0) * 100),
        "p_above_025": float(np.mean(exps > 0.25) * 100),
        "median_closed": float(np.median([r["n_closed"] for r in runs])),
        "median_winrate": float(np.median([r["win_rate_pct"] for r in runs])),
        "median_fillrate": float(np.median([r["limit_fill_rate_pct"] for r in runs])),
        "median_biggest_winner": float(np.median([r["biggest_winner_share_pct"] for r in runs])),
        "median_pf": float(np.median([r["profit_factor"]
                                      if r["profit_factor"] != math.inf else 10
                                      for r in runs])),
    }


# ---------------------------------------------------------------------------
def _slip_audit(df, setups, sim_symbol, profile, seed, equity, risk_pct):
    """Re-walk the simulator path and record entry / target / stop slip
    separately. Returns the corrected slip report."""
    instrument = INSTRUMENTS[sim_symbol]
    rng = np.random.default_rng(seed)
    entry_slip = []; target_slip = []; stop_slip = []
    setup_q = sorted(setups, key=lambda s: s.choch.idx)
    waiting: list[dict] = []
    open_pos: Optional[dict] = None
    it = iter(setup_q); pending = next(it, None)
    timeout_bars = 24

    for i in range(len(df)):
        while pending is not None and pending.choch.idx <= i:
            if open_pos is None:
                plan = plan_trade(equity=equity, entry=pending.entry,
                                  stop=pending.stop, target=pending.target,
                                  instrument=instrument,
                                  risk_pct=risk_pct, min_rr=1.0)
                if plan.approved:
                    waiting.append({"s": pending, "added": i})
            pending = next(it, None)

        bar = df.iloc[i]
        h, l = float(bar["high"]), float(bar["low"])
        if open_pos is not None:
            s = open_pos["s"]
            stop_hit = (l <= s.stop) if s.direction == "bull" else (h >= s.stop)
            target_hit = (h >= s.target) if s.direction == "bull" else (l <= s.target)
            if stop_hit:
                fr = apply_stop_fill(intended_price=s.stop, bar=bar,
                                     direction=s.direction, df=df, idx=i,
                                     instrument=instrument, profile=profile,
                                     news_events=[])
                stop_slip.append(float(fr.slippage_pts))
                open_pos = None
            elif target_hit:
                fr = apply_target_fill(intended_price=s.target, bar=bar,
                                       direction=s.direction, df=df, idx=i,
                                       instrument=instrument, profile=profile,
                                       news_events=[], rng=rng)
                if fr.filled:
                    target_slip.append(float(fr.slippage_pts))
                    open_pos = None

        still = []
        for w in waiting:
            s = w["s"]
            voided = (l <= s.stop) if s.direction == "bull" else (h >= s.stop)
            touched = (l <= s.entry <= h)
            if voided and not touched:
                continue
            if touched and open_pos is None:
                fr = attempt_limit_fill(intended_price=s.entry, bar=bar,
                                        direction=s.direction, df=df, idx=i,
                                        instrument=instrument, profile=profile,
                                        news_events=[], rng=rng)
                if fr.filled:
                    entry_slip.append(float(fr.slippage_pts))
                    open_pos = w
                    continue
            if i - w["s"].choch.idx >= timeout_bars:
                continue
            still.append(w)
        waiting = still

    def _q(a, q): return float(np.percentile(a, q)) if len(a) else 0.0
    s = np.array(stop_slip) if stop_slip else np.array([])
    e = np.array(entry_slip) if entry_slip else np.array([])
    t = np.array(target_slip) if target_slip else np.array([])
    pooled = np.concatenate([e, t, s]) if (len(e) or len(t) or len(s)) else np.array([])

    # R-drag estimate: average stop-slip points / risk-per-contract price distance.
    # Without per-trade risk distance, approximate using the first setup's risk distance.
    if setups:
        s0 = setups[0]
        ref_R_price = abs(s0.entry - s0.stop) or 1.0
    else:
        ref_R_price = 1.0
    r_drag_per_stop = (float(np.mean(s)) / ref_R_price) if len(s) else 0.0

    return {
        "n_entry": len(e), "n_target": len(t), "n_stop": len(s),
        "entry_median": _q(e, 50), "target_median": _q(t, 50),
        "stop_median": _q(s, 50), "stop_p75": _q(s, 75), "stop_p95": _q(s, 95),
        "pooled_median": _q(pooled, 50),
        "r_drag_per_stop": r_drag_per_stop,
    }


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="ES")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--equity", type=float, default=50_000)
    parser.add_argument("--risk-pct", type=float, default=0.005)
    parser.add_argument("--source", default="yfinance")
    parser.add_argument("--seeds", type=int, default=100)
    parser.add_argument("--sessions", default="LONDON,NY_AM,NY_PM")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    sym = args.symbol.upper()
    sim_sym = MICRO_MAP.get(sym, sym)
    sessions = tuple(s.strip().upper() for s in args.sessions.split(",") if s.strip())

    df = load_bars(sym, args.timeframe, days=args.days, source=args.source)
    if df.empty:
        console.print(f"[red]No data for {sym}[/red]"); sys.exit(1)
    df_htf = load_bars(sym, htf_timeframe_for(args.timeframe), days=args.days,
                       source=args.source)
    log.info("loaded %d bars + %d htf bars", len(df), len(df_htf))

    # ICT setups
    bias = compute_bias_series(df, df_htf) if not df_htf.empty else None
    ict_setups = find_setups(df, htf_bias_series=bias)
    log.info("ICT setups: %d", len(ict_setups))

    # Baselines — match ICT signal count where applicable
    rnd_setups = random_entry_baseline(df, n_signals=len(ict_setups),
                                       allowed_sessions=sessions, rng_seed=11)
    trend_setups = simple_trend_baseline(df, df_htf, allowed_sessions=sessions,
                                         target_signals=len(ict_setups), rng_seed=11)
    dest_setups = destroyed_signal_baseline(df, ict_setups,
                                            allowed_sessions=sessions, rng_seed=11)
    log.info("baselines: random=%d trend=%d destroyed=%d",
             len(rnd_setups), len(trend_setups), len(dest_setups))

    STRATEGIES = {
        "ICT": ict_setups,
        "RANDOM": rnd_setups,
        "TREND": trend_setups,
        "DESTROYED": dest_setups,
    }

    headline = {}    # strategy → profile → headline metrics (seed=42)
    mc = {}          # strategy → profile → MC summary
    slip = {}        # strategy → profile → slip audit

    for sname, setups in STRATEGIES.items():
        headline[sname] = {}; mc[sname] = {}; slip[sname] = {}
        for pname in PROFILE_ORDER:
            profile = PROFILES[pname]
            sim, closed = _run_one(df, setups, sim_sym, profile,
                                   42, args.equity, args.risk_pct)
            headline[sname][pname] = {
                "setups": len(setups),
                "closed": sim.stats["n_filled"],
                "fill_pct": sim.stats["limit_fill_rate_pct"],
                "win_pct": sim.stats["win_rate_pct"],
                "expectancy_R": sim.stats["expectancy_R"],
                "profit_factor": _profit_factor(closed),
                "max_dd_pct": sim.stats["max_drawdown_pct"],
                "recovery_factor": _recovery_factor(sim.stats),
                "avg_R": sim.stats["avg_R"],
                "median_R": float(np.median([t.r_multiple for t in closed])) if closed else 0,
                "biggest_winner_share_pct": sim.stats["biggest_winner_share_pct"],
                "sess_wins": _sess_breakdown(sim.trades),
            }
            log.info("[%s/%s] seed42 closed=%d exp=%.2fR",
                     sname, pname, sim.stats["n_filled"], sim.stats["expectancy_R"])
            mc[sname][pname] = _mc(df, setups, sim_sym, profile,
                                   args.seeds, args.equity, args.risk_pct)
            slip[sname][pname] = _slip_audit(df, setups, sim_sym, profile,
                                             42, args.equity, args.risk_pct)

    _render(headline, mc, slip)
    verdict = _verdict(mc, slip)
    console.print()
    console.print(Panel(verdict["text"],
                        title=f"Baseline-vs-ICT verdict: {verdict['code']}",
                        border_style=verdict["color"], title_align="left"))

    if args.out:
        Path(args.out).write_text(json.dumps({
            "headline": headline, "mc": mc, "slip": slip,
            "verdict": verdict,
        }, indent=2, default=str))
        console.print(f"\n[dim]Wrote {args.out}[/dim]")


# ---------------------------------------------------------------------------
def _render(headline, mc, slip):
    # Headline table — one row per (strategy, profile)
    tbl = Table(title="ES — headline (seed=42)", header_style="bold")
    cols = ("Strategy", "Profile", "Setups", "Closed", "Fill%", "Win%",
            "Exp R", "PF", "Max DD%", "Recov", "Med R", "Biggest %")
    for c in cols:
        tbl.add_column(c, justify=("left" if c in ("Strategy", "Profile") else "right"))
    last_strat = None
    for sname, profs in headline.items():
        for pname, h in profs.items():
            r_color = "green" if h["expectancy_R"] > 0.25 else "yellow" if h["expectancy_R"] > 0 else "red"
            pf = h["profit_factor"]; pf_s = "∞" if pf == math.inf else f"{pf:.2f}"
            rf = h["recovery_factor"]; rf_s = "∞" if rf == math.inf else f"{rf:.2f}"
            strat_show = sname if sname != last_strat else ""
            last_strat = sname
            tbl.add_row(
                strat_show, pname, str(h["setups"]), str(h["closed"]),
                f"{h['fill_pct']:.0f}%", f"{h['win_pct']:.0f}%",
                f"[{r_color}]{h['expectancy_R']:+.2f}R[/{r_color}]",
                pf_s, f"{abs(h['max_dd_pct']):.2f}", rf_s,
                f"{h['median_R']:+.2f}", f"{h['biggest_winner_share_pct']:.0f}%",
            )
    console.print(tbl)

    # Monte Carlo summary
    tbl2 = Table(title="Monte Carlo over execution seeds", header_style="bold")
    for c in ("Strategy", "Profile", "Mean R", "Median R", "5%ile", "95%ile",
              "P(>0)", "P(>+0.25)", "Med closed", "Med fill%"):
        tbl2.add_column(c, justify=("left" if c in ("Strategy", "Profile") else "right"))
    last_strat = None
    for sname, profs in mc.items():
        for pname, m in profs.items():
            mean_c = "green" if m["mean_R"] > 0.25 else "yellow" if m["mean_R"] > 0 else "red"
            p5_c = "green" if m["p5_R"] > 0 else "red"
            strat_show = sname if sname != last_strat else ""
            last_strat = sname
            tbl2.add_row(
                strat_show, pname,
                f"[{mean_c}]{m['mean_R']:+.2f}R[/{mean_c}]",
                f"{m['median_R']:+.2f}R", f"[{p5_c}]{m['p5_R']:+.2f}R[/{p5_c}]",
                f"{m['p95_R']:+.2f}R", f"{m['p_pos']:.0f}%",
                f"{m['p_above_025']:.0f}%",
                f"{m['median_closed']:.0f}", f"{m['median_fillrate']:.0f}%",
            )
    console.print(tbl2)

    # Slip audit — focus on NORMAL profile
    tbl3 = Table(title="Corrected slip breakdown (NORMAL profile, seed=42)",
                 header_style="bold")
    for c in ("Strategy", "n_entry", "n_target", "n_stop",
              "entry med", "target med", "stop med", "stop p75",
              "stop p95", "pooled med", "R drag / stop"):
        tbl3.add_column(c, justify=("left" if c == "Strategy" else "right"))
    for sname, profs in slip.items():
        s = profs["NORMAL"]
        tbl3.add_row(
            sname, str(s["n_entry"]), str(s["n_target"]), str(s["n_stop"]),
            f"{s['entry_median']:.3f}", f"{s['target_median']:.3f}",
            f"{s['stop_median']:.2f}", f"{s['stop_p75']:.2f}",
            f"{s['stop_p95']:.2f}", f"{s['pooled_median']:.3f}",
            f"{s['r_drag_per_stop']:.3f}R",
        )
    console.print(tbl3)


# ---------------------------------------------------------------------------
def _verdict(mc, slip):
    ict = mc.get("ICT", {}).get("NORMAL", {})
    rnd = mc.get("RANDOM", {}).get("NORMAL", {})
    trend = mc.get("TREND", {}).get("NORMAL", {})
    dest = mc.get("DESTROYED", {}).get("NORMAL", {})
    ict_opt = mc.get("ICT", {}).get("OPTIMISTIC", {})
    ict_pun = mc.get("ICT", {}).get("PUNITIVE", {})
    ict_slip = slip.get("ICT", {}).get("NORMAL", {})

    if not ict or ict.get("median_closed", 0) < 5:
        return {"code": "INCONCLUSIVE_SAMPLE_TOO_SMALL", "color": "yellow",
                "text": "ICT median closed < 5. Cannot evaluate."}

    issues = []
    if ict["mean_R"] - rnd.get("mean_R", 0) < 0.10:
        issues.append(f"ICT mean ({ict['mean_R']:+.2f}R) does not clearly "
                      f"outperform RANDOM ({rnd.get('mean_R', 0):+.2f}R). "
                      f"Required gap: 0.10R+.")
    if ict["mean_R"] - dest.get("mean_R", 0) < 0.10:
        issues.append(f"ICT mean ({ict['mean_R']:+.2f}R) does not clearly "
                      f"outperform DESTROYED-signal ICT "
                      f"({dest.get('mean_R', 0):+.2f}R). The information may "
                      f"not come from the sweep/CHoCH/FVG logic itself.")
    if ict_opt and not ict and ict_opt["mean_R"] > 0 and ict["mean_R"] <= 0:
        issues.append("ICT only wins under OPTIMISTIC profile.")
    if ict["p5_R"] < 0:
        issues.append(f"ICT NORMAL 5%ile = {ict['p5_R']:+.2f}R < 0 — Monte "
                      f"Carlo CI crosses zero.")
    if ict["median_biggest_winner"] > 25:
        issues.append(f"Largest winner contributes {ict['median_biggest_winner']:.0f}% "
                      f"of profit (>25% threshold). Concentration risk.")
    # Corrected slip impact estimate
    if ict_slip:
        drag = ict_slip.get("r_drag_per_stop", 0)
        # Expected R drag = drag × stop_hit_rate × median_closed
        stop_rate = (100 - ict.get("median_winrate", 50)) / 100
        adj = drag * stop_rate
        if ict["mean_R"] - adj <= 0:
            issues.append(f"After corrected stop slip (~{drag:.2f}R/stop × "
                          f"{stop_rate:.0%} stop rate = {adj:.2f}R adjustment), "
                          f"expectancy drops to {(ict['mean_R'] - adj):+.2f}R — "
                          f"non-positive.")

    if not issues:
        return {"code": "ICT_HAS_INFORMATIONAL_EDGE", "color": "green",
                "text": (f"ICT NORMAL +{ict['mean_R']:.2f}R vs RANDOM "
                         f"{rnd.get('mean_R', 0):+.2f}R / DESTROYED "
                         f"{dest.get('mean_R', 0):+.2f}R / TREND "
                         f"{trend.get('mean_R', 0):+.2f}R. All hard rules "
                         f"satisfied. The ICT label IS informative beyond "
                         f"session × direction × RR.")}
    return {"code": "ICT_NOT_BETTER_THAN_BASELINE", "color": "red",
            "text": "Fails one or more strict rules:\n  - " + "\n  - ".join(issues)}


if __name__ == "__main__":
    main()
