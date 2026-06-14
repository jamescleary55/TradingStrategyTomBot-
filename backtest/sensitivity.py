"""Execution-realism sensitivity analysis.

Re-runs the canonical backtests under three profiles
(``OPTIMISTIC`` / ``NORMAL`` / ``PUNITIVE``) and surfaces the gap.

The question being answered is binary:

    "Does the strategy still have positive expectancy under PUNITIVE
    execution assumptions?"

If yes — we have evidence the edge is real.
If no  — the edge was an artifact of optimistic fill modeling.

Usage::

    python -m backtest.sensitivity --symbol NQ --days 180
    python -m backtest.sensitivity --symbols NQ,ES,CL --days 730
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.execution_model import OPTIMISTIC, NORMAL, PUNITIVE, ExecutionProfile, PROFILES
from backtest.simulator import simulate
from data.loader import load_bars
from signals.htf_bias import compute_bias_series, htf_timeframe_for
from signals.setup import find_setups

console = Console()
log = logging.getLogger("sensitivity")


# ---------------------------------------------------------------------------
def _profit_factor(trades) -> float:
    wins = sum(t.r_multiple for t in trades if t.outcome == "target")
    losses = abs(sum(t.r_multiple for t in trades if t.outcome == "stop"))
    if losses == 0:
        return math.inf if wins > 0 else 0.0
    return wins / losses


def _recovery_factor(stats: dict) -> float:
    dd = abs(stats.get("max_drawdown_pct", 0) or 0)
    ret = stats.get("return_pct", 0) or 0
    if dd == 0:
        return math.inf if ret > 0 else 0.0
    return ret / dd


def _run_profile(df, setups, profile: ExecutionProfile, *,
                 equity: float, instrument: str, risk_pct: float) -> dict:
    sim = simulate(
        df=df, setups=setups,
        starting_equity=equity,
        instrument_symbol=instrument,
        risk_pct=risk_pct, min_rr=1.0,
        execution_profile=profile,
    )
    closed = [t for t in sim.trades if t.outcome in ("target", "stop")]
    return {
        "profile": profile.name,
        "n_setups": len(setups),
        "n_closed": len(closed),
        "wins": sim.stats["n_wins"],
        "losses": sim.stats["n_losses"],
        "win_rate_pct": sim.stats["win_rate_pct"],
        "avg_R": sim.stats["avg_R"],
        "expectancy_R": sim.stats["expectancy_R"],
        "profit_factor": _profit_factor(closed),
        "max_drawdown_pct": sim.stats["max_drawdown_pct"],
        "recovery_factor": _recovery_factor(sim.stats),
        "limit_fill_rate_pct": sim.stats["limit_fill_rate_pct"],
        "avg_slippage_pts": sim.stats["avg_slippage_pts"],
        "total_pnl_usd": sim.stats["total_pnl_usd"],
        "return_pct": sim.stats["return_pct"],
    }


def _run_symbol(symbol: str, sim_symbol: str, timeframe: str, days: int,
                source: str, equity: float, risk_pct: float) -> list[dict]:
    df = load_bars(symbol, timeframe, days=days, source=source)
    if df.empty:
        log.warning("No bars for %s; skip", symbol)
        return []
    htf_tf = htf_timeframe_for(timeframe)
    df_htf = load_bars(symbol, htf_tf, days=days, source=source)
    htf_bias = compute_bias_series(df, df_htf) if not df_htf.empty else None
    setups = find_setups(df, htf_bias_series=htf_bias)
    log.info("[%s] %d bars, %d setups", symbol, len(df), len(setups))

    rows: list[dict] = []
    for prof in (OPTIMISTIC, NORMAL, PUNITIVE):
        row = _run_profile(df, setups, prof,
                           equity=equity, instrument=sim_symbol, risk_pct=risk_pct)
        row["symbol"] = symbol
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
def _render_table(rows: Iterable[dict], title: str) -> Table:
    tbl = Table(title=title, header_style="bold")
    tbl.add_column("Symbol"); tbl.add_column("Profile")
    tbl.add_column("Closed", justify="right")
    tbl.add_column("Win %", justify="right")
    tbl.add_column("Avg R", justify="right")
    tbl.add_column("Profit factor", justify="right")
    tbl.add_column("Limit fill %", justify="right")
    tbl.add_column("Avg slip", justify="right")
    tbl.add_column("Max DD %", justify="right")
    tbl.add_column("Recovery", justify="right")
    last_sym = None
    for r in rows:
        sym_show = r["symbol"] if r["symbol"] != last_sym else ""
        last_sym = r["symbol"]
        r_color = "green" if r["avg_R"] > 0 else "red"
        pf = r["profit_factor"]
        pf_str = "∞" if pf == math.inf else f"{pf:.2f}"
        rc = r["recovery_factor"]
        rc_str = "∞" if rc == math.inf else f"{rc:.2f}"
        tbl.add_row(
            sym_show, r["profile"],
            str(r["n_closed"]),
            f"{r['win_rate_pct']:.0f}%" if r["n_closed"] else "—",
            f"[{r_color}]{r['avg_R']:+.2f}R[/{r_color}]",
            pf_str,
            f"{r['limit_fill_rate_pct']:.0f}%",
            f"{r['avg_slippage_pts']:.2f}",
            f"{abs(r['max_drawdown_pct']):.2f}",
            rc_str,
        )
    return tbl


def _survival_verdict(rows: list[dict]) -> str:
    by_prof: dict[str, list[dict]] = {}
    for r in rows:
        by_prof.setdefault(r["profile"], []).append(r)
    out = []
    for name in ("OPTIMISTIC", "NORMAL", "PUNITIVE"):
        rs = by_prof.get(name, [])
        if not rs:
            continue
        # Equal-weighted avg expectancy across symbols (only count cells with n≥5)
        sig = [r for r in rs if r["n_closed"] >= 5]
        if not sig:
            verdict = "INSUFFICIENT (no symbol with ≥ 5 closed trades)"
            avg_exp = sum(r["avg_R"] for r in rs) / len(rs) if rs else 0.0
        else:
            avg_exp = sum(r["avg_R"] for r in sig) / len(sig)
            if avg_exp > 0.3:
                verdict = f"SURVIVES (avg expectancy {avg_exp:+.2f}R across {len(sig)} symbols)"
            elif avg_exp > 0:
                verdict = f"MARGINAL (avg expectancy {avg_exp:+.2f}R across {len(sig)} symbols)"
            else:
                verdict = f"COLLAPSES (avg expectancy {avg_exp:+.2f}R across {len(sig)} symbols)"
        out.append(f"  {name:11}  →  {verdict}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Execution-profile sensitivity analysis")
    parser.add_argument("--symbols", default="NQ,ES,CL,GC",
                        help="Comma-separated data symbols (resolve to micro sims).")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--equity", type=float, default=50_000.0)
    parser.add_argument("--risk-pct", type=float, default=0.005)
    parser.add_argument("--source", default="yfinance",
                        choices=["auto", "tradovate", "yfinance", "synthetic", "local"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    micro_map = {"NQ": "MNQ", "ES": "MES", "GC": "MGC", "CL": "MCL"}
    rows: list[dict] = []
    for sym in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        sim_sym = micro_map.get(sym, sym)
        rows.extend(_run_symbol(sym, sim_sym, args.timeframe,
                                args.days, args.source,
                                args.equity, args.risk_pct))

    console.print(_render_table(rows,
        f"Sensitivity · {args.timeframe} · {args.days}d · source={args.source}"))
    console.print(Panel(_survival_verdict(rows),
                        title="Edge survival verdict (per profile)",
                        border_style="blue", title_align="left"))


if __name__ == "__main__":
    main()
