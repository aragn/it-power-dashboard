"""
Fetch the balancing data of the Italian bidding zones and write one file
per zone, app/data/balancing/<ZONE>.json (the zone's series and those of
its Terna areas; the dashboard loads it when the zone is opened), one for
Italy as a whole (IT.json, with the area series), and one bid file per
zone and day under app/data/balancing_bids/<ZONE>/<YYYY-MM-DD>.json.

Time series (15-minute; hourly = average of the quarter-hours; daily =
average for prices and capacities, sum of the hours for energy; hourly and
daily activation prices are weighted by the energy activated):

  ENTSO-E (zone = bidding zone, Italy = the Italian LFC area)
    imbalance_price        17.1.G  A85          zone  EUR/MWh (NORD has its own
                                                      price, the other zones share one)
    price_afrr_up/_down    17.1.F  A84 A16 A96  zone  EUR/MWh, specific (local) aFRR product
    price_picasso_up/_down 17.1.F  A84 A68 A96  zone  EUR/MWh, standard aFRR product
                                                      (the bids offered to PICASSO)
    price_rr_up/_down      17.1.F  A84 A16 A98  zone  EUR/MWh, RR (MB) activation
    activated_*            12.3.E  A24          zone  MW activated: afrr (specific, A51),
                                                      picasso (standard, A68), rr (A46)
    picasso_price_up/_down IF aFRR 3.16 A84 A67 Italy  EUR/MWh, 15-minute mean of the
                                                      4-second cross-border marginal price
    activated_central_*    12.3.E  A24 A67    Italy  PICASSO central selection for the
                                                     Italian area ("activated" can exceed
                                                     the Italian standard bids offered:
                                                     not Italian bids alone); not shown
    igcc_import/_export    IF 3.10 B17 A63    Italy  MW netted with the IGCC partners

    Italy publishes each zone's standard-product (PICASSO) activations under
    processType A68, "local selection"; the numbers match balancing.services.

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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests

import compact
from entsoe_api import (
    API_URL,
    MARKET_TZ,
    day_chunks,
    label_minutes,
    local_midnight_utc,
    market_today,
    merge_resolutions,
    minutes_label,
    parse_date,
    parse_response,
    point_label,
    to_api_datetime,
)
from fetch_outages import IT_DOMAIN, ZONES
from fetch_terna import Client
from fetch_zonal import REQUEST_PAUSE_SECONDS, get_token, market_time_from_period, request_chunk

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "data")
BALANCING_DIR = os.path.join(DATA_DIR, "balancing")
LEGACY_PATH = os.path.join(DATA_DIR, "balancing.json")  # before the split into zone files
BIDS_DIR = os.path.join(DATA_DIR, "balancing_bids")

RESOLUTIONS = ("quarter_hourly", "hourly", "daily")

# Incremental runs re-read this many days (corrections, final Terna values).
LOOKBACK_DAYS = 3

# The history starts here; routine runs fill Terna's gaps before the
# lookback window (see main): Terna allows 300 calls a day per key (reset
# at midnight UTC), which the routine runs share with the catch-up unless a
# second key is set. With the second key a run goes on, block after block,
# until it has used TERNA_CATCH_UP_CALLS (a block costs up to ~70 calls),
# so the first run of each UTC day uses that day's quota however many of
# the scheduled runs GitHub starts.
HISTORY_START = date(2025, 1, 1)
TERNA_CATCH_UP_DAYS = 28
TERNA_CATCH_UP_CALLS = 220
TERNA_CATCH_UP_DAYS_SHARED_KEY = 7
BID_RETENTION_DAYS = 35

# Request sizes: ENTSO-E allows a year per request (the PICASSO prices one
# day), Terna 60 days, GME is paced per request.
ENTSOE_CHUNK_DAYS = 90
REQUIREMENT_CHUNK_DAYS = 30
GME_CHUNK_DAYS = 31
# ENTSO-E allows 400 requests a minute per token.
PARALLEL_REQUESTS = 4

# Terna answers 403 "Developer Over Rate" when the day's quota is used up
# (stop at once), other 403s now and then (wait, then stop).
TERNA_QUOTA_MESSAGE = "Over Rate"
TERNA_REFUSAL_RETRIES = 3
TERNA_REFUSAL_WAIT_SECONDS = 120

# Terna's FCR auctions started on 3 June 2026 (Allegato A.83 of the grid
# code); earlier days return zeros.
FCR_START = date(2026, 6, 1)

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
                 "rr_requirement")

# Series no longer written: Italy's central selection used to be stored as
# *_picasso_* (now *_central_*), and the offered volumes are not shown.
RETIRED_GROUPS = ("IT|activated_picasso_", "IT|offered_picasso_")

ENTSOE_PAUSE_SECONDS = 0.5


def market_days(start_day, end_day):
    day = start_day
    while day <= end_day:
        yield day
        day += timedelta(days=1)


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


def _series(root, tag="TimeSeries"):
    return root.findall(f".//{{*}}{tag}") if root is not None else []


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


def point_records(group, points, scale=1.0, energy=False):
    """
    Records for merge_resolutions from (UTC slot start, slot seconds, value)
    points; an hourly slot covers its four quarter-hours.  energy: the value
    is MWh per slot, stored as MW.
    """
    records = []
    for instant, seconds, value in points:
        market_date, label = point_label(instant)
        factor = scale * (3600 / seconds if energy else 1)
        records.append({"group": group, "date": market_date, "time": label,
                        "minutes": max(15, seconds // 60), "value": round(value * factor, 3)})
    return records


def number(point, tag):
    value = _text(point, tag)
    return float(value) if value not in (None, "") else None


def imbalance_prices(token, zone, eic, start, end):
    """17.1.G: the zone's single imbalance price (category A04; A05 only fills gaps)."""
    root = entsoe_get(token, {"documentType": "A85", "controlArea_Domain": eic, "periodStart": start, "periodEnd": end})
    by_category = defaultdict(dict)
    for ts in _series(root):
        for instant, seconds, (category, price) in expand_points(
                ts, lambda p: (_text(p, "imbalance_Price.category"), number(p, "imbalance_Price.amount"))):
            if price is not None:
                by_category[category][instant] = (seconds, price)
    prices = dict(by_category.get("A04", {}))
    for instant, (seconds, price) in by_category.get("A05", {}).items():
        if instant not in prices and price:
            prices[instant] = (seconds, price)
    return point_records(f"{zone}|imbalance_price",
                         ((instant, seconds, price) for instant, (seconds, price) in prices.items()))


