"""2-way interaction fragility — does ES robustness survive when two
assumptions move together?

The single-knob fragility test found max swing 0.07R on ES — too
robust to credit at face value. This test perturbs **pairs** of knobs
simultaneously, in both "both worse" and "one worse / one better"
directions, to see if the small swings combine multiplicatively or
cancel.

Pairs tested (chosen by leverage from the single-knob test + brief):

1. fill_p_med_vol × stop_slip_atr_frac_med
2. fill_p_med_vol × partial_fill_prob
3. fill_p_high_vol × fill_p_med_vol
4. stop_slip_atr_frac_med × news_blackout_slip_mult
5. stop_slip_atr_frac_med × overnight_slip_mult (session)
6. partial_fill_prob × partial_fill_qty_pct

For each (symbol, pair, direction) report mean expectancy, win rate,
median DD over N seeds.
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.execution_model import NORMAL, ExecutionProfile
from backtest.simulator import simulate
from backtest.tier1_montecarlo import SymbolData, _prepare

console = Console()
log = logging.getLogger("interaction_fragility")


@dataclass
class PairSpec:
    name: str
    knob_a: tuple             # (attr, worse, better)
    knob_b: tuple


PAIRS: list[PairSpec] = [
    PairSpec("fill_p_med × stop_slip_atr",
             ("limit_fill_prob_med_vol", 0.35, 0.85),
             ("stop_slip_atr_frac_med", 0.30, 0.075)),
    PairSpec("fill_p_med × partial_rate",
             ("limit_fill_prob_med_vol", 0.35, 0.85),
             ("partial_fill_prob", 0.40, 0.05)),
    PairSpec("fill_p_high × fill_p_med",
             ("limit_fill_prob_high_vol", 0.15, 0.60),
             ("limit_fill_prob_med_vol", 0.35, 0.85)),
    PairSpec("stop_slip × news_blackout_mult",
             ("stop_slip_atr_frac_med", 0.30, 0.075),
             ("stop_slip_blackout_mult", 5.0, 1.5)),
    PairSpec("partial_rate × partial_qty",
             ("partial_fill_prob", 0.40, 0.05),
             ("partial_fill_qty_pct", 0.30, 0.75)),
]


def _perturb_two(base: ExecutionProfile, a: tuple, b: tuple) -> ExecutionProfile:
    out = copy.deepcopy(base)
    setattr(out, a[0], a[1])
    setattr(out, b[0], b[1])
    return out


def _mc(sd: SymbolData, profile, n_seeds, equity, risk_pct) -> dict:
    exps, dds, wins, closed = [], [], [], []
    for s in range(n_seeds):
        sim = simulate(df=sd.df, setups=sd.setups,
                       starting_equity=equity, instrument_symbol=sd.sim_symbol,
                       risk_pct=risk_pct, min_rr=1.0,
                       execution_profile=profile, random_seed=s)
        st = sim.stats
        exps.append(st["expectancy_R"])
        dds.append(abs(st["max_drawdown_pct"]))
        wins.append(st["win_rate_pct"])
        closed.append(st["n_filled"])
    return {
        "mean": float(np.mean(exps)),
        "median": float(np.median(exps)),
        "p5": float(np.percentile(exps, 5)),
        "win_rate": float(np.mean(wins)),
        "median_dd_pct": float(np.median(dds)),
        "median_closed": float(np.median(closed)),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="ES,CL")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--equity", type=float, default=50_000)
    parser.add_argument("--risk-pct", type=float, default=0.005)
    parser.add_argument("--source", default="yfinance")
    parser.add_argument("--seeds", type=int, default=50)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    sd = _prepare(syms, args.timeframe, args.days, args.source)
    if not sd:
        console.print("[red]No data.[/red]"); sys.exit(1)

    results = {}
    for sym, data in sd.items():
        log.info("[%s] baseline …", sym)
        base = _mc(data, NORMAL, args.seeds, args.equity, args.risk_pct)
        sym_rows = []
        for p in PAIRS:
            log.info("[%s] PAIR %s — both worse", sym, p.name)
            # both worse: a.worse + b.worse
            both_worse_prof = _perturb_two(NORMAL,
                (p.knob_a[0], p.knob_a[1]),
                (p.knob_b[0], p.knob_b[1]))
            r_ww = _mc(data, both_worse_prof, args.seeds, args.equity, args.risk_pct)

            log.info("[%s] PAIR %s — both better", sym, p.name)
            both_better_prof = _perturb_two(NORMAL,
                (p.knob_a[0], p.knob_a[2]),
                (p.knob_b[0], p.knob_b[2]))
            r_bb = _mc(data, both_better_prof, args.seeds, args.equity, args.risk_pct)

            log.info("[%s] PAIR %s — a worse / b better", sym, p.name)
            r_wb = _mc(data, _perturb_two(NORMAL,
                (p.knob_a[0], p.knob_a[1]),
                (p.knob_b[0], p.knob_b[2])),
                args.seeds, args.equity, args.risk_pct)

            log.info("[%s] PAIR %s — a better / b worse", sym, p.name)
            r_bw = _mc(data, _perturb_two(NORMAL,
                (p.knob_a[0], p.knob_a[2]),
                (p.knob_b[0], p.knob_b[1])),
                args.seeds, args.equity, args.risk_pct)

            sym_rows.append({
                "pair": p.name, "knob_a": p.knob_a[0], "knob_b": p.knob_b[0],
                "baseline_R": base["mean"],
                "both_worse_R": r_ww["mean"],
                "both_better_R": r_bb["mean"],
                "a_worse_b_better_R": r_wb["mean"],
                "a_better_b_worse_R": r_bw["mean"],
                # The interaction effect: does (both worse) drop further than
                # the sum of single-knob drops would predict?
                "both_worse_dd": base["mean"] - r_ww["mean"],
                "both_worse_closed": r_ww["median_closed"],
                "both_worse_winrate": r_ww["win_rate"],
            })
        results[sym] = {"baseline": base, "pairs": sym_rows}

    # Render
    for sym, res in results.items():
        base = res["baseline"]
        tbl = Table(title=f"{sym} — 2-way fragility around NORMAL "
                          f"(baseline mean {base['mean']:+.2f}R, "
                          f"closed≈{base['median_closed']:.0f})",
                    header_style="bold")
        for col in ("Pair", "Both worse R", "Both better R",
                    "A worse / B better", "A better / B worse",
                    "Δ from baseline (both worse)", "Closed", "Win %"):
            tbl.add_column(col, justify=("left" if col == "Pair" else "right"))
        for r in sorted(res["pairs"], key=lambda x: -x["both_worse_dd"]):
            dd = r["both_worse_dd"]
            dd_color = "red" if dd > 0.20 else "yellow" if dd > 0.10 else "green"
            ww_color = "red" if r["both_worse_R"] < 0 else "yellow" if r["both_worse_R"] < 0.25 else "green"
            tbl.add_row(
                r["pair"],
                f"[{ww_color}]{r['both_worse_R']:+.2f}R[/{ww_color}]",
                f"{r['both_better_R']:+.2f}R",
                f"{r['a_worse_b_better_R']:+.2f}R",
                f"{r['a_better_b_worse_R']:+.2f}R",
                f"[{dd_color}]{dd:+.2f}R[/{dd_color}]",
                f"{r['both_worse_closed']:.0f}",
                f"{r['both_worse_winrate']:.0f}%",
            )
        console.print(tbl)

    # Bottom line per symbol
    lines = []
    for sym, res in results.items():
        worst_pair = max(res["pairs"], key=lambda r: r["both_worse_dd"])
        worst_R = worst_pair["both_worse_R"]
        any_neg = any(r["both_worse_R"] < 0 for r in res["pairs"])
        survival = ("FRAGILE — at least one pair flips expectancy negative"
                    if any_neg else
                    "ROBUST — no pair flips expectancy negative")
        lines.append(
            f"{sym}: worst pair = '{worst_pair['pair']}' → {worst_R:+.2f}R "
            f"(drop {worst_pair['both_worse_dd']:.2f}R). {survival}."
        )
    console.print(Panel("\n".join(lines),
                        title="2-way fragility — does ES robustness survive?",
                        border_style="magenta", title_align="left"))

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, default=str))
        console.print(f"\n[dim]Wrote {args.out}[/dim]")


if __name__ == "__main__":
    main()
