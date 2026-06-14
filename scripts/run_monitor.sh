#!/bin/zsh
# Wrapper script — runs the multi-symbol monitor in review mode against
# the configured ~/.ict-bot/personal_rules.yaml. Designed to be invoked
# directly OR by launchd.
set -e
cd "$(dirname "$0")/.."
source .venv/bin/activate
mkdir -p ~/.ict-bot/logs
exec python -m live.monitor \
    --symbols MNQ,MES \
    --timeframe 1h \
    --htf 1d \
    --days 14 \
    --poll 300 \
    --news-filter \
    --entry-mode closer_edge \
    --source yfinance \
    --mode review \
    --strategy sweep_choch_fvg
