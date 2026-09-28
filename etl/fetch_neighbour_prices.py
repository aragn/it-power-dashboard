"""
Fetch the day-ahead prices of the bidding zones neighbouring Italy from the
ENTSO-E Transparency Platform: Energy Prices [12.1.D], documentType A44.

The zones are the neighbouring control areas used for the cross-border
exchanges (fetch_crossborder.BORDERS).  Some of them may publish no
day-ahead price (e.g. Malta has no day-ahead market); they are simply
absent from the output.

A44 documents can hold more than the day-ahead auction's prices, and some
zones publish the same day at two resolutions (hourly and 15-minute), so:
  - only TimeSeries with contract type A01 (day-ahead) and classification
    sequence 1 are kept (either may be absent, which counts as a match);
  - for each zone and day only the finest resolution is kept.

Hourly prices are the average of the quarter-hours and daily prices the
average of the hours.  Output: app/data/neighbour_prices.json, in EUR/MWh
on the Italian market-time basis of the other data.  Quarter-hourly series
are stored from 1 October 2025 (15-minute day-ahead market time unit);
before that the dashboard repeats the hourly price.

The API token is read from the ENTSOE_API_KEY environment variable.
"""

import argparse
import os
import time
from datetime import date, timedelta

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
from fetch_crossborder import BORDERS

# ============================================================================
# CONFIGURATION
# ============================================================================

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "app",
    "data",
    "neighbour_prices.json",
)

DEFAULT_HISTORY_START = date(2025, 1, 1)
PT15_START = date(2025, 10, 1)

# The API accepts up to a year per request; one price series per request.
CHUNK_DAYS = 183

# Incremental runs re-download this many days to pick up corrections.
LOOKBACK_DAYS = 7

RESOLUTIONS = ("quarter_hourly", "hourly", "daily")


# ============================================================================
# DOWNLOAD
# ============================================================================


def _text(ts, tag):
    elem = ts.find(f"{{*}}{tag}")
    return elem.text if elem is not None else None


def is_day_ahead(ts):
    """The day-ahead auction's series: contract A01, sequence 1, in EUR."""
    return (
        _text(ts, "contract_MarketAgreement.type") in (None, "A01")
        and _text(ts, "classificationSequence_AttributeInstanceComponent.position") in (None, "1")
        and _text(ts, "currency_Unit.name") in (None, "EUR")
    )


def finest_resolution(records):
    """Where a zone has a day at two resolutions, keep only the finest."""
    finest = {}
    for row in records:
        key = (row["group"], row["date"])
        finest[key] = min(finest.get(key, row["minutes"]), row["minutes"])
    return [row for row in records if row["minutes"] == finest[(row["group"], row["date"])]]


def download_prices(token, start_date, end_date):
    """Point records grouped by country code."""
    records = []
    failures = 0
    requests_made = 0

    for country, (label, eic) in BORDERS.items():
        for chunk_start, chunk_end in day_chunks(start_date, end_date, CHUNK_DAYS):
            print(f"  {country} ({label}): {chunk_start} -> {chunk_end}")
            requests_made += 1

            try:
                root = request_entsoe(token, {
                    "documentType": "A44",
                    "in_Domain": eic,
                    "out_Domain": eic,
                    "periodStart": to_api_datetime(chunk_start),
                    "periodEnd": to_api_datetime(chunk_end, end_of_day=True),
                })
            except requests.HTTPError as error:
                # One zone failing must not stop the others; its existing
                # data is kept.
                failures += 1
                print(f"    WARNING: {error}")
                continue

            points = parse_points(root, group_of=lambda ts, c=country: c, keep=is_day_ahead)
            if root is None:
                print("    No matching data")
            records.extend(points)
            time.sleep(REQUEST_PAUSE_SECONDS)

    if requests_made and failures == requests_made:
        raise RuntimeError("Every day-ahead price request failed.")

    return finest_resolution(records)


# ============================================================================
# LOAD / SAVE
# ============================================================================


def load_existing():
    """{resolution: {country: rows}}."""
    existing = {resolution: {} for resolution in RESOLUTIONS}
    if not os.path.exists(OUTPUT_PATH):
        return existing

    payload = compact.load(OUTPUT_PATH)
    for resolution in RESOLUTIONS:
        existing[resolution] = dict(payload.get("prices", {}).get(resolution, {}))
    return existing


def build_output(prices):
    return {
        "source": "ENTSO-E Transparency Platform",
        "description": (
            "Day-ahead prices (Energy Prices 12.1.D, A44) of the bidding "
            "zones neighbouring Italy, EUR/MWh. Hourly = average of the "
            "quarter-hours; daily = average of the hours. Italian market "
            "time (Europe/Rome), labelled by elapsed time since local "
            "midnight like GME periods. Quarter-hourly series start at "
            "quarter_hourly_native_from; earlier quarter-hours repeat the "
            "hourly price."
        ),
        "quarter_hourly_native_from": PT15_START.isoformat(),
        "countries": {country: label for country, (label, _) in BORDERS.items()},
        "prices": {
            resolution: {
                country: compact.encode_series(
                    rows,
                    resolution,
                    skip_before=PT15_START.isoformat() if resolution == "quarter_hourly" else None,
                )
                for country, rows in sorted(prices[resolution].items())
            }
            for resolution in RESOLUTIONS
        },
    }


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Fetch ENTSO-E day-ahead prices of Italy's neighbouring bidding zones."
    )
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="Rebuild from 2025-01-01 instead of merging onto existing data",
    )
    args = parser.parse_args()

    # Tomorrow's prices are published around midday.
    end_date = parse_date(args.end) if args.end else market_today() + timedelta(days=1)

    if args.start:
        start_date = parse_date(args.start)
    elif args.full_history:
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
    print("ENTSO-E DAY-AHEAD PRICES (Italy's neighbouring bidding zones)")
    print("=" * 70)
    print(f"Date range: {start_date} -> {end_date}")
    print()

    existing = (
        {resolution: {} for resolution in RESOLUTIONS}
        if args.full_history
        else load_existing()
    )

    records = download_prices(token, start_date, end_date)
    print(f"  {len(records):,} points")

    prices = merge_resolutions(existing, records, RESOLUTIONS, daily="mean")

    for country in BORDERS:
        days = prices["daily"].get(country, [])
        print(f"  {country}: {len(days):,} days" + (f", last {days[-1]['date']}" if days else ""))

    if not any(prices["daily"].values()):
        raise RuntimeError("No day-ahead price data downloaded; not writing output.")

    compact.dump(build_output(prices), OUTPUT_PATH)

    print("=" * 70)
    print(f"Wrote: {OUTPUT_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    main()
