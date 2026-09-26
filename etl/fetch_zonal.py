import argparse
import base64
import io
import json
import os
import time
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

import compact

# ============================================================================
# CONFIGURATION
# ============================================================================

API_BASE = "https://api.mercatoelettrico.org/request"
AUTH_URL = f"{API_BASE}/api/v1/Auth"
DATA_URL = f"{API_BASE}/api/v1/RequestData"

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "app",
    "data",
    "zonal_prices.json",
)

# The Italian MGP zonal market is split into these bidding zones.
# (Foreign virtual zones such as FRAN, AUST, SLOV, CORS, GREC, COAC,
# COUP, BSP, MALT, MONT and the national PUN index are intentionally
# excluded - this script is about *Italian* bidding-zone prices.)
ITALIAN_ZONES = [
    "NORD",
    "CNOR",
    "CSUD",
    "SUD",
    "CALA",
    "SICI",
    "SARD",
]

# GME introduced 15-minute MTU products for delivery from 1 October 2025.
PT15_START = date(2025, 10, 1)

# Earliest date covered by this script (per project requirements).
DEFAULT_HISTORY_START = date(2025, 1, 1)

# Pause between successful API requests.
REQUEST_PAUSE_SECONDS = 20

# Retry settings for HTTP 429.
MAX_RETRIES = 6
INITIAL_RETRY_SECONDS = 30

# ============================================================================
# AUTHENTICATION
# ============================================================================


def get_token(login, password):
    """Authenticate with GME and return the JWT token."""

    response = requests.post(
        AUTH_URL,
        json={
            "Login": login,
            "Password": password,
        },
        timeout=60,
    )

    response.raise_for_status()

    payload = response.json()

    if not payload.get("success"):
        reason = payload.get(
            "reason",
            "Unknown authentication error",
        )
        raise RuntimeError(
            f"GME authentication failed: {reason}"
        )

    token = payload.get("token")

    if not token:
        raise RuntimeError(
            "GME authentication succeeded but no token was returned."
        )

    return token


# ============================================================================
# DATE HELPERS
# ============================================================================


def month_ranges(start_date, end_date):
    """Split an inclusive date range into calendar-month chunks."""

    current = start_date

    while current <= end_date:

        next_month = (
            current.replace(day=28) + timedelta(days=4)
        ).replace(day=1)

        month_end = next_month - timedelta(days=1)

        chunk_end = min(month_end, end_date)

        yield current, chunk_end

        current = chunk_end + timedelta(days=1)


def parse_date(value):
    """Convert YYYYMMDD or YYYY-MM-DD into a date."""

    value = str(value)

    if "-" in value:
        return datetime.strptime(value, "%Y-%m-%d").date()

    return datetime.strptime(value, "%Y%m%d").date()


# ============================================================================
# GME API REQUEST
# ============================================================================


