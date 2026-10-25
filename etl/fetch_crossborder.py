"""
Fetch Italy's cross-border electricity exchanges from the ENTSO-E
Transparency Platform, per neighbouring control area and direction:

  - Physical Flows                     [12.1.G]  documentType A11
  - Scheduled Commercial Exchanges     [12.1.F]  documentType A09
      * Day-ahead  (contract_MarketAgreement.Type A01): commercial
        exchanges from yearly, quarterly, monthly, weekly and daily
        allocations.
      * Total      (contract_MarketAgreement.Type A05): all allocations,
        i.e. day-ahead plus intraday.
    (ENTSO-E Detailed Data Descriptions v3r4, TR article 12.1.f.)
  - Forecasted Transfer Capacities     [11.1.A]  documentType A61
      * Day-ahead  (contract_MarketAgreement.Type A01): the capacity
        offered each way (NTC), which the dashboard's real time grid sets
        the physical flows against.

"imports" are flows/schedules into the Italian control area, "exports"
out of it.  Output: app/data/crossborder.json with 15-minute / hourly /
daily series on the same Italian market-time basis as the other data.

With --zone, the same for an Italian bidding zone and the zones and
countries next to it (ZONE_BORDERS): app/data/zones/<ZONE>/crossborder.json
(etl/zones.py), imports into the zone and exports out of it.  Sardinia's
links to Corsica are ENTSO-E's areas IT-SACO AC (SARCO, alternating current)
and IT-SACO DC (SACOI, direct current, which goes on to Centre-North),
stored under GME's names of the two Corsica zones, COAC and CORS; ENTSO-E
publishes no physical flow on the DC link, only its schedules.

Quarter-hourly series are stored from 1 October 2025 (15-minute market
time unit) only; before that the dashboard repeats the hourly value.

The API token is read from the ENTSOE_API_KEY environment variable.
"""

import argparse
import os
import time
from datetime import date, timedelta

import requests

import compact
from entsoe_api import (
    REQUEST_PAUSE_SECONDS,
    day_chunks,
    market_today,
    merge_resolutions,
    parse_date,
    parse_points,
    request_entsoe,
    to_api_datetime,
)
from zones import IT_DOMAIN, ZONE_DOMAINS, ZONE_NAMES, area_label, run_areas, targets

# ============================================================================
# CONFIGURATION
# ============================================================================

# Neighbouring control areas (EIC codes).
BORDERS = {
    "AT": ("Austria", "10YAT-APG------L"),
    "CH": ("Switzerland", "10YCH-SWISSGRIDZ"),
    "FR": ("France", "10YFR-RTE------C"),
    "GR": ("Greece", "10YGR-HTSO-----Y"),
    "ME": ("Montenegro", "10YCS-CG-TSO---S"),
    "MT": ("Malta", "10Y1001A1001A93C"),
    "SI": ("Slovenia", "10YSI-ELES-----O"),
}

# The Corsica ends of Sardinia's links (ENTSO-E areas), under GME's zone names.
CORSICA = {
    "COAC": ("Corsica AC (SARCO)", "10Y1001A1001A885"),
    "CORS": ("Corsica DC (SACOI)", "10Y1001A1001A893"),
}


def _zone(code):
    return (ZONE_NAMES[code], ZONE_DOMAINS[code])


# Each bidding zone's neighbours: zones, countries, Corsica.
ZONE_BORDERS = {
    "NORD": {"CNOR": _zone("CNOR"), **{code: BORDERS[code] for code in ("FR", "CH", "AT", "SI")}},
    "CNOR": {"NORD": _zone("NORD"), "CSUD": _zone("CSUD"), "CORS": CORSICA["CORS"]},
    "CSUD": {"CNOR": _zone("CNOR"), "SUD": _zone("SUD"), "SARD": _zone("SARD"), "ME": BORDERS["ME"]},
    "SUD": {"CSUD": _zone("CSUD"), "CALA": _zone("CALA"), "GR": BORDERS["GR"]},
    "CALA": {"SUD": _zone("SUD"), "SICI": _zone("SICI")},
    "SICI": {"CALA": _zone("CALA"), "MT": BORDERS["MT"]},
    "SARD": {"CSUD": _zone("CSUD"), "CORS": CORSICA["CORS"], "COAC": CORSICA["COAC"]},
}

DATASETS = {
    "physical_flows": {"documentType": "A11"},
    "scheduled_day_ahead": {"documentType": "A09", "contract_MarketAgreement.Type": "A01"},
    "scheduled_total": {"documentType": "A09", "contract_MarketAgreement.Type": "A05"},
    "capacity_day_ahead": {"documentType": "A61", "contract_MarketAgreement.Type": "A01"},
}

DIRECTIONS = ("imports", "exports")

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "app",
    "data",
    "crossborder.json",
)

DEFAULT_HISTORY_START = date(2025, 1, 1)

# Incremental runs re-download this many days, so points published late
# or revised are picked up by a later run.
LOOKBACK_DAYS = 7
PT15_START = date(2025, 10, 1)

# These documents are one series per request; the API accepts up to a
# year per request, half a year keeps responses modest.
CHUNK_DAYS = 183

RESOLUTIONS = ("quarter_hourly", "hourly", "daily")


# ============================================================================
# DOWNLOAD
# ============================================================================


def domains(direction, neighbour, home=IT_DOMAIN):
    """in_Domain receives the energy, out_Domain sends it."""
    if direction == "imports":
        return {"in_Domain": home, "out_Domain": neighbour}
    return {"in_Domain": neighbour, "out_Domain": home}


