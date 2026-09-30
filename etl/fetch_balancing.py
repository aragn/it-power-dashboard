"""
Fetch the balancing data of the Italian bidding zones and write
app/data/balancing.json plus one bid file per zone and day under
app/data/balancing_bids/<ZONE>/<YYYY-MM-DD>.json.

Time series (15-minute; hourly = average of the quarter-hours; daily =
average for prices and capacities, sum of the hours for energy):

  ENTSO-E (zone = bidding zone, Italy = the Italian LFC area)
    imbalance_price        17.1.G  A85        zone   EUR/MWh (NORD has its own
                                                     price, the other zones share one)
    price_afrr_up/_down    17.1.F  A84 A96    zone   EUR/MWh, local aFRR activation
    price_rr_up/_down      17.1.F  A84 A98    zone   EUR/MWh, RR (MB) activation
    activated_* offered_*  12.3.E  A24        zone   MW activated / offered bids:
                                                     afrr (specific product), rr
    picasso_price_up/_down IF aFRR 3.16 A84 A67  Italy  EUR/MWh, 15-minute mean of the
                                                     4-second cross-border marginal price
    picasso_activated_*    12.3.E  A24 A67    Italy  MW of standard aFRR bids activated
    igcc_import/_export    IF 3.10 B17 A63    Italy  MW netted with the IGCC partners

  Terna (developer.terna.it)
    imbalance_volume       FEES macrozonal imbalance  NORD / SUD macrozone, MW
                           (final, else preliminary); positive = zone long
    fcr_*                  MARKET Aste FCR  area (Continente+Sicilia / Sardegna):
                           price EUR/MW, procured and required MW, up and down
    afrr_requirement       MARKET secondary reserve requirement, area, MW (half band)
    rr_requirement         MARKET replacement reserve requirement, zone, MW
                           (latest MSD session of each quarter-hour)

  GME (api.mercatoelettrico.org)
    msd_price_up/_down     ME_MSDExAnteResults  zone  average price of the accepted
    msd_volume_up/_down                         offers to sell (up) / buy (down), MW

Bids (ENTSO-E 12.3.B&C, A37 B74): every balancing energy bid of a zone for
each quarter-hour, merged by price and sorted in merit order (up: cheapest
first; down: highest price first).  Products: afrr_picasso_* (standard
aFRR product, offered to PICASSO), afrr_* (specific aFRR product, local),
rr_*.  Kept for BID_RETENTION_DAYS.

Italian market time, labelled by elapsed time since local midnight like the
other series.  Credentials: ENTSOE_API_KEY, TERNA_KEY / TERNA_SECRET,
GME_API_LOGIN / GME_API_PASSWORD.
"""

import argparse
import glob
import json
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

import compact
from entsoe_api import (
    API_URL,
    MARKET_TZ,
    day_chunks,
    market_today,
    merge_resolutions,
    parse_date,
    parse_response,
    point_label,
    to_api_datetime,
)
from fetch_outages import IT_DOMAIN, ZONES
from fetch_terna import Client
from fetch_zonal import REQUEST_PAUSE_SECONDS, get_token, market_time_from_period, request_chunk

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "balancing.json")
BIDS_DIR = os.path.join(DATA_DIR, "balancing_bids")

RESOLUTIONS = ("quarter_hourly", "hourly", "daily")

# Incremental runs re-read this many days (corrections, final Terna values).
LOOKBACK_DAYS = 3
BID_RETENTION_DAYS = 35

TERNA_ZONES = {
    "North": "NORD", "Centre-North": "CNOR", "Centre-South": "CSUD", "South": "SUD",
    "Calabria": "CALA", "Sicily": "SICI", "Sardinia": "SARD",
}
MACROZONE = {zone: ("NORD" if zone == "NORD" else "SUD") for zone in ZONES}
AFRR_AREA = {zone: {"SARD": "Sardinia", "SICI": "Sicily"}.get(zone, "Continent") for zone in ZONES}
FCR_AREA = {zone: ("Sardegna" if zone == "SARD" else "Continente+Sicilia") for zone in ZONES}
DIRECTIONS = {"A01": "up", "A02": "down"}

