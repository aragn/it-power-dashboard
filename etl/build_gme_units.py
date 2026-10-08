"""
The GME market-unit database (app/data/gme_units.json): every unit that bid
in the MGP, from the per-unit summaries of fetch_gme_units.py, with

  - what the code says: the kind of unit (UP production, UC consumption,
    UVZi/UVZp zonal aggregates of injection/withdrawal since February 2026,
    UP_DI/UC_DP aggregates until January 2026, UPV/UCV virtual units on the
    foreign zones), the zone, the technology letter of the aggregates;
  - what GME says: the market operator (the most frequent one apart from
    "Bilateralista", GME's name for the schedules of bilateral contracts),
    the largest MW offered in one period (quarter-hour quantities are MW),
    the side (sale, purchase, both), the quantity-weighted offer price,
    the days seen;
  - ENTSO-E's unit lists (gme_units_registry.py; and outage_units.json, the
    units of 15.1): production type, installed MW, voltage, municipality;
  - the curated reference (etl/gme_units_reference.json): what was found by
    research, which wins over everything else;
  - else, for production units, the type inferred from the bids (solar:
    nothing offered at night at about 0 EUR/MWh; storage: buys about as much
    as it sells; ...), marked as inferred.

The virtual units on the foreign zones are grouped per zone and side, and so
are the aggregates whose only operator is "Bilateralista".  Missing values
stay null (shown as a hyphen).
"""

import json
import os
import re
import unicodedata
from collections import Counter, defaultdict

from gme_units_registry import MAX_UNIT_MW, PSR_SOURCES, entsoe_units

HERE = os.path.dirname(os.path.abspath(__file__))
REFERENCE_PATH = os.path.join(HERE, "gme_units_reference.json")
MUNICIPALITIES_PATH = os.path.join(HERE, "municipalities.json")
OUTPUT_PATH = os.path.join(os.path.dirname(HERE), "app", "data", "gme_units.json")
OUTAGE_UNITS_PATH = os.path.join(os.path.dirname(HERE), "app", "data", "outage_units.json")

BILATERAL = "Bilateralista"
ITALIAN_ZONES = ("NORD", "CNOR", "CSUD", "SUD", "CALA", "SICI", "SARD")
FOREIGN_ZONES = {"SVIZ": "Switzerland", "MONT": "Montenegro", "FRAN": "France", "AUST": "Austria",
                 "SLOV": "Slovenia", "GREC": "Greece", "MALT": "Malta", "CORS": "Corsica",
                 "COAC": "Corsica (AC link)", "BSP": "Slovenia (BSP)"}
REGIONS = {
    "01": "Piemonte", "02": "Valle d'Aosta", "03": "Lombardia", "04": "Trentino Alto Adige", "05": "Veneto",
    "06": "Friuli Venezia Giulia", "07": "Liguria", "08": "Emilia Romagna", "09": "Toscana", "10": "Umbria",
    "11": "Marche", "12": "Lazio", "13": "Abruzzo", "14": "Molise", "15": "Campania", "16": "Puglia",
    "17": "Basilicata", "18": "Calabria", "19": "Sicilia", "20": "Sardegna",
}

# Technology letters of the zonal aggregates whose bids leave no doubt;
# the others are shown as their letter.
AGGREGATE_LETTERS = {
    "UVZi": {"C": "solar", "Z": "solar", "K": "battery", "J": "battery", "L": "auxiliaries"},
    "UP_DI": {"C": "solar", "Y": "solar", "F": "battery", "J": "auxiliaries"},
}

# Words in a unit's name that give its type away (the first that matches).
GEOTHERMAL_PLANTS = (r"BAGNORE|PIANCAST|FARINELLO|NUOVA_RAD|NUOVA_SER|NUOVA_GAB|NUOVA_LAR|NUOVA_CAS|NUOVA_LAG|"
                     r"NUOVA_MON|NUOVA_SAS|NUOVA_MOL|N_SMARTIN|CARBOLI|CORNIA|SELVA_|RANCIA|MONTEVERD|VALLE_SEC|"
                     r"LE_PRATA|CHIUSDINO|TRAVALE|LAGONI|SESTA_|PIANACCE|ROTONDO|LARDERELL")
