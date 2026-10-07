"""
The public versions of GME's data files, for the public site (GME's terms:
its data shown publicly only re-elaborated):

  numbers           as they are for now (the owner is asking GME whether
                    that is allowed); with the steps set, rounded: prices
                    (EUR/MWh) to PRICE_STEP (the PUN, zonal, MI-A, MI-XBID,
                    coupling, MSD/MB prices, the merit order's bid, awarded
                    and zonal prices), volumes (MW) to VOLUME_STEP
                    (OFFER_STEP for single offers), the dispatching cost
                    estimate to ESTIMATE_STEP
  market units      no GME codes, operators or code families (the bidding
                    zone stays): each day's units named by source and
                    numbered in a random order
                    ("Gas plant 7"), from a salt kept in the private data
                    (SALT_PATH); the order of the units and of the offers
                    in the file says nothing either
  not published     the unit list (gme_units.json, its state) and the code
                    map (mgp_merit/units.json): removed here

The merit orders, mgp_merit (MGP) and mi_merit (MI-A), and the other
markets' offers, market_offers (MSD, MB, MI-XBID): a unit has one name in
every market of a day, kept in the private data (NAMES_DIR, which the jobs
publish to the private data branch only).

Run in the private repository after a GME job has published its private
files, on the paths that job writes, in place (the runner's copy); the job
then publishes them to the public repository's data branch.

  publicise_gme.py app/data/pun.json app/data/mgp_merit app/data/mi_merit ...
"""

import argparse
import gzip
import json
import os
import random
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compact  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SALT_PATH = os.path.join(ROOT, "app", "data", "public_salt.txt")
# The units' public names of each day (kept in the private data, published
# by the jobs to its data branch): one name per unit in every market.
NAMES_DIR = os.path.join(ROOT, "app", "data", "public_names")

# Rounding steps; None: no rounding (for now).  E.g. 5 EUR/MWh, 1 MW,
# 0.1 MW and EUR 100 if GME asks for rounded numbers.
PRICE_STEP = None       # EUR/MWh
VOLUME_STEP = None      # MW, time series
OFFER_STEP = None       # MW, single offers and bids
ESTIMATE_STEP = None    # EUR

# The page's source groups (MGP_GROUPS) and the public names of their units.
GROUP_OF = {
    "solar": "solar", "wind_onshore": "wind", "wind_offshore": "wind", "hydro": "hydro", "hydro_ror": "hydro",
    "hydro_reservoir": "hydro", "pumped_hydro": "pumped", "geothermal": "geothermal", "bioenergy": "bioenergy",
    "waste": "bioenergy", "other_res": "bioenergy", "gas": "gas", "coal": "coal", "oil": "oil", "battery": "battery",
    "interconnection": "imports", "consumption": "consumption",
}
PUBLIC_NAMES = {
    "solar": "Solar plant", "wind": "Wind farm", "hydro": "Hydro plant", "pumped": "Pumped hydro plant",
    "geothermal": "Geothermal plant", "bioenergy": "Bioenergy plant", "gas": "Gas plant", "coal": "Coal plant",
    "oil": "Oil plant", "battery": "Battery", "imports": "Cross-border unit", "consumption": "Consumption unit",
    "other": "Other power plant",
}
NOTE = "Re-elaborated for the public site: units renamed, unit list not published (GME's terms); source GME."


def step(value, size):
    """value rounded to a multiple of size (None stays None; no size: as it is)."""
    if value is None or size is None:
        return value
    rounded = round(round(value / size) * size, 6)
    return int(rounded) if float(rounded).is_integer() else rounded


def round_series(payload, kind_of, path=""):
    """Every compact series in payload rounded as kind_of(path) says ("price",
    "volume", "estimate"); the rest as it is."""
    if compact.is_series(payload):
        size = {"price": PRICE_STEP, "volume": VOLUME_STEP, "estimate": ESTIMATE_STEP}[kind_of(path)]
        if size is None:
            return payload
        if "values" in payload:
            return {**payload, "values": [step(value, size) for value in payload["values"]]}
        return {**payload, "days": [None if day is None else [step(value, size) for value in day]
                                    for day in payload.get("days", [])]}
    if isinstance(payload, dict):
        return {key: round_series(value, kind_of, f"{path}/{key}") for key, value in payload.items()}
    return payload


def balancing_kind(path):
    group = path.rsplit("/", 1)[-1]
    if "|cost_" in group:
        return "estimate"
    return "price" if "_price" in group else "volume"


def series_kind(name):
    """How the series of a GME file are rounded, by file."""
    if name.startswith("coupling"):
        return lambda path: "price" if "/prices/" in path else "volume"
    return lambda path: "price"


