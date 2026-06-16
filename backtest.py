"""
Backtesting.py integration for the ICT strategy.

The Strategy consumes the pre-computed signal columns (SignalNum, Entry,
StopLoss, TakeProfit) produced by ict.generate_signals and places risk-based
limit orders.  Position size is solved so the loss at the stop equals
`risk_per_trade` of current equity.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
from backtesting import Backtest, Strategy

from config import StrategyParams, DEFAULT_PARAMS
from ict.signals import generate_signals


# --------------------------------------------------------------------------- #
#  Strategy
# --------------------------------------------------------------------------- #
class ICTStrategy(Strategy):
    # These class attributes are what Backtest.optimize() tweaks.
    risk_per_trade = 0.01
    risk_reward = 3.0
    entry_valid_bars = 10
    breakeven_r = 0.0
    long_only = True             # block ALL short execution (index mode)
    use_trailing_stop = False
    trail_atr_mult = 2.0

    def init(self):
        # NOTE: do NOT cache self.data.<col> here — a reference captured in
        # init() is a static snapshot whose [-1] always points at the final
        # bar of the full dataset, so the strategy would never see live
        # signals.  Read self.data.<col>[-1] inside next() instead.
        self._pending_bar = -1       # bar index when the live entry was placed
        self._short_blocked = 0      # count of sell signals refused by long_only

    def _size_for_risk(self, entry: float, stop: float) -> float:
        """Fraction of equity so the stop-out loss == risk_per_trade."""
        dist = abs(entry - stop)
        if dist <= 0:
            return 0.0
        frac = self.risk_per_trade * entry / dist
        # Backtesting.py needs 0 < fraction < 1 for a relative size.
        return float(min(max(frac, 0.0), 0.99))

    def next(self):
        i = len(self.data) - 1
        price = self.data.Close[-1]

        # --- break-even stop: once price is breakeven_r * R in profit, move
        #     the stop to entry so the trade can't turn into a loss --------- #
        if self.breakeven_r > 0 and self.position and self.trades:
            trade = self.trades[-1]
            ep = trade.entry_price
            if self.position.is_long:
                risk = (trade.tp - ep) / self.risk_reward if trade.tp else 0.0
                if risk > 0 and self.data.High[-1] >= ep + self.breakeven_r * risk \
                        and (trade.sl is None or trade.sl < ep):
                    trade.sl = ep
            else:
                risk = (ep - trade.tp) / self.risk_reward if trade.tp else 0.0
                if risk > 0 and self.data.Low[-1] <= ep - self.breakeven_r * risk \
                        and (trade.sl is None or trade.sl > ep):
                    trade.sl = ep

        # --- optional trailing stop on the open position ------------------ #
        if self.use_trailing_stop and self.position and self.trades:
            atr = self.data.ATR[-1]
            trade = self.trades[-1]
            if self.position.is_long:
                new_sl = price - self.trail_atr_mult * atr
                if trade.sl is None or new_sl > trade.sl:
                    trade.sl = new_sl
            else:
                new_sl = price + self.trail_atr_mult * atr
                if trade.sl is None or new_sl < trade.sl:
                    trade.sl = new_sl

        # --- already in a trade: let SL/TP manage the exit ---------------- #
        if self.position:
            return

        # --- a pending entry is resting: wait, or expire it --------------- #
        if self.orders:
            if i - self._pending_bar >= self.entry_valid_bars:
                for o in self.orders:
                    o.cancel()
            return

        # --- news filter: skip new entries around high-impact events ------ #
        if self.data.NewsBlock[-1]:
            return

        sig = self.data.SignalNum[-1]
        if sig == 0:
            return

        # --- LONG-ONLY execution guard: refuse every short, no matter what
        #     the signal layer produced.  This is the authoritative gate that
        #     guarantees no sell order can ever be submitted in index mode. -- #
        if self.long_only and sig < 0:
            self._short_blocked += 1
            return

        entry = float(self.data.Entry[-1])
        stop = float(self.data.StopLoss[-1])
        take = float(self.data.TakeProfit[-1])
        if not np.isfinite(entry) or not np.isfinite(stop) or not np.isfinite(take):
            return

        size = self._size_for_risk(entry, stop)
        if size <= 0:
            return

        if sig > 0 and stop < entry < take:
            self.buy(size=size, limit=entry, sl=stop, tp=take)
            self._pending_bar = i
        elif sig < 0 and take < entry < stop:
            self.sell(size=size, limit=entry, sl=stop, tp=take)
            self._pending_bar = i


# --------------------------------------------------------------------------- #
#  Runner + metrics
# --------------------------------------------------------------------------- #
def prepare(df: pd.DataFrame, params: StrategyParams,
            news_events=None) -> pd.DataFrame:
    """Generate signals and attach the numeric columns the Strategy reads.

    `news_events` is an optional DatetimeIndex of high-impact event times
    (same tz convention as df.index).  Bars within +/- params.news_window_min
    of an event get NewsBlock=True and the strategy skips entries there.
    """
    raw = generate_signals(df, params)
    sig = raw.copy()
    sig.attrs["diag"] = raw.attrs.get("diag", {})   # preserve signal-funnel diag
    sig["SignalNum"] = sig["Signal"].map({"BUY": 1, "SELL": -1, "NONE": 0}).astype(float)
    # Backtesting.py needs finite numbers in the columns it indexes.
    for col in ("Entry", "StopLoss", "TakeProfit", "ATR"):
        sig[col] = sig[col].astype(float).ffill().fillna(0.0)

    if news_events is not None and params.news_window_min > 0:
        from data.forexfactory import news_block_mask
        sig["NewsBlock"] = news_block_mask(sig.index, news_events,
                                           params.news_window_min).to_numpy()
    else:
        sig["NewsBlock"] = False
    return sig


def average_r_multiple(stats, risk_per_trade: float) -> float:
    """Mean R multiple of closed trades (PnL / planned 1% risk at entry)."""
    trades = stats.get("_trades")
    eq = stats.get("_equity_curve")
    if trades is None or len(trades) == 0 or eq is None:
        return float("nan")
    equity = eq["Equity"].to_numpy()
    rs = []
    for _, t in trades.iterrows():
        bar = int(t["EntryBar"])
        bar = min(max(bar, 0), len(equity) - 1)
        risk_dollars = risk_per_trade * equity[bar]
        if risk_dollars > 0:
            rs.append(t["PnL"] / risk_dollars)
    return float(np.mean(rs)) if rs else float("nan")


def run_backtest(df: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS,
                 cash: float = 100_000, commission: float = 0.0004,
                 plot: bool = False, plot_path: Optional[str] = None,
                 news_events=None):
    """Run a single backtest and return (stats, Backtest, prepared_df)."""
    data = prepare(df, params, news_events=news_events)

    bt = Backtest(data, ICTStrategy, cash=cash, commission=commission,
                  trade_on_close=False, exclusive_orders=False,
                  finalize_trades=True)
    stats = bt.run(
        risk_per_trade=params.risk_per_trade,
        risk_reward=params.risk_reward,
        entry_valid_bars=params.entry_valid_bars,
        breakeven_r=params.breakeven_r,
        long_only=params.long_only,
        use_trailing_stop=params.use_trailing_stop,
        trail_atr_mult=params.trail_atr_mult,
    )

    metrics = summarize(stats, cash, params.risk_per_trade)
    if plot:
        bt.plot(filename=plot_path, open_browser=False)
    return stats, bt, data, metrics


def _count_short_trades(stats) -> int:
    """Number of executed SHORT trades — must be 0 in long-only index mode."""
    tr = stats.get("_trades")
    if tr is None or len(tr) == 0:
        return 0
    return int((tr["Size"] < 0).sum())


def summarize(stats, cash: float, risk_per_trade: float) -> dict:
    """Pull the required headline metrics out of a Backtesting.py stats object."""
    return {
        "Net Profit [$]": stats["Equity Final [$]"] - cash,
        "Return [%]": stats["Return [%]"],
        "CAGR [%]": stats.get("Return (Ann.) [%]", float("nan")),
        "Exposure [%]": stats.get("Exposure Time [%]", float("nan")),
        "Win Rate [%]": stats["Win Rate [%]"],
        "Profit Factor": stats["Profit Factor"],
        "Max Drawdown [%]": stats["Max. Drawdown [%]"],
        "Sharpe Ratio": stats["Sharpe Ratio"],
        "Total Trades": stats["# Trades"],
        "Long Trades": stats["# Trades"] - _count_short_trades(stats),
        "Short Trades": _count_short_trades(stats),
        "Avg R Multiple": average_r_multiple(stats, risk_per_trade),
    }


def report_diagnostics(stats, prepared, title: str = "LONG-ONLY DIAGNOSTICS"):
    """Print the signal-funnel + execution diagnostics required for the
    long-only index model (setups detected -> blocked by HTF -> executed)."""
    d = getattr(prepared, "attrs", {}).get("diag", {})
    strat = stats._strategy
    longs = int(stats["# Trades"]) - _count_short_trades(stats)
    print("\n" + "-" * 52)
    print(f"  {title}")
    print("-" * 52)
    print(f"  Bullish setups detected     {d.get('bull_detected', 0):>8}")
    print(f"  Blocked by HTF filter       {d.get('bull_blocked_htf', 0):>8}")
    print(f"  BUY signals emitted         {d.get('bull_emitted', 0):>8}")
    print(f"  Long trades EXECUTED        {longs:>8}")
    print(f"  Bearish setups detected     {d.get('bear_detected', 0):>8}  (analysis only)")
    print(f"  Short signals refused       {getattr(strat, '_short_blocked', 0):>8}  (long_only guard)")
    print(f"  Short trades EXECUTED       {_count_short_trades(stats):>8}  <- must be 0")
    print("-" * 52)


def print_metrics(metrics: dict, title: str = "BACKTEST RESULTS"):
    print("\n" + "=" * 46)
    print(f"  {title}")
    print("=" * 46)
    for k, v in metrics.items():
        if isinstance(v, float):
            print(f"  {k:<22} {v:>18.2f}")
        else:
            print(f"  {k:<22} {v:>18}")
    print("=" * 46 + "\n")