NAME_HINTS = [
    # BATT only as a word of its own (BATTERY, BATT_1): not a place such as Battiggio.
    ("battery", r"BESS|STORAGE|ACCUM|BATTER|BATT(?=_|\d|$)"),
    ("geothermal", GEOTHERMAL_PLANTS + r"|GEOTERM"),
    ("waste", r"TERMOUT|TRMVL|TERMOVAL|WTE|INCENER"),
    ("bioenergy", r"BIOMAS|BMSS|BOMASS|BIOGAS|BIOG_"),
    ("solar", r"SOLAR|FOTOV|AGRIV|AGROV|FTV|SLRPRK|(^|_)(FV|PV)|FV\d*$|PV\d*$|SUN"),
    ("wind_onshore", r"EOLIC|EOLO|WIND|WNDFRM|VENTO|OLICO|IVPC|(^|_)PRCL"),
    ("gas", r"PEAKER|TURBOGAS|CCGT|COGEN|(^|_)CHP|NPWR|(^|_)GTG|(^|_)TG\d*(_|$)|_TG$"),
    # "Centrale termoelettrica": a thermal plant, fuel not stated.
    ("thermal", r"CNTRLTRMLT|TRMLTTRC|TERMOELET"),
]

def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def kind_of(code):
    if re.match(r"UP_DI\d", code):
        return "legacy_production"
    if re.match(r"UC_DP\d", code):
        return "legacy_consumption"
    prefix = code.split("_")[0]
    return {"UP": "production", "UC": "consumption", "UVZi": "aggregate_injection",
            "UVZp": "aggregate_withdrawal", "UPV": "import", "UCV": "export"}.get(prefix, "other")


def top(counter, skip=()):
    counter = Counter({key: value for key, value in (counter or {}).items() if key not in skip and key})
    return counter.most_common(1)[0][0] if counter else None


def largest_mw(purposes):
    return max([value for purpose in purposes.values() for key, value in purpose["max_period"].items()
                if not key.endswith("_day")] or [0]) or None


def features(purposes):
    """Bid features of the sale side: night/midday ratio, share at <= 10 EUR/MWh, fill, purchase/sale ratio."""
    sale, purchase = purposes.get("OFF"), purposes.get("BID")
    if not sale or not sale.get("offered"):
        return None
    hourly = sale["hourly_offered"]
    midday = sum(hourly[10:15]) / 5
    night = (sum(hourly[0:5]) + sum(hourly[21:24])) / 8
    cap = largest_mw({"OFF": sale}) or 0
    periods = sale["days"] * 96
    return {
        "night": night / midday if midday else None,
        "low": sale.get("at_or_below_10", 0) / sale["offered"],
        "fill": sale["offered"] / periods / cap if cap and periods else 0,
        "buys": (purchase or {}).get("offered", 0) / sale["offered"],
        "cap": cap,
    }


def infer_source(code, purposes):
    """
    The production type from the unit's name, else from its bids where they
    leave no doubt: solar (nothing offered at night, the rest at 10 EUR/MWh
    or less) and storage (buys at least 30% of what it sells).  Wind, hydro
    and thermal units overlap in every bid feature (a wind farm can offer at
    150 EUR/MWh, a gas plant under a bilateral contract at 0): no guess.
    """
    body = re.sub(r"_\d+$", "", code[3:])
    for source, pattern in NAME_HINTS:
        if re.search(pattern, body):
            return source, "name"
    f = features(purposes)
    if not f:
        return None, None
    if f["buys"] >= 0.3:
        return ("battery" if f["cap"] < 300 else "pumped_hydro"), "bids"
    if f["night"] is not None and f["night"] < 0.05 and f["low"] >= 0.5:
        return "solar", "bids"
    return None, None


