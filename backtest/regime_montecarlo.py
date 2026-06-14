"""Regime Monte Carlo — vary the price history, not the execution seed.

Four methods, each preserving local microstructure:

1. **Rolling window** — slide a fixed-length window across the full
   history. Reveals which sub-periods the strategy actually worked in.
2. **Block bootstrap** — sample contiguous blocks of B bars with
   replacement to construct synthetic histories. Preserves
   intra-block structure; breaks inter-block dependence.
3. **Quarter-by-quarter** — split history into calendar quarters and
   run each separately. Direct regime breakdown.
4. **Vol-regime stratified bootstrap** — same as (2) but blocks
   sampled only from a chosen vol percentile band (low / medium /
   high). Asks "would the edge survive a year of one regime?"

For each (symbol, method) report:
mean / median / 5th / 95th expectancy, P(>0R), P(>+0.25R),
P(maxDD > 3R).

**Caveats** baked into the design and the report:

- Block bootstrap fragments long-range structure. Sweep levels
  (PDH/PWH) computed on a stitched series may differ from the
  original. This biases the test toward fewer / lower-quality
  setups, which is consistent with the adversarial posture — if the
  edge survives a corrupted long-range structure, the edge is real.
- Rolling windows of < 60d on 1h data produce ~3-15 closed trades.
  Wide CIs are expected.
- Vol-regime sampling defines vol on the original index, then maps
  blocks; "high-vol-only" universes are extrapolations, not actual
  observed years.
"""
from __future__ import annotations

import argparse
import json
import logging
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

from backtest.execution_model import NORMAL
from backtest.simulator import simulate
from backtest.tier1_montecarlo import MICRO_MAP
from data.loader import load_bars
from signals.htf_bias import compute_bias_series, htf_timeframe_for
from signals.setup import find_setups

console = Console()
log = logging.getLogger("regime_mc")

DEFAULT_SYMBOLS = ("NQ", "ES", "CL")


# ---------------------------------------------------------------------------
@dataclass
class SymCache:
    symbol: str
    sim_symbol: str
    df: pd.DataFrame
    df_htf: pd.DataFrame


def _load(symbols, timeframe, days, source) -> dict[str, SymCache]:
    out = {}
    for sym in symbols:
        df = load_bars(sym, timeframe, days=days, source=source)
        if df.empty:
            log.error("[%s] no data", sym); continue
        df_htf = load_bars(sym, htf_timeframe_for(timeframe), days=days, source=source)
        out[sym] = SymCache(symbol=sym, sim_symbol=MICRO_MAP.get(sym, sym),
                            df=df, df_htf=df_htf)
        log.info("[%s] bars=%d htf_bars=%d", sym, len(df), len(df_htf))
    return out


# ---------------------------------------------------------------------------
def _run(df: pd.DataFrame, df_htf: pd.DataFrame, sim_symbol: str,
         equity: float, risk_pct: float, seed: int) -> dict:
    """Run a single simulator pass on the given df. Returns the metric row."""
    if len(df) < 100:
        return None
    htf_bias = compute_bias_series(df, df_htf) if df_htf is not None and not df_htf.empty else None
    setups = find_setups(df, htf_bias_series=htf_bias)
    sim = simulate(
        df=df, setups=setups,
        starting_equity=equity, instrument_symbol=sim_symbol,
        risk_pct=risk_pct, min_rr=1.0,
        execution_profile=NORMAL, random_seed=seed,
    )
    return {
        "expectancy_R": sim.stats["expectancy_R"],
        "n_closed": sim.stats["n_filled"],
        "max_dd_pct": sim.stats["max_drawdown_pct"],
        "n_setups": len(setups),
    }


def _summarise(rows: list[dict], equity: float, risk_pct: float) -> dict:
    rows = [r for r in rows if r is not None]
    if not rows:
        return {"n": 0}
    exps = np.array([r["expectancy_R"] for r in rows])
    # Convert max_dd_pct to "drawdown in R-units": dd_dollars / R-risk
    R_usd = max(1.0, equity * risk_pct)
    dd_R = np.array([abs(r["max_dd_pct"]) / 100 * equity / R_usd for r in rows])
    closed = np.array([r["n_closed"] for r in rows])
    return {
        "n_runs": len(rows),
        "mean": float(np.mean(exps)),
        "median": float(np.median(exps)),
        "std": float(np.std(exps)),
        "p5": float(np.percentile(exps, 5)),
        "p95": float(np.percentile(exps, 95)),
        "p_pos": float(np.mean(exps > 0) * 100),
        "p_above_025": float(np.mean(exps > 0.25) * 100),
        "p_dd_above_3R": float(np.mean(dd_R > 3.0) * 100),
        "median_closed": float(np.median(closed)),
        "median_setups": float(np.median([r["n_setups"] for r in rows])),
    }


