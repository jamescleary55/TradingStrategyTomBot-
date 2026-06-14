"""Account-size sweep for NQ and ES.

Hypothesis under test:
NQ was previously classified INSUFFICIENT_DATA because 27/31 setups
were rejected by the $250/trade risk budget on a $50k account. If
true, scaling the account to $100-200k should let more NQ setups
through and reveal whether the strategy on NQ is statistically
meaningful or just structurally undersized.

The strategy, filters, sessions, profile knobs, timeframe, and
data source are UNCHANGED. Only `starting_equity` varies (which
moves the per-trade risk dollar cap).

Outputs:
  - Capacity table — how many setups make it through risk-sizing
  - Headline run (seed 42) per (symbol, account, profile)
  - Monte Carlo over 200 seeds per cell
  - Per-cell verdict + symbol recommendation per account size
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

import numpy as np
import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.execution_model import PROFILES
from backtest.simulator import simulate
from backtest.tier1_montecarlo import MICRO_MAP
from data.loader import load_bars
from signals.htf_bias import compute_bias_series, htf_timeframe_for
from signals.setup import find_setups

console = Console()
log = logging.getLogger("acct_size")

PROFILE_ORDER = ("OPTIMISTIC", "NORMAL", "PUNITIVE")
ACCOUNTS = (100_000, 150_000, 200_000)
SYMBOLS = ("NQ", "ES")


# ---------------------------------------------------------------------------
@dataclass
class Cache:
    symbol: str
    sim_symbol: str
    df: pd.DataFrame
    setups: list


def _prepare(symbols, timeframe, days, source) -> dict[str, Cache]:
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


# ---------------------------------------------------------------------------
def _profit_factor(closed) -> float:
    wins = sum(t.r_multiple for t in closed if t.outcome == "target")
    losses = abs(sum(t.r_multiple for t in closed if t.outcome == "stop"))
    if losses == 0:
        return math.inf if wins > 0 else 0.0
    return wins / losses


def _recovery(stats) -> float:
    dd = abs(stats.get("max_drawdown_pct", 0))
    ret = stats.get("return_pct", 0)
    if dd == 0:
        return math.inf if ret > 0 else 0.0
    return ret / dd


def _run_one(cache: Cache, profile, equity, risk_pct, seed):
    sim = simulate(
        df=cache.df, setups=cache.setups,
        starting_equity=equity, instrument_symbol=cache.sim_symbol,
        risk_pct=risk_pct, min_rr=1.0,
        execution_profile=profile, random_seed=seed,
    )
    closed = [t for t in sim.trades if t.outcome in ("target", "stop")]
    skip_breakdown = Counter()
    for t in sim.trades:
        if t.outcome == "skipped":
            key = (t.skip_reason or "skipped").split(":")[0].strip()
            skip_breakdown[key] += 1
    return sim, closed, skip_breakdown


def _capacity(cache: Cache, equity, risk_pct):
    """Capacity uses OPTIMISTIC profile and seed 0 — execution-independent.
    Acceptance is governed by risk_sizing.plan_trade only."""
    sim, _, skip = _run_one(cache, PROFILES["OPTIMISTIC"],
                            equity, risk_pct, seed=0)
    total = len(sim.trades)
    skipped_size = skip.get("risk-per-contract exceeds per-trade cap", 0)
    skipped_other = sum(v for k, v in skip.items()
                        if k != "risk-per-contract exceeds per-trade cap")
    accepted = total - skipped_size - skipped_other
    return {
        "setups": total,
        "accepted": accepted,
        "rejected_risk_cap": skipped_size,
        "rejected_other": skipped_other,
        "accept_pct": (accepted / total * 100) if total else 0,
    }


def _mc(cache: Cache, profile, equity, risk_pct, n_seeds):
    exps = []; closed_n = []; dds = []; winrates = []; fills = []
    biggest = []
    for s in range(n_seeds):
        sim, closed, _ = _run_one(cache, profile, equity, risk_pct, s)
        exps.append(sim.stats["expectancy_R"])
        closed_n.append(sim.stats["n_filled"])
        dds.append(abs(sim.stats["max_drawdown_pct"]))
        winrates.append(sim.stats["win_rate_pct"])
        fills.append(sim.stats["limit_fill_rate_pct"])
        biggest.append(sim.stats["biggest_winner_share_pct"])
    e = np.array(exps)
    # Map drawdown pct → drawdown in R-units: dd_dollars / R-risk
    R_usd = max(1.0, equity * risk_pct)
    dd_R = np.array([d / 100 * equity / R_usd for d in dds])
    return {
        "n_seeds": n_seeds,
        "mean_R": float(np.mean(e)),
        "median_R": float(np.median(e)),
        "std_R": float(np.std(e)),
        "p5_R": float(np.percentile(e, 5)),
        "p95_R": float(np.percentile(e, 95)),
        "p_pos": float(np.mean(e > 0) * 100),
        "p_above_025": float(np.mean(e > 0.25) * 100),
        "p_dd_above_3R": float(np.mean(dd_R > 3.0) * 100),
        "median_closed": float(np.median(closed_n)),
        "median_winrate": float(np.median(winrates)),
        "median_fill": float(np.median(fills)),
        "median_biggest": float(np.median(biggest)),
    }


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--risk-pct", type=float, default=0.005)
    parser.add_argument("--source", default="yfinance")
    parser.add_argument("--seeds", type=int, default=200)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    cache = _prepare(SYMBOLS, args.timeframe, args.days, args.source)
    if not cache:
        console.print("[red]No data.[/red]"); sys.exit(1)

    # --- P1 capacity ---
    capacity = {}
    for sym, c in cache.items():
        capacity[sym] = {}
        for eq in ACCOUNTS:
            log.info("[%s/$%dk] capacity …", sym, eq // 1000)
            capacity[sym][eq] = _capacity(c, eq, args.risk_pct)

    # --- P2 headline run + P3 MC ---
    headline = {}    # sym → eq → profile → headline
    mc = {}          # sym → eq → profile → mc
    for sym, c in cache.items():
        headline[sym] = {}; mc[sym] = {}
        for eq in ACCOUNTS:
            headline[sym][eq] = {}; mc[sym][eq] = {}
            for pname in PROFILE_ORDER:
                profile = PROFILES[pname]
                sim, closed, _ = _run_one(c, profile, eq, args.risk_pct, 42)
                headline[sym][eq][pname] = {
                    "closed": sim.stats["n_filled"],
                    "fill_pct": sim.stats["limit_fill_rate_pct"],
                    "win_pct": sim.stats["win_rate_pct"],
                    "expectancy_R": sim.stats["expectancy_R"],
                    "profit_factor": _profit_factor(closed),
                    "max_dd_pct": sim.stats["max_drawdown_pct"],
                    "recovery": _recovery(sim.stats),
                    "avg_R": sim.stats["avg_R"],
                    "median_R": float(np.median([t.r_multiple for t in closed]))
                                 if closed else 0.0,
                    "avg_slippage_pts": sim.stats["avg_slippage_pts"],
                    "median_slippage_pts": sim.stats["median_slippage_pts"],
                }
                log.info("[%s/$%dk/%s] MC %d seeds …",
                         sym, eq // 1000, pname, args.seeds)
                mc[sym][eq][pname] = _mc(c, profile, eq, args.risk_pct, args.seeds)

    _render_capacity(capacity)
    _render_headline(headline)
    _render_mc(mc)
    recs = _recommend(capacity, headline, mc)
    console.print()
    console.print(Panel("\n".join(recs),
                        title="Per-account-size symbol recommendation",
                        border_style="magenta", title_align="left"))

    if args.out:
        Path(args.out).write_text(json.dumps({
            "capacity": capacity, "headline": headline, "mc": mc,
            "recommendation": recs,
        }, indent=2, default=str))
        console.print(f"\n[dim]Wrote {args.out}[/dim]")


# ---------------------------------------------------------------------------
def _render_capacity(capacity):
    tbl = Table(title="Capacity analysis (setup → risk-gate acceptance)",
                header_style="bold")
    for c in ("Symbol", "Account", "Setups", "Accepted", "Rejected (risk cap)",
              "Rejected (other)", "Accept %"):
        tbl.add_column(c, justify=("left" if c in ("Symbol", "Account") else "right"))
    last_sym = None
    for sym, accts in capacity.items():
        for eq, cap in accts.items():
            sym_show = sym if sym != last_sym else ""
            last_sym = sym
            col = "green" if cap["accept_pct"] >= 75 else "yellow" if cap["accept_pct"] >= 50 else "red"
            tbl.add_row(
                sym_show, f"${eq // 1000}k", str(cap["setups"]),
                str(cap["accepted"]), str(cap["rejected_risk_cap"]),
                str(cap["rejected_other"]),
                f"[{col}]{cap['accept_pct']:.0f}%[/{col}]",
            )
    console.print(tbl)


def _render_headline(headline):
    tbl = Table(title="Headline performance (seed 42)", header_style="bold")
    for c in ("Sym", "Acct", "Profile", "Closed", "Fill%", "Win%",
              "Exp R", "PF", "Max DD%", "Recov", "Avg slip", "Med slip"):
        tbl.add_column(c, justify=("left" if c in ("Sym", "Acct", "Profile") else "right"))
    last_sym = None; last_eq = None
    for sym, accts in headline.items():
        for eq, profs in accts.items():
            for pname, h in profs.items():
                sym_show = sym if sym != last_sym else ""
                eq_show = f"${eq // 1000}k" if eq != last_eq or sym != last_sym else ""
                last_sym = sym; last_eq = eq
                col = "green" if h["expectancy_R"] > 0.25 else "yellow" if h["expectancy_R"] > 0 else "red"
                pf = h["profit_factor"]; pf_s = "∞" if pf == math.inf else f"{pf:.2f}"
                rf = h["recovery"]; rf_s = "∞" if rf == math.inf else f"{rf:.2f}"
                tbl.add_row(
                    sym_show, eq_show, pname,
                    str(h["closed"]), f"{h['fill_pct']:.0f}%",
                    f"{h['win_pct']:.0f}%",
                    f"[{col}]{h['expectancy_R']:+.2f}R[/{col}]",
                    pf_s, f"{abs(h['max_dd_pct']):.2f}", rf_s,
                    f"{h['avg_slippage_pts']:.2f}",
                    f"{h['median_slippage_pts']:.2f}",
                )
    console.print(tbl)


def _render_mc(mc):
    tbl = Table(title="Monte Carlo over execution seeds", header_style="bold")
    for c in ("Sym", "Acct", "Profile", "Seeds", "Mean R", "Median R",
              "5%ile", "95%ile", "P(>0)", "P(>+0.25)", "P(DD>3R)",
              "Med closed"):
        tbl.add_column(c, justify=("left" if c in ("Sym", "Acct", "Profile") else "right"))
    last_sym = None; last_eq = None
    for sym, accts in mc.items():
        for eq, profs in accts.items():
            for pname, m in profs.items():
                sym_show = sym if sym != last_sym else ""
                eq_show = f"${eq // 1000}k" if eq != last_eq or sym != last_sym else ""
                last_sym = sym; last_eq = eq
                mean_col = "green" if m["mean_R"] > 0.25 else "yellow" if m["mean_R"] > 0 else "red"
                p5_col = "green" if m["p5_R"] > 0 else "red"
                tbl.add_row(
                    sym_show, eq_show, pname, str(m["n_seeds"]),
                    f"[{mean_col}]{m['mean_R']:+.2f}R[/{mean_col}]",
                    f"{m['median_R']:+.2f}R",
                    f"[{p5_col}]{m['p5_R']:+.2f}R[/{p5_col}]",
                    f"{m['p95_R']:+.2f}R",
                    f"{m['p_pos']:.0f}%",
                    f"{m['p_above_025']:.0f}%",
                    f"{m['p_dd_above_3R']:.0f}%",
                    f"{m['median_closed']:.0f}",
                )
    console.print(tbl)


# ---------------------------------------------------------------------------
def _classify(mc_normal):
    """One-cell verdict given NORMAL-profile MC stats."""
    if mc_normal["median_closed"] < 5:
        return "INSUFFICIENT_DATA"
    if mc_normal["p5_R"] < 0:
        return "NOT_PROVEN (5%ile < 0)"
    if mc_normal["median_closed"] < 20:
        return "MARGINAL_SAMPLE (<20 closed)"
    if mc_normal["mean_R"] > 0.25 and mc_normal["p_pos"] >= 70:
        return "PASSES"
    return "MARGINAL"


def _recommend(capacity, headline, mc):
    lines = []
    for eq in ACCOUNTS:
        # Build per-symbol NORMAL MC summary
        per_sym = {}
        for sym in SYMBOLS:
            cap = capacity[sym][eq]
            n_norm = mc[sym][eq]["NORMAL"]
            n_pun = mc[sym][eq]["PUNITIVE"]
            per_sym[sym] = {
                "accept_pct": cap["accept_pct"],
                "norm_mean": n_norm["mean_R"],
                "norm_p5": n_norm["p5_R"],
                "norm_pos": n_norm["p_pos"],
                "norm_med_closed": n_norm["median_closed"],
                "pun_mean": n_pun["mean_R"],
                "verdict": _classify(n_norm),
            }

        nq = per_sym["NQ"]; es = per_sym["ES"]
        # Recommendation rules
        if nq["verdict"].startswith("PASSES") and es["verdict"].startswith("PASSES"):
            choice = "C) ES + NQ"
            reason = ("Both symbols pass NORMAL: ES "
                      f"{es['norm_mean']:+.2f}R / NQ {nq['norm_mean']:+.2f}R, "
                      f"both 5%ile > 0, both ≥ 20 median closed.")
        elif es["verdict"].startswith("PASSES"):
            choice = "A) ES only"
            reason = (f"ES passes (mean {es['norm_mean']:+.2f}R, 5%ile "
                      f"{es['norm_p5']:+.2f}R, closed {es['norm_med_closed']:.0f}); "
                      f"NQ verdict {nq['verdict']}.")
        elif nq["verdict"].startswith("PASSES"):
            choice = "B) NQ only"
            reason = (f"NQ passes; ES verdict {es['verdict']}.")
        elif es["verdict"].startswith("MARGINAL") and not es["verdict"].startswith("MARGINAL_SAMPLE"):
            choice = "A) ES only (with caveat)"
            reason = (f"ES is the strongest borderline candidate "
                      f"(mean {es['norm_mean']:+.2f}R, 5%ile {es['norm_p5']:+.2f}R); "
                      f"NQ is {nq['verdict']}.")
        elif "MARGINAL_SAMPLE" in es["verdict"] or "MARGINAL_SAMPLE" in nq["verdict"]:
            choice = "A) ES only (paper-only, no real money)"
            reason = (f"Both symbols are sample-thin (ES closed "
                      f"{es['norm_med_closed']:.0f} / NQ {nq['norm_med_closed']:.0f}); "
                      f"ES has stronger mean ({es['norm_mean']:+.2f}R vs "
                      f"NQ {nq['norm_mean']:+.2f}R) — continue paper-only on ES.")
        else:
            choice = "D) Neither"
            reason = (f"Neither symbol qualifies. ES: {es['verdict']} "
                      f"(mean {es['norm_mean']:+.2f}R). NQ: {nq['verdict']} "
                      f"(mean {nq['norm_mean']:+.2f}R).")

        lines.append(
            f"${eq // 1000}k → {choice}\n"
            f"   ES: {es['verdict']:<30s} | "
            f"NORMAL mean {es['norm_mean']:+.2f}R, 5%ile {es['norm_p5']:+.2f}R, "
            f"closed {es['norm_med_closed']:.0f}, accept {es['accept_pct']:.0f}%\n"
            f"   NQ: {nq['verdict']:<30s} | "
            f"NORMAL mean {nq['norm_mean']:+.2f}R, 5%ile {nq['norm_p5']:+.2f}R, "
            f"closed {nq['norm_med_closed']:.0f}, accept {nq['accept_pct']:.0f}%\n"
            f"   reason: {reason}"
        )
    return lines


if __name__ == "__main__":
    main()
