"""
Fetch Terna's available generating capacity of the units of 100 MW or more
(public API, Adequacy):

  effective   aggregate-effective-available-capacity: the final hourly
              available capacity, published the day after
  expected    expected-available-capacity: the hourly capacity the
              operators expect to have available, from the past to about
              two months ahead (by operator, summed here)

Both come per macro-area (Nord, Sud_Isole), plant type and prevailing fuel;
they are summed into the sources of the unavailable capacity chart:
  gas         combined cycle, turbogas, conventional steam on natural gas
  coal        conventional steam on coal
  other       conventional steam on oil products or other fuels, other plants
  hydro       hydroelectric

Series "<kind>|<macro-area>|<source>" (macro-area NORD or SUD, the latter
for the south and the islands), hourly and daily (average), MW, Italian
market time.  Output: app/data/available_capacity.json.

Terna allows 300 calls a day per key: routine runs re-read the last
LOOKBACK_DAYS of effective capacity and the expected capacity from
yesterday to EXPECTED_DAYS ahead, then extend the history back towards
HISTORY_START, CATCH_UP_CALLS at most per run, until a request comes back
empty (the start of Terna's history, kept in the output so that later runs
do not ask again).  The odd day Terna leaves out is not asked for again.
The key is TERNA_KEY_3 / TERNA_SECRET_3, else TERNA_KEY / TERNA_SECRET
(used up or not accepted), which the history does not use.
"""

import argparse
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import compact
from entsoe_api import day_chunks, market_today, merge_resolutions, parse_date, point_label
from fetch_terna import MAIN_KEY, THIRD_KEY, Client

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "data", "available_capacity.json")

ENDPOINTS = {
    "effective": ("/adequacy/v1.0/aggregate-effective-available-capacity", "aggregate_available_capacity"),
    "expected": ("/adequacy/v1.0/expected-available-capacity", "expected_available_capacity"),
}
MACRO_AREAS = {"NORD": "NORD", "SUD_ISOLE": "SUD"}
SOURCES = ("gas", "coal", "other", "hydro")

HISTORY_START = parse_date("2025-01-01")
LOOKBACK_DAYS = 7
EXPECTED_DAYS = 30
# A month of effective capacity came back cut to 25 days: a week per request.
CHUNK_DAYS = {"effective": 7, "expected": 14}
CATCH_UP_CALLS = 100

RESOLUTIONS = ("hourly", "daily")


def source_of(plant_type, fuel):
    plant_type, fuel = (plant_type or "").upper(), (fuel or "").upper()
    if plant_type in ("TERMICO CICLO COMBINATO", "TERMICO TURBOGAS"):
        return "gas"
    if plant_type == "TERMICO TRADIZIONALE":
        return {"CARBONE": "coal", "GAS NATURALE": "gas"}.get(fuel, "other")
    if plant_type == "IDROELETTRICO":
        return "hydro"
    return "other"


def records(kind, rows):
    """Hourly point records summed per macro-area and source."""
    totals = defaultdict(float)
    for row in rows:
        area = MACRO_AREAS.get(str(row.get("macroarea", "")).upper())
        value = row.get("available_capacity_MW")
        if area is None or value in (None, ""):
            continue
        local = datetime.strptime(row["market_date"][:19], "%Y-%m-%d %H:%M:%S")
        sign = 1 if row.get("offset", "+01:00").startswith("+") else -1
        hours, minutes = (int(part) for part in row.get("offset", "+01:00")[1:].split(":"))
        instant = (local - sign * timedelta(hours=hours, minutes=minutes)).replace(tzinfo=timezone.utc)
        day, label = point_label(instant)
        group = f"{kind}|{area}|{source_of(row.get('plant_type'), row.get('prevailing_fuel'))}"
        totals[(group, day, label)] += float(str(value).replace(",", "."))
    return [{"group": group, "date": day, "time": label, "minutes": 60, "value": round(value, 1)}
            for (group, day, label), value in totals.items()]


class Fetcher:
    """Counts the calls."""

    def __init__(self, client):
        self.client = client
        self.calls = 0

    def get(self, kind, first, last):
        path, key = ENDPOINTS[kind]
        self.calls += 1
        return self.client.get(path, {"dateFrom": first.strftime("%d/%m/%Y"),
                                      "dateTo": last.strftime("%d/%m/%Y")}).get(key) or []

    @property
    def own_key(self):
        """Still on the first key: the history is not filled with the fallback's calls."""
        return self.client.key_name == self.client.first_key


