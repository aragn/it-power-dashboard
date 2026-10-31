"""
Fetch the MGP flows between the market zones (ME_Transits) and their
transmission limits (ME_TransmissionLimits) from GME, with the MGP prices
of the virtual zones of the links to Corsica (ME_ZonalPrices), and write
app/data/transits.json: the interconnections of each bidding zone with the
zones and countries next to it, for the zone pages of the dashboard.

ME_Transits gives, per pair of zones and period, the flow the MGP scheduled
from "From" to "To" (negative: the other way); ME_TransmissionLimits the
limits of both directions, the pair sometimes named the other way round.
A pair is stored as GME's transits name it ("CNOR|NORD": from Centre-North
to North): its flow (positive from the first zone to the second), the limit
that way (limit_forward) and the other way (limit_back), all MW.  GME puts
10,000 MW (or more) where it sets no limit of its own (the borders coupled
with Europe, where the coupling sets the flows): left out.

The virtual zones (GME's names): COUP Italia Coupling, the hub of the
northern borders coupled with Europe (XAUS, XFRA, BSP their neighbours'
sides); AUST, FRAN, SLOV, SVIZ the northern borders' own zones (Switzerland
is not coupled); GREC and XGRE Greece, MONT Montenegro, MALT Malta; CORS
and COAC Corsica, at the ends of the SACOI direct-current link (Sardinia -
Corsica - Centre-North) and of the SARCO alternating-current one (Sardinia
- Corsica).  Prices kept: CORS and COAC (the others repeat an Italian
zone's price, or are in coupling.json).

Periods are 15 minutes from 1 October 2025 and hours before.  Flows and
limits: hourly = average of the quarter-hours (MW), daily = sum of the
hours (MWh).  Prices: hourly and daily averages (EUR/MWh).  Quarter-hourly
series are stored from 1 October 2025.

Credentials come from GME_API_LOGIN / GME_API_PASSWORD.
"""

import argparse
import os
import time
from datetime import timedelta

import compact
from entsoe_api import merge_resolutions
from fetch_coupling import LOOKBACK_DAYS, RESOLUTIONS, default_end, slot
import catch_up
from fetch_zonal import DEFAULT_HISTORY_START, PT15_START, REQUEST_PAUSE_SECONDS, parse_date, request_data

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "data", "transits.json")

# GME's market zones: the Italian bidding zones and the virtual ones.
ZONE_LABELS = {
    "NORD": "North", "CNOR": "Centre-North", "CSUD": "Centre-South", "SUD": "South", "CALA": "Calabria",
    "SICI": "Sicily", "SARD": "Sardinia",
    "COUP": "Italia Coupling", "XAUS": "Austria (coupled)", "XFRA": "France (coupled)", "BSP": "Slovenia (coupled)",
    "AUST": "Austria", "FRAN": "France", "SLOV": "Slovenia", "SVIZ": "Switzerland", "GREC": "Greece",
    "XGRE": "Greece (coupled)", "MONT": "Montenegro", "MALT": "Malta", "CORS": "Corsica (SACOI, DC)",
    "COAC": "Corsica (SARCO, AC)",
}

PRICE_ZONES = ["CORS", "COAC"]

# A Corsica zone's price stands only while its links have capacity: with
# none (the link out of service, e.g. SACOI from September 2026) GME lists a
# placeholder price (1,700 or 1,750 EUR/MWh), left out (mask_prices).
CORSICA_LINKS = {"CORS": ("SARD|CORS", "CNOR|CORS"), "COAC": ("SARD|COAC",)}

# At or above this GME sets no limit of its own.
NO_LIMIT_MW = 10000

FIELDS = ("flow", "limit_forward", "limit_back")