def download_dataset(token, dataset, start_date, end_date, borders=BORDERS, home=IT_DOMAIN):
    """Point records grouped as "<direction>|<country>"."""
    records = []
    failures = 0
    requests_made = 0

    for country, (label, eic) in borders.items():
        for direction in DIRECTIONS:
            group = f"{direction}|{country}"

            for chunk_start, chunk_end in day_chunks(start_date, end_date, CHUNK_DAYS):
                print(f"  {dataset} {direction:7} {country}: {chunk_start} -> {chunk_end}")
                requests_made += 1

                params = {
                    **DATASETS[dataset],
                    **domains(direction, eic, home),
                    "periodStart": to_api_datetime(chunk_start),
                    "periodEnd": to_api_datetime(chunk_end, end_of_day=True),
                }

                try:
                    root = request_entsoe(token, params)
                except requests.HTTPError as error:
                    # One border failing must not stop the others; the
                    # existing data for it is kept.
                    failures += 1
                    print(f"    WARNING: {error}")
                    continue

                records.extend(parse_points(root, group_of=lambda ts, g=group: g))
                time.sleep(REQUEST_PAUSE_SECONDS)

    if requests_made and failures == requests_made:
        raise RuntimeError(f"Every request for {dataset} failed.")

    return records


# ============================================================================
# LOAD / SAVE
# ============================================================================


def empty_dataset():
    return {resolution: {} for resolution in RESOLUTIONS}


def load_existing(path=OUTPUT_PATH):
    """{dataset: {resolution: {"<direction>|<country>": rows}}}."""
    existing = {name: empty_dataset() for name in DATASETS}

    if not os.path.exists(path):
        return existing

    payload = compact.load(path)

    for name in DATASETS:
        for resolution in RESOLUTIONS:
            by_direction = payload.get(name, {}).get(resolution, {})
            for direction in DIRECTIONS:
                for country, rows in by_direction.get(direction, {}).items():
                    existing[name][resolution][f"{direction}|{country}"] = rows

    return existing


def encode_dataset(dataset):
    output = {}
    for resolution in RESOLUTIONS:
        output[resolution] = {direction: {} for direction in DIRECTIONS}
        for group, rows in sorted(dataset[resolution].items()):
            direction, country = group.split("|")
            output[resolution][direction][country] = compact.encode_series(
                rows,
                resolution,
                # Before the 15-minute MTU the dashboard expands hourly.
                skip_before=PT15_START.isoformat() if resolution == "quarter_hourly" else None,
            )
    return output


def build_output(datasets, borders=BORDERS, area=f"IT ({IT_DOMAIN})"):
    return {
        "source": "ENTSO-E Transparency Platform",
        "control_area": area,
        "description": (
            f"Cross-border exchanges between {area} and "
            "each neighbouring area, per direction: physical flows "
            "(12.1.G), scheduled commercial exchanges (12.1.F), day-ahead "
            "(long-term + day-ahead allocations) and total (all allocations "
            "including intraday), and the day-ahead forecasted transfer "
            "capacity (11.1.A, imports: into the area, exports: out of it). "
            "Values in MW; hourly = average of the "
            "quarter-hours; daily = sum of the hourly values (MWh/day). "
            "Italian market time (Europe/Rome), labelled by elapsed time "
            "since local midnight like GME periods. Quarter-hourly series "
            "start at quarter_hourly_native_from; earlier quarter-hours "
            "repeat the hourly value."
        ),
        "quarter_hourly_native_from": PT15_START.isoformat(),
        "borders": {country: label for country, (label, _) in borders.items()},
        **{name: encode_dataset(dataset) for name, dataset in datasets.items()},
    }


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Fetch ENTSO-E cross-border flows and scheduled exchanges for Italy."
    )
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="Rebuild from 2025-01-01 instead of merging onto existing data",
    )
    parser.add_argument("--zone", default=None,
                        help="A bidding zone (NORD, CNOR, CSUD, SUD, CALA, SICI, SARD) or 'all'; default Italy")
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

    token = os.environ.get("ENTSOE_API_KEY")
    if not token:
        raise RuntimeError("ENTSOE_API_KEY environment variable must be set.")

    # An area without a file yet (a zone's first run) reads its whole history.
    explicit = bool(args.start) or args.full_history
    run_areas(targets(args.zone, OUTPUT_PATH, "crossborder.json"),
              lambda label, domain, path: fetch_area(
                  token, label, domain, path,
                  start_date if explicit or os.path.exists(path) else DEFAULT_HISTORY_START,
                  end_date, args.full_history))


def fetch_area(token, label, domain, path, start_date, end_date, full_history):
    area = area_label(label, domain)
    borders = BORDERS if label == "IT" else ZONE_BORDERS[label]
    print()
    print("=" * 70)
    print(f"ENTSO-E CROSS-BORDER EXCHANGES ({area})")
    print("=" * 70)
    print(f"Date range: {start_date} -> {end_date}")
    print()

    existing = (
        {name: empty_dataset() for name in DATASETS}
        if full_history
        else load_existing(path)
    )

    datasets = {}
    for name in DATASETS:
        print(f"Downloading {name}...")
        records = download_dataset(token, name, start_date, end_date, borders, domain)
        print(f"  {len(records):,} points")

        datasets[name] = merge_resolutions(existing[name], records, RESOLUTIONS)
        print()

    if not any(datasets["physical_flows"]["daily"].values()):
        raise RuntimeError(f"No physical flow data downloaded for {area}; not writing output.")

    compact.dump(build_output(datasets, borders, area), path)

    print("=" * 70)
    print(f"Wrote: {path}")
    print("=" * 70)

if __name__ == "__main__":
    main()
