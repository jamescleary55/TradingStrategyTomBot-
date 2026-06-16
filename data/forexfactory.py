"""
ForexFactory economic-calendar loader.

ForexFactory's HTML calendar sits behind Cloudflare and blocks scrapers, but
they publish the same calendar as an official JSON feed via faireconomy.media
(the feed that powers the site's own "This Week" export).  We use that instead
of scraping HTML — it is reliable, structured, and ToS-clean.

Feeds (all current/forward-looking, not deep history):
    ff_calendar_thisweek.json
    ff_calendar_nextmonth.json   (when published)

Each event: title, country, date (ISO w/ tz), impact (High/Medium/Low/Holiday),
forecast, previous.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
import requests

FEED_BASE = "https://nfs.faireconomy.media"
_UA = "Mozilla/5.0 (compatible; ICTBacktester/1.0)"


def load_calendar(period: str = "thisweek") -> pd.DataFrame:
    """
    Fetch the ForexFactory economic calendar from the official JSON feed.

    Parameters
    ----------
    period : "thisweek" | "nextweek" | "thismonth" | "nextmonth"
             (availability depends on what faireconomy currently publishes).

    Returns a DataFrame indexed by tz-naive UTC datetime with columns:
        Country, Title, Impact, Forecast, Previous
    """
    url = f"{FEED_BASE}/ff_calendar_{period}.json"
    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        raise RuntimeError(f"ForexFactory feed empty for period '{period}'.")

    df = pd.DataFrame(rows)
    # 'date' is ISO 8601 with an offset (e.g. 2026-06-07T05:15:00-04:00).
    dt = pd.to_datetime(df["date"], utc=True).dt.tz_convert("UTC").dt.tz_localize(None)
    df = df.assign(Date=dt).set_index("Date").sort_index()
    df = df.rename(columns={"country": "Country", "title": "Title",
                            "impact": "Impact", "forecast": "Forecast",
                            "previous": "Previous"})
    return df[["Country", "Title", "Impact", "Forecast", "Previous"]]


def high_impact_times(df: pd.DataFrame,
                      currencies: Optional[list] = None) -> pd.DatetimeIndex:
    """
    Timestamps (tz-naive UTC) of High-impact ("red folder") events — useful to
    flag/avoid entries around major news.  Optionally filter to specific
    currencies (e.g. ['USD', 'EUR']).  Feed the result to news_block_mask().
    """
    mask = df["Impact"].str.lower() == "high"
    if currencies:
        mask &= df["Country"].isin(currencies)
    return df.index[mask]


def nfp_times(start: str, end: str) -> pd.DatetimeIndex:
    """
    Deterministic schedule of US Non-Farm Payrolls releases (1st Friday of each
    month, 08:30 America/New_York), returned as tz-naive UTC timestamps.

    NFP is the single biggest scheduled mover for US equity indices and its
    timing is public and fixed, so it works as a free stand-in for a historical
    calendar when building/validating a news filter over a long backtest.
    Swap in a real historical calendar (paid) for full coverage — the block
    mask below treats any list of event times identically.
    """
    months = pd.date_range(pd.Timestamp(start).normalize().replace(day=1),
                           pd.Timestamp(end), freq="MS")
    events = []
    for m in months:
        # first Friday of the month
        first_fri = m + pd.Timedelta(days=(4 - m.weekday()) % 7)
        ts = pd.Timestamp(first_fri.date()) + pd.Timedelta(hours=8, minutes=30)
        utc = ts.tz_localize("America/New_York").tz_convert("UTC").tz_localize(None)
        if pd.Timestamp(start) <= utc <= pd.Timestamp(end):
            events.append(utc)
    return pd.DatetimeIndex(events)


def news_block_mask(index: pd.DatetimeIndex, events: pd.DatetimeIndex,
                    window_min: int) -> "pd.Series":
    """
    Boolean Series over `index`: True where the bar is within +/- window_min
    minutes of any event time.  index and events must share a timezone
    convention (both tz-naive UTC here).
    """
    blocked = pd.Series(False, index=index)
    if window_min <= 0 or len(events) == 0:
        return blocked
    win = pd.Timedelta(minutes=window_min)
    ev = pd.DatetimeIndex(events).sort_values()
    pos = ev.searchsorted(index)            # nearest event on each side
    for k, ts in enumerate(index):
        for j in (pos[k] - 1, pos[k]):
            if 0 <= j < len(ev) and abs(ts - ev[j]) <= win:
                blocked.iloc[k] = True
                break
    return blocked


if __name__ == "__main__":
    import os
    cal = load_calendar("thisweek")
    print(f"Loaded {len(cal)} events "
          f"({cal.index.min()} -> {cal.index.max()})")
    print(cal.to_string())

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "market_data", "forexfactory_thisweek.csv")
    cal.to_csv(out)
    print(f"\nSaved -> {out}")
    highs = high_impact_times(cal, ["USD"])
    print(f"\nHigh-impact USD events this week: {len(highs)}")