def transit_records(rows):
    """Flow point records grouped as "<from>|<to>|flow"; the pairs seen."""
    records, pairs = [], set()
    for row in rows:
        start, end = str(row.get("From") or "").strip().upper(), str(row.get("To") or "").strip().upper()
        if not start or not end or row.get("Transit") is None:
            continue
        day, label, minutes = slot(row)
        pairs.add(f"{start}|{end}")
        records.append({"group": f"{start}|{end}|flow", "date": day, "time": label, "minutes": minutes,
                        "value": round(float(row["Transit"]), 1)})
    return records, pairs


def limit_records(rows, pairs):
    """Limit point records grouped as "<pair>|limit_forward" (from the pair's
    first zone to its second) and "<pair>|limit_back", the pair as the
    transits name it (a limits row may name it the other way round)."""
    records = []
    for row in rows:
        start, end = str(row.get("From") or "").strip().upper(), str(row.get("To") or "").strip().upper()
        forward, back = row.get("MaxTransmissionLimitFrom"), row.get("MaxTransmissionLimitTo")
        if f"{start}|{end}" not in pairs and f"{end}|{start}" in pairs:
            start, end, forward, back = end, start, back, forward
        pair = f"{start}|{end}"
        day, label, minutes = slot(row)
        for name, value in (("limit_forward", forward), ("limit_back", back)):
            if value is None or float(value) >= NO_LIMIT_MW:
                continue
            records.append({"group": f"{pair}|{name}", "date": day, "time": label, "minutes": minutes,
                            "value": round(float(value), 1)})
    return records


def price_records(rows):
    records = []
    for row in rows:
        zone = str(row.get("Zone") or "").strip().upper()
        if zone not in PRICE_ZONES or row.get("Price") is None:
            continue
        day, label, minutes = slot(row)
        records.append({"group": zone, "date": day, "time": label, "minutes": minutes,
                        "value": round(float(row["Price"]), 2)})
    return records


def mask_prices(price_points, limit_points):
    """The Corsica zones' price points of the periods in which a link of the
    zone had a limit above zero, either way."""
    open_slots = {zone: set() for zone in CORSICA_LINKS}
    for record in limit_points:
        if record["value"] <= 0:
            continue
        pair = record["group"].rsplit("|", 1)[0]
        for zone, pairs in CORSICA_LINKS.items():
            if pair in pairs:
                open_slots[zone].add((record["date"], record["time"]))
    return [record for record in price_points
            if record["group"] not in CORSICA_LINKS or (record["date"], record["time"]) in open_slots[record["group"]]]


# ============================================================================
# LOAD / SAVE
# ============================================================================


def empty():
    return {resolution: {} for resolution in RESOLUTIONS}


def load_existing(path=OUTPUT_PATH):
    """Flows as {resolution: {"<from>|<to>|<field>": rows}}, prices as
    {resolution: {zone: rows}}."""
    flows, prices = empty(), empty()
    if not os.path.exists(path):
        return flows, prices
    payload = compact.load(path)
    for resolution in RESOLUTIONS:
        for pair, fields in payload.get("flows", {}).get(resolution, {}).items():
            for name, rows in fields.items():
                flows[resolution][f"{pair}|{name}"] = rows
        prices[resolution] = dict(payload.get("prices", {}).get(resolution, {}))
    return flows, prices


def encode(rows, resolution):
    return compact.encode_series(rows, resolution,
                                 skip_before=PT15_START.isoformat() if resolution == "quarter_hourly" else None)