# (processType, businessType, product) of the zone activation prices.  The
# standard aFRR product (PICASSO) was published with the specific one (A16)
# until late June 2026 and under A68 since: A68 comes later, so its values
# win where both exist.
ACTIVATION_PRICES = (("A16", "A96", "afrr"), ("A68", "A96", "picasso"), ("A16", "A98", "rr"))


def standard_product(ts):
    return _text(ts, "standard_MarketProduct.marketProductType") == "A01"


def activation_prices(token, zone, eic, start, end):
    """17.1.F: prices of the activated aFRR (specific, standard) and RR energy."""
    records = []
    for process, business, product in ACTIVATION_PRICES:
        root = entsoe_get(token, {"documentType": "A84", "processType": process, "businessType": business,
                                  "controlArea_Domain": eic, "periodStart": start, "periodEnd": end})
        for ts in _series(root):
            direction = DIRECTIONS.get(_text(ts, "flowDirection.direction"))
            if product == "afrr" and standard_product(ts):
                product_of_ts = "picasso"
            else:
                product_of_ts = product
            records += point_records(f"{zone}|price_{product_of_ts}_{direction}",
                                     expand_points(ts, lambda p: number(p, "activation_Price.amount")))
    return records


def drop_idle_prices(records):
    """
    Activation prices read 0 in the quarter-hours without activation: keep a
    0 only where the matching volume (activated_* of the same product,
    direction and zone) was activated.
    """
    active = {(record["group"].replace("|activated_", "|price_"), record["date"], record["time"])
              for record in records if "|activated_" in record["group"] and record["value"]}
    return [record for record in records
            if not ("|price_" in record["group"] and record["value"] == 0
                    and (record["group"], record["date"], record["time"]) not in active)]


