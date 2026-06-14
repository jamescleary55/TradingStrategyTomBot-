# ict-futures-bot

ICT-style detectors for futures markets: market structure (BOS / CHoCH),
liquidity (EQH/EQL, PDH/PDL, PWH/PWL, session highs/lows + sweeps), and
Fair Value Gaps. Wired to Tradovate for live data; falls back to a synthetic
NQ-like series when credentials aren't set so the pipeline runs offline.

## Install

```bash
cd ~/projects/ict-futures-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Tradovate creds (optional for backtest)
```

## Run the pipeline

```bash
python -m backtest.run                       # NQ 15m, last 30 days
python -m backtest.run --symbol MNQ --days 7
```

Prints summary counts and writes an annotated chart to `charts/`.

## Project layout

```
data/             Tradovate REST + WS client, OHLCV loader
engine/           market_structure (swings, BOS, CHoCH) + liquidity
signals/          fvg
risk/             (placeholder for position sizing + daily loss limit)
execution/        (placeholder for order routing)
backtest/         pipeline runner + matplotlib chart
utils/            time/session helpers
config.py         sessions, instruments, risk params
```

`backtrader` is included in `requirements.txt` for future strategy
backtesting; the current `backtest/run.py` is a custom detector walkthrough,
not a backtrader cerebro run.

## Notes

- Sessions are US Eastern Time (ICT convention). DST is handled via `pytz`.
- All credentials live in `.env` (gitignored); use `TRADOVATE_ENV=demo` for paper.
- Detector parameters live in `config.py` (`SWING_LOOKBACK`, `EQUAL_LEVEL_TOLERANCE`, etc.).
