"""Safe paper-execution gate tests (Phase 3) + data-status classifier (Phase 2)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from risk.exec_gate import evaluate_execution_gate
from data.ibkr_feed import (
    classify_data_status, DATA_LIVE, DATA_DELAYED, DATA_HISTORICAL_ONLY, DATA_UNAVAILABLE,
)


def _good(**over):
    base = dict(
        account_id="DUQ834606", broker="ibkr", mode="paper", live_account=False,
        snapshot=types.SimpleNamespace(partial=False, positions=[]),
        data_status="LIVE", order_qty=1, symbol="MES",
        kill_switch_present=False, open_positions=0,
    )
    base.update(over)
    return base


# ---- gate: happy path ----
def test_gate_allows_when_all_conditions_met():
    r = evaluate_execution_gate(**_good())
    assert r.allowed is True and r.reasons == [] and bool(r) is True


# ---- gate: each condition blocks ----
def test_gate_blocks_non_paper_account():
    r = evaluate_execution_gate(**_good(account_id="U1234567"))
    assert not r.allowed and any("paper" in x for x in r.reasons)

def test_gate_blocks_wrong_broker():
    assert not evaluate_execution_gate(**_good(broker="tradovate")).allowed

def test_gate_blocks_non_paper_mode():
    assert not evaluate_execution_gate(**_good(mode="live")).allowed

def test_gate_blocks_live_account_flag():
    assert not evaluate_execution_gate(**_good(live_account=True)).allowed

def test_gate_blocks_hard_snapshot_failure():
    r = evaluate_execution_gate(**_good(snapshot=None))
    assert not r.allowed and any("hard-failed" in x for x in r.reasons)

def test_gate_blocks_non_live_data():
    r = evaluate_execution_gate(**_good(data_status="HISTORICAL_ONLY"))
    assert not r.allowed and any("market-data" in x for x in r.reasons)

def test_gate_allows_non_live_data_with_override():
    assert evaluate_execution_gate(**_good(data_status="HISTORICAL_ONLY", data_override=True)).allowed

def test_gate_blocks_qty_over_one():
    assert not evaluate_execution_gate(**_good(order_qty=2)).allowed

def test_gate_blocks_disallowed_symbol():
    assert not evaluate_execution_gate(**_good(symbol="NQ")).allowed

def test_gate_blocks_kill_switch():
    assert not evaluate_execution_gate(**_good(kill_switch_present=True)).allowed

def test_gate_blocks_open_positions():
    assert not evaluate_execution_gate(**_good(open_positions=1)).allowed


# ---- runner handles PARTIAL snapshot safely (Phase 1 test 4) ----
def test_gate_accepts_partial_snapshot():
    """A partial snapshot (not None) must NOT count as a hard failure."""
    partial = types.SimpleNamespace(partial=True, positions=[], warnings=["account values unavailable"])
    r = evaluate_execution_gate(**_good(snapshot=partial))
    assert r.allowed is True
    assert not any("snapshot" in x for x in r.reasons)


# ---- data-status classifier (Phase 2) ----
def test_classify_live_wins():
    assert classify_data_status(True, True, True) == DATA_LIVE

def test_classify_delayed_when_no_live():
    assert classify_data_status(False, True, True) == DATA_DELAYED

def test_classify_historical_only():
    assert classify_data_status(False, False, True, error_354=True) == DATA_HISTORICAL_ONLY

def test_classify_unavailable():
    assert classify_data_status(False, False, False) == DATA_UNAVAILABLE