def request_chunk(token, start_date, end_date, granularity=None):
    """
    Request one date chunk from GME.

    granularity:
      None  -> GME historical/default granularity
      PT60  -> hourly
      PT15  -> quarter-hourly

    GME returns the data as a Base64-encoded ZIP in the top-level
    contentResponse field.
    """

    body = {
        "Platform": "PublicMarketResults",
        "Segment": "MGP",
        "DataName": "ME_ZonalPrices",
        "IntervalStart": start_date.strftime("%Y%m%d"),
        "IntervalEnd": end_date.strftime("%Y%m%d"),
        "Attributes": {},
    }

    # IMPORTANT:
    # For pre-October-2025 historical data we deliberately leave
    # GranularityType out of the request.
    if granularity is not None:
        body["Attributes"]["GranularityType"] = granularity

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    retry_seconds = INITIAL_RETRY_SECONDS

    for attempt in range(MAX_RETRIES + 1):

        response = requests.post(
            DATA_URL,
            headers=headers,
            json=body,
            timeout=180,
        )

        # ------------------------------------------------------------
        # Rate limit
        # ------------------------------------------------------------

        if response.status_code == 429:

            if attempt >= MAX_RETRIES:
                response.raise_for_status()

            retry_after = response.headers.get("Retry-After")

            if retry_after:
                try:
                    wait_seconds = int(retry_after)
                except ValueError:
                    wait_seconds = retry_seconds
            else:
                wait_seconds = retry_seconds

            print(
                f"  HTTP 429 rate limit. Waiting {wait_seconds}s "
                "before retry..."
            )

            time.sleep(wait_seconds)

            retry_seconds *= 2

            continue

        # ------------------------------------------------------------
        # Authentication expiry
        # ------------------------------------------------------------

        if response.status_code == 401:
            raise PermissionError("GME token expired or unauthorized.")

        response.raise_for_status()

        payload = response.json()

        # ------------------------------------------------------------
        # GME response
        # ------------------------------------------------------------

        result_request = payload.get("resultRequest")

        if result_request not in (None, "", "OK", "Ok", "Success"):
            raise RuntimeError(f"GME API request failed: {result_request}")

        content_response = (
            payload.get("contentResponse")
            or payload.get("ContentResponse")
        )

        if not content_response:
            raise RuntimeError(
                "GME API returned no contentResponse. "
                f"Response: {payload}"
            )

        # ------------------------------------------------------------
        # Base64 -> ZIP
        # ------------------------------------------------------------

        try:
            zip_bytes = base64.b64decode(content_response)
        except Exception as exc:
            raise RuntimeError(
                "Could not decode GME contentResponse from Base64."
            ) from exc

        # ------------------------------------------------------------
        # ZIP -> JSON
        # ------------------------------------------------------------

        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:

                names = archive.namelist()

                json_files = [
                    name for name in names if name.lower().endswith(".json")
                ]

                if not json_files:
                    raise RuntimeError(
                        f"GME ZIP contains no JSON file. Files: {names}"
                    )

                json_name = json_files[0]

                with archive.open(json_name) as json_file:
                    data = json.load(json_file)

        except zipfile.BadZipFile as exc:
            raise RuntimeError(
                "GME contentResponse is not a valid ZIP file."
            ) from exc

        # ------------------------------------------------------------
        # JSON -> rows
        # ------------------------------------------------------------

        if isinstance(data, list):
            return data

        if isinstance(data, dict):

            for key in ("contentResponse", "data", "rows", "result", "items"):

                value = data.get(key)

                if isinstance(value, list):
                    return value

            for value in data.values():

                if isinstance(value, list):
                    return value

                if isinstance(value, dict):

                    for nested_value in value.values():

                        if isinstance(nested_value, list):
                            return nested_value

            raise RuntimeError(
                "Could not identify market-data rows inside the GME "
                "JSON response."
            )

        raise RuntimeError("Unexpected error while requesting GME data.")


# ============================================================================
# MONTHLY DOWNLOAD
# ============================================================================


def request_data(login, password, start_date, end_date, granularity=None):
    """
    Download a date range in monthly chunks.

    A new JWT is obtained for each complete download section.
    If the token expires during the download, it is refreshed.
    """

    token = get_token(login, password)

    all_rows = []

    chunks = list(month_ranges(start_date, end_date))

    print(f"  {granularity or 'DEFAULT'}: {len(chunks)} monthly API request(s)")

    for index, (chunk_start, chunk_end) in enumerate(chunks, start=1):

        print(
            f"    [{index}/{len(chunks)}] {chunk_start:%Y-%m-%d} -> "
            f"{chunk_end:%Y-%m-%d}"
        )

        try:
            rows = request_chunk(
                token=token,
                start_date=chunk_start,
                end_date=chunk_end,
                granularity=granularity,
            )
        except PermissionError:
            print("    Token expired. Re-authenticating...")
            token = get_token(login, password)
            rows = request_chunk(
                token=token,
                start_date=chunk_start,
                end_date=chunk_end,
                granularity=granularity,
            )

        print(f"    Received {len(rows):,} rows")

        all_rows.extend(rows)

        if index < len(chunks):
            time.sleep(REQUEST_PAUSE_SECONDS)

    return all_rows


# ============================================================================
# ZONAL FILTER
# ============================================================================


def filter_zonal(rows):
    """
    Keep only rows belonging to an Italian bidding zone (ITALIAN_ZONES).

    Foreign virtual zones and the national PUN index are dropped.
    If none of the expected zones are found, print the available Zone
    values so schema differences are visible rather than silently
    producing an incomplete dataset.
    """

    result = []

    zones_seen = set()

    for row in rows:

        zone_value = row.get("Zone")

        if zone_value is not None:
            zones_seen.add(str(zone_value))

        zone = str(zone_value or "").strip().upper()

        if zone not in ITALIAN_ZONES:
            continue

        if row.get("Price") is None:
            continue

        if row.get("FlowDate") is None:
            continue

        if row.get("Period") is None:
            continue

        result.append(row)

    if rows and not result:

        print()
        print(
            "  WARNING: No rows matching the Italian bidding zones "
            f"{ITALIAN_ZONES} were found in this dataset."
        )
        print("  Available Zone values:", sorted(zones_seen)[:50])
        print("  First row:", rows[0])
        print()

    return result


