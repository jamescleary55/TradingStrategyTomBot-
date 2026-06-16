# ICT Trading System

An ICT (Inner Circle Trader) strategy implemented **two ways**:

1. **Python / Backtesting.py** — full research stack: data loading, signal
   generation, backtesting, metrics, optimisation and Plotly charts.
2. **Pine Script v5** (`ict_strategy.pine`) — a single file to paste straight
   into the TradingView **Pine Editor** and run on any chart.

Both implement the same model:

```
LONG  = bullish liquidity sweep  +  bullish CHoCH  +  bullish FVG
        -> limit entry at 50% of the FVG, stop below the sweep low, TP at 1:RR
SHORT = bearish liquidity sweep  +  bearish CHoCH  +  bearish FVG
        -> limit entry at 50% of the FVG, stop above the sweep high, TP at 1:RR
```

ICT concepts covered: Market Structure (**BOS / CHoCH**), Liquidity
(**equal highs/lows + sweeps**), **Fair Value Gaps**, risk-based sizing
(1% per trade), 1:3 minimum RR, optional ATR trailing stop, and full
visualisation.

---

## Folder structure

```
ict_trading_system/
├── README.md
├── requirements.txt
├── config.py              # all tunable parameters (StrategyParams)
├── main.py                # CLI: backtest / optimise / plot
├── backtest.py            # Backtesting.py Strategy + runner + metrics
├── optimize.py            # grid search: FVG size / liquidity lookback / RR
├── plotting.py            # Plotly ICT chart (BOS, CHoCH, FVG, sweeps, levels)
├── ict_strategy.pine      # TradingView Pine Script v5 (copy/paste)
├── data/
│   └── data_loader.py     # Binance + Yahoo Finance loaders
├── ict/
│   ├── market_structure.py   # swing pivots, BOS, CHoCH
│   ├── liquidity.py          # equal highs/lows, liquidity sweeps
│   ├── fvg.py                # fair value gaps
│   └── signals.py            # combines everything -> signal columns
└── results/               # CSVs + HTML charts (created on run)
```

---

## Installation (Python)

```bash
cd ict_trading_system
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## Usage (Python)

```bash
# Single backtest + charts (BTC hourly via Binance)
python main.py --symbol BTCUSDT --interval 1h --start 2025-06-01 --plot

# Forex / equities via Yahoo Finance
python main.py --symbol EURUSD --source yfinance --interval 1h
python main.py --symbol SPY    --source yfinance --interval 1d

# Grid optimisation (FVG size / liquidity lookback / risk-reward)
python main.py --symbol ETHUSDT --optimize
```

Supported symbols: `BTCUSDT`, `ETHUSDT` (Binance or Yahoo), `EURUSD`, `SPY`
(Yahoo). Any other ticker works too — pass `--source yfinance`.

> **Note on Yahoo intraday data:** `yfinance` only serves `1h` data for the
> last ~730 days, so use recent `--start` dates for hourly tests, or use
> `--source binance` for crypto history.

### Signal output

`ict.generate_signals()` returns a DataFrame with the required columns plus all
intermediate ICT fields:

| Column | Values |
|---|---|
| `Signal` | `BUY` / `SELL` / `NONE` |
| `Entry` | FVG 50% retracement price |
| `StopLoss` | beyond the sweep extreme |
| `TakeProfit` | entry ± RR × risk |
| `RiskReward` | realised RR of the setup |

### Metrics reported

Net Profit, Return %, Win Rate, Profit Factor, Max Drawdown, Sharpe Ratio,
Total Trades, and **Average R Multiple** (PnL ÷ planned 1% risk per trade).

---

## Usage (TradingView / Pine Script)

1. Open TradingView → **Pine Editor** (bottom panel).
2. Open `ict_strategy.pine`, copy its contents, paste into the editor.
3. Click **Add to chart**. Open the **Strategy Tester** tab for backtest stats.
4. Tune inputs in the strategy settings — the optimiser knobs are
   **Min FVG size %**, **Sweep validity**, and **Risk : Reward**.

The Pine version plots BOS, CHoCH, FVG zones, liquidity sweeps, equal-level
pools, entries and entry/SL/TP guide lines, and fires `alertcondition` alerts
on each setup.

---

## Tuning / disclaimer

Default parameters are **not** profit-tuned — they are sensible starting points.
The triple-confluence model is intentionally strict; expect to optimise
`min_fvg_size`, `liquidity_lookback` and `risk_reward` per symbol/timeframe via
`python main.py --symbol X --optimize`. This is educational software for
backtesting research, **not** financial advice.
