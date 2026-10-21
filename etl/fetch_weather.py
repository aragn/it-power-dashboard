"""
Fetch weather forecasts for the Italian bidding zones from Open-Meteo and
write app/data/forecast/weather.json, the price forecast's weather inputs
(Forecast tab, etl/forecast_backtest.py):

  hourly, per zone, from 10 March 2024 (ECMWF IFS 0.25 on Open-Meteo's
  Previous Runs API, https://open-meteo.com/en/docs/previous-runs-api):
    <ZONE>|wind_speed|d<k>   100 m wind speed, m/s, at the zone's wind
                             farms, weighted by their MW (on the model's 25 km
                             grid the mean speed tracked the zones' actual wind
                             output better than a turbine power curve, whose
                             rated speed the grid's smoothed winds rarely
                             reach: correlation 0.86-0.88 against 0.72-0.78 in
                             SUD and SICI, 2025-26)
    <ZONE>|ghi|d<k>          global horizontal irradiance, W/m2, at the
                             zone's solar farms, weighted by their MW
    <ZONE>|temperature|d<k>  2 m temperature, C, at the zone's main
                             cities, weighted by their population
  d<k> is the value forecast k days (24k hours) before the hour, k = 0..7
  (Open-Meteo's previous_day<k>; d0 is the latest run's).  The forecast for
  day X made on X-j before the 12:00 auction gate can only use runs up to
  00 UTC of X-j, so it takes d<j> for the hours before 06 UTC of X and
  d<j+1> after (forecast_data.py).

The points (etl/weather_points.json) are made once from the OpenStreetMap
plants of the grid map (app/data/grid/plants.json) and the zones' outlines
(app/geo/bidding_zones.json), and kept, so the series do not move when OSM
changes:  fetch_weather.py --make-points

  fetch_weather.py                  the last 10 days and the next 7
  fetch_weather.py --full-history   from 10 March 2024
  fetch_weather.py --start 20240310 --end 20241231
  fetch_weather.py --full-history --variables wind_speed   one variable only

Open-Meteo's free API is for non-commercial use (open-meteo.com/en/terms);
with OPEN_METEO_API_KEY set the requests go to the paid ("customer-") API.
Data CC BY 4.0, Open-Meteo.com and ECMWF.
"""

import argparse
import json
import math
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

import compact
from entsoe_api import market_today, parse_date, point_label
from zones import DATA_DIR, ZONE_DOMAINS

# ============================================================================
# CONFIGURATION
# ============================================================================

OUTPUT_PATH = os.path.join(DATA_DIR, "forecast", "weather.json")
POINTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_points.json")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ECMWF IFS 0.25 on the Previous Runs API: all of d1..d7 from about this day.
HISTORY_START = date(2024, 3, 10)
MODEL = "ecmwf_ifs025"
OFFSETS = range(0, 8)

# Incremental runs re-read the last days (Open-Meteo fills its archive late)
# and the next days' forecasts.
LOOKBACK_DAYS = 10
FORECAST_DAYS = 7

# A request covers this many days; Open-Meteo counts a call per location and
# per two weeks, so a request of ~15 points and 90 days is ~100 calls of the
# free API's 600 a minute and 10,000 a day.
CHUNK_DAYS = 90
REQUEST_PAUSE_SECONDS = 12
MAX_RETRIES = 5

VARIABLES = {
    # name in the file: (Open-Meteo variable, points, decimals)
    "wind_speed": ("wind_speed_100m", "wind", 1),
    "ghi": ("shortwave_radiation", "solar", 0),
    "temperature": ("temperature_2m", "cities", 1),
}

# Wind points per zone (by its wind capacity) and solar points per zone.
WIND_POINTS = {"NORD": 2, "CNOR": 3, "CSUD": 5, "SUD": 8, "CALA": 5, "SICI": 8, "SARD": 6}
SOLAR_POINTS = {"NORD": 3, "CNOR": 2, "CSUD": 2, "SUD": 2, "CALA": 1, "SICI": 2, "SARD": 1}

