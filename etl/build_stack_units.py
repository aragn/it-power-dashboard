"""
Stack model: the table of the units the stack is built from,
app/data/stack/units.json (GME's unit codes: private data, like the rest of
app/data/stack), from

  - the GME market-unit database (gme_units.json): source, zone, operator,
    MW, ENTSO-E EIC, commissioning;
  - ENTSO-E's outage units (outage_units.json): the units of 100 MW or more
    whose unavailability ENTSO-E publishes, under the same codes;
  - the capacity market (policy/capacity_market.json): the new units awarded,
    matched to codes by plant name and area, with their technology;
  - powerplantmatching's plant list (optional, --ppm powerplants.csv, from
    github.com/PyPSA/powerplantmatching): plant name, technology, CHP and
    commissioning year, matched by ENTSO-E EIC;
  - etl/stack_essential.json: the plants and dispatching users ARERA's
    decisions name as essential for grid security.

Per unit, the prior the stack model starts from: a technology (ccgt, ocgt,
gas_steam, gas_mid, gas_small, coal, oil, ...), an efficiency range (low,
mid, high: net, on the fuel's lower heating value) and the fuel's emission
factor (t CO2 per MWh of fuel).  The model then estimates each unit's
implied efficiency from its offers (STACK_MODEL_PLAN.md, section 4); the
prior only keeps that estimate within reason.

  build_stack_units.py [--ppm powerplants.csv] [--since 2025-07-01]
"""

import argparse
import ast
import csv
import json
import os
import re
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "app", "data")
OUT_PATH = os.path.join(DATA, "stack", "units.json")
ESSENTIAL_PATH = os.path.join(HERE, "stack_essential.json")
CM_PATH = os.path.join(ROOT, "app", "policy", "capacity_market.json")

# The units whose offers follow a fuel cost.
THERMAL = {"gas", "coal", "oil", "thermal"}
# Efficiency ranges (low, mid, high) by technology, net LHV.
EFFICIENCY = {
    "ccgt": (0.48, 0.54, 0.60),
    "ocgt": (0.30, 0.36, 0.42),
    "gas_steam": (0.33, 0.38, 0.42),
    "gas_mid": (0.32, 0.48, 0.58),      # 100-250 MW, technology not known: CCGT, CHP or OCGT
    "gas_small": (0.35, 0.42, 0.48),    # engines and small CHP
    "coal": (0.33, 0.38, 0.43),
    "oil": (0.30, 0.35, 0.40),
    "thermal": (0.30, 0.38, 0.45),
}
# t CO2 per MWh of fuel (LHV).
EMISSIONS = {"gas": 0.202, "coal": 0.341, "oil": 0.279, "thermal": 0.25}
CM_TECH = {"ccgt": "ccgt", "ocgt": "ocgt", "engine": "gas_small"}
PPM_TECH = {"CCGT": "ccgt", "OCGT": "ocgt", "Steam Turbine": "gas_steam"}
CM_AREA_ZONE = {"SARD_N": "SARD", "SARD_S": "SARD"}


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def table(data, columns_key="columns", rows_key="units"):
    columns = data[columns_key]
    return [dict(zip(columns, row)) for row in data[rows_key]]


def code_name(code):
    """The plant part of a code: UP_TAVAZZANO_C_6 -> TAVAZZANOC, its number 6."""
    parts = code.split("_")
    number = parts[-1] if len(parts) > 2 and parts[-1].isdigit() else None
    middle = parts[1:-1] if number else parts[1:]
    return re.sub(r"[^A-Z0-9]", "", "".join(middle).upper()), number


def plant_key(name):
    """A CM plant name: its letters (no spaces, accents dropped) and its unit number, if it ends with one."""
    text = re.sub(r"[^A-Z0-9 ]", "", (name or "").upper().replace("CENTRALE DI", "").replace("CCGT", ""))
    words = text.split()
    number = words.pop() if len(words) > 1 and words[-1].isdigit() else None
    return "".join(words), number


# A CM award's technology -> the unit sources it can be.
CM_SOURCES = {"ccgt": THERMAL, "ocgt": THERMAL, "engine": THERMAL, "thermal": THERMAL | {"bioenergy", "waste"},
              "battery": {"battery"}, "battery_solar": {"battery"}, "hydro": {"hydro", "hydro_reservoir", "hydro_ror"}}


def match_cm(units, cm_units):
    """code -> the CM awards matched to it: same zone, a source the award's
    technology can be, the code's plant part starting with the plant name's
    first 5 letters (or the other way round), and the unit number the same
    when the plant name ends with one."""
    by_zone = defaultdict(list)
    for unit in units:
        by_zone[unit["zone"]].append(unit)
    matched = defaultdict(list)
    for award in cm_units:
        name, number = plant_key(award["plant"])
        sources = CM_SOURCES.get(award["technology"])
        if len(name) < 4 or not sources:
            continue
        zone = CM_AREA_ZONE.get(award["area"], award["area"])
        for unit in by_zone.get(zone, []):
            if unit["source"] not in sources:
                continue
            plant, unit_number = code_name(unit["code"])
            if not (plant.startswith(name[:5]) or (len(plant) >= 5 and name.startswith(plant[:5]))):
                continue
            if number and unit_number != number:
                continue
            matched[unit["code"]].append([award["year"], award["technology"], award["mw"]])
    return matched


