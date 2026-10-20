"""
Fetch the ENTSO-E prices that the prices and flows map (Maps > Prices and
flows) shows and no other script fetches, into app/data/map_prices.json:

  <ZONE>|day_ahead        Energy Prices 12.1.D (A44) of the seven Italian
                          bidding zones: the day-ahead auction (contract A01,
                          sequence 1), as fetch_neighbour_prices.py reads the
                          neighbours'.  The map's day-ahead prices are then
                          ENTSO-E's in both versions of the page (GME's zonal
                          prices stay out of the public one).
  <AREA>|imbalance        Imbalance Prices 17.1.G (A85) of the neighbouring
  <AREA>|imbalance_short  control areas (France, Switzerland, Austria,
                          Slovenia, Greece, Montenegro; Malta has no
                          imbalance settlement on the platform):
                          "imbalance" is the price of the excess balance
                          category (A04), the insufficient balance price (A05)
                          only filling its gaps, as fetch_balancing.py reads
                          Italy's; "imbalance_short" is A05 where an area
                          publishes two prices (Switzerland) and is left out
                          where it equals "imbalance" throughout.

An area that publishes nothing is simply absent.  Hourly = average of the
quarter-hours; daily = average of the hours.  Italian market time
(Europe/Rome), labelled by elapsed time since local midnight like GME
periods.  Quarter-hourly series start at quarter_hourly_native_from; before
it the page repeats the hourly price.

The API token is read from the ENTSOE_API_KEY environment variable.
"""

import argparse
import os
import time
from collections import defaultdict
from datetime import timedelta

import requests

import compact
from entsoe_api import (
    REQUEST_PAUSE_SECONDS,
    day_chunks,
    market_today,
    merge_resolutions,
    parse_date,
    parse_points,
    request_entsoe,
    to_api_datetime,
)
from fetch_balancing import expand_points, number, point_records
from fetch_crossborder import BORDERS
from fetch_neighbour_prices import DEFAULT_HISTORY_START, PT15_START, finest_resolution, is_day_ahead
from zones import ZONE_DOMAINS, ZONE_NAMES

# ============================================================================
# CONFIGURATION
# ============================================================================

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "data", "map_prices.json")

# Malta has no imbalance prices on the platform.
IMBALANCE_AREAS = {code: BORDERS[code] for code in ("FR", "CH", "AT", "SI", "GR", "ME")}

# The API accepts a year of day-ahead prices per request; imbalance prices
# are asked for a month at a time (15-minute points, two categories).
DAY_AHEAD_CHUNK_DAYS = 183
IMBALANCE_CHUNK_DAYS = 31

# Incremental runs re-download this many days to pick up corrections.
LOOKBACK_DAYS = 7

RESOLUTIONS = ("quarter_hourly", "hourly", "daily")


# ============================================================================
# DOWNLOAD
# ============================================================================


def _text(elem, tag):
    child = elem.find(f"{{*}}{tag}")
    return child.text if child is not None else None


def day_ahead_records(root, zone):
    """The day-ahead auction's prices of a zone, at its finest resolution."""
    return finest_resolution(parse_points(root, group_of=lambda ts: f"{zone}|day_ahead", keep=is_day_ahead))


def imbalance_records(root, area):
    """
    Point records of an area's imbalance prices: "imbalance" (A04, A05
    filling its gaps) and "imbalance_short" (A05) where it differs.
    """
    by_category = defaultdict(dict)
    for ts in root.findall(".//{*}TimeSeries") if root is not None else []:
        for instant, seconds, (category, price) in expand_points(
                ts, lambda p: (_text(p, "imbalance_Price.category"), number(p, "imbalance_Price.amount"))):
            if price is not None:
                by_category[category][instant] = (seconds, price)
    long_prices, short_prices = by_category.get("A04", {}), by_category.get("A05", {})
    single = dict(long_prices)
    for instant, value in short_prices.items():
        single.setdefault(instant, value)
    records = point_records(f"{area}|imbalance", ((instant, seconds, price) for instant, (seconds, price) in single.items()))
    if any(long_prices.get(instant, value)[1] != value[1] for instant, value in short_prices.items()):
        records += point_records(f"{area}|imbalance_short",
                                 ((instant, seconds, price) for instant, (seconds, price) in short_prices.items()))
    return records


