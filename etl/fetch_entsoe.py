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
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

import compact

# ============================================================================
# CONFIGURATION
# ============================================================================

API_URL = "https://web-api.tp.entsoe.eu/api"

# Italy control area (used both as in_Domain for generation and
# outBiddingZone_Domain for total load).
IT_DOMAIN = "10YIT-GRTN-----B"

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "app",
    "data",
    "generation_load.json",
)

# ENTSO-E switched several bidding zones (including Italy) from hourly
# to 15-minute market time units for delivery from 1 October 2025.
PT15_START = date(2025, 10, 1)

DEFAULT_HISTORY_START = date(2025, 1, 1)

# Chunk size for API requests. Generation-per-type responses contain one
# TimeSeries per production type, so we keep chunks short to avoid huge
# XML payloads and to respect the platform's practical size limits.
CHUNK_DAYS = 7

# Pause between requests, and retry settings for HTTP 429.
REQUEST_PAUSE_SECONDS = 2
MAX_RETRIES = 6
INITIAL_RETRY_SECONDS = 20

XML_NS = {"ns": "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"}

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
# DATE HELPERS
# ============================================================================


def parse_date(value):
    value = str(value)
    if "-" in value:
        return datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.strptime(value, "%Y%m%d").date()


def day_chunks(start_date, end_date, chunk_days=CHUNK_DAYS):
    current = start_date
    while current <= end_date:
        chunk_end = min(current + timedelta(days=chunk_days - 1), end_date)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def to_api_datetime(d, end_of_day=False):
    """ENTSO-E expects UTC timestamps as YYYYMMDDHHMM."""
    if end_of_day:
        dt = datetime(d.year, d.month, d.day) + timedelta(days=1)
    else:
        dt = datetime(d.year, d.month, d.day)
    return dt.strftime("%Y%m%d%H%M")


# ============================================================================
# API REQUEST
# ============================================================================


def request_entsoe(token, params):
    """
    Call the ENTSO-E Transparency Platform Restful API and return the
    parsed XML root. Handles HTTP 429 (rate limit) with exponential
    backoff. A 400 response with "No matching data found" is treated
    as an empty result rather than an error.
    """

    query = dict(params)
    query["securityToken"] = token

    retry_seconds = INITIAL_RETRY_SECONDS

    for attempt in range(MAX_RETRIES + 1):

        response = requests.get(API_URL, params=query, timeout=120)

        if response.status_code == 429:
            if attempt >= MAX_RETRIES:
                response.raise_for_status()
            print(f"    HTTP 429 rate limit. Waiting {retry_seconds}s...")
            time.sleep(retry_seconds)
            retry_seconds *= 2
            continue

        if response.status_code == 400 and "No matching data" in response.text:
            return None

        response.raise_for_status()

        return ET.fromstring(response.content)

    return None


# ============================================================================
# XML PARSING
# ============================================================================


def _find(elem, tag):
    return elem.find(f"ns:{tag}", XML_NS)


def _findall(elem, tag):
    return elem.findall(f"ns:{tag}", XML_NS)


def _resolution_minutes(resolution):
    return {"PT15M": 15, "PT60M": 60, "PT30M": 30}.get(resolution, 60)


