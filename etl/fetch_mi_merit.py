"""
MI-A merit orders: the days' public offers of the three intraday auctions
(GME Offers_PublicDomain, MI-A1, MI-A2, MI-A3), written to
app/data/mi_merit/<date>.json.gz with the index of days, auctions,
quarter-hours and files, app/data/mi_merit/index.json.

A day file holds the three auctions of the market day, in the MGP merit
order's format per quarter-hour (prices, supply, demand, others: see
fetch_mgp_merit.py), with one unit list for the three:

  {"date", "market": "MI-A", "units": [...],
   "markets": {"MI-A1": [quarter, ...], "MI-A2": [...], "MI-A3": [...]}}

MI-A1 and MI-A2 (held the day before) cover the whole day, MI-A3 (held on
the day) the afternoon and evening only.  An auction GME has no offers for
is left out of the day.

  fetch_mi_merit.py --date 2026-09-17
  fetch_mi_merit.py [--from 2026-09-01 --to 2026-09-30 --max-days 4]
      the days of the range without the three auctions on file, oldest
      first, at most --max-days a run (three downloads a day, one request
      at a time with a pause between)

Credentials: GME_API_LOGIN / GME_API_PASSWORD.
"""

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_gme_units import REQUEST_PAUSE_SECONDS, get_token, request_offers, rows_of  # noqa: E402
from fetch_mgp_merit import PUBLISHED_AFTER, build, read_day, unit_sources, write_day  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "app", "data", "mi_merit")

MARKETS = ["MI-A1", "MI-A2", "MI-A3"]
MAX_DAYS = 4


def request_market(token, day, market):
    """(segment, file name, bytes) of an auction's offers (the segment is
    the auction's name, as for its prices)."""
    name, content = request_offers(token, day, segment=market)
    return market, name, content


def day_data(day, offers, units):
    """offers: market -> rows (streamed: each auction built as it is read).
    The day's file, one unit list for the three."""
    registry = ({}, [])
    markets = {}
    for market in MARKETS:
        if market not in offers:
            continue
        quarters = build(offers[market], 0, 95, units, registry)["quarters"]
        if quarters:
            markets[market] = quarters
    return {"date": day.isoformat(), "market": "MI-A",
            "source": "GME public offers (Offers_PublicDomain, MI-A1, MI-A2, MI-A3); unit sources from gme_units.json",
            "units": registry[1], "markets": markets}


def write_index(out_dir):
    """index.json: per day, each auction's quarter-hours; the file names."""
    days, files = {}, {}
    for name in sorted(os.listdir(out_dir)):
        if name == "index.json" or not name.endswith((".json", ".json.gz")):
            continue
        data = read_day(os.path.join(out_dir, name))
        days[data["date"]] = {market: [item["time"] for item in quarters]
                              for market, quarters in data["markets"].items()}
        files[data["date"]] = name
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as handle:
        json.dump({"days": days, "files": files}, handle, separators=(",", ":"))
    return days


def missing_days(first, last, out_dir):
    """The days of first..last without all three auctions on file."""
    on_file = {}
    index = os.path.join(out_dir, "index.json")
    if os.path.exists(index):
        with open(index, encoding="utf-8") as handle:
            on_file = json.load(handle).get("days", {})
    days, day = [], first
    while day <= last:
        if len(on_file.get(day.isoformat(), {})) < len(MARKETS):
            days.append(day)
        day += timedelta(days=1)
    return days


def read_days(days, out_dir, max_days):
    login, password = os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"]
    token, units = get_token(login, password), unit_sources()
    done, asked = [], False
    for day in days[:max_days]:
        registry, markets = ({}, []), {}
        for market in MARKETS:
            if asked:
                time.sleep(REQUEST_PAUSE_SECONDS)
            asked = True
            try:
                try:
                    segment, name, content = request_market(token, day, market)
                except PermissionError:
                    token = get_token(login, password)
                    segment, name, content = request_market(token, day, market)
            except Exception as error:  # not out yet, or a failure: the other auctions go on
                print(f"  {day} {market}: {error}")
                continue
            # Built as the rows stream in, into the day's one unit list.
            quarters = build(rows_of(name, content), 0, 95, units, registry)["quarters"]
            print(f"  {day} {market} (segment {segment}): {name}, {len(content) / 1e6:.0f} MB, "
                  f"{len(quarters)} quarter-hours")
            del content
            if quarters:
                markets[market] = quarters
        if not markets:
            continue
        data = {**day_data(day, {}, units), "units": registry[1], "markets": markets}
        path = write_day(out_dir, data)
        print(f"  {day}: {', '.join(markets)}; {len(data['units']):,} units, {os.path.getsize(path) / 1e6:.1f} MB")
        done.append(day)
    return done


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="one day (else the range)")
    parser.add_argument("--from", dest="first", help="first day of the range")
    parser.add_argument("--to", dest="last", help=f"last day (default: {PUBLISHED_AFTER} days ago)")
    parser.add_argument("--max-days", type=int, default=MAX_DAYS)
    parser.add_argument("--out", default=OUT_DIR)
    args = parser.parse_args()

    if args.date:
        days = [date.fromisoformat(args.date)]
    elif args.first:
        last = date.fromisoformat(args.last) if args.last else date.today() - timedelta(days=PUBLISHED_AFTER)
        days = missing_days(date.fromisoformat(args.first), last, args.out)
    else:
        parser.error("--date or --from")
    print(f"{len(days)} day(s) to read, at most {args.max_days} this run")
    os.makedirs(args.out, exist_ok=True)
    done = read_days(days, args.out, args.max_days) if days else []
    on_file = write_index(args.out)
    message = f"MI-A merit orders: {len(done)} day(s) read; {len(on_file)} day(s) on file."
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    # One day asked for by hand must be read; a range may find nothing new
    # (GME has not published the next day yet).
    if args.date and not done:
        raise SystemExit("No MI-A offers read")


if __name__ == "__main__":
    main()
