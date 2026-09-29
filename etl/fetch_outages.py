"""
Unavailable generation capacity in Italy from ENTSO-E 15.1.A/B
(documentType A80, unavailability of generation units), written to
app/data/outages.json.

Every outage document names one unit (100 MW or more: smaller units, and so
nearly all solar and wind, do not have to report), its bidding zone,
production type (psrType), nominal power, whether it is planned (A53) or
forced (A54), and its available capacity over time.  Unavailable MW =
nominal - available.  Only the latest revision of each document counts;
cancelled (A09) and withdrawn (A13) ones are dropped.

Series are kept per "ZONE|psrType|A53/A54" so zone charts can use them
later.  Values are MW every quarter-hour (slots without an outage are 0
and not stored); hourly = average of the quarter-hours; daily = average
MW over the day (capacity, not energy).  Italian market time, labelled by
elapsed time since local midnight like the other series.

The API token is read from ENTSOE_API_KEY.
"""

import argparse
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

import compact
from entsoe_api import (
    REQUEST_PAUSE_SECONDS,
    local_midnight_utc,
    market_today,
    parse_date,
    point_label,
    request_entsoe,
)

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "data", "outages.json")

ZONES = {
    "NORD": "10Y1001A1001A73I", "CNOR": "10Y1001A1001A70O", "CSUD": "10Y1001A1001A71M",
    "SUD": "10Y1001A1001A788", "CALA": "10Y1001C--00096J", "SICI": "10Y1001A1001A75E",
    "SARD": "10Y1001A1001A74G",
}
EIC_ZONE = {eic: zone for zone, eic in ZONES.items()}

DEFAULT_HISTORY_START = datetime(2025, 1, 1).date()
LOOKBACK_DAYS = 45      # outages are revised after the fact
LOOKAHEAD_DAYS = 14     # planned outages ahead
WINDOW_DAYS = 31
PAGE_SIZE = 200         # the API returns at most 200 documents per request
DROPPED_STATUS = {"A09", "A13"}  # cancelled, withdrawn
STEP = timedelta(minutes=15)
RESOLUTIONS = ("quarter_hourly", "hourly", "daily")


def text(element, tag):
    found = element.find(f".//{{*}}{tag}")
    return found.text if found is not None else None


