"""
Fetch from the Terna public API (developer.terna.it) and write
app/data/terna.json:

  - geothermal   actual generation, primary source "Geothermal"
                 (/generation/v2.0/actual-generation).  ENTSO-E's
                 geothermal series for Italy (B09) is missing on many
                 days; where it is there it agrees with Terna within ~2%.
  - total_load   total load of Italy (/load/v2.0/total-load), which
                 includes self-consumption.  ENTSO-E's Italian "total
                 load" is Terna's market load, without it.

Terna publishes 15-minute values labelled by the start of the quarter-hour
in Italian local time, with the UTC offset (so the repeated hour of the
autumn DST change is unambiguous).  The generation values are named
"GWh" but are the average power in GW (checked against ENTSO-E), so both
series are stored in MW; hourly = average of the quarter-hours, daily =
sum of the hours (MWh/day), like the ENTSO-E series.  Italian market time,
labelled by elapsed time since local midnight like GME periods.

Credentials: TERNA_KEY / TERNA_SECRET (OAuth2 client credentials; a token
lasts 300 s).  The API allows at most 60 days per request and few requests
per second.
"""

import argparse
import os
import time
from datetime import datetime, timedelta, timezone

import requests

import compact
from entsoe_api import day_chunks, market_today, merge_resolutions, parse_date, point_label

TOKEN_URL = "https://api.terna.it/public-api/access-token"
API_BASE = "https://api.terna.it"

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "app",
    "data",
    "terna.json",
)

DEFAULT_HISTORY_START = datetime(2025, 1, 1).date()
CHUNK_DAYS = 30
# Generation is published with a delay of a few days; re-download enough
# to fill it in and pick up revisions.
LOOKBACK_DAYS = 10
PAUSE_SECONDS = 3
MAX_RETRIES = 6

RESOLUTIONS = ("quarter_hourly", "hourly", "daily")

SERIES = {
    "geothermal": {
        "path": "/generation/v2.0/actual-generation",
        "params": {"type": "Geothermal"},
        "records": "actual_generation",
        "value": "actual_generation_GWh",
        "scale": 1000,  # GW -> MW
        "label": "Geothermal generation (Terna actual generation)",
    },
    "total_load": {
        "path": "/load/v2.0/total-load",
        "params": {"biddingZone": "Italy"},
        "records": "total_load",
        "value": "total_load_MW",
        "scale": 1,
        "label": "Total load of Italy, incl. self-consumption (Terna)",
    },
}


# ============================================================================
# API
# ============================================================================


