"""IBKR migration tests — data feed + order adapter.

No TWS/IB Gateway needed. Validation paths run before any connection, and the
happy-path order/feed flows use a fake ``ib_async`` injected into sys.modules so
we exercise the real adapter/feed code without the IBKR library installed.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as cfg
from execution.base import get_adapter
from execution.ibkr_orders import IBKRAdapter, IBKROrderError

MNQ = cfg.INSTRUMENTS["MNQ"]


# ---------------------------------------------------------------------------
# Fake ib_async / ib_insync library
# ---------------------------------------------------------------------------
class _Order:
    def __init__(self, oid):
        self.orderId = oid
        self.account = None


class _OrderStatus:
    def __init__(self, status):
        self.status = status


class _Trade:
    def __init__(self, order, status):
        self.order = order
        self.orderStatus = _OrderStatus(status)


class _FakeIB:
    """Records calls so tests can assert what was sent to IBKR."""
    last_bracket = None

    def __init__(self):
        self._hist_calls = 0

    # --- connection ---
    def connect(self, host, port, clientId, timeout):
        self.host, self.port, self.clientId = host, port, clientId

    def disconnect(self):
        self.disconnected = True

    def sleep(self, _s):
        pass

    def qualifyContracts(self, c):
        return [c]

    # --- orders ---
    def bracketOrder(self, action, qty, limitPrice, takeProfitPrice, stopLossPrice):
        _FakeIB.last_bracket = dict(action=action, qty=qty, limitPrice=limitPrice,
                                    takeProfitPrice=takeProfitPrice, stopLossPrice=stopLossPrice)
        return [_Order(100), _Order(101), _Order(102)]

    def placeOrder(self, contract, order):
        return _Trade(order, "PreSubmitted")

    # --- historical data ---
    def reqHistoricalData(self, contract, **kwargs):
        self._hist_calls += 1
        if self._hist_calls > 1:
            return []                       # second page empty → loop terminates
        now = pd.Timestamp.utcnow().tz_localize(None)
        return [
            {"date": now - pd.Timedelta(hours=3), "open": 1.0, "high": 2.0,
             "low": 0.5, "close": 1.5, "volume": 10},
            {"date": now - pd.Timedelta(hours=2), "open": 1.5, "high": 2.5,
             "low": 1.0, "close": 2.0, "volume": 11},
            {"date": now - pd.Timedelta(hours=1), "open": 2.0, "high": 3.0,
             "low": 1.5, "close": 2.5, "volume": 12},
        ]


def _make_fake_lib():
    mod = types.ModuleType("ib_async")
    mod.IB = _FakeIB
    mod.ContFuture = lambda sym, exch: types.SimpleNamespace(
        symbol=sym, exchange=exch, localSymbol=f"{sym}Z6")
    mod.util = types.SimpleNamespace(df=lambda bars: pd.DataFrame(bars))
    return mod


@pytest.fixture
def fake_ib(monkeypatch):
    _FakeIB.last_bracket = None
    mod = _make_fake_lib()
    monkeypatch.setitem(sys.modules, "ib_async", mod)
    # Ensure paper mode for the rail (default, but be explicit/robust).
    monkeypatch.setattr("execution.ibkr_orders.IB_ENV", "paper", raising=False)
    return mod


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def test_get_adapter_returns_ibkr_by_default(monkeypatch):
    monkeypatch.delenv("BROKER", raising=False)
    assert get_adapter().name == "ibkr"


def test_get_adapter_ibkr_explicit():
    assert isinstance(get_adapter("ibkr"), IBKRAdapter)


# ---------------------------------------------------------------------------
# Order validation (runs before any connection — no fake lib needed)
# ---------------------------------------------------------------------------
def test_rejects_nonpositive_qty():
    with pytest.raises(IBKROrderError, match="qty must be > 0"):
        IBKRAdapter().place_bracket(instrument=MNQ, side="Buy", qty=0,
                                    entry=100, stop=99, target=102)


def test_rejects_bad_side():
    with pytest.raises(IBKROrderError, match="side must be"):
        IBKRAdapter().place_bracket(instrument=MNQ, side="Long", qty=1,
                                    entry=100, stop=99, target=102)


def test_rejects_buy_bracket_bad_geometry():
    # Buy needs stop < entry < target
    with pytest.raises(IBKROrderError, match="Buy bracket invalid"):
        IBKRAdapter().place_bracket(instrument=MNQ, side="Buy", qty=1,
                                    entry=100, stop=101, target=102)


def test_rejects_sell_bracket_bad_geometry():
    # Sell needs target < entry < stop
    with pytest.raises(IBKROrderError, match="Sell bracket invalid"):
        IBKRAdapter().place_bracket(instrument=MNQ, side="Sell", qty=1,
                                    entry=100, stop=99, target=98)


def test_paper_rail_blocks_live_without_allow(monkeypatch):
    monkeypatch.setattr("execution.ibkr_orders.IB_ENV", "live", raising=False)
    with pytest.raises(IBKROrderError, match="allow_live=True"):
        IBKRAdapter().place_bracket(instrument=MNQ, side="Buy", qty=1,
                                    entry=100, stop=99, target=102, dry_run=False)


def test_dry_run_builds_body_without_connecting():
    # No fake lib injected: if this tried to connect it would raise about ib_async.
    res = IBKRAdapter().place_bracket(instrument=MNQ, side="Buy", qty=2,
                                      entry=100, stop=99, target=102, dry_run=True)
    assert res.order_id == 0
    assert res.raw_response["dry_run"] is True
    assert res.raw_response["body"]["side"] == "Buy"
    assert res.raw_response["body"]["qty"] == 2


# ---------------------------------------------------------------------------
# Order happy path (fake lib)
# ---------------------------------------------------------------------------
def test_place_bracket_buy_happy_path(fake_ib):
    res = IBKRAdapter().place_bracket(instrument=MNQ, side="Buy", qty=2,
                                      entry=100.0, stop=99.0, target=102.0)
    assert res.order_id == 100
    # The bracket was built with the correct action and prices.
    b = _FakeIB.last_bracket
    assert b["action"] == "BUY"
    assert b["qty"] == 2
    assert b["limitPrice"] == 100.0
    assert b["stopLossPrice"] == 99.0
    assert b["takeProfitPrice"] == 102.0


def test_place_bracket_sell_maps_to_sell_action(fake_ib):
    IBKRAdapter().place_bracket(instrument=MNQ, side="Sell", qty=1,
                                entry=100.0, stop=101.0, target=98.0)
    assert _FakeIB.last_bracket["action"] == "SELL"


# ---------------------------------------------------------------------------
# Data feed
# ---------------------------------------------------------------------------
def test_feed_rejects_bad_timeframe():
    from data import ibkr_feed
    with pytest.raises(ValueError, match="supports"):
        ibkr_feed.get_bars("NQ", "3m", days=5)


def test_feed_rejects_non_routable_symbol():
    from data import ibkr_feed
    with pytest.raises(ValueError, match="not an IBKR-routable"):
        ibkr_feed.get_bars("BTCUSDT", "15m", days=5)


def test_feed_happy_path_shape(fake_ib):
    from data import ibkr_feed
    df = ibkr_feed.get_bars("NQ", "1h", days=30)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.name == "timestamp"
    assert str(df.index.tz) == "UTC"
    assert len(df) == 3
    assert df["close"].dtype == float
