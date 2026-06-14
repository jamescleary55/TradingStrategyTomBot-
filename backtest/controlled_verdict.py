"""Controlled-RNG re-run: ES at $50k/$100k/$150k/$200k + stop-distance
bucket analysis for ES and NQ.

P1 question: when account size changes, the OLDER simulator's
expectancy could shift either because new (lower-quality) setups are
admitted OR because the global RNG path diverged. The controlled
simulator (per-setup deterministic RNG) eliminates the second cause.

P2 question: bucket setups by stop distance (quantiles). Are tight-
stop setups genuinely higher-expectancy, or did the small-account
risk cap accidentally select for some other correlate?

Outputs:
- ES four-account comparison with shared-vs-new breakdown
- ES + NQ bucket-by-stop-distance results
- Final ES vs NQ verdict separating trading from research recs
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.execution_model import PROFILES
from backtest.simulator_controlled import (
    _setup_identity, simulate_controlled,
)
from backtest.tier1_montecarlo import MICRO_MAP
from data.loader import load_bars
from signals.htf_bias import compute_bias_series, htf_timeframe_for
from signals.setup import find_setups

console = Console()
log = logging.getLogger("controlled_verdict")

ACCOUNTS = (50_000, 100_000, 150_000, 200_000)
PROFILES_ORDER = ("OPTIMISTIC", "NORMAL", "PUNITIVE")


@dataclass
class Cache:
    symbol: str
    sim_symbol: str
    df: pd.DataFrame
    setups: list


def _load(symbols, timeframe, days, source) -> dict[str, Cache]:
    out = {}
    for sym in symbols:
        df = load_bars(sym, timeframe, days=days, source=source)
        if df.empty:
            log.error("[%s] no data", sym); continue
        df_htf = load_bars(sym, htf_timeframe_for(timeframe), days=days,
                           source=source)
        bias = compute_bias_series(df, df_htf) if not df_htf.empty else None
        setups = find_setups(df, htf_bias_series=bias)
        out[sym] = Cache(symbol=sym, sim_symbol=MICRO_MAP.get(sym, sym),
                         df=df, setups=setups)
        log.info("[%s] bars=%d setups=%d", sym, len(df), len(setups))
    return out


def _profit_factor(closed) -> float:
    wins = sum(t.r_multiple for t in closed if t.outcome == "target")
    losses = abs(sum(t.r_multiple for t in closed if t.outcome == "stop"))
    if losses == 0:
        return math.inf if wins > 0 else 0.0
    return wins / losses


def _run(cache: Cache, equity, profile, seed):
    sim = simulate_controlled(
        df=cache.df, setups=cache.setups,
        starting_equity=equity, instrument_symbol=cache.sim_symbol,
        risk_pct=0.005, min_rr=1.0,
        execution_profile=profile, master_seed=seed,
    )
    closed = [t for t in sim.trades if t.outcome in ("target", "stop")]
    return sim, closed


# ---------------------------------------------------------------------------
# Phase 1 — RNG-controlled account-size comparison on ES
# ---------------------------------------------------------------------------
def phase1(cache_es: Cache, n_seeds: int):
    profile = PROFILES["NORMAL"]
    # First, for each account size, identify which setups GOT ACCEPTED.
    accepted_by_acct: dict[int, set[str]] = {}
    for eq in ACCOUNTS:
        sim, _ = _run(cache_es, eq, profile, seed=42)
        accepted = {
            _setup_identity(t.setup) for t in sim.trades
            if t.outcome != "skipped"
        }
        accepted_by_acct[eq] = accepted
        log.info("[ES/$%dk] accepted=%d", eq // 1000, len(accepted))

    base = accepted_by_acct[ACCOUNTS[0]]    # $50k
    shared: dict[int, set[str]] = {eq: accepted_by_acct[eq] & base
                                   for eq in ACCOUNTS}
    newly: dict[int, set[str]] = {eq: accepted_by_acct[eq] - base
                                  for eq in ACCOUNTS}

    # Now Monte Carlo, but partition trades into shared vs newly-accepted at each acct.
    out = {}
    for eq in ACCOUNTS:
        exps_all = []; exps_shared = []; exps_new = []
        n_shared_closed_per_run = []; n_new_closed_per_run = []
        for s in range(n_seeds):
            sim, closed = _run(cache_es, eq, profile, seed=s)
            shared_closed = [t for t in closed
                             if _setup_identity(t.setup) in shared[eq]]
            new_closed = [t for t in closed
                          if _setup_identity(t.setup) in newly[eq]]
            exps_all.append(sim.stats["expectancy_R"])
            exps_shared.append(
                float(np.mean([t.r_multiple for t in shared_closed]))
                if shared_closed else 0.0
            )
            exps_new.append(
                float(np.mean([t.r_multiple for t in new_closed]))
                if new_closed else 0.0
            )
            n_shared_closed_per_run.append(len(shared_closed))
            n_new_closed_per_run.append(len(new_closed))
        out[eq] = {
            "all": {
                "mean": float(np.mean(exps_all)),
                "median": float(np.median(exps_all)),
                "p5": float(np.percentile(exps_all, 5)),
            },
            "shared": {
                "n": len(shared[eq]),
                "median_closed": float(np.median(n_shared_closed_per_run)),
                "mean_R": float(np.mean(exps_shared)),
            },
            "newly_accepted": {
                "n": len(newly[eq]),
                "median_closed": float(np.median(n_new_closed_per_run)),
                "mean_R": float(np.mean(exps_new)),
            },
        }
    return out


# ---------------------------------------------------------------------------
# Phase 2 — stop-distance buckets
# ---------------------------------------------------------------------------
def phase2(cache: Cache, account: int, n_seeds: int):
    """Bucket setups by stop distance (terciles); run sim filtered to each."""
    setups = cache.setups
    dists = np.array([abs(s.entry - s.stop) for s in setups])
    if len(dists) < 3:
        return {}
    q33 = float(np.quantile(dists, 1/3))
    q66 = float(np.quantile(dists, 2/3))
    buckets = {"tight": [], "medium": [], "wide": []}
    for s in setups:
        d = abs(s.entry - s.stop)
        if d <= q33:
            buckets["tight"].append(s)
        elif d <= q66:
            buckets["medium"].append(s)
        else:
            buckets["wide"].append(s)

    profile = PROFILES["NORMAL"]
    out = {}
    for bname, subset in buckets.items():
        if not subset:
            out[bname] = {"n_setups": 0}
            continue
        exps = []; closed_n = []; winrates = []
        dds = []; fills = []
        for s in range(n_seeds):
            sub_cache = Cache(symbol=cache.symbol, sim_symbol=cache.sim_symbol,
                              df=cache.df, setups=subset)
            sim, closed = _run(sub_cache, account, profile, seed=s)
            exps.append(sim.stats["expectancy_R"])
            closed_n.append(sim.stats["n_filled"])
            winrates.append(sim.stats["win_rate_pct"])
            dds.append(abs(sim.stats["max_drawdown_pct"]))
            fills.append(sim.stats["limit_fill_rate_pct"])
        exps = np.array(exps)
        avg_stop_dist = float(np.mean([abs(x.entry - x.stop) for x in subset]))
        out[bname] = {
            "n_setups": len(subset),
            "avg_stop_dist": avg_stop_dist,
            "mean_R": float(np.mean(exps)), "median_R": float(np.median(exps)),
            "p5_R": float(np.percentile(exps, 5)),
            "p95_R": float(np.percentile(exps, 95)),
            "p_pos": float(np.mean(exps > 0) * 100),
            "p_above_025": float(np.mean(exps > 0.25) * 100),
            "median_closed": float(np.median(closed_n)),
            "median_winrate": float(np.median(winrates)),
            "median_dd_pct": float(np.median(dds)),
            "median_fill": float(np.median(fills)),
        }
    out["thresholds"] = {"q33": q33, "q66": q66}
    return out


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--source", default="yfinance")
    parser.add_argument("--seeds", type=int, default=100)
    parser.add_argument("--bucket-account", type=int, default=150_000)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    cache = _load(("ES", "NQ"), "1h", args.days, args.source)
    if "ES" not in cache:
        console.print("[red]No ES data.[/red]"); sys.exit(1)

    log.info("PHASE 1 — controlled RNG account-size sweep on ES")
    p1 = phase1(cache["ES"], args.seeds)

    log.info("PHASE 2 — stop-distance buckets at $%dk", args.bucket_account // 1000)
    p2 = {"ES": phase2(cache["ES"], args.bucket_account, args.seeds)}
    if "NQ" in cache:
        p2["NQ"] = phase2(cache["NQ"], args.bucket_account, args.seeds)

    _render_p1(p1)
    _render_p2(p2)
    verdict = _verdict(p1, p2)
    console.print()
    console.print(Panel(verdict, title="Controlled verdict",
                        border_style="magenta", title_align="left"))

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"phase1": p1, "phase2": p2, "verdict": verdict},
            indent=2, default=str))
        console.print(f"\n[dim]Wrote {args.out}[/dim]")


def _render_p1(p1):
    tbl = Table(title="Phase 1 — ES under controlled per-setup RNG (NORMAL)",
                header_style="bold")
    for c in ("Acct", "Accepted total", "Shared w/ $50k", "Mean R (all)",
              "Median R (all)", "5%ile (all)", "Mean R (shared only)",
              "New admits", "Mean R (new admits only)", "Closed new"):
        tbl.add_column(c, justify=("left" if c == "Acct" else "right"))
    for eq, r in p1.items():
        all_ = r["all"]; sh = r["shared"]; nw = r["newly_accepted"]
        color = "green" if all_["mean"] > 0.25 else "yellow" if all_["mean"] > 0 else "red"
        tbl.add_row(
            f"${eq // 1000}k", str(sh["n"] + nw["n"]), str(sh["n"]),
            f"[{color}]{all_['mean']:+.2f}R[/{color}]",
            f"{all_['median']:+.2f}R", f"{all_['p5']:+.2f}R",
            f"{sh['mean_R']:+.2f}R", str(nw["n"]),
            f"{nw['mean_R']:+.2f}R" if nw['n'] else "—",
            f"{nw['median_closed']:.0f}",
        )
    console.print(tbl)


def _render_p2(p2):
    for sym, buckets in p2.items():
        if not buckets:
            continue
        th = buckets.get("thresholds", {})
        tbl = Table(title=f"Phase 2 — {sym} stop-distance buckets at NORMAL "
                          f"(q33={th.get('q33', 0):.2f}, q66={th.get('q66', 0):.2f})",
                    header_style="bold")
        for c in ("Bucket", "N setups", "Avg stop dist", "Mean R", "Median R",
                  "5%ile", "P(>0)", "P(>+0.25)", "Med closed", "Win%", "Med DD%"):
            tbl.add_column(c, justify=("left" if c == "Bucket" else "right"))
        for bname in ("tight", "medium", "wide"):
            d = buckets.get(bname)
            if not d or d.get("n_setups", 0) == 0:
                tbl.add_row(bname, "0", "—", "—", "—", "—", "—", "—", "—", "—", "—")
                continue
            mc = "green" if d["mean_R"] > 0.25 else "yellow" if d["mean_R"] > 0 else "red"
            p5c = "green" if d["p5_R"] > 0 else "red"
            tbl.add_row(
                bname, str(d["n_setups"]),
                f"{d['avg_stop_dist']:.2f}",
                f"[{mc}]{d['mean_R']:+.2f}R[/{mc}]",
                f"{d['median_R']:+.2f}R",
                f"[{p5c}]{d['p5_R']:+.2f}R[/{p5c}]",
                f"{d['p_pos']:.0f}%", f"{d['p_above_025']:.0f}%",
                f"{d['median_closed']:.0f}",
                f"{d['median_winrate']:.0f}%",
                f"{d['median_dd_pct']:.2f}",
            )
        console.print(tbl)


def _verdict(p1, p2) -> str:
    es_50k = p1[50_000]["all"]["mean"]
    es_100k = p1[100_000]["all"]["mean"]
    es_150k = p1[150_000]["all"]["mean"]
    es_200k = p1[200_000]["all"]["mean"]
    drop_50_100 = es_100k - es_50k
    shared_drift = (p1[100_000]["shared"]["mean_R"]
                    - p1[50_000]["shared"]["mean_R"])
    new_mean_100 = p1[100_000]["newly_accepted"]["mean_R"]
    n_new_100 = p1[100_000]["newly_accepted"]["n"]

    parts = []
    parts.append(f"ES $50k mean = {es_50k:+.2f}R; $100k = {es_100k:+.2f}R "
                 f"(Δ {drop_50_100:+.2f}R); $150k = {es_150k:+.2f}R; "
                 f"$200k = {es_200k:+.2f}R.")
    parts.append(f"Shared-setups drift (should be ~0 under controlled RNG): "
                 f"{shared_drift:+.3f}R.")
    if n_new_100:
        parts.append(f"Newly admitted at $100k: n={n_new_100}, "
                     f"mean R = {new_mean_100:+.2f}R "
                     f"({'lower' if new_mean_100 < es_50k else 'higher'} "
                     f"than the $50k accepted average).")

    es_buckets = p2.get("ES", {})
    nq_buckets = p2.get("NQ", {})
    if es_buckets:
        t = es_buckets.get("tight", {})
        w = es_buckets.get("wide", {})
        if t.get("n_setups") and w.get("n_setups"):
            parts.append(f"\nES tight-stop bucket: {t['mean_R']:+.2f}R "
                         f"(n={t['n_setups']}); wide: {w['mean_R']:+.2f}R "
                         f"(n={w['n_setups']}). Tight "
                         f"{'BEATS' if t['mean_R'] > w['mean_R'] + 0.1 else 'does NOT clearly beat'} "
                         f"wide.")
    if nq_buckets:
        t = nq_buckets.get("tight", {})
        w = nq_buckets.get("wide", {})
        if t.get("n_setups") and w.get("n_setups"):
            parts.append(f"NQ tight-stop bucket: {t['mean_R']:+.2f}R "
                         f"(n={t['n_setups']}); wide: {w['mean_R']:+.2f}R "
                         f"(n={w['n_setups']}). Tight "
                         f"{'BEATS' if t['mean_R'] > w['mean_R'] + 0.1 else 'does NOT clearly beat'} "
                         f"wide.")
    return "\n".join(parts)


if __name__ == "__main__":
    main()