class Client:
    def __init__(self, key, secret):
        self.key, self.secret = key, secret
        self.token, self.token_time = None, 0.0

    def access_token(self):
        # Tokens last 300 s; renew well before.
        if self.token and time.time() - self.token_time < 240:
            return self.token
        response = requests.post(TOKEN_URL, data={
            "client_id": self.key,
            "client_secret": self.secret,
            "grant_type": "client_credentials",
        }, timeout=60)
        response.raise_for_status()
        self.token, self.token_time = response.json()["access_token"], time.time()
        time.sleep(PAUSE_SECONDS)  # the token call counts towards the rate limit
        return self.token

    def get(self, path, params):
        for attempt in range(MAX_RETRIES + 1):
            response = requests.get(API_BASE + path, params=params, timeout=120, headers={
                "Authorization": f"Bearer {self.access_token()}",
                "Accept": "application/json",
            })
            over_limit = response.status_code == 429 or (
                response.status_code == 403 and "Over Qps" in response.text)
            if over_limit and attempt < MAX_RETRIES:
                wait = PAUSE_SECONDS * 2 ** attempt
                print(f"    rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            if response.status_code == 401 and attempt < MAX_RETRIES:
                self.token = None
                continue
            response.raise_for_status()
            time.sleep(PAUSE_SECONDS)
            return response.json()
        raise RuntimeError(f"Terna API request failed: {path} {params}")


def record_label(record):
    """(market date, "HH:MM" since local midnight) of a Terna record."""
    local = datetime.strptime(record["date"], "%Y-%m-%d %H:%M:%S")
    offset = record["date_offset"]
    sign = 1 if offset.startswith("+") else -1
    hours, minutes = (int(part) for part in offset[1:].split(":"))
    utc = (local - sign * timedelta(hours=hours, minutes=minutes)).replace(tzinfo=timezone.utc)
    return point_label(utc)


def parse_records(records, spec):
    """Point records for entsoe_api.merge_resolutions."""
    points = []
    for record in records:
        value = record.get(spec["value"])
        if value in (None, "", "null"):
            continue
        market_date, label = record_label(record)
        points.append({
            "group": "TOTAL",
            "date": market_date,
            "time": label,
            "minutes": 15,
            "value": float(value) * spec["scale"],
        })
    return points


def download(client, name, start_date, end_date):
    spec = SERIES[name]
    points = []
    for chunk_start, chunk_end in day_chunks(start_date, end_date, CHUNK_DAYS):
        print(f"  {name}: {chunk_start} -> {chunk_end}")
        payload = client.get(spec["path"], {
            **spec["params"],
            "dateFrom": chunk_start.strftime("%d/%m/%Y"),
            "dateTo": chunk_end.strftime("%d/%m/%Y"),
        })
        points.extend(parse_records(payload.get(spec["records"]) or [], spec))
    return points


# ============================================================================
# LOAD / SAVE
# ============================================================================


def empty():
    return {resolution: {} for resolution in RESOLUTIONS}


def load_existing():
    existing = {name: empty() for name in SERIES}
    if not os.path.exists(OUTPUT_PATH):
        return existing
    payload = compact.load(OUTPUT_PATH)
    for name in SERIES:
        for resolution in RESOLUTIONS:
            existing[name][resolution] = dict(payload.get(name, {}).get(resolution, {}))
    return existing


def build_output(series):
    return {
        "source": "Terna public API (developer.terna.it)",
        "description": (
            "Geothermal actual generation and total load of Italy (including "
            "self-consumption), MW; hourly = average of the quarter-hours, "
            "daily = sum of the hours (MWh/day). Italian market time "
            "(Europe/Rome), labelled by elapsed time since local midnight."
        ),
        "labels": {name: spec["label"] for name, spec in SERIES.items()},
        **{
            name: {
                resolution: {
                    group: compact.encode_series(rows, resolution)
                    for group, rows in sorted(data[resolution].items())
                }
                for resolution in RESOLUTIONS
            }
            for name, data in series.items()
        },
    }


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="Fetch Terna geothermal generation and total load.")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--full-history", action="store_true",
                        help="Rebuild from 2025-01-01 instead of merging onto existing data")
    args = parser.parse_args()

    end_date = parse_date(args.end) if args.end else market_today()
    if args.start:
        start_date = parse_date(args.start)
    elif args.full_history:
        start_date = DEFAULT_HISTORY_START
    else:
        start_date = end_date - timedelta(days=LOOKBACK_DAYS)
    start_date = max(start_date, DEFAULT_HISTORY_START)
    if end_date < start_date:
        raise ValueError("End date must not be before start date.")

    key, secret = os.environ.get("TERNA_KEY"), os.environ.get("TERNA_SECRET")
    if not key or not secret:
        raise RuntimeError("TERNA_KEY and TERNA_SECRET environment variables must be set.")

    print()
    print("=" * 70)
    print("TERNA GEOTHERMAL GENERATION AND TOTAL LOAD")
    print("=" * 70)
    print(f"Date range: {start_date} -> {end_date}")
    print()

    client = Client(key, secret)
    existing = {name: empty() for name in SERIES} if args.full_history else load_existing()
    series = {}
    for name in SERIES:
        points = download(client, name, start_date, end_date)
        print(f"  {len(points):,} quarter-hours")
        series[name] = merge_resolutions(existing[name], points, RESOLUTIONS) if points else existing[name]
        quarters = series[name]["quarter_hourly"].get("TOTAL", [])
        if quarters:
            print(f"  {name}: {len(series[name]['daily'].get('TOTAL', [])):,} days, "
                  f"last {quarters[-1]['date']} {quarters[-1]['time']}")
        print()

    if not any(series[name]["daily"].get("TOTAL") for name in SERIES):
        raise RuntimeError("No Terna data downloaded; not writing output.")

    compact.dump(build_output(series), OUTPUT_PATH)
    print("=" * 70)
    print(f"Wrote: {OUTPUT_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    main()
