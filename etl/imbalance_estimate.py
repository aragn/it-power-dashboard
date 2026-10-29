"""
Imbalance estimate of the large units: for each Italian unit ENTSO-E
publishes the output of (16.1.A, 100 MW or more; fetch_unit_generation.py),
its metered output against its final schedule in GME's markets, quarter-hour
by quarter-hour, written per month to app/data/unit_imbalance/<YYYY-MM>.json.gz
for the Analysis tab, with app/data/unit_imbalance/index.json.

  schedule = MGP + MI-A (the three auctions) + MI-XBID + MSD + MB, MW:
    MGP, MI-A   the unit's accepted sales minus its accepted purchases
                (mgp_merit, mi_merit files)
    MI-XBID     its fills, sales minus purchases; an hourly product's in
                each of its four quarter-hours (market_offers/XBID)
    MSD         its accepted energy offers, up minus down, scopes GR1-GR4
                and AS; the secondary reserve band (RS) and the start-up
                and change-of-mode events left out (market_offers/MSD)
    MB          its accepted offers, up minus down, every scope (RS: the
                secondary regulation energy) (market_offers/MB)
  imbalance = metered - schedule (above zero: the unit produced more).

Per unit and day, per quarter-hour: the metered MW and the five markets'
MW ("fields").  Per zone and day: the imbalance price (ENTSO-E 17.1.G,
balancing/<ZONE>.json) and the MGP zonal price (zonal_prices.json), for
the page to value the imbalance.

Units are matched by name: ENTSO-E names its generation units by their GME
code (UP_...), or a production unit name holding it (IM_<n>UP_...); a GME
unit with several ENTSO-E generation units gets their sum.  Storage
(batteries, pumped hydro) is metered charging too, below zero
(fetch_unit_generation.py).

  imbalance_estimate.py --month 2026-07
"""

import argparse
import gzip
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compact import decode_series  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "app", "data")
OUT_DIR = os.path.join(DATA, "unit_imbalance")
ZONES = ["NORD", "CNOR", "CSUD", "SUD", "CALA", "SICI", "SARD"]
FIELDS = ["metered", "mgp", "mia", "xbid", "msd", "mb"]
MSD_SCOPES = {0, 1, 2, 3, 4}          # GR1-GR4, AS (fetch_market_offers.ENERGY_SCOPES); 5 = RS
SOURCE = ("ENTSO-E 16.1.A (actual generation per generation unit) and 17.1.G (imbalance prices); "
          "GME public offers (MGP, MI-A, MI-XBID, MSD, MB): schedules estimated by the author")


def read(path):
    if not os.path.exists(path):
        return None
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def gme_code(name):
    match = re.search(r"(UP_[A-Za-z0-9_.\-]+)", name or "")
    return match.group(1) if match else None


def position(item, index):
    """A quarter-hour's index in the day: its period, else its place."""
    return int(item["period"]) - 1 if "period" in item else index


def merit_net(add, data, market):
    """A merit-order file's accepted sales minus purchases per unit (MGP, or an MI-A auction)."""
    units = data["units"]
    for index, quarter in enumerate(data["quarters"] if market is None else data["markets"].get(market, [])):
        q = position(quarter, index)
        supply = quarter.get("supply", [])
        for i in range(0, len(supply), 7):
            if supply[i + 4]:
                add(units[supply[i]][0], q, supply[i + 4])
        purchases = quarter.get("purchases", [])
        for i in range(0, len(purchases), 4):
            add(units[purchases[i]][0], q, -purchases[i + 1])


def xbid_net(add, data):
    units = data["units"]
    for key, span in (("quarters", 1), ("hours", 4)):
        for index, item in enumerate(data.get(key, [])):
            first = position(item, index) * span
            fills = item.get("fills", [])
            for i in range(0, len(fills), 5):
                mw = fills[i + 2] if fills[i + 1] == 0 else -fills[i + 2]
                for q in range(first, first + span):
                    add(units[fills[i]][0], q, mw)


def balancing_net(add, data, scopes=None):
    """MSD / MB: accepted energy offers, up minus down."""
    units = data["units"]
    for quarter in data.get("quarters", []):
        # MB's quarter-hours have no period: their clock time (no clock change in the months read so far).
        hours, minutes = (int(part) for part in quarter["time"].rstrip("*").split(":"))
        q = int(quarter["period"]) - 1 if "period" in quarter else hours * 4 + minutes // 15
        offers = quarter.get("offers", [])
        for i in range(0, len(offers), 8):
            unit, direction, scope, _mw, _price, status, accepted = offers[i:i + 7]
            if status != 0 or not accepted or (scopes is not None and scope not in scopes):
                continue
            add(units[unit][0], q, accepted if direction == 0 else -accepted)