def read_ppm(path):
    """EIC -> powerplantmatching's Italian plant row."""
    by_eic = {}
    with open(path, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["Country"] != "Italy" or row["EIC"] in ("", "set()"):
                continue
            try:
                codes = ast.literal_eval(row["EIC"])
            except (ValueError, SyntaxError):
                continue
            for code in codes:
                by_eic[code] = row
    return by_eic


def technology(unit, cm, ppm_row):
    source = unit["source"]
    if source not in THERMAL:
        return source
    if source != "gas":
        return source
    techs = Counter(CM_TECH[award[1]] for award in cm if award[1] in CM_TECH)
    if techs:
        return techs.most_common(1)[0][0]
    if ppm_row and ppm_row.get("Technology") in PPM_TECH:
        return PPM_TECH[ppm_row["Technology"]]
    mw = unit["mw"] or 0
    return "ccgt" if mw >= 250 else "gas_mid" if mw >= 100 else "gas_small"


def build(since, ppm_path=None):
    gme = table(load(os.path.join(DATA, "gme_units.json")))
    outages = load(os.path.join(DATA, "outage_units.json"))["units"]
    cm_data = load(CM_PATH)
    cm_units = table(cm_data, "units_columns", "units")
    essential = load(ESSENTIAL_PATH)
    ppm = read_ppm(ppm_path) if ppm_path else {}

    units = []
    for row in gme:
        if (row["last_seen"] or "") < since or row["side"] not in ("sell", "both"):
            continue
        if row["kind"] in ("import", "export") or "withdrawal" in (row["kind"] or "") or "consumption" in (row["kind"] or ""):
            continue
        outage = outages.get(row["code"])
        mw = max(value for value in (row["capacity_mw"], row["offered_mw"], outage and outage.get("nominal"), 0)
                 if value is not None)
        units.append({"code": row["code"], "kind": row["kind"], "zone": row["zone"], "source": row["source"],
                      "operator": row["operator"], "mw": round(mw, 1) or None, "eic": row["eic"],
                      "commissioning": row["commissioning"], "outage_unit": outage is not None,
                      "outage_psr": outage and outage.get("psr")})

    cm = match_cm(units, cm_units)
    operators_essential = {year: [name.upper() for name in names] for year, names in essential["operators"].items()}
    for unit in units:
        ppm_row = ppm.get(unit["eic"]) if unit["eic"] else None
        awards = cm.get(unit["code"], [])
        tech = technology(unit, awards, ppm_row)
        unit["technology"] = tech
        unit["efficiency"] = list(EFFICIENCY[tech]) if tech in EFFICIENCY else None
        unit["emissions"] = EMISSIONS.get(unit["source"]) if unit["source"] in THERMAL else None
        unit["cm_new"] = awards or None
        unit["essential"] = essential["units"].get(unit["code"])
        operator = (unit["operator"] or "").upper()
        unit["essential_operator"] = [year for year, names in sorted(operators_essential.items())
                                      if any(operator.startswith(name) for name in names)] or None
        unit["ppm"] = ({"name": ppm_row["Name"], "technology": ppm_row["Technology"], "set": ppm_row["Set"],
                        "year": ppm_row["DateIn"] and int(float(ppm_row["DateIn"]))} if ppm_row else None)
        unit["chp"] = True if ppm_row and ppm_row.get("Set") == "CHP" else None

    columns = ["code", "kind", "zone", "source", "technology", "operator", "mw", "eic", "commissioning",
               "outage_unit", "outage_psr", "efficiency", "emissions", "chp", "essential", "essential_operator",
               "cm_new", "ppm"]
    units.sort(key=lambda unit: (unit["source"] or "~", unit["zone"] or "", -(unit["mw"] or 0), unit["code"]))
    return {
        "description": "The stack model's units (etl/build_stack_units.py): every production unit that offered in "
                       f"the MGP since {since}, with the prior the model starts from.",
        "sources": "GME market-unit database; ENTSO-E 15.1 outage units; Terna capacity market reports; "
                   "powerplantmatching (PyPSA, by EIC); ARERA essential-plant decisions",
        "efficiency_note": "efficiency = [low, mid, high], net on the fuel's LHV; emissions = t CO2 per MWh of fuel",
        "columns": columns,
        "units": [[unit[column] for column in columns] for unit in units],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ppm", help="powerplantmatching's powerplants.csv")
    parser.add_argument("--since", default="2025-07-01", help="units seen in the MGP since this day")
    parser.add_argument("--out", default=OUT_PATH)
    args = parser.parse_args()
    data = build(args.since, args.ppm)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
    rows = [dict(zip(data["columns"], row)) for row in data["units"]]
    by_tech = Counter()
    mw_tech = Counter()
    for row in rows:
        by_tech[row["technology"]] += 1
        mw_tech[row["technology"]] += row["mw"] or 0
    print(f"{args.out}: {len(rows)} units")
    for tech, count in by_tech.most_common():
        print(f"  {tech or 'not known':18s} {count:5d} units {mw_tech[tech] / 1000:6.1f} GW")
    thermal = [row for row in rows if row["source"] in THERMAL]
    print(f"  thermal: {sum(1 for row in thermal if row['outage_unit'])} of {len(thermal)} with ENTSO-E outages, "
          f"{sum(1 for row in thermal if row['cm_new'])} with a CM award, "
          f"{sum(1 for row in thermal if row['ppm'])} in powerplantmatching, "
          f"{sum(1 for row in rows if row['essential'])} essential, "
          f"{sum(1 for row in thermal if row['essential_operator'])} of an essential operator")


if __name__ == "__main__":
    main()
