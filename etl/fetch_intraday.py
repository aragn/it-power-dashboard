"""
Fetch GME intraday auction (MI-A1, MI-A2, MI-A3) zonal prices for the
Italian bidding zones and write app/data/intraday_prices.json.

The three MI-A auctions replaced the old MI1..MI7 sessions in June 2024
and are coupled with the European intraday auctions (IDA1-3):
  - MI-A1: held on D-1 afternoon, covers all of day D
  - MI-A2: held on D-1 evening, covers all of day D
  - MI-A3: held on day D morning, covers only the second half of day D

GME returns them at their native granularity (hourly before the 15-minute
market time unit, 15-minute from 1 October 2025); GranularityType is only
valid for MGP.  Hourly values from 15-minute results are the average of
the quarter-hours, and daily values the average of the day's periods, so
an MI-A3 "daily" value only covers the hours that auction trades.

Credentials come from GME_API_LOGIN / GME_API_PASSWORD.
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
    PT15_START,
    REQUEST_PAUSE_SECONDS,
    filter_zonal,
    market_time_from_period,
    parse_date,
    request_data,
)

MARKETS = ["MI-A1", "MI-A2", "MI-A3"]

RESOLUTIONS = ("quarter_hourly", "hourly", "daily")

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "app",
    "data",
    "intraday_prices.json",
)

# Incremental runs re-download this many days to pick up corrections.
LOOKBACK_DAYS = 7


# ============================================================================
# SERIES
# ============================================================================


def minutes_of(label):
    hours, minutes = (int(part) for part in label.split(":"))
    return hours * 60 + minutes


def sort_rows(rows):
    return sorted(rows, key=lambda r: (r["date"], minutes_of(r["time"]) if "time" in r else 0))


def build_series(rows):
    """
    {zone: {"quarter_hourly": [...], "hourly": [...], "daily": [...]}} from
    raw GME rows.  A day with more than 25 periods is 15-minute data.
    """
    by_day = defaultdict(dict)
    for row in filter_zonal(rows):
        zone = str(row["Zone"]).strip().upper()
        flow_date = datetime.strptime(str(row["FlowDate"]), "%Y%m%d").date().isoformat()
        by_day[(zone, flow_date)][int(row["Period"])] = round(float(row["Price"]), 2)

    output = {zone: {resolution: [] for resolution in RESOLUTIONS} for zone in ITALIAN_ZONES}

    for (zone, day), prices in sorted(by_day.items()):
        quarter = max(prices) > 25
        step = 15 if quarter else 60

        points = [
            {"date": day, "time": market_time_from_period(period, step), "price": price}
            for period, price in sorted(prices.items())
        ]

        if quarter:
            output[zone]["quarter_hourly"].extend(points)
            by_hour = defaultdict(list)
            for point in points:
                by_hour[minutes_of(point["time"]) // 60].append(point["price"])
            hourly = [
                {"date": day, "time": f"{hour:02d}:00", "price": round(sum(v) / len(v), 2)}
                for hour, v in sorted(by_hour.items())
            ]
        else:
            hourly = points

        output[zone]["hourly"].extend(hourly)
        values = list(prices.values())
        output[zone]["daily"].append({"date": day, "price": round(sum(values) / len(values), 2)})

    return output


def merge_rows(existing, new):
    """Merge by (date, time); new rows win."""
    merged = {(r["date"], r.get("time")): r for r in existing}
    merged.update({(r["date"], r.get("time")): r for r in new})
    return sort_rows(merged.values())


# ============================================================================
# LOAD / SAVE
# ============================================================================


def empty_market():
    return {zone: {resolution: [] for resolution in RESOLUTIONS} for zone in ITALIAN_ZONES}


def load_existing():
    markets = {market: empty_market() for market in MARKETS}
    if not os.path.exists(OUTPUT_PATH):
        return markets

    payload = compact.load(OUTPUT_PATH)
    for market in MARKETS:
        for zone, series in payload.get("markets", {}).get(market, {}).items():
            if zone in markets[market]:
                for resolution in RESOLUTIONS:
                    markets[market][zone][resolution] = series.get(resolution, [])
    return markets


def build_output(markets):
    return {
        "source": "GME - MI-A intraday auction zonal prices (Italian bidding zones)",
        "description": (
            "Zonal prices of the GME intraday auctions MI-A1, MI-A2 and "
            "MI-A3 for the Italian bidding zones, EUR/MWh. Native hourly "
            "results before 1 October 2025 and 15-minute results from then "
            "on. Hourly = average of the quarter-hours; daily = average of "
            "the day's periods (MI-A3 covers only part of the day). "
            "Quarter-hourly series start at quarter_hourly_native_from; "
            "earlier quarter-hours repeat the hourly value."
        ),
        "quarter_hourly_native_from": PT15_START.isoformat(),
        "markets": {
            market: {
                zone: {
                    resolution: compact.encode_series(
                        series[resolution],
                        resolution,
                        "price",
                        skip_before=PT15_START.isoformat() if resolution == "quarter_hourly" else None,
                    )
                    for resolution in RESOLUTIONS
                }
                for zone, series in zones.items()
            }
            for market, zones in markets.items()
        },
    }


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="Fetch GME MI-A intraday zonal prices.")
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
    print("GME INTRADAY AUCTION ZONAL PRICES (MI-A1, MI-A2, MI-A3)")
    print("=" * 70)
    print(f"Date range: {start_date} -> {end_date}")
    print()

    existing = {market: empty_market() for market in MARKETS} if args.full_history else load_existing()
    markets = {}

    for index, market in enumerate(MARKETS):
        if index:
            time.sleep(REQUEST_PAUSE_SECONDS)
        print(f"Downloading {market}...")
        rows = request_data(login, password, start_date, end_date, segment=market)
        print(f"  {len(rows):,} raw rows")

        new = build_series(rows)
        markets[market] = {
            zone: {
                resolution: merge_rows(existing[market][zone][resolution], new[zone][resolution])
                for resolution in RESOLUTIONS
            }
            for zone in ITALIAN_ZONES
        }
        nord = markets[market]["NORD"]
        print(f"  NORD: {len(nord['daily']):,} days, {len(nord['quarter_hourly']):,} quarter-hours")
        print()

    if not any(markets[m]["NORD"]["daily"] for m in MARKETS):
        raise RuntimeError("No intraday data downloaded; not writing output.")

    compact.dump(build_output(markets), OUTPUT_PATH)
    print("=" * 70)
    print(f"Wrote: {OUTPUT_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    main()