# ---------------------------------------------------------------------------
# Method 1: ROLLING WINDOW
def method_rolling(sc: SymCache, equity, risk_pct, window_days, stride_days, seed=42):
    bars_per_day = 24
    W = window_days * bars_per_day
    S = stride_days * bars_per_day
    n = len(sc.df)
    rows = []
    for start in range(0, n - W, S):
        end = start + W
        sub_df = sc.df.iloc[start:end].copy()
        # Clip HTF to overlapping date range
        sub_htf = sc.df_htf.loc[(sc.df_htf.index >= sub_df.index[0])
                                & (sc.df_htf.index <= sub_df.index[-1])]
        r = _run(sub_df, sub_htf, sc.sim_symbol, equity, risk_pct, seed)
        if r is not None:
            rows.append(r)
    return rows


# Method 2: BLOCK BOOTSTRAP
def method_block_bootstrap(sc: SymCache, equity, risk_pct,
                           block_days, n_resamples, target_days=180, seed_base=1000):
    bars_per_day = 24
    B = block_days * bars_per_day
    target_bars = target_days * bars_per_day
    n_blocks = target_bars // B
    n = len(sc.df)
    rng_global = np.random.default_rng(seed_base)
    rows = []
    for k in range(n_resamples):
        starts = rng_global.integers(0, n - B, size=n_blocks)
        chunks = [sc.df.iloc[s:s + B] for s in starts]
        boot = pd.concat(chunks, axis=0)
        # Re-index with a synthetic monotonic timeline (anchor at first
        # bar of the first chunk). This avoids duplicate timestamps that
        # would break the simulator's sorted iteration.
        synthetic_index = pd.date_range(
            start=boot.index[0], periods=len(boot),
            freq=pd.infer_freq(sc.df.index[:5]) or "1h", tz=boot.index.tz,
        )
        boot.index = synthetic_index
        # HTF for the synthetic series — recompute from the bootstrapped 1h
        # by resampling. Imperfect but consistent.
        htf_freq = "4h"
        try:
            boot_htf = boot.resample(htf_freq).agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum",
            }).dropna()
        except Exception:
            boot_htf = pd.DataFrame()
        r = _run(boot, boot_htf, sc.sim_symbol, equity, risk_pct, seed_base + k)
        if r is not None:
            rows.append(r)
    return rows


# Method 3: QUARTER-BY-QUARTER
def method_quarters(sc: SymCache, equity, risk_pct, seed=42):
    rows = []
    breakdown = []
    df = sc.df
    quarters = df.index.to_series().dt.to_period("Q").unique()
    for q in quarters:
        mask = df.index.to_series().dt.to_period("Q") == q
        sub_df = df[mask.values]
        if len(sub_df) < 100:
            continue
        sub_htf = sc.df_htf.loc[(sc.df_htf.index >= sub_df.index[0])
                                & (sc.df_htf.index <= sub_df.index[-1])]
        r = _run(sub_df, sub_htf, sc.sim_symbol, equity, risk_pct, seed)
        if r is not None:
            rows.append(r)
            breakdown.append({"quarter": str(q), **r})
    return rows, breakdown


# Method 4: VOL-REGIME STRATIFIED BOOTSTRAP
def method_vol_strat(sc: SymCache, equity, risk_pct,
                     block_days, n_resamples, vol_regime, target_days=180, seed_base=2000):
    """Sample blocks from a chosen vol band only."""
    bars_per_day = 24
    B = block_days * bars_per_day
    df = sc.df

    # Per-block realized vol percentile (use std of returns)
    log_ret = np.log(df["close"] / df["close"].shift(1)).fillna(0)
    block_vol = log_ret.rolling(B).std()
    # Pool of allowed starting indices by vol bucket
    q_low, q_high = block_vol.quantile(0.33), block_vol.quantile(0.66)
    if vol_regime == "low":
        candidate = np.where((block_vol <= q_low).values)[0]
    elif vol_regime == "high":
        candidate = np.where((block_vol >= q_high).values)[0]
    else:
        candidate = np.where(((block_vol > q_low) & (block_vol < q_high)).values)[0]
    # Constrain candidate starts so block fits
    candidate = candidate[(candidate >= B) & (candidate < len(df) - B)]
    if len(candidate) < 5:
        return []
    # Shift to be block-start indices
    starts_pool = candidate - B

    target_bars = target_days * bars_per_day
    n_blocks = target_bars // B
    rng = np.random.default_rng(seed_base)
    rows = []
    for k in range(n_resamples):
        starts = rng.choice(starts_pool, size=n_blocks, replace=True)
        chunks = [df.iloc[s:s + B] for s in starts]
        boot = pd.concat(chunks, axis=0)
        boot.index = pd.date_range(
            start=boot.index[0], periods=len(boot),
            freq=pd.infer_freq(df.index[:5]) or "1h", tz=boot.index.tz,
        )
        try:
            boot_htf = boot.resample("4h").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum",
            }).dropna()
        except Exception:
            boot_htf = pd.DataFrame()
        r = _run(boot, boot_htf, sc.sim_symbol, equity, risk_pct, seed_base + k)
        if r is not None:
            rows.append(r)
    return rows


