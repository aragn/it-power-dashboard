"""Shared helpers for the ENTSO-E Transparency Platform scripts.

Used by fetch_entsoe.py (generation & load) and fetch_crossborder.py
(cross-border flows and scheduled exchanges).

Time basis
----------
ENTSO-E publishes timestamps in UTC.  The dashboard's time axis is the GME
market day in Italian time (Europe/Rome), labelled by elapsed time since
local midnight, the way GME numbers its periods: a 23-hour spring DST day
runs 00:00 .. 22:45 and a 25-hour autumn day runs 00:00 .. 24:45.
point_label() converts every ENTSO-E point to that (date, "HH:MM") basis so
all series line up with the PUN and zonal prices.

Compressed series
-----------------
Series with curveType A03 ("variable sized block") omit a point whenever
its value equals the previous one.  parse_points() re-expands them, so a
missing point only ever means missing data.

The API token is read by the calling script from the ENTSOE_API_KEY
environment variable; it is never stored in the repository.
"""

import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

API_URL = "https://web-api.tp.entsoe.eu/api"

MARKET_TZ = ZoneInfo("Europe/Rome")

REQUEST_PAUSE_SECONDS = 2
MAX_RETRIES = 6
INITIAL_RETRY_SECONDS = 20


# ============================================================================
# DATES
# ============================================================================


def parse_date(value):
    value = str(value)
    if "-" in value:
        return datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.strptime(value, "%Y%m%d").date()


def day_chunks(start_date, end_date, chunk_days):
    current = start_date
    while current <= end_date:
        chunk_end = min(current + timedelta(days=chunk_days - 1), end_date)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def local_midnight_utc(day):
    """UTC instant of 00:00 Italian time on the given date."""
    return datetime(day.year, day.month, day.day, tzinfo=MARKET_TZ).astimezone(timezone.utc)


def to_api_datetime(day, end_of_day=False):
    """
    API period boundary (UTC, YYYYMMDDHHMM) for an Italian market day:
    local midnight at the start of `day`, or at its end if end_of_day.
    """
    if end_of_day:
        day = day + timedelta(days=1)
    return local_midnight_utc(day).strftime("%Y%m%d%H%M")


def point_label(instant_utc):
    """(market date, "HH:MM" elapsed since local midnight) for a UTC instant."""
    market_date = instant_utc.astimezone(MARKET_TZ).date()
    elapsed = instant_utc - local_midnight_utc(market_date)
    minutes = int(elapsed.total_seconds() // 60)
    return market_date.isoformat(), f"{minutes // 60:02d}:{minutes % 60:02d}"


def label_minutes(label):
    hours, minutes = (int(part) for part in label.split(":"))
    return hours * 60 + minutes


def minutes_label(total):
    return f"{total // 60:02d}:{total % 60:02d}"


# ============================================================================
# API REQUEST
# ============================================================================


def request_entsoe(token, params):
    """
    Call the Restful API and return the parsed XML root, or None when the
    platform reports "No matching data".  Retries HTTP 429 with backoff.
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


def _child(elem, tag):
    # "{*}" matches any namespace, so one parser reads every document type.
    return elem.find(f"{{*}}{tag}")


def _text(elem, tag):
    child = _child(elem, tag)
    return child.text if child is not None else None


RESOLUTION_MINUTES = {"PT15M": 15, "PT30M": 30, "PT60M": 60}


def parse_points(root, group_of=lambda ts: "TOTAL", keep=lambda ts: True):
    """
    Flatten every TimeSeries in a response into point records:
        {"group", "date", "time", "minutes", "value"}
    with (date, time) on the Italian market-time basis (see point_label).

    group_of(ts) names the series a TimeSeries belongs to (e.g. its
    production type); keep(ts) filters TimeSeries out.
    """
    if root is None:
        return []

    records = []

    for ts in root.findall(".//{*}TimeSeries"):
        if not keep(ts):
            continue

        group = group_of(ts)
        compressed = _text(ts, "curveType") == "A03"

        for period in ts.findall("{*}Period"):
            interval = _child(period, "timeInterval")
            start = datetime.strptime(_text(interval, "start"), "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            end = datetime.strptime(_text(interval, "end"), "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            step = RESOLUTION_MINUTES.get(_text(period, "resolution"), 60)
            positions = int((end - start).total_seconds() // 60 // step)

            points = sorted(
                (int(_text(point, "position")), float(_text(point, "quantity")))
                for point in period.findall("{*}Point")
            )

            for i, (position, value) in enumerate(points):
                # A03: the value holds until the next listed position.
                if compressed:
                    last = points[i + 1][0] - 1 if i + 1 < len(points) else positions
                else:
                    last = position

                for pos in range(position, last + 1):
                    instant = start + timedelta(minutes=step * (pos - 1))
                    market_date, label = point_label(instant)
                    records.append({
                        "group": group,
                        "date": market_date,
                        "time": label,
                        "minutes": step,
                        "value": value,
                    })

    return records


# ============================================================================
# SERIES BUILDERS
# ============================================================================


def to_quarter_hourly(records):
    """
    {group: [{"date","time","value"}]} at 15-minute resolution.  Coarser
    points (hourly, 30-minute) are repeated across their quarter-hours.
    """
    output = defaultdict(dict)

    for row in records:
        start = label_minutes(row["time"])
        for offset in range(0, row["minutes"], 15):
            output[row["group"]][(row["date"], minutes_label(start + offset))] = row["value"]

    return {
        group: sorted(
            ({"date": d, "time": t, "value": round(v, 2)} for (d, t), v in points.items()),
            key=lambda x: (x["date"], label_minutes(x["time"])),
        )
        for group, points in output.items()
    }


def quarter_hourly_to_hourly(quarter_hourly_by_group):
    """Hourly value = average of the (up to 4) quarter-hour values."""
    result = {}

    for group, rows in quarter_hourly_by_group.items():
        grouped = defaultdict(list)
        for row in rows:
            hour = minutes_label(label_minutes(row["time"]) // 60 * 60)
            grouped[(row["date"], hour)].append(row["value"])

        result[group] = sorted(
            ({"date": d, "time": h, "value": round(sum(v) / len(v), 2)} for (d, h), v in grouped.items()),
            key=lambda x: (x["date"], label_minutes(x["time"])),
        )

    return result


def hourly_to_daily(hourly_by_group):
    """Daily value = sum of the hourly values (MWh per day)."""
    result = {}

    for group, rows in hourly_by_group.items():
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["date"]].append(row["value"])

        result[group] = sorted(
            ({"date": d, "value": round(sum(v), 2)} for d, v in grouped.items()),
            key=lambda x: x["date"],
        )

    return result


def build_resolutions(records):
    """All three resolutions from raw point records."""
    quarter_hourly = to_quarter_hourly(records)
    hourly = quarter_hourly_to_hourly(quarter_hourly)
    return {
        "quarter_hourly": quarter_hourly,
        "hourly": hourly,
        "daily": hourly_to_daily(hourly),
    }


def merge_group_series(existing, new):
    """Merge two {group: [rows]} dicts by (date, time); new rows win."""
    existing = existing or {}
    new = new or {}
    merged = {}

    for group in set(existing) | set(new):
        by_key = {}
        for row in existing.get(group, []):
            by_key[(row["date"], row.get("time"))] = row
        for row in new.get(group, []):
            by_key[(row["date"], row.get("time"))] = row

        merged[group] = sorted(
            by_key.values(),
            key=lambda x: (x["date"], label_minutes(x["time"]) if x.get("time") else 0),
        )

    return merged
