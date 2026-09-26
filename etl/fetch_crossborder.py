"""
Fetch Italy's cross-border electricity exchanges from the ENTSO-E
Transparency Platform, per neighbouring control area and direction:

  - Physical Flows                     [12.1.G]  documentType A11
  - Scheduled Commercial Exchanges     [12.1.F]  documentType A09
      * Day-ahead  (contract_MarketAgreement.Type A01): commercial
        exchanges from yearly, quarterly, monthly, weekly and daily
        allocations.
      * Total      (contract_MarketAgreement.Type A05): all allocations,
        i.e. day-ahead plus intraday.
    (ENTSO-E Detailed Data Descriptions v3r4, TR article 12.1.f.)

"imports" are flows/schedules into the Italian control area, "exports"
out of it.  Output: app/data/crossborder.json with 15-minute / hourly /
daily series on the same Italian market-time basis as the other data.

Quarter-hourly series are stored from 1 October 2025 (15-minute market
time unit) only; before that the dashboard repeats the hourly value.

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
    build_resolutions,
    day_chunks,
    merge_group_series,
    parse_date,
    parse_points,
    request_entsoe,
    to_api_datetime,
)

# ============================================================================
# CONFIGURATION
# ============================================================================

IT_DOMAIN = "10YIT-GRTN-----B"

# Neighbouring control areas (EIC codes).
BORDERS = {
    "AT": ("Austria", "10YAT-APG------L"),
    "CH": ("Switzerland", "10YCH-SWISSGRIDZ"),
    "FR": ("France", "10YFR-RTE------C"),
    "GR": ("Greece", "10YGR-HTSO-----Y"),
    "ME": ("Montenegro", "10YCS-CG-TSO---S"),
    "MT": ("Malta", "10Y1001A1001A93C"),
    "SI": ("Slovenia", "10YSI-ELES-----O"),
}

DATASETS = {
    "physical_flows": {"documentType": "A11"},
    "scheduled_day_ahead": {"documentType": "A09", "contract_MarketAgreement.Type": "A01"},
    "scheduled_total": {"documentType": "A09", "contract_MarketAgreement.Type": "A05"},
}

DIRECTIONS = ("imports", "exports")

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "app",
    "data",
    "crossborder.json",
)

DEFAULT_HISTORY_START = date(2025, 1, 1)
PT15_START = date(2025, 10, 1)

# These documents are one series per request; the API accepts up to a
# year per request, half a year keeps responses modest.
CHUNK_DAYS = 183

RESOLUTIONS = ("quarter_hourly", "hourly", "daily")


# ============================================================================
# DOWNLOAD
# ============================================================================


def domains(direction, neighbour):
    """in_Domain receives the energy, out_Domain sends it."""
    if direction == "imports":
        return {"in_Domain": IT_DOMAIN, "out_Domain": neighbour}
    return {"in_Domain": neighbour, "out_Domain": IT_DOMAIN}


def download_dataset(token, dataset, start_date, end_date):
    """Point records grouped as "<direction>|<country>"."""
    records = []
    failures = 0
    requests_made = 0

    for country, (label, eic) in BORDERS.items():
        for direction in DIRECTIONS:
            group = f"{direction}|{country}"

            for chunk_start, chunk_end in day_chunks(start_date, end_date, CHUNK_DAYS):
                print(f"  {dataset} {direction:7} {country}: {chunk_start} -> {chunk_end}")
                requests_made += 1

                params = {
                    **DATASETS[dataset],
                    **domains(direction, eic),
                    "periodStart": to_api_datetime(chunk_start),
                    "periodEnd": to_api_datetime(chunk_end, end_of_day=True),
                }

                try:
                    root = request_entsoe(token, params)
                except requests.HTTPError as error:
                    # One border failing must not stop the others; the
                    # existing data for it is kept.
                    failures += 1
                    print(f"    WARNING: {error}")
                    continue

                records.extend(parse_points(root, group_of=lambda ts, g=group: g))
                time.sleep(REQUEST_PAUSE_SECONDS)

    if requests_made and failures == requests_made:
        raise RuntimeError(f"Every request for {dataset} failed.")

    return records


# ============================================================================
# LOAD / SAVE
# ============================================================================


def empty_dataset():
    return {resolution: {} for resolution in RESOLUTIONS}


def load_existing():
    """{dataset: {resolution: {"<direction>|<country>": rows}}}."""
    existing = {name: empty_dataset() for name in DATASETS}

    if not os.path.exists(OUTPUT_PATH):
        return existing

    payload = compact.load(OUTPUT_PATH)

    for name in DATASETS:
        for resolution in RESOLUTIONS:
            by_direction = payload.get(name, {}).get(resolution, {})
            for direction in DIRECTIONS:
                for country, rows in by_direction.get(direction, {}).items():
                    existing[name][resolution][f"{direction}|{country}"] = rows

    return existing


def encode_dataset(dataset):
    output = {}
    for resolution in RESOLUTIONS:
        output[resolution] = {direction: {} for direction in DIRECTIONS}
        for group, rows in sorted(dataset[resolution].items()):
            direction, country = group.split("|")
            output[resolution][direction][country] = compact.encode_series(
                rows,
                resolution,
                # Before the 15-minute MTU the dashboard expands hourly.
                skip_before=PT15_START.isoformat() if resolution == "quarter_hourly" else None,
            )
    return output


def build_output(datasets):
    return {
        "source": "ENTSO-E Transparency Platform",
        "control_area": f"IT ({IT_DOMAIN})",
        "description": (
            "Cross-border exchanges between the Italian control area and "
            "each neighbouring control area, per direction: physical flows "
            "(12.1.G) and scheduled commercial exchanges (12.1.F), day-ahead "
            "(long-term + day-ahead allocations) and total (all allocations "
            "including intraday). Values in MW; hourly = average of the "
            "quarter-hours; daily = sum of the hourly values (MWh/day). "
            "Italian market time (Europe/Rome), labelled by elapsed time "
            "since local midnight like GME periods. Quarter-hourly series "
            "start at quarter_hourly_native_from; earlier quarter-hours "
            "repeat the hourly value."
        ),
        "quarter_hourly_native_from": PT15_START.isoformat(),
        "borders": {country: label for country, (label, _) in BORDERS.items()},
        **{name: encode_dataset(dataset) for name, dataset in datasets.items()},
    }


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Fetch ENTSO-E cross-border flows and scheduled exchanges for Italy."
    )
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="Rebuild from 2025-01-01 instead of merging onto existing data",
    )
    args = parser.parse_args()

    end_date = parse_date(args.end) if args.end else date.today()

    if args.start:
        start_date = parse_date(args.start)
    elif args.full_history:
        start_date = DEFAULT_HISTORY_START
    else:
        start_date = end_date - timedelta(days=3)

    start_date = max(start_date, DEFAULT_HISTORY_START)

    if end_date < start_date:
        raise ValueError("End date must not be before start date.")

    token = os.environ.get("ENTSOE_API_KEY")
    if not token:
        raise RuntimeError("ENTSOE_API_KEY environment variable must be set.")

    print()
    print("=" * 70)
    print("ENTSO-E CROSS-BORDER EXCHANGES (Italy)")
    print("=" * 70)
    print(f"Date range: {start_date} -> {end_date}")
    print()

    existing = (
        {name: empty_dataset() for name in DATASETS}
        if args.full_history
        else load_existing()
    )

    datasets = {}
    for name in DATASETS:
        print(f"Downloading {name}...")
        records = download_dataset(token, name, start_date, end_date)
        print(f"  {len(records):,} points")

        new = build_resolutions(records)
        datasets[name] = {
            resolution: merge_group_series(existing[name][resolution], new[resolution])
            for resolution in RESOLUTIONS
        }
        print()

    if not any(datasets["physical_flows"]["daily"].values()):
        raise RuntimeError("No physical flow data downloaded; not writing output.")

    compact.dump(build_output(datasets), OUTPUT_PATH)

    print("=" * 70)
    print(f"Wrote: {OUTPUT_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    main()
