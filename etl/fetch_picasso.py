"""
Fetch the cross-border marginal prices (CBMP) of PICASSO, the European aFRR
platform, from TransnetBW, which publishes them for the platform: one CSV
per day with every TSO's up (POS) and down (NEG) price for each 4-second
optimisation cycle (21,600 a day, 225 a quarter-hour, ~5.6 MB).  Only
aggregates are kept, for Italy (Terna) and its neighbours in PICASSO:
Austria (APG), Slovenia (ELES), France (RTE) and Greece (IPTO); Switzerland
(Swissgrid) is in the feed without prices.

In each cycle a TSO has an up price only (aFRR activated upward), a down
price only (downward), or the same price both ways (no activation, see
PICASSO's algorithm description).  Series "<country>|<name>", per
quarter-hour, hour and day, Italian market time labelled by elapsed time
since local midnight:

  share_up, share_down, share_none   % of the cycles with a price
  price_up, price_down               average CBMP of the upward / downward
                                     cycles, EUR/MWh
  coupled                            neighbours: % of the cycles both priced
                                     in which they had Italy's prices

ENTSO-E's average of Italy's CBMP (IF aFRR 3.16, see fetch_balancing.py)
takes every cycle with a price in that direction, those without activation
included.

Output: app/data/picasso.json.  Routine runs fetch yesterday and today (so
far), then up to CATCH_UP_DAYS missing days back to HISTORY_START, the day
Italy joined PICASSO.
"""

import argparse
import csv
import io
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests

import compact
from entsoe_api import market_today, parse_date, point_label

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "data", "picasso.json")
API_URL = "https://api.transnetbw.de/picasso-cbmp/csv"

# Country -> the TSO's code in the feed.
COUNTRIES = {"IT": "TERNA", "AT": "APG", "SI": "ELES", "FR": "RTE", "CH": "SG", "GR": "ADMIE"}
REFERENCE = "IT"

HISTORY_START = date(2025, 11, 25)
CATCH_UP_DAYS = 40
# A day takes ~10 s to come; a few at a time.
PARALLEL_REQUESTS = 3
RETRIES = 4

RESOLUTIONS = ("quarter_hourly", "hourly", "daily")
# Per country and quarter-hour: up, down and no-activation cycles, the sums
# of the up and down prices, cycles priced for Italy too, those with Italy's prices.
UP, DOWN, NONE, SUM_UP, SUM_DOWN, PAIRS, SAME = range(7)


def download(day):
    """The day's CSV, or None when TransnetBW has none."""
    for attempt in range(RETRIES + 1):
        try:
            response = requests.get(API_URL, params={"date": day.isoformat(), "lang": "en"}, timeout=180)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.content.decode("utf-8-sig")
        except requests.RequestException as error:
            if attempt == RETRIES:
                raise
            print(f"    {day}: {error}; retrying")
            time.sleep(30 * (attempt + 1))


def number(text):
    return None if text in ("", "N/A") else float(text)