# Series whose daily value is the average (prices, capacities); the others
# are energy (daily sum of the hours, MWh/day).
MEAN_PREFIXES = ("imbalance_price", "price_", "picasso_price", "msd_price", "fcr_", "afrr_requirement",
                 "rr_requirement", "offered_")

ENTSOE_PAUSE_SECONDS = 0.5


# ============================================================================
# ENTSO-E
# ============================================================================


def entsoe_get(token, params):
    """Parsed XML root, or None for "no matching data"."""
    for attempt in range(6):
        response = requests.get(API_URL, params={**params, "securityToken": token}, timeout=180)
        if response.status_code == 429 or response.status_code >= 500:
            wait = 20 * (attempt + 1)
            print(f"    HTTP {response.status_code}, waiting {wait}s")
            time.sleep(wait)
            continue
        time.sleep(ENTSOE_PAUSE_SECONDS)
        if response.status_code == 400 and "No matching data" in response.text:
            return None
        response.raise_for_status()
        root = parse_response(response.content)
        if root.tag.endswith("Acknowledgement_MarketDocument"):
            return None
        return root
    raise RuntimeError(f"ENTSO-E request kept failing: {params.get('documentType')}")


def _text(elem, tag):
    child = elem.find(f"{{*}}{tag}")
    return child.text if child is not None else None


