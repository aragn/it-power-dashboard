"""
Fetch Italy North CCR cross-zonal capacity data from the JAO Publication
Tool (https://publicationtool.jao.eu/ibwt/): how much capacity the TSOs gave
on Italy's northern border and how much the markets used, the allocation
constraint on imports, and the network elements that limited the capacity.

Series (Italian market time, MW unless noted):
  ntc_da|import          final day-ahead NTC into Italy, Austria + Switzerland
                         + France + Slovenia (Final TTC & NTC day-ahead)
  atc_da|<AT/FR/SI>      day-ahead ATC into Italy (NTC minus long-term
                         nominations) of the coupled borders
  sched_da|<AT/FR/SI>    day-ahead scheduled exchange into Italy
  ci_da|<country>        day-ahead congestion income of the border, both
  ci_ida|<country>       directions (IDA 1 + 2 + 3), in EUR per hour, so the
                         daily value is the day's total in EUR
  ac_da|import           allocation constraint on imports at the northern
  ac_id|import           border, day-ahead and intraday (hourly; JAO writes
                         99999 for "none", stored as missing)
  ac_da|load             its day-ahead inputs: forecast load, non-dispatchable
  ac_da|nondisp          infeed and minimum dispatchable generation needed
  ac_da|mindisp
  limit_da|<category>    what limited the final NTC (TTC "limited by"), as a
  limit_id|<category>    share of the market time units (0-1)

and the day-ahead limiting critical network element of each market time
unit (CNEC Info day-ahead, limiting = true) with its contingency.

Hourly = average of the quarter-hours, daily = average of the hours (sum
for the congestion income).  Quarter-hourly series are kept from
QUARTER_HOURLY_FROM; the allocation constraint and the "limited by" shares
only hourly and daily.  Output: app/data/jao_capacity.json.  No API key.
"""

import argparse
import os
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import requests

import compact
from entsoe_api import label_minutes, market_today, merge_resolutions, parse_date
from fetch_jao_spreads import (
    HISTORY_START,
    PAUSE_SECONDS,
    jao_get,
    market_time,
    quarter_hour_days,
    request_chunks,
)

# ============================================================================
# CONFIGURATION
# ============================================================================

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "data", "jao_capacity.json")

NORTHERN = ("AT", "CH", "FR", "SI")
COUPLED = ("AT", "FR", "SI")
INCOME_BORDERS = ("AT", "FR", "SI", "GR")
IDA_MARKETS = ("ID1", "ID2", "ID3")

NO_CONSTRAINT = 99999

# JAO's "limited by" values -> category shown on the dashboard.
LIMIT_CATEGORIES = {
    "Critical Branch": "critical_element",
    "Validation phase": "validation",
    "TTC Validation phase - bilateral reduction": "validation",
    "TTC Selection - LTTC": "long_term",
    "TTC Selection - LTTCExport": "long_term",
    "TTC Selection - LTTCImport": "long_term",
    "TTC Selection - LTTCImport and LTTCExport": "long_term",
    "Scheduled TTC": "long_term",
    "Smoothing ramp": "ramp",
    "ID schedules": "schedules",
    "Minimum Margin": "minimum",
    "Adjustment reached": "minimum",
    "Min import NTC Threshold": "minimum",
}
CATEGORY_ORDER = ("critical_element", "validation", "long_term", "ramp", "schedules", "minimum", "other")

# Series without quarter-hours (hourly data, or shares that only make sense
# over an hour), and the ones summed over the day.
HOURLY_ONLY = ("ac_", "limit_")
SUMMED = ("ci_",)

QUARTER_HOURLY_FROM = "2025-10-01"
LOOKBACK_DAYS = 3
RESOLUTIONS = ("quarter_hourly", "hourly", "daily")


# ============================================================================
# RECORDS
# ============================================================================


def point(group, row, minutes, value):
    day, label = market_time(row)
    return {"group": group, "date": day, "time": label, "minutes": minutes, "value": float(value)}


def step_of(rows):
    """Market time unit (minutes) of each row: 15 on days with quarter-hours."""
    quarter_days = quarter_hour_days(rows)
    return lambda row: 15 if market_time(row)[0] in quarter_days else 60


def ntc_records(rows, prefix):
    """Total northern import NTC (day-ahead only) and the "limited by" shares."""
    minutes = step_of(rows)
    records = []
    for row in rows:
        if prefix == "da":
            values = [row.get(f"border_{country}_IT") for country in NORTHERN]
            if all(value is not None for value in values):
                records.append(point("ntc_da|import", row, minutes(row), sum(values)))
        reason = row.get("ttc_LimitedBy")
        if reason:
            category = LIMIT_CATEGORIES.get(reason.strip(), "other")
            records += [point(f"limit_{prefix}|{name}", row, minutes(row), 1.0 if name == category else 0.0)
                        for name in CATEGORY_ORDER]
    return records


def border_records(rows, group, countries=COUPLED):
    minutes = step_of(rows)
    return [point(f"{group}|{country}", row, minutes(row), row[f"border_{country}_IT"])
            for row in rows for country in countries if row.get(f"border_{country}_IT") is not None]


