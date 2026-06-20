"""IBKR snapshot timeout-safety tests (Phase 1).

Proves the snapshot path is bounded: succeeds when data loads, degrades to a
PARTIAL result on timeout, and never hangs indefinitely. No TWS needed — the
IB connection is faked via monkeypatching `_connect`.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import execution.ibkr_orders as io


def _av(tag, value, ccy="EUR"):
    return types.SimpleNamespace(tag=tag, value=value, currency=ccy)


class _FakeIB:
    """Fake ib_insync IB whose async calls are real coroutines."""
    def __init__(self, *, summary_delay=0.0, pos_delay=0.0, positions=None):
        self.summary_delay = summary_delay
        self.pos_delay = pos_delay
        self._positions = positions or []
        self.disconnected = False

    def managedAccounts(self):
        return ["DUQ834606"]

    async def accountSummaryAsync(self, acct):
        await asyncio.sleep(self.summary_delay)
        return [_av("TotalCashValue", "1000000.00"), _av("NetLiquidation", "1000000.00")]

    async def reqPositionsAsync(self):
        await asyncio.sleep(self.pos_delay)
        return self._positions

    def run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def disconnect(self):
        self.disconnected = True


def _patch(monkeypatch, fake):
    monkeypatch.setattr(io, "_connect", lambda: (fake, None))


def test_snapshot_success_loads_values(monkeypatch):
    fake = _FakeIB()
    _patch(monkeypatch, fake)
    snap = io.IBKRAdapter().snapshot(timeout=2.0)
    assert snap.partial is False
    assert snap.warnings == []
    assert snap.cash == 1000000.0
    assert snap.equity == 1000000.0
    assert snap.currency == "EUR"
    assert snap.positions == []
    assert fake.disconnected is True


def test_snapshot_partial_on_account_value_timeout(monkeypatch):
    fake = _FakeIB(summary_delay=5.0)   # longer than the timeout below
    _patch(monkeypatch, fake)
    snap = io.IBKRAdapter().snapshot(timeout=0.2)
    assert snap.partial is True
    assert any("account values" in w for w in snap.warnings)
    assert snap.cash == 0.0            # safe default, not a hang
    assert snap.positions == []        # positions still loaded


def test_snapshot_never_hangs(monkeypatch):
    # Both calls would block for 10s; bounded to 0.2s each → returns fast.
    fake = _FakeIB(summary_delay=10.0, pos_delay=10.0)
    _patch(monkeypatch, fake)
    start = time.monotonic()
    snap = io.IBKRAdapter().snapshot(timeout=0.2)
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"snapshot took {elapsed:.1f}s — should be bounded"
    assert snap.partial is True
    assert len(snap.warnings) == 2     # both account values AND positions timed out
