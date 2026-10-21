"""
Fetch the history the price forecast learns from and write
app/data/forecast/inputs.json (Forecast tab, etl/forecast_backtest.py):

  hourly, per Italian bidding zone (etl/zones.py), from 1 January 2021 (the
  zones as they are now: Calabria a zone of its own since that day):
    <ZONE>|price            Energy Prices 12.1.D (A44), the MGP's price
    <ZONE>|load_forecast    Day-ahead total load forecast 6.1.B (A65, A01)
    <ZONE>|wind_forecast    Day-ahead wind onshore forecast 14.1.D (A69, A01, B19)
    <ZONE>|solar_forecast   Day-ahead solar forecast 14.1.D (A69, A01, B16)
  hourly, the coupled neighbours' day-ahead prices:
    FR|price, CH|price, AT|price, SI|price, GR|price
  daily:
    eua                     EU ETS1 auction clearing price on EEX (auction
                            days only; etl/fetch_eua.py's source)

All from the ENTSO-E Transparency Platform except the EUA price.  Values are
EUR/MWh and MW; hourly = average of the quarter-hours, on the Italian
market-time basis of the other files (labelled by elapsed time since local
midnight), so a zone's days laid end to end are one unbroken hourly series.
The gas price at the PSV comes from app/data/gas_prices.json (GME, private).

  fetch_forecast_inputs.py                  the last 7 days and up to tomorrow
  fetch_forecast_inputs.py --full-history   from 1 January 2021
  fetch_forecast_inputs.py --start 20210101 --end 20211231

The file stays in the private repository's data branch with the forecast.
The API token is read from the ENTSOE_API_KEY environment variable.
"""

import argparse
import io
import os
import time
import zipfile
from datetime import date, timedelta

import requests

import compact
import fetch_eua
from entsoe_api import (
    REQUEST_PAUSE_SECONDS,
    build_resolutions,
    day_chunks,
    market_today,
    merge_group_series,
    parse_date,
    parse_points,
    request_entsoe,
    to_api_datetime,
)
from fetch_crossborder import BORDERS
from fetch_neighbour_prices import finest_resolution, is_day_ahead
from fetch_res_forecasts import production_type
from zones import DATA_DIR, ZONE_DOMAINS

# ============================================================================
# CONFIGURATION
# ============================================================================

OUTPUT_PATH = os.path.join(DATA_DIR, "forecast", "inputs.json")

HISTORY_START = date(2021, 1, 1)

# The documents allow a year per request.
CHUNK_DAYS = 365

# Incremental runs re-download this many days to pick up revisions.
LOOKBACK_DAYS = 7

# The neighbours coupled with Italy in the MGP, and Switzerland.
NEIGHBOURS = ("FR", "CH", "AT", "SI", "GR")

SERIES = {
    "price": lambda eic: {"documentType": "A44", "in_Domain": eic, "out_Domain": eic},
    "load_forecast": lambda eic: {"documentType": "A65", "processType": "A01", "outBiddingZone_Domain": eic},
    # Wind and solar come in one document, split by production type below.
    "res_forecast": lambda eic: {"documentType": "A69", "processType": "A01", "in_Domain": eic},
}

RES_TYPES = {"B19": "wind_forecast", "B16": "solar_forecast"}


# ============================================================================
# DOWNLOAD
# ============================================================================


def download(token, area, eic, kind, start_date, end_date):
    """{series name: hourly rows} of one area and document, or None if every
    request failed."""
    records = []
    failures = 0
    requests_made = 0

    if kind == "price":
        group_of, keep = (lambda ts: "price"), is_day_ahead
    elif kind == "load_forecast":
        group_of, keep = (lambda ts: "load_forecast"), (lambda ts: True)
    else:
        group_of = lambda ts: RES_TYPES.get(production_type(ts))
        keep = lambda ts: production_type(ts) in RES_TYPES

    for chunk_start, chunk_end in day_chunks(start_date, end_date, CHUNK_DAYS):
        print(f"  {area} {kind}: {chunk_start} -> {chunk_end}")
        requests_made += 1
        try:
            root = request_entsoe(token, {
                **SERIES[kind](eic),
                "periodStart": to_api_datetime(chunk_start),
                "periodEnd": to_api_datetime(chunk_end, end_of_day=True),
            })
        except requests.HTTPError as error:
            failures += 1
            print(f"    WARNING: {error}")
            continue
        if root is None:
            print("    No matching data")
        records.extend(parse_points(root, group_of=group_of, keep=keep))
        time.sleep(REQUEST_PAUSE_SECONDS)

    if requests_made and failures == requests_made:
        return None
    if kind == "price":
        # A day published at two resolutions: the finest only.
        records = finest_resolution(records)
    hourly = build_resolutions(records, daily="mean")["hourly"]
    return {f"{area}|{name}": rows for name, rows in hourly.items()}