def income_records(rows, group):
    """Congestion income of each border (both directions) in EUR per hour."""
    minutes = step_of(rows)
    records = []
    for row in rows:
        for country in INCOME_BORDERS:
            values = [row.get(f"grossBorder_{country}_IT"), row.get(f"grossBorder_IT_{country}")]
            if all(value is None for value in values):
                continue
            step = minutes(row)
            records.append(point(f"{group}|{country}", row, step, sum(v or 0 for v in values) * 60 / step))
    return records


def constraint_records(rows, prefix, inputs):
    records = []
    for row in rows:
        value = row.get("allocationConstraintImport")
        if value is not None and value < NO_CONSTRAINT:
            records.append(point(f"ac_{prefix}|import", row, 60, value))
        if inputs:
            for field, name in (("totalLoad", "load"), ("totalNonDisp", "nondisp"), ("minDispNeeded", "mindisp")):
                if row.get(field) is not None:
                    records.append(point(f"ac_{prefix}|{name}", row, 60, row[field]))
    return records


def limiting_rows(rows):
    """(date, time, minutes, element, contingency, element's area) of the limiting CNECs."""
    minutes = step_of(rows)
    out = []
    for row in rows:
        if not row.get("limiting") or not row.get("cneName"):
            continue
        day, label = market_time(row)
        contingencies = row.get("contingencies") or []
        contingency = contingencies[0]["name"] if contingencies and contingencies[0].get("name") else "Unknown"
        out.append({"date": day, "time": label, "minutes": minutes(row), "cne": row["cneName"].strip(),
                    "contingency": contingency.strip(), "hub": row.get("hubFrom")})
    return out


# ============================================================================
# DOWNLOAD
# ============================================================================


# (endpoint, extra query parameters, records from its rows)
ENDPOINTS = [
    ("CCR_finalTtcNtc", None, lambda rows: ntc_records(rows, "da")),
    ("CCR_idFinalTtcNtc", None, lambda rows: ntc_records(rows, "id")),
    ("DA_atc", None, lambda rows: border_records(rows, "atc_da")),
    ("DA_scheduledExchanges", None, lambda rows: border_records(rows, "sched_da")),
    ("DA_congestionIncome", None, lambda rows: income_records(rows, "ci_da")),
    *((f"{market}_congestionIncome", None, lambda rows: income_records(rows, "ci_ida")) for market in IDA_MARKETS),
    ("CCR_allocationConstraint", None, lambda rows: constraint_records(rows, "da", inputs=True)),
    ("CCR_idAllocationConstraint", None, lambda rows: constraint_records(rows, "id", inputs=False)),
]
LIMITING_ENDPOINT = ("CCR_cnecInfo", {"Filter": '{"limiting":true}'})
PARALLEL_REQUESTS = 4


def download(start_day, end_day):
    """Each chunk's requests run in parallel (PARALLEL_REQUESTS at a time)."""
    session = requests.Session()
    records, limiting = [], []
    with ThreadPoolExecutor(PARALLEL_REQUESTS) as pool:
        for first, last in request_chunks(start_day, end_day):
            began = time.time()
            jobs = [pool.submit(jao_get, session, endpoint, first, last, extra) for endpoint, extra, _ in ENDPOINTS]
            limiting_job = pool.submit(jao_get, session, *LIMITING_ENDPOINT[:1], first, last, LIMITING_ENDPOINT[1])
            ida = []
            for (endpoint, _, to_records), job in zip(ENDPOINTS, jobs):
                if endpoint.endswith("_congestionIncome") and not endpoint.startswith("DA"):
                    ida += to_records(job.result())
                else:
                    records += to_records(job.result())
            records += sum_by_slot(ida)
            limiting += limiting_rows(limiting_job.result())
            print(f"  {first} -> {last} ({time.time() - began:.1f}s)")
            time.sleep(PAUSE_SECONDS)
    return records, limiting


def sum_by_slot(records):
    """The IDA 1-3 congestion income of a border and slot added up."""
    totals = {}
    for record in records:
        key = (record["group"], record["date"], record["time"])
        if key in totals:
            totals[key]["value"] += record["value"]
        else:
            totals[key] = dict(record)
    return list(totals.values())


# ============================================================================
# LOAD / MERGE / SAVE
# ============================================================================


def load_existing():
    existing = {resolution: {} for resolution in RESOLUTIONS}
    limiting = []
    if not os.path.exists(OUTPUT_PATH):
        return existing, limiting
    payload = compact.load(OUTPUT_PATH)
    for group, by_resolution in payload.get("series", {}).items():
        for resolution in RESOLUTIONS:
            existing[resolution][group] = by_resolution.get(resolution, [])
    table = payload.get("limiting") or {}
    for day_index, minute, step, cne, contingency in table.get("rows", []):
        day = (parse_date(table["start"]) + timedelta(days=day_index)).isoformat()
        limiting.append({"date": day, "time": f"{minute // 60:02d}:{minute % 60:02d}", "minutes": step,
                         "cne": table["elements"][cne], "contingency": table["contingencies"][contingency],
                         "hub": table["areas"][cne]})
    return existing, limiting


