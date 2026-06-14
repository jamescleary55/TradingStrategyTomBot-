"""Forward-testing report: live vs backtest comparison + readiness verdict.

Reads the three forward logs and emits:

- A rich terminal summary
- ``forward_report.html`` — self-contained HTML (sidebar UI)
- ``forward_signals.csv`` — every signal as a row
- ``forward_summary.json`` — machine-readable rollup

Sections of the report:
- Headline KPIs (total, fill rate, win rate, avg R, expectancy, max DD)
- By symbol / session / setup subtype / HTF bias
- Skipped breakdown (which rules triggered most)
- **Do Not Trust Yet** — overfitting / readiness concerns

Usage:
    python -m live.forward_report
    python -m live.forward_report --since 7d --backtest-expectancy 0.96
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live.forward_log import load_signals, load_skipped, load_trades
from live.overfitting import Concern, evaluate as evaluate_concerns, render_lines

console = Console()
log = logging.getLogger("live.forward_report")
STATE_DIR = Path.home() / ".ict-bot"
REPORT_DIR = STATE_DIR / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
def _parse_since(s: Optional[str]):
    if not s:
        return None
    s = s.strip().lower()
    now = pd.Timestamp.utcnow().tz_localize("UTC")
    if s.endswith("d"):
        return now - pd.Timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return now - pd.Timedelta(hours=int(s[:-1]))
    if s.endswith("w"):
        return now - pd.Timedelta(weeks=int(s[:-1]))
    try:
        ts = pd.Timestamp(s)
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts
    except Exception:
        return None


def _within(row: dict, since) -> bool:
    if since is None:
        return True
    ts = row.get("ts_logged") or row.get("timestamp")
    if not ts:
        return False
    try:
        return pd.Timestamp(ts) >= since
    except Exception:
        return False


# ---------------------------------------------------------------------------
def _closed(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t.get("outcome") in ("target", "stop")
            and "r_realised" in t]


def _basic_stats(closed: list[dict]) -> dict:
    if not closed:
        return {"n": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "avg_R": 0.0, "median_R": 0.0, "expectancy_R": 0.0,
                "max_dd_R": 0.0, "total_R": 0.0}
    rs = [float(t.get("r_realised") or 0) for t in closed]
    wins = sum(1 for r in rs if r > 0)
    losses = sum(1 for r in rs if r <= 0)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    rs_sorted = sorted(rs)
    median = rs_sorted[len(rs_sorted) // 2]
    return {
        "n": len(rs), "wins": wins, "losses": losses,
        "win_rate": (wins / len(rs)) * 100,
        "avg_R": sum(rs) / len(rs),
        "median_R": median,
        "expectancy_R": sum(rs) / len(rs),
        "max_dd_R": max_dd,
        "total_R": cum,
    }


def _slippage(trades: list[dict]) -> dict:
    slips = [abs(float(t.get("slippage_pts") or 0))
             for t in trades if t.get("slippage_pts") is not None]
    return {
        "n_measured": len(slips),
        "avg_slip_pts": sum(slips) / len(slips) if slips else 0.0,
        "max_slip_pts": max(slips) if slips else 0.0,
    }


def _time_in_trade(trades: list[dict]) -> float:
    durations = []
    for t in trades:
        ts = t.get("timestamp")
        exit_ts = t.get("exit_ts")
        if ts and exit_ts:
            try:
                durations.append((pd.Timestamp(exit_ts) - pd.Timestamp(ts)).total_seconds() / 3600)
            except Exception:
                pass
    return (sum(durations) / len(durations)) if durations else 0.0


def _slice(field: str, closed: list[dict]) -> dict:
    by = {}
    for t in closed:
        k = t.get(field) or "?"
        by.setdefault(k, []).append(t)
    return {k: _basic_stats(v) for k, v in by.items()}


# ---------------------------------------------------------------------------
def compile_report(since=None, backtest_expectancy_R: Optional[float] = None) -> dict:
    signals = [r for r in load_signals() if _within(r, since)]
    skipped = [r for r in load_skipped() if _within(r, since)]
    trades = [r for r in load_trades() if _within(r, since)]
    closed = _closed(trades)

    n_signals = len(signals)
    n_skipped_from_signals = sum(1 for s in signals if not s.get("trade_allowed"))
    n_trades_attempted = len(trades)

    fill_rate = (len(closed) / n_trades_attempted * 100) if n_trades_attempted else 0.0
    overall = _basic_stats(closed)

    by_symbol = _slice("symbol", closed)
    by_session = _slice("session", closed)
    by_subtype = _slice("setup_subtype", closed)
    by_htf_bias = _slice("htf_bias", closed)

    skip_reasons = Counter(s.get("reason", "?") for s in skipped)

    slip = _slippage(trades)
    avg_hours = _time_in_trade(trades)

    concerns: list[Concern] = evaluate_concerns(
        signals=signals, trades=trades, skipped=skipped,
        backtest_expectancy_R=backtest_expectancy_R,
    )

    return {
        "since": str(since) if since else None,
        "totals": {
            "n_signals_detected": n_signals,
            "n_signals_blocked": n_skipped_from_signals,
            "n_skipped_log": len(skipped),
            "n_trades_attempted": n_trades_attempted,
            "n_trades_closed": len(closed),
            "fill_rate_pct": fill_rate,
            "avg_slip_pts": slip["avg_slip_pts"],
            "avg_time_in_trade_h": avg_hours,
        },
        "overall": overall,
        "by_symbol": by_symbol,
        "by_session": by_session,
        "by_subtype": by_subtype,
        "by_htf_bias": by_htf_bias,
        "skip_reasons": dict(skip_reasons.most_common()),
        "backtest_expectancy_R": backtest_expectancy_R,
        "concerns": [
            {"severity": c.severity, "code": c.code,
             "message": c.message, "detail": c.detail}
            for c in concerns
        ],
        "ready_for_real_money": not any(c.severity == "block" for c in concerns) and overall["n"] >= 30,
    }


# ---------------------------------------------------------------------------
def print_terminal(rep: dict) -> None:
    t = rep["totals"]
    o = rep["overall"]
    console.rule("[bold]Forward report[/bold]")

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column(justify="right")
    grid.add_row("Detected signals", str(t["n_signals_detected"]))
    grid.add_row("Blocked by gate", str(t["n_signals_blocked"]))
    grid.add_row("Trade attempts", str(t["n_trades_attempted"]))
    grid.add_row("Trades closed (target/stop)", str(t["n_trades_closed"]))
    grid.add_row("Fill rate", f"{t['fill_rate_pct']:.1f}%")
    grid.add_row("Avg slippage (pts)", f"{t['avg_slip_pts']:.2f}")
    grid.add_row("Avg time in trade (h)", f"{t['avg_time_in_trade_h']:.2f}")
    console.print(Panel(grid, title="Totals", border_style="blue", title_align="left"))

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold"); grid.add_column(justify="right")
    avg_color = "green" if o["avg_R"] >= 0 else "red"
    grid.add_row("Wins / Losses", f"[green]{o['wins']}[/green] / [red]{o['losses']}[/red]")
    grid.add_row("Win rate", f"{o['win_rate']:.1f}%")
    grid.add_row("Avg R", f"[{avg_color}]{o['avg_R']:+.2f}R[/{avg_color}]")
    grid.add_row("Median R", f"{o['median_R']:+.2f}R")
    grid.add_row("Expectancy", f"[{avg_color}]{o['expectancy_R']:+.2f}R[/{avg_color}]")
    grid.add_row("Max drawdown", f"[red]{o['max_dd_R']:+.2f}R[/red]")
    grid.add_row("Total R", f"[{avg_color}]{o['total_R']:+.2f}R[/{avg_color}]")
    console.print(Panel(grid, title="Closed trades — performance",
                        border_style="green" if o["avg_R"] >= 0 else "red",
                        title_align="left"))

    for field, label in (("by_symbol", "By symbol"),
                         ("by_session", "By session"),
                         ("by_subtype", "By setup subtype"),
                         ("by_htf_bias", "By HTF bias state")):
        d = rep[field]
        if not d:
            continue
        tbl = Table(title=label, header_style="bold")
        tbl.add_column("Key")
        tbl.add_column("N", justify="right")
        tbl.add_column("Win %", justify="right")
        tbl.add_column("Avg R", justify="right")
        tbl.add_column("Total R", justify="right")
        for k, v in sorted(d.items(), key=lambda kv: -kv[1]["n"]):
            color = "green" if v["avg_R"] >= 0 else "red"
            tbl.add_row(
                str(k), str(v["n"]),
                f"{v['win_rate']:.0f}%" if v["n"] else "—",
                f"[{color}]{v['avg_R']:+.2f}R[/{color}]",
                f"[{color}]{v['total_R']:+.2f}R[/{color}]",
            )
        console.print(tbl)

    if rep["skip_reasons"]:
        tbl = Table(title="Skipped breakdown", header_style="bold")
        tbl.add_column("Reason")
        tbl.add_column("Count", justify="right")
        for r, n in rep["skip_reasons"].items():
            tbl.add_row(r, str(n))
        console.print(tbl)

    # Do Not Trust Yet
    body = "\n".join(render_lines([Concern(**c) for c in rep["concerns"]]))
    border = "red" if any(c["severity"] == "block" for c in rep["concerns"]) else "yellow"
    console.print(Panel(body, title="Do Not Trust Yet",
                        border_style=border, title_align="left"))

    verdict = ("✓  No blocking concerns. Stats are still tentative."
               if rep["ready_for_real_money"]
               else "✗  NOT ready for real money. See concerns above.")
    color = "green" if rep["ready_for_real_money"] else "red"
    console.print(f"[{color} bold]{verdict}[/{color} bold]")


# ---------------------------------------------------------------------------
def write_csv(rep: dict, path: Path) -> Path:
    signals = load_signals()
    if not signals:
        path.write_text("no signals\n")
        return path
    keys = sorted({k for r in signals for k in r.keys()})
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in signals:
            w.writerow({k: r.get(k, "") for k in keys})
    return path


def write_json(rep: dict, path: Path) -> Path:
    path.write_text(json.dumps(rep, default=str, indent=2))
    return path


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/>
<title>Forward report</title>
<style>
  body{{margin:0;padding:28px;background:#07100d;color:#ecf6f0;
       font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display",system-ui,sans-serif;
       font-size:14px;-webkit-font-smoothing:antialiased;}}
  h1{{font-size:22px;margin:0 0 4px;letter-spacing:-0.02em;}}
  .sub{{color:#7c9287;font-size:12px;margin-bottom:18px;}}
  .grid{{display:grid;gap:14px;}}
  .cols-4{{grid-template-columns:repeat(4,1fr);}}
  .cols-2{{grid-template-columns:1fr 1fr;}}
  .card{{background:#0f1a16;border:1px solid #1f3329;border-radius:14px;padding:18px 20px;}}
  .card h2{{margin:0 0 10px;font-size:11px;letter-spacing:0.6px;text-transform:uppercase;
           color:#7c9287;font-weight:600;}}
  .kpi .v{{font-size:26px;font-weight:700;letter-spacing:-0.02em;font-variant-numeric:tabular-nums;}}
  .kpi .s{{color:#7c9287;font-size:12px;margin-top:4px;}}
  .green{{color:#22c55e;}} .red{{color:#f87171;}} .yellow{{color:#fbbf24;}} .dim{{color:#7c9287;}}
  table{{width:100%;border-collapse:collapse;font-size:13px;}}
  th{{text-align:left;color:#7c9287;padding:8px 10px;border-bottom:1px solid #1f3329;
     font-size:10.5px;letter-spacing:0.5px;text-transform:uppercase;font-weight:600;}}
  td{{padding:8px 10px;border-bottom:1px solid #1f3329;font-variant-numeric:tabular-nums;}}
  tr:last-child td{{border-bottom:none;}}
  td.right,th.right{{text-align:right;}}
  .concern{{background:#1a2e26;border:1px solid #2a4234;border-radius:10px;
            padding:12px 14px;margin-bottom:10px;}}
  .concern.block{{border-color:#f87171;}}
  .concern.warn{{border-color:#fbbf24;}}
  .concern.info{{border-color:#67e8f9;}}
  .concern .code{{font-size:10px;text-transform:uppercase;color:#7c9287;letter-spacing:0.4px;}}
  .verdict{{padding:14px 16px;border-radius:12px;font-size:15px;font-weight:600;}}
  .verdict.go{{background:rgba(34,197,94,0.15);color:#22c55e;border:1px solid #22c55e;}}
  .verdict.no{{background:rgba(248,113,113,0.15);color:#f87171;border:1px solid #f87171;}}
</style>
</head>
<body>
  <h1>ICT forward report</h1>
  <div class="sub">generated {now} · since {since} · backtest expectancy reference: {bt_exp}</div>

  <div class="grid cols-4">{kpi_cards}</div>

  <div class="card" style="margin-top:14px;">
    <h2>Closed trades — performance</h2>
    {overall_table}
  </div>

  <div class="grid cols-2" style="margin-top:14px;">
    <div class="card"><h2>By symbol</h2>{by_symbol}</div>
    <div class="card"><h2>By session</h2>{by_session}</div>
  </div>
  <div class="grid cols-2" style="margin-top:14px;">
    <div class="card"><h2>By setup subtype</h2>{by_subtype}</div>
    <div class="card"><h2>By HTF bias</h2>{by_htf_bias}</div>
  </div>

  <div class="card" style="margin-top:14px;">
    <h2>Skipped breakdown</h2>
    {skip_table}
  </div>

  <div class="card" style="margin-top:14px;">
    <h2>Do Not Trust Yet</h2>
    {concern_html}
    <div class="verdict {verdict_cls}" style="margin-top:14px;">{verdict_text}</div>
  </div>
</body></html>"""