def read_json(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(payload, path):
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(gzip.compress(text, compresslevel=6, mtime=0) if path.endswith(".gz") else text)


def salt():
    if not os.path.exists(SALT_PATH):
        raise SystemExit(f"{SALT_PATH} missing (the private data's salt for the units' public names)")
    with open(SALT_PATH, encoding="utf-8") as handle:
        return handle.read().strip()


CONSUMPTION_KINDS = ("consumption", "aggregate_withdrawal", "legacy_consumption")


def fallback_source(kind):
    """The source of a unit the database has none for, from its kind:
    virtual import/export units are interconnection, consumption units
    (UC, UVZp) consumption."""
    if kind in ("import", "export"):
        return "interconnection"
    return "consumption" if kind in CONSUMPTION_KINDS else "unknown"


def load_names(day):
    path = os.path.join(NAMES_DIR, f"{day}.json")
    return read_json(path) if os.path.exists(path) else {"codes": {}, "next": {}}


def save_names(day, store):
    os.makedirs(NAMES_DIR, exist_ok=True)
    write_json(store, os.path.join(NAMES_DIR, f"{day}.json"))


def public_units(units, day, sources, secret, store=None):
    """(old index -> new index, the units renamed): no codes or operators.
    store: the day's names ({"codes": code -> name, "next": group -> the
    next number}), changed in place: a unit named before keeps its name, the
    new ones take the next numbers of their group in a random order.  The
    list is sorted by name, so its order says nothing of the codes."""
    store = store if store is not None else {"codes": {}, "next": {}}
    new_by_group, known_units = {}, []
    for index, unit in enumerate(units):
        known = sources.get(unit[0]) or [None, None, None]
        source = known[0] or unit[1] or fallback_source(unit[4])
        group = GROUP_OF.get(source, "other")
        known_units.append((index, source, known[2] or unit[3]))
        if unit[0] not in store["codes"]:
            new_by_group.setdefault(group, []).append(unit[0])
    for group in sorted(new_by_group):
        codes = sorted(set(new_by_group[group]))
        first = store["next"].get(group, 1)
        random.Random(f"{secret}|{day}|{group}|{first}").shuffle(codes)
        for number, code in enumerate(codes, first):
            store["codes"][code] = f"{PUBLIC_NAMES[group]} {number}"
        store["next"][group] = first + len(codes)

    def order(item):
        stem, _, number = store["codes"][units[item[0]][0]].rpartition(" ")
        return stem, int(number) if number.isdigit() else 0

    new_index, renamed = {}, []
    for index, source, zone in sorted(known_units, key=order):
        new_index[index] = len(renamed)
        renamed.append([store["codes"][units[index][0]], source, None, zone, None])
    return new_index, renamed


def public_quarters(quarters, new_index):
    """A market's quarter-hours with the units renumbered (and the numbers
    rounded, with the steps set); the offers sorted, so their order says
    nothing of the units."""
    result = []
    for quarter in quarters:
        flat = quarter["supply"]
        supply = [[new_index[flat[i]], step(flat[i + 1], OFFER_STEP), step(flat[i + 2], PRICE_STEP), flat[i + 3],
                   step(flat[i + 4], OFFER_STEP), step(flat[i + 5], PRICE_STEP), flat[i + 6]]
                  for i in range(0, len(flat), 7)]
        supply.sort(key=lambda record: (record[3], record[2] if record[2] is not None else 0, record[0], record[1]))
        demand = {}
        for i in range(0, len(quarter["demand"]), 3):
            price = step(quarter["demand"][i], PRICE_STEP)
            totals = demand.setdefault(price, [0.0, 0.0])
            totals[0] += quarter["demand"][i + 1]
            totals[1] += quarter["demand"][i + 2]
        public = {
            "time": quarter["time"],
            "prices": {zone: step(price, PRICE_STEP) for zone, price in quarter.get("prices", {}).items()},
            "supply": [value for record in supply for value in record],
            "demand": [value for price, (accepted, rest) in sorted(demand.items(), key=lambda item: -item[0])
                       for value in (price, step(accepted, OFFER_STEP), step(rest, OFFER_STEP))],
        }
        if "purchases" in quarter:
            flat = quarter["purchases"]
            rows = [[new_index[flat[i]], step(flat[i + 1], OFFER_STEP), step(flat[i + 2], PRICE_STEP), flat[i + 3]]
                    for i in range(0, len(flat), 4)]
            rows.sort(key=lambda record: (record[2] if record[2] is not None else 0, record[0], record[1], record[3]))
            public["purchases"] = [value for record in rows for value in record]
        if "period" in quarter:
            public["period"] = quarter["period"]
        if "others" in quarter:
            others = {}
            for i in range(0, len(quarter["others"]), 3):
                key = (new_index[quarter["others"][i]], quarter["others"][i + 1])
                others[key] = others.get(key, 0.0) + quarter["others"][i + 2]
            public["others"] = [value for (unit, status), mw in sorted(others.items())
                                for value in (unit, status, step(mw, OFFER_STEP))]
        elif "status" in quarter:
            public["status"] = {source: {status: step(mw, OFFER_STEP) for status, mw in by_status.items()}
                                for source, by_status in quarter["status"].items()}
        result.append(public)
    return result


def publicise_day(data, sources, secret, store=None):
    """A merit-order day file without GME's numbers or the units' identities:
    an MGP day ("quarters") or an MI-A day (the three auctions' quarters in
    "markets", one unit list: a unit has the same name in all three)."""
    new_index, renamed = public_units(data["units"], data["date"], sources, secret, store)
    public = {"date": data["date"], "market": data.get("market", "MGP"), "source": NOTE, "units": renamed}
    if "markets" in data:
        public["markets"] = {market: public_quarters(quarters, new_index) for market, quarters in data["markets"].items()}
    else:
        public["quarters"] = public_quarters(data["quarters"], new_index)
    return public


def publicise_offers_day(data, sources, secret, store=None):
    """An MSD, MB or MI-XBID day file (fetch_market_offers.py) the same way:
    units renamed and renumbered, the records sorted."""
    new_index, renamed = public_units(data["units"], data["date"], sources, secret, store)

    def records(flat, size, transform, key):
        rows = [transform(flat[i:i + size]) for i in range(0, len(flat), size)]
        rows.sort(key=key)
        return [value for row in rows for value in row]

    def offer(r):
        return [new_index[r[0]], r[1], r[2], step(r[3], OFFER_STEP), step(r[4], PRICE_STEP), r[5], step(r[6], OFFER_STEP), r[7]]

    def event(r):
        return [new_index[r[0]], r[1], r[2]]

    def fill(r):
        return [new_index[r[0]], r[1], step(r[2], OFFER_STEP), step(r[3], PRICE_STEP), r[4]]

    public = {"date": data["date"], "market": data["market"], "source": NOTE, "units": renamed}
    if data["market"] == "XBID":
        for part in ("quarters", "hours"):
            public[part] = [{"time": item["time"],
                             "fills": records(item["fills"], 5, fill, lambda r: (-r[4], r[3], r[1], r[0], r[2]))}
                            for item in data.get(part, [])]
    else:
        public["quarters"] = [{"time": item["time"],
                               "offers": records(item["offers"], 8, offer,
                                                 lambda r: (r[1], r[5], r[4] if r[4] is not None else 0, r[0], r[3])),
                               "events": records(item.get("events", []), 3, event, lambda r: (r[1], r[0]))}
                              for item in data["quarters"]]
    return public


def code_map(directory):
    """The code map (mgp_merit/units.json) for a folder of the private data."""
    for path in (os.path.join(directory, "units.json"),
                 os.path.join(os.path.dirname(os.path.normpath(directory)), "mgp_merit", "units.json")):
        if os.path.exists(path):
            return read_json(path)
    return {}


def publicise_offers(directory, secret):
    """The day files of market_offers/<MARKET>/, in place."""
    sources = code_map(directory)
    count = 0
    for market in sorted(os.listdir(directory)):
        folder = os.path.join(directory, market)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.endswith((".json", ".json.gz")):
                continue
            path = os.path.join(folder, name)
            data = read_json(path)
            store = load_names(data["date"])
            write_json(publicise_offers_day(data, sources, secret, store), path)
            save_names(data["date"], store)
            count += 1
    return count


def publicise_merit(directory, secret):
    """The day files of mgp_merit or mi_merit, in place; the code map
    (mgp_merit/units.json, read for both) is removed."""
    sources = code_map(directory)
    count = 0
    for name in sorted(os.listdir(directory)):
        if name in ("index.json", "units.json") or not name.endswith((".json", ".json.gz")):
            continue
        path = os.path.join(directory, name)
        data = read_json(path)
        store = load_names(data["date"])
        write_json(publicise_day(data, sources, secret, store), path)
        save_names(data["date"], store)
        count += 1
    own = os.path.join(directory, "units.json")
    if os.path.exists(own):
        os.remove(own)
    return count


def publicise(path, secret=None):
    """One file or directory of the private data, in place."""
    name = os.path.basename(os.path.normpath(path))
    if name in ("mgp_merit", "mi_merit"):
        return f"{publicise_merit(path, secret if secret is not None else salt())} day(s)"
    if name == "market_offers":
        return f"{publicise_offers(path, secret if secret is not None else salt())} file(s)"
    if name == "balancing_gme":
        for file in sorted(os.listdir(path)):
            if file.endswith(".json"):
                full = os.path.join(path, file)
                payload = read_json(full)
                payload = {**round_series(payload, balancing_kind), "source": NOTE}
                write_json(payload, full)
        return "balancing series"
    if name in ("gme_units.json", "gme_units_state.json"):
        if os.path.exists(path):
            os.remove(path)
        return "removed (not published)"
    targets = [path] + ([compact.recent_path(path)] if os.path.exists(compact.recent_path(path)) else [])
    for target in targets:
        payload = read_json(target)
        write_json({**round_series(payload, series_kind(name)), "source": NOTE}, target)
    return f"{len(targets)} file(s)"


def new_salt(path=SALT_PATH):
    """The salt of the units' public names, made once (kept in the private data)."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(secrets.token_hex(16) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="private data files or directories (app/data/...), changed in place")
    args = parser.parse_args()
    for path in args.paths:
        print(f"{path}: {publicise(path)}")


if __name__ == "__main__":
    main()