def download_eua(first_year, last_year):
    """EU auction prices {date: price} of the years given."""
    archive = None
    prices = {}
    for year in range(first_year, last_year + 1):
        try:
            content = fetch_eua.download(fetch_eua.YEAR_URL.format(year=year))
        except requests.HTTPError:
            if archive is None:
                archive = zipfile.ZipFile(io.BytesIO(fetch_eua.download(fetch_eua.ARCHIVE_URL)))
            content = fetch_eua.year_report(year, archive)
        auctions = fetch_eua.parse_report(content)
        print(f"  EUA {year}: {len(auctions)} auctions")
        prices.update({auction["date"]: auction["price"] for auction in auctions})
    return prices


# ============================================================================
# LOAD / SAVE
# ============================================================================


def load_existing(path=OUTPUT_PATH):
    """({series: hourly rows}, {date: EUA price})."""
    if not os.path.exists(path):
        return {}, {}
    payload = compact.load(path)
    eua = {row["date"]: row["value"] for row in payload.get("daily", {}).get("eua", [])}
    return dict(payload.get("hourly", {})), eua


def build_output(hourly, eua):
    return {
        "source": "ENTSO-E Transparency Platform; EEX (EUA auctions)",
        "description": (
            "Inputs of the day-ahead price forecast. Hourly, per Italian bidding zone: "
            "<ZONE>|price (12.1.D, A44, EUR/MWh), <ZONE>|load_forecast (6.1.B, A65 A01, MW), "
            "<ZONE>|wind_forecast and <ZONE>|solar_forecast (14.1.D, A69 A01, MW); the "
            "neighbours' day-ahead prices <COUNTRY>|price. Hourly = average of the "
            "quarter-hours, Italian market time labelled by elapsed time since local "
            "midnight. Daily: eua = EU ETS1 auction clearing price on EEX (EUR/tCO2), "
            "auction days only."
        ),
        "hourly": {name: compact.encode_series(rows, "hourly") for name, rows in sorted(hourly.items())},
        "daily": {"eua": compact.encode_series(
            [{"date": day, "value": price} for day, price in sorted(eua.items())], "daily")},
    }


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="Fetch the price forecast's inputs from ENTSO-E and EEX.")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--full-history", action="store_true",
                        help="Rebuild from 2021-01-01 instead of merging onto existing data")
    args = parser.parse_args()

    existing, eua = ({}, {}) if args.full_history else load_existing()

    # Tomorrow's forecasts (and, after the auction, prices) are published the day before.
    end_date = parse_date(args.end) if args.end else market_today() + timedelta(days=1)
    if args.start:
        start_date = parse_date(args.start)
    elif args.full_history or not existing:
        start_date = HISTORY_START
    else:
        start_date = market_today() - timedelta(days=LOOKBACK_DAYS)
    start_date = max(start_date, HISTORY_START)

    token = os.environ.get("ENTSOE_API_KEY")
    if not token:
        raise RuntimeError("ENTSOE_API_KEY environment variable must be set.")

    areas = [(zone, eic, ("price", "load_forecast", "res_forecast")) for zone, eic in ZONE_DOMAINS.items()]
    areas += [(code, BORDERS[code][1], ("price",)) for code in NEIGHBOURS]

    new = {}
    failed = []
    for area, eic, kinds in areas:
        for kind in kinds:
            series = download(token, area, eic, kind, start_date, end_date)
            if series is None:
                failed.append(f"{area} {kind}")
                continue
            new.update(series)

    if failed and len(failed) == sum(len(kinds) for _, _, kinds in areas):
        raise RuntimeError("Every request failed.")

    hourly = merge_group_series(existing, new)

    # EUA: the years not on file yet, and always the current one.
    have_years = {int(day[:4]) for day in eua}
    first_missing = next((year for year in range(HISTORY_START.year, end_date.year + 1)
                          if year not in have_years), end_date.year)
    eua.update(download_eua(min(first_missing, end_date.year), end_date.year))

    compact.dump(build_output(hourly, eua), OUTPUT_PATH)

    for name, rows in sorted(hourly.items()):
        print(f"  {name}: {len(rows)} hours, {rows[0]['date']} to {rows[-1]['date']}" if rows else f"  {name}: empty")
    print(f"  eua: {len(eua)} auctions, {min(eua)} to {max(eua)}")
    if failed:
        print(f"  Failed, left for a later run: {', '.join(failed)}")


if __name__ == "__main__":
    main()