def quarter_sums(text):
    """(country, UTC quarter-hour) -> the counts and sums above, from a day's CSV."""
    rows = csv.reader(io.StringIO(text), delimiter=";")
    header = next(rows)
    columns = {country: (header.index(f"{tso}_POS"), header.index(f"{tso}_NEG"))
               for country, tso in COUNTRIES.items() if f"{tso}_POS" in header and f"{tso}_NEG" in header}
    sums = defaultdict(lambda: [0] * 7)
    for row in rows:
        if not row:
            continue
        instant = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        quarter = instant.replace(minute=instant.minute // 15 * 15, second=0, microsecond=0)
        prices = {}
        for country, (up_column, down_column) in columns.items():
            up, down = number(row[up_column]), number(row[down_column])
            if up is None and down is None:
                continue
            prices[country] = (up, down)
            entry = sums[(country, quarter)]
            if up is not None and down is not None:
                entry[NONE] += 1
            elif up is not None:
                entry[UP] += 1
                entry[SUM_UP] += up
            else:
                entry[DOWN] += 1
                entry[SUM_DOWN] += down
        italy = prices.get(REFERENCE)
        if italy:
            for country, pair in prices.items():
                if country != REFERENCE:
                    sums[(country, quarter)][PAIRS] += 1
                    sums[(country, quarter)][SAME] += pair == italy
    return sums


def series_rows(sums):
    """{group: {resolution: rows}} from the quarter-hour sums (hours and days add them up)."""
    buckets = {resolution: defaultdict(lambda: [0] * 7) for resolution in RESOLUTIONS}
    for (country, quarter), entry in sums.items():
        day, quarter_label = point_label(quarter)
        hour_label = point_label(quarter.replace(minute=0))[1]
        for resolution, label in (("quarter_hourly", quarter_label), ("hourly", hour_label), ("daily", None)):
            bucket = buckets[resolution][(country, day, label)]
            for index, value in enumerate(entry):
                bucket[index] += value
    out = defaultdict(lambda: {resolution: [] for resolution in RESOLUTIONS})
    for resolution, items in buckets.items():
        for (country, day, label), entry in items.items():
            cycles = entry[UP] + entry[DOWN] + entry[NONE]
            values = {
                "share_up": 100 * entry[UP] / cycles,
                "share_down": 100 * entry[DOWN] / cycles,
                "share_none": 100 * entry[NONE] / cycles,
                "price_up": entry[SUM_UP] / entry[UP] if entry[UP] else None,
                "price_down": entry[SUM_DOWN] / entry[DOWN] if entry[DOWN] else None,
                "coupled": 100 * entry[SAME] / entry[PAIRS] if entry[PAIRS] else None,
            }
            for name, value in values.items():
                if value is None:
                    continue
                row = {"date": day, "value": round(value, 2 if name.startswith("price") else 1)}
                if label is not None:
                    row["time"] = label
                out[f"{country}|{name}"][resolution].append(row)
    return out


def fetch_day(day):
    text = download(day)
    rows = series_rows(quarter_sums(text)) if text else {}
    print(f"  {day}: {'no file' if text is None else f'{len(rows)} series'}")
    return day, rows


def load_existing():
    if not os.path.exists(OUTPUT_PATH):
        return {}, set()
    payload = compact.load(OUTPUT_PATH)
    series = {group: {resolution: list(by_resolution.get(resolution, [])) for resolution in RESOLUTIONS}
              for group, by_resolution in payload.get("series", {}).items()}
    return series, set(payload.get("days_without_italy", []))


def merge(series, fetched):
    """Replace the rows of the fetched days ({day: {group: {resolution: rows}}}) in every series."""
    stamps = {day.isoformat() for day in fetched}
    for by_resolution in series.values():
        for resolution in RESOLUTIONS:
            by_resolution[resolution] = [row for row in by_resolution[resolution] if row["date"] not in stamps]
    for rows in fetched.values():
        for group, by_resolution in rows.items():
            target = series.setdefault(group, {resolution: [] for resolution in RESOLUTIONS})
            for resolution in RESOLUTIONS:
                target[resolution] += by_resolution[resolution]
    for by_resolution in series.values():
        for resolution in RESOLUTIONS:
            by_resolution[resolution].sort(key=lambda row: (row["date"], row.get("time", "")))
    return series


def italy_days(series):
    return {row["date"] for row in series.get(f"{REFERENCE}|share_up", {}).get("daily", [])}


def build_output(series, days_without_italy):
    return {
        "source": "TransnetBW, PICASSO cross-border marginal prices (api.transnetbw.de/picasso-cbmp)",
        "description": __doc__.split("\n\n")[0].strip(),
        "countries": {country: tso for country, tso in COUNTRIES.items()},
        "days_without_italy": sorted(days_without_italy),
        "series": {group: {resolution: compact.encode_series(by_resolution[resolution], resolution)
                           for resolution in RESOLUTIONS}
                   for group, by_resolution in sorted(series.items()) if by_resolution["daily"]},
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch PICASSO's cross-border marginal prices (TransnetBW).")
    parser.add_argument("--start", help="First day to (re)fetch (YYYY-MM-DD); default: yesterday")
    parser.add_argument("--end", help="Last day (default: today)")
    parser.add_argument("--catch-up-days", type=int, default=CATCH_UP_DAYS)
    args = parser.parse_args()

    today = market_today()
    first = parse_date(args.start) if args.start else today - timedelta(days=1)
    last = min(parse_date(args.end), today) if args.end else today
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]

    series, days_without_italy = load_existing()
    if not args.start:
        have = italy_days(series) | days_without_italy
        missing = [HISTORY_START + timedelta(days=n) for n in range((first - HISTORY_START).days)]
        missing = [day for day in missing if day.isoformat() not in have]
        print(f"{len(missing)} days of history missing; fetching up to {args.catch_up_days}")
        days += missing[::-1][:args.catch_up_days]

    print(f"Fetching {len(days)} days from TransnetBW")
    with ThreadPoolExecutor(PARALLEL_REQUESTS) as pool:
        fetched = dict(pool.map(fetch_day, days))
    series = merge(series, fetched)
    for day, rows in fetched.items():
        # A past day without Terna's prices is not asked for again.
        if f"{REFERENCE}|share_up" not in rows and day < today - timedelta(days=1):
            days_without_italy.add(day.isoformat())
        else:
            days_without_italy.discard(day.isoformat())

    for group in sorted(series):
        daily = series[group]["daily"]
        if daily and group.endswith(("|share_up", "|coupled")):
            print(f"  {group}: {len(daily)} days, {daily[0]['date']} -> {daily[-1]['date']}")
    compact.dump(build_output(series, days_without_italy), OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH} ({os.path.getsize(OUTPUT_PATH) // 1024:,} KB)")


if __name__ == "__main__":
    main()
