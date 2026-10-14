"""
Build the static reference files the project map uses (run by hand when
they need refreshing; both are committed):

  app/geo/italy_regions.json   Italy's regions, simplified (Douglas-Peucker)
                               from ISTAT's limits as published by openpolis
                               (github.com/openpolis/geojson-italy, CC BY 4.0)
  etl/municipalities.json      every Italian municipality, current and
                               merged ones, with its ISTAT code and the
                               coordinates Wikidata gives it (CC0); the
                               provinces' ISTAT codes and names
  etl/neighbours.json          the countries around Italy (Natural Earth
                               1:10m, public domain), cut to NEIGHBOUR_BOX:
                               fine, for etl/fetch_grid.py to tell which
                               country a line ends in
  app/geo/neighbours.json      the same, coarse, drawn round Italy on the
                               grid map

Run: python etl/build_reference.py [regions|municipalities|neighbours ...]
"""

import json
import os

import requests

HERE = os.path.dirname(__file__)
REGIONS_URL = "https://raw.githubusercontent.com/openpolis/geojson-italy/master/geojson/limits_IT_regions.geojson"
REGIONS_PATH = os.path.join(HERE, "..", "app", "geo", "italy_regions.json")
MUNICIPALITIES_PATH = os.path.join(HERE, "municipalities.json")
WIKIDATA_URL = "https://query.wikidata.org/sparql"
USER_AGENT = "it-power-dashboard/1.0 (+https://github.com/aragn/it-power-dashboard)"
TOLERANCE = 0.008      # degrees, about 0.8 km
MIN_RING_POINTS = 4    # rings that simplify below this (tiny islets) are dropped
COUNTRIES_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_0_countries.geojson"
NEIGHBOURS_ETL_PATH = os.path.join(HERE, "neighbours.json")
NEIGHBOURS_MAP_PATH = os.path.join(HERE, "..", "app", "geo", "neighbours.json")
NEIGHBOUR_BOX = (2.0, 33.0, 23.0, 49.5)   # west, south, east, north
NEIGHBOUR_TOLERANCE = {"etl": 0.002, "map": 0.01}   # degrees
NOT_NEIGHBOURS = {"IT", "SM", "VA"}       # San Marino and the Vatican are on Italy's grid

MUNICIPALITY_QUERY = """
SELECT ?istat ?name ?coord ?end WHERE {
  ?item wdt:P635 ?istat; wdt:P625 ?coord .
  FILTER(STRLEN(?istat) = 6)
  OPTIONAL { ?item wdt:P576 ?end }
  ?item rdfs:label ?name . FILTER(LANG(?name) = "it")
}"""
PROVINCE_QUERY = """
SELECT ?istat ?name WHERE {
  ?item wdt:P635 ?istat .
  FILTER(STRLEN(?istat) = 3)
  ?item rdfs:label ?name . FILTER(LANG(?name) = "it")
}"""


def simplify(points, tolerance):
    """Douglas-Peucker on a list of [lon, lat]."""
    if len(points) < 3:
        return points
    (x1, y1), (x2, y2) = points[0], points[-1]
    dx, dy = x2 - x1, y2 - y1
    length = (dx * dx + dy * dy) ** 0.5
    farthest, index = -1.0, 0
    for position in range(1, len(points) - 1):
        x, y = points[position]
        distance = (abs(dy * x - dx * y + x2 * y1 - y2 * x1) / length if length
                    else ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5)
        if distance > farthest:
            farthest, index = distance, position
    if farthest <= tolerance:
        return [points[0], points[-1]]
    return simplify(points[:index + 1], tolerance)[:-1] + simplify(points[index:], tolerance)


def simplify_ring(ring, tolerance):
    # Split the closed ring in two so the end points are not the same point.
    half = len(ring) // 2
    out = simplify(ring[:half + 1], tolerance)[:-1] + simplify(ring[half:], tolerance)
    return [[round(x, 3), round(y, 3)] for x, y in out]


