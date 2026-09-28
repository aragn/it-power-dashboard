"""
Fetch the GME MGP market coupling results for Italy's coupled borders
(ME_MarketCoupling) and the Italian-side prices of those borders
(ME_ZonalPrices), and write app/data/coupling.json.

ME_MarketCoupling returns, per coupled zone and period, the import and
export limits and flows of the border, "import" meaning into Italy:
  BSP   Italia Coupling - Slovenia Coupling
  XAUS  Italia Coupling - Austria Coupling
  XFRA  Italia Coupling - Francia Coupling
  SUD   Sud - Grecia Coupling

The Italian side of the first three is the "Italia Coupling" zone (COUP),
of the Greek border the Sud zone (SUD); their MGP prices are stored too.
(GME's "... Coupling" zones of the neighbours carry the Italian price, not
the neighbour's market price, so the dashboard takes those from the
ENTSO-E day-ahead prices in neighbour_prices.json.)

Periods are 15 minutes from 1 October 2025 and hours before (when the
Period field is 0).  Flows and limits: hourly = average of the
quarter-hours (MW), daily = sum of the hours (MWh).  Prices: hourly and
daily averages (EUR/MWh).  Quarter-hourly series are stored from
1 October 2025; before that the dashboard repeats the hourly value.

Credentials come from GME_API_LOGIN / GME_API_PASSWORD.
"""

import argparse
import os
import time
from datetime import date, datetime, timedelta

import compact
from entsoe_api import merge_resolutions
from fetch_zonal import (
    DEFAULT_HISTORY_START,
    PT15_START,
    REQUEST_PAUSE_SECONDS,
    market_time_from_period,
    parse_date,
    request_data,
)

# ============================================================================
# CONFIGURATION
# ============================================================================

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "app",
    "data",
    "coupling.json",
)

# Coupled zone (as ME_MarketCoupling names it) -> label and Italian side.
BORDERS = {
    "BSP": {"label": "Italia Coupling - Slovenia Coupling", "italian_zone": "COUP"},
    "XAUS": {"label": "Italia Coupling - Austria Coupling", "italian_zone": "COUP"},
    "XFRA": {"label": "Italia Coupling - Francia Coupling", "italian_zone": "COUP"},
    "SUD": {"label": "Sud - Grecia Coupling", "italian_zone": "SUD"},
}

FLOW_FIELDS = {
    "ImportFlow": "import_flow",
    "ExportFlow": "export_flow",
    "ImportLimit": "import_limit",
    "ExportLimit": "export_limit",
}

PRICE_ZONES = sorted({border["italian_zone"] for border in BORDERS.values()})

RESOLUTIONS = ("quarter_hourly", "hourly", "daily")

# Incremental runs re-download this many days to pick up corrections.
LOOKBACK_DAYS = 7


# ============================================================================
# ROWS -> RECORDS
# ============================================================================


def slot(row):
    """(date, "HH:MM", minutes) of a GME row: 15-minute periods from the
    15-minute market time unit, hours (Period 0) before."""
    day = datetime.strptime(str(row["FlowDate"]), "%Y%m%d").date()
    period = int(float(row.get("Period") or 0))
    if period > 0 and day >= PT15_START:
        return day.isoformat(), market_time_from_period(period, 15), 15
    hour = int(float(row.get("Hour") or period))
    return day.isoformat(), f"{hour - 1:02d}:00", 60


def coupling_records(rows):
    """Point records grouped as "<zone>|<field>"."""
    records = []
    unknown = set()
    for row in rows:
        zone = str(row.get("Zone") or "").strip().upper()
        if zone not in BORDERS:
            unknown.add(zone)
            continue
        day, label, minutes = slot(row)
        for field, name in FLOW_FIELDS.items():
            if row.get(field) is None:
                continue
            records.append({
                "group": f"{zone}|{name}",
                "date": day,
                "time": label,
                "minutes": minutes,
                "value": round(float(row[field]), 1),
            })
    if unknown:
        print(f"  Ignored unexpected coupling zones: {sorted(unknown)}")
    return records