# Load centres: the main cities (metropolitan population, millions).
CITIES = {
    "NORD": [("Milano", 45.46, 9.19, 3.2), ("Torino", 45.07, 7.69, 2.2), ("Padova", 45.41, 11.88, 1.7),
             ("Bologna", 44.49, 11.34, 1.0)],
    "CNOR": [("Firenze", 43.77, 11.25, 1.0), ("Ancona", 43.62, 13.52, 0.5)],
    "CSUD": [("Roma", 41.90, 12.50, 4.2), ("Napoli", 40.85, 14.27, 3.0)],
    "SUD": [("Bari", 41.12, 16.87, 1.2), ("Foggia", 41.46, 15.55, 0.6)],
    "CALA": [("Cosenza", 39.30, 16.25, 0.7), ("Reggio Calabria", 38.11, 15.65, 0.5)],
    "SICI": [("Palermo", 38.12, 13.36, 1.2), ("Catania", 37.50, 15.09, 1.1)],
    "SARD": [("Cagliari", 39.22, 9.11, 0.4), ("Sassari", 40.73, 8.56, 0.45)],
}


# ============================================================================
# POINTS (made once)
# ============================================================================


def inside(lon, lat, ring):
    """Ray casting: is the point inside the polygon ring?"""
    result = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            result = not result
    return result


def zone_of(lon, lat, zones):
    for name, polygons in zones.items():
        for polygon in polygons:
            if inside(lon, lat, polygon[0]) and not any(inside(lon, lat, hole) for hole in polygon[1:]):
                return name
    return None


def weighted_kmeans(points, k, iterations=50):
    """Weighted k-means of (lon, lat, weight) points, started from the
    heaviest point and then each time the one farthest (weighted) from the
    centres so far; returns [(lon, lat, weight)]."""
    def dist2(a, b):
        dx = (a[0] - b[0]) * math.cos(math.radians((a[1] + b[1]) / 2))
        return dx * dx + (a[1] - b[1]) ** 2

    centres = [max(points, key=lambda p: p[2])[:2]]
    while len(centres) < min(k, len(points)):
        centres.append(max(points, key=lambda p: p[2] * min(dist2(p, c) for c in centres))[:2])
    for _ in range(iterations):
        groups = defaultdict(list)
        for p in points:
            groups[min(range(len(centres)), key=lambda i: dist2(p, centres[i]))].append(p)
        new = []
        for i in range(len(centres)):
            members = groups.get(i) or [(*centres[i], 0.0)]
            weight = sum(p[2] for p in members) or 1.0
            new.append((sum(p[0] * p[2] for p in members) / weight, sum(p[1] * p[2] for p in members) / weight))
        if new == centres:
            break
        centres = new
    groups = defaultdict(float)
    for p in points:
        groups[min(range(len(centres)), key=lambda i: dist2(p, centres[i]))] += p[2]
    return [(round(c[0], 2), round(c[1], 2), round(groups[i], 1)) for i, c in enumerate(centres) if groups[i] > 0]


