"""
Fetch the ENTSO-E series that the prices and flows map (Maps > Prices and
flows) shows and no other script fetches, into app/data/map_prices.json:

  <ZONE>|day_ahead        Energy Prices 12.1.D (A44) of the seven Italian
                          bidding zones: the day-ahead auction (contract A01,
                          sequence 1), as fetch_neighbour_prices.py reads the
                          neighbours'.  The public version of the page shows
                          these (GME's zonal prices stay out of it).
  <AREA>|imbalance        Imbalance Prices 17.1.G (A85) of the neighbouring
  <AREA>|imbalance_short  control areas (France, Switzerland, Austria,
                          Slovenia, Greece, Montenegro; Malta has no
                          imbalance settlement on the platform):
                          "imbalance" is the price of the excess balance
                          category (A04), the insufficient balance price (A05)
                          only filling its gaps, as fetch_balancing.py reads
                          Italy's; "imbalance_short" is A05 where an area
                          publishes two prices (France; Switzerland until
                          January 2026) and is left out where it equals
                          "imbalance" throughout.
  <AREA>|generation       the neighbours' generation, MW: Actual Generation
                          per Production Type 16.1.B&C (A75), the production
                          types summed (hourly values over their quarter-
                          hours; up to the last quarter-hour every type has
                          reached that day, a type without a value there
                          counting as 0), and the same areas and Malta;
  <AREA>|load             Actual Total Load 6.1.A&B (A65), MW.

(The Italian zones' generation and load are in zones/<ZONE>/
generation_load.json, fetch_entsoe.py.)  An area that publishes nothing is
simply absent; a series is read from 1 January 2025 the first time
(history_read lists the series read so: one an area never publishes is not
asked for its whole history again), then over the last LOOKBACK_DAYS.  Hourly = average of the quarter-hours;
daily = average of the hours.  Italian market time (Europe/Rome), labelled by
elapsed time since local midnight like GME periods.  Quarter-hourly series
start at quarter_hourly_native_from; before it the page repeats the hourly
value.

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
    label_minutes,
    market_today,
    merge_resolutions,
    minutes_label,
    parse_date,
    parse_points,
    request_entsoe,
    to_api_datetime,
    to_quarter_hourly,
)
from fetch_balancing import expand_points, number, point_records
from fetch_crossborder import BORDERS
from fetch_entsoe import is_generation, psr_type
from fetch_neighbour_prices import DEFAULT_HISTORY_START, PT15_START, finest_resolution, is_day_ahead
from zones import ZONE_DOMAINS, ZONE_NAMES

# ============================================================================
# CONFIGURATION
# ============================================================================

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "data", "map_prices.json")

# Malta has no imbalance prices on the platform.
IMBALANCE_AREAS = {code: BORDERS[code] for code in ("FR", "CH", "AT", "SI", "GR", "ME")}
SYSTEM_AREAS = {code: BORDERS[code] for code in ("FR", "CH", "AT", "SI", "GR", "ME", "MT")}

# Days per request: a year of day-ahead prices; a month of imbalance prices
# (15-minute points, two categories) and of load; a week of generation (one
# series per production type).
CHUNK_DAYS = {"day_ahead": 183, "imbalance": 31, "generation": 7, "load": 31}

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


def generation_records(root, area):
    """
    An area's generation per quarter-hour, its production types summed.
    Each day goes up to the last quarter-hour every type has reached (so a
    day still being published does not end in a dip); a type without a value
    before that counts as 0.
    """
    by_type = to_quarter_hourly(parse_points(root, group_of=psr_type, keep=is_generation))
    days = defaultdict(dict)   # date -> type -> {quarter: MW}
    for kind, rows in by_type.items():
        for row in rows:
            days[row["date"]].setdefault(kind, {})[label_minutes(row["time"]) // 15] = row["value"]
    records = []
    for day, kinds in sorted(days.items()):
        last = min(max(quarters) for quarters in kinds.values())
        for quarter in range(last + 1):
            records.append({"group": f"{area}|generation", "date": day, "time": minutes_label(quarter * 15), "minutes": 15,
                            "value": round(sum(quarters.get(quarter, 0) for quarters in kinds.values()), 1)})
    return records


def load_records(root, area):
    """An area's actual total load, at its finest resolution."""
    return finest_resolution(parse_points(root, group_of=lambda ts: f"{area}|load"))


JOBS = (
    [(zone, eic, "day_ahead") for zone, eic in ZONE_DOMAINS.items()]
    + [(area, eic, "imbalance") for area, (_, eic) in IMBALANCE_AREAS.items()]
    + [(area, eic, kind) for area, (_, eic) in SYSTEM_AREAS.items() for kind in ("generation", "load")]
)


def request_params(kind, eic, period):
    if kind == "day_ahead":
        return {"documentType": "A44", "in_Domain": eic, "out_Domain": eic, **period}
    if kind == "imbalance":
        return {"documentType": "A85", "controlArea_Domain": eic, **period}
    if kind == "generation":
        return {"documentType": "A75", "processType": "A16", "in_Domain": eic, **period}
    return {"documentType": "A65", "processType": "A16", "outBiddingZone_Domain": eic, **period}


PARSERS = {"day_ahead": day_ahead_records, "imbalance": imbalance_records, "generation": generation_records, "load": load_records}


