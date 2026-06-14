"""ForexFactory economic calendar scraper.

Scrapes ``https://www.forexfactory.com/calendar`` for high-impact news
events used by ``utils/news.py`` blackouts. Covers a configurable number
of past weeks; can also be called for a single week for testing.

Cloudflare is bypassed via ``cloudscraper`` (no JS execution required).
Be courteous: defaults polite — 1.5s sleep between weeks.

Output schema (one row per event)::

    date_et   (YYYY-MM-DD, Eastern)
    time_et   ("HH:MM" 24h, or "all_day", "tentative")
    currency  (USD, EUR, GBP, JPY, ... or "ALL")
    impact    (high | medium | low | holiday)
    event     (free text)
    actual    (string as released)
    forecast  (string)
    previous  (string)

The scraper does NOT try to map ambiguous times ("Tentative", "All Day")
to a precise minute — those rows pass through untouched. Downstream
consumers should decide how to handle them.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Optional

import cloudscraper
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE = "https://www.forexfactory.com/calendar"
DEFAULT_DIR = Path.home() / ".ict-bot" / "news"
DEFAULT_DIR.mkdir(parents=True, exist_ok=True)

IMPACT_MAP = {
    "red": "high",
    "ora": "medium",
    "yel": "low",
    "gra": "holiday",
}

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]


@dataclass
class FFEvent:
    date_et: str
    time_et: str
    currency: str
    impact: str
    event: str
    actual: str = ""
    forecast: str = ""
    previous: str = ""
    week_key: str = ""


# ---------------------------------------------------------------------------
def week_key_for(d: dt.date) -> str:
    """ForexFactory's URL week key uses the *Sunday* of that week.

    Example: jun8.2025  (Sunday of week containing Jun 9-13)
    """
    # FF weeks start Sunday. weekday(): Mon=0 ... Sun=6
    sunday = d - dt.timedelta(days=(d.weekday() + 1) % 7)
    return f"{_MONTHS[sunday.month - 1]}{sunday.day}.{sunday.year}"


# ---------------------------------------------------------------------------
def _make_session():
    return cloudscraper.create_scraper(
        browser={"browser": "firefox", "platform": "darwin", "desktop": True},
    )


def fetch_week_html(scraper, week_key: Optional[str] = None) -> str:
    url = BASE if week_key is None else f"{BASE}?week={week_key}"
    r = scraper.get(url, timeout=30)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------------
def _parse_time(raw: str) -> str:
    """Map "8:30am" → "08:30"; "All Day" → "all_day"; "Tentative" → "tentative"."""
    raw = (raw or "").strip().lower()
    if not raw:
        return ""
    if "all" in raw and "day" in raw:
        return "all_day"
    if "tentative" in raw:
        return "tentative"
    # 12-hour like "8:30am" / "10:00pm"
    raw = raw.replace(" ", "")
    is_pm = raw.endswith("pm")
    raw = raw.rstrip("amp")
    parts = raw.split(":")
    if len(parts) != 2:
        return raw
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return raw
    if is_pm and h != 12:
        h += 12
    if not is_pm and h == 12:
        h = 0
    return f"{h:02d}:{m:02d}"


def _impact_from_classes(icon_classes: list[str]) -> str:
    for cls in icon_classes or []:
        if cls.startswith("icon--ff-impact-"):
            code = cls.rsplit("-", 1)[-1]
            return IMPACT_MAP.get(code, code)
    return ""


def parse_week(html: str, week_key: str = "") -> list[FFEvent]:
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("tr.calendar__row")
    events: list[FFEvent] = []
    current_date = ""        # carried across rows after a day-breaker
    for row in rows:
        classes = row.get("class") or []
        # Pick up the date from new-day rows
        date_cell = row.select_one(".calendar__date")
        if date_cell:
            txt = date_cell.get_text(" ", strip=True)
            if txt:
                current_date = _normalise_date(txt)
        # Skip day-breakers without event data
        if "calendar__row--day-breaker" in classes:
            continue

        time_cell = row.select_one(".calendar__time")
        currency_cell = row.select_one(".calendar__currency")
        event_cell = row.select_one(".calendar__event")
        actual_cell = row.select_one(".calendar__actual")
        forecast_cell = row.select_one(".calendar__forecast")
        previous_cell = row.select_one(".calendar__previous")
        impact_cell = row.select_one(".calendar__impact")

        time_raw = time_cell.get_text(" ", strip=True) if time_cell else ""
        currency = currency_cell.get_text(" ", strip=True) if currency_cell else ""
        event_name = event_cell.get_text(" ", strip=True) if event_cell else ""
        if not event_name:
            continue

        impact_icon = impact_cell.select_one("span") if impact_cell else None
        impact = _impact_from_classes(impact_icon.get("class")) if impact_icon else ""

        events.append(FFEvent(
            date_et=current_date,
            time_et=_parse_time(time_raw),
            currency=(currency or "ALL").upper(),
            impact=impact,
            event=event_name,
            actual=(actual_cell.get_text(" ", strip=True) if actual_cell else ""),
            forecast=(forecast_cell.get_text(" ", strip=True) if forecast_cell else ""),
            previous=(previous_cell.get_text(" ", strip=True) if previous_cell else ""),
            week_key=week_key,
        ))
    return events


def _normalise_date(raw: str) -> str:
    """ForexFactory's date headers look like 'Mon Jun 8' (no year)."""
    raw = raw.replace("\u00a0", " ").strip()
    parts = raw.split()
    # Drop weekday name if present
    if len(parts) == 3 and parts[0].isalpha():
        parts = parts[1:]
    if len(parts) != 2:
        return ""
    month_name, day = parts
    month_idx = _MONTHS.index(month_name[:3].lower()) + 1 \
        if month_name[:3].lower() in _MONTHS else 0
    if not month_idx:
        return ""
    # Year defaults to current calendar year of the URL — we'll fix in main()
    return f"{month_idx:02d}-{int(day):02d}"


