"""
GME market units: every unit (UNIT_REFERENCE_NO) that bids in the MGP, from
GME's public offers (PublicMarketResults / Offers_PublicDomain), summarised
per unit: operator, zone, purpose (OFF sale / BID purchase), the largest
quantity offered in one period, the quantity per hour of the day, prices.

Daily (no arguments): reads the days not read yet among the last
LOOKBACK_DAYS into app/data/gme_units_state.json (with no state, SEED_DAYS
random days since 2025), refreshes ENTSO-E's unit lists once a week, and
writes the database app/data/gme_units.json (build_gme_units.py).  New
codes go to the log and the run's summary.

Research: --sample N / --dates ... [--registries] --out DIR writes the raw
summaries (and ENTSO-E's unit lists) to DIR.

Credentials: GME_API_LOGIN / GME_API_PASSWORD, ENTSOE_API_KEY.
"""

import argparse
import base64
import io
import json
import os
import random
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date, timedelta

import requests

API_BASE = "https://api.mercatoelettrico.org/request/api/v1"
REQUEST_PAUSE_SECONDS = 20
MAX_RETRIES = 6
INITIAL_RETRY_SECONDS = 30

# Columns kept per unit as value counts (the rest are per-bid numbers,
# times and references).
NUMERIC = {"QUANTITY_NO", "AWARDED_QUANTITY_NO", "ENERGY_PRICE_NO", "MERIT_ORDER_NO", "ADJ_QUANTITY_NO",
           "AWARDED_PRICE_NO", "INTERVAL_NO", "PERIOD"}
SKIP = {"TRANSACTION_REFERENCE_NO", "SUBMITTED_DT", "BID_OFFER_DATE_DT", "UNIT_REFERENCE_NO"}
MAX_VALUES = 12

STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "data",
                          "gme_units_state.json")
LOOKBACK_DAYS = 20      # days back the daily run looks for offers not read yet
NEW_DAYS = 3            # at most this many days a run (each file is 500-650 MB)
SEED_DAYS = 20          # the start, without a state
REGISTRY_DAYS = 7       # ENTSO-E unit lists refreshed once a week

IT_DOMAIN = "10YIT-GRTN-----B"
ZONE_EICS = {
    "10Y1001A1001A73I": "NORD", "10Y1001A1001A70O": "CNOR", "10Y1001A1001A71M": "CSUD",
    "10Y1001A1001A788": "SUD", "10Y1001C--00096J": "CALA", "10Y1001A1001A75E": "SICI",
    "10Y1001A1001A74G": "SARD",
}


# ============================================================================
# GME
# ============================================================================


def get_token(login, password):
    response = requests.post(f"{API_BASE}/Auth", json={"Login": login, "Password": password}, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"GME authentication failed: {payload.get('reason')}")
    return payload["token"]


