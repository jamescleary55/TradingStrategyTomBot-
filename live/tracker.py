"""Live alert outcome resolver + performance stats.

Closes the loop on the monitor. The monitor appends one JSON row per
alert to ``~/.ict-bot/alerts.jsonl``. This module walks every unresolved
row and decides what actually happened:

- ``pending`` — limit hasn't been touched yet, no stop hit either
- ``filled``  — limit touched, still open
- ``target``  — TP hit first
- ``stop``    — SL hit first
- ``voided``  — stop touched before entry (limit never filled)
- ``expired`` — older than ``--max-age-hours`` and still unfilled

Resolutions are written back into the same JSONL row (overwritten via a
rewrite of the file). Stats are computed from resolved rows.

Subcommands:

    python -m live.tracker resolve              # walk & resolve all open alerts
    python -m live.tracker stats                # show live performance dashboard
    python -m live.tracker stats --since 7d     # last 7 days only
    python -m live.tracker show 12              # full detail of alert #12
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from data.loader import load_bars

console = Console()
log = logging.getLogger("live.tracker")

STATE_DIR = Path.home() / ".ict-bot"
STATE_DIR.mkdir(parents=True, exist_ok=True)
ALERT_LOG = STATE_DIR / "alerts.jsonl"


# ---------------------------------------------------------------------------
def _load_alerts() -> list[dict]:
    if not ALERT_LOG.exists():
        return []
    rows: list[dict] = []
    with open(ALERT_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _save_alerts(rows: list[dict]) -> None:
    """Atomic rewrite (tmp + rename)."""
    tmp = ALERT_LOG.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    tmp.replace(ALERT_LOG)


def _ensure_status(row: dict) -> None:
    if "status" not in row:
        row["status"] = "pending"


# ---------------------------------------------------------------------------
def _resolve_one(row: dict, source: str = "yfinance",
                 max_age_hours: float = 240) -> bool:
    """Mutate ``row`` in place with status & exit details. Returns True if
    anything changed.
    """
    _ensure_status(row)
    if row["status"] in ("target", "stop", "voided", "expired"):
        return False

    symbol = row.get("symbol")
    timeframe = row.get("timeframe", "1h")
    choch_ts_raw = row.get("choch_ts")
    if not symbol or not choch_ts_raw:
        return False

    try:
        choch_ts = pd.Timestamp(choch_ts_raw)
        if choch_ts.tzinfo is None:
            choch_ts = choch_ts.tz_localize("UTC")
    except Exception:
        return False

    # Skip if older than max_age and still pending
    now = pd.Timestamp.utcnow()
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    age_hours = (now - choch_ts).total_seconds() / 3600
    if age_hours > max_age_hours and row["status"] == "pending":
        row["status"] = "expired"
        row["resolved_at"] = now.isoformat()
        return True

    # Pull enough recent bars to cover the alert's age
    days = max(2, int(age_hours / 24) + 2)
    df = load_bars(symbol, timeframe, days=days, source=source)
    if df.empty:
        return False

    # Only consider bars strictly after the CHoCH
    after = df[df.index > choch_ts]
    if after.empty:
        return False

    entry = float(row["entry"])
    stop = float(row["stop"])
    target = float(row["target"])
    direction = row["direction"]
    filled_idx = None
    filled_ts = None
    changed = False

    if row["status"] == "pending":
        for i, ts in enumerate(after.index):
            h, l = float(after["high"].iloc[i]), float(after["low"].iloc[i])
            # Voided if stop hit before entry touched
            stop_hit_first = (direction == "bull" and l <= stop and not (l <= entry <= h)) or \
                             (direction == "bear" and h >= stop and not (l <= entry <= h))
            entry_touched = l <= entry <= h
            if stop_hit_first and not entry_touched:
                row["status"] = "voided"
                row["resolved_at"] = ts.isoformat()
                changed = True
                break
            if entry_touched:
                row["status"] = "filled"
                row["fill_ts"] = ts.isoformat()
                filled_idx = i
                filled_ts = ts
                changed = True
                break

    if row["status"] != "filled":
        return changed

    # Walk forward from fill to find first-touch stop/target
    fill_ts = pd.Timestamp(row.get("fill_ts", choch_ts.isoformat()))
    if fill_ts.tzinfo is None:
        fill_ts = fill_ts.tz_localize("UTC")
    post_fill = after[after.index >= fill_ts]
    for i in range(len(post_fill)):
        h, l = float(post_fill["high"].iloc[i]), float(post_fill["low"].iloc[i])
        ts = post_fill.index[i]
        stop_hit = (direction == "bull" and l <= stop) or (direction == "bear" and h >= stop)
        target_hit = (direction == "bull" and h >= target) or (direction == "bear" and l <= target)
        if stop_hit and target_hit:
            outcome = "stop"     # pessimistic when same bar straddles both
            exit_px = stop
        elif stop_hit:
            outcome = "stop"
            exit_px = stop
        elif target_hit:
            outcome = "target"
            exit_px = target
        else:
            continue
        row["status"] = outcome
        row["exit_ts"] = ts.isoformat()
        row["exit_price"] = exit_px
        # Realised R
        risk = abs(entry - stop)
        if direction == "bull":
            pnl_price = exit_px - entry
        else:
            pnl_price = entry - exit_px
        row["r_multiple"] = (pnl_price / risk) if risk > 0 else 0.0
        # Realised USD if a plan was attached
        plan = row.get("plan") or {}
        if plan.get("contracts"):
            inst = cfg.INSTRUMENTS.get(row.get("sim_symbol")) or cfg.INSTRUMENTS.get("MNQ")
            if inst:
                row["pnl_usd"] = pnl_price * inst.point_value * plan["contracts"]
        return True
    return changed


# ---------------------------------------------------------------------------
def cmd_resolve(args) -> None:
    rows = _load_alerts()
    if not rows:
        console.print("[dim]No alerts logged yet.[/dim]")
        return
    n_changed = 0
    for r in rows:
        if _resolve_one(r, source=args.source, max_age_hours=args.max_age_hours):
            n_changed += 1
    _save_alerts(rows)
    console.print(f"[green]Resolved {n_changed} alert(s).[/green]  "
                  f"Total in log: {len(rows)}")


def _parse_since(s: Optional[str]) -> Optional[pd.Timestamp]:
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
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts
    except Exception:
        return None


def cmd_stats(args) -> None:
    rows = _load_alerts()
    # Auto-resolve any pending before stats unless --no-resolve
    if not args.no_resolve:
        for r in rows:
            _resolve_one(r, source=args.source, max_age_hours=args.max_age_hours)
        _save_alerts(rows)

    since = _parse_since(args.since)
    if since is not None:
        rows = [r for r in rows
                if r.get("ts_alerted") and pd.Timestamp(r["ts_alerted"]) >= since]

    if not rows:
        console.print("[dim]No alerts in the requested window.[/dim]")
        return

    # Aggregate
    def _empty_bucket() -> dict:
        return {"alerts": 0, "filled": 0, "wins": 0, "losses": 0,
                "voided": 0, "pending": 0, "expired": 0,
                "sum_r": 0.0, "sum_pnl": 0.0}
    by_symbol: dict[str, dict] = {}
    totals = _empty_bucket()
    for r in rows:
        _ensure_status(r)
        sym = r.get("symbol", "?")
        bucket = by_symbol.setdefault(sym, _empty_bucket())
        bucket["alerts"] += 1
        totals["alerts"] += 1
        status = r["status"]
        if status == "target":
            bucket["wins"] += 1
            bucket["filled"] += 1
            totals["wins"] += 1
            totals["filled"] += 1
        elif status == "stop":
            bucket["losses"] += 1
            bucket["filled"] += 1
            totals["losses"] += 1
            totals["filled"] += 1
        elif status == "voided":
            bucket["voided"] += 1
            totals["voided"] += 1
        elif status == "filled":
            bucket["filled"] += 1
            totals["filled"] += 1
        elif status == "expired":
            bucket["expired"] += 1
            totals["expired"] += 1
        else:  # pending
            bucket["pending"] += 1
            totals["pending"] += 1
        if "r_multiple" in r:
            bucket["sum_r"] += float(r["r_multiple"])
            totals["sum_r"] += float(r["r_multiple"])
        if "pnl_usd" in r:
            bucket["sum_pnl"] += float(r["pnl_usd"])
            totals["sum_pnl"] += float(r["pnl_usd"])

    # Print per-symbol + portfolio
    tbl = Table(title=f"Live performance "
                      f"(since {args.since or 'beginning'})",
                header_style="bold")
    tbl.add_column("Symbol")
    tbl.add_column("Alerts", justify="right")
    tbl.add_column("Filled", justify="right")
    tbl.add_column("W / L", justify="right")
    tbl.add_column("Pending", justify="right")
    tbl.add_column("Voided", justify="right")
    tbl.add_column("Win %", justify="right")
    tbl.add_column("Avg R", justify="right")
    tbl.add_column("Realised P&L", justify="right")

    def fmt_row(name: str, b: dict) -> list:
        win_rate = (b["wins"] / (b["wins"] + b["losses"]) * 100) if (b["wins"] + b["losses"]) else 0
        closed = b["wins"] + b["losses"]
        avg_r = (b["sum_r"] / closed) if closed else 0
        avg_color = "green" if avg_r > 0 else ("red" if avg_r < 0 else "dim")
        pnl_color = "green" if b["sum_pnl"] > 0 else ("red" if b["sum_pnl"] < 0 else "dim")
        return [
            name, str(b["alerts"]), str(b["filled"]),
            f"[green]{b['wins']}[/green] / [red]{b['losses']}[/red]",
            str(b["pending"]), str(b["voided"]),
            f"{win_rate:.0f}%" if closed else "—",
            f"[{avg_color}]{avg_r:+.2f}R[/{avg_color}]",
            f"[{pnl_color}]${b['sum_pnl']:+,.2f}[/{pnl_color}]",
        ]

    for sym in sorted(by_symbol):
        tbl.add_row(*fmt_row(sym, by_symbol[sym]))
    tbl.add_section()
    tbl.add_row(*fmt_row("[bold]Portfolio[/bold]", totals))
    console.print(tbl)

    # Quick equity narrative line
    if totals["sum_pnl"] != 0 or totals["sum_r"] != 0:
        color = "green" if totals["sum_pnl"] >= 0 else "red"
        console.print(
            f"\n[{color}]●[/{color}]  {totals['wins']}W / {totals['losses']}L  ·  "
            f"{totals['filled']} filled of {totals['alerts']} alerts  ·  "
            f"realised ${totals['sum_pnl']:+,.2f}  ·  "
            f"{totals['pending']} still open"
        )


def cmd_show(args) -> None:
    rows = _load_alerts()
    if args.index < 1 or args.index > len(rows):
        console.print(f"[red]Index out of range: 1..{len(rows)}[/red]")
        return
    r = rows[args.index - 1]
    body = json.dumps(r, indent=2)
    console.print(Panel(body, title=f"Alert #{args.index}",
                        border_style="blue", title_align="left"))


def cmd_recent(args) -> None:
    rows = _load_alerts()
    if not rows:
        console.print("[dim]No alerts logged yet.[/dim]")
        return
    rows = rows[-args.n:]
    tbl = Table(title=f"Last {len(rows)} alert(s)", header_style="bold")
    tbl.add_column("#", justify="right")
    tbl.add_column("When alerted")
    tbl.add_column("Symbol")
    tbl.add_column("Dir")
    tbl.add_column("Entry", justify="right")
    tbl.add_column("Stop", justify="right")
    tbl.add_column("Target", justify="right")
    tbl.add_column("RR", justify="right")
    tbl.add_column("Status")
    tbl.add_column("R", justify="right")
    for i, r in enumerate(rows, start=len(_load_alerts()) - len(rows) + 1):
        _ensure_status(r)
        status = r["status"]
        status_color = {
            "target": "green", "stop": "red", "voided": "yellow",
            "filled": "cyan", "pending": "dim", "expired": "dim",
        }.get(status, "white")
        r_mult = r.get("r_multiple")
        r_color = "green" if (r_mult or 0) > 0 else "red"
        tbl.add_row(
            str(i),
            (r.get("ts_alerted") or "")[:19].replace("T", " "),
            r.get("symbol", "?"),
            r.get("direction", "?"),
            f"{r.get('entry', 0):.2f}",
            f"{r.get('stop', 0):.2f}",
            f"{r.get('target', 0):.2f}",
            f"{r.get('rr', 0):.2f}",
            f"[{status_color}]{status}[/{status_color}]",
            f"[{r_color}]{r_mult:+.2f}R[/{r_color}]" if r_mult is not None else "—",
        )
    console.print(tbl)


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Live alert outcome tracker")
    subs = parser.add_subparsers(dest="cmd", required=True)

    p_res = subs.add_parser("resolve", help="Walk unresolved alerts and decide their outcome")
    p_res.add_argument("--source", default="yfinance",
                       choices=["auto", "tradovate", "yfinance", "synthetic"])
    p_res.add_argument("--max-age-hours", type=float, default=240)

    p_st = subs.add_parser("stats", help="Show live performance dashboard")
    p_st.add_argument("--since", default=None,
                      help="Filter to recent window: 24h, 7d, 4w, or an ISO date.")
    p_st.add_argument("--source", default="yfinance",
                      choices=["auto", "tradovate", "yfinance", "synthetic"])
    p_st.add_argument("--max-age-hours", type=float, default=240)
    p_st.add_argument("--no-resolve", action="store_true",
                      help="Skip the auto-resolve pass before showing stats.")

    p_sh = subs.add_parser("show", help="Print a single alert's full JSON")
    p_sh.add_argument("index", type=int, help="1-based index of the alert")

    p_re = subs.add_parser("recent", help="Last N alerts as a quick table")
    p_re.add_argument("-n", type=int, default=20)

    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    if args.cmd == "resolve":
        cmd_resolve(args)
    elif args.cmd == "stats":
        cmd_stats(args)
    elif args.cmd == "show":
        cmd_show(args)
    elif args.cmd == "recent":
        cmd_recent(args)


if __name__ == "__main__":
    main()
