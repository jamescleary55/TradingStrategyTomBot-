"""High-impact news blackout windows.

Two sources of events:

1. **Recurring rules** generated on the fly:
   - **NFP** — first Friday of each month, 08:30 ET (high).
   - **CPI** — monthly, around the 10th–15th, 08:30 ET (high). We use a
     deterministic ``8:30 on the 10th business day`` approximation.
   - **NY open volatility window** — 09:30–10:00 ET (medium); optional.
2. **Static list** — known FOMC press-conference dates (high). Update the
   list once per year. Times are 14:00 ET (statement) and 14:30 ET
   (press conference).

:func:`is_in_blackout` answers the question "should I skip a setup whose
CHoCH/entry fires at this time?" given configurable ``minutes_before`` /
``minutes_after`` padding.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable, Literal

import pandas as pd

from utils.time_utils import to_et


Severity = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class NewsEvent:
    ts_et: datetime          # event time in US Eastern (naive — date+time)
    label: str
    severity: Severity = "high"


# Known FOMC statement days (statement 14:00 ET, presser 14:30 ET).
# Source: federalreserve.gov calendar. Extend yearly.
FOMC_DATES_2024_2026 = [
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31",
    "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-04-30", "2025-06-18", "2025-07-30",
    "2025-09-17", "2025-10-29", "2025-12-17",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
    "2026-09-16", "2026-10-28", "2026-12-16",
]


# ---------------------------------------------------------------------------
def _first_friday(year: int, month: int) -> datetime:
    d = datetime(year, month, 1)
    offset = (4 - d.weekday()) % 7   # Mon=0, Fri=4
    return datetime(year, month, 1 + offset, 8, 30)


def _cpi_release_day(year: int, month: int) -> datetime:
    """Coarse approximation: 10th of the month at 08:30 ET; bumped to next
    business day if the 10th lands on a weekend.
    """
    d = datetime(year, month, 10, 8, 30)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def generate_events(start: datetime, end: datetime,
                    include_nfp: bool = True,
                    include_cpi: bool = True,
                    include_fomc: bool = True,
                    include_ny_open: bool = False) -> list[NewsEvent]:
    """Return all events whose ET-naive time falls in ``[start, end]``."""
    events: list[NewsEvent] = []
    cur = datetime(start.year, start.month, 1)
    while cur <= end:
        if include_nfp:
            ev = _first_friday(cur.year, cur.month)
            if start <= ev <= end:
                events.append(NewsEvent(ev, f"NFP {ev.strftime('%Y-%m-%d')}", "high"))
        if include_cpi:
            ev = _cpi_release_day(cur.year, cur.month)
            if start <= ev <= end:
                events.append(NewsEvent(ev, f"CPI {ev.strftime('%Y-%m-%d')}", "high"))
        # next month
        if cur.month == 12:
            cur = datetime(cur.year + 1, 1, 1)
        else:
            cur = datetime(cur.year, cur.month + 1, 1)

    if include_fomc:
        for ds in FOMC_DATES_2024_2026:
            d = datetime.strptime(ds, "%Y-%m-%d").replace(hour=14, minute=0)
            if start <= d <= end:
                events.append(NewsEvent(d, f"FOMC statement {ds}", "high"))
                presser = d.replace(minute=30)
                events.append(NewsEvent(presser, f"FOMC presser {ds}", "high"))

    if include_ny_open:
        cur = datetime(start.year, start.month, start.day, 9, 30)
        while cur <= end:
            if cur.weekday() < 5 and start <= cur <= end:
                events.append(NewsEvent(cur, "NY open volatility", "medium"))
            cur += timedelta(days=1)

    events.sort(key=lambda e: e.ts_et)
    return events


# ---------------------------------------------------------------------------
def is_in_blackout(ts, events: Iterable[NewsEvent],
                   minutes_before: int = 30, minutes_after: int = 30) -> tuple[bool, NewsEvent | None]:
    """Return (blocked, matching_event_or_None) for the given timestamp."""
    et = to_et(ts).replace(tzinfo=None)
    for ev in events:
        delta = (et - ev.ts_et).total_seconds() / 60
        if -minutes_before <= delta <= minutes_after:
            return True, ev
    return False, None


def filter_setups(setups, events: Iterable[NewsEvent],
                  minutes_before: int = 30, minutes_after: int = 30) -> tuple[list, list]:
    """Split setups into (kept, blocked) based on the blackout windows."""
    events = list(events)
    kept, blocked = [], []
    for s in setups:
        hit, ev = is_in_blackout(s.timestamp, events, minutes_before, minutes_after)
        if hit:
            blocked.append((s, ev))
        else:
            kept.append(s)
    return kept, blocked