def _instant(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


RESOLUTION_SECONDS = {"PT4S": 4, "PT1M": 60, "PT15M": 900, "PT30M": 1800, "PT60M": 3600}


def expand_points(ts, value_of):
    """
    (UTC start of slot, slot seconds, value) of a TimeSeries.  curveType A03
    lists a point only where the value changes: it holds until the next
    listed position.  value_of(point) may return None (no value there).
    """
    compressed = _text(ts, "curveType") == "A03"
    for period in ts.findall("{*}Period"):
        interval = period.find("{*}timeInterval")
        start, end = _instant(_text(interval, "start")), _instant(_text(interval, "end"))
        step = RESOLUTION_SECONDS[_text(period, "resolution")]
        positions = int((end - start).total_seconds() // step)
        points = sorted((int(_text(p, "position")), value_of(p)) for p in period.findall("{*}Point"))
        for i, (position, value) in enumerate(points):
            last = (points[i + 1][0] - 1 if i + 1 < len(points) else positions) if compressed else position
            if value is None:
                continue
            for pos in range(position, last + 1):
                yield start + timedelta(seconds=step * (pos - 1)), step, value


def quarter_records(group, slots, scale=1.0):
    """Point records for merge_resolutions from {UTC quarter start: value}."""
    records = []
    for instant, value in slots.items():
        market_date, label = point_label(instant)
        records.append({"group": group, "date": market_date, "time": label, "minutes": 15,
                        "value": round(value * scale, 3)})
    return records


def number(point, tag):
    value = _text(point, tag)
    return float(value) if value not in (None, "") else None


def imbalance_prices(token, zone, eic, start, end):
    """17.1.G: the zone's single imbalance price (category A04; A05 only fills gaps)."""
    root = entsoe_get(token, {"documentType": "A85", "controlArea_Domain": eic, "periodStart": start, "periodEnd": end})
    by_category = defaultdict(dict)
    for ts in (root.findall(".//{*}TimeSeries") if root is not None else []):
        for instant, _, value in expand_points(ts, lambda p: (_text(p, "imbalance_Price.category"),
                                                               number(p, "imbalance_Price.amount"))):
            category, price = value
            if price is not None:
                by_category[category][instant] = price
    prices = dict(by_category.get("A04", {}))
    for instant, price in by_category.get("A05", {}).items():
        if instant not in prices and price:
            prices[instant] = price
    return quarter_records(f"{zone}|imbalance_price", prices)


def activation_prices(token, zone, eic, start, end):
    """17.1.F: prices of activated aFRR (A96) and RR (A98); 0 = nothing activated."""
    records = []
    for business, product in (("A96", "afrr"), ("A98", "rr")):
        root = entsoe_get(token, {"documentType": "A84", "processType": "A16", "businessType": business,
                                  "controlArea_Domain": eic, "periodStart": start, "periodEnd": end})
        for ts in (root.findall(".//{*}TimeSeries") if root is not None else []):
            direction = DIRECTIONS.get(_text(ts, "flowDirection.direction"))
            slots = {instant: value for instant, _, value in expand_points(ts, lambda p: number(p, "activation_Price.amount"))
                     if value}
            records += quarter_records(f"{zone}|price_{product}_{direction}", slots)
    return records


def aggregated_bids(token, area, eic, process, product, start, end):
    """12.3.E: offered (quantity) and activated (secondaryQuantity) MW per direction."""
    root = entsoe_get(token, {"documentType": "A24", "processType": process, "area_Domain": eic,
                              "curveType": "A03", "periodStart": start, "periodEnd": end})
    records = []
    for ts in (root.findall(".//{*}TimeSeries") if root is not None else []):
        direction = DIRECTIONS.get(_text(ts, "flowDirection.direction"))
        standard = _text(ts, "standard_MarketProduct.marketProductType") == "A01"
        # Per zone, only the specific (local) aFRR product is split out;
        # standard-product activations are published for Italy as a whole.
        if process == "A51" and standard:
            continue
        for kind, tag in (("offered", "quantity"), ("activated", "secondaryQuantity")):
            slots = {instant: value for instant, _, value in expand_points(ts, lambda p, t=tag: number(p, t))}
            records += quarter_records(f"{area}|{kind}_{product}_{direction}", slots)
    return records


def picasso_prices(token, day):
    """IF aFRR 3.16: 15-minute mean of Italy's 4-second cross-border marginal prices."""
    start, end = to_api_datetime(day), to_api_datetime(day, end_of_day=True)
    root = entsoe_get(token, {"documentType": "A84", "processType": "A67", "businessType": "A96",
                              "Standard_MarketProduct": "A01", "controlArea_Domain": IT_DOMAIN,
                              "periodStart": start, "periodEnd": end})
    records = []
    for ts in (root.findall(".//{*}TimeSeries") if root is not None else []):
        direction = DIRECTIONS.get(_text(ts, "flowDirection.direction"))
        sums = defaultdict(lambda: [0.0, 0])
        for instant, _, value in expand_points(ts, lambda p: number(p, "activation_Price.amount")):
            quarter = instant.replace(minute=instant.minute // 15 * 15, second=0)
            sums[quarter][0] += value
            sums[quarter][1] += 1
        records += quarter_records(f"IT|picasso_price_{direction}", {q: s / n for q, (s, n) in sums.items()})
    return records


def igcc_netting(token, day):
    """IF 3.10: MWh per quarter-hour netted between Italy and the IGCC partners, as MW."""
    start, end = to_api_datetime(day), to_api_datetime(day, end_of_day=True)
    root = entsoe_get(token, {"documentType": "B17", "processType": "A63", "Acquiring_Domain": IT_DOMAIN,
                              "Connecting_Domain": IT_DOMAIN, "periodStart": start, "periodEnd": end})
    records = []
    for ts in (root.findall(".//{*}TimeSeries") if root is not None else []):
        name = "igcc_import" if _text(ts, "acquiring_Domain.mRID") == IT_DOMAIN else "igcc_export"
        slots = {instant: value for instant, _, value in expand_points(ts, lambda p: number(p, "quantity"))}
        records += quarter_records(f"IT|{name}", slots, scale=4)
    return records


def fetch_entsoe(start_day, end_day):
    token = os.environ["ENTSOE_API_KEY"]
    start, end = to_api_datetime(start_day), to_api_datetime(end_day, end_of_day=True)
    records = []
    for zone, eic in ZONES.items():
        print(f"  ENTSO-E {zone}")
        records += imbalance_prices(token, zone, eic, start, end)
        records += activation_prices(token, zone, eic, start, end)
        records += aggregated_bids(token, zone, eic, "A51", "afrr", start, end)
        records += aggregated_bids(token, zone, eic, "A46", "rr", start, end)
    print("  ENTSO-E Italy: PICASSO and IGCC")
    records += aggregated_bids(token, "IT", IT_DOMAIN, "A67", "picasso", start, end)
    day = start_day
    while day <= end_day:
        records += picasso_prices(token, day)
        records += igcc_netting(token, day)
        day += timedelta(days=1)
    return records


# ============================================================================
# ENTSO-E BIDS (MARKET DEPTH)
# ============================================================================


BID_PROCESSES = (("A51", "afrr"), ("A46", "rr"))


def bid_product(ts, product):
    direction = DIRECTIONS.get(_text(ts, "flowDirection.direction"))
    if product == "afrr" and _text(ts, "standard_MarketProduct.marketProductType") == "A01":
        return f"afrr_picasso_{direction}"
    return f"{product}_{direction}"


def merit_curves(bids):
    """{product: {time: [price, MW, price, MW, ...]}} in merit order, equal prices merged."""
    curves = {}
    for product, by_time in bids.items():
        descending = product.endswith("_down")
        curves[product] = {}
        for label, pairs in sorted(by_time.items()):
            merged = defaultdict(float)
            for price, quantity in pairs:
                merged[round(price, 2)] += quantity
            flat = []
            for price in sorted(merged, reverse=descending):
                flat += [compact._pack_number(price), compact._pack_number(round(merged[price], 3))]
            curves[product][label] = flat
    return curves


def fetch_bids(token, zone, eic, day):
    """Every bid of one zone and market day, paged by 100 TimeSeries."""
    start, end = to_api_datetime(day), to_api_datetime(day, end_of_day=True)
    bids = defaultdict(lambda: defaultdict(list))
    for process, product in BID_PROCESSES:
        for offset in range(0, 100_000, 100):
            root = entsoe_get(token, {"documentType": "A37", "businessType": "B74", "processType": process,
                                      "connecting_Domain": eic, "periodStart": start, "periodEnd": end,
                                      "offset": offset})
            series = root.findall(".//{*}Bid_TimeSeries") if root is not None else []
            for ts in series:
                key = bid_product(ts, product)
                for instant, _, (quantity, price) in expand_points(
                        ts, lambda p: (number(p, "quantity.quantity"), number(p, "energy_Price.amount"))):
                    if quantity is None or price is None or quantity <= 0:
                        continue
                    market_date, label = point_label(instant)
                    if market_date == day.isoformat():
                        bids[key][label].append((price, quantity))
            if len(series) < 100:
                break
    return merit_curves(bids)


def bid_path(zone, day):
    return os.path.join(BIDS_DIR, zone, f"{day.isoformat()}.json")


def update_bids(start_day, end_day):
    token = os.environ["ENTSOE_API_KEY"]
    day = start_day
    while day <= end_day:
        for zone, eic in ZONES.items():
            curves = fetch_bids(token, zone, eic, day)
            count = sum(len(v) // 2 for by_time in curves.values() for v in by_time.values())
            print(f"  bids {zone} {day}: {count} price steps")
            if not count:
                continue
            path = bid_path(zone, day)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"zone": zone, "date": day.isoformat(), "products": curves}, f, separators=(",", ":"))
        day += timedelta(days=1)


def prune_bids(today):
    cutoff = (today - timedelta(days=BID_RETENTION_DAYS)).isoformat()
    for path in glob.glob(os.path.join(BIDS_DIR, "*", "*.json")):
        if os.path.basename(path)[:10] < cutoff:
            os.remove(path)


def bid_days():
    days = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(BIDS_DIR, "*", "*.json"))):
        days[os.path.basename(os.path.dirname(path))].append(os.path.basename(path)[:10])
    return dict(days)


