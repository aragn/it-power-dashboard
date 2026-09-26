"""
Fetch Actual Generation per Production Type (ENTSO-E 16.1.B&C, A75) and
Actual Total Load (ENTSO-E 6.1.A&B, A65) for Italy from the ENTSO-E
Transparency Platform Restful API, and build 15-minute / hourly / daily
series for the it-power-dashboard.

API docs: https://transparency.entsoe.eu (Restful API, single GET
endpoint at https://web-api.tp.entsoe.eu/api).

Authentication: a personal security token is required (free, requested
by e-mailing transparency@entsoe.eu with subject "Restful API access").
The token must be supplied via the ENTSOE_API_KEY environment variable
-- never hard-code it in this file or commit it to the repository.
"""

import argparse
import os
import time
from datetime import date, timedelta

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

# Italy control area (used both as in_Domain for generation and
# outBiddingZone_Domain for total load).
IT_DOMAIN = "10YIT-GRTN-----B"

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "app",
    "data",
    "generation_load.json",
)

DEFAULT_HISTORY_START = date(2025, 1, 1)

# Generation-per-type responses contain one TimeSeries per production
# type, so chunks stay short to keep XML payloads small.
GENERATION_CHUNK_DAYS = 7
LOAD_CHUNK_DAYS = 30

# Power System Resource (production) type names.
PSR_TYPE_NAMES = {
    "B01": "Biomass",
    "B02": "Fossil Brown coal/Lignite",
    "B03": "Fossil Coal-derived gas",
    "B04": "Fossil Gas",
    "B05": "Fossil Hard coal",
    "B06": "Fossil Oil",
    "B07": "Fossil Oil shale",
    "B08": "Fossil Peat",
    "B09": "Geothermal",
    "B10": "Hydro Pumped Storage",
    "B11": "Hydro Run-of-river and poundage",
    "B12": "Hydro Water Reservoir",
    "B13": "Marine",
    "B14": "Nuclear",
    "B15": "Other renewable",
    "B16": "Solar",
    "B17": "Waste",
    "B18": "Wind Offshore",
    "B19": "Wind Onshore",
    "B20": "Other",
    "B25": "Energy storage",
}


# ============================================================================
# DOWNLOAD
# ============================================================================


def psr_type(ts):
    elem = ts.find("{*}MktPSRType/{*}psrType")
    return elem.text if elem is not None else "TOTAL"


def is_generation(ts):
    return ts.find("{*}inBiddingZone_Domain.mRID") is not None


def is_consumption(ts):
    # Consumption series (pumped-storage and battery charging) are
    # published against outBiddingZone_Domain.
    return ts.find("{*}outBiddingZone_Domain.mRID") is not None


def download_generation_and_consumption(token, start_date, end_date):
    gen_records, cons_records = [], []

    for chunk_start, chunk_end in day_chunks(start_date, end_date, GENERATION_CHUNK_DAYS):
        print(f"  Generation/consumption: {chunk_start} -> {chunk_end}")

        root = request_entsoe(token, {
            "documentType": "A75",
            "processType": "A16",
            "in_Domain": IT_DOMAIN,
            "periodStart": to_api_datetime(chunk_start),
            "periodEnd": to_api_datetime(chunk_end, end_of_day=True),
        })

        gen_records.extend(parse_points(root, group_of=psr_type, keep=is_generation))
        cons_records.extend(parse_points(root, group_of=psr_type, keep=is_consumption))

        time.sleep(REQUEST_PAUSE_SECONDS)

    return gen_records, cons_records


def download_total_load(token, start_date, end_date):
    load_records = []

    for chunk_start, chunk_end in day_chunks(start_date, end_date, LOAD_CHUNK_DAYS):
        print(f"  Total load: {chunk_start} -> {chunk_end}")

        root = request_entsoe(token, {
            "documentType": "A65",
            "processType": "A16",
            "outBiddingZone_Domain": IT_DOMAIN,
            "periodStart": to_api_datetime(chunk_start),
            "periodEnd": to_api_datetime(chunk_end, end_of_day=True),
        })

        load_records.extend(parse_points(root))

        time.sleep(REQUEST_PAUSE_SECONDS)

    return load_records


# ============================================================================
# LOAD / SAVE EXISTING DATA
# ============================================================================


def empty_dataset():
    return {"quarter_hourly": {}, "hourly": {}, "daily": {}}


def empty_existing():
    return {
        "generation": empty_dataset(),
        "consumption": empty_dataset(),
        "total_load": empty_dataset(),
    }