def activated_volumes(token, area, eic, process, start, end):
    """
    12.3.E: activated MW (secondaryQuantity) per product and direction.  The
    standard aFRR product (PICASSO) was published in the aFRR document
    (A51) until late June 2026 and under A68 since (A51 then reads 0):
    A68 is fetched after A51, so its values win where both exist.
    """
    root = entsoe_get(token, {"documentType": "A24", "processType": process, "area_Domain": eic,
                              "curveType": "A03", "periodStart": start, "periodEnd": end})
    records = []
    for ts in _series(root):
        direction = DIRECTIONS.get(_text(ts, "flowDirection.direction"))
        product = {"A51": "picasso" if standard_product(ts) else "afrr", "A68": "picasso", "A46": "rr",
                   "A67": "central"}[process]
        records += point_records(f"{area}|activated_{product}_{direction}",
                                 expand_points(ts, lambda p: number(p, "secondaryQuantity")))
    return records


def picasso_prices(token, day):
    """
    IF aFRR 3.16: 15-minute mean of Italy's 4-second cross-border marginal
    prices.  At most 24 hours per request, so the 25-hour day of the
    autumn clock change takes two.
    """
    start = local_midnight_utc(day)
    end = local_midnight_utc(day + timedelta(days=1))
    roots = []
    while start < end:
        stop = min(start + timedelta(hours=24), end)
        roots.append(entsoe_get(token, {"documentType": "A84", "processType": "A67", "businessType": "A96",
                                        "Standard_MarketProduct": "A01", "controlArea_Domain": IT_DOMAIN,
                                        "periodStart": start.strftime("%Y%m%d%H%M"),
                                        "periodEnd": stop.strftime("%Y%m%d%H%M")}))
        start = stop
    records = []
    for ts in (ts for root in roots for ts in _series(root)):
        direction = DIRECTIONS.get(_text(ts, "flowDirection.direction"))
        sums = defaultdict(lambda: [0.0, 0])
        for instant, _, value in expand_points(ts, lambda p: number(p, "activation_Price.amount")):
            quarter = instant.replace(minute=instant.minute // 15 * 15, second=0)
            sums[quarter][0] += value
            sums[quarter][1] += 1
        records += point_records(f"IT|picasso_price_{direction}",
                                 ((quarter, 900, total / count) for quarter, (total, count) in sums.items()))
    return records


def igcc_netting(token, start, end):
    """IF 3.10: energy netted between Italy and the IGCC partners, as MW."""
    root = entsoe_get(token, {"documentType": "B17", "processType": "A63", "Acquiring_Domain": IT_DOMAIN,
                              "Connecting_Domain": IT_DOMAIN, "periodStart": start, "periodEnd": end})
    records = []
    for ts in _series(root):
        name = "igcc_import" if _text(ts, "acquiring_Domain.mRID") == IT_DOMAIN else "igcc_export"
        records += point_records(f"IT|{name}", expand_points(ts, lambda p: number(p, "quantity")), energy=True)
    return records


def imbalance_volume(token, start, end):
    """
    17.1.H: Italy's total imbalance, MWh per quarter-hour as MW; the
    direction A01 (surplus) above zero, A02 (deficit) below.
    """
    root = entsoe_get(token, {"documentType": "A86", "controlArea_Domain": IT_DOMAIN,
                              "periodStart": start, "periodEnd": end})
    records = []
    for ts in _series(root):
        sign = -1 if _text(ts, "flowDirection.direction") == "A02" else 1
        records += point_records("IT|imbalance_volume_total", expand_points(ts, lambda p: number(p, "quantity")),
                                 scale=sign, energy=True)
    return records


def zone_entsoe(token, zone, eic, start, end):
    records = imbalance_prices(token, zone, eic, start, end)
    records += activation_prices(token, zone, eic, start, end)
    for process in ("A51", "A68", "A46"):
        records += activated_volumes(token, zone, eic, process, start, end)
    return records


def logged(what, fetch, *args):
    """fetch(*args), or no records (with a log line) if it fails: one bad
    request must not lose the rest of a long download."""
    try:
        return fetch(*args)
    except Exception as error:
        print(f"    {what} failed: {error!r}")
        return []


def fetch_entsoe(start_day, end_day):
    token = os.environ["ENTSOE_API_KEY"]
    records = []
    with ThreadPoolExecutor(PARALLEL_REQUESTS) as pool:
        for chunk_start, chunk_end in day_chunks(start_day, end_day, ENTSOE_CHUNK_DAYS):
            print(f"  ENTSO-E {chunk_start} -> {chunk_end}")
            start, end = to_api_datetime(chunk_start), to_api_datetime(chunk_end, end_of_day=True)
            for zone_records in pool.map(
                    lambda item: logged(f"{item[0]} {chunk_start}", zone_entsoe, token, item[0], item[1], start, end),
                    ZONES.items()):
                records += zone_records
            records += logged(f"IT central selection {chunk_start}", activated_volumes,
                              token, "IT", IT_DOMAIN, "A67", start, end)
            records += logged(f"IGCC {chunk_start}", igcc_netting, token, start, end)
            records += logged(f"Italy imbalance volume {chunk_start}", imbalance_volume, token, start, end)
        print("  ENTSO-E PICASSO cross-border marginal prices, one day per request")
        for day_records in pool.map(lambda day: logged(f"PICASSO prices {day}", picasso_prices, token, day),
                                    market_days(start_day, end_day)):
            records += day_records
    return drop_idle_prices(records)


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
            series = _series(root, "Bid_TimeSeries")
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

    def run(task):
        zone, eic, day = task
        curves = fetch_bids(token, zone, eic, day)
        count = sum(len(v) // 2 for by_time in curves.values() for v in by_time.values())
        if count:
            path = bid_path(zone, day)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"zone": zone, "date": day.isoformat(), "products": curves}, f, separators=(",", ":"))
        return zone, day, count

    tasks = [(zone, eic, day) for day in market_days(start_day, end_day) for zone, eic in ZONES.items()]
    with ThreadPoolExecutor(PARALLEL_REQUESTS) as pool:
        for zone, day, count in pool.map(run, tasks):
            print(f"  bids {zone} {day}: {count} price steps")


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