# ============================================================================
# TERNA
# ============================================================================


def local_records(rows, time_field, group_of, value_of, scale=1.0):
    """
    Point records from Terna rows labelled in local time.  Rows with an
    "offset" field are exact; without it the second occurrence of a local
    time is the repeated hour of the autumn DST change.
    """
    seen = set()
    records = []
    for row in rows:
        group, value = group_of(row), value_of(row)
        if group is None or value in (None, "", "null"):
            continue
        local = datetime.strptime(row[time_field][:19], "%Y-%m-%d %H:%M:%S")
        if row.get("offset"):
            sign = 1 if row["offset"].startswith("+") else -1
            hours, minutes = (int(part) for part in row["offset"][1:].split(":"))
            instant = (local - sign * timedelta(hours=hours, minutes=minutes)).replace(tzinfo=timezone.utc)
        else:
            fold = int((group, local) in seen)
            seen.add((group, local))
            instant = local.replace(tzinfo=MARKET_TZ, fold=fold).astimezone(timezone.utc)
        market_date, label = point_label(instant)
        records.append({"group": group, "date": market_date, "time": label, "minutes": 15,
                        "value": round(float(value) * scale, 3)})
    return records


def latest_session(records):
    """Requirements are restated by each MSD session: keep the last value per slot."""
    latest = {}
    for record in records:
        latest[(record["group"], record["date"], record["time"])] = record
    return list(latest.values())


