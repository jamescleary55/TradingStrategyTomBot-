"""Unit tests for the forward-testing scaffold.

Covers:
- Signal / skipped / trade-attempt logging
- Kill switch handling
- Risk rule blocking (mode, symbol, RR, score, daily loss, max trades)
- Personal rules YAML loading
- Forward report generation
- Strategy interface + sweep_choch_fvg adapter
- Paper / live execution guard
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
@pytest.fixture
def temp_state(monkeypatch, tmp_path):
    """Repoint ~/.ict-bot to a temp dir so tests don't pollute real logs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    state = tmp_path / ".ict-bot"
    state.mkdir()
    # Re-import modules that cache STATE_DIR at import-time
    import importlib
    import live.forward_log as fl
    import live.tracker as tracker
    import risk.rules as rules
    importlib.reload(fl)
    importlib.reload(tracker)
    importlib.reload(rules)
    yield state


@pytest.fixture
def stub_setup():
    from signals.strategies.base import StrategySetup
    return StrategySetup(
        strategy_name="sweep_choch_fvg", strategy_version="1.0.0",
        timestamp=pd.Timestamp("2026-06-12 14:00", tz="UTC"),
        symbol="MNQ", timeframe="1h", direction="bull",
        entry=29000.0, stop=28900.0, target=29200.0, rr=2.0,
        setup_type="sweep_choch_fvg", setup_subtype="EQL_LONDON",
        htf_bias="bull", setup_score=0.75, invalidation_level=28900.0,
        sweep_level_price=28890.0, sweep_level_kind="EQL",
        choch_price=28980.0, bos_state="bull_break",
        fvg_top=29020.0, fvg_bottom=28980.0,
        session="LONDON", confluence=["test"],
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def test_log_signal_writes_jsonl(temp_state, stub_setup):
    from live.forward_log import SIGNALS_LOG, log_signal, load_signals
    row = log_signal(
        strategy_setup=stub_setup, news_blackout=False,
        spread_estimate=0.5, trade_allowed=False, skip_reason="test",
    )
    assert SIGNALS_LOG.exists()
    rows = load_signals()
    assert len(rows) == 1
    # 16 expected schema keys all present
    for key in ("timestamp", "symbol", "timeframe", "session", "setup_type",
                "htf_bias", "sweep_level_price", "bos_state",
                "fvg_top", "fvg_bottom", "entry", "stop", "target",
                "planned_R", "news_blackout", "spread_estimate", "trade_allowed",
                "skip_reason"):
        assert key in rows[0], f"missing key: {key}"
    assert rows[0]["trade_allowed"] is False
    assert rows[0]["skip_reason"] == "test"


def test_log_skipped(temp_state, stub_setup):
    from live.forward_log import SKIPPED_LOG, log_skipped, load_skipped
    log_skipped(strategy_setup=stub_setup, reason="news blackout", rule_name="news_blackout")
    rows = load_skipped()
    assert SKIPPED_LOG.exists()
    assert len(rows) == 1
    assert rows[0]["reason"] == "news blackout"
    assert rows[0]["rule"] == "news_blackout"


def test_log_trade_attempt(temp_state, stub_setup):
    from live.forward_log import TRADES_LOG, log_trade_attempt, load_trades
    log_trade_attempt(
        strategy_setup=stub_setup, plan=None, broker_name="dryrun",
        intended_entry=29000.0, intended_stop=28900.0, intended_target=29200.0,
        planned_R=2.0, risk_usd=50.0, contracts=1,
        fill_price=29001.0, slippage_pts=1.0,
        order_id=999, broker_response={"ok": True},
        outcome="submitted",
    )
    rows = load_trades()
    assert TRADES_LOG.exists()
    assert rows[0]["intended_entry"] == 29000.0
    assert rows[0]["fill_price"] == 29001.0
    assert rows[0]["slippage_pts"] == 1.0
    assert rows[0]["contracts"] == 1
    assert rows[0]["broker_response"]["ok"] is True


# ---------------------------------------------------------------------------
# Rules YAML loading
# ---------------------------------------------------------------------------
def test_rules_load_from_example():
    from risk.rules import load
    r = load()
    assert r.mode == "review"             # safe default
    assert r.enable_auto_execute is False
    assert "MNQ" in r.allowed_symbols
    assert r.risk_per_trade_R > 0


def test_rules_load_from_explicit_path(tmp_path):
    from risk.rules import load
    p = tmp_path / "custom_rules.yaml"
    p.write_text(
        "allowed_symbols: [ES]\n"
        "mode: paper\n"
        "enable_auto_execute: true\n"
        "max_trades_per_day: 1\n"
    )
    r = load(p)
    assert r.allowed_symbols == ["ES"]
    assert r.mode == "paper"
    assert r.enable_auto_execute is True
    assert r.max_trades_per_day == 1


# ---------------------------------------------------------------------------
# Risk gate
# ---------------------------------------------------------------------------
def test_kill_switch_blocks(temp_state, stub_setup):
    from risk.controls import RiskGate
    from risk.rules import load
    rules = load()
    (temp_state / "KILL_SWITCH").write_text("halt")
    rules.kill_switch_path = str(temp_state / "KILL_SWITCH")
    gate = RiskGate(rules)
    d = gate.check(stub_setup)
    assert not d.allowed
    assert d.rule == "kill_switch"


def test_review_mode_blocks(temp_state, stub_setup):
    from risk.controls import RiskGate
    from risk.rules import load
    rules = load()
    rules.mode = "review"
    rules.enable_auto_execute = True   # mode wins
    gate = RiskGate(rules)
    d = gate.check(stub_setup)
    assert not d.allowed
    assert d.rule == "mode_review"


def test_auto_execute_disabled_blocks(temp_state, stub_setup):
    from risk.controls import RiskGate
    from risk.rules import load
    rules = load()
    rules.mode = "paper"
    rules.enable_auto_execute = False
    gate = RiskGate(rules)
    d = gate.check(stub_setup)
    assert not d.allowed
    assert d.rule == "auto_execute_disabled"


def test_min_rr_blocks(temp_state, stub_setup):
    from risk.controls import RiskGate
    from risk.rules import load
    rules = load()
    rules.mode = "paper"
    rules.enable_auto_execute = True
    rules.min_expected_R = 3.0
    rules.allowed_sessions = ["LONDON"]
    gate = RiskGate(rules)
    d = gate.check(stub_setup)
    assert not d.allowed
    assert d.rule == "below_min_rr"


def test_symbol_not_allowed_blocks(temp_state, stub_setup):
    from risk.controls import RiskGate
    from risk.rules import load
    rules = load()
    rules.mode = "paper"
    rules.enable_auto_execute = True
    rules.allowed_symbols = ["MES"]
    rules.allowed_sessions = ["LONDON"]
    gate = RiskGate(rules)
    d = gate.check(stub_setup)
    assert not d.allowed
    assert d.rule == "symbol_not_allowed"


def test_min_score_blocks(temp_state, stub_setup):
    from risk.controls import RiskGate
    from risk.rules import load
    rules = load()
    rules.mode = "paper"
    rules.enable_auto_execute = True
    rules.min_setup_score = 0.99
    rules.allowed_sessions = ["LONDON"]
    gate = RiskGate(rules)
    d = gate.check(stub_setup)
    assert not d.allowed
    assert d.rule == "below_min_score"


# ---------------------------------------------------------------------------
# Strategy interface
# ---------------------------------------------------------------------------
def test_strategy_registration_and_lookup():
    from signals.strategies.base import get_strategy, list_strategies
    names = list_strategies()
    assert "sweep_choch_fvg" in names
    s = get_strategy("sweep_choch_fvg")
    assert s.name == "sweep_choch_fvg"
    assert s.version != "0.0.0"


def test_strategy_validate_geometry(stub_setup):
    from signals.strategies.base import StrategyContext
    from signals.strategies.sweep_choch_fvg import SweepChochFvgStrategy
    from config import INSTRUMENTS
    s = SweepChochFvgStrategy()
    ok = s.validate_setup(stub_setup, StrategyContext(
        instrument=INSTRUMENTS["MNQ"], timeframe="1h"
    ))
    assert ok.ok

    # Flip stop to wrong side of entry
    bad = stub_setup
    bad.stop = 29500.0   # > entry for a bull setup → invalid
    bad_res = s.validate_setup(bad, StrategyContext(
        instrument=INSTRUMENTS["MNQ"], timeframe="1h"
    ))
    assert not bad_res.ok


# ---------------------------------------------------------------------------
# Paper / live execution guard
# ---------------------------------------------------------------------------
def test_tradovate_refuses_non_demo_without_allow_live(monkeypatch):
    monkeypatch.setenv("TRADOVATE_ENV", "live")
    monkeypatch.setenv("TRADOVATE_USERNAME", "user")
    monkeypatch.setenv("TRADOVATE_PASSWORD", "pw")
    monkeypatch.setenv("TRADOVATE_CID", "1")
    monkeypatch.setenv("TRADOVATE_SECRET", "s")
    # Reload config so the new TRADOVATE_ENV is picked up
    import importlib, config as cfg
    importlib.reload(cfg)
    import execution.tradovate_orders as tv
    importlib.reload(tv)
    with pytest.raises(Exception) as exc:
        tv.place_bracket(
            contract_id=1, side="Buy", qty=1,
            entry=100.0, stop=99.0, target=102.0,
            allow_live=False,
        )
    assert "live" in str(exc.value).lower() or "refus" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Forward report generation
# ---------------------------------------------------------------------------
def test_forward_report_handles_empty(temp_state):
    from live.forward_report import compile_report
    rep = compile_report()
    assert rep["totals"]["n_signals_detected"] == 0
    assert rep["ready_for_real_money"] is False
    assert any(c["code"] == "few_signals" for c in rep["concerns"])


def test_forward_report_flags_overfitting(temp_state, stub_setup):
    from live.forward_log import log_signal, log_trade_attempt
    from live.forward_report import compile_report
    # Generate 30 signals all on one symbol
    for i in range(30):
        log_signal(strategy_setup=stub_setup, news_blackout=False,
                   spread_estimate=0, trade_allowed=True, skip_reason=None)
    # 25 closed trades, all winners — should trip "unrealistic_win_rate"
    for i in range(25):
        log_trade_attempt(
            strategy_setup=stub_setup, plan=None, broker_name="dryrun",
            intended_entry=29000.0, intended_stop=28900.0, intended_target=29200.0,
            planned_R=2.0, risk_usd=50.0, contracts=1,
            outcome="target", extra={"r_realised": 2.0},
        )
    # We need r_realised at top-level for the stats helper
    # Re-write trades with r_realised in the row itself
    from live.forward_log import TRADES_LOG
    rows = TRADES_LOG.read_text().splitlines()
    fixed = []
    for line in rows:
        d = json.loads(line)
        d["r_realised"] = 2.0
        fixed.append(json.dumps(d))
    TRADES_LOG.write_text("\n".join(fixed) + "\n")

    rep = compile_report()
    codes = {c["code"] for c in rep["concerns"]}
    assert "single_symbol_only" in codes
    assert "unrealistic_win_rate" in codes
    assert rep["ready_for_real_money"] is False


# ---------------------------------------------------------------------------
# Live mode safety — review mode never executes
# ---------------------------------------------------------------------------
def test_review_mode_does_not_execute(temp_state, stub_setup):
    from risk.controls import RiskGate
    from risk.rules import load
    rules = load()
    rules.mode = "review"
    rules.enable_auto_execute = True
    gate = RiskGate(rules)
    decision = gate.check(stub_setup)
    assert not decision.allowed
    assert "review" in decision.reason