def local_records(rows, time_field, group_of, value_of, scale=1.0, minutes=15):
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
            hours, offset_minutes = (int(part) for part in row["offset"][1:].split(":"))
            instant = (local - sign * timedelta(hours=hours, minutes=offset_minutes)).replace(tzinfo=timezone.utc)
        else:
            fold = int((group, local) in seen)
            seen.add((group, local))
            instant = local.replace(tzinfo=MARKET_TZ, fold=fold).astimezone(timezone.utc)
        market_date, label = point_label(instant)
        records.append({"group": group, "date": market_date, "time": label, "minutes": minutes,
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


def imbalance_records(rows):
    """Macrozonal imbalance in MW: 15-minute values, or the hourly ones where only those exist."""
    quarter = [row for row in rows if row.get("data_type", "Quarto Orario") == "Quarto Orario"]
    hourly = [row for row in rows if row.get("data_type") == "Orario"]
    group_of = lambda row: f"{row['macrozone']}|imbalance_volume"  # noqa: E731
    value_of = lambda row: row.get("zonal_aggregate_unbalance_MWh")  # noqa: E731
    if quarter:
        return local_records(quarter, "reference_date", group_of, value_of, scale=4)
    return local_records(hourly, "reference_date", group_of, value_of, minutes=60)


class TernaRefused(Exception):
    """Terna keeps answering 403 (a request quota, not the per-second limit)."""


def terna_get(client, path, params):
    """
    client.get, counting the calls in client.calls; a 403 is waited out a
    few times before giving up, except the one for a used-up daily quota.
    """
    for attempt in range(TERNA_REFUSAL_RETRIES + 1):
        client.calls = getattr(client, "calls", 0) + 1
        try:
            return client.get(path, params)
        except requests.HTTPError as error:
            if error.response is None or error.response.status_code != 403:
                raise
            if TERNA_QUOTA_MESSAGE in error.response.text:
                raise TernaRefused(f"daily quota used up: {path} {params}") from error
            if attempt == TERNA_REFUSAL_RETRIES:
                raise TernaRefused(f"{path} {params}") from error
            print(f"    Terna refused ({path}), waiting {TERNA_REFUSAL_WAIT_SECONDS}s")
            time.sleep(TERNA_REFUSAL_WAIT_SECONDS)


def terna_rows(client, path, key, first, last):
    stamps = {"dateFrom": first.strftime("%d/%m/%Y"), "dateTo": last.strftime("%d/%m/%Y")}
    return terna_get(client, path, stamps).get(key) or []


def imbalance_rows(client, first, last):
    """
    Macrozonal imbalance rows of a week: one request for the whole range
    when Terna answers it, else day by day (final, from D+1 17:00, else
    preliminary).
    """
    final = ("/fees/v1.0/daily-macrozonal-imbalance", "daily_macrozonal_imbalance")
    preliminary = ("/fees/v1.0/preliminary-macrozonal-imbalance", "preliminary_macrozonal_imbalance")
    try:
        rows = terna_rows(client, *final, first, last)
    except requests.HTTPError:
        rows = []
    covered = {row.get("reference_date", "")[:10] for row in rows}
    for day in market_days(first, last):
        if day.isoformat() in covered:
            continue
        day_rows = terna_rows(client, *final, day, day) or terna_rows(client, *preliminary, day, day)
        rows += day_rows
    return rows


def terna_gap(series, before):
    """Days from HISTORY_START to before (excluded) without Terna's macrozonal imbalance."""
    have = {row["date"] for row in series["daily"].get("NORD|imbalance_volume", [])}
    return [day for day in market_days(HISTORY_START, before - timedelta(days=1)) if day.isoformat() not in have]


def terna_client(keys=("TERNA_KEY", "TERNA_SECRET")):
    return Client(os.environ[keys[0]], os.environ[keys[1]])


def fetch_terna(start_day, end_day, client=None):
    """
    Terna's records for the days; when Terna stops answering (quota) it
    returns what came in and sets client.refused.
    """
    client = client or terna_client()
    client.refused = False
    records = []
    try:
        for first, last in day_chunks(start_day, end_day, 7):
            print(f"  Terna {first} -> {last}")
            records += imbalance_records(imbalance_rows(client, first, last))
            for day in market_days(max(first, FCR_START), last):
                fcr = terna_get(client, "/market/v1.0/aste-fcr",
                                {"marketDate": day.strftime("%d/%m/%Y")}).get("aste_fcr") or []
                for field, name in (("price", "price"), ("quantity", "procured"), ("requirement", "requirement")):
                    records += local_records(
                        fcr, "date", lambda r, n=name: f"{r['zone']}|fcr_{n}_{r['direction'].lower()}",
                        lambda r, f=field: r.get(f))

        # Reserve requirements: one request per MSD session and chunk; each
        # session restates the hours still ahead.
        for path, key, name, zone_of in (
                ("/market/v1.0/input/afrr-requirement", "secondary_reserve_requirement", "afrr_requirement",
                 lambda z: z),
                ("/market/v1.0/input/rr-requirement", "replacement_reserve_requirement", "rr_requirement",
                 lambda z: TERNA_ZONES.get(z))):
            session_records = []
            for chunk_start, chunk_end in day_chunks(start_day, end_day, REQUIREMENT_CHUNK_DAYS):
                print(f"  Terna {name} {chunk_start} -> {chunk_end}")
                dates = {"dateFrom": chunk_start.strftime("%d/%m/%Y"), "dateTo": chunk_end.strftime("%d/%m/%Y")}
                for session in ("MSD1", "MSD2", "MSD3", "MSD4", "MSD5", "MSD6"):
                    try:
                        rows = terna_get(client, path, {**dates, "sessionType": session}).get(key) or []
                    except requests.HTTPError as error:
                        print(f"    {name} {session}: {error}")
                        continue
                    session_records += local_records(
                        rows, "reference_date",
                        lambda r, n=name, zo=zone_of: (f"{zo(r['zone'])}|{n}" if zo(r["zone"]) else None),
                        lambda r: r.get("requirement_MW"))
            records += latest_session(session_records)
    except TernaRefused as error:
        # Keep what came in; a later run fills the rest (the gap is found
        # from the imbalance volume, so a block cut short is fetched again).
        print(f"  Terna stopped answering: {error}; going on from there in a later run")
        client.refused = True
    return records


def terna_catch_up(series, before):
    """
    Terna's history before the lookback window, newest gap first: one block
    of TERNA_CATCH_UP_DAYS after the other with the second key, until
    TERNA_CATCH_UP_CALLS are used or Terna refuses; a single short block with
    the main key, which the routine runs need too.
    """
    second_key = bool(os.environ.get("TERNA_KEY_2") and os.environ.get("TERNA_SECRET_2"))
    client = terna_client(("TERNA_KEY_2", "TERNA_SECRET_2") if second_key else ("TERNA_KEY", "TERNA_SECRET"))
    client.calls = 0
    days = TERNA_CATCH_UP_DAYS if second_key else TERNA_CATCH_UP_DAYS_SHARED_KEY
    while True:
        # Each block ends before the previous one, so a day Terna never
        # publishes is not asked for again within the run.
        missing = terna_gap(series, before)
        if not missing:
            print(f"  Terna catch-up: nothing missing before {before}")
            return series
        first = max(missing[-1] - timedelta(days=days - 1), HISTORY_START)
        print(f"  Terna catch-up {first} -> {missing[-1]} ({len(missing)} days missing, "
              f"{'second' if second_key else 'main'} key, {client.calls} calls so far)")
        records = fetch_terna(first, missing[-1], client)
        series = merge(series, records)
        if client.refused or not second_key or client.calls >= TERNA_CATCH_UP_CALLS:
            return series
        if not any(r["group"] == "NORD|imbalance_volume" for r in records):
            # Terna has nothing for these days: stop rather than ask again.
            print(f"  Terna catch-up: no imbalance volume for {first} -> {missing[-1]}; stopping")
            return series
        before = first


# ============================================================================
# GME
# ============================================================================


def gme_number(value):
    if value in (None, "", "null"):
        return None
    return float(value)


def msd_records(rows):
    """MSD ex-ante results per zone: 15-minute periods, or hours where there are no periods."""
    records = []
    for row in rows:
        zone = str(row.get("Zone") or "").upper()
        if zone not in ZONES:
            continue
        flow_date = datetime.strptime(str(row["FlowDate"]), "%Y%m%d").date().isoformat()
        period = str(row.get("Period") or "").strip()
        if period and period.lower() != "null":
            label, minutes = market_time_from_period(int(period), 15), 15
        else:
            label, minutes = market_time_from_period(int(row["Hour"]), 60), 60
        for field, name in (("VolumesSold", "msd_volume_up"), ("VolumesPurchased", "msd_volume_down"),
                            ("AverageSellingPrice", "msd_price_up"),
                            ("AveragePurchasingPrice", "msd_price_down")):
            value = gme_number(row.get(field))
            # No accepted offers: no price.
            if value is None or (name.startswith("msd_price") and not value):
                continue
            # Volumes are MWh per period: MW = MWh x periods per hour.
            scale = 60 / minutes if name.startswith("msd_volume") else 1
            records.append({"group": f"{zone}|{name}", "date": flow_date, "time": label, "minutes": minutes,
                            "value": round(value * scale, 3)})
    return records


def fetch_gme(start_day, end_day):
    login = (os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"])
    token = get_token(*login)
    rows = []
    for chunk_start, chunk_end in day_chunks(start_day, end_day, GME_CHUNK_DAYS):
        print(f"  GME MSD ex-ante {chunk_start} -> {chunk_end}")
        try:
            rows += request_chunk(token, chunk_start, chunk_end, None, "MSD", "ME_MSDExAnteResults")
        except PermissionError:
            # The token expires during long downloads: log in again.
            token = get_token(*login)
            rows += request_chunk(token, chunk_start, chunk_end, None, "MSD", "ME_MSDExAnteResults")
        time.sleep(REQUEST_PAUSE_SECONDS)
    return msd_records(rows)


# ============================================================================
# LOAD / MERGE / SAVE
# ============================================================================


def is_mean(group):
    return group.split("|", 1)[1].startswith(MEAN_PREFIXES)


def load_existing():
    """All series from the zone files (or the single file they replaced)."""
    paths = sorted(glob.glob(os.path.join(BALANCING_DIR, "*.json")))
    if not paths and os.path.exists(LEGACY_PATH):
        paths = [LEGACY_PATH]
    series = {resolution: {} for resolution in RESOLUTIONS}
    for path in paths:
        payload = compact.load(path)
        for resolution in RESOLUTIONS:
            for group, rows in payload.get("series", {}).get(resolution, {}).items():
                if not group.startswith(RETIRED_GROUPS) and "|offered_" not in group:
                    series[resolution][group] = rows
    return series


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


def weight_activation_prices(series):
    """
    Hourly and daily activation prices as averages weighted by the energy
    activated in each quarter-hour (a quarter-hour with 0.001 MW activated
    can publish hundreds of thousands of EUR/MWh); a plain average where
    nothing was activated.
    """
    quarter = series["quarter_hourly"]
    buckets = {
        "hourly": lambda row: (row["date"], minutes_label(label_minutes(row["time"]) // 60 * 60)),
        "daily": lambda row: (row["date"], None),
    }
    for group, rows in quarter.items():
        if "|price_" not in group:
            continue
        volumes = {(row["date"], row["time"]): abs(row["value"])
                   for row in quarter.get(group.replace("|price_", "|activated_"), [])}
        for resolution, bucket_of in buckets.items():
            sums = defaultdict(lambda: [0.0, 0.0, 0.0, 0])  # weighted sum, weight, sum, count
            for row in rows:
                weight = volumes.get((row["date"], row["time"]), 0.0)
                total = sums[bucket_of(row)]
                total[0] += row["value"] * weight
                total[1] += weight
                total[2] += row["value"]
                total[3] += 1
            output = []
            for (day, label), (weighted, weight, plain, count) in sorted(
                    sums.items(), key=lambda item: (item[0][0], label_minutes(item[0][1]) if item[0][1] else 0)):
                row = {"date": day, "value": round(weighted / weight if weight else plain / count, 2)}
                if label is not None:
                    row["time"] = label
                output.append(row)
            series[resolution][group] = output
    return series


def area_prefixes(zone):
    """The Terna area series a zone's charts use."""
    return (f"{MACROZONE[zone]}|imbalance_volume", f"{FCR_AREA[zone]}|fcr_", f"{AFRR_AREA[zone]}|afrr_requirement")


# The imbalance prices of the two macrozones, for Italy's file.
MACROZONE_PRICES = ("NORD|imbalance_price", "SUD|imbalance_price")


def national_sums(series):
    """
    IT|sum_activated_<product>_<direction>: the activated volumes summed over
    the zones, at every resolution (energy: the sum of the zones' sums).
    """
    for resolution in RESOLUTIONS:
        for product in ("picasso", "afrr", "rr"):
            for direction in ("up", "down"):
                totals = defaultdict(float)
                for zone in ZONES:
                    for row in series[resolution].get(f"{zone}|activated_{product}_{direction}", []):
                        totals[(row["date"], row.get("time"))] += row["value"]
                series[resolution][f"IT|sum_activated_{product}_{direction}"] = [
                    {"date": day, **({"time": label} if label else {}), "value": round(value, 2)}
                    for (day, label), value in sorted(
                        totals.items(), key=lambda item: (item[0][0], label_minutes(item[0][1]) if item[0][1] else 0))]
    return series


def file_groups(name, groups):
    """Series of one file: a zone's own and its areas', or Italy's, all areas' and the macrozone prices."""
    if name == "IT":
        prefixes = ("IT|",) + tuple({prefix for zone in ZONES for prefix in area_prefixes(zone)}) + MACROZONE_PRICES
    else:
        prefixes = (f"{name}|",) + area_prefixes(name)
    return sorted(group for group in groups if group.startswith(prefixes))


def build_output(name, series):
    groups = file_groups(name, {group for resolution in RESOLUTIONS for group in series[resolution]})
    return {
        "source": "ENTSO-E Transparency Platform, Terna public API, GME API",
        "description": (
            f"Balancing data of {'Italy as a whole and the Terna areas' if name == 'IT' else 'bidding zone ' + name} "
            "('ZONE|series'; 'NORD|'/'SUD|' imbalance volume = macrozone, FCR and aFRR requirement = Terna area). "
            "Prices EUR/MWh (FCR EUR/MW), volumes MW; hourly = average of the quarter-hours (activation prices "
            "weighted by the energy activated), daily = average for prices and capacities, sum of the hours "
            "(MWh/day) for energy. Italian market time, labelled by elapsed time since local midnight."
        ),
        "zone": name,
        "areas": ({"macrozone": MACROZONE, "afrr": AFRR_AREA, "fcr": FCR_AREA} if name == "IT" else
                  {"macrozone": MACROZONE[name], "afrr": AFRR_AREA[name], "fcr": FCR_AREA[name]}),
        "bid_days": (sorted({day for days in bid_days().values() for day in days}) if name == "IT"
                     else bid_days().get(name, [])),
        "series": {
            resolution: {group: compact.encode_series(series[resolution][group], resolution)
                         for group in groups if group in series[resolution]}
            for resolution in RESOLUTIONS
        },
    }


def write_outputs(series):
    series = national_sums(series)
    for name in list(ZONES) + ["IT"]:
        compact.dump(build_output(name, series), os.path.join(BALANCING_DIR, f"{name}.json"))
    if os.path.exists(LEGACY_PATH):
        os.remove(LEGACY_PATH)


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

    # Merged source by source, so a long backfill never holds every
    # source's point records at once.
    series = load_existing()
    for name, fetch, last in (("entsoe", fetch_entsoe, min(end_day, today)),
                              ("terna", fetch_terna, end_day),
                              ("gme", fetch_gme, end_day)):
        if name not in sources:
            continue
        try:
            series = merge(series, fetch(start_day, last))
        except Exception as error:  # one source failing must not lose the others
            print(f"  {name} failed: {error!r}")
    if "terna" in sources and not args.start:
        try:
            series = terna_catch_up(series, start_day)
        except Exception as error:
            print(f"  Terna catch-up failed: {error!r}")
    if "bids" in sources:
        # Bids are not revised once published: routine runs fetch only
        # yesterday (its last hours) and today, and none are kept beyond
        # BID_RETENTION_DAYS.
        bid_start = max(start_day if args.start else today - timedelta(days=1),
                        today - timedelta(days=BID_RETENTION_DAYS))
        update_bids(bid_start, min(end_day, today))
        prune_bids(today)

    series = weight_activation_prices(series)
    write_outputs(series)
    sizes = {name: os.path.getsize(os.path.join(BALANCING_DIR, f"{name}.json")) // 1024 for name in list(ZONES) + ["IT"]}
    print(f"Wrote {BALANCING_DIR}: {len(series['quarter_hourly'])} series; KB per file {sizes}; "
          f"bids for {sum(len(v) for v in bid_days().values())} zone-days")


if __name__ == "__main__":
    main()