def terna_day(client, path, key, day, params=None):
    stamp = day.strftime("%d/%m/%Y")
    payload = client.get(path, {"dateFrom": stamp, "dateTo": stamp, **(params or {})})
    return payload.get(key) or []


def fetch_terna(start_day, end_day):
    client = Client(os.environ["TERNA_KEY"], os.environ["TERNA_SECRET"])
    records = []
    day = start_day
    while day <= end_day:
        print(f"  Terna {day}")
        # Macrozonal imbalance: final (D+1 17:00), else preliminary.
        rows = terna_day(client, "/fees/v1.0/daily-macrozonal-imbalance", "daily_macrozonal_imbalance", day)
        if not rows:
            rows = terna_day(client, "/fees/v1.0/preliminary-macrozonal-imbalance",
                             "preliminary_macrozonal_imbalance", day)
        rows = [row for row in rows if row.get("data_type", "Quarto Orario") == "Quarto Orario"]
        records += local_records(rows, "reference_date", lambda r: f"{r['macrozone']}|imbalance_volume",
                                 lambda r: r.get("zonal_aggregate_unbalance_MWh"), scale=4)

        fcr = client.get("/market/v1.0/aste-fcr", {"marketDate": day.strftime("%d/%m/%Y")}).get("aste_fcr") or []
        for field, name in (("price", "price"), ("quantity", "procured"), ("requirement", "requirement")):
            records += local_records(
                fcr, "date", lambda r, n=name: f"{r['zone']}|fcr_{n}_{r['direction'].lower()}",
                lambda r, f=field: r.get(f))
        day += timedelta(days=1)

    # Reserve requirements: one request per MSD session for the whole range.
    dates = {"dateFrom": start_day.strftime("%d/%m/%Y"), "dateTo": end_day.strftime("%d/%m/%Y")}
    for path, key, name, zone_of in (
            ("/market/v1.0/input/afrr-requirement", "secondary_reserve_requirement", "afrr_requirement",
             lambda z: z),
            ("/market/v1.0/input/rr-requirement", "replacement_reserve_requirement", "rr_requirement",
             lambda z: TERNA_ZONES.get(z))):
        session_records = []
        for session in ("MSD1", "MSD2", "MSD3", "MSD4", "MSD5", "MSD6"):
            try:
                rows = client.get(path, {**dates, "sessionType": session}).get(key) or []
            except requests.HTTPError as error:
                print(f"    {name} {session}: {error}")
                continue
            session_records += local_records(
                rows, "reference_date", lambda r, n=name, zo=zone_of: (f"{zo(r['zone'])}|{n}" if zo(r["zone"]) else None),
                lambda r: r.get("requirement_MW"))
        records += latest_session(session_records)
    return records


# ============================================================================
# GME
# ============================================================================


def gme_number(value):
    if value in (None, "", "null"):
        return None
    return float(value)