# ============================================================================
# TIME CONVERSION
# ============================================================================


def market_time_from_period(period, minutes_per_period):
    """
    Convert GME Period into an HH:MM market-time label.

    Hourly:
      Period 1  -> 00:00
      Period 24 -> 23:00
      Period 25 -> 24:00

    Quarter-hourly:
      Period 1  -> 00:00
      Period 2  -> 00:15
      ...
      Period 96  -> 23:45
      Period 97  -> 24:00
    """

    period = int(period)

    total_minutes = (period - 1) * minutes_per_period

    hour = total_minutes // 60
    minute = total_minutes % 60

    return f"{hour:02d}:{minute:02d}"


# ============================================================================
# BUILD HOURLY SERIES (per zone)
# ============================================================================


def build_hourly_by_zone(rows):

    by_zone = defaultdict(dict)

    for row in filter_zonal(rows):

        zone = str(row["Zone"]).strip().upper()

        flow_date = datetime.strptime(str(row["FlowDate"]), "%Y%m%d").date()

        hour = int(row["Hour"])

        price = float(row["Price"])

        time_string = f"{hour - 1:02d}:00"

        key = (flow_date.isoformat(), hour)

        by_zone[zone][key] = {
            "date": flow_date.isoformat(),
            "time": time_string,
            "hour": hour,
            "price": round(price, 2),
        }

    return {
        zone: sorted(values.values(), key=lambda x: (x["date"], x["hour"]))
        for zone, values in by_zone.items()
    }


# ============================================================================
# BUILD QUARTER-HOURLY SERIES (per zone)
# ============================================================================


def build_quarter_hourly_by_zone(rows):

    by_zone = defaultdict(dict)

    for row in filter_zonal(rows):

        zone = str(row["Zone"]).strip().upper()

        flow_date = datetime.strptime(str(row["FlowDate"]), "%Y%m%d").date()

        period = int(row["Period"])

        price = float(row["Price"])

        key = (flow_date.isoformat(), period)

        by_zone[zone][key] = {
            "date": flow_date.isoformat(),
            "time": market_time_from_period(period, 15),
            "period": period,
            "price": round(price, 2),
        }

    return {
        zone: sorted(values.values(), key=lambda x: (x["date"], x["period"]))
        for zone, values in by_zone.items()
    }


# ============================================================================
# SYNTHETIC QUARTER-HOURLY DATA BEFORE 1 OCTOBER 2025
# ============================================================================


def expand_hourly_to_quarter_hourly(hourly):
    """
    Before 1 October 2025, GME does not provide PT15 MGP results.
    Each hourly zonal price is therefore expanded into four
    quarter-hour observations with the same price.
    """

    output = []

    for row in hourly:

        date_string = row["date"]
        hour = int(row["hour"])
        price = float(row["price"])

        first_quarter_period = ((hour - 1) * 4) + 1

        for quarter_offset in range(4):

            quarter_period = first_quarter_period + quarter_offset

            output.append(
                {
                    "date": date_string,
                    "time": market_time_from_period(quarter_period, 15),
                    "period": quarter_period,
                    "price": round(price, 2),
                }
            )

    return output


# ============================================================================
# MERGE SERIES
# ============================================================================


def merge_series(existing, new, key_field="period"):
    """
    Merge records using date + key_field.

    For hourly data:        key_field = "hour"
    For quarter-hourly data: key_field = "period"
    """

    merged = {}

    for row in existing:

        key = (row.get("date"), row.get(key_field))

        if key[0] is not None and key[1] is not None:
            merged[key] = row

    for row in new:

        key = (row.get("date"), row.get(key_field))

        if key[0] is not None and key[1] is not None:
            merged[key] = row

    return sorted(merged.values(), key=lambda x: (x["date"], x[key_field]))


# ============================================================================
# DAILY AVERAGES
# ============================================================================