def merge(existing, records):
    merged = {resolution: dict(existing[resolution]) for resolution in RESOLUTIONS}
    for summed in (True, False):
        subset = [r for r in records if r["group"].startswith(SUMMED) == summed]
        if subset:
            result = merge_resolutions(existing, subset, RESOLUTIONS, daily="sum" if summed else "mean")
            groups = {r["group"] for r in subset}
            for resolution in RESOLUTIONS:
                merged[resolution].update({g: rows for g, rows in result[resolution].items() if g in groups})
    return merged


def merge_limiting(existing, new):
    """New rows replace the existing ones of the same days."""
    days = {row["date"] for row in new}
    rows = [row for row in existing if row["date"] not in days] + new
    return sorted(rows, key=lambda row: (row["date"], label_minutes(row["time"])))


def element_area(name, hubs):
    """The country of an element: JAO's hub when given, else the TSO tag at the end of its name."""
    if hubs:
        return Counter(hubs).most_common(1)[0][0]
    tags = [part.strip("[] ") for part in name.replace("][", "] [").split() if part.startswith("[")]
    tags = [tag for tag in tags if len(tag) == 2 and tag.isalpha() and tag.isupper()]
    return tags[-1] if tags else None


def limiting_table(rows, areas):
    """Compact table: element and contingency names once, rows as indices."""
    if not rows:
        return None
    start = parse_date(rows[0]["date"])
    elements = sorted({row["cne"] for row in rows})
    contingencies = sorted({row["contingency"] for row in rows})
    element_index = {name: i for i, name in enumerate(elements)}
    contingency_index = {name: i for i, name in enumerate(contingencies)}
    return {
        "start": start.isoformat(),
        "columns": ["day", "minute", "minutes", "element", "contingency"],
        "elements": elements,
        "areas": [areas.get(name) for name in elements],
        "contingencies": contingencies,
        "rows": [[(parse_date(row["date"]) - start).days, label_minutes(row["time"]), row["minutes"],
                  element_index[row["cne"]], contingency_index[row["contingency"]]] for row in rows],
    }


def build_output(series, limiting, areas):
    groups = sorted({group for resolution in RESOLUTIONS for group in series[resolution]})
    out = {}
    for group in groups:
        out[group] = {}
        for resolution in RESOLUTIONS:
            if resolution == "quarter_hourly" and group.startswith(HOURLY_ONLY):
                continue
            out[group][resolution] = compact.encode_series(
                series[resolution].get(group, []), resolution,
                skip_before=QUARTER_HOURLY_FROM if resolution == "quarter_hourly" else None)
    return {
        "source": "JAO Publication Tool, Italy North CCR & IBWT (publicationtool.jao.eu/ibwt)",
        "description": __doc__.split("\n\n")[1].strip(),
        "quarter_hourly_native_from": QUARTER_HOURLY_FROM,
        "limit_categories": list(CATEGORY_ORDER),
        "series": out,
        "limiting": limiting_table(limiting, areas),
    }


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="Fetch JAO Italy North CCR capacity, allocation constraint "
                                                 "and limiting element data.")
    parser.add_argument("--start", help="First market day (YYYY-MM-DD or YYYYMMDD); default: LOOKBACK_DAYS ago")
    parser.add_argument("--end", help="Last market day; default: tomorrow")
    parser.add_argument("--full-history", action="store_true", help="Rebuild from 2025-01-01")
    args = parser.parse_args()

    today = market_today()
    end_day = parse_date(args.end) if args.end else today + timedelta(days=1)
    if args.start:
        start_day = parse_date(args.start)
    elif args.full_history:
        start_day = HISTORY_START
    else:
        start_day = today - timedelta(days=LOOKBACK_DAYS)
    start_day = max(start_day, HISTORY_START)
    if end_day < start_day:
        raise ValueError("End date must not be before start date.")

    print(f"JAO Italy North capacity {start_day} -> {end_day}")
    if args.full_history:
        existing, old_limiting = {resolution: {} for resolution in RESOLUTIONS}, []
    else:
        existing, old_limiting = load_existing()
    records, new_limiting = download(start_day, end_day)
    if not records and not any(existing["daily"].values()):
        raise RuntimeError("No JAO capacity data downloaded; not writing output.")

    series = merge(existing, records)
    limiting = merge_limiting(old_limiting, new_limiting)
    hubs = defaultdict(list)
    for row in limiting:
        if row["hub"]:
            hubs[row["cne"]].append(row["hub"])
    areas = {name: element_area(name, hubs.get(name)) for name in {row["cne"] for row in limiting}}

    for group in sorted(series["daily"]):
        days = series["daily"][group]
        print(f"  {group}: {len(days):,} days" + (f", {days[0]['date']} -> {days[-1]['date']}" if days else ""))
    print(f"  limiting elements: {len(limiting):,} market time units, {len(areas):,} elements")

    compact.dump(build_output(series, limiting, areas), OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH} ({os.path.getsize(OUTPUT_PATH) // 1024:,} KB)")


if __name__ == "__main__":
    main()
