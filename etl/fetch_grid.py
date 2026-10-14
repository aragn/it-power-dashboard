"""
Build the grid map's data from OpenStreetMap and Terna's development plan:

  app/data/grid/lines.json        power lines and cables of 100 kV and more,
                                  in service and under construction
  app/data/grid/substations.json  substations of 220 kV and more
  app/data/grid/plants.json       power plants
  app/data/grid/plans.json        Terna's planned projects: etl/grid_plans.json
                                  (curated: codes, stations, routes) with the
                                  phases and dates of Terna's summary tables
                                  (Excel, linked from its Development Plan
                                  page), see build_plans

OpenStreetMap: Geofabrik's daily extract of Italy (italy-latest.osm.pbf,
about 2 GB, ODbL; it also holds the parts of foreign lines and cables near
the border).  A first pass keeps the objects tagged power (and
construction:power), with the nodes they use, in a small file; a second
reads that with the node locations:

  - lines: power=line and power=cable ways (and those under construction:
    power=construction + construction=line|cable, or construction:power),
    their highest voltage 100 kV or more, busbars and bays left out.  Ways
    with the same tags that meet end to end are joined; the geometry is
    simplified (Douglas-Peucker, SIMPLIFY degrees) and stored as integer
    steps of 1/SCALE degree.  A line with an end outside Italy (see
    etl/neighbours.json) is an interconnector, with the country it goes to.
  - substations: power=substation of 220 kV and more (traction ones left
    out), at their area's centre.
  - plants: power=plant, at their area's centre, with the source, method,
    MW (plant:output:electricity), operator and start date mapped.  Wind
    farms often are only their turbines (power=generator): turbines more
    than WIND_CLUSTER km from any mapped wind plant are grouped into farms.

Run: python etl/fetch_grid.py [--pbf FILE] [--keep] [--plans-only]
  --pbf FILE    read a local extract instead of downloading it
  --keep        keep the download (in --work, default a temporary folder)
  --plans-only  only plans.json (Terna's tables and the curated file)
"""

import argparse
import json
import math
import os
import re
import shutil
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(__file__)
PBF_URL = "https://download.geofabrik.de/europe/italy-latest.osm.pbf"
OUT_DIR = os.path.join(HERE, "..", "app", "data", "grid")
NEIGHBOURS_PATH = os.path.join(HERE, "neighbours.json")
PLANS_PATH = os.path.join(HERE, "grid_plans.json")
USER_AGENT = "it-power-dashboard/1.0 (+https://github.com/aragn/it-power-dashboard)"

MIN_LINE_KV = 100
MIN_SUBSTATION_KV = 220
SIMPLIFY = 0.00025     # degrees, about 25 m
SCALE = 10000          # coordinates stored in steps of 1/SCALE degree (about 10 m)
SEA_LANDING_KM = 8     # a submarine cable ending this close to a foreign coast lands there
WIND_CLUSTER = 1.0     # km: turbines this close are one farm...
WIND_FARM_CELL = 5.0   # km: ...and a group wider than this is cut into cells this wide
SOLAR_MW_PER_KM2 = 50  # a solar farm without a stated output: about 0.5 MW a hectare
TURBINE_MW = 1.5       # a turbine without a stated output (Italy's average is under 2 MW)
LINE_KINDS = {"line", "cable"}
ALIAS_KEYS = ("alt_name", "official_name", "old_name", "short_name", "name:it", "name:en")
SKIPPED_LINES = {"busbar", "bay"}

# OSM plant:source -> the map's technology.
PLANT_SOURCES = {
    "solar": "solar", "wind": "wind", "hydro": "hydro", "geothermal": "geothermal",
    "gas": "gas", "coal": "coal", "oil": "oil", "diesel": "oil", "gasoline": "oil",
    "biomass": "bioenergy", "biogas": "bioenergy", "biofuel": "bioenergy", "waste": "bioenergy",
    "battery": "storage", "nuclear": "other"
}


# ---------------------------------------------------------------- helpers