def parse_timeseries(root, mode):
    """
    Parse a GL_MarketDocument response into flat point records.

    mode:
      "generation"  -> reads inBiddingZone_Domain series (per PSR type)
      "consumption" -> reads outBiddingZone_Domain series (per PSR type,
                        mainly pumped-storage charging)
      "load"        -> reads the single total-load series (no PSR type)

    Returns a list of dicts:
      {"date", "time", "period", "minutes_per_period", "psr_type", "value"}
    """

    if root is None:
        return []

    records = []

    for ts in _findall(root, "TimeSeries"):

        psr_type = None
        psr_elem = _find(ts, "MktPSRType")
        if psr_elem is not None:
            code_elem = _find(psr_elem, "psrType")
            if code_elem is not None:
                psr_type = code_elem.text

        if mode == "generation" and _find(ts, "inBiddingZone_Domain.mRID") is None:
            continue
        if mode == "consumption" and _find(ts, "outBiddingZone_Domain.mRID") is None:
            continue

        for period in _findall(ts, "Period"):

            time_interval = _find(period, "timeInterval")
            start_text = _find(time_interval, "start").text
            period_start = datetime.strptime(start_text, "%Y-%m-%dT%H:%MZ")

            resolution = _find(period, "resolution").text
            minutes_per_period = _resolution_minutes(resolution)

            for point in _findall(period, "Point"):

                position = int(_find(point, "position").text)
                quantity = float(_find(point, "quantity").text)

                point_start = period_start + timedelta(
                    minutes=minutes_per_period * (position - 1)
                )

                records.append(
                    {
                        "date": point_start.date().isoformat(),
                        "time": point_start.strftime("%H:%M"),
                        "minutes_per_period": minutes_per_period,
                        "psr_type": psr_type,
                        "value": quantity,
                    }
                )

    return records


# ============================================================================
# SERIES BUILDERS
# ============================================================================


def to_quarter_hourly(records):
    """
    Convert raw points (15-min or hourly resolution) into a native
    quarter-hourly series. Hourly points are expanded into four
    quarter-hour points carrying the same value (consistent with how
    fetch_pun.py / fetch_zonal.py handle pre-PT15 data).
    """

    output = defaultdict(dict)

    for row in records:

        key_group = row["psr_type"] or "TOTAL"

        if row["minutes_per_period"] == 15:
            output[key_group][(row["date"], row["time"])] = row["value"]
        else:
            start = datetime.strptime(
                f"{row['date']}T{row['time']}", "%Y-%m-%dT%H:%M"
            )
            for offset in range(4):
                q_time = (start + timedelta(minutes=15 * offset)).strftime("%H:%M")
                output[key_group][(row["date"], q_time)] = row["value"]

    result = {}
    for key_group, points in output.items():
        rows = [
            {"date": d, "time": t, "value": round(v, 2)}
            for (d, t), v in points.items()
        ]
        result[key_group] = sorted(rows, key=lambda x: (x["date"], x["time"]))

    return result


def quarter_hourly_to_hourly(quarter_hourly_by_group):
    """Hourly value = average of the (up to 4) quarter-hour values."""

    result = {}

    for key_group, rows in quarter_hourly_by_group.items():

        grouped = defaultdict(list)
        for row in rows:
            hour = row["time"][:2] + ":00"
            grouped[(row["date"], hour)].append(row["value"])

        hourly_rows = [
            {"date": d, "time": h, "value": round(sum(v) / len(v), 2)}
            for (d, h), v in grouped.items()
        ]

        result[key_group] = sorted(hourly_rows, key=lambda x: (x["date"], x["time"]))

    return result


def hourly_to_daily(hourly_by_group):
    """Daily value = sum of the 24 hourly values (approximate MWh/day)."""

    result = {}

    for key_group, rows in hourly_by_group.items():

        grouped = defaultdict(list)
        for row in rows:
            grouped[row["date"]].append(row["value"])

        daily_rows = [
            {"date": d, "value": round(sum(v), 2)} for d, v in grouped.items()
        ]

        result[key_group] = sorted(daily_rows, key=lambda x: x["date"])

    return result


