"""
Fetch the wind and solar generation forecasts for Italy from the ENTSO-E
Transparency Platform and write app/data/res_forecasts.json:

  - Generation Forecasts for Wind and Solar  [14.1.D]  documentType A69
      day-ahead   processType A01
      intraday    processType A40
    (the "current" forecast, A18, is left out: for Italy it repeats the
    intraday one)

Only onshore wind (B19) and solar (B16) are kept; Italy has no offshore
forecast.  The actual generation of both is already in generation_load.json
(16.1.B&C).  Values in MW; hourly = average of the quarter-hours; daily =
sum of the hours (MWh/day), like the actual generation.  Italian market
time, labelled by elapsed time since local midnight like GME periods.

The API token is read from the ENTSOE_API_KEY environment variable.
"""

import argparse
import os
import time
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
from fetch_forecasts import CHUNK_DAYS, DEFAULT_HISTORY_START, LOOKBACK_DAYS, RESOLUTIONS, area_params
from zones import IT_DOMAIN, area_label, run_areas, targets

# ============================================================================
# CONFIGURATION
# ============================================================================

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "app",
    "data",
    "res_forecasts.json",
)

PRODUCTION_TYPES = {"B19": "Wind Onshore", "B16": "Solar"}

FORECASTS = {
    "day_ahead": {
        "label": "Day-ahead wind and solar forecast (14.1.D)",
        "params": {"documentType": "A69", "processType": "A01", "in_Domain": IT_DOMAIN},
    },
    "intraday": {
        "label": "Intraday wind and solar forecast (14.1.D)",
        "params": {"documentType": "A69", "processType": "A40", "in_Domain": IT_DOMAIN},
    },
}


def production_type(ts):
    elem = ts.find("{*}MktPSRType/{*}psrType")
    return elem.text if elem is not None else None


def is_wind_or_solar(ts):
    return production_type(ts) in PRODUCTION_TYPES


# ============================================================================
# DOWNLOAD
# ============================================================================


def download(token, name, start_date, end_date, domain=IT_DOMAIN):
    """Point records of one forecast, or None if every request failed."""
    records = []
    failures = 0
    requests_made = 0

    for chunk_start, chunk_end in day_chunks(start_date, end_date, CHUNK_DAYS):
        print(f"  {name}: {chunk_start} -> {chunk_end}")
        requests_made += 1
        try:
            root = request_entsoe(token, {
                **area_params(FORECASTS[name]["params"], domain),
                "periodStart": to_api_datetime(chunk_start),
                "periodEnd": to_api_datetime(chunk_end, end_of_day=True),
            })
        except requests.HTTPError as error:
            failures += 1
            print(f"    WARNING: {error}")
            continue

        records.extend(parse_points(root, group_of=production_type, keep=is_wind_or_solar))
        time.sleep(REQUEST_PAUSE_SECONDS)

    if requests_made and failures == requests_made:
        return None
    return records


# ============================================================================
# LOAD / SAVE
# ============================================================================


def empty():
    return {resolution: {} for resolution in RESOLUTIONS}


def load_existing(path=OUTPUT_PATH):
    """{forecast: {resolution: {psrType: rows}}}."""
    existing = {name: empty() for name in FORECASTS}
    if not os.path.exists(path):
        return existing

    payload = compact.load(path)
    for name in FORECASTS:
        for resolution in RESOLUTIONS:
            existing[name][resolution] = dict(payload.get(name, {}).get(resolution, {}))
    return existing


def build_output(forecasts, area=f"IT ({IT_DOMAIN})"):
    return {
        "source": "ENTSO-E Transparency Platform",
        "control_area": area,
        "description": (
            f"Wind onshore (B19) and solar (B16) generation forecasts for {area} "
            "(14.1.D, A69): day-ahead (A01) and intraday (A40), MW; hourly = "
            "average of the quarter-hours, daily = sum of the hours (MWh/day). "
            "Italian market time (Europe/Rome), labelled by elapsed time since "
            "local midnight like GME periods."
        ),
        "labels": {name: spec["label"] for name, spec in FORECASTS.items()},
        "production_types": PRODUCTION_TYPES,
        **{
            name: {
                resolution: {
                    group: compact.encode_series(rows, resolution)
                    for group, rows in sorted(series[resolution].items())
                }
                for resolution in RESOLUTIONS
            }
            for name, series in forecasts.items()
        },
    }


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="Fetch ENTSO-E wind and solar forecasts for Italy.")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--full-history", action="store_true",
                        help="Rebuild from 2025-01-01 instead of merging onto existing data")
    parser.add_argument("--zone", default=None,
                        help="A bidding zone (NORD, CNOR, CSUD, SUD, CALA, SICI, SARD) or 'all'; default Italy")
    args = parser.parse_args()

    # Tomorrow's day-ahead forecast is published the day before.
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

    run_areas(targets(args.zone, OUTPUT_PATH, "res_forecasts.json"),
              lambda label, domain, path: fetch_area(token, label, domain, path, start_date, end_date, args.full_history))


def fetch_area(token, label, domain, path, start_date, end_date, full_history):
    area = area_label(label, domain)
    print()
    print("=" * 70)
    print(f"ENTSO-E WIND AND SOLAR FORECASTS ({area})")
    print("=" * 70)
    print(f"Date range: {start_date} -> {end_date}")
    print()

    existing = {name: empty() for name in FORECASTS} if full_history else load_existing(path)
    forecasts = {}
    downloaded = False

    for name in FORECASTS:
        records = download(token, name, start_date, end_date, domain)
        if records is None:
            # Keep what we have; the next run tries again.
            print(f"  Every {name} request failed; keeping the existing data.")
            forecasts[name] = existing[name]
            continue
        downloaded = True
        print(f"  {len(records):,} points")
        forecasts[name] = merge_resolutions(existing[name], records, RESOLUTIONS)
        for code, label_ in PRODUCTION_TYPES.items():
            days = forecasts[name]["daily"].get(code, [])
            print(f"  {name} {label_}: {len(days):,} days" + (f", last {days[-1]['date']}" if days else ""))
        print()

    if not downloaded:
        raise RuntimeError(f"Every forecast request failed for {area}; not writing output.")

    compact.dump(build_output(forecasts, area), path)

    print("=" * 70)
    print(f"Wrote: {path}")
    print("=" * 70)

if __name__ == "__main__":
    main()
