"""
Fetch the price spreads between Italy and its coupled neighbours from the
JAO Publication Tool for the Italy North CCR and the IBWT
(https://publicationtool.jao.eu/ibwt/), for the day-ahead market and the
three intraday auctions (IDA 1, 2 and 3).

JAO publishes, per market time unit and border, the price difference in the
direction of the border: border_X_Y = price(Y) - price(X).  The output keeps
the neighbour's price minus the Italian one (JAO's IT->X borders):
  AT, FR, SI: neighbour - Italy North (NORD)
  GR:         Greece - Italy South (SUD)
so a positive spread means the neighbour was dearer than Italy.

The API needs no key and answers at most 2 days per request.  Day-ahead
spreads are hourly until the 15-minute day-ahead market time unit
(1 October 2025); the intraday auctions' spreads are 15-minute throughout.
Hourly = average of the quarter-hours; daily = average of the hours (IDA 3
covers only the afternoon and evening, so its daily average is for those
hours).  Output: app/data/jao_spreads.json, EUR/MWh, Italian market time.

The same file holds the congestion income of each border and market
(JAO's gross congestion income per direction), signed like the spreads:
the income earned on exports from Italy (IT->X, the neighbour dearer)
above zero, on imports into Italy (X->IT, Italy dearer) below.  It is kept
as EUR per hour, so the hourly value is the hour's income and the daily
value (the sum of the hours) the day's; quarter-hours from 1 October 2025.
"""

import argparse
import os
import threading
import time
from datetime import datetime, timedelta

import requests

import compact
from entsoe_api import (
    day_chunks,
    local_midnight_utc,
    market_today,
    merge_resolutions,
    parse_date,
    point_label,
)

# ============================================================================
# CONFIGURATION
# ============================================================================

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "data", "jao_spreads.json")

API_BASE = "https://publicationtool.jao.eu/ibwt/api/data/"

# market code in the output -> JAO's prefix, label
MARKETS = {
    "DA": ("DA", "Day-ahead"),
    "IDA1": ("ID1", "IDA 1"),
    "IDA2": ("ID2", "IDA 2"),
    "IDA3": ("ID3", "IDA 3"),
}

# country -> (JAO field of the neighbour - Italy spread, label)
COUNTRIES = {
    "AT": ("border_IT_AT", "Austria"),
    "FR": ("border_IT_FR", "France"),
    "SI": ("border_IT_SI", "Slovenia"),
    "GR": ("border_IT_GR", "Greece"),
}

HISTORY_START = parse_date("2025-01-01")
# Before this the day-ahead market time unit was the hour; the intraday
# auctions were 15-minute from the start of the history.
QUARTER_HOURLY_NATIVE_FROM = {"DA": "2025-10-01", "IDA1": "2025-01-01", "IDA2": "2025-01-01", "IDA3": "2025-01-01"}

# The API answers at most 2 days per request, and "Too many requests"
# (429) beyond roughly 90 requests in half a minute: requests start at
# least REQUEST_INTERVAL_SECONDS apart (across threads), and a 429 is
# waited out.
CHUNK_DAYS = 2
PAUSE_SECONDS = 0.2
REQUEST_INTERVAL_SECONDS = 0.7
MAX_RETRIES = 3
TOO_MANY_REQUESTS_RETRIES = 6
TOO_MANY_REQUESTS_WAIT_SECONDS = 30

# Congestion income: quarter-hours kept from here (hourly before), to keep
# the file small.
INCOME_QUARTER_HOURLY_FROM = "2025-10-01"

# Incremental runs re-download this many days.
LOOKBACK_DAYS = 3

RESOLUTIONS = ("quarter_hourly", "hourly", "daily")


# ============================================================================
# DOWNLOAD
# ============================================================================


def request_chunks(start_day, end_day):
    """CHUNK_DAYS-day chunks, split into single days where the 25-hour day
    of the autumn clock change would make the chunk longer than 48 hours."""
    for first, last in day_chunks(start_day, end_day, CHUNK_DAYS):
        span = local_midnight_utc(last + timedelta(days=1)) - local_midnight_utc(first)
        if span <= timedelta(days=CHUNK_DAYS):
            yield first, last
        else:
            day = first
            while day <= last:
                yield day, day
                day += timedelta(days=1)


