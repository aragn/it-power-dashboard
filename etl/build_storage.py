"""
Storage units in GME's markets: the battery and pumped hydro units' offers,
bids, sales, purchases and estimated revenues per quarter-hour, taken from
the market files already on file (the MGP and MI-A merit orders,
fetch_mgp_merit.py and fetch_mi_merit.py; MSD, MB and MI-XBID,
fetch_market_offers.py), for the page's Storage tab: a day of all markets
in about 50 kB instead of the MGP's 2 MB.

app/data/storage/<date>.json.gz, per quarter-hour (labelled by the clock
with its period, like the market files), one record per storage unit active
in it:

  [unit, MGP MW sold, MGP EUR sold, MGP MW bought, MGP EUR bought,
   MW offered for sale (MGP), its average bid price (EUR/MWh, by MW),
   MW bid to buy (MGP), its average bid price,
   MI-A net EUR, MI-XBID net EUR, MSD net EUR, MB net EUR,
   MW offered in all markets, MI-A net MW, MI-XBID net MW]

  MW of the quarter-hour; EUR of the quarter-hour (MW x price x 0.25 h):
  sales above zero, purchases below in the net columns.  MGP and MI-A at
  the price awarded (the zone's), MI-XBID at the price traded (an hourly
  product's fill in each of its four quarter-hours), MSD and MB pay-as-bid
  (upward paid to the unit, downward paid by it), the secondary reserve
  (RS, a band) and the start-up and change-of-mode events left out, as on
  the Market tab.  The MGP bids to buy per unit are on file from its format
  3 (null before).  MW offered in all markets: the MGP's and MI-A's sale
  offers and bids (the MI-A purchases accepted, its bids not being kept),
  MSD's and MB's energy offers both ways, MI-XBID's trades.  Net MW: sold
  minus bought (with the MGP's, a unit's net position across the energy
  markets).  "fields": the values in a record (16 since VERSION 2, 14 before).

app/data/storage/index.json: the days on file (the markets each had, the
quarter-hours), the units (technology, operator, zone, MW: the database's
installed capacity, else the largest MW offered; in the benchmark or not:
not a unit the database's research marks as a portfolio of plants bid as
one, category "portfolio") and per day and unit the totals (TOTALS) for
the weekly charts: net EUR in all markets, MWh offered, the net EUR of
each market, and the MWh discharged and charged with their EUR, taken
quarter-hour by quarter-hour from the unit's net position across the MGP,
MI-A and MI-XBID (sold minus bought: above zero discharging, below zero
charging; with the EUR of those three markets).

The units' technology is the database's (gme_units.json) as it is now: a
day is built again when its market files or the storage units change.

  build_storage.py            the days whose inputs changed
  build_storage.py --all      every day
"""

import argparse
import gzip
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_mgp_merit import quarter as clock_quarter, read_day  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "app", "data")
OUT_DIR = os.path.join(DATA, "storage")
UNITS_PATH = os.path.join(DATA, "gme_units.json")
TECHNOLOGIES = {"battery": "battery", "pumped_hydro": "pumped_hydro"}
OTHER_MARKETS = ["XBID", "MSD", "MB"]
FIELDS = 16                     # values in a record
RS = 5                          # the secondary reserve's scope in MSD/MB files
PORTFOLIO = "portfolio"         # a unit's category: plants bid as one (left out of the benchmark)
VERSION = 2                     # 2: MI-A and MI-XBID net MW


