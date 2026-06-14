"""Knob-by-knob fragility — which single execution assumption is keeping
the strategy alive?

For each of 7 execution knobs we move the NORMAL profile +/-50% (or to a
clearly-pessimistic value) and re-run a small Monte Carlo per (symbol,
knob, direction). We report the delta in NORMAL-profile mean expectancy
vs the unperturbed baseline, ranked by absolute impact.

Symbols: ES and CL only — NQ produces too few closed trades for the
delta to be meaningful per-knob.

Runtime: ~3-5 min at 50 seeds × 2 symbols × 7 knobs × 2 directions.
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
from backtest.tier1_montecarlo import MICRO_MAP, SymbolData, _prepare

console = Console()
log = logging.getLogger("knob_fragility")


# ---------------------------------------------------------------------------
@dataclass
class KnobSpec:
    name: str               # human label
    attr: str               # ExecutionProfile attribute
    worse: float            # value that makes execution HARDER
    better: float           # value that makes execution EASIER
    direction: str          # text — "↑ = harsher fills" etc, for the table


KNOBS: list[KnobSpec] = [
    KnobSpec("stop slip ATR frac (med vol)",
             "stop_slip_atr_frac_med", worse=0.30, better=0.075,
             direction="higher = wider stops"),
    KnobSpec("blackout slip mult",
             "stop_slip_blackout_mult", worse=5.0, better=1.5,
             direction="higher = worse stops in news"),
    KnobSpec("limit fill p — med vol",
             "limit_fill_prob_med_vol", worse=0.35, better=0.85,
             direction="lower = fewer entries"),
    KnobSpec("limit fill p — high vol",
             "limit_fill_prob_high_vol", worse=0.15, better=0.60,
             direction="lower = fewer entries"),
    KnobSpec("limit fill p — elevated news",
             "limit_fill_prob_elevated", worse=0.15, better=0.60,
             direction="lower = fewer entries near news"),
    KnobSpec("partial fill rate",
             "partial_fill_prob", worse=0.40, better=0.05,
             direction="higher = more partials"),
    KnobSpec("partial qty pct",
             "partial_fill_qty_pct", worse=0.30, better=0.75,
             direction="lower = smaller partial size"),
]


# ---------------------------------------------------------------------------
def _mc(sd: SymbolData, profile: ExecutionProfile, n_seeds: int,
        equity: float, risk_pct: float) -> dict:
    exps = []
    fills = []
    closed = []
    for s in range(n_seeds):
        sim = simulate(
            df=sd.df, setups=sd.setups,
            starting_equity=equity,
            instrument_symbol=sd.sim_symbol,
            risk_pct=risk_pct, min_rr=1.0,
            execution_profile=profile,
            random_seed=s,
        )
        st = sim.stats
        exps.append(st["expectancy_R"])
        fills.append(st["limit_fill_rate_pct"])
        closed.append(st["n_filled"])
    return {
        "mean": float(np.mean(exps)),
        "median": float(np.median(exps)),
        "p5": float(np.percentile(exps, 5)),
        "p95": float(np.percentile(exps, 95)),
        "median_fill": float(np.median(fills)),
        "median_closed": float(np.median(closed)),
    }


def _perturb(base: ExecutionProfile, attr: str, value) -> ExecutionProfile:
    out = copy.deepcopy(base)
    setattr(out, attr, value)
    out.name = f"NORMAL_{attr}={value}"
    return out


# ---------------------------------------------------------------------------
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
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    sd = _prepare(syms, args.timeframe, args.days, args.source)
    if not sd:
        console.print("[red]No data.[/red]"); sys.exit(1)

    results: dict[str, dict] = {}

    for sym, data in sd.items():
        log.info("[%s] baseline NORMAL …", sym)
        baseline = _mc(data, NORMAL, args.seeds, args.equity, args.risk_pct)
        rows = []
        for k in KNOBS:
            log.info("[%s] knob '%s' worse=%s …", sym, k.name, k.worse)
            worse_prof = _perturb(NORMAL, k.attr, k.worse)
            r_worse = _mc(data, worse_prof, args.seeds, args.equity, args.risk_pct)
            log.info("[%s] knob '%s' better=%s …", sym, k.name, k.better)
            better_prof = _perturb(NORMAL, k.attr, k.better)
            r_better = _mc(data, better_prof, args.seeds, args.equity, args.risk_pct)

            rows.append({
                "knob": k.name,
                "attr": k.attr,
                "direction": k.direction,
                "baseline_R": baseline["mean"],
                "worse_R": r_worse["mean"],
                "better_R": r_better["mean"],
                "delta_worse": r_worse["mean"] - baseline["mean"],
                "delta_better": r_better["mean"] - baseline["mean"],
                "swing": r_better["mean"] - r_worse["mean"],
                "worse_closed": r_worse["median_closed"],
                "better_closed": r_better["median_closed"],
            })
        results[sym] = {"baseline": baseline, "knobs": rows}

    # ---- render per symbol ----
    for sym, res in results.items():
        rows = sorted(res["knobs"], key=lambda r: -abs(r["swing"]))
        baseline = res["baseline"]
        tbl = Table(
            title=f"{sym} — knob fragility around NORMAL (baseline mean "
                  f"{baseline['mean']:+.2f}R, "
                  f"closed≈{baseline['median_closed']:.0f})",
            header_style="bold",
        )
        for col in ("Rank", "Knob", "Baseline", "Worse val→R", "Better val→R",
                    "Δ worse", "Δ better", "Swing"):
            tbl.add_column(col, justify=("left" if col in ("Knob",) else "right"))
        for i, r in enumerate(rows, 1):
            w_color = "red" if r["delta_worse"] < -0.1 else "yellow" if r["delta_worse"] < 0 else "green"
            b_color = "green" if r["delta_better"] > 0.1 else "yellow"
            tbl.add_row(
                str(i), r["knob"],
                f"{baseline['mean']:+.2f}R",
                f"{r['worse_R']:+.2f}R",
                f"{r['better_R']:+.2f}R",
                f"[{w_color}]{r['delta_worse']:+.2f}R[/{w_color}]",
                f"[{b_color}]{r['delta_better']:+.2f}R[/{b_color}]",
                f"{r['swing']:.2f}R",
            )
        console.print(tbl)

    # ---- which knob is keeping the strategy alive? ----
    summary_lines = []
    for sym, res in results.items():
        rows = sorted(res["knobs"], key=lambda r: -abs(r["swing"]))
        top = rows[0]
        baseline_R = res["baseline"]["mean"]
        # The "load-bearing" knob is the one whose worse-direction value
        # crosses expectancy through 0.
        load_bearing = None
        for r in rows:
            if baseline_R > 0 and r["worse_R"] <= 0:
                load_bearing = r
                break
        summary_lines.append(
            f"{sym}: most sensitive = '{top['knob']}' (swing {top['swing']:+.2f}R). "
            + (f"Load-bearing: '{load_bearing['knob']}' "
               f"(worse-value flips expectancy to {load_bearing['worse_R']:+.2f}R)."
               if load_bearing else
               "No single knob alone flips expectancy negative.")
        )

    console.print()
    console.print(Panel("\n".join(summary_lines),
                        title="Knob fragility — what's keeping it alive?",
                        border_style="magenta", title_align="left"))

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, default=str))
        console.print(f"\n[dim]Wrote {args.out}[/dim]")


if __name__ == "__main__":
    main()
