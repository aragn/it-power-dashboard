"""
Fetch GME intraday auction (MI-A1, MI-A2, MI-A3) zonal prices for the
Italian bidding zones and write app/data/intraday_prices.json, with each
auction's zonal volumes (ME_ZonalVolumes: what the zone's sellers sold and
its buyers bought, MW per quarter-hour) for the battery calculator's cap on
the share of an auction a battery can take, and the MGP's zonal volumes
alongside (the Prices tab's quantities by market).

The three MI-A auctions replaced the old MI1..MI7 sessions in June 2024
and are coupled with the European intraday auctions (IDA1-3):
  - MI-A1: held on D-1 afternoon, covers all of day D
  - MI-A2: held on D-1 evening, covers all of day D
  - MI-A3: held on day D morning, covers only the second half of day D

GME returns them at their native granularity (15-minute for the whole
history since 2025, unlike MGP, which only went 15-minute on 1 October
2025); GranularityType is only valid for MGP.  Hourly days are still
handled in case older data is requested.  Hourly values from 15-minute results are the average of
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
    REQUEST_PAUSE_SECONDS,
    filter_zonal,
    market_time_from_period,
    parse_date,
    request_data,
)

MARKETS = ["MI-A1", "MI-A2", "MI-A3"]

RESOLUTIONS = ("quarter_hourly", "hourly", "daily")

# GME's zonal volumes: "Sold" and "Purchased" of each period, MW.
VOLUME_DATA_NAME = "ME_ZonalVolumes"
VOLUME_SIDES = {"sold": "Sold", "bought": "Purchased"}
VOLUME_MARKETS = ["MGP"] + MARKETS

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


def number(value):
    """GME's numbers come as text, "null" where there is none."""
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if not text or text.lower() == "null":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def build_volumes(rows):
    """
    {zone: {"sold": [...], "bought": [...]}}, quarter-hourly rows of MW, from
    GME's ME_ZonalVolumes rows.  An hourly day (the MGP before 1 October
    2025) repeats each hour's MW over its four quarter-hours.
    """
    by_day = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        zone = str(row.get("Zone") or "").strip().upper()
        if zone not in ITALIAN_ZONES or row.get("FlowDate") is None or row.get("Period") is None:
            continue
        flow_date = datetime.strptime(str(row["FlowDate"]), "%Y%m%d").date().isoformat()
        for side, field in VOLUME_SIDES.items():
            value = number(row.get(field))
            if value is not None:
                by_day[(zone, flow_date)][side][int(row["Period"])] = round(value, 1)

    output = {zone: {side: [] for side in VOLUME_SIDES} for zone in ITALIAN_ZONES}
    for (zone, day), sides in sorted(by_day.items()):
        hourly = max((max(values) for values in sides.values() if values), default=0) <= 25
        for side, values in sides.items():
            for period, value in sorted(values.items()):
                quarters = range((period - 1) * 4 + 1, period * 4 + 1) if hourly else (period,)
                output[zone][side].extend(
                    {"date": day, "time": market_time_from_period(quarter, 15), "mw": value} for quarter in quarters
                )
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


def empty_volumes():
    return {zone: {side: [] for side in VOLUME_SIDES} for zone in ITALIAN_ZONES}


def load_existing():
    """(prices, volumes) of the file on disk."""
    markets = {market: empty_market() for market in MARKETS}
    volumes = {market: empty_volumes() for market in VOLUME_MARKETS}
    if not os.path.exists(OUTPUT_PATH):
        return markets, volumes

    payload = compact.load(OUTPUT_PATH)
    for market in MARKETS:
        for zone, series in payload.get("markets", {}).get(market, {}).items():
            if zone in markets[market]:
                for resolution in RESOLUTIONS:
                    markets[market][zone][resolution] = series.get(resolution, [])
    for market in VOLUME_MARKETS:
        for zone, sides in payload.get("volumes", {}).get(market, {}).items():
            if zone in volumes[market]:
                for side in VOLUME_SIDES:
                    volumes[market][zone][side] = sides.get(side, [])
    return markets, volumes


def build_output(markets, volumes=None):
    return {
        "source": "GME - MI-A intraday auction zonal prices (Italian bidding zones)",
        "description": (
            "Zonal prices of the GME intraday auctions MI-A1, MI-A2 and "
            "MI-A3 for the Italian bidding zones, EUR/MWh, at GME's native "
            "15-minute resolution. Hourly = average of the quarter-hours; "
            "daily = average of the day's periods (MI-A3 covers only part "
            "of the day). volumes: the zonal volumes of the MGP and of each "
            "auction per quarter-hour, MW, sold (the zone's sellers) and "
            "bought (its buyers); the MGP's hours before 1 October 2025 "
            "repeated over their quarter-hours."
        ),
        # Native quarter-hours for the whole history.
        "quarter_hourly_native_from": DEFAULT_HISTORY_START.isoformat(),
        "markets": {
            market: {
                zone: {
                    resolution: compact.encode_series(series[resolution], resolution, "price")
                    for resolution in RESOLUTIONS
                }
                for zone, series in zones.items()
            }
            for market, zones in markets.items()
        },
        "volumes": {
            market: {
                zone: {side: compact.encode_series(rows, "quarter_hourly", "mw") for side, rows in sides.items()}
                for zone, sides in zones.items()
            }
            for market, zones in (volumes or {}).items()
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
    parser.add_argument("--volumes-only", action="store_true",
                        help="Download only the zonal volumes, keeping the prices on file (half the API calls)")
    parser.add_argument("--volume-markets", nargs="+", choices=VOLUME_MARKETS, default=VOLUME_MARKETS,
                        help="The markets whose volumes to download (the others' on file are kept)")
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

    if args.full_history:
        existing = {market: empty_market() for market in MARKETS}
        existing_volumes = {market: empty_volumes() for market in VOLUME_MARKETS}
    else:
        existing, existing_volumes = load_existing()
    markets, volumes = {}, {}

    for index, market in enumerate(MARKETS):
        if index:
            time.sleep(REQUEST_PAUSE_SECONDS)
        print(f"Downloading {market}...")
        if args.volumes_only:
            new = empty_market()
        else:
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

    # The volumes; without them the prices still go out, with the volumes on file.
    for index, market in enumerate(VOLUME_MARKETS):
        if market not in args.volume_markets:
            volumes[market] = existing_volumes[market]
            continue
        if index or not args.volumes_only:
            time.sleep(REQUEST_PAUSE_SECONDS)
        print(f"Downloading {market} volumes...")
        try:
            new_volumes = build_volumes(
                request_data(login, password, start_date, end_date, segment=market, data_name=VOLUME_DATA_NAME))
        except Exception as error:
            print(f"  {VOLUME_DATA_NAME} {market}: failed ({error}); the volumes on file are kept")
            new_volumes = empty_volumes()
        volumes[market] = {
            zone: {side: merge_rows(existing_volumes[market][zone][side], new_volumes[zone][side]) for side in VOLUME_SIDES}
            for zone in ITALIAN_ZONES
        }
        print(f"  NORD volumes: {len(volumes[market]['NORD']['sold']):,} quarter-hours")
        print()

    if not any(markets[m]["NORD"]["daily"] for m in MARKETS):
        raise RuntimeError("No intraday data downloaded; not writing output.")

    compact.dump(build_output(markets, volumes), OUTPUT_PATH)
    print("=" * 70)
    print(f"Wrote: {OUTPUT_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    main()