def download(token, start_of, end_date):
    """
    Point records of every series, and the series a request failed for;
    start_of(area, kind) is the first day to ask for.
    """
    records, failures, requests_made, failed = [], 0, 0, set()

    for area, eic, kind in JOBS:
        for chunk_start, chunk_end in day_chunks(start_of(area, kind), end_date, CHUNK_DAYS[kind]):
            print(f"  {area} {kind}: {chunk_start} -> {chunk_end}")
            requests_made += 1
            period = {"periodStart": to_api_datetime(chunk_start), "periodEnd": to_api_datetime(chunk_end, end_of_day=True)}
            try:
                root = request_entsoe(token, request_params(kind, eic, period))
            except requests.HTTPError as error:
                # One area failing must not stop the others; its data on file is kept.
                failures += 1
                failed.add(f"{area}|{kind}")
                print(f"    WARNING: {error}")
                continue
            if root is None:
                print("    No matching data")
            records += PARSERS[kind](root, area)
            time.sleep(REQUEST_PAUSE_SECONDS)

    if requests_made and failures == requests_made:
        raise RuntimeError("Every request failed.")
    return records, failed


# ============================================================================
# LOAD / SAVE
# ============================================================================


def load_existing():
    """{resolution: {group: rows}}, and the series whose history was read."""
    existing = {resolution: {} for resolution in RESOLUTIONS}
    if not os.path.exists(OUTPUT_PATH):
        return existing, set()
    payload = compact.load(OUTPUT_PATH)
    for resolution in RESOLUTIONS:
        existing[resolution] = dict(payload.get("series", {}).get(resolution, {}))
    # Files written before history_read: every series on file had its history read.
    read = payload.get("history_read")
    return existing, set(read) if read is not None else set(existing["daily"])


def build_output(series, history_read):
    return {
        "source": "ENTSO-E Transparency Platform",
        "description": (
            "Series for the prices and flows map: the day-ahead prices (Energy "
            "Prices 12.1.D, A44, day-ahead auction) of the Italian bidding zones "
            "('ZONE|day_ahead'), EUR/MWh; the imbalance prices (17.1.G, A85) of "
            "the neighbouring control areas ('AREA|imbalance': excess balance "
            "price, A04, the insufficient balance price filling its gaps; "
            "'AREA|imbalance_short': the insufficient balance price, A05, where "
            "an area publishes two), EUR/MWh; the neighbours' generation "
            "('AREA|generation': Actual Generation per Production Type 16.1.B&C, "
            "A75, the types summed) and load ('AREA|load': Actual Total Load "
            "6.1.A&B, A65), MW. Hourly = average of the quarter-hours; daily = "
            "average of the hours. Italian market time (Europe/Rome), labelled by "
            "elapsed time since local midnight like GME periods. Quarter-hourly "
            "series start at quarter_hourly_native_from; earlier quarter-hours "
            "repeat the hourly value."
        ),
        "quarter_hourly_native_from": PT15_START.isoformat(),
        "history_read": sorted(history_read),
        "areas": {**{zone: ZONE_NAMES[zone] for zone in ZONE_DOMAINS},
                  **{area: label for area, (label, _) in SYSTEM_AREAS.items()}},
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
        description="Fetch the ENTSO-E series of the prices and flows map (Italian zones' day-ahead, "
                    "neighbours' imbalance prices, generation and load)."
    )
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--full-history", action="store_true",
                        help="Rebuild from 2025-01-01 instead of merging onto existing data")
    args = parser.parse_args()

    # Tomorrow's day-ahead prices are published around midday.
    end_date = parse_date(args.end) if args.end else market_today() + timedelta(days=1)
    existing, history_read = ({resolution: {} for resolution in RESOLUTIONS}, set()) if args.full_history else load_existing()
    recent = market_today() - timedelta(days=LOOKBACK_DAYS)
    first_reads = set()

    def start_of(area, kind):
        """--start if given; else the history start the first time a series is asked for, then the lookback."""
        group = f"{area}|{kind}"
        if args.start:
            return max(parse_date(args.start), DEFAULT_HISTORY_START)
        if group in history_read:
            return recent
        first_reads.add(group)
        return DEFAULT_HISTORY_START

    if end_date < DEFAULT_HISTORY_START:
        raise ValueError("End date must not be before the history start.")

    token = os.environ.get("ENTSOE_API_KEY")
    if not token:
        raise RuntimeError("ENTSOE_API_KEY environment variable must be set.")

    print()
    print("=" * 70)
    print("ENTSO-E SERIES FOR THE MAP (zones' day-ahead; neighbours' imbalance, generation, load)")
    print("=" * 70)
    print(f"Up to {end_date}")
    print()

    records, failed = download(token, start_of, end_date)
    # A history read with a failed request is tried again next time.
    history_read |= first_reads - failed
    print(f"  {len(records):,} points")
    series = merge_resolutions(existing, records, RESOLUTIONS, daily="mean")

    for group in sorted(series["daily"]):
        days = series["daily"][group]
        print(f"  {group}: {len(days):,} days" + (f", last {days[-1]['date']}" if days else ""))
    if not any(series["daily"].values()):
        raise RuntimeError("No data downloaded; not writing output.")

    compact.dump(build_output(series, history_read), OUTPUT_PATH)
    print("=" * 70)
    print(f"Wrote: {OUTPUT_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    main()
