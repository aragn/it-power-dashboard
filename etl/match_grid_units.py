"""
Match GME's market units to the OpenStreetMap power plants of the grid map:
app/data/grid/plant_units.json.

PRIVATE: it reads GME's unit list (app/data/gme_units.json), so it runs in
the private repository (aragn/it-power-dashboard-private, update-grid-units)
and its file stays on that repository's data branch, never the public one.
The page shows the matches in the full version only.

Inputs: app/data/grid/plants.json (etl/fetch_grid.py, public) and
app/data/gme_units.json.  Production units only (kind "production", UP_
codes): the zonal aggregates (UVZ, UP_DI...) are no single plant.  Units
whose codes share a stem (UP_TURBIGO_3, UP_TURBIGO_4) and zone are one
plant.  Its candidates are the OSM plants of a compatible technology in or
next to its bidding zone; each is scored on

  - the name (name_score): the code's stem against the plant's names.  A
    stem with few vowels is GME's abbreviation of a phrase (TRRVLDLIGA,
    CNTRLDSCND: "CeNTRaLe Di SCaNDale") and may match letter by letter; a
    plain word (EDOLO, LEVANE) only as a word or the start of one.  The
    names our research gave a unit (Monte Grighine wind farm) match on
    their distinctive words;
  - the distance, where the unit has a position (ENTSO-E's municipality,
    sometimes only the province's capital, so a strong name may be up to
    NAME_RADIUS_KM away, a weaker one RADIUS_KM);
  - the capacity (the units' against the plant's, where both are known).

A match needs a name score of NAME_MIN or more, or a plant of the same
technology and a similar capacity within CLOSE_KM (BIG_CLOSE_KM for plants
of BIG_MW or more within 15% of each other).  Several GME plants may
match one OSM plant (sections of one site): one row with all their codes.

Run: python etl/match_grid_units.py
"""

import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(__file__)
DATA = os.path.join(HERE, "..", "app", "data")
PLANTS_PATH = os.path.join(DATA, "grid", "plants.json")
UNITS_PATH = os.path.join(DATA, "gme_units.json")
REGIONS_PATH = os.path.join(HERE, "..", "app", "geo", "italy_regions.json")
OUT_PATH = os.path.join(DATA, "grid", "plant_units.json")

RADIUS_KM = 25
NAME_RADIUS_KM = 60
CLOSE_KM = 4
BIG_MW = 100          # plants this big and this alike in capacity...
BIG_CLOSE_KM = 15     # ...may match on place and capacity this far apart
CAPACITY_MIN = 0.2    # a plant with under a fifth (or five times) the units' capacity is another one
NAME_MIN = 0.7
SCORE_MIN = 0.55

# GME source -> the OSM technologies it can be.
FAMILIES = {
    "gas": {"gas", "oil", "other"}, "coal": {"coal", "gas", "oil", "other"}, "oil": {"oil", "gas", "other"},
    "thermal": {"gas", "coal", "oil", "bioenergy", "other"}, "bioenergy": {"bioenergy", "other"},
    "waste": {"bioenergy", "other"}, "hydro": {"hydro", "pumped_hydro"}, "hydro_reservoir": {"hydro", "pumped_hydro"},
    "hydro_ror": {"hydro"}, "pumped_hydro": {"pumped_hydro", "hydro"}, "wind_onshore": {"wind", "wind_offshore"},
    "solar": {"solar"}, "geothermal": {"geothermal"}, "battery": {"storage"},
}
EXACT = {"gas": "gas", "coal": "coal", "oil": "oil", "hydro_ror": "hydro", "hydro_reservoir": "hydro", "hydro": "hydro",
         "pumped_hydro": "pumped_hydro", "wind_onshore": "wind", "solar": "solar", "geothermal": "geothermal",
         "battery": "storage", "bioenergy": "bioenergy", "waste": "bioenergy"}
# Region -> the bidding zones it may be in (a unit may only match a plant there or next door).
REGION_ZONES = {
    "Piemonte": {"NORD"}, "Valle d'Aosta": {"NORD"}, "Lombardia": {"NORD"}, "Trentino-Alto Adige": {"NORD"},
    "Veneto": {"NORD"}, "Friuli-Venezia Giulia": {"NORD"}, "Liguria": {"NORD"}, "Emilia-Romagna": {"NORD"},
    "Toscana": {"CNOR"}, "Marche": {"CNOR"}, "Umbria": {"CNOR", "CSUD"}, "Lazio": {"CSUD"}, "Abruzzo": {"CSUD"},
    "Campania": {"CSUD"}, "Molise": {"CSUD", "SUD"}, "Puglia": {"SUD"}, "Basilicata": {"SUD"}, "Calabria": {"CALA"},
    "Sicilia": {"SICI"}, "Sardegna": {"SARD"},
}
NEIGHBOUR_ZONES = {"NORD": {"CNOR"}, "CNOR": {"NORD", "CSUD"}, "CSUD": {"CNOR", "SUD"}, "SUD": {"CSUD", "CALA"},
                   "CALA": {"SUD", "SICI"}, "SICI": {"CALA"}, "SARD": set()}
