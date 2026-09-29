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
from collections import Counter, defaultdict
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
IT_DOMAIN = "10YIT-GRTN-----B"

DEFAULT_HISTORY_START = datetime(2025, 1, 1).date()
LOOKBACK_DAYS = 45      # outages are revised after the fact
LOOKAHEAD_DAYS = 14     # planned outages ahead
WINDOW_DAYS = 31
PAGE_SIZE = 200         # the API returns at most 200 documents per request
DROPPED_STATUS = {"A09", "A13"}  # cancelled, withdrawn
MAX_UNIT_MW = 5000      # no Italian unit is anywhere near this
STEP = timedelta(minutes=15)
RESOLUTIONS = ("quarter_hourly", "hourly", "daily")


def text(element, tag):
    found = element.find(f".//{{*}}{tag}")
    return found.text if found is not None else None


def parse_time(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


RESOLUTION_MINUTES = {"PT1M": 1, "PT15M": 15, "PT30M": 30, "PT60M": 60, "P1D": 1440}


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
        # When the document (this revision) was created, e.g. "2026-09-22T10:01:41Z".
        "created": text(doc, "createdDateTime"),
        "status": text(doc, "docStatus/{*}value"),
        "zone": zone,
        "psr": text(ts, "production_RegisteredResource.pSRType.psrType"),
        "business": text(ts, "businessType"),
        # The generation unit, to spot overlapping outages of the same unit.
        "unit": (text(ts, "production_RegisteredResource.pSRType.powerSystemResources.mRID")
                 or text(ts, "production_RegisteredResource.pSRType.powerSystemResources.name")
                 or text(doc, "mRID")),
        "unit_name": text(ts, "production_RegisteredResource.pSRType.powerSystemResources.name"),
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
    active = [o for o in outages.values() if o["status"] not in DROPPED_STATUS]
    repair(active, unit_types(token))
    return [o for o in active if plausible(o)]


def unit_types(token):
    """
    {generation unit mRID or name: psrType} from ENTSO-E 14.1.B (units of
    100 MW or more), this year and last, to correct outage documents.
    """
    types = {}
    year = market_today().year
    for y in (year - 1, year):
        try:
            root = request_entsoe(token, {"documentType": "A71", "processType": "A33", "in_Domain": IT_DOMAIN,
                                          "periodStart": f"{y}01010000", "periodEnd": f"{y}01020000"})
        except requests.HTTPError as error:
            print(f"  unit list {y}: {error}")
            continue
        for ts in [] if root is None else root.findall(".//{*}TimeSeries"):
            psr = text(ts, "psrType")
            for resource in ts.findall(".//{*}PowerSystemResources"):
                for key in (text(resource, "mRID"), text(resource, "name")):
                    if key and psr:
                        types[key] = psr
        time.sleep(REQUEST_PAUSE_SECONDS)
    return types


def repair(outages, types):
    """
    Some documents (for outages from September 2026) give the nominal power
    in kW while the available capacity stays in MW: SIMERI CRICHI, nominal
    885000.0 with 368 available, is 517 MW out - Terna reports 505 MW for
    the same days.  Most of them also label coal and gas units B09,
    geothermal.  Rescale the nominal power (and an available capacity only
    if it cannot be MW, i.e. exceeds the nominal), and take the production
    type of every B09 document from the ENTSO-E unit list or from the unit's
    other documents (the one real geothermal unit of 100 MW or more, in
    Tuscany, is listed as B09 there and stays so).
    """
    scaled = 0
    scaled_starts = []
    for outage in outages:
        if outage["nominal"] > MAX_UNIT_MW and outage["nominal"] / 1000 <= MAX_UNIT_MW:
            outage["nominal"] /= 1000
            outage["periods"] = [
                (s, e, a / 1000 if a > outage["nominal"] and a / 1000 <= outage["nominal"] else a)
                for s, e, a in outage["periods"]
            ]
            outage["scaled"] = True
            scaled += 1
            scaled_starts += [s for s, _, _ in outage["periods"]]
    if scaled_starts:
        print(f"  documents in kW cover {min(scaled_starts):%Y-%m-%d} -> {max(scaled_starts):%Y-%m-%d}")

    learned = defaultdict(Counter)
    for outage in outages:
        if not outage.get("scaled") and outage["psr"] != "B09":
            learned[outage["unit"]][outage["psr"]] += 1

    retyped, unresolved = Counter(), Counter()
    for outage in outages:
        if not outage.get("scaled") and outage["psr"] != "B09":
            continue
        known = (types.get(outage["unit"]) or types.get(outage["unit_name"])
                 or (learned[outage["unit"]].most_common(1)[0][0] if learned[outage["unit"]] else None))
        if known and known != outage["psr"]:
            retyped[f"{outage['psr']}->{known}"] += 1
            outage["psr"] = known
        elif not known and outage["psr"] == "B09":
            unresolved[outage["unit_name"]] += 1
            outage["psr"] = "B20"  # "other" rather than a wrong geothermal
    print(f"  repaired: {scaled} documents rescaled from kW, types {dict(retyped)}, "
          f"B09 without a known type (shown as other): {dict(unresolved)}")


def plausible(outage):
    """
    Some documents carry impossible values (a nominal power of hundreds of
    GW, or an available capacity outside 0..nominal).  Drop the first and
    clamp the second, logging both so they can be checked at the source.
    """
    if not 0 < outage["nominal"] <= MAX_UNIT_MW:
        print(f"  DROPPED {outage['unit']} {outage['zone']} {outage['psr']} {outage['business']}: "
              f"nominal {outage['nominal']:,.0f} MW (mRID {outage['mrid']})")
        return False
    bad = [a for _, _, a in outage["periods"] if not 0 <= a <= outage["nominal"]]
    if bad:
        print(f"  CLAMPED {outage['unit']} {outage['zone']} {outage['psr']} {outage['business']}: "
              f"nominal {outage['nominal']:,.0f} MW, available {min(bad):,.0f}..{max(bad):,.0f} MW")
        outage["periods"] = [(s, e, min(max(a, 0.0), outage["nominal"])) for s, e, a in outage["periods"]]
    return True


def quarter_totals(outages, window_start, window_end):
    """
    {group: {utc slot start: MW unavailable}} within [window_start, window_end).

    A unit counts once per slot.  Where its documents overlap, the most
    recently created one holds: operators post new documents rather than
    withdrawing old ones, so a planned outage announced months ahead can
    stay active after later documents (and Terna's own availability) show
    the unit running - e.g. TAVAZZANO 5, planned 0 MW from 17 Sept 2026 in
    December 2025, with 711 MW available in documents of 22 September.
    Ties go to the higher revision, then to the larger unavailability.
    """
    by_unit = defaultdict(list)
    for outage in outages:
        by_unit[outage["unit"]].append(outage)

    totals = defaultdict(lambda: defaultdict(float))
    for unit_outages in by_unit.values():
        latest = {}  # slot -> (created, revision, MW, group)
        for outage in unit_outages:
            group = f"{outage['zone']}|{outage['psr']}|{outage['business']}"
            created = outage.get("created") or ""
            for start, end, available in outage["periods"]:
                # A newer document with the unit fully available still counts:
                # it overrides older ones.
                unavailable = max(outage["nominal"] - available, 0.0)
                rank = (created, outage["revision"], unavailable)
                slot = max(start, window_start)
                # First slot boundary at or after the start.
                slot += (-(slot - window_start)) % STEP
                last = min(end, window_end)
                while slot < last:
                    held = latest.get(slot)
                    if held is None or rank > held[:3]:
                        latest[slot] = (*rank, group)
                    slot += STEP
        for slot, (_, _, value, group) in latest.items():
            if value > 0:
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