def fetch_gme(start_day, end_day):
    token = get_token(os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"])
    rows = []
    for chunk_start, chunk_end in day_chunks(start_day, end_day, 7):
        print(f"  GME MSD ex-ante {chunk_start} -> {chunk_end}")
        rows += request_chunk(token, chunk_start, chunk_end, None, "MSD", "ME_MSDExAnteResults")
        time.sleep(REQUEST_PAUSE_SECONDS)
    records = []
    for row in rows:
        zone = str(row.get("Zone") or "").upper()
        if zone not in ZONES:
            continue
        flow_date = datetime.strptime(str(row["FlowDate"]), "%Y%m%d").date().isoformat()
        label = market_time_from_period(int(row["Period"]), 15)
        for field, name, scale in (("VolumesSold", "msd_volume_up", 4), ("VolumesPurchased", "msd_volume_down", 4),
                                   ("AverageSellingPrice", "msd_price_up", 1),
                                   ("AveragePurchasingPrice", "msd_price_down", 1)):
            value = gme_number(row.get(field))
            # No accepted offers: no price.
            if value is None or (name.startswith("msd_price") and not value):
                continue
            records.append({"group": f"{zone}|{name}", "date": flow_date, "time": label, "minutes": 15,
                            "value": round(value * scale, 3)})
    return records


# ============================================================================
# LOAD / MERGE / SAVE
# ============================================================================


def is_mean(group):
    return group.split("|", 1)[1].startswith(MEAN_PREFIXES)


def load_existing():
    if not os.path.exists(OUTPUT_PATH):
        return {resolution: {} for resolution in RESOLUTIONS}
    payload = compact.load(OUTPUT_PATH)
    return {resolution: dict(payload.get("series", {}).get(resolution, {})) for resolution in RESOLUTIONS}


def merge(existing, records):
    """merge_resolutions per daily rule (mean or sum)."""
    merged = {resolution: dict(existing[resolution]) for resolution in RESOLUTIONS}
    for mean in (True, False):
        subset = [record for record in records if is_mean(record["group"]) == mean]
        if not subset:
            continue
        groups = {record["group"] for record in subset}
        current = {resolution: {g: rows for g, rows in existing[resolution].items() if g in groups}
                   for resolution in RESOLUTIONS}
        result = merge_resolutions(current, subset, daily="mean" if mean else "sum")
        for resolution in RESOLUTIONS:
            merged[resolution].update(result[resolution])
    return merged


def build_output(series):
    return {
        "source": "ENTSO-E Transparency Platform, Terna public API, GME API",
        "description": (
            "Balancing data per Italian bidding zone ('ZONE|series'; 'IT|' = Italy as a whole, "
            "'NORD|'/'SUD|' imbalance volume = macrozone, FCR and aFRR requirement = Terna area). "
            "Prices EUR/MWh (FCR EUR/MW), volumes MW; hourly = average of the quarter-hours, "
            "daily = average for prices and capacities, sum of the hours (MWh/day) for energy. "
            "Italian market time, labelled by elapsed time since local midnight."
        ),
        "areas": {"macrozone": MACROZONE, "afrr": AFRR_AREA, "fcr": FCR_AREA},
        "bid_days": bid_days(),
        "series": {
            resolution: {group: compact.encode_series(rows, resolution)
                         for group, rows in sorted(series[resolution].items())}
            for resolution in RESOLUTIONS
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--start", help="First market day (YYYY-MM-DD); default: LOOKBACK_DAYS ago")
    parser.add_argument("--end", help="Last market day (YYYY-MM-DD); default: tomorrow")
    parser.add_argument("--sources", default="entsoe,terna,gme,bids",
                        help="Comma-separated subset of entsoe,terna,gme,bids")
    args = parser.parse_args()

    today = market_today()
    start_day = parse_date(args.start) if args.start else today - timedelta(days=LOOKBACK_DAYS)
    end_day = parse_date(args.end) if args.end else today + timedelta(days=1)
    sources = set(args.sources.split(","))
    print(f"Balancing data {start_day} -> {end_day} ({', '.join(sorted(sources))})")

    records = []
    for name, fetch, last in (("entsoe", fetch_entsoe, min(end_day, today)),
                              ("terna", fetch_terna, end_day),
                              ("gme", fetch_gme, end_day)):
        if name not in sources:
            continue
        try:
            records += fetch(start_day, last)
        except Exception as error:  # one source failing must not lose the others
            print(f"  {name} failed: {error!r}")
    if "bids" in sources:
        # Bids are not revised once published: routine runs fetch only
        # yesterday (its last hours) and today.
        bid_start = start_day if args.start else today - timedelta(days=1)
        update_bids(bid_start, min(end_day, today))
        prune_bids(today)

    series = merge(load_existing(), records)
    compact.dump(build_output(series), OUTPUT_PATH)
    groups = sorted(series["quarter_hourly"])
    print(f"Wrote {OUTPUT_PATH}: {len(groups)} series, bids for {sum(len(v) for v in bid_days().values())} zone-days")


if __name__ == "__main__":
    main()
