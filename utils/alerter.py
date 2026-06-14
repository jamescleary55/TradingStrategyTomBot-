"""Multi-channel alert delivery.

Three channels, fan-out:

- **Console** — always prints a rich panel.
- **Telegram** — if ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID`` set in .env.
  Uses the Bot API directly (no telegram lib needed). Markdown-formatted.
  When :meth:`Alerter.notify_setup` is called with a ``df`` argument, a
  focused chart of the last ~80 bars is rendered and posted via
  ``sendPhoto`` so the setup lands on phone with the chart inline.
- **macOS** — native banner via ``osascript`` if ``ALERT_MACOS=1`` and the
  binary exists.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config import Instrument

log = logging.getLogger(__name__)
console = Console()


@dataclass
class AlertConfig:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    macos: bool = True


def load_config_from_env() -> AlertConfig:
    return AlertConfig(
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        macos=os.getenv("ALERT_MACOS", "1").strip() == "1",
    )


class Alerter:
    def __init__(self, cfg: AlertConfig | None = None):
        self.cfg = cfg or load_config_from_env()

    # --------------------------------------------------------------
    def notify(self, title: str, body: str, *, severity: str = "info") -> None:
        """Generic notification. ``severity`` ∈ {info, success, warning, error}."""
        color = {"success": "green", "warning": "yellow", "error": "red"}.get(severity, "blue")
        console.print(Panel(body, title=title, border_style=color, title_align="left"))
        if self.cfg.macos:
            self._notify_macos(title, body)
        if self.cfg.telegram_token and self.cfg.telegram_chat_id:
            self._notify_telegram(f"*{title}*\n\n{body}")

    # --------------------------------------------------------------
    def notify_setup(self, setup, instrument: Instrument,
                     sim_symbol: str | None = None, *, df=None) -> None:
        """High-level ICT setup card.

        If ``df`` is provided AND Telegram is configured, the Telegram
        alert ships as a photo (small annotated chart) via sendPhoto with
        the same Markdown caption. Without ``df`` the Telegram path falls
        back to text-only sendMessage.
        """
        direction_emoji = "🟢" if setup.direction == "bull" else "🔴"
        title = f"{direction_emoji}  {setup.direction.upper()}  {instrument.symbol}  @ {setup.timestamp.strftime('%Y-%m-%d %H:%M UTC')}"

        # Console
        tbl = Table.grid(padding=(0, 2))
        tbl.add_column(style="bold")
        tbl.add_column(justify="right")
        tbl.add_row("Entry", f"{setup.entry:.2f}")
        tbl.add_row("Stop",  f"[red]{setup.stop:.2f}[/red]")
        tbl.add_row("TP",    f"[green]{setup.target:.2f}[/green]")
        tbl.add_row("RR",    f"{setup.rr:.2f}")
        tbl.add_row("Bias",  setup.bias)
        for c in setup.confluence:
            tbl.add_row("·", c)
        border = "green" if setup.direction == "bull" else "red"
        console.print(Panel(tbl, title=title, border_style=border, title_align="left"))

        # macOS
        body_short = (f"E {setup.entry:.2f}  S {setup.stop:.2f}  T {setup.target:.2f}  "
                      f"RR {setup.rr:.2f}")
        if self.cfg.macos:
            self._notify_macos(title, body_short)

        # Telegram (Markdown)
        if self.cfg.telegram_token and self.cfg.telegram_chat_id:
            md = (
                f"{direction_emoji} *{setup.direction.upper()}* `{instrument.symbol}`\n"
                f"`{setup.timestamp.strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
                f"*Entry:* `{setup.entry:.2f}`\n"
                f"*Stop:*  `{setup.stop:.2f}`\n"
                f"*TP:*    `{setup.target:.2f}`\n"
                f"*RR:*    `{setup.rr:.2f}`\n"
                f"*Bias:*  `{setup.bias}`\n\n"
                + "\n".join(f"_{c}_" for c in setup.confluence)
            )
            if df is not None:
                try:
                    png = _render_setup_chart_png(df, setup, instrument)
                    self._telegram_send_photo(png, caption=md)
                except Exception as e:
                    log.warning("chart render failed (%s); falling back to text", e)
                    self._notify_telegram(md)
            else:
                self._notify_telegram(md)

    # --------------------------------------------------------------
    def _telegram_send_photo(self, png_bytes: bytes, caption: str) -> None:
        url = f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendPhoto"
        try:
            r = requests.post(
                url,
                data={
                    "chat_id": self.cfg.telegram_chat_id,
                    "caption": caption,
                    "parse_mode": "Markdown",
                },
                files={"photo": ("setup.png", png_bytes, "image/png")},
                timeout=15,
            )
            if not r.ok:
                log.warning("Telegram sendPhoto failed: %s %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.warning("Telegram sendPhoto error: %s", e)

    # --------------------------------------------------------------
    def _notify_telegram(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage"
        try:
            r = requests.post(url, json={
                "chat_id": self.cfg.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }, timeout=10)
            if not r.ok:
                log.warning("Telegram alert failed: %s %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.warning("Telegram alert error: %s", e)

    def _notify_macos(self, title: str, body: str) -> None:
        if not shutil.which("osascript"):
            return
        # Escape double-quotes for AppleScript string literals
        t = title.replace('"', '\\"')
        b = body.replace('"', '\\"')
        script = f'display notification "{b}" with title "{t}"'
        try:
            subprocess.run(["osascript", "-e", script], check=False, timeout=4)
        except subprocess.TimeoutExpired:
            pass

    # --------------------------------------------------------------
    def test(self) -> None:
        """One-shot connectivity test for every configured channel."""
        body = ("Channels configured: " +
                ", ".join(filter(None, [
                    "console",
                    "macos" if self.cfg.macos else None,
                    "telegram" if (self.cfg.telegram_token and self.cfg.telegram_chat_id) else None,
                ])))
        self.notify("ict-futures-bot alert test", body, severity="success")


# ---------------------------------------------------------------------------
# Setup mini-chart renderer (used for Telegram photo alerts)
# ---------------------------------------------------------------------------
def _render_setup_chart_png(df, setup, instrument: Instrument, focus_bars: int = 80) -> bytes:
    """Render a phone-friendly setup chart and return PNG bytes.

    Last ``focus_bars`` of price are drawn as candles. The FVG zone is
    shaded; the swept liquidity level + CHoCH + Entry/Stop/Target lines
    are annotated.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.patches as patches
    import matplotlib.pyplot as plt

    n = len(df)
    # Show some context after the CHoCH too
    end_idx = min(setup.choch.idx + 12, n - 1)
    start_idx = max(end_idx - focus_bars, 0)
    df_view = df.iloc[start_idx: end_idx + 1]

    fig, ax = plt.subplots(figsize=(9, 4.8), constrained_layout=True)
    ax.set_facecolor("#0a0f0d")
    fig.patch.set_facecolor("#07100d")

    # Candles
    if len(df_view) > 1:
        bar_seconds = (df_view.index[1] - df_view.index[0]).total_seconds()
    else:
        bar_seconds = 60
    w_days = (bar_seconds / 86400.0) * 0.7
    for t, o, h, l, c in zip(df_view.index, df_view["open"], df_view["high"],
                             df_view["low"], df_view["close"]):
        is_bull = c >= o
        color = "#22c55e" if is_bull else "#f87171"
        x = mdates.date2num(t)
        ax.add_line(plt.Line2D([x, x], [l, h], color=color, linewidth=0.7, alpha=0.85))
        body_low = min(o, c); body_high = max(o, c)
        height = max(body_high - body_low, (h - l) * 0.001)
        rect = patches.Rectangle(
            (x - w_days / 2, body_low), w_days, height,
            facecolor=color if is_bull else "#0a0f0d",
            edgecolor=color, linewidth=0.6, alpha=0.85,
        )
        ax.add_patch(rect)

    # FVG zone
    fvg = setup.fvg
    fvg_color = "#22c55e" if setup.direction == "bull" else "#f87171"
    fvg_start_ts = df.index[fvg.idx] if fvg.idx < n else df_view.index[0]
    fvg_end_ts = df.index[-1]
    rect = patches.Rectangle(
        (mdates.date2num(fvg_start_ts), fvg.bottom),
        mdates.date2num(fvg_end_ts) - mdates.date2num(fvg_start_ts),
        fvg.top - fvg.bottom,
        facecolor=fvg_color, edgecolor="none", alpha=0.12,
    )
    ax.add_patch(rect)

    # Swept liquidity level
    sweep = setup.sweep
    ax.axhline(sweep.level.price, color="#67e8f9", linestyle="--",
               linewidth=0.8, alpha=0.75)
    ax.annotate(
        f"swept {sweep.level.kind} {sweep.level.price:g}",
        xy=(df_view.index[-1], sweep.level.price), xytext=(-6, 4),
        textcoords="offset points", ha="right",
        color="#67e8f9", fontsize=8, fontweight="bold",
    )

    # CHoCH diamond
    ax.scatter([setup.choch.timestamp], [setup.choch.price],
               marker="D", facecolor="none", edgecolor="#c084fc",
               s=110, linewidth=1.6, zorder=6)
    ax.annotate("CHoCH", xy=(setup.choch.timestamp, setup.choch.price),
                xytext=(8, -10), textcoords="offset points",
                color="#c084fc", fontsize=8, fontweight="bold")

    # Setup entry + dotted stop/target
    color_dir = "#22c55e" if setup.direction == "bull" else "#f87171"
    marker_dir = "^" if setup.direction == "bull" else "v"
    ax.scatter([setup.timestamp], [setup.entry], marker=marker_dir,
               s=220, facecolor=color_dir, edgecolor="white",
               linewidth=1.6, zorder=8)
    ax.axhline(setup.stop, color="#f87171", linestyle=(0, (3, 3)),
               linewidth=1, alpha=0.85)
    ax.axhline(setup.target, color="#22c55e", linestyle=(0, (3, 3)),
               linewidth=1, alpha=0.85)

    # Right-edge price labels
    label_x = df_view.index[-1]
    for y, txt, c in [
        (setup.entry,  f"E  {setup.entry:.2f}",                  "#ecf6f0"),
        (setup.stop,   f"SL {setup.stop:.2f}",                   "#f87171"),
        (setup.target, f"TP {setup.target:.2f}  ({setup.rr:.1f}R)", "#22c55e"),
    ]:
        ax.annotate(txt, xy=(label_x, y), xytext=(8, 0),
                    textcoords="offset points", va="center", ha="left",
                    color=c, fontsize=9, fontweight="bold")

    # Title
    arrow = "▲" if setup.direction == "bull" else "▼"
    title = (f"{arrow} {setup.direction.upper()}  {instrument.symbol}   "
             f"{setup.timestamp.strftime('%Y-%m-%d %H:%M UTC')}")
    ax.set_title(title, color=color_dir, fontsize=12, pad=10, loc="left",
                 fontweight="bold")

    # Style
    ax.tick_params(colors="#7c9287", labelsize=8)
    ax.grid(True, axis="y", color="#1f3329", linestyle="--",
            linewidth=0.4, alpha=0.5)
    for spine in ax.spines.values():
        spine.set_color("#1f3329")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate(rotation=0, ha="center")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()