def build_daily_from_quarter_hourly(quarter_hourly):
    """
    Calculate the daily average zonal price from the quarter-hourly
    series. Before 1 October 2025, each hourly price is repeated four
    times, so this produces the same arithmetic average as the hourly
    series.
    """

    grouped = defaultdict(list)

    for row in quarter_hourly:
        grouped[row["date"]].append(float(row["price"]))

    output = []

    for date_string in sorted(grouped):

        values = grouped[date_string]

        if not values:
            continue

        average = sum(values) / len(values)

        output.append(
            {
                "date": date_string,
                "price": round(average, 2),
            }
        )

    return output


# ============================================================================
# LOAD EXISTING DATA
# ============================================================================


def empty_zone_series():
    return {"daily": [], "hourly": [], "quarter_hourly": []}


def load_existing():

    if not os.path.exists(OUTPUT_PATH):
        return {
            "source": "GME - MGP Zonal Prices (Italian bidding zones)",
            "zones": {zone: empty_zone_series() for zone in ITALIAN_ZONES},
        }

    payload = compact.load(OUTPUT_PATH)

    zones = payload.get("zones", {})

    for zone in ITALIAN_ZONES:
        zones.setdefault(zone, empty_zone_series())

    return {
        "source": payload.get(
            "source", "GME - MGP Zonal Prices (Italian bidding zones)"
        ),
        "zones": zones,
    }


# ============================================================================
# OUTPUT
# ============================================================================


def build_output(zones_output):

    # Quarter-hour points before PT15_START are only the hourly price
    # repeated four times, so they are not stored: the dashboard rebuilds
    # them from the hourly series, and load_existing() ignores them anyway.
    zones = {
        zone: {
            "daily": compact.encode_series(series["daily"], "daily", "price"),
            "hourly": compact.encode_series(series["hourly"], "hourly", "price"),
            "quarter_hourly": compact.encode_series(
                series["quarter_hourly"],
                "quarter_hourly",
                "price",
                skip_before=PT15_START.isoformat(),
            ),
        }
        for zone, series in zones_output.items()
    }

    return {
        "source": "GME - MGP Zonal Prices (Italian bidding zones)",
        "description": (
            "MGP day-ahead zonal electricity prices for the Italian "
            "bidding zones (NORD, CNOR, CSUD, SUD, CALA, SICI, SARD). "
            "Hourly data uses GME historical/default granularity before "
            "1 October 2025 and PT60 from 1 October 2025 onward. "
            "Quarter-hourly data uses native GME PT15 results from "
            "1 October 2025 onward. Before 1 October 2025, hourly "
            "prices are repeated across four quarter-hour intervals."
        ),
        "quarter_hourly_native_from": PT15_START.isoformat(),
        "zones": zones,
    }


# ============================================================================
# MAIN
# ============================================================================