# ---------------------------------------------------------------------------
def scrape_weeks(weeks_back: int = 4, sleep_s: float = 1.5) -> list[FFEvent]:
    """Pull ``weeks_back`` weeks ending with the current one."""
    scraper = _make_session()
    today = dt.date.today()
    out: list[FFEvent] = []
    for i in range(weeks_back, -1, -1):
        anchor = today - dt.timedelta(weeks=i)
        wk = week_key_for(anchor)
        log.info("Fetching week %s (i=%d)", wk, i)
        try:
            html = fetch_week_html(scraper, wk)
        except Exception as e:
            log.warning("week %s fetch failed: %s", wk, e)
            time.sleep(sleep_s)
            continue
        events = parse_week(html, week_key=wk)
        # Reattach year from the week's anchor
        for ev in events:
            if ev.date_et and "-" in ev.date_et and len(ev.date_et) == 5:
                month, day = ev.date_et.split("-")
                year = anchor.year
                # Edge: week spans year boundary
                if int(month) == 12 and anchor.month == 1:
                    year -= 1
                elif int(month) == 1 and anchor.month == 12:
                    year += 1
                ev.date_et = f"{year}-{month}-{day}"
        out.extend(events)
        time.sleep(sleep_s)
    return out


# ---------------------------------------------------------------------------
def save_csv(events: Iterable[FFEvent], path: Path) -> Path:
    rows = [asdict(e) for e in events]
    if not rows:
        path.write_text("no_events\n")
        return path
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def save_json(events: Iterable[FFEvent], path: Path) -> Path:
    path.write_text(json.dumps([asdict(e) for e in events], indent=2))
    return path


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ForexFactory calendar scraper")
    parser.add_argument("--weeks-back", type=int, default=8,
                        help="Number of past weeks to include (plus current)")
    parser.add_argument("--sleep", type=float, default=1.5,
                        help="Sleep between fetches (politeness)")
    parser.add_argument("--out-dir", default=str(DEFAULT_DIR))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    events = scrape_weeks(weeks_back=args.weeks_back, sleep_s=args.sleep)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = save_csv(events, out_dir / f"forexfactory_{ts}.csv")
    json_path = save_json(events, out_dir / f"forexfactory_{ts}.json")
    high = sum(1 for e in events if e.impact == "high")
    medium = sum(1 for e in events if e.impact == "medium")
    log.info("Saved %d events (high=%d medium=%d) → %s",
             len(events), high, medium, csv_path)


if __name__ == "__main__":
    main()