# Words that say nothing about the place.
STOP_WORDS = {
    "CENTRALE", "CENTRALI", "TERMOELETTRICA", "TERMOELETTRICHE", "IDROELETTRICA", "IDROELETTRICO", "ELETTRICA", "IMPIANTO",
    "IMPIANTI", "STABILIMENTO", "POWER", "PLANT", "STATION", "ENERGIA", "ENERGY", "DI", "DEL", "DELLA", "DELLE", "DEI", "DA",
    "E", "IL", "LA", "LO", "LE", "SPA", "S", "P", "A", "SRL", "ENEL", "PRODUZIONE", "GREEN", "EDISON", "A2A", "EP", "TERMICA",
    "FOTOVOLTAICO", "AGRIVOLTAICO", "EOLICO", "EOLICA", "PARCO", "CICLO", "COMBINATO", "CCGT", "TURBOGAS", "NUOVA", "NUOVO",
    "WIND", "FARM", "PARK", "UNIT", "HYDRO", "SOLAR", "PV", "BESS", "STORAGE", "SYSTEM", "SISTEMA", "GROUPING", "NEW",
    "COMBINED", "CYCLE", "THE", "OF", "AND", "SECTION", "GROUP", "GRUPPO", "SEZIONE", "COGENERAZIONE",
    "TELERISCALDAMENTO", "TERMOVALORIZZATORE", "PRESA", "CENTRALINA", "ACQUA", "SOLARE", "KRAFTWERK", "WASSERKRAFTWERK",
}
# Common words of place names: they match only as part of a longer name.
WEAK_WORDS = {
    "MONTE", "MONTI", "SAN", "SANTO", "SANTA", "SANT", "PIANO", "PIAN", "VALLE", "VAL", "CASTEL", "CASTELLO", "PORTO",
    "CAPO", "PONTE", "COLLE", "POGGIO", "TORRE", "LAGO", "PIZZO", "SERRA", "CASA", "MASSERIA", "CONTRADA", "LOCALITA",
    "BOSCO", "FIUME", "ALTO", "ALTA", "BASSO", "NORD", "SUD", "EST", "OVEST", "CENTRO",
}