def make_points():
    with open(os.path.join(ROOT, "app", "geo", "bidding_zones.json"), encoding="utf-8") as handle:
        geo = json.load(handle)
    zones = {}
    for feature in geo["features"]:
        geometry = feature["geometry"]
        polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        zones[feature["properties"]["name"]] = [[[tuple(c) for c in ring] for ring in polygon] for polygon in polygons]
    with open(os.path.join(DATA_DIR, "grid", "plants.json"), encoding="utf-8") as handle:
        plants = json.load(handle)
    columns = plants["columns"]
    at = {name: columns.index(name) for name in ("lon", "lat", "technology", "mw")}

    by_zone = {"wind": defaultdict(list), "solar": defaultdict(list)}
    for plant in plants["plants"]:
        technology = plant[at["technology"]]
        if technology not in by_zone or not plant[at["mw"]]:
            continue
        zone = zone_of(plant[at["lon"]], plant[at["lat"]], zones)
        if zone:
            by_zone[technology][zone].append((plant[at["lon"]], plant[at["lat"]], plant[at["mw"]]))

    points = {"source": "OpenStreetMap plants (app/data/grid/plants.json), clustered by MW; cities by population",
              "made": date.today().isoformat(), "wind": {}, "solar": {}, "cities": {}}
    for technology, counts in (("wind", WIND_POINTS), ("solar", SOLAR_POINTS)):
        for zone in ZONE_DOMAINS:
            clusters = weighted_kmeans(by_zone[technology][zone], counts[zone])
            points[technology][zone] = [{"lon": lon, "lat": lat, "mw": mw} for lon, lat, mw in clusters]
            total = sum(p[2] for p in by_zone[technology][zone])
            print(f"  {technology} {zone}: {len(by_zone[technology][zone])} plants, {total:,.0f} MW -> "
                  + ", ".join(f"({lat}, {lon}) {mw:,.0f} MW" for lon, lat, mw in clusters))
    for zone, cities in CITIES.items():
        points["cities"][zone] = [{"name": name, "lon": lon, "lat": lat, "population_m": population}
                                  for name, lat, lon, population in cities]
    with open(POINTS_PATH, "w", encoding="utf-8") as handle:
        json.dump(points, handle, indent=1)
    print(f"Wrote {POINTS_PATH}")


# ============================================================================
# DOWNLOAD
# ============================================================================


def api_url():
    key = os.environ.get("OPEN_METEO_API_KEY")
    host = "customer-previous-runs-api.open-meteo.com" if key else "previous-runs-api.open-meteo.com"
    return f"https://{host}/v1/forecast", key


def request_points(locations, variable, start_date, end_date):
    """[(times, {offset: values})] per location, UTC hours."""
    url, key = api_url()
    hourly = [variable] + [f"{variable}_previous_day{k}" for k in OFFSETS if k]
    params = {
        "latitude": ",".join(str(p["lat"]) for p in locations),
        "longitude": ",".join(str(p["lon"]) for p in locations),
        "hourly": ",".join(hourly),
        "models": MODEL,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "timezone": "GMT",
        "wind_speed_unit": "ms",
    }
    if key:
        params["apikey"] = key
    wait = 30
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=180)
        except (requests.ConnectionError, requests.Timeout) as error:
            if attempt >= MAX_RETRIES:
                raise
            print(f"    {type(error).__name__}. Waiting {wait}s...")
            time.sleep(wait)
            wait *= 2
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt >= MAX_RETRIES:
                response.raise_for_status()
            print(f"    HTTP {response.status_code}: {response.text[:120]}. Waiting {wait}s...")
            time.sleep(wait)
            wait *= 2
            continue
        response.raise_for_status()
        payload = response.json()
        payload = payload if isinstance(payload, list) else [payload]
        out = []
        for location in payload:
            data = location["hourly"]
            values = {k: data[f"{variable}_previous_day{k}" if k else variable] for k in OFFSETS}
            out.append((data["time"], values))
        return out
    return []


def to_rows(times, values, variable):
    """{(date, time): value} on the Italian market-time basis.  Irradiance is
    Open-Meteo's average over the hour before its timestamp, so it goes to the
    hour that ends there."""
    shift = timedelta(hours=1) if variable == "shortwave_radiation" else timedelta(0)
    rows = {}
    for stamp, value in zip(times, values):
        if value is None:
            continue
        instant = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc) - shift
        rows[point_label(instant)] = value
    return rows