def merge_group_series(existing, new):
    """Merge two {group: [{"date","time"/None,"value"}]} dicts, new wins."""

    # Normalize None / empty inputs
    existing = existing or {}
    new = new or {}

    # Both inputs must be dictionaries keyed by group
    if not isinstance(existing, dict):
        raise TypeError(
            f"Expected existing series to be dict, got {type(existing).__name__}"
        )

    if not isinstance(new, dict):
        raise TypeError(
            f"Expected new series to be dict, got {type(new).__name__}"
        )

    merged = {}
    all_groups = set(existing) | set(new)

    for group in all_groups:
        by_key = {}

        existing_rows = existing.get(group, [])
        new_rows = new.get(group, [])

        if not isinstance(existing_rows, list):
            raise TypeError(
                f"Expected existing group '{group}' to be list, "
                f"got {type(existing_rows).__name__}"
            )

        if not isinstance(new_rows, list):
            raise TypeError(
                f"Expected new group '{group}' to be list, "
                f"got {type(new_rows).__name__}"
            )

        for row in existing_rows:
            if not isinstance(row, dict):
                raise TypeError(
                    f"Invalid existing row in group '{group}': "
                    f"expected dict, got {type(row).__name__}"
                )

            key = (row["date"], row.get("time"))
            by_key[key] = row

        for row in new_rows:
            if not isinstance(row, dict):
                raise TypeError(
                    f"Invalid new row in group '{group}': "
                    f"expected dict, got {type(row).__name__}"
                )

            key = (row["date"], row.get("time"))
            by_key[key] = row

        merged[group] = sorted(
            by_key.values(),
            key=lambda x: (x["date"], x.get("time") or "")
        )

    return merged


# ============================================================================
# DOWNLOAD
# ============================================================================


def download_generation_and_consumption(token, start_date, end_date):

    gen_records, cons_records = [], []

    for chunk_start, chunk_end in day_chunks(start_date, end_date):

        print(f"  Generation/consumption: {chunk_start} -> {chunk_end}")

        params = {
            "documentType": "A75",
            "processType": "A16",
            "in_Domain": IT_DOMAIN,
            "periodStart": to_api_datetime(chunk_start),
            "periodEnd": to_api_datetime(chunk_end, end_of_day=True),
        }

        root = request_entsoe(token, params)

        gen_records.extend(parse_timeseries(root, mode="generation"))
        cons_records.extend(parse_timeseries(root, mode="consumption"))

        time.sleep(REQUEST_PAUSE_SECONDS)

    return gen_records, cons_records


def download_total_load(token, start_date, end_date):

    load_records = []

    for chunk_start, chunk_end in day_chunks(start_date, end_date, chunk_days=30):

        print(f"  Total load: {chunk_start} -> {chunk_end}")

        params = {
            "documentType": "A65",
            "processType": "A16",
            "outBiddingZone_Domain": IT_DOMAIN,
            "periodStart": to_api_datetime(chunk_start),
            "periodEnd": to_api_datetime(chunk_end, end_of_day=True),
        }

        root = request_entsoe(token, params)

        load_records.extend(parse_timeseries(root, mode="load"))

        time.sleep(REQUEST_PAUSE_SECONDS)

    return load_records


# ============================================================================
# LOAD / SAVE EXISTING DATA
# ============================================================================


def empty_dataset():
    return {"quarter_hourly": {}, "hourly": {}, "daily": {}}


def load_existing():
    if not os.path.exists(OUTPUT_PATH):
        return {
            "generation": empty_dataset(),
            "consumption": empty_dataset(),
            "total_load": empty_dataset(),
        }

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
            "sum of the 24 hourly values (approximate MWh/day)."
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

    existing = load_existing()

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
        new_qh = to_quarter_hourly(records)
        new_hourly = quarter_hourly_to_hourly(new_qh)
        new_daily = hourly_to_daily(new_hourly)

        merged_qh = merge_group_series(existing_dataset["quarter_hourly"], new_qh)
        merged_hourly = merge_group_series(existing_dataset["hourly"], new_hourly)
        merged_daily = merge_group_series(existing_dataset["daily"], new_daily)

        return {
            "quarter_hourly": merged_qh,
            "hourly": merged_hourly,
            "daily": merged_daily,
        }

    generation = build_dataset(gen_records, existing["generation"])
    consumption = build_dataset(cons_records, existing["consumption"])
    total_load = build_dataset(load_records, existing["total_load"])

    compact.dump(
        build_output(generation, consumption, total_load), OUTPUT_PATH
    )

    print()
    print("=" * 70)
    print(f"Wrote: {OUTPUT_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    main()
