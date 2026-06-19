"""End-to-end pipeline runner.

Loads bars (Tradovate if creds set, synthetic fallback otherwise), runs every
detector, walk-forwards the setups, prints a tight rich-formatted summary and
writes a self-contained HTML report + annotated chart (+ optional trade CSV).

Run from project root:
    python -m backtest.run                          # NQ 15m, last 30 days
    python -m backtest.run --symbol MNQ --days 7
    python -m backtest.run --equity 50000 --html    # open report.html when done
"""
from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.patches as patches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DEFAULT_SYMBOL, DEFAULT_TIMEFRAME, INSTRUMENTS, RISK
from backtest.simulator import simulate
from data.loader import load_bars
from engine.liquidity import map_all_liquidity
from engine.market_structure import summary as ms_summary
from signals.fvg import summary as fvg_summary
from signals.htf_bias import compute_bias_series, htf_timeframe_for
from signals.setup import summary as setup_summary
from utils.news import filter_setups as filter_setups_news, generate_events
from utils.report import print_run, write_html_report, write_trade_csv

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("backtest")


# ---------------------------------------------------------------------------
def _draw_candles(ax, df_view, width_frac: float = 0.7):
    """Render OHLC candlesticks. width_frac is fraction of bar spacing."""
    if len(df_view) == 0:
        return
    times = df_view.index
    bar_seconds = (times[1] - times[0]).total_seconds() if len(times) > 1 else 60
    w_days = (bar_seconds / 86400.0) * width_frac
    bull_color = "#22c55e"
    bear_color = "#f87171"
    for t, o, h, l, c in zip(times, df_view["open"], df_view["high"], df_view["low"], df_view["close"]):
        is_bull = c >= o
        color = bull_color if is_bull else bear_color
        x = mdates.date2num(t)
        # Wick
        ax.add_line(plt.Line2D([x, x], [l, h], color=color, linewidth=0.7, alpha=0.85, zorder=2))
        # Body
        body_low = min(o, c); body_high = max(o, c)
        height = max(body_high - body_low, (h - l) * 0.001)
        rect = patches.Rectangle(
            (x - w_days / 2, body_low), w_days, height,
            facecolor=color if is_bull else "#0a0f0d",
            edgecolor=color, linewidth=0.7, alpha=0.85, zorder=3,
        )
        ax.add_patch(rect)


