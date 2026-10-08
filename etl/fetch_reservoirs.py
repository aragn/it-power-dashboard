"""
Hydro reservoirs: the energy stored in Italy's water reservoirs and hydro
storage plants, week by week (ENTSO-E 16.1.D, Aggregated Filling Rate of
Water Reservoirs and Hydro Storage Plants: documentType A72, processType
A16, MWh), written to app/data/reservoirs.json:

  {"source", "description", "unit": "MWh", "weeks": [[week start, MWh], ...]}

the week start as the date (Italian time) of the week's first day.  The
Storage tab draws the latest years against the range and the average of
the years before, like the gas storage.

Daily: the weeks of the last LOOKBACK_DAYS (late or revised weeks); the
whole history from FIRST_YEAR when the file is missing or with
--full-history, a year a request.

Credentials: ENTSOE_API_KEY.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from entsoe_api import MARKET_TZ, _child, _text, market_today, request_entsoe, to_api_datetime  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(ROOT, "app", "data", "reservoirs.json")
ITALY = "10YIT-GRTN-----B"
FIRST_YEAR = 2015
LOOKBACK_DAYS = 70
STEPS = {"P7D": timedelta(days=7), "P1D": timedelta(days=1)}


def parse_weeks(root):
    """{week start date (ISO): MWh} of a response."""
    weeks = {}
    if root is None:
        return weeks
    for ts in root.findall(".//{*}TimeSeries"):
        for period in ts.findall("{*}Period"):
            interval = _child(period, "timeInterval")
            start = datetime.strptime(_text(interval, "start"), "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            step = STEPS.get(_text(period, "resolution"), timedelta(days=7))
            for point in period.findall("{*}Point"):
                position = int(_text(point, "position"))
                instant = start + step * (position - 1)
                weeks[instant.astimezone(MARKET_TZ).date().isoformat()] = float(_text(point, "quantity"))
    return weeks


def download(token, first, last):
    """{week start: MWh} of first..last, a year a request."""
    weeks, start = {}, first
    while start <= last:
        end = min(date(start.year, 12, 31), last)
        root = request_entsoe(token, {"documentType": "A72", "processType": "A16", "in_Domain": ITALY,
                                      "periodStart": to_api_datetime(start),
                                      "periodEnd": to_api_datetime(end, end_of_day=True)})
        found = parse_weeks(root)
        print(f"  {start} - {end}: {len(found)} week(s)")
        weeks.update(found)
        start = end + timedelta(days=1)
    return weeks


def load_existing(path=OUTPUT_PATH):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return {day: value for day, value in json.load(handle).get("weeks", [])}


def build_output(weeks):
    return {
        "source": "ENTSO-E Transparency Platform, 16.1.D Aggregated Filling Rate of Water Reservoirs and Hydro Storage Plants",
        "description": "Energy stored in Italy's water reservoirs and hydro storage plants (MWh), week by week; "
                       "the date is the week's first day (Italian time).",
        "unit": "MWh",
        "weeks": [[day, round(weeks[day], 1)] for day in sorted(weeks)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--full-history", action="store_true", help=f"read every week from {FIRST_YEAR}")
    args = parser.parse_args()
    token = os.environ.get("ENTSOE_API_KEY")
    if not token:
        raise SystemExit("ENTSOE_API_KEY is not set")
    existing = {} if args.full_history else load_existing()
    today = market_today()
    first = date(FIRST_YEAR, 1, 1) if not existing else today - timedelta(days=LOOKBACK_DAYS)
    print(f"Hydro reservoirs (16.1.D): {first} - {today}")
    new = download(token, first, today)
    if not new and not existing:
        print("No reservoir data published for Italy: nothing written.")
        return
    weeks = {**existing, **new}
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(build_output(weeks), handle, separators=(",", ":"))
    days = sorted(weeks)
    print(f"Wrote {OUTPUT_PATH}: {len(days)} week(s), {days[0]} - {days[-1]}")


if __name__ == "__main__":
    main()
