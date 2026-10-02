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

Run: python etl/build_reference.py
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
    build_regions()
    build_municipalities()