def download(token, start_date, end_date):
    """Point records of every series, and how many requests failed of how many."""
    jobs = [(zone, eic, "day_ahead") for zone, eic in ZONE_DOMAINS.items()]
    jobs += [(area, eic, "imbalance") for area, (_, eic) in IMBALANCE_AREAS.items()]
    records, failures, requests_made = [], 0, 0

    for area, eic, kind in jobs:
        chunk_days = DAY_AHEAD_CHUNK_DAYS if kind == "day_ahead" else IMBALANCE_CHUNK_DAYS
        for chunk_start, chunk_end in day_chunks(start_date, end_date, chunk_days):
            print(f"  {area} {kind}: {chunk_start} -> {chunk_end}")
            requests_made += 1
            period = {"periodStart": to_api_datetime(chunk_start), "periodEnd": to_api_datetime(chunk_end, end_of_day=True)}
            params = ({"documentType": "A44", "in_Domain": eic, "out_Domain": eic, **period} if kind == "day_ahead"
                      else {"documentType": "A85", "controlArea_Domain": eic, **period})
            try:
                root = request_entsoe(token, params)
            except requests.HTTPError as error:
                # One area failing must not stop the others; its data on file is kept.
                failures += 1
                print(f"    WARNING: {error}")
                continue
            if root is None:
                print("    No matching data")
            records += day_ahead_records(root, area) if kind == "day_ahead" else imbalance_records(root, area)
            time.sleep(REQUEST_PAUSE_SECONDS)

    if requests_made and failures == requests_made:
        raise RuntimeError("Every price request failed.")
    return records


# ============================================================================
# LOAD / SAVE
# ============================================================================


def load_existing():
    """{resolution: {group: rows}}."""
    existing = {resolution: {} for resolution in RESOLUTIONS}
    if not os.path.exists(OUTPUT_PATH):
        return existing
    payload = compact.load(OUTPUT_PATH)
    for resolution in RESOLUTIONS:
        existing[resolution] = dict(payload.get("series", {}).get(resolution, {}))
    return existing


def build_output(series):
    return {
        "source": "ENTSO-E Transparency Platform",
        "description": (
            "Prices for the prices and flows map, EUR/MWh: the day-ahead prices "
            "(Energy Prices 12.1.D, A44, day-ahead auction) of the Italian bidding "
            "zones ('ZONE|day_ahead') and the imbalance prices (17.1.G, A85) of the "
            "neighbouring control areas ('AREA|imbalance': excess balance price, "
            "A04, the insufficient balance price filling its gaps; "
            "'AREA|imbalance_short': the insufficient balance price, A05, where an "
            "area publishes two). Hourly = average of the quarter-hours; daily = "
            "average of the hours. Italian market time (Europe/Rome), labelled by "
            "elapsed time since local midnight like GME periods. Quarter-hourly "
            "series start at quarter_hourly_native_from; earlier quarter-hours "
            "repeat the hourly price."
        ),
        "quarter_hourly_native_from": PT15_START.isoformat(),
        "areas": {**{zone: ZONE_NAMES[zone] for zone in ZONE_DOMAINS},
                  **{area: label for area, (label, _) in IMBALANCE_AREAS.items()}},
        "series": {
            resolution: {
                group: compact.encode_series(
                    rows, resolution, skip_before=PT15_START.isoformat() if resolution == "quarter_hourly" else None)
                for group, rows in sorted(series[resolution].items())
            }
            for resolution in RESOLUTIONS
        },
    }


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Fetch the ENTSO-E prices of the prices and flows map (Italian zones' day-ahead, neighbours' imbalance)."
    )
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--full-history", action="store_true",
                        help="Rebuild from 2025-01-01 instead of merging onto existing data")
    args = parser.parse_args()

    # Tomorrow's day-ahead prices are published around midday.
    end_date = parse_date(args.end) if args.end else market_today() + timedelta(days=1)
    if args.start:
        start_date = parse_date(args.start)
    elif args.full_history or not os.path.exists(OUTPUT_PATH):
        start_date = DEFAULT_HISTORY_START
    else:
        start_date = market_today() - timedelta(days=LOOKBACK_DAYS)
    start_date = max(start_date, DEFAULT_HISTORY_START)
    if end_date < start_date:
        raise ValueError("End date must not be before start date.")

    token = os.environ.get("ENTSOE_API_KEY")
    if not token:
        raise RuntimeError("ENTSOE_API_KEY environment variable must be set.")

    print()
    print("=" * 70)
    print("ENTSO-E PRICES FOR THE MAP (Italian zones' day-ahead, neighbours' imbalance)")
    print("=" * 70)
    print(f"Date range: {start_date} -> {end_date}")
    print()

    existing = {resolution: {} for resolution in RESOLUTIONS} if args.full_history else load_existing()
    records = download(token, start_date, end_date)
    print(f"  {len(records):,} points")
    series = merge_resolutions(existing, records, RESOLUTIONS, daily="mean")

    for group in sorted(series["daily"]):
        days = series["daily"][group]
        print(f"  {group}: {len(days):,} days" + (f", last {days[-1]['date']}" if days else ""))
    if not any(series["daily"].values()):
        raise RuntimeError("No price data downloaded; not writing output.")

    compact.dump(build_output(series), OUTPUT_PATH)
    print("=" * 70)
    print(f"Wrote: {OUTPUT_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    main()
