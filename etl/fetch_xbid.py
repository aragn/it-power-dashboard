"""
Fetch the GME MI-XBID continuous intraday trading results for the Italian
bidding zones and write app/data/xbid_prices.json.

MI-XBID is the Italian part of the European continuous intraday market
(SIDC/XBID).  GME's ME_XBIDResults (segment XBID) returns, per flow date,
zone and phase:
  - Period 0      the 60-minute product of that Hour (1-25)
  - Period 1-100  the 15-minute products
  - Phase 0       "Esiti totali", all trades of the product; phases 1 and 2
                  are the results after the first and second trading phase
                  (Phase 0 for a day is published the day after)
The price kept is ReferencePrice ("Riferimento"): the volume-weighted
average price of all trades concluded for the product.  A product without
trades in a zone has no price ("null") and is left out.

The 60-minute series has hourly and daily values (the dashboard repeats the
hourly price over the quarter-hours in its 15-minute view).  The 15-minute
series has quarter-hourly values, hourly = average of the traded
quarter-hours and daily = average of the day's traded quarter-hours; the
60-minute daily value is the average of the traded hours.

Italian market time, labelled by elapsed time since local midnight like
the other GME series.  Credentials come from GME_API_LOGIN /
GME_API_PASSWORD.
"""

import argparse
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import compact
from fetch_zonal import (
    DEFAULT_HISTORY_START,
    ITALIAN_ZONES,
    REQUEST_PAUSE_SECONDS,
    get_token,
    market_time_from_period,
    month_ranges,
    parse_date,
    request_chunk,
)

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "app",
    "data",
    "xbid_prices.json",
)

SEGMENT = "XBID"
DATA_NAME = "ME_XBIDResults"

# "Esiti totali": every trade of the product, whichever trading phase.
TOTAL_PHASE = "0"

PRODUCTS = {
    "XBID-60": ("hourly", "daily"),
    "XBID-15": ("quarter_hourly", "hourly", "daily"),
}

# Incremental runs re-download this many days to pick up corrections.
LOOKBACK_DAYS = 7


# ============================================================================
# PARSING
# ============================================================================