def price_records(rows):
    """Point records of the Italian-side zones, grouped by zone."""
    records = []
    for row in rows:
        zone = str(row.get("Zone") or "").strip().upper()
        if zone not in PRICE_ZONES or row.get("Price") is None:
            continue
        day, label, minutes = slot(row)
        records.append({
            "group": zone,
            "date": day,
            "time": label,
            "minutes": minutes,
            "value": round(float(row["Price"]), 2),
        })
    return records


# ============================================================================
# LOAD / SAVE
# ============================================================================


def empty():
    return {resolution: {} for resolution in RESOLUTIONS}


def load_existing():
    """Flows as {resolution: {"<zone>|<field>": rows}}, prices as
    {resolution: {zone: rows}}."""
    flows, prices = empty(), empty()
    if not os.path.exists(OUTPUT_PATH):
        return flows, prices

    payload = compact.load(OUTPUT_PATH)
    for resolution in RESOLUTIONS:
        for zone, fields in payload.get("flows", {}).get(resolution, {}).items():
            for name, rows in fields.items():
                flows[resolution][f"{zone}|{name}"] = rows
        prices[resolution] = dict(payload.get("prices", {}).get(resolution, {}))
    return flows, prices


def encode(rows, resolution):
    return compact.encode_series(
        rows,
        resolution,
        skip_before=PT15_START.isoformat() if resolution == "quarter_hourly" else None,
    )


def build_output(flows, prices):
    nested = {}
    for resolution in RESOLUTIONS:
        nested[resolution] = {}
        for group, rows in sorted(flows[resolution].items()):
            zone, name = group.split("|")
            nested[resolution].setdefault(zone, {})[name] = encode(rows, resolution)

    return {
        "source": "GME - MGP market coupling (ME_MarketCoupling) and zonal prices (ME_ZonalPrices)",
        "description": (
            "Import/export limits and flows of Italy's coupled borders in the "
            "MGP (import = into Italy), MW; daily values are MWh/day. MGP "
            "prices of the Italian side of those borders (Italia Coupling, "
            "Sud), EUR/MWh; daily values are averages. Italian market time, "
            "labelled by elapsed time since local midnight like GME periods. "
            "Quarter-hourly series start at quarter_hourly_native_from; "
            "earlier quarter-hours repeat the hourly value."
        ),
        "quarter_hourly_native_from": PT15_START.isoformat(),
        "borders": BORDERS,
        "flows": nested,
        "prices": {
            resolution: {zone: encode(rows, resolution) for zone, rows in sorted(prices[resolution].items())}
            for resolution in RESOLUTIONS
        },
    }


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="Fetch GME MGP market coupling results.")
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
    print("GME MGP MARKET COUPLING")
    print("=" * 70)
    print(f"Date range: {start_date} -> {end_date}")
    print()

    flows, prices = (empty(), empty()) if args.full_history else load_existing()

    print("Downloading market coupling limits and flows...")
    rows = request_data(login, password, start_date, end_date, data_name="ME_MarketCoupling")
    flow_points = coupling_records(rows)
    print(f"  {len(flow_points):,} points")

    time.sleep(REQUEST_PAUSE_SECONDS)

    print("Downloading Italian-side zonal prices...")
    rows = request_data(login, password, start_date, end_date)
    price_points = price_records(rows)
    print(f"  {len(price_points):,} points")

    flows = merge_resolutions(flows, flow_points, RESOLUTIONS)
    prices = merge_resolutions(prices, price_points, RESOLUTIONS, daily="mean")

    for zone in BORDERS:
        days = flows["daily"].get(f"{zone}|import_flow", [])
        print(f"  {zone}: {len(days):,} days" + (f", last {days[-1]['date']}" if days else ""))
    for zone in PRICE_ZONES:
        print(f"  {zone} price: {len(prices['daily'].get(zone, [])):,} days")

    if not flow_points and not any(flows["daily"].values()):
        raise RuntimeError("No market coupling data downloaded; not writing output.")

    compact.dump(build_output(flows, prices), OUTPUT_PATH)

    print("=" * 70)
    print(f"Wrote: {OUTPUT_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    main()