def normal(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


def stem_of(code):
    """UP_TORVISCOSA_1 -> TORVISCOSA; UP_PREM-GROSIO_1 -> PREMGROSIO; UP_TERMOLI1_1 -> TERMOLI."""
    parts = code.split("_")
    body = parts[1:-1] if len(parts) > 2 and parts[-1].isdigit() else parts[1:]
    return re.sub(r"[^A-Z]", "", "".join(body).upper())


def is_abbreviation(stem):
    """GME's stems drop vowels when they abbreviate a phrase."""
    return sum(char in "AEIOU" for char in stem) / max(1, len(stem)) < 0.35


def subsequence_span(short, long):
    """Positions where the letters of short appear in order in long (earliest), or None."""
    positions, start = [], 0
    for char in short:
        start = long.find(char, start)
        if start < 0:
            return None
        positions.append(start)
        start += 1
    return positions


def name_score(stem, names):
    """0..1: how well a GME stem names one of the plant's names."""
    if len(stem) < 4:
        return 0.0
    best = 0.0
    for name in names:
        words = normal(name).split()
        places = [word for word in words if word not in STOP_WORDS]
        # A name of generic words only ("Centrale termoelettrica Edison") names no place.
        if not places:
            continue
        # Names of common words only (Fiume Santo) count them as distinctive.
        distinctive = [word for word in places if word not in WEAK_WORDS and len(word) >= 3] or places
        full = "".join(words)
        # The stem as a word, a word's start or words run together
        # (SERMIDE, PRESENZAN, FIUMESANT, LACASELL).
        if (stem in distinctive or (len(stem) >= 6 and stem in full)
                or any(len(word) >= 6 and word.startswith(stem) for word in distinctive)
                or any(len(word) >= 5 and stem.startswith(word) for word in distinctive)):
            best = max(best, 1.0)
            continue
        if is_abbreviation(stem):
            best = max(best, abbreviation_score(stem, words, distinctive))
        # A distinctive word sharing its first six letters with the stem.
        if any(len(word) >= 6 and len(stem) >= 6 and word[:6] == stem[:6] for word in distinctive):
            best = max(best, 0.75)
    return best


def abbreviation_score(stem, words, distinctive):
    """GME's abbreviations keep a phrase's letters in order (CNTRLDSCND: CeNTRaLe Di
    SCaNDale), sometimes behind an operator's (NPWRBRNDSI: eNiPoWeR BRiNDiSi): the
    stem, or its end (at least 5 letters), from a word's start, packed tightly
    enough and taking in the start of a distinctive word."""
    full = "".join(words)
    starts, offset = [], 0
    for word in words:
        starts.append(offset)
        offset += len(word)
    distinctive_starts = {start for start, word in zip(starts, words) if word in distinctive}
    best = 0.0
    for drop in range(0, min(4, len(stem) - 6) + 1):
        part = stem[drop:]
        # Without its first letters, what is left must still be a long abbreviation.
        if drop and not is_abbreviation(part):
            continue
        for start in starts:
            if full[start] != part[0]:
                continue
            positions = subsequence_span(part, full[start:])
            if not positions:
                continue
            span = positions[-1] + 1
            density = len(part) / span
            if density < 0.5 or not ({start + p for p in positions} & distinctive_starts):
                continue
            score = (0.9 if density >= 0.65 else 0.8) * (0.85 + 0.15 * len(part) / len(stem))
            best = max(best, score)
    return best


def research_score(research_names, names):
    """Our research's plant names against the plant's: a shared distinctive word."""
    def words(text):
        return {word for word in normal(text).split() if word not in STOP_WORDS and word not in WEAK_WORDS and len(word) >= 5}
    ours = set().union(*(words(name) for name in research_names)) if research_names else set()
    theirs = set().union(*(words(name) for name in names)) if names else set()
    return 0.9 if ours & theirs else 0.0


def km_between(a, b):
    lat = math.radians((a[1] + b[1]) / 2)
    return math.hypot((b[0] - a[0]) * math.cos(lat), b[1] - a[1]) * 111.32


class Regions:
    """Which region a point is in (ISTAT's simplified outlines; points off the
    coast go to the nearest region's centre)."""

    def __init__(self, path):
        with open(path, encoding="utf-8") as handle:
            features = json.load(handle)["features"]
        self.shapes = [(f["properties"]["name"], polygon[0]) for f in features for polygon in f["geometry"]["coordinates"]]
        self.centres = {}
        for name, ring in self.shapes:
            self.centres.setdefault(name, []).extend(ring)
        self.centres = {name: (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
                        for name, ring in self.centres.items()}

    def at(self, lon, lat):
        for name, ring in self.shapes:
            inside = False
            for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
                if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                    inside = not inside
            if inside:
                return name
        return min(self.centres, key=lambda name: km_between((lon, lat), self.centres[name]))


def gme_plants(units):
    """Production units grouped by code stem and zone."""
    groups = defaultdict(list)
    for unit in units:
        if unit["kind"] != "production" or not unit["code"].startswith("UP_"):
            continue
        groups[(stem_of(unit["code"]), unit["zone"])].append(unit)
    plants = []
    for (stem, zone), members in groups.items():
        located = [m for m in members if m.get("lat") is not None and m.get("lon") is not None]
        sources = Counter(m["source"] for m in members if m.get("source"))
        capacities = [m.get("capacity_mw") or m.get("offered_mw") for m in members]
        plants.append({
            "stem": stem, "zone": zone, "codes": sorted(m["code"] for m in members),
            "source": sources.most_common(1)[0][0] if sources else None,
            "mw": round(sum(c for c in capacities if c), 1) if any(capacities) else None,
            "lon": located[0]["lon"] if located else None, "lat": located[0]["lat"] if located else None,
            "names": sorted({m["plant"] for m in members if m.get("plant")}),
        })
    return plants


def match(plant, osm_plants, named):
    """The best OSM plant for a GME plant: (score, method, osm, km) or None.
    named: OSM plants already matched by name, which a match on place and
    capacity alone may not take."""
    family = FAMILIES.get(plant["source"])
    zones = {plant["zone"]} | NEIGHBOUR_ZONES.get(plant["zone"], set())
    best = None
    for osm in osm_plants:
        if family and osm["technology"] not in family:
            continue
        if not (osm["zones"] & zones):
            continue
        distance = None if plant["lon"] is None else km_between((plant["lon"], plant["lat"]), (osm["lon"], osm["lat"]))
        if distance is not None and distance > NAME_RADIUS_KM:
            continue
        names = osm["names"]
        name = max(name_score(plant["stem"], names), research_score(plant["names"], names)) if names else 0.0
        capacity = None
        if plant["mw"] and osm["mw"] and not osm["estimated"]:
            capacity = min(plant["mw"], osm["mw"]) / max(plant["mw"], osm["mw"])
        exact = EXACT.get(plant["source"]) == osm["technology"]
        # Wildly different capacities: another plant of the same name.
        if capacity is not None and capacity < CAPACITY_MIN:
            continue
        if name >= NAME_MIN and (distance is None or distance <= RADIUS_KM or name >= 0.9):
            method = "name"
        elif osm["id"] not in named and distance is not None and exact and capacity and (
                (distance <= CLOSE_KM and capacity >= 0.6)
                or (distance <= BIG_CLOSE_KM and capacity >= 0.85 and min(plant["mw"], osm["mw"]) >= BIG_MW)):
            method = "place and capacity"
        else:
            continue
        near = 0.5 if distance is None else max(0.0, 1 - distance / NAME_RADIUS_KM)
        total = 0.5 * name + 0.25 * near + 0.25 * (capacity if capacity is not None else 0.5) + (0.1 if exact else 0.0)
        if total >= SCORE_MIN and (best is None or total > best[0]):
            best = (total, method, osm, distance)
    return best


def main():
    with open(PLANTS_PATH, encoding="utf-8") as handle:
        plants_file = json.load(handle)
    with open(UNITS_PATH, encoding="utf-8") as handle:
        units_file = json.load(handle)
    regions = Regions(REGIONS_PATH)
    osm_plants = []
    for row in plants_file["plants"]:
        osm = dict(zip(plants_file["columns"], row))
        osm["names"] = [name for name in [osm["name"], *(osm.get("aliases") or "").split("; ")] if name]
        osm["zones"] = REGION_ZONES.get(regions.at(osm["lon"], osm["lat"]), set())
        osm_plants.append(osm)
    units = [dict(zip(units_file["columns"], row)) for row in units_file["units"]]
    merged, unmatched = {}, []
    # Names first; then place and capacity, on the plants no name took.
    results = [(plant, match(plant, osm_plants, set())) for plant in gme_plants(units)]
    named = {best[2]["id"] for _, best in results if best and best[1] == "name"}
    results = [(plant, best if best is None or best[1] == "name" else match(plant, osm_plants, named))
               for plant, best in results]
    for plant, best in results:
        if best is None:
            unmatched.append(plant)
            continue
        total, method, osm, distance = best
        row = merged.get(osm["id"])
        if row:
            # One OSM plant, several GME plants: one row with all the codes.
            row[1] = sorted(set(row[1]) | set(plant["codes"]))
            row[2] = round((row[2] or 0) + (plant["mw"] or 0), 1) or None
            row[6] = max(row[6], round(total, 2))
        else:
            merged[osm["id"]] = [osm["id"], plant["codes"], plant["mw"], plant["zone"], plant["source"], method,
                                 round(total, 2), None if distance is None else round(distance, 1)]
    rows = sorted(merged.values(), key=lambda row: -(row[2] or 0))
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        json.dump({
            "source": "GME market units (app/data/gme_units.json) matched to OpenStreetMap plants (grid/plants.json); PRIVATE",
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "columns": ["plant", "codes", "mw", "zone", "source", "method", "score", "km"],
            "matches": rows,
            "unmatched_columns": ["codes", "mw", "source", "zone"],
            "unmatched": sorted(([p["codes"], p["mw"], p["source"], p["zone"]] for p in unmatched), key=lambda row: -(row[1] or 0)),
        }, handle, ensure_ascii=False, separators=(",", ":"))
    units_matched = sum(len(row[1]) for row in rows)
    matched_mw = sum(row[2] or 0 for row in rows)
    total_mw = matched_mw + sum(p["mw"] or 0 for p in unmatched)
    print(f"Wrote {os.path.relpath(OUT_PATH)}: {units_matched} of {units_matched + sum(len(p['codes']) for p in unmatched)} "
          f"production units matched to {len(rows)} plants, {matched_mw / 1000:.1f} of {total_mw / 1000:.1f} GW; "
          f"by method {dict(Counter(row[5] for row in rows))}")


if __name__ == "__main__":
    main()