def download(url, path):
    started = time.time()
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": USER_AGENT}) as response:
        response.raise_for_status()
        with open(path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                handle.write(chunk)
    print(f"Downloaded {url} ({os.path.getsize(path) / 1e9:.2f} GB, {time.time() - started:.0f} s)")


def voltages_kv(value):
    """'380000;132000' -> [380, 132] (OSM gives volts; '380 kV' is read too).
    Voltages under 1 kV are left out."""
    out = []
    for part in re.split(r"[;,/]", value or ""):
        match = re.match(r"\s*(\d+(?:\.\d+)?)\s*(kv)?\s*$", part, re.I)
        if not match:
            continue
        number = float(match.group(1))
        kv = number if match.group(2) else number / 1000
        if kv >= 1:
            out.append(int(round(kv)))
    return out


def megawatts(value):
    """'1200 MW', '1,2 GW', '500 kW', '12.5 MWp' -> MW (None if unreadable)."""
    match = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*([kMG])?W", value or "", re.I)
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    unit = (match.group(2) or "").lower()
    return number * {"k": 0.001, "m": 1, "g": 1000}.get(unit, 0.000001)


def simplify(points, tolerance):
    """Douglas-Peucker on [(lon, lat), ...], without recursion."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        (x1, y1), (x2, y2) = points[first], points[last]
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        farthest, index = -1.0, None
        for position in range(first + 1, last):
            x, y = points[position]
            distance = (abs(dy * x - dx * y + x2 * y1 - y2 * x1) / length if length
                        else math.hypot(x - x1, y - y1))
            if distance > farthest:
                farthest, index = distance, position
        if index is not None and farthest > tolerance:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [point for point, kept in zip(points, keep) if kept]


def encode(points):
    """[(lon, lat), ...] -> [x0, y0, dx1, dy1, ...] in steps of 1/SCALE degree."""
    out, last_x, last_y = [], 0, 0
    for lon, lat in points:
        x, y = round(lon * SCALE), round(lat * SCALE)
        if out and x == last_x and y == last_y:
            continue
        out += [x - last_x, y - last_y]
        last_x, last_y = x, y
    return out


def km_between(a, b):
    lat = math.radians((a[1] + b[1]) / 2)
    return math.hypot((b[0] - a[0]) * math.cos(lat), b[1] - a[1]) * 111.32


def length_km(points):
    return sum(km_between(a, b) for a, b in zip(points, points[1:]))


def ring_centre(rings):
    """Area-weighted centre of the outer rings [[(lon, lat), ...], ...]."""
    total = cx = cy = 0.0
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            cross = x1 * y2 - x2 * y1
            total += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
    if abs(total) < 1e-12:
        points = [point for ring in rings for point in ring]
        return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)
    return cx / (3 * total), cy / (3 * total)


def segment_km(point, a, b):
    """Distance from a point to the segment a-b, in km (flat, fine at these scales)."""
    scale = math.cos(math.radians(point[1]))
    px, py = point[0] * scale, point[1]
    ax, ay, bx, by = a[0] * scale, a[1], b[0] * scale, b[1]
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy) * 111.32


def point_in_ring(x, y, ring):
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


class Countries:
    """Which neighbouring country a point is in (etl/neighbours.json: simplified
    Natural Earth outlines, built by etl/build_reference.py)."""

    def __init__(self, path):
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.shapes = []
        for feature in data["features"]:
            code = feature["properties"]["code"]
            for polygon in feature["geometry"]["coordinates"]:
                ring = [tuple(point) for point in polygon[0]]
                xs, ys = [p[0] for p in ring], [p[1] for p in ring]
                self.shapes.append((code, (min(xs), min(ys), max(xs), max(ys)), ring))

    def at(self, lon, lat):
        for code, (west, south, east, north), ring in self.shapes:
            if west <= lon <= east and south <= lat <= north and point_in_ring(lon, lat, ring):
                return code
        return None

    def near(self, lon, lat, km):
        """The country with the nearest coast, if within km of a point at sea."""
        margin = km / 80
        best, nearest = None, km
        for code, (west, south, east, north), ring in self.shapes:
            if not (west - margin <= lon <= east + margin and south - margin <= lat <= north + margin):
                continue
            for a, b in zip(ring, ring[1:]):
                distance = segment_km((lon, lat), a, b)
                if distance < nearest:
                    best, nearest = code, distance
        return best


def tag(tags, key):
    value = tags.get(key)
    return value.strip() if value else None


# ---------------------------------------------------------------- OSM passes

def is_power_object(tags):
    power = tags.get("power")
    if power in ("line", "cable", "substation", "plant"):
        return True
    if power == "generator":
        return tags.get("generator:source") == "wind"
    if power == "construction":
        return tags.get("construction") in ("line", "cable", "substation", "plant")
    return tags.get("construction:power") in ("line", "cable")


def extract_power(pbf, small):
    """Pass 1: the power objects and the nodes and ways they use, in a small file."""
    import osmium  # pyosmium: only the job that reads OSM needs it
    started = time.time()
    count = 0
    with osmium.BackReferenceWriter(small, ref_src=pbf, overwrite=True) as writer:
        for obj in osmium.FileProcessor(pbf).with_filter(osmium.filter.KeyFilter("power", "construction:power")):
            if is_power_object(obj.tags):
                writer.add(obj)
                count += 1
    print(f"Kept {count} power objects in {os.path.getsize(small) / 1e6:.0f} MB ({time.time() - started:.0f} s)")


def line_status(tags):
    power = tags.get("power")
    if power in LINE_KINDS:
        return "service", power
    if power == "construction" and tags.get("construction") in LINE_KINDS:
        return "construction", tags.get("construction")
    if tags.get("construction:power") in LINE_KINDS:
        return "construction", tags.get("construction:power")
    return None, None


def read_power(small):
    """Pass 2: lines (ways with their node locations), substations, plants and wind turbines."""
    import osmium
    ways, substations, plants, turbines = [], [], [], []
    stats = Counter()
    for obj in osmium.FileProcessor(small).with_areas():
        tags = dict(obj.tags)
        if not tags:
            continue
        if obj.is_way():
            status, kind = line_status(tags)
            if not status or tags.get("line") in SKIPPED_LINES:
                continue
            kv = voltages_kv(tags.get("voltage"))
            if not kv or max(kv) < MIN_LINE_KV:
                stats["line_low_or_no_voltage"] += 1
                continue
            try:
                points = [(node.lon, node.lat) for node in obj.nodes]
                nodes = [node.ref for node in obj.nodes]
            except osmium.InvalidLocationError:
                stats["line_missing_nodes"] += 1
                continue
            ways.append({"id": obj.id, "tags": tags, "status": status, "kind": kind, "kv": kv,
                         "points": points, "first": nodes[0], "last": nodes[-1]})
        elif obj.is_node():
            power = tags.get("power")
            if power == "generator":
                turbines.append({"id": obj.id, "lon": obj.location.lon, "lat": obj.location.lat, "tags": tags})
            elif power in ("substation", "plant") or tags.get("construction") in ("substation", "plant"):
                (substations if "substation" in (power, tags.get("construction")) else plants).append(
                    {"type": "node", "id": obj.id, "lon": obj.location.lon, "lat": obj.location.lat, "tags": tags})
        elif isinstance(obj, osmium.osm.Area):
            power = tags.get("power")
            target = tags.get("construction") if power == "construction" else power
            if target not in ("substation", "plant"):
                continue
            rings = [[(node.lon, node.lat) for node in ring] for ring in obj.outer_rings()]
            if not rings:
                continue
            lon, lat = ring_centre(rings)
            entry = {"type": "way" if obj.from_way() else "relation", "id": obj.orig_id(), "lon": lon, "lat": lat,
                     "tags": tags, "area_km2": area_km2(rings, lat)}
            (substations if target == "substation" else plants).append(entry)
    return ways, substations, plants, turbines, stats


def area_km2(rings, lat):
    total = 0.0
    for ring in rings:
        total += sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:])) / 2
    return abs(total) * 111.32 ** 2 * math.cos(math.radians(lat))


# ---------------------------------------------------------------- lines

def line_signature(way):
    tags = way["tags"]
    return (way["status"], way["kind"], tuple(way["kv"]), tag(tags, "frequency"), tag(tags, "circuits"),
            tag(tags, "name"), tag(tags, "ref"), tag(tags, "operator"), tag(tags, "location"))


def join_ways(ways):
    """Ways with the same tags that meet end to end -> one line (only where
    exactly two ways meet, so branches stay apart)."""
    groups = defaultdict(list)
    for way in ways:
        groups[line_signature(way)].append(way)
    joined = []
    for group in groups.values():
        ends = defaultdict(list)
        for index, way in enumerate(group):
            ends[way["first"]].append(index)
            ends[way["last"]].append(index)
        used = [False] * len(group)
        for start in range(len(group)):
            if used[start]:
                continue
            used[start] = True
            way = group[start]
            points, ids = list(way["points"]), [way["id"]]
            head, tail = way["first"], way["last"]
            for at_tail in (True, False):
                while True:
                    node = tail if at_tail else head
                    others = [index for index in ends[node] if not used[index]]
                    if len(ends[node]) != 2 or len(others) != 1:
                        break
                    other = group[others[0]]
                    used[others[0]] = True
                    ids.append(other["id"])
                    forward = other["points"] if other["first"] == node else other["points"][::-1]
                    far = other["last"] if other["first"] == node else other["first"]
                    if at_tail:
                        points += forward[1:]
                        tail = far
                    else:
                        points = forward[::-1][:-1] + points
                        head = far
            joined.append({**way, "points": points, "ids": ids})
    return joined


def build_lines(ways, countries):
    lines = []
    for line in join_ways(ways):
        tags = line["tags"]
        points = line["points"]
        dc = tag(tags, "frequency") == "0" or tags.get("line") == "dc"
        circuits = tag(tags, "circuits")
        abroad = [countries.at(*points[0]), countries.at(*points[-1])]
        # Lines wholly abroad (the extract's margin) are left out.
        if abroad[0] and abroad[1] and all(countries.at(*point) for point in points[::max(1, len(points) // 8)]):
            continue
        coords = encode(simplify(points, SIMPLIFY))
        # A line shorter than the coordinates' step is a dot: left out.
        if len(coords) < 4:
            continue
        country = abroad[0] or abroad[1]
        # A submarine cable lands on the foreign coast, which may be just off it.
        if not country and tags.get("location") == "underwater":
            country = countries.near(*points[0], SEA_LANDING_KM) or countries.near(*points[-1], SEA_LANDING_KM)
        lines.append([
            line["ids"][0], line["status"], line["kind"], max(line["kv"]), sorted(set(line["kv"]), reverse=True),
            int(circuits) if circuits and circuits.isdigit() else None, dc,
            tag(tags, "name"), tag(tags, "ref"), tag(tags, "operator"), tag(tags, "location"),
            country, round(length_km(points), 1), coords
        ])
    # The rest of an interconnector (its land sections, under the same name)
    # goes to the same country.
    by_name = {row[7]: row[11] for row in lines if row[11] and row[7]}
    for row in lines:
        if not row[11] and row[7] in by_name:
            row[11] = by_name[row[7]]
    lines.sort(key=lambda row: (row[3], row[-2]))
    return lines


# ---------------------------------------------------------------- substations and plants

def build_substations(entries, countries):
    out = []
    for entry in entries:
        tags = entry["tags"]
        kv = voltages_kv(tags.get("voltage"))
        if not kv or max(kv) < MIN_SUBSTATION_KV or tags.get("substation") in ("traction", "minor_distribution"):
            continue
        if countries.at(entry["lon"], entry["lat"]):
            continue
        out.append([f'{entry["type"][0]}{entry["id"]}', round(entry["lon"], 5), round(entry["lat"], 5), max(kv),
                    sorted(set(kv), reverse=True), tag(tags, "substation"), tag(tags, "name"), tag(tags, "operator"),
                    "construction" if tags.get("power") == "construction" else "service"])
    return out


def plant_technology(tags):
    source = (tags.get("plant:source") or tags.get("generator:source") or "").split(";")[0].strip()
    method = tags.get("plant:method") or ""
    if source == "hydro" and "pumped" in method:
        return "pumped_hydro"
    if source == "wind" and tags.get("location") == "offshore":
        return "wind_offshore"
    return PLANT_SOURCES.get(source, "other")


def cluster_turbines(turbines, plants):
    """Turbines far from any mapped wind plant -> farms of turbines closer than WIND_CLUSTER km."""
    wind_plants = [(p["lon"], p["lat"]) for p in plants if plant_technology(p["tags"]) in ("wind", "wind_offshore")]
    cell = 0.05
    grid = defaultdict(list)
    for lon, lat in wind_plants:
        grid[(int(lon / cell), int(lat / cell))].append((lon, lat))

    def near_plant(lon, lat):
        cx, cy = int(lon / cell), int(lat / cell)
        return any(km_between((lon, lat), point) < 3
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1) for point in grid.get((cx + dx, cy + dy), ()))

    loose = [t for t in turbines if not near_plant(t["lon"], t["lat"])]
    # Union-find over a grid of cells about WIND_CLUSTER km wide.
    parent = list(range(len(loose)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    cell = WIND_CLUSTER / 111.0
    cells = defaultdict(list)
    for index, turbine in enumerate(loose):
        cells[(int(turbine["lon"] / cell), int(turbine["lat"] / cell))].append(index)
    for (cx, cy), members in cells.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in cells.get((cx + dx, cy + dy), ()):
                    for index in members:
                        if other > index and km_between((loose[index]["lon"], loose[index]["lat"]),
                                                         (loose[other]["lon"], loose[other]["lat"])) < WIND_CLUSTER:
                            parent[find(index)] = find(other)
    groups = defaultdict(list)
    for index in range(len(loose)):
        groups[find(index)].append(loose[index])
    farms = []
    cell = WIND_FARM_CELL / 111.0
    for members in groups.values():
        lons, lats = [t["lon"] for t in members], [t["lat"] for t in members]
        if km_between((min(lons), min(lats)), (max(lons), max(lats))) <= WIND_FARM_CELL:
            farms.append(members)
            continue
        cells = defaultdict(list)
        for turbine in members:
            cells[(int(turbine["lon"] / cell), int(turbine["lat"] / cell))].append(turbine)
        farms += cells.values()
    out = []
    for members in farms:
        mw = [megawatts(t["tags"].get("generator:output:electricity")) for t in members]
        known = [value for value in mw if value]
        # Turbines without an output count at the farm's known average, or TURBINE_MW.
        average = sum(known) / len(known) if known else TURBINE_MW
        operators = Counter(t["tags"].get("operator") for t in members if t["tags"].get("operator"))
        names = Counter(t["tags"].get("name") for t in members if t["tags"].get("name"))
        out.append({"type": "turbines", "id": min(t["id"] for t in members),
                    "lon": sum(t["lon"] for t in members) / len(members), "lat": sum(t["lat"] for t in members) / len(members),
                    "turbines": len(members),
                    "mw": round(average * len(members), 1), "estimated": len(known) < len(members),
                    "tags": {"plant:source": "wind", **({"operator": operators.most_common(1)[0][0]} if operators else {}),
                             **({"name": names.most_common(1)[0][0]} if names and len(names) == 1 else {})}})
    return out


def build_plants(entries, countries):
    out = []
    seen = set()
    for entry in entries:
        key = (entry["type"], entry["id"])
        if key in seen:
            continue
        seen.add(key)
        tags = entry["tags"]
        if countries.at(entry["lon"], entry["lat"]):
            continue
        technology = plant_technology(tags)
        mw = entry.get("mw")
        estimated = entry.get("estimated", False)
        if mw is None:
            mw = megawatts(tags.get("plant:output:electricity"))
        if mw is None and technology == "solar" and entry.get("area_km2"):
            mw, estimated = entry["area_km2"] * SOLAR_MW_PER_KM2, True
        name = tag(tags, "name")
        # Unknown, unnamed and unsized: nothing to show.
        if technology == "other" and not mw and not name:
            continue
        start = tag(tags, "start_date")
        # Other names, for matching the plants to market units.
        aliases = sorted({tag(tags, key) for key in ALIAS_KEYS if tag(tags, key)} - {name})
        out.append([f'{entry["type"][0]}{entry["id"]}', round(entry["lon"], 5), round(entry["lat"], 5),
                    technology, round(mw, 2) if mw else None, estimated, name, tag(tags, "operator"),
                    tag(tags, "plant:source"), tag(tags, "plant:method"), start[:4] if start and start[:4].isdigit() else None,
                    entry.get("turbines"), tag(tags, "wikidata"),
                    "construction" if tags.get("power") == "construction" else "service", "; ".join(aliases) or None])
    out.sort(key=lambda row: -(row[4] or 0))
    return out


# ---------------------------------------------------------------- plans

# Terna's work phases ("Fase 1".."Fase 5" in its summary tables: planning,
# consultation and/or design, authorisation, executive design, works) and
# the progress report's words for them.
TABLE_PHASES = {1: "planned", 2: "consultation", 3: "authorisation", 4: "authorised", 5: "construction"}
PHASE_ORDER = ["study", "planned", "consultation", "authorisation", "authorised", "construction", "completed"]
TEXT_PHASES = [("valutazione", "study"), ("cancellat", "cancelled"), ("costruzione", "construction"),
               ("realizzazione", "construction"), ("progettazione esecutiva", "authorised"),
               ("autorizzazione", "authorisation"), ("concertazione", "planned"), ("pianificat", "planned"),
               ("completat", "completed")]


def terna_id(value):
    """Terna's intervention code as in its tables, normalised: '723 / E-P' -> '723-E-P',
    '360-N (ex 337-P)' -> '360-N', '554-p' -> '554-P'."""
    text = re.sub(r"\(.*?\)", "", str(value or "")).upper().replace(" ", "")
    text = re.sub(r"/(?=[A-Z])", "-", text)
    return text if re.match(r"^\d+", text) else None


def table_date(name):
    """The date a Terna table describes: a Development Plan's summary (PdS YYYY) is at
    31 December of the year before; its progress report's, at 31 December of YYYY."""
    match = re.search(r"(?:sviluppo|pds)\D{0,4}(20\d\d)", name, re.I)
    if not match:
        return None
    year = int(match.group(1))
    return f"{year}-12-31" if re.search(r"avanzamento", name, re.I) else f"{year - 1}-12-31"


def header_name(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def table_columns(cells):
    """Header cells -> {field: column}, for the fields the map uses."""
    columns = {"shares": {}}
    for index, cell in enumerate(cells):
        phase = re.match(r"fase\s*([1-5])\b", cell)
        if cell in ("identificativo pds", "codice intervento", "codice pds"):
            columns["id"] = index
        elif cell.startswith(("nome intervento", "denominazione intervento")):
            columns["name"] = index
        elif phase:
            columns["shares"][TABLE_PHASES[int(phase.group(1))]] = index
        elif cell.startswith("completato"):
            columns["shares"]["completed"] = index
        elif cell.startswith("fase di avanzamento"):
            columns["phase_text"] = index
        elif cell.startswith("progresso"):
            columns["progress"] = index
        elif cell.startswith(("avvio cantieri", "avvio realizzazione")):
            columns["works_start"] = index
        elif cell.startswith("completamento"):
            columns["completion"] = index
        elif cell.startswith("impatto"):
            columns["capacity_gain"] = index
    return columns if "id" in columns and ("completion" in columns or columns["shares"] or "phase_text" in columns) else None


def text_phase(value):
    """'Completato / In costruzione' -> construction (what is still to do)."""
    found = [phase for part in re.split(r"[/;]", str(value or "").lower())
             for key, phase in TEXT_PHASES if key in part][:3]
    open_phases = [phase for phase in found if phase != "completed"]
    return open_phases[0] if open_phases else (found[0] if found else None)


def share_phase(shares):
    """The phase of most of the main works still to do (the more advanced one on a tie);
    'completed' when all are done."""
    open_shares = {phase: share for phase, share in shares.items() if phase != "completed" and share}
    if not open_shares:
        return "completed" if shares.get("completed") else None
    return max(open_shares, key=lambda phase: (round(open_shares[phase], 6), PHASE_ORDER.index(phase)))


def cell_text(value):
    if value is None or str(value).strip() in ("", "-"):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", re.sub(r"\s*/\s*", "/", str(value).strip()))


def parse_terna_table(content, name):
    """A Terna summary table (Excel) -> {code: facts}."""
    import io
    import warnings
    import openpyxl
    # openpyxl warns about Excel extensions it drops, while it reads the sheets.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        return table_rows(book)


def table_rows(book):
    rows = {}
    for sheet in book.worksheets:
        columns = None
        for row in sheet.iter_rows(values_only=True):
            if columns is None:
                columns = table_columns([header_name(cell) for cell in row])
                continue
            code = terna_id(row[columns["id"]]) if columns["id"] < len(row) else None
            if not code or code in rows:
                continue
            value = lambda field: cell_text(row[columns[field]]) if field in columns and columns[field] < len(row) else None
            shares = {}
            for phase, index in columns["shares"].items():
                try:
                    shares[phase] = float(row[index] or 0)
                except (TypeError, ValueError):
                    shares[phase] = 0.0
            rows[code] = {"name": value("name"), "shares": shares if any(shares.values()) else None,
                          "phase": share_phase(shares) if any(shares.values()) else text_phase(value("phase_text")),
                          "progress": value("progress"), "works_start": value("works_start"),
                          "completion": value("completion"), "capacity_gain": value("capacity_gain")}
    return rows


def terna_tables(plans):
    """Terna's summary tables: the Excel files linked from its Development Plan page
    (a summary table or a progress report's), with those listed in the plans file."""
    from urllib.parse import unquote
    urls = list(plans.get("tables") or [])
    try:
        page = requests.get(plans["plan_page"], timeout=60, headers={"User-Agent": USER_AGENT})
        page.raise_for_status()
        for link in re.findall(r"https://download\.terna\.it/terna/[^\"'\\\s<>]+?\.xlsx?", page.text):
            if link not in urls and re.search(r"sintesi|avanzamento", unquote(link), re.I):
                urls.append(link)
    except requests.RequestException as error:
        print(f"Terna's plan page unavailable ({error}): the tables listed in the plans file only")
    tables = []
    for url in urls:
        name = unquote(url.rsplit("/", 1)[-1])
        as_of = table_date(name)
        try:
            response = requests.get(url, timeout=120, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            rows = parse_terna_table(response.content, name)
        except Exception as error:  # a table that cannot be read leaves the curated facts
            print(f"Terna table {name} unreadable: {error}")
            continue
        if not as_of or not rows:
            print(f"Terna table {name}: no date or no interventions, left out")
            continue
        print(f"Terna table {name}: {len(rows)} interventions at {as_of}")
        tables.append({"name": name, "url": url, "as_of": as_of, "rows": rows})
    return sorted(tables, key=lambda table: table["as_of"], reverse=True)


def first_year(text):
    match = re.search(r"20\d\d", str(text or ""))
    return int(match.group(0)) if match else None


def merge_plan(project, tables, fallback_as_of):
    """A curated project with Terna's newest facts on it: the newest table that has
    it, unless the project's update (the progress report) is newer; the curated
    phase and in-service year where neither has one.  A project that is only part
    of Terna's intervention ("part": its other works may be far behind or ahead)
    never takes the table's phase, and its year only where the curated file has none."""
    out = {key: value for key, value in project.items() if key not in ("update", "phase", "in_service")}
    facts = {key: (project.get(key), fallback_as_of, "curated") for key in ("phase", "in_service")}
    table = next((t for t in tables if any(code in t["rows"] for code in project.get("terna_ids", []))), None)
    table_as_of = None
    if table:
        rows = [table["rows"][code] for code in project["terna_ids"] if code in table["rows"]]
        table_as_of = table["as_of"]
        shares = defaultdict(float)
        for row in rows:
            for phase, share in (row["shares"] or {}).items():
                shares[phase] += share / len(rows)
        phase = share_phase(shares) if shares else next((row["phase"] for row in rows if row["phase"]), None)
        completions = sorted({row["completion"] for row in rows if row["completion"]}, key=lambda text: first_year(text) or 0)
        part = project.get("part")
        # The phase of the whole intervention is not a part's.
        if phase and not part:
            facts["phase"] = (phase, table_as_of, "table")
        if completions and not (part and (project.get("in_service") or "in_service" in (project.get("update") or {}))):
            facts["in_service"] = (" and ".join(completions), table_as_of, "table")
        out["shares"] = {phase: round(share) for phase, share in shares.items() if round(share)} or None
        for key in ("works_start", "capacity_gain", "progress"):
            out[key] = next((row[key] for row in rows if row[key]), None)
        out["table"] = {"name": table["name"], "as_of": table_as_of}
    update = project.get("update")
    if update and (table_as_of is None or update["as_of"] > table_as_of or project.get("part")):
        for key in ("phase", "in_service"):
            if key in update:
                facts[key] = (update[key], update["as_of"], update["source"])
    for key in ("phase", "in_service"):
        out[key], out[f"{key}_as_of"], out[f"{key}_source"] = facts[key]
    out["year"] = first_year(out["in_service"])
    return out


def build_plans():
    """etl/grid_plans.json with Terna's tables read in: app/data/grid/plans.json."""
    with open(PLANS_PATH, encoding="utf-8") as handle:
        plans = json.load(handle)
    tables = terna_tables(plans)
    projects = [merge_plan(project, tables, plans["fallback_as_of"]) for project in plans["projects"]]
    return {
        "description": "Terna's main planned transmission projects (etl/grid_plans.json, etl/fetch_grid.py build_plans)",
        "updated": plans["updated"], "sources": plans["sources"], "phases": plans["phases"],
        "tables": [{"name": t["name"], "url": t["url"], "as_of": t["as_of"], "interventions": len(t["rows"])} for t in tables],
        "projects": projects,
    }


# ---------------------------------------------------------------- main

def write(name, payload):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {os.path.relpath(path)} ({os.path.getsize(path) / 1e6:.2f} MB)")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pbf", help="a local OSM extract of Italy instead of the download")
    parser.add_argument("--work", help="folder for the download and the power file (default: temporary)")
    parser.add_argument("--keep", action="store_true", help="keep the work folder")
    parser.add_argument("--plans-only", action="store_true", help="only Terna's plans (plans.json), no OpenStreetMap")
    args = parser.parse_args()
    if args.plans_only:
        write("plans.json", build_plans())
        return

    work = args.work or tempfile.mkdtemp(prefix="grid-")
    os.makedirs(work, exist_ok=True)
    try:
        pbf = args.pbf or os.path.join(work, "italy-latest.osm.pbf")
        if not args.pbf:
            download(PBF_URL, pbf)
        import osmium
        header = osmium.io.Reader(pbf, osmium.osm.osm_entity_bits.NOTHING).header()
        osm_time = header.get("osmosis_replication_timestamp") or None
        small = os.path.join(work, "power.osm.pbf")
        extract_power(pbf, small)
        ways, substation_entries, plant_entries, turbines, stats = read_power(small)
        print(f"Read {len(ways)} line ways, {len(substation_entries)} substations, {len(plant_entries)} plants, "
              f"{len(turbines)} wind turbines; {dict(stats)}")
    finally:
        if not args.keep and not args.work:
            shutil.rmtree(work, ignore_errors=True)

    countries = Countries(NEIGHBOURS_PATH)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source = "OpenStreetMap contributors (ODbL), Geofabrik extract of Italy"
    lines = build_lines(ways, countries)
    write("lines.json", {
        "source": source, "updated": updated, "osm_timestamp": osm_time, "scale": SCALE,
        "columns": ["id", "status", "kind", "kv", "voltages", "circuits", "dc", "name", "ref", "operator", "location",
                    "country", "km", "coords"],
        "lines": lines})
    write("substations.json", {
        "source": source, "updated": updated, "osm_timestamp": osm_time,
        "columns": ["id", "lon", "lat", "kv", "voltages", "kind", "name", "operator", "status"],
        "substations": build_substations(substation_entries, countries)})
    plants = plant_entries + cluster_turbines(turbines, plant_entries)
    write("plants.json", {
        "source": source, "updated": updated, "osm_timestamp": osm_time,
        "columns": ["id", "lon", "lat", "technology", "mw", "estimated", "name", "operator", "source", "method", "start",
                    "turbines", "wikidata", "status", "aliases"],
        "plants": build_plants(plants, countries)})
    if os.path.exists(PLANS_PATH):
        write("plans.json", build_plans())


if __name__ == "__main__":
    main()