def fetch(fetcher, kind, first, last):
    out = []
    for chunk_first, chunk_last in day_chunks(first, last, CHUNK_DAYS[kind]):
        print(f"  {kind} {chunk_first} -> {chunk_last}")
        out += records(kind, fetcher.get(kind, chunk_first, chunk_last))
    return out


def first_day(series, kind):
    days = [row["date"] for group, rows in series["daily"].items() if group.startswith(f"{kind}|") for row in rows]
    return parse_date(min(days)) if days else None


def catch_up(fetcher, series, history_start, budget):
    """
    Extend each kind's history back from its first day, a request after the
    other, until the budget or the start of Terna's history (a request that
    comes back empty; its first day goes into history_start).
    """
    for kind in ENDPOINTS:
        start = max(HISTORY_START, parse_date(history_start.get(kind, HISTORY_START.isoformat())))
        while fetcher.calls < budget and fetcher.own_key:
            first_have = first_day(series, kind)
            if first_have is None or first_have <= start:
                break
            last = first_have - timedelta(days=1)
            new = fetch(fetcher, kind, max(last - timedelta(days=CHUNK_DAYS[kind] - 1), start), last)
            if not new:
                print(f"  {kind}: Terna has nothing before {first_have}")
                history_start[kind] = first_have.isoformat()
                break
            series = merge(series, new)
        print(f"  {kind}: history from {first_day(series, kind)}")
    return series


def load_existing():
    """The series, and per kind the day before which Terna has nothing."""
    series = {resolution: {} for resolution in RESOLUTIONS}
    if not os.path.exists(OUTPUT_PATH):
        return series, {}
    payload = compact.load(OUTPUT_PATH)
    for group, by_resolution in payload.get("series", {}).items():
        for resolution in RESOLUTIONS:
            series[resolution][group] = by_resolution.get(resolution, [])
    return series, payload.get("history_start", {})


def merge(series, new):
    if not new:
        return series
    merged = merge_resolutions({**series, "quarter_hourly": {}}, new, ("quarter_hourly",) + RESOLUTIONS,
                               daily="mean")
    groups = {record["group"] for record in new}
    out = {resolution: dict(series[resolution]) for resolution in RESOLUTIONS}
    for resolution in RESOLUTIONS:
        out[resolution].update({group: rows for group, rows in merged[resolution].items() if group in groups})
    return out


def build_output(series, history_start):
    groups = sorted({group for resolution in RESOLUTIONS for group in series[resolution]})
    effective_days = [row["date"] for group in groups if group.startswith("effective|")
                      for row in series["daily"].get(group, [])]
    return {
        "source": "Terna public API (Adequacy): aggregate effective and expected available capacity",
        "description": __doc__.split("\n\n")[0].strip(),
        "macro_areas": {"NORD": "Nord", "SUD": "Sud e isole"},
        "sources": list(SOURCES),
        "effective_until": max(effective_days) if effective_days else None,
        "history_start": history_start,
        "series": {group: {resolution: compact.encode_series(series[resolution].get(group, []), resolution)
                           for resolution in RESOLUTIONS} for group in groups},
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch Terna's effective and expected available capacity.")
    parser.add_argument("--start", help="First day to (re)fetch (YYYY-MM-DD); default: the routine window")
    parser.add_argument("--end", help="Last day for --start (default: yesterday for effective)")
    parser.add_argument("--catch-up-calls", type=int, default=CATCH_UP_CALLS)
    args = parser.parse_args()

    today = market_today()
    fetcher = Fetcher(Client(THIRD_KEY, MAIN_KEY))
    series, history_start = load_existing()

    if args.start:
        first = parse_date(args.start)
        last = parse_date(args.end) if args.end else today - timedelta(days=1)
        series = merge(series, fetch(fetcher, "effective", first, min(last, today - timedelta(days=1))))
        expected_last = parse_date(args.end) if args.end else today + timedelta(days=EXPECTED_DAYS)
        series = merge(series, fetch(fetcher, "expected", first, expected_last))
    else:
        series = merge(series, fetch(fetcher, "effective", today - timedelta(days=LOOKBACK_DAYS),
                                     today - timedelta(days=1)))
        series = merge(series, fetch(fetcher, "expected", today - timedelta(days=1),
                                     today + timedelta(days=EXPECTED_DAYS)))
        series = catch_up(fetcher, series, history_start, fetcher.calls + args.catch_up_calls)

    for group in sorted(series["daily"]):
        days = series["daily"][group]
        if days:
            print(f"  {group}: {len(days)} days, {days[0]['date']} -> {days[-1]['date']}")
    compact.dump(build_output(series, history_start), OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH} ({os.path.getsize(OUTPUT_PATH) // 1024:,} KB), {fetcher.calls} Terna calls")


if __name__ == "__main__":
    main()