# ---------------------------------------------------------------------------
def render(results: dict):
    tbl = Table(title="Regime Monte Carlo — NORMAL profile",
                header_style="bold")
    for col in ("Symbol", "Method", "Runs", "Mean R", "Median R", "5%ile",
                "95%ile", "P(>0)", "P(>+0.25)", "P(DD>3R)", "Med closed"):
        tbl.add_column(col, justify=("left" if col in ("Symbol", "Method") else "right"))
    last_sym = None
    for sym, methods in results.items():
        for method, s in methods.items():
            if s.get("n_runs", 0) == 0:
                continue
            sym_show = sym if sym != last_sym else ""
            last_sym = sym
            mean_color = "green" if s["mean"] > 0 else "red"
            p5_color = "green" if s["p5"] > 0 else "red"
            tbl.add_row(
                sym_show, method, str(s["n_runs"]),
                f"[{mean_color}]{s['mean']:+.2f}R[/{mean_color}]",
                f"{s['median']:+.2f}R",
                f"[{p5_color}]{s['p5']:+.2f}R[/{p5_color}]",
                f"{s['p95']:+.2f}R",
                f"{s['p_pos']:.0f}%",
                f"{s['p_above_025']:.0f}%",
                f"{s['p_dd_above_3R']:.0f}%",
                f"{s['median_closed']:.0f}",
            )
    console.print(tbl)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--equity", type=float, default=50_000)
    parser.add_argument("--risk-pct", type=float, default=0.005)
    parser.add_argument("--source", default="yfinance")
    parser.add_argument("--rolling-window", type=int, default=60)
    parser.add_argument("--rolling-stride", type=int, default=10)
    parser.add_argument("--block-days", type=int, default=15)
    parser.add_argument("--block-resamples", type=int, default=60)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    sc = _load(syms, args.timeframe, args.days, args.source)
    if not sc:
        console.print("[red]No data.[/red]"); sys.exit(1)

    results = {}
    quarter_breakdowns = {}
    for sym, data in sc.items():
        log.info("[%s] METHOD 1 rolling window …", sym)
        roll = method_rolling(data, args.equity, args.risk_pct,
                              args.rolling_window, args.rolling_stride)
        log.info("[%s] METHOD 2 block bootstrap (B=%dd, N=%d) …",
                 sym, args.block_days, args.block_resamples)
        boot = method_block_bootstrap(data, args.equity, args.risk_pct,
                                      args.block_days, args.block_resamples,
                                      target_days=args.days)
        log.info("[%s] METHOD 3 quarter by quarter …", sym)
        qrows, qbreakdown = method_quarters(data, args.equity, args.risk_pct)
        log.info("[%s] METHOD 4 vol-regime LOW …", sym)
        v_low = method_vol_strat(data, args.equity, args.risk_pct,
                                 args.block_days, args.block_resamples, "low",
                                 target_days=args.days)
        log.info("[%s] METHOD 4 vol-regime HIGH …", sym)
        v_high = method_vol_strat(data, args.equity, args.risk_pct,
                                  args.block_days, args.block_resamples, "high",
                                  target_days=args.days)

        results[sym] = {
            "rolling": _summarise(roll, args.equity, args.risk_pct),
            "block_bootstrap": _summarise(boot, args.equity, args.risk_pct),
            "quarters": _summarise(qrows, args.equity, args.risk_pct),
            "vol_low": _summarise(v_low, args.equity, args.risk_pct),
            "vol_high": _summarise(v_high, args.equity, args.risk_pct),
        }
        quarter_breakdowns[sym] = qbreakdown

    render(results)

    # Per-quarter detail panel
    qlines = []
    for sym, qb in quarter_breakdowns.items():
        if not qb:
            qlines.append(f"{sym}: no quarters with ≥100 bars")
            continue
        items = ", ".join(f"{q['quarter']}: {q['expectancy_R']:+.2f}R "
                          f"(closed={q['n_closed']})" for q in qb)
        qlines.append(f"{sym}: {items}")
    console.print(Panel("\n".join(qlines),
                        title="Quarter-by-quarter expectancy breakdown",
                        border_style="cyan", title_align="left"))

    # Verdict — regime survival is binary per symbol
    verdict_lines = []
    for sym, methods in results.items():
        survivors = []
        for m, s in methods.items():
            if s.get("n_runs", 0) == 0:
                continue
            if s["p5"] > 0:
                survivors.append(m)
        all_methods = [m for m, s in methods.items() if s.get("n_runs", 0)]
        verdict_lines.append(
            f"{sym}: passes 5%ile>0 on {len(survivors)}/{len(all_methods)} methods "
            f"({', '.join(survivors) if survivors else 'none'})"
        )
    console.print(Panel("\n".join(verdict_lines),
                        title="Regime survival — methods with 5%ile > 0",
                        border_style="magenta", title_align="left"))

    if args.out:
        Path(args.out).write_text(json.dumps({
            "summary": results, "quarters": quarter_breakdowns,
        }, indent=2, default=str))
        console.print(f"\n[dim]Wrote {args.out}[/dim]")


if __name__ == "__main__":
    main()