def parse_time(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


RESOLUTION_MINUTES = {"PT15M": 15, "PT30M": 30, "PT60M": 60, "P1D": 1440}


def parse_document(doc):
    """One outage as a dict, or None for documents that do not describe one."""
    ts = doc.find("{*}TimeSeries")
    if ts is None:
        return None
    zone = EIC_ZONE.get(text(ts, "biddingZone_Domain.mRID"))
    nominal = text(ts, "production_RegisteredResource.pSRType.powerSystemResources.nominalP")
    if zone is None or nominal is None:
        return None
    periods = []
    for period in ts.findall("{*}Available_Period"):
        start = parse_time(text(period, "start"))
        end = parse_time(text(period, "end"))
        step = RESOLUTION_MINUTES.get(text(period, "resolution"), 60)
        points = sorted((int(text(p, "position")), float(text(p, "quantity"))) for p in period.findall("{*}Point"))
        # curveType A03: a value holds until the next listed position.
        for index, (position, available) in enumerate(points):
            p_start = start + timedelta(minutes=step * (position - 1))
            p_end = start + timedelta(minutes=step * (points[index + 1][0] - 1)) if index + 1 < len(points) else end
            periods.append((p_start, min(p_end, end), available))
    return {
        "mrid": text(doc, "mRID"),
        "revision": int(text(doc, "revisionNumber") or 0),
        "status": text(doc, "docStatus/{*}value"),
        "zone": zone,
        "psr": text(ts, "production_RegisteredResource.pSRType.psrType"),
        "business": text(ts, "businessType"),
        # The generation unit, to spot overlapping outages of the same unit.
        "unit": (text(ts, "production_RegisteredResource.pSRType.powerSystemResources.mRID")
                 or text(ts, "production_RegisteredResource.pSRType.powerSystemResources.name")
                 or text(doc, "mRID")),
        "nominal": float(nominal),
        "periods": periods,
    }


def download(token, start_date, end_date):
    """Latest revision of every outage overlapping the range, per mRID."""
    outages = {}
    for zone, eic in ZONES.items():
        window_start = start_date
        while window_start <= end_date:
            window_end = min(window_start + timedelta(days=WINDOW_DAYS - 1), end_date)
            offset = 0
            while True:
                try:
                    root = request_entsoe(token, {
                        "documentType": "A80",
                        "BiddingZone_Domain": eic,
                        "periodStart": local_midnight_utc(window_start).strftime("%Y%m%d%H%M"),
                        "periodEnd": local_midnight_utc(window_end + timedelta(days=1)).strftime("%Y%m%d%H%M"),
                        "offset": offset,
                    })
                except requests.HTTPError as error:
                    # Fail the run (a skipped window would read as "no outages"),
                    # but show ENTSO-E's reason first.
                    print(f"  {zone} {window_start} offset {offset}: {error}\n  {error.response.text[:500]}")
                    raise
                docs = [] if root is None else [
                    d for d in ([root] if root.tag.endswith("Unavailability_MarketDocument") else list(root))
                    if d.tag.endswith("Unavailability_MarketDocument")
                ]
                for doc in docs:
                    outage = parse_document(doc)
                    if outage is None:
                        continue
                    known = outages.get(outage["mrid"])
                    if known is None or outage["revision"] > known["revision"]:
                        outages[outage["mrid"]] = outage
                time.sleep(REQUEST_PAUSE_SECONDS)
                if len(docs) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE
            print(f"  {zone} {window_start} -> {window_end}: {len(outages):,} outages so far")
            window_start = window_end + timedelta(days=1)
    return [o for o in outages.values() if o["status"] not in DROPPED_STATUS]


def quarter_totals(outages, window_start, window_end):
    """
    {group: {utc slot start: MW unavailable}} within [window_start, window_end).

    A unit with overlapping outages (e.g. a planned and a forced one) counts
    once per slot, with its largest unavailability - its lowest available
    capacity - attributed to that outage's group.
    """
    by_unit = defaultdict(list)
    for outage in outages:
        by_unit[outage["unit"]].append(outage)

    totals = defaultdict(lambda: defaultdict(float))
    for unit_outages in by_unit.values():
        worst = {}  # slot -> (MW, group)
        for outage in unit_outages:
            group = f"{outage['zone']}|{outage['psr']}|{outage['business']}"
            for start, end, available in outage["periods"]:
                unavailable = outage["nominal"] - available
                if unavailable <= 0:
                    continue
                slot = max(start, window_start)
                # First slot boundary at or after the start.
                slot += (-(slot - window_start)) % STEP
                last = min(end, window_end)
                while slot < last:
                    if unavailable > worst.get(slot, (0.0, None))[0]:
                        worst[slot] = (unavailable, group)
                    slot += STEP
        for slot, (value, group) in worst.items():
            totals[group][slot] += value
    return totals


def to_rows(slots):
    """UTC slot -> MW into quarter/hourly/daily rows on the market-time basis."""
    quarter = {}
    for slot, value in slots.items():
        quarter[point_label(slot)] = round(value, 1)
    hourly, daily = defaultdict(float), defaultdict(float)
    for (day, label), value in quarter.items():
        hours, minutes = (int(part) for part in label.split(":"))
        hourly[(day, f"{hours:02d}:00")] += value / 4
        daily[day] += value
    day_slots = {}
    for day in daily:
        start = local_midnight_utc(datetime.fromisoformat(day).date())
        end = local_midnight_utc(datetime.fromisoformat(day).date() + timedelta(days=1))
        day_slots[day] = (end - start) / STEP
    return {
        "quarter_hourly": [{"date": d, "time": t, "value": v} for (d, t), v in sorted(quarter.items(), key=lambda x: (x[0][0], minutes_of(x[0][1])))],
        "hourly": [{"date": d, "time": t, "value": round(v, 1)} for (d, t), v in sorted(hourly.items(), key=lambda x: (x[0][0], minutes_of(x[0][1])))],
        "daily": [{"date": d, "value": round(v / day_slots[d], 1)} for d, v in sorted(daily.items())],
    }


def minutes_of(label):
    hours, minutes = (int(part) for part in label.split(":"))
    return hours * 60 + minutes


def load_existing():
    if not os.path.exists(OUTPUT_PATH):
        return {}
    payload = compact.load(OUTPUT_PATH)
    return payload.get("groups", {})


def merge(existing, new, first_day, last_day):
    """Rows of existing groups outside [first_day, last_day] plus all new rows."""
    merged = {}
    for group in set(existing) | set(new):
        merged[group] = {}
        for resolution in RESOLUTIONS:
            kept = [r for r in existing.get(group, {}).get(resolution, [])
                    if not first_day <= r["date"] <= last_day]
            merged[group][resolution] = sorted(
                kept + new.get(group, {}).get(resolution, []),
                key=lambda r: (r["date"], minutes_of(r["time"]) if "time" in r else 0))
    return {g: v for g, v in merged.items() if any(v[r] for r in RESOLUTIONS)}


def main():
    parser = argparse.ArgumentParser(description="Fetch ENTSO-E unavailability of generation units for Italy.")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--full-history", action="store_true")
    args = parser.parse_args()

    today = market_today()
    end_date = parse_date(args.end) if args.end else today + timedelta(days=LOOKAHEAD_DAYS)
    if args.start:
        start_date = parse_date(args.start)
    elif args.full_history:
        start_date = DEFAULT_HISTORY_START
    else:
        start_date = today - timedelta(days=LOOKBACK_DAYS)
    start_date = max(start_date, DEFAULT_HISTORY_START)

    token = os.environ.get("ENTSOE_API_KEY")
    if not token:
        raise RuntimeError("ENTSOE_API_KEY environment variable must be set.")

    print(f"Unavailability of generation units (A80), {start_date} -> {end_date}")
    outages = download(token, start_date, end_date)
    print(f"{len(outages):,} active outages")

    totals = quarter_totals(outages, local_midnight_utc(start_date), local_midnight_utc(end_date + timedelta(days=1)))
    new = {group: to_rows(slots) for group, slots in totals.items()}
    existing = {} if args.full_history else load_existing()
    groups = merge(existing, new, start_date.isoformat(), end_date.isoformat())

    compact.dump({
        "source": "ENTSO-E Transparency Platform, 15.1.A/B (A80)",
        "description": (
            "Unavailable capacity (nominal - available, MW) of Italian generation "
            "units of 100 MW or more, per 'ZONE|psrType|businessType' (A53 "
            "planned, A54 forced). Quarter-hourly MW, hourly = average, daily = "
            "average MW over the day. Missing slots are 0. Italian market time."
        ),
        "groups": {
            group: {resolution: compact.encode_series(rows[resolution], resolution) for resolution in RESOLUTIONS}
            for group, rows in sorted(groups.items())
        },
    }, OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH} ({len(groups)} groups)")


if __name__ == "__main__":
    main()
