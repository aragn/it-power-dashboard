"""
The physical lines between Italy's bidding zones and to its neighbours, for
the real time grid (Maps > Prices and flows > Real time grid), drawn each
in the colour of its border's physical flow against capacity:
app/data/grid/ties.json.

The list is Terna's (Grid Code Annex A.24, "Individuazione zone di offerta
della rete rilevante", Rev. 06, January 2025), curated in etl/grid_ties.json
with the stations' positions.  Each line's route is OpenStreetMap's
(app/data/grid/lines.json, etl/fetch_grid.py): the shortest chain of mapped
lines of its voltage (direct current with direct current) from one station
to the other, the chain's lines meeting end to end.  Where OSM's lines
stop short (at the edge of the Italy extract, abroad), the rest is drawn
straight to the station ("partly"); where none is found, the whole line is
straight between its stations ("schematic").  Two circuits on the same
towers get the same route.

Run after fetch_grid.py: python etl/build_grid_ties.py
"""

import heapq
import json
import math
import os
from datetime import datetime, timezone

HERE = os.path.dirname(__file__)
GRID_DIR = os.path.join(HERE, "..", "app", "data", "grid")
CURATED_PATH = os.path.join(HERE, "grid_ties.json")
LINES_PATH = os.path.join(GRID_DIR, "lines.json")
SUBSTATIONS_PATH = os.path.join(GRID_DIR, "substations.json")
OUTPUT_PATH = os.path.join(GRID_DIR, "ties.json")

SCALE = 10000
# A chain starts at a line ending this near the first station and ends this near the second.
STATION_KM = 3.0
# Lines meeting end to end: their ends this close.
JOIN_KM = 0.6
MAX_LINES = 12
# A chain that stops short still counts if it gets this much of the way (the rest drawn straight).
PARTLY_SHARE = 0.6


def km(a, b):
    """Distance between two (lon, lat) points, equirectangular."""
    x = (b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot(x, b[1] - a[1]) * 111.2


def decode(coords, scale):
    x = y = 0
    points = []
    for index in range(0, len(coords), 2):
        x += coords[index]
        y += coords[index + 1]
        points.append((x / scale, y / scale))
    return points


def encode(points):
    out, px, py = [], 0, 0
    for lon, lat in points:
        x, y = round(lon * SCALE), round(lat * SCALE)
        if out and x == px and y == py:
            continue
        out += [x - px, y - py]
        px, py = x, y
    return out


def load_lines():
    with open(LINES_PATH, encoding="utf-8") as handle:
        data = json.load(handle)
    columns = data["columns"]
    lines = []
    for row in data["lines"]:
        line = dict(zip(columns, row))
        if line["status"] != "service":
            continue
        line["points"] = decode(line["coords"], data["scale"])
        lines.append(line)
    return lines


def station_positions(stations):
    with open(SUBSTATIONS_PATH, encoding="utf-8") as handle:
        data = json.load(handle)
    by_name = {}
    for row in data["substations"]:
        entry = dict(zip(data["columns"], row))
        if entry.get("name"):
            by_name.setdefault(entry["name"], (entry["lon"], entry["lat"]))
    positions = {}
    for key, spec in stations.items():
        if "pos" in spec:
            positions[key] = tuple(spec["pos"])
        elif spec.get("osm") in by_name:
            positions[key] = by_name[spec["osm"]]
        else:
            raise KeyError(f"Station {key}: OSM substation {spec.get('osm')!r} not found; give its pos")
    return positions


def fits(line, kv, dc):
    """A mapped line that can be part of a cable of kv (380 and 400 kV are the same level)."""
    if bool(line["dc"]) != bool(dc):
        return False
    return any(abs(voltage - kv) <= (25 if kv >= 380 else 10) for voltage in line["voltages"])


def route(lines, start, goal, kv, dc):
    """
    The shortest chain of fitting lines from start towards goal: (points,
    "osm"), or a chain that stops short with the rest straight ("partly"),
    or (None, "schematic").
    """
    candidates = [line for line in lines if fits(line, kv, dc)]
    ends = [(line["points"][0], line["points"][-1]) for line in candidates]
    # (cost, index, reversed, chain) with chain a tuple of (index, reversed)
    heap = []
    for index, (first, last) in enumerate(ends):
        for flipped, near in ((False, first), (True, last)):
            if km(near, start) <= STATION_KM:
                heapq.heappush(heap, (candidates[index]["km"], ((index, flipped),)))
    best_partial, best_left = None, km(start, goal) * (1 - PARTLY_SHARE)
    seen = set()
    while heap:
        cost, chain = heapq.heappop(heap)
        index, flipped = chain[-1]
        if (index, flipped) in seen:
            continue
        seen.add((index, flipped))
        tip = ends[index][0] if flipped else ends[index][1]
        left = km(tip, goal)
        if left <= STATION_KM:
            return points_of(candidates, chain), "osm"
        if left < best_left:
            best_partial, best_left = chain, left
        if len(chain) >= MAX_LINES:
            continue
        used = {step[0] for step in chain}
        for other, (first, last) in enumerate(ends):
            if other in used:
                continue
            for other_flipped, near in ((False, first), (True, last)):
                if km(near, tip) <= JOIN_KM:
                    heapq.heappush(heap, (cost + candidates[other]["km"], chain + ((other, other_flipped),)))
    if best_partial:
        return points_of(candidates, best_partial) + [goal], "partly"
    return None, "schematic"


def points_of(candidates, chain):
    points = []
    for index, flipped in chain:
        part = candidates[index]["points"]
        points += list(reversed(part)) if flipped else list(part)
    return points


def build():
    with open(CURATED_PATH, encoding="utf-8") as handle:
        curated = json.load(handle)
    positions = station_positions(curated["stations"])
    lines = load_lines()
    cables, routes = [], {}
    for spec in curated["cables"]:
        a, b, kv, dc = spec["a"], spec["b"], spec["kv"], bool(spec.get("dc"))
        name = spec.get("name") or f"{a} – {b}"
        key = (a, b, kv, dc)
        if key not in routes:
            points, how = route(lines, positions[a], positions[b], kv, dc)
            if points is None:
                points = [positions[a], positions[b]]
            else:
                points = [positions[a]] + points
            routes[key] = (points, how)
        points, how = routes[key]
        cables.append({"name": name, "kv": kv, "dc": dc, "borders": spec["borders"], "a": a, "b": b,
                       "route": how, "note": spec.get("note"), "coords": encode(points)})
        print(f"  {'/'.join(spec['borders']):20} {name:55} {kv:>3} kV {'DC' if dc else 'AC'}  {how}")
    return {
        "source": curated["source"] + "; routes: OpenStreetMap contributors (ODbL), app/data/grid/lines.json",
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scale": SCALE,
        "cables": cables,
    }


def main():
    payload = build()
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    counts = {how: sum(cable["route"] == how for cable in payload["cables"]) for how in ("osm", "partly", "schematic")}
    print(f"Wrote {OUTPUT_PATH}: {len(payload['cables'])} lines ({counts})")


if __name__ == "__main__":
    main()