def innermost(raw):
    """The file inside the (possibly nested) zip: (name, bytes)."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        name = archive.namelist()[0]
        content = archive.read(name)
    return innermost(content) if name.lower().endswith(".zip") else (name, content)


def request_offers(token, day, segment="MGP"):
    body = {"Platform": "PublicMarketResults", "Segment": segment, "DataName": "Offers_PublicDomain",
            "IntervalStart": day.strftime("%Y%m%d"), "IntervalEnd": day.strftime("%Y%m%d"), "Attributes": {}}
    wait = INITIAL_RETRY_SECONDS
    for attempt in range(MAX_RETRIES + 1):
        response = requests.post(f"{API_BASE}/RequestData", json=body, timeout=900,
                                 headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 401:
            raise PermissionError("GME token expired or not accepted")
        if response.status_code == 429 and attempt < MAX_RETRIES:
            print(f"    HTTP 429, waiting {wait}s")
            time.sleep(wait)
            wait *= 2
            continue
        response.raise_for_status()
        payload = response.json()
        content = payload.get("contentResponse") or payload.get("ContentResponse")
        if not content:
            raise RuntimeError(f"No data for {day}: {payload.get('resultRequest') or payload}")
        return innermost(base64.b64decode(content))
    raise RuntimeError(f"GME request failed for {day}")


def local(tag):
    return tag.rsplit("}", 1)[-1]


def rows_of(name, content):
    """The offer rows as dicts (XML streamed; JSON as a list)."""
    if name.lower().endswith(".json"):
        data = json.loads(content)
        while isinstance(data, dict):
            data = next(value for value in data.values() if isinstance(value, (list, dict)))
        yield from ({str(k).upper(): v for k, v in row.items()} for row in data)
        return
    # The row element: the first one of leaves only that repeats (a header
    # block of leaves comes once).
    record, first = None, {}
    for _, element in ET.iterparse(io.BytesIO(content), events=("end",)):
        children = list(element)
        tag = local(element.tag)
        if record is None:
            if len(children) < 3 or any(len(child) for child in children):
                continue
            if tag not in first:
                first[tag] = {local(child.tag).upper(): (child.text or "").strip() for child in children}
                continue
            record = tag
            yield first[tag]
        if tag == record:
            yield {local(child.tag).upper(): (child.text or "").strip() for child in children}
            element.clear()


def number(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


# Offers that stand at the market's close: accepted, rejected, partly
# rejected.  Replaced (REP), revoked (REV) and invalid (INC) ones would
# count the same quantity several times.
FINAL_STATUS = {"ACC", "REJ", "PREJ"}


def hour_of(row):
    """Hour of the day (0-23) of the offer's period."""
    period = int(number(row.get("PERIOD") or row.get("INTERVAL_NO")) or 1)
    return min(23, (period - 1) // 4 if row.get("GRANULARITY") == "PT15" else period - 1)


def summarise_day(rows):
    """
    Per unit: value counts of the descriptive columns and, per purpose (OFF
    sale, BID purchase), from the final offers: the largest quantity in a
    period, the quantity offered and awarded per hour of the day (average
    over the hour's periods), the quantity-weighted price and the shares
    offered at 0 EUR/MWh or less and at 10 or less, the bilateral share.
    """
    values = defaultdict(lambda: defaultdict(Counter))
    period_sum = defaultdict(float)              # (unit, purpose, period) -> quantity
    stats = defaultdict(lambda: defaultdict(float))
    hourly = defaultdict(lambda: [[0.0, 0.0, 0] for _ in range(24)])   # offered, awarded, periods
    granularity = Counter()
    columns = Counter()
    sample = []
    count = 0
    for row in rows:
        count += 1
        if len(sample) < 400:
            sample.append(row)
        columns.update(row.keys())
        unit = row.get("UNIT_REFERENCE_NO")
        if not unit:
            continue
        for column, value in row.items():
            if column in NUMERIC or column in SKIP:
                continue
            counter = values[unit][column]
            if value in counter or len(counter) < MAX_VALUES:
                counter[value] += 1
        if row.get("STATUS_CD") not in FINAL_STATUS:
            continue
        granularity[row.get("GRANULARITY") or ""] += 1
        purpose = row.get("PURPOSE_CD") or ""
        period = row.get("PERIOD") or row.get("INTERVAL_NO") or ""
        quantity = number(row.get("QUANTITY_NO")) or 0
        awarded = number(row.get("AWARDED_QUANTITY_NO")) or 0
        price = number(row.get("ENERGY_PRICE_NO"))
        key = (unit, purpose)
        period_sum[(unit, purpose, period)] += quantity
        item = stats[key]
        item["offered"] += quantity
        item["awarded"] += awarded
        if price is not None:
            item["price_quantity"] += price * quantity
            item["priced"] += quantity
            item["at_or_below_0"] += quantity if price <= 0 else 0
            item["at_or_below_10"] += quantity if price <= 10 else 0
            item["price_min"] = min(item.get("price_min", price), price)
            item["price_max"] = max(item.get("price_max", price), price)
        if row.get("BILATERAL_IN") == "true":
            item["bilateral"] += quantity
        cell = hourly[key][hour_of(row)]
        cell[0] += quantity
        cell[1] += awarded
    # Periods per hour, for the hourly averages.
    periods_per_hour = 4 if granularity.get("PT15", 0) >= granularity.get("PT60", 0) else 1
    per_unit = defaultdict(lambda: {"purposes": {}})
    for (unit, purpose, period), quantity in period_sum.items():
        entry = per_unit[unit]["purposes"].setdefault(purpose, {"max_period": 0})
        entry["max_period"] = max(entry["max_period"], quantity)
    for (unit, purpose), item in stats.items():
        entry = per_unit[unit]["purposes"][purpose]
        entry.update({name: round(value, 3) for name, value in item.items()})
        entry["hourly_offered"] = [round(cell[0] / periods_per_hour, 2) for cell in hourly[(unit, purpose)]]
        entry["hourly_awarded"] = [round(cell[1] / periods_per_hour, 2) for cell in hourly[(unit, purpose)]]
    for unit, columns_values in values.items():
        per_unit[unit]["values"] = {column: dict(counter) for column, counter in columns_values.items()}
    return {"rows": count, "periods_per_hour": periods_per_hour, "columns": dict(columns), "sample": sample,
            "units": dict(per_unit)}


def sample_days(count, today, seed=7):
    """Mostly recent days: half from the last three months, the rest of 2026, a quarter from 2025."""
    rng = random.Random(seed)
    last = today - timedelta(days=10)   # public offers are published with a delay
    pools = [
        (last - timedelta(days=90), last, round(count * 0.5)),
        (date(2026, 1, 1), last - timedelta(days=91), round(count * 0.25)),
        (date(2025, 1, 1), date(2025, 9, 30), max(1, count // 10)),
        (date(2025, 10, 1), date(2025, 12, 31), 0),
    ]
    pools[3] = (pools[3][0], pools[3][1], count - sum(n for _, _, n in pools[:3]))
    days = set()
    for first, end, n in pools:
        span = (end - first).days
        while n > 0:
            day = first + timedelta(days=rng.randint(0, span))
            if day not in days:
                days.add(day)
                n -= 1
    return sorted(days)


def merge_units(store, day, summary):
    """Sums over the days; the largest period per granularity (MWh in an hour or a quarter-hour)."""
    pt = "PT15" if summary["periods_per_hour"] == 4 else "PT60"
    for unit, info in summary["units"].items():
        entry = store.setdefault(unit, {"days": [], "values": {}, "purposes": {}})
        entry["days"].append(day.isoformat())
        for column, counter in info.get("values", {}).items():
            merged = entry["values"].setdefault(column, {})
            for value, n in counter.items():
                if value in merged or len(merged) < MAX_VALUES:
                    merged[value] = merged.get(value, 0) + n
        for purpose, stats in info["purposes"].items():
            per = entry["purposes"].setdefault(purpose, {"days": 0, "max_period": {}, "hourly_offered": [0.0] * 24,
                                                         "hourly_awarded": [0.0] * 24})
            per["days"] += 1
            if stats["max_period"] > per["max_period"].get(pt, 0):
                per["max_period"][pt] = stats["max_period"]
                per["max_period"][f"{pt}_day"] = day.isoformat()
            for name in ("offered", "awarded", "price_quantity", "priced", "at_or_below_0", "at_or_below_10",
                         "bilateral"):
                per[name] = round(per.get(name, 0) + stats.get(name, 0), 3)
            for name, pick in (("price_min", min), ("price_max", max)):
                if name in stats:
                    per[name] = pick(per.get(name, stats[name]), stats[name])
            for name in ("hourly_offered", "hourly_awarded"):
                per[name] = [round(a + b, 2) for a, b in zip(per[name], stats.get(name, [0] * 24))]


# ============================================================================
# REGISTRIES (ENTSO-E 14.1.B, TERNA DETAIL AVAILABLE CAPACITY)
# ============================================================================


def leaves(element, prefix=""):
    """Every leaf under element (Periods left out) as {"path": text}."""
    out = {}
    for child in element:
        tag = local(child.tag)
        if tag == "Period":
            continue
        path = f"{prefix}{tag}"
        if len(child):
            out.update(leaves(child, path + "."))
        elif child.text and child.text.strip():
            out[path] = child.text.strip()
    return out


def entsoe_units(token, previous=None):
    """
    ENTSO-E's unit lists: 14.1.B (installed capacity per production unit,
    this year and last) and the production units master data (A95/B11, per
    zone).  A year or zone that fails keeps its previous records.
    """
    from entsoe_api import request_entsoe
    previous = previous or {}
    installed, master = {}, {}
    year = date.today().year
    for y in (year - 1, year):
        try:
            root = request_entsoe(token, {"documentType": "A71", "processType": "A33", "in_Domain": IT_DOMAIN,
                                          "periodStart": f"{y}01010000", "periodEnd": f"{y}01020000"})
        except requests.HTTPError as error:
            print(f"  ENTSO-E 14.1.B {y}: {error}, previous records kept")
            installed.update({k: v for k, v in previous.get("installed_14_1_B", {}).items() if k.startswith(f"{y}|")})
            continue
        series = [] if root is None else root.findall(".//{*}TimeSeries")
        for ts in series:
            info = leaves(ts)
            point = ts.find(".//{*}Point/{*}quantity")
            info["installed_MW"] = point.text if point is not None else None
            info["year"] = y
            installed[f"{y}|{info.get('registeredResource.mRID') or info.get('registeredResource.name')}"] = info
        print(f"  ENTSO-E 14.1.B {y}: {len(series)} units")
        time.sleep(1)
    for eic, zone in ZONE_EICS.items():
        try:
            root = request_entsoe(token, {"documentType": "A95", "businessType": "B11", "BiddingZone_Domain": eic,
                                          "Implementation_DateAndOrTime": f"{year}-01-01"})
        except requests.HTTPError as error:
            print(f"  ENTSO-E A95 {zone}: {error}, previous records kept")
            master.update({k: v for k, v in previous.get("master_A95", {}).items() if k.startswith(f"{zone}|")})
            continue
        series = [] if root is None else root.findall(".//{*}TimeSeries")
        for ts in series:
            info = leaves(ts)
            info["zone"] = zone
            master[f"{zone}|{info.get('registeredResource.mRID')}|{info.get('implementation_DateAndOrTime.date')}|"
                   f"{info.get('MktPSRType.GeneratingUnit_PowerSystemResources.mRID', '')}"] = info
        print(f"  ENTSO-E A95 {zone}: {len(series)} units")
        time.sleep(1)
    return {"installed_14_1_B": installed, "master_A95": master}


# ============================================================================
# DAILY UPDATE
# ============================================================================


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"processed": [], "units": {}, "registries": {}, "registries_updated": None, "daily_from": None}
    with open(STATE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def download_days(days, store, out=None, stop_after=None):
    """Summarise each day's offers into store; returns the days read (one not published yet is skipped)."""
    login, password = os.environ.get("GME_API_LOGIN"), os.environ.get("GME_API_PASSWORD")
    if not login or not password:
        raise RuntimeError("GME_API_LOGIN and GME_API_PASSWORD must be set.")
    token = get_token(login, password)
    done = []
    for index, day in enumerate(days):
        if stop_after is not None and len(done) >= stop_after:
            break
        if index:
            time.sleep(REQUEST_PAUSE_SECONDS)
        try:
            try:
                name, content = request_offers(token, day)
            except PermissionError:
                token = get_token(login, password)
                name, content = request_offers(token, day)
        except Exception as error:  # a day not published yet must not stop the others
            print(f"  {day}: {error}")
            continue
        summary = summarise_day(rows_of(name, content))
        print(f"  {day}: {name}, {len(content) / 1e6:.1f} MB, {summary['rows']:,} rows, "
              f"{summary['periods_per_hour']} periods an hour, {len(summary['units']):,} units")
        if out:
            with open(os.path.join(out, "sample_rows.json"), "w", encoding="utf-8") as handle:
                json.dump({"day": day.isoformat(), "rows": summary["sample"]}, handle, ensure_ascii=False)
        merge_units(store, day, summary)
        done.append(day.isoformat())
    return done


def top_value(counter):
    counter = {k: v for k, v in (counter or {}).items() if k and k != "Bilateralista"} or counter or {}
    return max(counter, key=counter.get) if counter else "-"


def daily(today):
    """
    The days not read yet among the last LOOKBACK_DAYS (newest first, at most
    NEW_DAYS a run: GME publishes the public offers some days after the
    market), the ENTSO-E lists once a week, then the database.  With no state
    yet, SEED_DAYS random days since 2025 make the start.  New codes are
    listed in the log and the run's summary.  (The scheduled run is
    update_gme_offers.py, which reads each day once for this and the merit
    order.)
    """
    state = load_state()
    before = set(state["units"])
    if not state["units"]:
        print(f"No state yet: {SEED_DAYS} sample days")
        state["processed"] += download_days(sample_days(SEED_DAYS, today), state["units"])
    else:
        candidates = [today - timedelta(days=n) for n in range(2, LOOKBACK_DAYS + 1)]
        todo = [day for day in candidates if day.isoformat() not in state["processed"]]
        done = download_days(todo, state["units"], stop_after=NEW_DAYS)
        state["processed"] += done
        if done and not state.get("daily_from"):
            state["daily_from"] = min(done)
    finish_units(state, before, today)


def finish_units(state, before, today):
    """
    After the days are read into the state: the ENTSO-E lists once a week,
    the state and the database written, the new codes (not in `before`) in
    the log and the run's summary.
    """
    from build_gme_units import OUTPUT_PATH, REFERENCE_PATH, build, load_json
    state["processed"] = sorted(set(state["processed"]))
    for unit in state["units"].values():
        unit["days"] = sorted(set(unit["days"]))

    token = os.environ.get("ENTSOE_API_KEY")
    updated = state.get("registries_updated")
    if token and (not updated or (today - date.fromisoformat(updated)).days >= REGISTRY_DAYS):
        state["registries"] = {"entsoe": entsoe_units(token, state.get("registries", {}).get("entsoe"))}
        state["registries_updated"] = today.isoformat()

    with open(STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
    output = build(state["units"], state["registries"], load_json(REFERENCE_PATH, {}))
    output["days"]["daily_from"] = state.get("daily_from")
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUTPUT_PATH}: {len(output['units']):,} rows, {len(state['processed'])} days read")

    new = sorted(set(state["units"]) - before) if before else []
    lines = [f"- `{code}` ({top_value(state['units'][code]['values'].get('OPERATORE'))}, "
             f"{top_value(state['units'][code]['values'].get('ZONE_CD'))})" for code in new]
    print(f"{len(new)} new GME codes" + (":\n" + "\n".join(lines) if lines else ""))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and new:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### {len(new)} new GME unit codes\n\n" + "\n".join(lines) + "\n")


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="GME MGP market units from the public offers.")
    parser.add_argument("--sample", type=int, help="research: number of random days since 2025")
    parser.add_argument("--dates", nargs="*", default=[], help="research: these days (YYYY-MM-DD)")
    parser.add_argument("--registries", action="store_true", help="research: also the ENTSO-E unit lists")
    parser.add_argument("--out", default="gme_units_raw", help="research: where the raw summaries go")
    args = parser.parse_args()

    days = sorted(set([date.fromisoformat(d) for d in args.dates] +
                      (sample_days(args.sample, date.today()) if args.sample else [])))
    if not days and not args.registries:
        daily(date.today())
        return

    os.makedirs(args.out, exist_ok=True)
    if days:
        store = {}
        done = download_days(days, store, out=args.out)
        with open(os.path.join(args.out, "gme_units.json"), "w", encoding="utf-8") as handle:
            json.dump({"days": done, "units": store}, handle, ensure_ascii=False)
        print(f"{len(store):,} units over {len(done)} days")
    if args.registries and os.environ.get("ENTSOE_API_KEY"):
        with open(os.path.join(args.out, "registries.json"), "w", encoding="utf-8") as handle:
            json.dump({"entsoe": entsoe_units(os.environ["ENTSOE_API_KEY"])}, handle, ensure_ascii=False)


if __name__ == "__main__":
    main()