def jao_get(session, endpoint, first, last, extra=None):
    """Rows of a JAO IBWT endpoint for the market days first..last (Italian market time)."""
    params = {
        "FromUtc": local_midnight_utc(first).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        # Inclusive upper bound: the last market time unit of the last day.
        "ToUtc": (local_midnight_utc(last + timedelta(days=1)) - timedelta(minutes=15)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"),
        "Skip": 0,
        "Take": 5000,
        **(extra or {}),
    }
    failures = throttled = 0
    while True:
        pace()
        try:
            response = session.get(API_BASE + endpoint, params=params, timeout=120)
            if response.status_code == 429 and throttled < TOO_MANY_REQUESTS_RETRIES:
                throttled += 1
                print(f"    {endpoint} {first}: too many requests, waiting {TOO_MANY_REQUESTS_WAIT_SECONDS}s")
                time.sleep(TOO_MANY_REQUESTS_WAIT_SECONDS)
                continue
            response.raise_for_status()
            return response.json().get("data") or []
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if failures == MAX_RETRIES or (status is not None and status < 500):
                raise
            failures += 1
            print(f"    {endpoint} {first}: {error.__class__.__name__} {status or ''}, retrying")
            time.sleep(5 * 2 ** failures)


_pace_lock = threading.Lock()
_next_request = [0.0]


def pace():
    """Wait for this request's turn: REQUEST_INTERVAL_SECONDS after the previous one started."""
    with _pace_lock:
        now = time.monotonic()
        start = max(now, _next_request[0])
        _next_request[0] = start + REQUEST_INTERVAL_SECONDS
    time.sleep(max(0.0, start - now))


def market_time(row):
    """(market date, "HH:MM" label) of a JAO row's dateTimeUtc."""
    return point_label(datetime.fromisoformat(row["dateTimeUtc"].replace("Z", "+00:00")))


def quarter_hour_days(rows):
    """Market days with a row off the whole hour, i.e. in 15-minute steps."""
    return {day for day, label in map(market_time, rows) if not label.endswith(":00")}


def spread_records(market, rows):
    """
    Point records {"group", "date", "time", "minutes", "value"}, group
    "<market>|<country>".  A day whose rows are all on the hour is hourly.
    """
    quarter_days = quarter_hour_days(rows)
    records = []
    for row in rows:
        market_date, label = market_time(row)
        minutes = 15 if market_date in quarter_days else 60
        for country, (field, _) in COUNTRIES.items():
            value = row.get(field)
            if value is None:
                continue
            records.append({"group": f"{market}|{country}", "date": market_date, "time": label,
                            "minutes": minutes, "value": float(value)})
    return records


def income_records(market, rows):
    """
    Congestion income in EUR per hour, group "<market>|<country>|income":
    exports from Italy (IT->X) above zero, imports into Italy (X->IT) below.
    """
    quarter_days = quarter_hour_days(rows)
    records = []
    for row in rows:
        market_date, label = market_time(row)
        minutes = 15 if market_date in quarter_days else 60
        for country in COUNTRIES:
            exports, imports = row.get(f"grossBorder_IT_{country}"), row.get(f"grossBorder_{country}_IT")
            if exports is None and imports is None:
                continue
            records.append({"group": f"{market}|{country}|income", "date": market_date, "time": label,
                            "minutes": minutes, "value": ((exports or 0) - (imports or 0)) * 60 / minutes})
    return records


def download(start_day, end_day):
    """Spread records and congestion income records."""
    session = requests.Session()
    spreads, income = [], []
    for market, (jao_market, label) in MARKETS.items():
        count = 0
        for first, last in request_chunks(start_day, end_day):
            rows = jao_get(session, f"{jao_market}_priceSpread", first, last)
            count += len(rows)
            spreads += spread_records(market, rows)
            income += income_records(market, jao_get(session, f"{jao_market}_congestionIncome", first, last))
            time.sleep(PAUSE_SECONDS)
        print(f"  {label}: {count:,} market time units {start_day} -> {end_day}")
    return spreads, income


# ============================================================================
# LOAD / SAVE
# ============================================================================


def load_existing():
    """Spreads and income as {resolution: {"<market>|<country>[|income]": rows}}."""
    spreads = {resolution: {} for resolution in RESOLUTIONS}
    income = {resolution: {} for resolution in RESOLUTIONS}
    if not os.path.exists(OUTPUT_PATH):
        return spreads, income
    payload = compact.load(OUTPUT_PATH)
    for target, key, suffix in ((spreads, "markets", ""), (income, "income", "|income")):
        for market, by_country in payload.get(key, {}).items():
            for country, series in by_country.items():
                for resolution in RESOLUTIONS:
                    target[resolution][f"{market}|{country}{suffix}"] = series.get(resolution, [])
    return spreads, income


def encode_markets(series, suffix, quarter_hourly_from, rounding=None):
    """{market: {country: {resolution: compact series}}} of the groups present."""
    markets = {}
    for market in MARKETS:
        markets[market] = {}
        for country in COUNTRIES:
            group = f"{market}|{country}{suffix}"
            if not series["daily"].get(group):
                continue
            markets[market][country] = {}
            for resolution in RESOLUTIONS:
                rows = series[resolution].get(group, [])
                if rounding is not None:
                    rows = [{**row, "value": round(row["value"], rounding)} for row in rows]
                markets[market][country][resolution] = compact.encode_series(
                    rows, resolution,
                    skip_before=quarter_hourly_from(market) if resolution == "quarter_hourly" else None)
    return markets


def build_output(spreads, income):
    return {
        "source": "JAO Publication Tool, Italy North CCR & IBWT (publicationtool.jao.eu/ibwt)",
        "description": (
            "Price spreads, neighbour minus Italy, EUR/MWh: Austria, France and Slovenia against "
            "Italy North (NORD), Greece against Italy South (SUD); day-ahead market and intraday "
            "auctions IDA 1-3. Hourly = average of the quarter-hours; daily = average of the hours. "
            "Italian market time (Europe/Rome), labelled by elapsed time since local midnight. "
            "Quarter-hourly series start at quarter_hourly_native_from (per market); earlier "
            "quarter-hours repeat the hourly value. income: congestion income, EUR per hour, exports "
            "from Italy (IT->X) above zero and imports into Italy (X->IT) below; daily = the day's total; "
            "quarter-hourly from income_quarter_hourly_from."
        ),
        "quarter_hourly_native_from": QUARTER_HOURLY_NATIVE_FROM,
        "market_labels": {market: label for market, (_, label) in MARKETS.items()},
        "countries": {country: label for country, (_, label) in COUNTRIES.items()},
        "markets": encode_markets(spreads, "", QUARTER_HOURLY_NATIVE_FROM.get),
        "income_quarter_hourly_from": INCOME_QUARTER_HOURLY_FROM,
        "income": encode_markets(income, "|income", lambda market: INCOME_QUARTER_HOURLY_FROM, rounding=0),
    }


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="Fetch JAO day-ahead and intraday auction price spreads.")
    parser.add_argument("--start", help="First market day (YYYY-MM-DD or YYYYMMDD); default: LOOKBACK_DAYS ago")
    parser.add_argument("--end", help="Last market day; default: tomorrow")
    parser.add_argument("--full-history", action="store_true", help="Rebuild from 2025-01-01")
    args = parser.parse_args()

    today = market_today()
    end_day = parse_date(args.end) if args.end else today + timedelta(days=1)
    if args.start:
        start_day = parse_date(args.start)
    elif args.full_history:
        start_day = HISTORY_START
    else:
        start_day = today - timedelta(days=LOOKBACK_DAYS)
    start_day = max(start_day, HISTORY_START)
    if end_day < start_day:
        raise ValueError("End date must not be before start date.")

    print(f"JAO price spreads {start_day} -> {end_day}")
    if args.full_history:
        old_spreads, old_income = ({resolution: {} for resolution in RESOLUTIONS} for _ in range(2))
    else:
        old_spreads, old_income = load_existing()
    spread_rows, income_rows = download(start_day, end_day)
    if not spread_rows and not any(old_spreads["daily"].values()):
        raise RuntimeError("No price spreads downloaded; not writing output.")

    spreads = merge_resolutions(old_spreads, spread_rows, RESOLUTIONS, daily="mean")
    income = merge_resolutions(old_income, income_rows, RESOLUTIONS, daily="sum")
    for series in (spreads, income):
        for group in sorted(series["daily"]):
            days = series["daily"][group]
            print(f"  {group}: {len(days):,} days, {days[0]['date']} -> {days[-1]['date']}")

    compact.dump(build_output(spreads, income), OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH} ({os.path.getsize(OUTPUT_PATH) // 1024:,} KB)")


if __name__ == "__main__":
    main()