def build_regions():
    data = requests.get(REGIONS_URL, timeout=120).json()
    features = []
    for feature in data["features"]:
        geometry = feature["geometry"]
        polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        kept = []
        for polygon in polygons:
            rings = [simplify_ring(ring, TOLERANCE) for ring in polygon]
            if len(rings[0]) >= MIN_RING_POINTS:
                kept.append([ring for ring in rings if len(ring) >= MIN_RING_POINTS])
        features.append({"type": "Feature", "properties": {"name": feature["properties"]["reg_name"].split("/")[0]},
                         "geometry": {"type": "MultiPolygon", "coordinates": kept}})
    os.makedirs(os.path.dirname(REGIONS_PATH), exist_ok=True)
    with open(REGIONS_PATH, "w", encoding="utf-8") as handle:
        json.dump({"type": "FeatureCollection", "source": "ISTAT via openpolis/geojson-italy (CC BY 4.0), simplified",
                   "features": features}, handle, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {REGIONS_PATH} ({os.path.getsize(REGIONS_PATH) // 1024} KB)")


def clip_ring(ring, box):
    """Sutherland-Hodgman: the part of a ring inside the box (west, south, east, north)."""
    west, south, east, north = box
    edges = [(lambda p: p[0] >= west, lambda a, b: (west, a[1] + (b[1] - a[1]) * (west - a[0]) / (b[0] - a[0]))),
             (lambda p: p[0] <= east, lambda a, b: (east, a[1] + (b[1] - a[1]) * (east - a[0]) / (b[0] - a[0]))),
             (lambda p: p[1] >= south, lambda a, b: (a[0] + (b[0] - a[0]) * (south - a[1]) / (b[1] - a[1]), south)),
             (lambda p: p[1] <= north, lambda a, b: (a[0] + (b[0] - a[0]) * (north - a[1]) / (b[1] - a[1]), north))]
    points = [tuple(point) for point in ring]
    for inside, cross in edges:
        if not points:
            break
        out = []
        for index, current in enumerate(points):
            previous = points[index - 1]
            if inside(current):
                if not inside(previous):
                    out.append(cross(previous, current))
                out.append(current)
            elif inside(previous):
                out.append(cross(previous, current))
        points = out
    if points and points[0] != points[-1]:
        points.append(points[0])
    return [list(point) for point in points]


def build_neighbours():
    data = requests.get(COUNTRIES_URL, timeout=300, headers={"User-Agent": USER_AGENT}).json()
    for target, path in (("etl", NEIGHBOURS_ETL_PATH), ("map", NEIGHBOURS_MAP_PATH)):
        tolerance = NEIGHBOUR_TOLERANCE[target]
        digits = 4 if target == "etl" else 3
        features = []
        for feature in data["features"]:
            properties = feature["properties"]
            code = properties.get("ISO_A2_EH") or properties.get("ISO_A2")
            if code in NOT_NEIGHBOURS or not code or code == "-99":
                continue
            geometry = feature["geometry"]
            polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
            kept = []
            for polygon in polygons:
                ring = clip_ring(polygon[0], NEIGHBOUR_BOX)
                if len(ring) < MIN_RING_POINTS:
                    continue
                half = len(ring) // 2
                ring = simplify(ring[:half + 1], tolerance)[:-1] + simplify(ring[half:], tolerance)
                if len(ring) >= MIN_RING_POINTS:
                    kept.append([[[round(x, digits), round(y, digits)] for x, y in ring]])
            if kept:
                features.append({"type": "Feature", "properties": {"name": properties["NAME_EN"], "code": code},
                                 "geometry": {"type": "MultiPolygon", "coordinates": kept}})
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"type": "FeatureCollection", "source": "Natural Earth 1:10m admin 0 countries (public domain), "
                       "outer rings cut to the area round Italy and simplified", "features": features},
                      handle, ensure_ascii=False, separators=(",", ":"))
        print(f"Wrote {path} ({os.path.getsize(path) // 1024} KB): {', '.join(f['properties']['code'] for f in features)}")


def sparql(query):
    response = requests.get(WIKIDATA_URL, params={"query": query}, timeout=180,
                            headers={"Accept": "application/sparql-results+json", "User-Agent": USER_AGENT})
    response.raise_for_status()
    return response.json()["results"]["bindings"]


def build_municipalities():
    rows = {}
    for item in sparql(MUNICIPALITY_QUERY):
        lon, lat = (float(value) for value in item["coord"]["value"].removeprefix("Point(").rstrip(")").split())
        key = (item["istat"]["value"], item["name"]["value"])
        current = "end" not in item
        # One row per code and name: a current entry over a dissolved one.
        if key not in rows or (current and not rows[key][4]):
            rows[key] = [item["istat"]["value"], item["name"]["value"], round(lat, 4), round(lon, 4), current]
    provinces = {item["istat"]["value"]: item["name"]["value"] for item in sparql(PROVINCE_QUERY)}
    with open(MUNICIPALITIES_PATH, "w", encoding="utf-8") as handle:
        json.dump({"source": "Wikidata (CC0): ISTAT code (P635), coordinates (P625), dissolved (P576)",
                   "columns": ["istat", "name", "lat", "lon", "current"],
                   "municipalities": sorted(rows.values()), "provinces": provinces},
                  handle, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {MUNICIPALITIES_PATH} ({os.path.getsize(MUNICIPALITIES_PATH) // 1024} KB): "
          f"{len(rows)} municipalities, {len(provinces)} provinces")


if __name__ == "__main__":
    import sys
    chosen = set(sys.argv[1:]) or {"regions", "municipalities", "neighbours"}
    if "regions" in chosen:
        build_regions()
    if "municipalities" in chosen:
        build_municipalities()
    if "neighbours" in chosen:
        build_neighbours()