def load_existing():
    if not os.path.exists(OUTPUT_PATH):
        return empty_existing()

    payload = compact.load(OUTPUT_PATH)

    def normalize_dataset(dataset):
        normalized = {}

        for granularity in ("quarter_hourly", "hourly", "daily"):
            groups = dataset.get(granularity, {})

            normalized[granularity] = {}

            for group, value in groups.items():
                # New JSON format:
                # "B05": {"label": "...", "points": [...]}
                if isinstance(value, dict) and "points" in value:
                    normalized[granularity][group] = value["points"]

                # Old/raw format:
                # "B05": [...]
                elif isinstance(value, list):
                    normalized[granularity][group] = value

                else:
                    raise TypeError(
                        f"Unexpected structure for {granularity}/{group}: "
                        f"{type(value).__name__}"
                    )

        return normalized

    return {
        "generation": normalize_dataset(
            payload.get("generation", empty_dataset())
        ),
        "consumption": normalize_dataset(
            payload.get("consumption", empty_dataset())
        ),
        "total_load": normalize_dataset(
            payload.get("total_load", empty_dataset())
        ),
    }


def label_groups(by_group, resolution):
    """Attach human-readable PSR names where applicable (generation/consumption)."""

    labelled = {}
    for code, rows in by_group.items():
        name = PSR_TYPE_NAMES.get(code, code)
        labelled[code] = {
            "label": name,
            "points": compact.encode_series(rows, resolution),
        }
    return labelled


def encode_groups(by_group, resolution):
    return {
        code: compact.encode_series(rows, resolution)
        for code, rows in by_group.items()
    }


def build_output(generation, consumption, total_load):
    return {
        "source": "ENTSO-E Transparency Platform",
        "control_area": f"IT ({IT_DOMAIN})",
        "description": (
            "16.1.B&C Actual Generation per Production Type (A75) and "
            "6.1.A&B Actual Total Load (A65) for Italy. Native "
            "quarter-hourly resolution from 1 October 2025 onward "
            "(hourly data before that is expanded into four identical "
            "quarter-hour points). Hourly values are the average of the "
            "quarter-hour values within the hour; daily values are the "
            "sum of the hourly values (approximate MWh/day). Times are "
            "Italian market time (Europe/Rome), labelled by elapsed time "
            "since local midnight like GME periods."
        ),
        "quarter_hourly_native_from": "2025-10-01",
        "generation": {
            resolution: label_groups(generation[resolution], resolution)
            for resolution in ("quarter_hourly", "hourly", "daily")
        },
        "consumption": {
            resolution: label_groups(consumption[resolution], resolution)
            for resolution in ("quarter_hourly", "hourly", "daily")
        },
        "total_load": {
            resolution: encode_groups(total_load[resolution], resolution)
            for resolution in ("quarter_hourly", "hourly", "daily")
        },
    }


# ============================================================================
# MAIN
# ============================================================================


def main():

    parser = argparse.ArgumentParser(
        description=(
            "Fetch ENTSO-E Actual Generation per Production Type (16.1.B&C) "
            "and Actual Total Load (6.1.A&B) for Italy."
        )
    )
    parser.add_argument("--start", required=False, default=None)
    parser.add_argument("--end", required=False, default=None)
    parser.add_argument("--full-history", action="store_true")
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
    print("ENTSO-E GENERATION & LOAD FETCH (Italy)")
    print("=" * 70)
    print(f"Date range: {start_date} -> {end_date}")
    print()

    # A full-history run rebuilds the file from scratch, so no points from
    # an earlier run (e.g. with a different time basis) survive.
    existing = empty_existing() if args.full_history else load_existing()

    print("Downloading generation (16.1.B&C, A75)...")
    gen_records, cons_records = download_generation_and_consumption(
        token, start_date, end_date
    )
    print(f"  {len(gen_records):,} generation points, "
          f"{len(cons_records):,} consumption points")

    print()
    print("Downloading actual total load (6.1.A&B, A65)...")
    load_records = download_total_load(token, start_date, end_date)
    print(f"  {len(load_records):,} load points")

    print()
    print("Building series...")

    def build_dataset(records, existing_dataset):
        new = build_resolutions(records)
        return {
            resolution: merge_group_series(existing_dataset[resolution], new[resolution])
            for resolution in ("quarter_hourly", "hourly", "daily")
        }

    generation = build_dataset(gen_records, existing["generation"])
    consumption = build_dataset(cons_records, existing["consumption"])
    total_load = build_dataset(load_records, existing["total_load"])

    if not generation["daily"] or not total_load["daily"]:
        raise RuntimeError("No generation or load data downloaded; not writing output.")

    compact.dump(
        build_output(generation, consumption, total_load), OUTPUT_PATH
    )

    print()
    print("=" * 70)
    print(f"Wrote: {OUTPUT_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    main()
