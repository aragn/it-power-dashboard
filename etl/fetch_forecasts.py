"""
Fetch the day-ahead forecasts for Italy (or with --zone the Italian bidding
zones: app/data/zones/<ZONE>/forecasts.json, etl/zones.py) from the ENTSO-E
Transparency Platform and write app/data/forecasts.json:

  - Day-ahead Total Load Forecast        [6.1.B]   documentType A65,
                                                   processType A01
  - Generation Forecast - Day ahead      [14.1.C]  documentType A71,
                                                   processType A01
    (only the generation series; the same document can also carry
    scheduled consumption)

Both are published the day before delivery, so incremental runs fetch up
to tomorrow.  Values in MW; hourly = average of the quarter-hours; daily =
sum of the hours (MWh/day), like the actual generation and load.  Italian
market time, labelled by elapsed time since local midnight like GME
periods.

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
import catch_up
from zones import IT_DOMAIN, area_label, run_areas, targets

# ============================================================================
# CONFIGURATION
# ============================================================================

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "app",
    "data",
    "forecasts.json",
)

DEFAULT_HISTORY_START = date(2025, 1, 1)

# One series per request; the API accepts up to a year.
CHUNK_DAYS = 90

# Incremental runs re-download this many days to pick up revisions.
LOOKBACK_DAYS = 7

RESOLUTIONS = ("quarter_hourly", "hourly", "daily")

FORECASTS = {
    "load_forecast": {
        "label": "Day-ahead total load forecast (6.1.B)",
        "params": {"documentType": "A65", "processType": "A01", "outBiddingZone_Domain": IT_DOMAIN},
    },
    "generation_forecast": {
        "label": "Day-ahead generation forecast (14.1.C)",
        "params": {"documentType": "A71", "processType": "A01", "in_Domain": IT_DOMAIN},
    },
}


def is_generation(ts):
    # Generation series name the bidding zone they feed (inBiddingZone);
    # scheduled consumption the one they draw from (outBiddingZone).
    return ts.find("{*}outBiddingZone_Domain.mRID") is None


# ============================================================================
# DOWNLOAD
# ============================================================================


def area_params(params, domain):
    """A forecast's request parameters for another area: its domain fields
    (in_Domain, outBiddingZone_Domain) set to the area's code."""
    return {key: (domain if value == IT_DOMAIN else value) for key, value in params.items()}


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

        keep = is_generation if name == "generation_forecast" else (lambda ts: True)
        records.extend(parse_points(root, keep=keep))
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
    """{forecast: {resolution: {"TOTAL": rows}}}."""
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
            f"Day-ahead forecasts for {area}: total load (6.1.B, A65) and "
            "generation (14.1.C, A71), MW; hourly = average of the "
            "quarter-hours, daily = sum of the hours (MWh/day). Italian market "
            "time (Europe/Rome), labelled by elapsed time since local midnight "
            "like GME periods."
        ),
        "labels": {name: spec["label"] for name, spec in FORECASTS.items()},
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
    parser = argparse.ArgumentParser(description="Fetch ENTSO-E day-ahead load and generation forecasts for Italy.")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--full-history", action="store_true",
                        help="Rebuild from 2025-01-01 instead of merging onto existing data")
    parser.add_argument("--zone", default=None,
                        help="A bidding zone (NORD, CNOR, CSUD, SUD, CALA, SICI, SARD) or 'all'; default Italy")
    args = parser.parse_args()

    # Tomorrow's forecasts are published the day before.
    end_date = parse_date(args.end) if args.end else market_today() + timedelta(days=1)

    if args.start:
        start_date = parse_date(args.start)
    elif args.full_history:
        start_date = DEFAULT_HISTORY_START
    else:
        start_date = catch_up.start(OUTPUT_PATH, market_today() - timedelta(days=LOOKBACK_DAYS))
    start_date = max(start_date, DEFAULT_HISTORY_START)

    if end_date < start_date:
        raise ValueError("End date must not be before start date.")

    token = os.environ.get("ENTSOE_API_KEY")
    if not token:
        raise RuntimeError("ENTSOE_API_KEY environment variable must be set.")

    # An area without a file yet (a zone's first run) reads its whole history.
    explicit = bool(args.start) or args.full_history
    run_areas(targets(args.zone, OUTPUT_PATH, "forecasts.json"),
              lambda label, domain, path: fetch_area(
                  token, label, domain, path,
                  start_date if explicit or os.path.exists(path) else DEFAULT_HISTORY_START,
                  end_date, args.full_history))


def fetch_area(token, label, domain, path, start_date, end_date, full_history):
    area = area_label(label, domain)
    print()
    print("=" * 70)
    print(f"ENTSO-E DAY-AHEAD FORECASTS ({area})")
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
        days = forecasts[name]["daily"].get("TOTAL", [])
        print(f"  {name}: {len(days):,} days" + (f", last {days[-1]['date']}" if days else ""))
        print()

    if not downloaded:
        raise RuntimeError(f"Every forecast request failed for {area}; not writing output.")

    compact.dump(build_output(forecasts, area), path)

    print("=" * 70)
    print(f"Wrote: {path}")
    print("=" * 70)

if __name__ == "__main__":
    main()