def day_schedules(day, wanted):
    """Per GME code in wanted: {field: [MW per quarter-hour]} of the day; the markets on file."""
    key = day.isoformat()
    quarters = 96 if day.month not in (3, 10) else None
    files = {
        "mgp": read(os.path.join(DATA, "mgp_merit", f"{key}.json.gz")),
        "mia": read(os.path.join(DATA, "mi_merit", f"{key}.json.gz")),
        "xbid": read(os.path.join(DATA, "market_offers", "XBID", f"{key}.json.gz")),
        "msd": read(os.path.join(DATA, "market_offers", "MSD", f"{key}.json.gz")),
        "mb": read(os.path.join(DATA, "market_offers", "MB", f"{key}.json.gz")),
    }
    if quarters is None and files["mgp"]:
        quarters = len(files["mgp"]["quarters"])
    quarters = quarters or 96
    out = defaultdict(lambda: {field: [0.0] * quarters for field in FIELDS[1:]})

    def adder(field):
        def add(code, q, mw):
            if code in wanted and 0 <= q < quarters:
                out[code][field][q] += mw
        return add

    if files["mgp"]:
        merit_net(adder("mgp"), files["mgp"], None)
    if files["mia"]:
        for market in ("MI-A1", "MI-A2", "MI-A3"):
            merit_net(adder("mia"), files["mia"], market)
    if files["xbid"]:
        xbid_net(adder("xbid"), files["xbid"])
    if files["msd"]:
        balancing_net(adder("msd"), files["msd"], MSD_SCOPES)
    if files["mb"]:
        balancing_net(adder("mb"), files["mb"])
    return out, quarters, [field for field, data in files.items() if data]


def series_day(series, day):
    start = date.fromisoformat(series["start"])
    index = (day - start).days
    rows = series.get("days") or []
    return rows[index] if 0 <= index < len(rows) else None


def prices(days):
    """Per zone and day: imbalance and MGP zonal prices per quarter-hour."""
    zonal = read(os.path.join(DATA, "zonal_prices.json"))["zones"]
    out = {}
    for zone in ZONES:
        balancing = read(os.path.join(DATA, "balancing", f"{zone}.json"))
        imbalance = balancing["series"]["quarter_hourly"].get(f"{zone}|imbalance_price") if balancing else None
        da = zonal.get(zone, {}).get("quarter_hourly")
        out[zone] = {day.isoformat(): {"imbalance": series_day(imbalance, day) if imbalance else None,
                                       "da": series_day(da, day) if da else None} for day in days}
    return out


def unit_info():
    """GME code -> [source, operator, zone, kind] (gme_units.json) and MW: ENTSO-E's installed
    capacity per production unit (14.1.B, the latest year listed)."""
    from fetch_mgp_merit import unit_sources
    units = unit_sources()
    state = read(os.path.join(DATA, "gme_units_state.json")) or {}
    installed = ((state.get("registries") or {}).get("entsoe") or {}).get("installed_14_1_B", {})
    mw = {}
    for key in sorted(installed):              # "year|mRID": the later year last
        record = installed[key]
        code = gme_code(record.get("registeredResource.name"))
        if code and record.get("installed_MW"):
            mw[code] = float(record["installed_MW"])
    return units, mw


def build_month(month):
    first = date.fromisoformat(f"{month}-01")
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    units, mw = unit_info()
    metered = {}           # day -> {code: [MW]}, psr
    psr = {}
    for day in days:
        data = read(os.path.join(DATA, "unit_generation", f"{day}.json.gz"))
        if not data:
            continue
        by_code = {}
        for (_eic, name, kind), row in zip(data["units"], data["values"]):
            code = gme_code(name)
            if not code:
                continue
            psr[code] = kind
            total = by_code.setdefault(code, [None] * data["quarters"])
            for q, value in enumerate(row):
                if value is not None:
                    total[q] = (total[q] or 0.0) + value
        metered[day] = by_code
    on_file = [day for day in days if day in metered]
    codes = sorted({code for by_code in metered.values() for code in by_code})
    wanted = set(codes)
    unit_index = {code: n for n, code in enumerate(codes)}
    rows = [[None] * len(on_file) for _ in codes]
    markets, quarters_of = {}, []
    for d, day in enumerate(on_file):
        schedules, quarters, present = day_schedules(day, wanted)
        markets[day.isoformat()] = present
        quarters_of.append(quarters)
        for code, values in metered[day].items():
            schedule = schedules.get(code) or {field: [0.0] * quarters for field in FIELDS[1:]}
            rows[unit_index[code]][d] = [[None if v is None else round(v, 1) for v in values[:quarters]]] + \
                [[round(v, 1) for v in schedule[field]] for field in FIELDS[1:]]
        print(f"  {day}: {len(metered[day])} units metered; markets on file: {', '.join(present) or 'none'}")
    unit_rows = []
    for code in codes:
        source, operator, zone, kind = units.get(code) or (None, None, None, None)
        unit_rows.append([code, source, operator, zone, psr.get(code), mw.get(code)])
    data = {"month": month, "source": SOURCE, "fields": FIELDS, "days": [day.isoformat() for day in on_file],
            "quarters": quarters_of, "markets": markets,
            "units_columns": ["code", "source", "operator", "zone", "psr", "mw"], "units": unit_rows,
            "data": rows, "prices": prices(on_file)}
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{month}.json.gz")
    with open(path, "wb") as handle:
        handle.write(gzip.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"), compresslevel=9, mtime=0))
    write_index()
    print(f"Unit imbalance {month}: {len(on_file)} day(s), {len(codes)} units, {os.path.getsize(path) / 1e6:.1f} MB")
    return data


def write_index():
    months = {}
    for name in sorted(os.listdir(OUT_DIR)):
        if name.endswith(".json.gz"):
            data = read(os.path.join(OUT_DIR, name))
            months[data["month"]] = {"file": name, "days": data["days"], "markets": data["markets"]}
    with open(os.path.join(OUT_DIR, "index.json"), "w", encoding="utf-8") as handle:
        json.dump({"months": months}, handle, separators=(",", ":"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--month", required=True, help="YYYY-MM")
    args = parser.parse_args()
    build_month(args.month)


if __name__ == "__main__":
    main()
