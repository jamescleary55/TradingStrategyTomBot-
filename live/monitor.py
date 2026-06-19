"""Live polling setup detector (multi-symbol).

One thread per symbol, all sharing a single :class:`utils.alerter.Alerter`.
Each thread runs its own polling loop at the configured cadence and keeps
state independently:

    ~/.ict-bot/monitor-<symbol>-<timeframe>.json     (per-symbol high-water mark)
    ~/.ict-bot/alerts.jsonl                          (shared alert log; tracker uses it)

Examples:
    python -m live.monitor --symbols MNQ --timeframe 1h --poll 60
    python -m live.monitor --symbols MNQ,MES,MCL --timeframe 15m --htf 1h --news-filter
    python -m live.monitor --symbols MNQ --auto-execute --equity 50000 --risk-pct 0.015
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg
from data.loader import load_bars
from execution.base import get_adapter
from live.forward_log import log_signal, log_skipped, log_trade_attempt
from risk.controls import RiskGate
from risk.rules import PersonalRules, load as load_rules
from risk.sizing import plan_trade
from signals.htf_bias import compute_bias_series, htf_timeframe_for
from signals.setup import find_setups
from signals.strategies.base import StrategyContext, get_strategy
from utils.alerter import Alerter
from utils.news import filter_setups as filter_setups_news, generate_events, is_in_blackout, generate_events as gen_news_events

log = logging.getLogger("live.monitor")
STATE_DIR = Path.home() / ".ict-bot"
STATE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
def _state_path(symbol: str, tf: str) -> Path:
    return STATE_DIR / f"monitor-{symbol}-{tf}.json"


def _load_state(symbol: str, tf: str) -> dict:
    p = _state_path(symbol, tf)
    if not p.exists():
        return {"last_choch_ts": None, "n_alerts": 0}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"last_choch_ts": None, "n_alerts": 0}


def _save_state(symbol: str, tf: str, state: dict) -> None:
    _state_path(symbol, tf).write_text(json.dumps(state, indent=2))


# Legacy alerts.jsonl writer was removed; everything now flows through
# live/forward_log.py (live_signals.jsonl / skipped_setups.jsonl /
# live_trades.jsonl). The tracker module is kept for migrating older
# data, but the monitor itself no longer writes to alerts.jsonl.


# ---------------------------------------------------------------------------
@dataclass
class WatchSpec:
    """Per-symbol configuration for one watch thread."""
    symbol: str
    sim_symbol: str
    timeframe: str
    days: int
    poll: int
    source: str
    htf: Optional[str]
    no_htf: bool
    htf_strict: bool
    news_filter: bool
    news_pad: int
    auto_execute: bool
    equity: float
    risk_pct: float
    allow_live: bool
    execute_dry_run: bool
    mode: str = "review"                  # review | paper | live
    strategy_name: str = "sweep_choch_fvg"
    rules: Optional[PersonalRules] = None


def _tick(spec: WatchSpec, alerter: Alerter, state: dict,
          risk_gate: Optional[RiskGate] = None) -> int:
    """One detection tick for a single symbol.

    Routes every setup through:
    detect (strategy) → validate (strategy) → news check → risk gate →
    log (signal + skipped where applicable) → alert → execute (only in
    paper/live modes, never in review).
    """
    df = load_bars(spec.symbol, spec.timeframe, days=spec.days, source=spec.source)
    if df.empty:
        log.warning("[%s] empty data from %s; retry next tick", spec.symbol, spec.source)
        return 0

    htf_bias = None
    htf_tf_used = None
    if not spec.no_htf:
        htf_tf = spec.htf or htf_timeframe_for(spec.timeframe)
        if htf_tf != spec.timeframe:
            df_htf = load_bars(spec.symbol, htf_tf, days=spec.days, source=spec.source)
            if not df_htf.empty:
                htf_bias = compute_bias_series(df, df_htf)
                htf_tf_used = htf_tf

    instrument = cfg.INSTRUMENTS.get(spec.sim_symbol) or cfg.INSTRUMENTS.get("MNQ")
    strategy = get_strategy(spec.strategy_name)
    context = StrategyContext(
        instrument=instrument,
        timeframe=spec.timeframe,
        htf_bias_series=htf_bias,
        htf_timeframe=htf_tf_used,
    )

    # ---- detect via strategy ------------------------------------
    str_setups = strategy.detect_setups(df, context)
    if not str_setups:
        return 0

    # ---- precompute news events once per tick -------------------
    news_events = []
    if spec.news_filter:
        try:
            news_events = gen_news_events(
                df.index[0].to_pydatetime().replace(tzinfo=None),
                df.index[-1].to_pydatetime().replace(tzinfo=None),
            )
        except Exception:
            news_events = []

    # ---- dedup against high-water mark --------------------------
    last_ts_iso = state.get("last_choch_ts")
    last_ts = None
    if last_ts_iso:
        try:
            last_ts = dt.datetime.fromisoformat(last_ts_iso)
        except ValueError:
            last_ts = None
    new_setups = [s for s in str_setups
                  if last_ts is None or s.timestamp.to_pydatetime() > last_ts]
    if not new_setups:
        return 0

    n_alerts = 0
    for s in new_setups:
        # Validation (geometry, etc.)
        val = strategy.validate_setup(s, context)
        if not val.ok:
            log_skipped(strategy_setup=s, reason=val.reason, rule_name="strategy_validate")
            continue

        # News blackout
        news_blackout = False
        if news_events:
            hit, _ev = is_in_blackout(s.timestamp, news_events,
                                       minutes_before=spec.news_pad,
                                       minutes_after=spec.news_pad)
            news_blackout = bool(hit)

        # Risk-gate decision (only meaningful when auto-executing; review always blocks below)
        decision = risk_gate.check(s, news_blackout=news_blackout) if risk_gate else None
        trade_allowed = bool(decision and decision.allowed) if decision else False

        # ALWAYS log the signal (16 fields). trade_allowed/skip_reason captured.
        skip_reason = (decision.reason
                       if (decision is not None and not decision.allowed)
                       else None)
        log_signal(
            strategy_setup=s,
            news_blackout=news_blackout,
            spread_estimate=0.0,                # TODO: derive from L1 quote when on Tradovate WS
            trade_allowed=trade_allowed,
            skip_reason=skip_reason,
        )
        if decision is not None and not decision.allowed:
            log_skipped(strategy_setup=s, reason=decision.reason, rule_name=decision.rule)

        # Alert in every mode — operator wants visibility even when blocked
        suffix = ""
        if spec.mode == "review":
            suffix = "  (mode=review — approve manually in broker, do not auto-execute)"
        alerter.notify_setup(s.native, instrument, sim_symbol=spec.sim_symbol, df=df)
        if suffix:
            alerter.notify("Manual approval required",
                           f"{spec.symbol} {s.direction.upper()} "
                           f"@ {s.entry:.2f}, SL {s.stop:.2f}, TP {s.target:.2f} "
                           f"(invalidates at {s.invalidation_level:.2f}){suffix}",
                           severity="info")
        n_alerts += 1

        # Execute only when mode != review AND auto-execute is on AND risk gate passed
        if spec.mode != "review" and spec.auto_execute and trade_allowed:
            try:
                plan = strategy.build_trade_plan(s, equity=spec.equity,
                                                  risk_pct=spec.risk_pct, min_rr=1.0)
                if not plan.approved:
                    log_skipped(strategy_setup=s, reason=plan.reason, rule_name="sizing")
                    alerter.notify(f"Order skipped ({spec.symbol})",
                                   f"sizing: {plan.reason}", severity="warning")
                    continue
                adapter = get_adapter()           # BROKER env → ibkr | tradovate | topstepx | dryrun
                result = adapter.place_bracket_for_setup(
                    s.native, plan, instrument,
                    allow_live=spec.allow_live, dry_run=spec.execute_dry_run,
                )
                log_trade_attempt(
                    strategy_setup=s, plan=plan, broker_name=adapter.name,
                    intended_entry=s.entry, intended_stop=s.stop,
                    intended_target=s.target, planned_R=s.rr,
                    risk_usd=plan.total_risk_usd, contracts=plan.contracts,
                    order_id=result.order_id, broker_response=result.raw_response,
                    outcome="submitted",
                )
                alerter.notify(f"Order placed ({spec.symbol})",
                               f"Bracket #{result.order_id} qty {plan.contracts}",
                               severity="success")
            except Exception as ex:
                import os as _os
                log_trade_attempt(
                    strategy_setup=s, plan=None,
                    broker_name=_os.getenv("BROKER", "ibkr").strip().lower(),
                    intended_entry=s.entry, intended_stop=s.stop,
                    intended_target=s.target, planned_R=s.rr,
                    risk_usd=0.0, contracts=0, outcome="failed", error=str(ex),
                )
                alerter.notify(f"Order failed ({spec.symbol})", str(ex), severity="error")

    state["last_choch_ts"] = new_setups[-1].timestamp.isoformat()
    state["n_alerts"] = state.get("n_alerts", 0) + n_alerts
    _save_state(spec.symbol, spec.timeframe, state)
    return n_alerts


# ---------------------------------------------------------------------------
_should_stop = threading.Event()


def _handle_signal(signum, frame):
    _should_stop.set()


def _watch_loop(spec: WatchSpec, alerter: Alerter, state: dict,
                risk_gate: Optional[RiskGate] = None) -> None:
    tick_no = 0
    while not _should_stop.is_set():
        tick_no += 1
        try:
            n = _tick(spec, alerter, state, risk_gate)
            log.info("[%s] tick #%d: %d new alert(s) (total %d)",
                     spec.symbol, tick_no, n, state.get("n_alerts", 0))
        except Exception as e:
            log.exception("[%s] tick #%d failed: %s", spec.symbol, tick_no, e)
        # Sleep in small slices so SIGINT is responsive
        for _ in range(spec.poll):
            if _should_stop.is_set():
                break
            time.sleep(1)


# ---------------------------------------------------------------------------
def _resolve_symbols(arg: str) -> List[tuple[str, str]]:
    """Return list of (data_symbol, sim_symbol) for each requested asset.

    Accepts a comma-separated list. ``NQ`` maps to (NQ, MNQ) by default so
    the existing equity-sized sim still works.
    """
    pairs: list[tuple[str, str]] = []
    for raw in arg.split(","):
        s = raw.strip().upper()
        if not s:
            continue
        if s in ("NQ",):
            pairs.append((s, "MNQ"))
        elif s in ("ES",):
            pairs.append((s, "MES"))
        elif s in ("GC",):
            pairs.append((s, "MGC"))
        elif s in ("CL",):
            pairs.append((s, "MCL"))
        else:
            pairs.append((s, s))   # already a sim symbol (MNQ/MES/MCL/MGC)
    return pairs


def main():
    parser = argparse.ArgumentParser(description="Live multi-symbol ICT setup monitor")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated list, e.g. MNQ,MES,MCL. "
                             "If omitted, falls back to --symbol.")
    parser.add_argument("--symbol", default=cfg.DEFAULT_SYMBOL,
                        help="Single symbol (legacy; ignored if --symbols set).")
    parser.add_argument("--timeframe", default=cfg.DEFAULT_TIMEFRAME)
    parser.add_argument("--days", type=int, default=14,
                        help="History pulled each tick (kept short for speed)")
    parser.add_argument("--poll", type=int, default=60, help="Seconds between polls")
    parser.add_argument("--source", default="yfinance",
                        choices=["auto", "tradovate", "yfinance", "synthetic"])
    parser.add_argument("--htf", default=None)
    parser.add_argument("--no-htf", action="store_true")
    parser.add_argument("--htf-strict", action="store_true")
    parser.add_argument("--news-filter", action="store_true")
    parser.add_argument("--news-pad", type=int, default=30)
    parser.add_argument("--entry-mode", default="closer_edge",
                        choices=["mid", "closer_edge", "farther_edge"])
    parser.add_argument("--reset", action="store_true",
                        help="Clear state for the requested symbols (re-alert from current data)")
    parser.add_argument("--test-alert", action="store_true",
                        help="Send a one-shot test alert and exit")
    parser.add_argument("--once", action="store_true",
                        help="Run a single tick per symbol and exit (smoke test)")
    # Auto-execute (opt-in; paper-only unless --allow-live)
    parser.add_argument("--auto-execute", action="store_true",
                        help="Place an OSO bracket order via Tradovate on every new setup.")
    parser.add_argument("--equity", type=float, default=10_000.0,
                        help="Account equity used for sizing under --auto-execute.")
    parser.add_argument("--risk-pct", type=float, default=cfg.RISK.max_risk_per_trade_pct)
    parser.add_argument("--allow-live", action="store_true",
                        help="Allow orders when TRADOVATE_ENV != demo. USE WITH CARE.")
    parser.add_argument("--execute-dry-run", action="store_true",
                        help="Build the order body and log it, but don't send.")
    parser.add_argument("--mode", default=None,
                        choices=["review", "paper", "live"],
                        help="review = alert only (no orders); paper = demo; live = real money. "
                             "Default: read from personal_rules.yaml (defaults to 'review').")
    parser.add_argument("--strategy", default="sweep_choch_fvg",
                        help="Strategy name from signals/strategies/.")
    parser.add_argument("--rules-file", default=None,
                        help="Path to a personal_rules.yaml override.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg.SETUP_ENTRY_MODE = args.entry_mode

    alerter = Alerter()
    if args.test_alert:
        alerter.test()
        return

    # Load personal rules — operator's safety net
    from pathlib import Path as _Path
    rules = load_rules(_Path(args.rules_file)) if args.rules_file else load_rules()
    mode = (args.mode or rules.mode).lower()
    log.info("Personal rules loaded from %s · mode=%s · auto_execute=%s",
             rules.source, mode, rules.enable_auto_execute)
    risk_gate = RiskGate(rules=rules)
    if rules.kill_switch.exists():
        log.warning("[KILL_SWITCH] %s exists — new trades will be blocked", rules.kill_switch)

    pairs = _resolve_symbols(args.symbols or args.symbol)
    if not pairs:
        log.error("No symbols resolved from %r", args.symbols or args.symbol)
        sys.exit(1)

    specs: list[WatchSpec] = []
    for sym, sim_sym in pairs:
        if args.reset:
            _save_state(sym, args.timeframe, {"last_choch_ts": None, "n_alerts": 0})
            log.info("[%s] state cleared", sym)
        state_now = _load_state(sym, args.timeframe)
        log.info("[%s → %s] watching %s every %ds (high-water = %s, total alerts = %d)",
                 sym, sim_sym, args.timeframe, args.poll,
                 state_now.get("last_choch_ts") or "—", state_now.get("n_alerts", 0))
        specs.append(WatchSpec(
            symbol=sym, sim_symbol=sim_sym, timeframe=args.timeframe,
            days=args.days, poll=args.poll, source=args.source,
            htf=args.htf, no_htf=args.no_htf, htf_strict=args.htf_strict,
            news_filter=args.news_filter, news_pad=args.news_pad,
            auto_execute=args.auto_execute, equity=args.equity, risk_pct=args.risk_pct,
            allow_live=args.allow_live, execute_dry_run=args.execute_dry_run,
            mode=mode, strategy_name=args.strategy, rules=rules,
        ))

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if args.once:
        total = 0
        for spec in specs:
            state = _load_state(spec.symbol, spec.timeframe)
            n = _tick(spec, alerter, state, risk_gate)
            log.info("[%s] one-shot: %d new alert(s) (total %d)",
                     spec.symbol, n, state.get("n_alerts", 0))
            total += n
        log.info("Combined one-shot: %d new alert(s) across %d symbol(s)", total, len(specs))
        return

    # One thread per symbol, sharing the alerter (rich Console is thread-safe)
    threads: list[threading.Thread] = []
    for spec in specs:
        state = _load_state(spec.symbol, spec.timeframe)
        t = threading.Thread(target=_watch_loop,
                             args=(spec, alerter, state, risk_gate),
                             name=f"watch-{spec.symbol}", daemon=True)
        t.start()
        threads.append(t)

    while not _should_stop.is_set():
        time.sleep(1)
    log.info("Stopping... draining %d watcher(s).", len(threads))
    for t in threads:
        t.join(timeout=3)
    log.info("Stopped (Ctrl-C). Bye.")


if __name__ == "__main__":
    main()