def build_output(flows, prices):
    nested = {}
    for resolution in RESOLUTIONS:
        nested[resolution] = {}
        for group, rows in sorted(flows[resolution].items()):
            start, end, name = group.split("|")
            nested[resolution].setdefault(f"{start}|{end}", {})[name] = encode(rows, resolution)
    pairs = sorted({pair for resolution in nested.values() for pair in resolution})
    return {
        "source": "GME - MGP transits (ME_Transits), transmission limits (ME_TransmissionLimits) and zonal prices",
        "description": (
            "MGP flows between the market zones, MW, positive from the pair's first zone to its second; the "
            "limits of both directions (limit_forward from the first zone to the second, limit_back the other "
            "way; left out where GME sets none, its 10,000 MW). Daily values are MWh/day. MGP prices of the "
            "virtual zones of the links to Corsica (CORS: SACOI, DC; COAC: SARCO, AC), EUR/MWh, daily values "
            "averages, only for the periods in which the zone's links have capacity. Italian market time, labelled by elapsed time since local midnight like GME periods. "
            "Quarter-hourly series start at quarter_hourly_native_from; earlier quarter-hours repeat the "
            "hourly value."
        ),
        "quarter_hourly_native_from": PT15_START.isoformat(),
        "zones": ZONE_LABELS,
        "pairs": pairs,
        "flows": nested,
        "prices": {
            resolution: {zone: encode(rows, resolution) for zone, rows in sorted(prices[resolution].items())}
            for resolution in RESOLUTIONS
        },
    }


# ============================================================================
# MAIN
# ============================================================================


def fetch(login, password, start_date, end_date, existing):
    flows, prices = existing
    print("Downloading MGP transits...")
    flow_points, pairs = transit_records(request_data(login, password, start_date, end_date, data_name="ME_Transits"))
    print(f"  {len(flow_points):,} points, {len(pairs)} pairs")
    time.sleep(REQUEST_PAUSE_SECONDS)

    print("Downloading MGP transmission limits...")
    known = pairs | {key.rsplit("|", 1)[0] for key in flows["daily"]}
    limit_points = limit_records(request_data(login, password, start_date, end_date, data_name="ME_TransmissionLimits"), known)
    print(f"  {len(limit_points):,} points")
    time.sleep(REQUEST_PAUSE_SECONDS)

    print("Downloading the Corsica zones' MGP prices...")
    price_points = mask_prices(price_records(request_data(login, password, start_date, end_date)), limit_points)
    print(f"  {len(price_points):,} points (while the links have capacity)")

    flows = merge_resolutions(flows, flow_points + limit_points, RESOLUTIONS)
    prices = merge_resolutions(prices, price_points, RESOLUTIONS, daily="mean")
    if not flow_points and not any(flows["daily"].values()):
        raise RuntimeError("No MGP transits downloaded; not writing output.")
    return flows, prices


def main():
    parser = argparse.ArgumentParser(description="Fetch GME MGP transits and transmission limits between zones.")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--full-history", action="store_true",
                        help="Rebuild from 2025-01-01 instead of merging onto existing data")
    parser.add_argument("--output", default=OUTPUT_PATH)
    args = parser.parse_args()

    end_date = parse_date(args.end) if args.end else default_end()
    if args.start:
        start_date = parse_date(args.start)
    elif args.full_history or not os.path.exists(args.output):
        # The first run (no file yet) reads the whole history.
        start_date = DEFAULT_HISTORY_START
    else:
        start_date = catch_up.start(OUTPUT_PATH, end_date - timedelta(days=LOOKBACK_DAYS))
    start_date = max(start_date, DEFAULT_HISTORY_START)
    if end_date < start_date:
        raise ValueError("End date must not be before start date.")

    login, password = os.environ.get("GME_API_LOGIN"), os.environ.get("GME_API_PASSWORD")
    if not login or not password:
        raise RuntimeError("GME_API_LOGIN and GME_API_PASSWORD environment variables must be set.")

    print()
    print("=" * 70)
    print("GME MGP TRANSITS AND TRANSMISSION LIMITS")
    print("=" * 70)
    print(f"Date range: {start_date} -> {end_date}")
    print()

    existing = (empty(), empty()) if args.full_history else load_existing(args.output)
    flows, prices = fetch(login, password, start_date, end_date, existing)
    compact.dump(build_output(flows, prices), args.output)

    print("=" * 70)
    print(f"Wrote: {args.output}")
    print("=" * 70)


if __name__ == "__main__":
    main()