def unit_database(path=None):
    """code -> (technology or None, operator, zone, MW, category) from the unit database."""
    try:
        with open(path or UNITS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    columns = data["columns"]
    units = {}
    for values in data["units"]:
        row = dict(zip(columns, values))
        entry = (TECHNOLOGIES.get(row.get("source")), row.get("operator"), row.get("zone"),
                 row.get("capacity_mw") or row.get("offered_mw"), row.get("category"))
        units[row["code"]] = entry
        for code in row.get("codes") or []:
            units.setdefault(code, entry)
    return units


def position(item, day):
    """The quarter-hour index (period - 1) of a market file's quarter-hour."""
    return item["period"] - 1 if "period" in item else clock_quarter(item["time"], day)


class Day:
    """A day's storage records, built market by market."""

    def __init__(self, day, database):
        self.day = day
        self.database = database
        self.units, self.index = [], {}
        self.rows = defaultdict(dict)        # quarter index -> unit index -> record
        self.times = {}                      # quarter index -> (clock time, period)

    def unit(self, file, index):
        """The storage unit index of a market file's unit, or None."""
        code, source, operator, zone = (file["units"][index] + [None] * 5)[:4]
        known = self.database.get(code)
        technology = known[0] if known else TECHNOLOGIES.get(source)
        if not technology:
            return None
        if code not in self.index:
            self.index[code] = len(self.units)
            self.units.append([code, technology, (known and known[1]) or operator, (known and known[2]) or zone,
                               known[3] if known else None])
        return self.index[code]

    def record(self, quarter, unit):
        rows = self.rows[quarter]
        if unit not in rows:
            row = [unit] + [0.0] * (FIELDS - 1)
            row[6] = row[7] = row[8] = None      # no offers or bids (yet)
            rows[unit] = row
        return rows[unit]

    def merit(self, file, quarters, market):
        """An MGP or MI-A auction's quarter-hours."""
        cache = {}
        unit_of = lambda index: cache[index] if index in cache else cache.setdefault(index, self.unit(file, index))
        for item in quarters:
            q = position(item, self.day)
            self.times.setdefault(q, (item["time"], q + 1))
            flat = item["supply"]
            for i in range(0, len(flat), 7):
                unit = unit_of(flat[i])
                if unit is None:
                    continue
                row = self.record(q, unit)
                mw, price, status, accepted, awarded = flat[i + 1], flat[i + 2], flat[i + 3], flat[i + 4], flat[i + 5]
                euro = accepted * (awarded or 0) * 0.25 if status == 0 and accepted > 0 else 0.0
                row[13] += mw
                if market == "MGP":
                    if price is not None and mw > 0:
                        row[6] = ((row[6] or 0) * row[5] + price * mw) / (row[5] + mw)
                    row[5] += mw
                    if euro:
                        row[1] += accepted
                        row[2] += euro
                else:
                    row[9] += euro
                    if euro:
                        row[14] += accepted
            flat = item.get("purchases") or []
            for i in range(0, len(flat), 4):
                unit = unit_of(flat[i])
                if unit is None:
                    continue
                row = self.record(q, unit)
                euro = flat[i + 1] * (flat[i + 2] or 0) * 0.25
                if market == "MGP":
                    row[3] += flat[i + 1]
                    row[4] += euro
                    if "bids" not in item:
                        row[13] += flat[i + 1]      # no bids on file: the purchases at least
                else:
                    row[9] -= euro
                    row[13] += flat[i + 1]
                    row[14] -= flat[i + 1]
            if market == "MGP" and "bids" in item:
                flat = item["bids"]
                for i in range(0, len(flat), 6):
                    unit = unit_of(flat[i])
                    if unit is None:
                        continue
                    row = self.record(q, unit)
                    mw, price = flat[i + 1], flat[i + 2]
                    offered = row[7] or 0.0
                    if price is not None and mw > 0:
                        row[8] = ((row[8] or 0) * offered + price * mw) / (offered + mw)
                    row[7] = offered + mw
                    row[13] += mw

    def xbid(self, file):
        cache = {}
        unit_of = lambda index: cache[index] if index in cache else cache.setdefault(index, self.unit(file, index))

        def add(q, flat):
            for i in range(0, len(flat), 5):
                unit = unit_of(flat[i])
                if unit is None:
                    continue
                row = self.record(q, unit)
                sign = 1 if flat[i + 1] == 0 else -1
                row[10] += sign * flat[i + 2] * flat[i + 3] * 0.25
                row[13] += flat[i + 2]
                row[15] += sign * flat[i + 2]

        for item in file.get("quarters", []):
            add(position(item, self.day), item["fills"])
        for item in file.get("hours", []):
            start = (item["period"] - 1) * 4 if "period" in item else int(item["time"][:2]) * 4
            for q in range(start, start + 4):
                add(q, item["fills"])

    def balancing(self, file, column):
        cache = {}
        unit_of = lambda index: cache[index] if index in cache else cache.setdefault(index, self.unit(file, index))
        for item in file.get("quarters", []):
            q = position(item, self.day)
            flat = item["offers"]
            for i in range(0, len(flat), 8):
                if flat[i + 2] == RS:
                    continue
                unit = unit_of(flat[i])
                if unit is None:
                    continue
                row = self.record(q, unit)
                row[13] += flat[i + 3]
                if flat[i + 5] == 0 and flat[i + 6] > 0:
                    sign = 1 if flat[i + 1] == 0 else -1
                    row[column] += sign * flat[i + 6] * (flat[i + 4] or 0) * 0.25

    def data(self, markets, bids):
        quarters = []
        for q in sorted(self.rows):
            time, period = self.times.get(q) or (None, q + 1)
            if time is None:
                continue                     # a quarter-hour not in the day's MGP file
            rows = sorted(self.rows[q].values(), key=lambda row: row[0])
            quarters.append({"time": time, "period": period,
                             "rows": [round(value, 3) if isinstance(value, float) else value
                                      for row in rows for value in row]})
        return {"date": self.day.isoformat(), "version": VERSION, "fields": FIELDS, "markets": markets, "bids": bids,
                "units": self.units, "quarters": quarters}


def input_files(day):
    """market -> path of the day's file (those on file)."""
    key = day.isoformat()
    found = {}
    for market, folder in (("MGP", "mgp_merit"), ("MI-A", "mi_merit")):
        for name in (f"{key}.json.gz", f"{key}.json"):
            path = os.path.join(DATA, folder, name)
            if os.path.exists(path):
                found[market] = path
                break
    for market in OTHER_MARKETS:
        for name in (f"{key}.json.gz", f"{key}.json"):
            path = os.path.join(DATA, "market_offers", market, name)
            if os.path.exists(path):
                found[market] = path
                break
    return found


def signature(files, units_key):
    """The day's inputs: its market files' contents, the storage units and the format."""
    digest = hashlib.sha1(f"{units_key}|{VERSION}".encode())
    for market, path in sorted(files.items()):
        digest.update(market.encode())
        with open(path, "rb") as handle:
            digest.update(handle.read())
    return digest.hexdigest()[:16]


def build_day(day, files, database):
    """The day's storage file from its market files (MGP required)."""
    result = Day(day, database)
    mgp = read_day(files["MGP"])
    result.merit(mgp, mgp["quarters"], "MGP")
    bids = any("bids" in item for item in mgp["quarters"])
    markets = ["MGP"]
    if "MI-A" in files:
        mi = read_day(files["MI-A"])
        for name, quarters in sorted((mi.get("markets") or {}).items()):
            result.merit(mi, quarters, name)
        markets.append("MI-A")
    for market in OTHER_MARKETS:
        if market not in files:
            continue
        file = read_day(files[market])
        if market == "XBID":
            result.xbid(file)
        else:
            result.balancing(file, 11 if market == "MSD" else 12)
        markets.append(market)
    return result.data(markets, bids)


TOTALS = ["net_eur", "offered_mwh", "mgp_eur", "mi_eur", "xbid_eur", "msd_eur", "mb_eur",
          "discharged_mwh", "discharged_eur", "charged_mwh", "charged_eur"]


def day_totals(data):
    """unit code -> its TOTALS of a day file."""
    totals = {}
    size = data.get("fields", 14)
    for item in data["quarters"]:
        flat = item["rows"]
        for i in range(0, len(flat), size):
            code = data["units"][flat[i]][0]
            sums = totals.setdefault(code, [0.0] * len(TOTALS))
            mgp = flat[i + 2] - flat[i + 4]
            sums[0] += mgp + flat[i + 9] + flat[i + 10] + flat[i + 11] + flat[i + 12]
            sums[1] += flat[i + 13] * 0.25
            sums[2] += mgp
            for column, field in ((3, 9), (4, 10), (5, 11), (6, 12)):
                sums[column] += flat[i + field]
            # The net position across the energy markets (MI-A and MI-XBID MW since VERSION 2).
            position = flat[i + 1] - flat[i + 3] + (flat[i + 14] + flat[i + 15] if size > 15 else 0.0)
            euro = mgp + flat[i + 9] + flat[i + 10]
            if position > 0:
                sums[7] += position * 0.25
                sums[8] += euro
            elif position < 0:
                sums[9] -= position * 0.25
                sums[10] -= euro
    return {code: [round(value, 1) for value in sums] for code, sums in sorted(totals.items())}


def write_json(payload, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if path.endswith(".gz"):
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(text)
    else:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)


def read_index(out_dir):
    path = os.path.join(out_dir, "index.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def mgp_days():
    folder = os.path.join(DATA, "mgp_merit")
    if not os.path.isdir(folder):
        return []
    return sorted(date.fromisoformat(name[:10]) for name in os.listdir(folder)
                  if name[:4].isdigit() and name.endswith((".json", ".json.gz")))


def write_index(out_dir, signatures, database):
    days, files, daily, units = {}, {}, {}, {}
    for name in sorted(os.listdir(out_dir)):
        if not name[:4].isdigit() or not name.endswith((".json", ".json.gz")):
            continue
        data = read_day(os.path.join(out_dir, name))
        days[data["date"]] = {"markets": data["markets"], "bids": data["bids"],
                              "times": [item["time"] for item in data["quarters"]]}
        files[data["date"]] = name
        daily[data["date"]] = day_totals(data)
        for code, technology, operator, zone, mw in data["units"]:
            category = (database.get(code) or (None,) * 5)[4]
            units[code] = [technology, operator, zone, mw, category != PORTFOLIO]
    index = {"days": days, "files": files, "units": dict(sorted(units.items())), "totals": TOTALS, "daily": daily,
             "signatures": {day: signatures[day] for day in days if day in signatures}}
    write_json(index, os.path.join(out_dir, "index.json"))
    return index


def build(out_dir=OUT_DIR, everything=False):
    database = unit_database()
    storage = sorted((code, entry[0]) for code, entry in database.items() if entry[0])
    units_key = hashlib.sha1(json.dumps(storage).encode()).hexdigest()[:12]
    old = read_index(out_dir).get("signatures", {})
    signatures, built = {}, []
    for day in mgp_days():
        files = input_files(day)
        key = day.isoformat()
        signatures[key] = signature(files, units_key)
        path = os.path.join(out_dir, f"{key}.json.gz")
        if not everything and old.get(key) == signatures[key] and os.path.exists(path):
            continue
        write_json(build_day(day, files, database), path)
        built.append(key)
    os.makedirs(out_dir, exist_ok=True)
    index = write_index(out_dir, signatures, database)
    return built, index


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="build every day again")
    args = parser.parse_args()
    built, index = build(everything=args.all)
    days = sorted(index["days"])
    print(f"Storage: {len(built)} day(s) built; on file {len(days)} day(s)"
          + (f", {days[0]} - {days[-1]}" if days else "") + f", {len(index['units'])} storage units.")


if __name__ == "__main__":
    main()