def number(value):
    """GME sends numbers as strings, and "null" where there were no trades."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    return float(text)


def minutes_of(label):
    hours, minutes = (int(part) for part in label.split(":"))
    return hours * 60 + minutes


def extract_prices(rows):
    """
    {(product, zone, date): {time label: reference price}} from raw GME
    rows: the total phase of the Italian zones, traded products only.
    """
    prices = defaultdict(dict)
    for row in rows:
        if str(row.get("Phase")).strip() != TOTAL_PHASE:
            continue
        zone = str(row.get("Zone") or "").strip().upper()
        if zone not in ITALIAN_ZONES:
            continue
        price = number(row.get("ReferencePrice"))
        if price is None:
            continue

        flow_date = datetime.strptime(str(row["FlowDate"]), "%Y%m%d").date().isoformat()
        period = int(row["Period"])
        if period == 0:
            key, label = "XBID-60", market_time_from_period(int(row["Hour"]), 60)
        else:
            key, label = "XBID-15", market_time_from_period(period, 15)
        prices[(key, zone, flow_date)][label] = round(price, 2)
    return prices


def average(values):
    return round(sum(values) / len(values), 2)


def build_series(prices):
    """{product: {zone: {resolution: rows}}} from extract_prices() output."""
    output = {product: {zone: {res: [] for res in resolutions} for zone in ITALIAN_ZONES}
              for product, resolutions in PRODUCTS.items()}

    for (product, zone, day), slots in sorted(prices.items()):
        ordered = sorted(slots.items(), key=lambda item: minutes_of(item[0]))
        series = output[product][zone]

        if product == "XBID-15":
            series["quarter_hourly"].extend(
                {"date": day, "time": label, "price": price} for label, price in ordered
            )
            by_hour = defaultdict(list)
            for label, price in ordered:
                by_hour[minutes_of(label) // 60].append(price)
            series["hourly"].extend(
                {"date": day, "time": f"{hour:02d}:00", "price": average(values)}
                for hour, values in sorted(by_hour.items())
            )
        else:
            series["hourly"].extend(
                {"date": day, "time": label, "price": price} for label, price in ordered
            )

        series["daily"].append({"date": day, "price": average([price for _, price in ordered])})

    return output


def merge_rows(existing, new):
    """Merge by (date, time); new rows win."""
    merged = {(r["date"], r.get("time")): r for r in existing}
    merged.update({(r["date"], r.get("time")): r for r in new})
    return sorted(merged.values(), key=lambda r: (r["date"], minutes_of(r["time"]) if "time" in r else 0))


# ============================================================================
# DOWNLOAD
# ============================================================================


def download(login, password, start_date, end_date):
    """
    Prices of every monthly chunk, filtered as soon as each chunk arrives
    (a month is ~190k rows across all zones and phases).
    """
    token = get_token(login, password)
    prices = {}
    chunks = list(month_ranges(start_date, end_date))
    print(f"  {DATA_NAME} {SEGMENT}: {len(chunks)} monthly API request(s)")

    for index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        print(f"    [{index}/{len(chunks)}] {chunk_start} -> {chunk_end}")
        try:
            rows = request_chunk(token, chunk_start, chunk_end, segment=SEGMENT, data_name=DATA_NAME)
        except PermissionError:
            print("    Token expired. Re-authenticating...")
            token = get_token(login, password)
            rows = request_chunk(token, chunk_start, chunk_end, segment=SEGMENT, data_name=DATA_NAME)

        found = extract_prices(rows)
        print(f"    {len(rows):,} rows, {sum(len(v) for v in found.values()):,} traded products kept")
        prices.update(found)
        del rows

        if index < len(chunks):
            time.sleep(REQUEST_PAUSE_SECONDS)

    return prices


# ============================================================================
# LOAD / SAVE
# ============================================================================


def empty_markets():
    return {product: {zone: {res: [] for res in resolutions} for zone in ITALIAN_ZONES}
            for product, resolutions in PRODUCTS.items()}


def load_existing():
    markets = empty_markets()
    if not os.path.exists(OUTPUT_PATH):
        return markets

    payload = compact.load(OUTPUT_PATH)
    for product, resolutions in PRODUCTS.items():
        for zone, series in payload.get("markets", {}).get(product, {}).items():
            if zone in markets[product]:
                for resolution in resolutions:
                    markets[product][zone][resolution] = series.get(resolution, [])
    return markets


def build_output(markets):
    return {
        "source": "GME - MI-XBID continuous intraday trading results (Italian bidding zones)",
        "description": (
            "Reference prices (volume-weighted average price of all trades, "
            "'Esiti totali') of the MI-XBID 60-minute and 15-minute products "
            "for the Italian bidding zones, EUR/MWh. 60-minute: hourly, daily "
            "= average of the traded hours. 15-minute: quarter-hourly, hourly "
            "= average of the traded quarter-hours, daily = average of the "
            "day's traded quarter-hours. Products without trades have no value."
        ),
        "markets": {
            product: {
                zone: {
                    resolution: compact.encode_series(series[resolution], resolution, "price")
                    for resolution in PRODUCTS[product]
                }
                for zone, series in zones.items()
            }
            for product, zones in markets.items()
        },
    }


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="Fetch GME MI-XBID reference prices per Italian zone.")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--full-history", action="store_true",
                        help="Rebuild from 2025-01-01 instead of merging onto existing data")
    args = parser.parse_args()

    end_date = parse_date(args.end) if args.end else date.today()
    if args.start:
        start_date = parse_date(args.start)
    elif args.full_history:
        start_date = DEFAULT_HISTORY_START
    else:
        start_date = end_date - timedelta(days=LOOKBACK_DAYS)
    start_date = max(start_date, DEFAULT_HISTORY_START)

    if end_date < start_date:
        raise ValueError("End date must not be before start date.")

    login = os.environ.get("GME_API_LOGIN")
    password = os.environ.get("GME_API_PASSWORD")
    if not login or not password:
        raise RuntimeError("GME_API_LOGIN and GME_API_PASSWORD environment variables must be set.")

    print()
    print("=" * 70)
    print("GME MI-XBID CONTINUOUS INTRADAY REFERENCE PRICES")
    print("=" * 70)
    print(f"Date range: {start_date} -> {end_date}")
    print()

    prices = download(login, password, start_date, end_date)
    if not prices:
        raise RuntimeError("No MI-XBID totals downloaded; not writing output.")

    new = build_series(prices)
    existing = empty_markets() if args.full_history else load_existing()
    markets = {
        product: {
            zone: {
                resolution: merge_rows(existing[product][zone][resolution], new[product][zone][resolution])
                for resolution in resolutions
            }
            for zone in ITALIAN_ZONES
        }
        for product, resolutions in PRODUCTS.items()
    }

    for product in PRODUCTS:
        days = markets[product]["NORD"]["daily"]
        print(f"  {product} NORD: {len(days):,} days" + (f", last {days[-1]['date']}" if days else ""))

    compact.dump(build_output(markets), OUTPUT_PATH)
    print("=" * 70)
    print(f"Wrote: {OUTPUT_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    main()
