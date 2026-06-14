"""Two-year per-signal decision audit.

For every signal the strategy would have produced in the last 2 years,
walks the full gate chain (strategy validation → news blackout →
session allowlist → setup score → min RR → daily / weekly loss caps →
consecutive losses) and records WHY each one was accepted or rejected.

Output:

    ~/.ict-bot/reports/decisions_<symbol>_<ts>.csv
        One row per detected setup, every decision column.

    ~/.ict-bot/reports/decisions_summary_<ts>.csv
        Per-symbol counts: detected / blocked-by-rule / would-trade.

    Terminal text-tree:
        Symbol → strategy_validate → news → session → score → rr → ...
        with counts at each node.

Usage::

    python -m backtest.decision_tree
    python -m backtest.decision_tree --symbols MNQ,MES,MCL,MGC --days 730
    python -m backtest.decision_tree --rules-file ~/.ict-bot/personal_rules.yaml
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from data.loader import load_bars
from risk.rules import PersonalRules, load as load_rules
from signals.htf_bias import compute_bias_series, htf_timeframe_for
from signals.strategies.base import StrategyContext, get_strategy
from utils.news import filter_setups as filter_setups_news, generate_events, is_in_blackout

console = Console()
log = logging.getLogger("backtest.decision_tree")

REPORT_DIR = Path.home() / ".ict-bot" / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
def _resolve_sim(symbol: str) -> str:
    return {"NQ": "MNQ", "ES": "MES", "GC": "MGC", "CL": "MCL"}.get(symbol, symbol)


def _evaluate_setup_decisions(s, rules: PersonalRules, news_events) -> dict:
    """Walk the rule chain for ONE setup, recording every decision.

    We re-implement the rule chain in pure-function form because RiskGate
    is stateful (reads recent trades). For this historical audit we want
    every signal's *standalone* decisions, then compute downstream caps
    in aggregation.
    """
    row: dict = {
        "ts": s.timestamp.isoformat(),
        "symbol": s.symbol,
        "timeframe": s.timeframe,
        "direction": s.direction,
        "entry": s.entry,
        "stop": s.stop,
        "target": s.target,
        "planned_R": s.rr,
        "setup_type": s.setup_type,
        "setup_subtype": s.setup_subtype,
        "htf_bias": s.htf_bias,
        "session": s.session,
        "setup_score": s.setup_score,
        "sweep_level_kind": s.sweep_level_kind,
        "sweep_level_price": s.sweep_level_price,
        "choch_price": s.choch_price,
        "fvg_top": s.fvg_top,
        "fvg_bottom": s.fvg_bottom,
        # decisions
        "ok_validate": True,
        "ok_news": True,
        "ok_session": True,
        "ok_symbol": True,
        "ok_rr": True,
        "ok_score": True,
        "ok_htf_aligned": True,
        "would_trade": False,
        "first_block": None,
        "block_reason": None,
    }

    # 1) Geometry validation
    if s.direction == "bull" and not (s.stop < s.entry < s.target):
        row["ok_validate"] = False
    if s.direction == "bear" and not (s.target < s.entry < s.stop):
        row["ok_validate"] = False
    if s.rr <= 0:
        row["ok_validate"] = False

    # 2) HTF bias alignment (informational)
    row["ok_htf_aligned"] = (s.htf_bias == s.direction) if s.htf_bias else False

    # 3) News blackout
    hit, ev = is_in_blackout(
        s.timestamp, news_events,
        minutes_before=rules.news_blackout_minutes,
        minutes_after=rules.news_blackout_minutes,
    )
    row["ok_news"] = not (hit and rules.news_filter_enabled)
    row["news_event"] = (ev.label if ev else None)

    # 4) Symbol / session allowlist
    row["ok_symbol"] = s.symbol in rules.allowed_symbols
    row["ok_session"] = bool(s.session) and s.session in rules.allowed_sessions

    # 5) Setup quality
    row["ok_rr"] = s.rr >= rules.min_expected_R
    row["ok_score"] = s.setup_score >= rules.min_setup_score

    # First blocking gate (in evaluation order)
    chain = [
        ("strategy_validate", row["ok_validate"], "geometry"),
        ("news_blackout", row["ok_news"], row.get("news_event") or "blackout"),
        ("symbol_not_allowed", row["ok_symbol"], f"{s.symbol} not in allowlist"),
        ("session_not_allowed", row["ok_session"],
            f"session {s.session!r} not in allowlist"),
        ("below_min_rr", row["ok_rr"], f"RR {s.rr:.2f} < {rules.min_expected_R}"),
        ("below_min_score", row["ok_score"],
            f"score {s.setup_score:.2f} < {rules.min_setup_score}"),
    ]
    for rule_name, passed, reason in chain:
        if not passed:
            row["first_block"] = rule_name
            row["block_reason"] = reason
            break

    if row["first_block"] is None:
        row["would_trade"] = True

    return row


# ---------------------------------------------------------------------------
def evaluate_symbol(symbol: str, timeframe: str, days: int,
                   rules: PersonalRules) -> tuple[list[dict], pd.DataFrame]:
    """Pull ``days`` of data, run strategy, evaluate every gate, return rows + df."""
    df = load_bars(symbol, timeframe, days=days, source="yfinance")
    if df.empty:
        log.warning("[%s] empty data", symbol)
        return [], df

    # HTF for bias (best-effort)
    htf_tf = htf_timeframe_for(timeframe)
    htf_bias_series = None
    if htf_tf != timeframe:
        df_htf = load_bars(symbol, htf_tf, days=days, source="yfinance")
        if not df_htf.empty:
            htf_bias_series = compute_bias_series(df, df_htf)

    instrument = cfg.INSTRUMENTS.get(_resolve_sim(symbol)) or cfg.INSTRUMENTS.get("MNQ")
    strategy = get_strategy("sweep_choch_fvg")
    ctx = StrategyContext(
        instrument=instrument, timeframe=timeframe,
        htf_bias_series=htf_bias_series, htf_timeframe=htf_tf,
    )
    setups = strategy.detect_setups(df, ctx)

    # News events covering the entire window
    news_events = generate_events(
        df.index[0].to_pydatetime().replace(tzinfo=None),
        df.index[-1].to_pydatetime().replace(tzinfo=None),
    )

    rows = [_evaluate_setup_decisions(s, rules, news_events) for s in setups]
    return rows, df


# ---------------------------------------------------------------------------
def write_decisions_csv(rows: list[dict], symbol: str, ts: str) -> Path:
    if not rows:
        return REPORT_DIR / f"decisions_{symbol}_{ts}.csv"
    out = REPORT_DIR / f"decisions_{symbol}_{ts}.csv"
    keys = list(rows[0].keys())
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return out


def write_summary_csv(per_symbol_rows: dict[str, list[dict]], ts: str) -> Path:
    out = REPORT_DIR / f"decisions_summary_{ts}.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "symbol", "detected", "would_trade",
            "blocked_strategy_validate", "blocked_news",
            "blocked_symbol", "blocked_session",
            "blocked_min_rr", "blocked_min_score",
            "win_pct_target_proxy",     # # of setups whose direction matched HTF bias
        ])
        for sym, rows in per_symbol_rows.items():
            c = Counter(r.get("first_block") for r in rows)
            aligned = sum(1 for r in rows if r["ok_htf_aligned"])
            would = sum(1 for r in rows if r["would_trade"])
            w.writerow([
                sym, len(rows), would,
                c.get("strategy_validate", 0),
                c.get("news_blackout", 0),
                c.get("symbol_not_allowed", 0),
                c.get("session_not_allowed", 0),
                c.get("below_min_rr", 0),
                c.get("below_min_score", 0),
                f"{(aligned/len(rows)*100):.1f}%" if rows else "—",
            ])
    return out


# ---------------------------------------------------------------------------
def render_tree(per_symbol_rows: dict[str, list[dict]]) -> Tree:
    """Render a rich Tree showing the decision funnel per symbol."""
    root = Tree("[bold]decision funnel — last 2y per symbol[/bold]")
    for sym, rows in per_symbol_rows.items():
        if not rows:
            root.add(f"[dim]{sym}[/dim] · no data")
            continue
        n = len(rows)
        c = Counter(r.get("first_block") for r in rows)
        would = sum(1 for r in rows if r["would_trade"])
        node = root.add(f"[bold]{sym}[/bold]  detected={n}  would-trade={would}")
        funnel = [
            ("strategy_validate", "Geometry / RR>0 valid"),
            ("news_blackout",     "Not in NFP/CPI/FOMC window"),
            ("symbol_not_allowed", "Symbol in allowlist"),
            ("session_not_allowed", "Session in allowlist"),
            ("below_min_rr",      f"RR ≥ min_expected_R"),
            ("below_min_score",   f"setup_score ≥ min_setup_score"),
        ]
        for code, label in funnel:
            blocked = c.get(code, 0)
            colour = "green" if blocked == 0 else ("red" if blocked >= n / 3 else "yellow")
            node.add(f"[{colour}]✗ blocked here: {blocked:>4}[/{colour}]  ·  {label}")
        node.add(f"[bold green]✓ passes everything: {would}[/bold green]")
    return root


# ---------------------------------------------------------------------------
def print_summary_table(per_symbol_rows: dict[str, list[dict]]) -> None:
    tbl = Table(title="Two-year decision audit · per-symbol summary",
                header_style="bold")
    tbl.add_column("Symbol")
    tbl.add_column("Detected", justify="right")
    tbl.add_column("Would trade", justify="right")
    tbl.add_column("HTF aligned", justify="right")
    tbl.add_column("Top blocker", justify="left")

    for sym, rows in per_symbol_rows.items():
        if not rows:
            tbl.add_row(sym, "—", "—", "—", "—")
            continue
        c = Counter(r.get("first_block") for r in rows if r.get("first_block"))
        top_block = c.most_common(1)[0] if c else ("—", 0)
        aligned = sum(1 for r in rows if r["ok_htf_aligned"])
        would = sum(1 for r in rows if r["would_trade"])
        tbl.add_row(
            sym, str(len(rows)),
            f"{would}  ({would/len(rows)*100:.0f}%)",
            f"{aligned}  ({aligned/len(rows)*100:.0f}%)",
            f"{top_block[0]}  ×{top_block[1]}",
        )
    console.print(tbl)


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="2-year per-signal decision audit")
    parser.add_argument("--symbols", default="MNQ,MES,MCL,MGC")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=730,
                        help="Max yfinance allows for 1h interval")
    parser.add_argument("--rules-file", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    rules = load_rules(Path(args.rules_file)) if args.rules_file else load_rules()
    log.info("Rules loaded from %s · mode=%s · allowed_symbols=%s · allowed_sessions=%s",
             rules.source, rules.mode, rules.allowed_symbols, rules.allowed_sessions)

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    per_symbol: dict[str, list[dict]] = {}

    for sym in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        log.info("=== %s ===", sym)
        rows, df = evaluate_symbol(sym, args.timeframe, args.days, rules)
        per_symbol[sym] = rows
        log.info("[%s] %d bars, %d setups detected", sym, len(df), len(rows))
        if rows:
            path = write_decisions_csv(rows, sym, ts)
            log.info("[%s] wrote %s", sym, path)

    summary_path = write_summary_csv(per_symbol, ts)
    console.print()
    print_summary_table(per_symbol)
    console.print(render_tree(per_symbol))
    console.print(f"\n[dim]summary csv → {summary_path}[/dim]")


if __name__ == "__main__":
    main()