def _slice_table_html(d: dict) -> str:
    if not d:
        return '<div class="dim">no data</div>'
    rows = ""
    for k, v in sorted(d.items(), key=lambda kv: -kv[1]["n"]):
        avg_cls = "green" if v["avg_R"] >= 0 else "red"
        rows += (f"<tr><td>{html.escape(str(k))}</td>"
                 f"<td class='right'>{v['n']}</td>"
                 f"<td class='right'>{v['win_rate']:.0f}%</td>"
                 f"<td class='right {avg_cls}'>{v['avg_R']:+.2f}R</td>"
                 f"<td class='right {avg_cls}'>{v['total_R']:+.2f}R</td></tr>")
    return ("<table><thead><tr><th>Key</th><th class='right'>N</th>"
            "<th class='right'>Win %</th><th class='right'>Avg R</th>"
            "<th class='right'>Total R</th></tr></thead><tbody>" + rows + "</tbody></table>")


def write_html(rep: dict, path: Path) -> Path:
    t, o = rep["totals"], rep["overall"]
    avg_cls = "green" if o["avg_R"] >= 0 else "red"
    kpi = "".join([
        f"<div class='card kpi'><h2>Signals detected</h2><div class='v'>{t['n_signals_detected']}</div><div class='s'>{t['n_signals_blocked']} blocked</div></div>",
        f"<div class='card kpi'><h2>Closed trades</h2><div class='v'>{t['n_trades_closed']}</div><div class='s'>{t['fill_rate_pct']:.0f}% fill rate</div></div>",
        f"<div class='card kpi'><h2>Win rate</h2><div class='v'>{o['win_rate']:.0f}%</div><div class='s'>{o['wins']}W / {o['losses']}L</div></div>",
        f"<div class='card kpi'><h2>Avg R</h2><div class='v {avg_cls}'>{o['avg_R']:+.2f}R</div><div class='s'>total {o['total_R']:+.2f}R, DD {o['max_dd_R']:+.2f}R</div></div>",
    ])
    overall_rows = "".join([
        f"<tr><td>Wins / Losses</td><td class='right'><span class='green'>{o['wins']}</span> / <span class='red'>{o['losses']}</span></td></tr>",
        f"<tr><td>Win rate</td><td class='right'>{o['win_rate']:.1f}%</td></tr>",
        f"<tr><td>Avg R</td><td class='right {avg_cls}'>{o['avg_R']:+.2f}R</td></tr>",
        f"<tr><td>Median R</td><td class='right'>{o['median_R']:+.2f}R</td></tr>",
        f"<tr><td>Total R</td><td class='right {avg_cls}'>{o['total_R']:+.2f}R</td></tr>",
        f"<tr><td>Max DD (R)</td><td class='right red'>{o['max_dd_R']:+.2f}R</td></tr>",
        f"<tr><td>Avg slippage (pts)</td><td class='right'>{t['avg_slip_pts']:.2f}</td></tr>",
        f"<tr><td>Avg time in trade (h)</td><td class='right'>{t['avg_time_in_trade_h']:.2f}</td></tr>",
    ])
    overall_table = "<table><tbody>" + overall_rows + "</tbody></table>"

    skip_rows = "".join(f"<tr><td>{html.escape(k)}</td><td class='right'>{v}</td></tr>"
                        for k, v in rep["skip_reasons"].items()) or '<tr><td class="dim" colspan="2">no skips logged</td></tr>'
    skip_table = "<table><thead><tr><th>Reason</th><th class='right'>Count</th></tr></thead><tbody>" + skip_rows + "</tbody></table>"

    concerns_html = ""
    for c in rep["concerns"]:
        concerns_html += (
            f"<div class='concern {html.escape(c['severity'])}'>"
            f"<div class='code'>{html.escape(c['severity'])} · {html.escape(c['code'])}</div>"
            f"<div style='margin-top:4px;font-weight:600;'>{html.escape(c['message'])}</div>"
            + (f"<div class='dim' style='margin-top:6px;font-size:12.5px;'>{html.escape(c['detail'])}</div>" if c['detail'] else "")
            + "</div>"
        )
    if not concerns_html:
        concerns_html = '<div class="dim">No automated concerns flagged. (Not the same as &ldquo;safe to trade live.&rdquo;)</div>'

    verdict_cls = "go" if rep["ready_for_real_money"] else "no"
    verdict_text = ("✓  No blocking concerns. Stats are still tentative."
                    if rep["ready_for_real_money"]
                    else "✗  NOT ready for real money. See concerns above.")

    path.write_text(HTML_TEMPLATE.format(
        now=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        since=rep.get("since") or "all time",
        bt_exp=(f"{rep['backtest_expectancy_R']:+.2f}R"
                if rep.get("backtest_expectancy_R") is not None else "—"),
        kpi_cards=kpi,
        overall_table=overall_table,
        by_symbol=_slice_table_html(rep["by_symbol"]),
        by_session=_slice_table_html(rep["by_session"]),
        by_subtype=_slice_table_html(rep["by_subtype"]),
        by_htf_bias=_slice_table_html(rep["by_htf_bias"]),
        skip_table=skip_table,
        concern_html=concerns_html,
        verdict_cls=verdict_cls,
        verdict_text=verdict_text,
    ))
    return path


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ICT forward-test report")
    parser.add_argument("--since", default=None,
                        help="Only include rows since: 7d, 4w, ISO date.")
    parser.add_argument("--backtest-expectancy", type=float, default=None,
                        help="Reference IS expectancy in R for the gap check.")
    parser.add_argument("--out-dir", default=str(REPORT_DIR))
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    since = _parse_since(args.since)
    rep = compile_report(since=since, backtest_expectancy_R=args.backtest_expectancy)
    print_terminal(rep)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    html_path = write_html(rep, out_dir / f"forward_report_{ts}.html")
    csv_path = write_csv(rep, out_dir / f"forward_signals_{ts}.csv")
    json_path = write_json(rep, out_dir / f"forward_summary_{ts}.json")
    console.print()
    console.print(f"  html → {html_path}")
    console.print(f"  csv  → {csv_path}")
    console.print(f"  json → {json_path}")


if __name__ == "__main__":
    main()