def render_chart(df, swings, bos, choch, fvgs, sweeps, setups,
                 equity_curve, starting_equity, out_path: Path,
                 visible_bars: int = 250, show_all_fvgs: bool = False) -> Path:
    """Annotated price + equity chart.

    To keep the picture readable when the dataset is large, only the most
    recent ``visible_bars`` are rendered on the price chart by default, and:

    - filled FVGs are hidden (you only see what's still in play),
    - swings shown are limited to those that were *broken* by a BOS/CHoCH
      visible in the window,
    - sweeps & structure events are clipped to the visible window.

    Pass ``show_all_fvgs=True`` to render every gap (busy but complete).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ---- Clip the visible window ---------------------------------------
    if visible_bars and len(df) > visible_bars:
        df_view = df.iloc[-visible_bars:]
    else:
        df_view = df
    win_start = df_view.index[0]
    win_end = df_view.index[-1]

    def in_window(ts) -> bool:
        return win_start <= ts <= win_end

    # FVGs: hide filled (unless asked otherwise), clip to window
    if show_all_fvgs:
        fvgs_vis = [g for g in fvgs if in_window(df.index[g.idx])]
    else:
        fvgs_vis = [g for g in fvgs if not g.filled and in_window(df.index[g.idx])]

    # Structure events in window
    bos_vis = [e for e in bos if in_window(e.timestamp)]
    choch_vis = [e for e in choch if in_window(e.timestamp)]
    sweeps_vis = [s for s in sweeps if in_window(s.timestamp)]
    setups_vis = [s for s in setups if in_window(s.timestamp)]

    # Swings: only those that were broken by a visible BOS/CHoCH
    broken_swing_ids = {id(e.broken_swing) for e in bos_vis + choch_vis}
    swings_vis = [s for s in swings if id(s) in broken_swing_ids]

    # ---- Figure scaffold ----------------------------------------------
    has_equity = equity_curve is not None and len(equity_curve) > 0
    if has_equity:
        fig, (ax, ax_eq) = plt.subplots(
            2, 1, figsize=(16, 10),
            gridspec_kw={"height_ratios": [3, 1], "hspace": 0.22},
            constrained_layout=True,
        )
    else:
        fig, ax = plt.subplots(figsize=(16, 8), constrained_layout=True)
        ax_eq = None
    ax.set_facecolor("#0a0f0d")
    fig.patch.set_facecolor("#07100d")

    # ---- Day separators (light vertical lines at midnight UTC) ---------
    days = pd.date_range(df_view.index[0].normalize(), df_view.index[-1].normalize(), freq="D")
    for d in days:
        ax.axvline(d, color="#1f3329", linewidth=0.5, alpha=0.4, zorder=1)

    # ---- Candles --------------------------------------------------------
    _draw_candles(ax, df_view)

    # ---- Open FVG rectangles -------------------------------------------
    for g in fvgs_vis:
        x_start = df.index[g.idx]
        end_idx = g.filled_idx if (g.filled and g.filled_idx is not None) else len(df) - 1
        x_end = df.index[end_idx]
        color = "#22c55e" if g.direction == "bull" else "#f87171"
        alpha = 0.12 if not g.filled else 0.05
        rect = patches.Rectangle(
            (mdates.date2num(x_start), g.bottom),
            mdates.date2num(x_end) - mdates.date2num(x_start),
            g.top - g.bottom,
            facecolor=color, edgecolor="none", alpha=alpha, zorder=1,
        )
        ax.add_patch(rect)

    # ---- Sweep markers (only those that mattered) ----------------------
    if sweeps_vis:
        ax.scatter([s.timestamp for s in sweeps_vis], [s.wick_extreme for s in sweeps_vis],
                   marker="*", color="#67e8f9", s=55, zorder=5, alpha=0.9,
                   edgecolor="#07100d", linewidth=0.5, label="Sweep")

    # ---- Broken swings only --------------------------------------------
    sh = [s for s in swings_vis if s.side == "high"]
    sl = [s for s in swings_vis if s.side == "low"]
    if sh:
        ax.scatter([s.timestamp for s in sh], [s.price for s in sh],
                   marker="v", color="#f87171", s=22, zorder=4, alpha=0.7,
                   label="Broken swing H")
    if sl:
        ax.scatter([s.timestamp for s in sl], [s.price for s in sl],
                   marker="^", color="#22c55e", s=22, zorder=4, alpha=0.7,
                   label="Broken swing L")

    # ---- BOS / CHoCH ---------------------------------------------------
    if bos_vis:
        ax.scatter([e.timestamp for e in bos_vis], [e.price for e in bos_vis],
                   marker="x", color="#fbbf24", s=32, linewidth=1.0, zorder=5, label="BOS")
    if choch_vis:
        ax.scatter([e.timestamp for e in choch_vis], [e.price for e in choch_vis],
                   marker="D", facecolor="none", edgecolor="#c084fc",
                   s=55, linewidth=1.3, zorder=6, label="CHoCH")

    # ---- Setups (the highlight of the chart) ---------------------------
    setup_label_done = False
    for s in setups_vis:
        color = "#22c55e" if s.direction == "bull" else "#f87171"
        marker = "^" if s.direction == "bull" else "v"
        ax.scatter([s.timestamp], [s.entry], marker=marker, s=180,
                   facecolor=color, edgecolor="white", linewidth=1.4, zorder=8,
                   label=("Setup" if not setup_label_done else None))
        setup_label_done = True
        ax.hlines(s.stop,   s.timestamp, win_end, colors="#f87171",
                  linestyles=(0, (3, 3)), linewidth=0.9, alpha=0.7, zorder=4)
        ax.hlines(s.target, s.timestamp, win_end, colors="#22c55e",
                  linestyles=(0, (3, 3)), linewidth=0.9, alpha=0.7, zorder=4)
        ax.annotate(f"{s.direction.upper()}  R={s.rr:.1f}",
                    xy=(s.timestamp, s.entry), xytext=(6, 8), textcoords="offset points",
                    color=color, fontsize=9, fontweight="bold", zorder=9)

    # ---- Axes formatting -----------------------------------------------
    visible_note = f"  (last {len(df_view)} of {len(df)} bars)" if len(df_view) < len(df) else ""
    ax.set_title(
        f"{df_view.index[0].strftime('%Y-%m-%d %H:%M')}  →  "
        f"{df_view.index[-1].strftime('%Y-%m-%d %H:%M')}{visible_note}",
        color="#ecf6f0", fontsize=11, pad=10, loc="left",
    )
    ax.set_ylabel("Price", color="#7c9287")
    ax.grid(True, axis="y", color="#1f3329", linestyle="--", linewidth=0.5, alpha=0.5)
    ax.tick_params(colors="#7c9287")
    ax.set_xlim(win_start, win_end)
    for spine in ax.spines.values():
        spine.set_color("#1f3329")
    # Tidy x-ticks: daily, MM-DD
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    leg = ax.legend(loc="upper left", facecolor="#0f1a16", edgecolor="#1f3329",
                    labelcolor="#ecf6f0", fontsize=9, ncol=4)
    leg.get_frame().set_alpha(0.85)

    # ---- Equity curve subplot (full range stays informative) -----------
    if has_equity and ax_eq is not None:
        ax_eq.set_facecolor("#0a0f0d")
        eq_color = "#22c55e" if equity_curve.iloc[-1] >= starting_equity else "#f87171"
        ax_eq.plot(equity_curve.index, equity_curve.values, color=eq_color, linewidth=1.4)
        ax_eq.fill_between(equity_curve.index, starting_equity, equity_curve.values,
                           where=equity_curve.values >= starting_equity, color="#22c55e", alpha=0.15)
        ax_eq.fill_between(equity_curve.index, starting_equity, equity_curve.values,
                           where=equity_curve.values < starting_equity, color="#f87171", alpha=0.15)
        ax_eq.axhline(starting_equity, color="#7c9287", linestyle="--", linewidth=0.7, alpha=0.6)
        # Mark the visible window on equity curve
        ax_eq.axvspan(win_start, win_end, color="#22c55e", alpha=0.04, zorder=0)
        ax_eq.set_ylabel("Equity ($)", color="#7c9287")
        ax_eq.tick_params(colors="#7c9287")
        ax_eq.grid(True, axis="y", color="#1f3329", linestyle="--", linewidth=0.5, alpha=0.5)
        for spine in ax_eq.spines.values():
            spine.set_color("#1f3329")
        ax_eq.set_title(
            f"Equity curve  ${starting_equity:,.0f} → ${equity_curve.iloc[-1]:,.0f}"
            f"     (shaded band = price-chart window)",
            color="#ecf6f0", fontsize=10, pad=8, loc="left",
        )

    fig.savefig(out_path, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ICT pipeline + walk-forward backtest")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--equity", type=float, default=10_000.0,
                        help="Account equity used for sizing + simulation")
    parser.add_argument("--sim-symbol", default=None,
                        help="Override the instrument used for the simulation (defaults to MNQ for NQ)")
    parser.add_argument("--html", action="store_true", help="Write + open self-contained HTML report")
    parser.add_argument("--ledger", action="store_true", help="Write trades.csv ledger next to the chart")
    parser.add_argument("--out-dir", default="charts", help="Output directory")
    parser.add_argument("--source", default="auto",
                        choices=["auto", "ibkr", "tradovate", "yfinance", "synthetic", "local"],
                        help="OHLCV data source. 'local' = checked-in market_data/ CSVs "
                             "(incl. James's 2-yr crypto/equity history). "
                             "auto = ibkr/tradovate if configured else yfinance.")
    parser.add_argument("--bars", type=int, default=250,
                        help="Visible bars on the price chart (last N). Use 0 for all.")
    parser.add_argument("--show-all-fvgs", action="store_true",
                        help="Render filled FVGs too (busier chart, complete history).")
    parser.add_argument("--htf", default=None,
                        help="HTF timeframe for bias filter (e.g. 1h, 4h, 1d). Default = one level up.")
    parser.add_argument("--no-htf", action="store_true", help="Disable HTF bias entirely.")
    parser.add_argument("--htf-strict", action="store_true",
                        help="Require setup direction to match HTF bias (rejects countertrends).")
    parser.add_argument("--news-filter", action="store_true",
                        help="Drop setups within ±30 min of NFP / CPI / FOMC.")
    parser.add_argument("--news-pad", type=int, default=30, help="News blackout padding minutes.")
    parser.add_argument("--risk-pct", type=float, default=None,
                        help="Override risk per trade (e.g. 0.01 = 1%%). Defaults to config RISK.")
    parser.add_argument("--entry-mode", default=None, choices=["mid", "closer_edge", "farther_edge"],
                        help="Where in the FVG to place the limit entry. 'closer_edge' = tighter stop.")
    parser.add_argument("--max-stop-pts", type=float, default=None,
                        help="Reject setups whose stop distance exceeds N price points (0 = disabled).")
    args = parser.parse_args()

    # Apply CLI overrides on the config module (read at call time everywhere)
    import config as _cfg
    if args.entry_mode is not None:
        _cfg.SETUP_ENTRY_MODE = args.entry_mode
    if args.max_stop_pts is not None:
        _cfg.SETUP_MAX_STOP_POINTS = args.max_stop_pts
    risk_pct = args.risk_pct if args.risk_pct is not None else RISK.max_risk_per_trade_pct

    df = load_bars(args.symbol, args.timeframe, days=args.days, source=args.source)
    if df.empty:
        log.error("No bars returned"); sys.exit(1)

    ms = ms_summary(df)
    liq = map_all_liquidity(df)
    fvg = fvg_summary(df)

    # ---- HTF bias series ------------------------------------------------
    htf_bias = None
    if not args.no_htf:
        htf_tf = args.htf or htf_timeframe_for(args.timeframe)
        if htf_tf != args.timeframe:
            df_htf = load_bars(args.symbol, htf_tf, days=args.days, source=args.source)
            if not df_htf.empty:
                htf_bias = compute_bias_series(df, df_htf)
                bias_counts = htf_bias.value_counts(dropna=False).to_dict()
                log.info("HTF %s bias distribution: %s", htf_tf, bias_counts)

    setups = setup_summary(df, htf_bias_series=htf_bias, require_htf_alignment=args.htf_strict)

    # ---- News blackout filter ------------------------------------------
    blocked_by_news: list = []
    if args.news_filter and setups["setups"]:
        events = generate_events(df.index[0].to_pydatetime().replace(tzinfo=None),
                                 df.index[-1].to_pydatetime().replace(tzinfo=None))
        kept, blocked = filter_setups_news(setups["setups"], events,
                                           minutes_before=args.news_pad,
                                           minutes_after=args.news_pad)
        blocked_by_news = blocked
        setups = {
            "setups": kept,
            "counts": {
                "total": len(kept),
                "bull":  sum(1 for s in kept if s.direction == "bull"),
                "bear":  sum(1 for s in kept if s.direction == "bear"),
                "aligned_with_bias": sum(1 for s in kept if s.bias == s.direction),
            },
        }

    sim_symbol = args.sim_symbol or ("MNQ" if args.symbol == "NQ" else args.symbol)
    if sim_symbol not in INSTRUMENTS:
        log.error("Unknown sim symbol %s; choose from %s", sim_symbol, list(INSTRUMENTS))
        sys.exit(1)
    sim = simulate(
        df=df, setups=setups["setups"],
        starting_equity=args.equity,
        instrument_symbol=sim_symbol,
        risk_pct=risk_pct,
        min_rr=1.0,
    )

    print_run(df, ms, liq, fvg, setups, sim, INSTRUMENTS[sim_symbol], args.equity)
    if blocked_by_news:
        from rich.console import Console
        Console().print(f"[yellow]News filter:[/yellow] dropped {len(blocked_by_news)} setup(s) "
                        f"in ±{args.news_pad} min of high-impact events")

    out_dir = Path(args.out_dir)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    chart_path = out_dir / f"{args.symbol}-{args.timeframe}-{ts}.png"
    render_chart(df, ms["swings"], ms["bos"], ms["choch"], fvg["fvgs"], liq["sweeps"],
                 setups["setups"], sim.equity_curve, sim.starting_equity, chart_path,
                 visible_bars=args.bars if args.bars > 0 else None,
                 show_all_fvgs=args.show_all_fvgs)
    print(f"  chart  →  {chart_path}")

    if args.ledger:
        csv_path = out_dir / f"{args.symbol}-{args.timeframe}-{ts}-trades.csv"
        write_trade_csv(sim, csv_path)
        print(f"  csv    →  {csv_path}")

    if args.html:
        html_path = out_dir / f"{args.symbol}-{args.timeframe}-{ts}.html"
        write_html_report(
            df=df, ms=ms, liq=liq, fvg=fvg,
            setups_dict=setups, sim=sim,
            chart_png_path=chart_path,
            instrument=INSTRUMENTS[sim_symbol],
            timeframe=args.timeframe,
            risk_pct=RISK.max_risk_per_trade_pct,
            out_path=html_path,
        )
        print(f"  html   →  {html_path}")
        try:
            webbrowser.open(f"file://{html_path.resolve()}")
        except Exception:
            pass
    print()


if __name__ == "__main__":
    main()