def municipality_lookup():
    data = load_json(MUNICIPALITIES_PATH, {"columns": [], "municipalities": [], "provinces": []})
    columns = data["columns"]
    by_code = {row[columns.index("istat")]: dict(zip(columns, row)) for row in data["municipalities"]}
    provinces = {code: re.sub(r"^(provincia autonoma|provincia|città metropolitana|libero consorzio comunale) di ",
                              "", name, flags=re.I)
                 for code, name in data.get("provinces", {}).items()}
    return by_code, provinces


def normal(name):
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z]", "", text)


def place(location, by_code, provinces):
    """ENTSO-E location RRRPPPCCC -> region, province, municipality, coordinates."""
    if not location or len(location) != 9:
        return {}
    region = REGIONS.get(location[1:3])
    found = by_code.get(location[3:])
    return {"region": region, "province": provinces.get(location[3:6]), "istat": location[3:],
            "municipality": found["name"] if found else None,
            "lat": found["lat"] if found else None, "lon": found["lon"] if found else None}


def build(store, registries, reference=None, last_day=None):
    reference = reference or {}
    researched = reference.get("units", {})
    registry = entsoe_units(registries)
    outage_units = (load_json(OUTAGE_UNITS_PATH, {}) or {}).get("units", {})
    by_code, provinces = municipality_lookup()
    by_name = defaultdict(list)
    for item in by_code.values():
        if item.get("current", True):
            by_name[normal(item["name"])].append(item)
    days = sorted({day for unit in store.values() for day in unit["days"]})
    last_day = last_day or (days[-1] if days else None)

    rows, groups = [], defaultdict(list)
    for code, unit in store.items():
        values = unit.get("values", {})
        purposes = unit.get("purposes", {})
        kind = kind_of(code)
        zone = top(values.get("ZONE_CD"))
        operators = values.get("OPERATORE", {})
        operator = top(operators, skip=(BILATERAL,))
        sides = {purpose for purpose, stats in purposes.items() if stats.get("offered")}
        side = "both" if sides >= {"OFF", "BID"} else "sell" if "OFF" in sides else "buy" if "BID" in sides else None
        offered = [stats for stats in purposes.values() if stats.get("priced")]
        price = (sum(s["price_quantity"] for s in offered) / sum(s["priced"] for s in offered)) if offered else None
        row = {
            "code": code, "codes": None, "kind": kind, "zone": zone, "operator": operator,
            "bilateral_only": operator is None and BILATERAL in operators,
            "offered_mw": largest_mw(purposes), "side": side, "price": round(price, 1) if price is not None else None,
            "first_seen": unit["days"][0] if unit["days"] else None, "last_seen": unit["days"][-1] if unit["days"] else None,
            "days_seen": len(unit["days"]), "tech_code": None, "source": None, "source_origin": None,
            "category": None, "owner": None, "plant": None, "capacity_mw": None, "storage_mwh": None,
            "region": None, "province": None, "municipality": None, "istat": None, "lat": None, "lon": None,
            "voltage_kv": None, "commissioning": None, "decommissioning": None, "eic": None,
            "notes": None, "refs": None,
        }
        if kind in ("aggregate_injection", "legacy_production"):
            family = "UVZi" if kind == "aggregate_injection" else "UP_DI"
            row["tech_code"] = code[-1]
            row["source"] = AGGREGATE_LETTERS[family].get(code[-1])
            row["source_origin"] = "bids" if row["source"] else None
        elif kind in ("aggregate_withdrawal", "legacy_consumption"):
            row["category"] = "retail"
        if kind == "production":
            entry = registry.get(code)
            outage = outage_units.get(code)
            if entry:
                # A capacity of 5 GW or more for one unit is a unit error (kW) or a placeholder.
                if entry["capacity_mw"] and entry["capacity_mw"] >= MAX_UNIT_MW:
                    entry["capacity_mw"] = None
                row.update({"source": entry["source"], "source_origin": "entsoe", "capacity_mw": entry["capacity_mw"],
                            "voltage_kv": entry["voltage_kv"], "eic": entry["eic"]})
                row.update({k: v for k, v in place(entry["location"], by_code, provinces).items() if v is not None})
            if outage:
                if not row["source"] and PSR_SOURCES.get(outage.get("psr")):
                    row.update(source=PSR_SOURCES[outage["psr"]], source_origin="entsoe")
                if not row["capacity_mw"] and outage.get("nominal"):
                    row["capacity_mw"] = outage["nominal"]
            if not row["source"]:
                row["source"], row["source_origin"] = infer_source(code, purposes)
            elif row["source"] == "other":
                # ENTSO-E's "other" (B20) says nothing; a name such as
                # UP_TRINOBESS2_1 does (batteries are listed as B20 there).
                source, origin = infer_source(code, {})
                if source:
                    row["source"], row["source_origin"] = source, origin
        # Research wins.
        extra = researched.get(code)
        if extra:
            for key, value in extra.items():
                if key in row and value not in (None, ""):
                    row[key] = value
            if extra.get("source"):
                row["source_origin"] = "research"
            if extra.get("municipality") and not extra.get("lat"):
                found = by_name.get(normal(extra["municipality"]), [])
                if len(found) == 1:
                    row.update(istat=found[0]["istat"], lat=found[0]["lat"], lon=found[0]["lon"],
                               province=provinces.get(found[0]["istat"][:3], row["province"]))
        if kind in ("import", "export"):
            groups[(kind, zone)].append(row)
        elif row["bilateral_only"] and kind in ("aggregate_injection", "aggregate_withdrawal",
                                                 "legacy_production", "legacy_consumption", "consumption"):
            groups[("bilateral_" + kind, zone)].append(row)
        else:
            rows.append(row)

    for (kind, zone), members in sorted(groups.items()):
        codes = sorted(member["code"] for member in members)
        rows.append({
            **{key: None for key in members[0]},
            "code": f"{len(codes)} codes", "codes": codes, "kind": kind, "zone": zone,
            "operator": None, "operators": sorted({m["operator"] for m in members if m["operator"]}),
            "offered_mw": max((m["offered_mw"] or 0) for m in members) or None,
            "side": "both" if len({m["side"] for m in members}) > 1 else members[0]["side"],
            "first_seen": min(m["first_seen"] for m in members if m["first_seen"]),
            "last_seen": max(m["last_seen"] for m in members if m["last_seen"]),
            "days_seen": max(m["days_seen"] for m in members),
            "source": "interconnection" if kind in ("import", "export") else None,
            "category": "interconnection" if kind in ("import", "export") else
                        ("retail" if "withdrawal" in kind or "consumption" in kind else None),
            "bilateral_only": kind.startswith("bilateral_"),
            "plant": FOREIGN_ZONES.get(zone, zone) if kind in ("import", "export") else None,
        })

    columns = ["code", "codes", "kind", "zone", "operator", "operators", "bilateral_only", "source", "source_origin",
               "tech_code", "category", "owner", "plant", "capacity_mw", "offered_mw", "storage_mwh", "side", "price",
               "region", "province", "municipality", "istat", "lat", "lon", "voltage_kv", "commissioning",
               "decommissioning", "first_seen", "last_seen", "days_seen", "eic", "notes", "refs"]
    rows.sort(key=lambda row: (-(row["capacity_mw"] or row["offered_mw"] or 0), row["code"]))
    return {
        "source": "GME public offers (MGP), ENTSO-E Transparency Platform (14.1.B, production units master data, 15.1), research",
        "description": __doc__.split("\n\n")[0].strip(),
        "days": {"count": len(days), "first": days[0] if days else None, "last": last_day, "list": days},
        "columns": columns,
        "units": [[row.get(column) for column in columns] for row in rows],
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Build the GME market-unit database.")
    parser.add_argument("--store", required=True, help="per-unit summaries (fetch_gme_units.py --out .../gme_units.json)")
    parser.add_argument("--registries", required=True)
    parser.add_argument("--output", default=OUTPUT_PATH)
    args = parser.parse_args()
    store = load_json(args.store)["units"]
    output = build(store, load_json(args.registries, {}), load_json(REFERENCE_PATH, {}))
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, separators=(",", ":"))
    kinds = Counter(row[2] for row in output["units"])
    print(f"Wrote {args.output}: {len(output['units']):,} rows {dict(kinds)}")


if __name__ == "__main__":
    main()