def download(points, start_date, end_date, names=None):
    """{"<ZONE>|<name>|d<k>": hourly rows}."""
    series = {}
    for name, (variable, group, decimals) in VARIABLES.items():
        if names and name not in names:
            continue
        locations = [(zone, p) for zone, zone_points in points[group].items() for p in zone_points]
        weights = [p.get("mw") or p.get("population_m") for _, p in locations]
        chunk_start = start_date
        while chunk_start <= end_date:
            chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), end_date)
            print(f"  {name} ({variable}, {len(locations)} points): {chunk_start} -> {chunk_end}", flush=True)
            results = request_points([p for _, p in locations], variable, chunk_start, chunk_end)
            # Weighted zone averages, per offset.
            sums = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
            for (zone, _), weight, (times, by_offset) in zip(locations, weights, results):
                for k, values in by_offset.items():
                    for key, value in to_rows(times, values, variable).items():
                        cell = sums[(zone, k)][key]
                        cell[0] += value * weight
                        cell[1] += weight
            for (zone, k), cells in sums.items():
                rows = series.setdefault(f"{zone}|{name}|d{k}", [])
                for (day, label), (total, weight) in cells.items():
                    # Only when every point of the zone has a value.
                    if weight >= sum(w for (z, _), w in zip(locations, weights) if z == zone) - 1e-9:
                        rows.append({"date": day, "time": label, "value": round(total / weight, decimals)})
            chunk_start = chunk_end + timedelta(days=1)
            time.sleep(REQUEST_PAUSE_SECONDS)
    return series


# ============================================================================
# LOAD / SAVE
# ============================================================================


def merge(existing, new):
    merged = {}
    for name in set(existing) | set(new):
        by_key = {(row["date"], row["time"]): row for row in existing.get(name, [])}
        by_key.update({(row["date"], row["time"]): row for row in new.get(name, [])})
        merged[name] = sorted(by_key.values(), key=lambda row: (row["date"], row["time"]))
    return merged


def build_output(series):
    return {
        "source": f"Open-Meteo.com Previous Runs API, ECMWF IFS 0.25 ({MODEL}); CC BY 4.0",
        "description": (
            "Weather forecasts per Italian bidding zone, hourly, Italian market time (labelled by elapsed time "
            "since local midnight): <ZONE>|wind_speed|d<k> = 100 m wind speed (m/s, the zone's wind farms, "
            "MW-weighted), <ZONE>|ghi|d<k> = global horizontal "
            "irradiance (W/m2, the zone's solar farms, MW-weighted), <ZONE>|temperature|d<k> = 2 m temperature "
            "(C, main cities, population-weighted); d<k> = forecast 24k hours before the hour (k = 0..7). "
            "Points: etl/weather_points.json."
        ),
        "hourly": {name: compact.encode_series(rows, "hourly") for name, rows in sorted(series.items()) if rows},
    }


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--full-history", action="store_true", help="Rebuild from 2024-03-10")
    parser.add_argument("--make-points", action="store_true", help="Make etl/weather_points.json from the OSM plants")
    parser.add_argument("--variables", default=None,
                        help=f"comma-separated, of {', '.join(VARIABLES)} (default all); the others are kept as on file")
    args = parser.parse_args()

    if args.make_points:
        make_points()
        return

    with open(POINTS_PATH, encoding="utf-8") as handle:
        points = json.load(handle)

    names = args.variables.split(",") if args.variables else list(VARIABLES)
    existing = {}
    if os.path.exists(OUTPUT_PATH):
        existing = dict(compact.load(OUTPUT_PATH).get("hourly", {}))
    # Series of variables no longer fetched go; with --full-history so do
    # those being fetched again.
    existing = {key: rows for key, rows in existing.items()
                if key.split("|")[1] in VARIABLES and not (args.full_history and key.split("|")[1] in names)}

    today = market_today()
    end_date = parse_date(args.end) if args.end else today + timedelta(days=FORECAST_DAYS)
    if args.start:
        start_date = parse_date(args.start)
    elif args.full_history or not any(key.split("|")[1] in names for key in existing):
        start_date = HISTORY_START
    else:
        start_date = today - timedelta(days=LOOKBACK_DAYS)
    start_date = max(start_date, HISTORY_START)

    series = merge(existing, download(points, start_date, end_date, names))
    compact.dump(build_output(series), OUTPUT_PATH)
    for name in ("NORD|wind_speed|d1", "SUD|wind_speed|d1", "SICI|ghi|d1", "CSUD|temperature|d1"):
        rows = series.get(name, [])
        if rows:
            print(f"  {name}: {len(rows)} hours, {rows[0]['date']} to {rows[-1]['date']}")
    print(f"  {len(series)} series")


if __name__ == "__main__":
    main()
