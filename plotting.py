"""
Rich ICT visualisation with Plotly.

Plots candles plus every ICT element on one chart:
    * BOS / CHoCH structural breaks   (triangle markers)
    * Fair Value Gap zones            (shaded rectangles, green/red)
    * Liquidity sweeps                (diamond markers)
    * Entry points                    (markers at the FVG 50%)
    * Stop loss / take profit         (short coloured dashes per signal)

Use this for the analytical view.  For trade-by-trade P&L use
Backtest.plot() (wired up in backtest.run_backtest(plot=True)).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go


def plot_ict(df: pd.DataFrame, title: str = "ICT Strategy",
             max_fvg_boxes: int = 200,
             save_html: Optional[str] = None,
             show: bool = False) -> go.Figure:
    """`df` must be the output of ict.generate_signals (or backtest.prepare)."""
    x = df.index

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=x, open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
        name="Price", increasing_line_color="#26a69a",
        decreasing_line_color="#ef5350",
    ))

    # --- Fair Value Gap zones --------------------------------------------- #
    fvg_bars = df.index[df["FVG"] != 0]
    for ts in fvg_bars[-max_fvg_boxes:]:
        row = df.loc[ts]
        bullish = row["FVG"] == 1
        loc = df.index.get_loc(ts)
        x1 = df.index[min(loc + 10, len(df) - 1)]
        fig.add_shape(
            type="rect", x0=ts, x1=x1,
            y0=row["FVGBottom"], y1=row["FVGTop"],
            fillcolor="rgba(38,166,154,0.18)" if bullish else "rgba(239,83,80,0.18)",
            line=dict(width=0), layer="below",
        )

    def _scatter(mask, y, name, symbol, color, size=11):
        if mask.any():
            fig.add_trace(go.Scatter(
                x=x[mask], y=y[mask], mode="markers", name=name,
                marker=dict(symbol=symbol, color=color, size=size,
                            line=dict(width=1, color="black")),
            ))

    # --- Structure -------------------------------------------------------- #
    _scatter(df["BOS"] == 1, df["Low"] * 0.999, "BOS (bull)", "triangle-up", "#2e7d32")
    _scatter(df["BOS"] == -1, df["High"] * 1.001, "BOS (bear)", "triangle-down", "#c62828")
    _scatter(df["CHoCH"] == 1, df["Low"] * 0.998, "CHoCH (bull)", "star", "#00c853")
    _scatter(df["CHoCH"] == -1, df["High"] * 1.002, "CHoCH (bear)", "star", "#d50000")

    # --- Liquidity sweeps ------------------------------------------------- #
    _scatter(df["Sweep"] == 1, df["Low"] * 0.997, "Sweep low", "diamond", "#1e88e5")
    _scatter(df["Sweep"] == -1, df["High"] * 1.003, "Sweep high", "diamond", "#fb8c00")

    # --- Entries + SL/TP -------------------------------------------------- #
    buy = df["Signal"] == "BUY"
    sell = df["Signal"] == "SELL"
    _scatter(buy, df["Entry"], "BUY entry", "circle", "#00e676", 12)
    _scatter(sell, df["Entry"], "SELL entry", "circle", "#ff1744", 12)

    # Stop / target dashes for each signal.
    sig_idx = df.index[(buy | sell)]
    for ts in sig_idx:
        row = df.loc[ts]
        loc = df.index.get_loc(ts)
        x1 = df.index[min(loc + 6, len(df) - 1)]
        fig.add_shape(type="line", x0=ts, x1=x1, y0=row["StopLoss"], y1=row["StopLoss"],
                      line=dict(color="red", width=1, dash="dot"), layer="above")
        fig.add_shape(type="line", x0=ts, x1=x1, y0=row["TakeProfit"], y1=row["TakeProfit"],
                      line=dict(color="green", width=1, dash="dot"), layer="above")

    fig.update_layout(
        title=title, xaxis_rangeslider_visible=False,
        template="plotly_dark", height=820,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )

    if save_html:
        fig.write_html(save_html)
        print(f"Saved chart -> {save_html}")
    if show:
        fig.show()
    return fig