def main():

    parser = argparse.ArgumentParser(
        description="Fetch GME MGP zonal prices for Italian bidding zones."
    )

    parser.add_argument(
        "--start",
        required=False,
        default=None,
        help=(
            "Start date, e.g. 20250101. Defaults to 7 days before --end "
            "(incremental update), or to 2025-01-01 if --full-history "
            "is set."
        ),
    )

    parser.add_argument(
        "--end",
        required=False,
        default=None,
        help="End date, e.g. 20260924. Defaults to today.",
    )

    parser.add_argument(
        "--full-history",
        action="store_true",
        help=(
            "Backfill the entire history from 2025-01-01 instead of "
            "doing a short incremental update."
        ),
    )

    args = parser.parse_args()

    end_date = parse_date(args.end) if args.end else date.today()

    if args.start:
        start_date = parse_date(args.start)
    elif args.full_history:
        start_date = DEFAULT_HISTORY_START
    else:
        # Incremental run: re-download the last 7 days to pick up any
        # late corrections published by GME, plus catch up on gaps.
        start_date = end_date - timedelta(days=7)

    start_date = max(start_date, DEFAULT_HISTORY_START)

    if end_date < start_date:
        raise ValueError("End date must not be before start date.")

    login = os.environ.get("GME_API_LOGIN")
    password = os.environ.get("GME_API_PASSWORD")

    if not login or not password:
        raise RuntimeError(
            "GME_API_LOGIN and GME_API_PASSWORD environment variables "
            "must be set."
        )

    print()
    print("=" * 70)
    print("GME ZONAL PRICES FETCH (Italian bidding zones)")
    print("=" * 70)
    print(f"Zones: {', '.join(ITALIAN_ZONES)}")
    print(f"Date range: {start_date:%Y-%m-%d} -> {end_date:%Y-%m-%d}")
    print()

    existing = load_existing()

    for zone in ITALIAN_ZONES:
        series = existing["zones"][zone]
        print(
            f"Existing {zone}: "
            f"{len(series['daily']):,} daily / "
            f"{len(series['hourly']):,} hourly / "
            f"{len(series['quarter_hourly']):,} quarter-hour points"
        )

    print()

    # ========================================================================
    # 1. HOURLY DATA
    # ========================================================================

    print("Downloading hourly zonal data...")

    historical_hourly_rows = []
    modern_hourly_rows = []

    historical_end = min(end_date, PT15_START - timedelta(days=1))

    if start_date <= historical_end:

        print(
            f"  Historical hourly data: {start_date:%Y-%m-%d} -> "
            f"{historical_end:%Y-%m-%d}"
        )

        historical_hourly_rows = request_data(
            login=login,
            password=password,
            start_date=start_date,
            end_date=historical_end,
            granularity=None,
        )

    modern_start = max(start_date, PT15_START)

    if modern_start <= end_date:

        print(
            f"  Modern hourly data (PT60): {modern_start:%Y-%m-%d} -> "
            f"{end_date:%Y-%m-%d}"
        )

        modern_hourly_rows = request_data(
            login=login,
            password=password,
            start_date=modern_start,
            end_date=end_date,
            granularity="PT60",
        )

    hourly_rows = historical_hourly_rows + modern_hourly_rows

    print(f"Hourly raw rows received in total: {len(hourly_rows):,}")

    new_hourly_by_zone = build_hourly_by_zone(hourly_rows)

    # ========================================================================
    # 2. NATIVE 15-MINUTE DATA
    # ========================================================================

    print()
    print("Downloading native 15-minute zonal data...")

    new_quarter_by_zone = {}

    quarter_start = max(start_date, PT15_START)

    if quarter_start <= end_date:

        quarter_rows = request_data(
            login=login,
            password=password,
            start_date=quarter_start,
            end_date=end_date,
            granularity="PT15",
        )

        print(f"15-minute raw rows received: {len(quarter_rows):,}")

        new_quarter_by_zone = build_quarter_hourly_by_zone(quarter_rows)

    else:
        print("No native 15-minute data requested.")

    # ========================================================================
    # 3. MERGE + SYNTHETIC 15-MINUTE DATA + DAILY, PER ZONE
    # ========================================================================

    print()
    print("Merging per-zone series...")

    zones_output = {}

    for zone in ITALIAN_ZONES:

        existing_zone = existing["zones"].get(zone, empty_zone_series())

        hourly = merge_series(
            existing_zone["hourly"],
            new_hourly_by_zone.get(zone, []),
            key_field="hour",
        )

        historical_hourly = [
            row for row in hourly if row["date"] < PT15_START.isoformat()
        ]

        synthetic_quarter_hourly = expand_hourly_to_quarter_hourly(
            historical_hourly
        )

        existing_native_quarter = [
            row
            for row in existing_zone["quarter_hourly"]
            if row["date"] >= PT15_START.isoformat()
        ]

        quarter_hourly = merge_series(
            existing_native_quarter,
            new_quarter_by_zone.get(zone, []),
        )

        quarter_hourly = [
            row
            for row in quarter_hourly
            if row["date"] >= PT15_START.isoformat()
        ]

        quarter_hourly.extend(synthetic_quarter_hourly)

        quarter_hourly = sorted(
            quarter_hourly, key=lambda x: (x["date"], x["period"])
        )

        daily = build_daily_from_quarter_hourly(quarter_hourly)

        zones_output[zone] = {
            "daily": daily,
            "hourly": hourly,
            "quarter_hourly": quarter_hourly,
        }

        print(
            f"  {zone}: {len(daily):,} daily / {len(hourly):,} hourly / "
            f"{len(quarter_hourly):,} quarter-hour points"
        )

    # ========================================================================
    # 4. WRITE JSON
    # ========================================================================

    compact.dump(build_output(zones_output), OUTPUT_PATH)

    print()
    print("=" * 70)
    print(f"Wrote: {OUTPUT_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    main()
