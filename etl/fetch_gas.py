"""
Fetch Italy's gas storage and LNG data from GIE (Gas Infrastructure
Europe) and write app/data/gas.json:

  AGSI+  storage, per gas day: Italy, the EU (for comparison) and the
         storage sites: Stogit HUB1 (nine fields), HUB2 (Collalto, Cellino,
         San Potito & Cotignola: Edison Stoccaggio's until 28 February
         2025, Snam's since) and IGS Cornegliano
  ALSI   LNG terminals, per gas day: Italy and Adriatic LNG (Rovigo), OLT
         (FSRU Toscana), Panigaglia (GNL Italia's until 28 February 2025,
         Snam LNG's since), Piombino and Ravenna (Snam LNG FSRUs)
  IIP    REMIT urgent market messages (UMM) on Italy's balancing zone: the
         latest version of each event, unavailable capacity in GWh/d

Series "<entity>|<field>", daily (gas day start):
  storage   gas_in_storage, working_gas_volume, contracted, available (TWh),
            full (%), injection, withdrawal, injection_capacity,
            withdrawal_capacity (GWh/d); Italy also consumption (TWh a
            year) and consumption_full (% of it in storage)
  lng       inventory, dtmi (max inventory) (GWh), inventory_lng,
            dtmi_lng (10^3 m3 of LNG), send_out, dtrs (send-out capacity),
            contracted, available (GWh/d)

Italy's storage from 2020 (the seasonal comparison), the rest from 2025.
Routine runs re-read the last LOOKBACK_DAYS (GIE publishes at 19:30 and
23:00 CET, operators revise).  GIE allows 60 calls a minute.
Credentials: GIE_KEY (the AGSI/ALSI/IIP API key).
"""

import argparse
import os
import time
from datetime import date, datetime, timedelta

import requests

import compact

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "data", "gas.json")
HOSTS = {"storage": "https://agsi.gie.eu", "lng": "https://alsi.gie.eu", "iip": "https://iip.gie.eu"}

HISTORY_START = date(2025, 1, 1)
SEASONAL_START = date(2020, 1, 1)
LOOKBACK_DAYS = 40
PAUSE_SECONDS = 1.1  # GIE: at most 60 calls a minute
PAGE_SIZE = 300
ITALY_ZONE = "21Y---A001A010-A"  # Italy's balancing zone on IIP

# Entity -> (kind, label, parts): a part is (company, facility, first day,
# last day) of one dataset; a site that changed operator has two.
OPERATOR_CHANGE = date(2025, 3, 1)
ENTITIES = {
    "IT": ("storage", "Italy", [{}]),
    "EU": ("storage", "EU", [{"type": "eu"}]),
    "HUB1": ("storage", "Stogit HUB1", [{"company": "21X000000001250I", "facility": "21Z000000000274I"}]),
    "HUB2": ("storage", "Stogit HUB2", [
        {"company": "21X0000000013651", "facility": "21W000000000095N", "last": OPERATOR_CHANGE - timedelta(days=1)},
        {"company": "21X000000001250I", "facility": "21W000000000095N", "first": OPERATOR_CHANGE}]),
    "CORNEGLIANO": ("storage", "IGS Cornegliano", [{"company": "59X4-IGSTORAGE-T", "facility": "59W-IGSTORAGE-0Q"}]),
    "LNG": ("lng", "Italy", [{}]),
    "ROVIGO": ("lng", "Adriatic LNG (Rovigo)", [{"company": "21X000000001360B", "facility": "21W000000000082W"}]),
    "OLT": ("lng", "OLT Toscana", [{"company": "21X000000001109G", "facility": "21W0000000000443"}]),
    "PANIGAGLIA": ("lng", "Panigaglia", [
        {"company": "26X00000117915-0", "facility": "59W0000000000011", "last": OPERATOR_CHANGE - timedelta(days=1)},
        {"company": "59XFSRUITALIASTY", "facility": "59W0000000000011", "first": OPERATOR_CHANGE}]),
    "PIOMBINO": ("lng", "Piombino FSRU", [{"company": "59XFSRUITALIASTY", "facility": "59WFSRUGOLARTUNH"}]),
    "RAVENNA": ("lng", "Ravenna FSRU", [{"company": "59XFSRUITALIASTY", "facility": "59WBWSINGAPORERX"}]),
}

STORAGE_FIELDS = {
    "gas_in_storage": "gasInStorage", "working_gas_volume": "workingGasVolume", "full": "full",
    "injection": "injection", "withdrawal": "withdrawal",
    "injection_capacity": "injectionCapacity", "withdrawal_capacity": "withdrawalCapacity",
    "contracted": "contractedCapacity", "available": "availableCapacity",
    "consumption": "consumption", "consumption_full": "consumptionFull",
}
LNG_FIELDS = {
    "inventory": ("inventory", "gwh"), "inventory_lng": ("inventory", "lng"),
    "dtmi": ("dtmi", "gwh"), "dtmi_lng": ("dtmi", "lng"),
    "send_out": ("sendOut", None), "dtrs": ("dtrs", None),
    "contracted": ("contractedCapacity", None), "available": ("availableCapacity", None),
}

# IIP units -> factor to GWh/d (OLT reports its daily send-out limit as "GWh").
UNIT_TO_GWH_DAY = {"GWh/d": 1, "GWh": 1, "GWh/h": 24, "kWh/d": 1e-6, "kWh/h": 24e-6, "MWh/d": 1e-3, "MWh/h": 24e-3}
# Facilities named by the reporting entity (the asset names vary).
UMM_FACILITIES = {
    "21X000000001360B": "Adriatic LNG (Rovigo)",
    "21X000000001109G": "OLT Toscana",
    "59X4-IGSTORAGE-T": "IGS Cornegliano",
    "59XFSRUITALIASTY": "Snam LNG",
}


class Gie:
    def __init__(self, key):
        self.key = key
        self.calls = 0

    def get(self, host, path="/api", **params):
        for attempt in range(5):
            time.sleep(PAUSE_SECONDS)
            self.calls += 1
            response = requests.get(HOSTS[host] + path, params=params, headers={"x-key": self.key}, timeout=120)
            if response.status_code == 429 and attempt < 4:
                print("    too many requests, waiting 61 s")
                time.sleep(61)
                continue
            response.raise_for_status()
            return response.json()

    def pages(self, host, **params):
        """Every row of a paginated AGSI/ALSI query."""
        rows, page = [], 1
        while True:
            body = self.get(host, size=PAGE_SIZE, page=page, **params)
            rows += body.get("data", [])
            last = body.get("last_page") or (body.get("meta") or {}).get("last_page") or 1
            if page >= last:
                return rows
            page += 1


def number(value):
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def entity_rows(kind, rows, first, last):
    """{field: {day: value}} from GIE rows within [first, last]."""
    out = {}
    for row in rows:
        day = row.get("gasDayStart")
        if not day or not (first.isoformat() <= day <= last.isoformat()) or row.get("status") == "N":
            continue
        if kind == "storage":
            values = {name: number(row.get(field)) for name, field in STORAGE_FIELDS.items()}
        else:
            values = {name: number((row.get(field) or {}).get(sub) if sub else row.get(field))
                      for name, (field, sub) in LNG_FIELDS.items()}
        for name, value in values.items():
            if value is not None:
                out.setdefault(name, {})[day] = value
    return out


def fetch_entity(gie, code, first, last):
    kind, label, parts = ENTITIES[code]
    host = "storage" if kind == "storage" else "lng"
    merged = {}
    for part in parts:
        part_first = max(first, part.get("first", first))
        part_last = min(last, part.get("last", last))
        if part_first > part_last:
            continue
        params = {key: value for key, value in part.items() if key in ("company", "facility", "type")}
        if "type" not in params:
            params["country"] = "IT"
        rows = gie.pages(host, **params, **{"from": part_first.isoformat(), "to": part_last.isoformat()})
        for name, values in entity_rows(kind, rows, part_first, part_last).items():
            merged.setdefault(name, {}).update(values)
    days = len(next(iter(merged.values()), {}))
    print(f"  {label} ({code}): {days} days {first} -> {last}")
    return merged


def parse_time(text):
    return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S") if text else None


def umm_events(rows):
    """The latest version of each event on Italy's balancing zone, dismissed ones left out."""
    latest = {}
    for row in rows:
        message = row.get("message") or {}
        event, _, version = (message.get("messageId") or "").rpartition("_")
        if not event:
            continue
        if event in latest and latest[event][0] >= version:
            continue
        latest[event] = (version, row)
    events = []
    for event, (version, row) in sorted(latest.items()):
        if row.get("status") == "Dismissed":
            continue
        entity = row.get("reportingEntity") or {}

        def gwh_day(block):
            block = row.get(block) or {}
            value, factor = number(block.get("capacity")), UNIT_TO_GWH_DAY.get(block.get("unit"))
            return round(value * factor, 3) if value is not None and factor is not None else None

        events.append({
            "id": event,
            "version": int(version) if version.isdigit() else version,
            "facility": UMM_FACILITIES.get(entity.get("code")) or (row.get("asset") or {}).get("name") or entity.get("name"),
            "operator": entity.get("name"),
            "operator_type": entity.get("type"),
            "message_type": message.get("messageType"),
            "planned": message.get("unavailabilityType") == "Planned",
            "status": row.get("status"),
            "from": (row.get("from") or "")[:16],
            "to": (row.get("to") or "")[:16],
            "unavailable": gwh_day("unavailable"),
            "technical": gwh_day("technical"),
            "unit": (row.get("unavailable") or {}).get("unit"),
            "reason": (row.get("unavailabilityReason") or "").strip(),
            "published": (row.get("published") or row.get("submitted") or "")[:16],
        })
    # IGS and Snam storage: the asset names are fine; Snam's Collalto plant is HUB2.
    for item in events:
        if item["facility"] and "Collalto" in item["facility"]:
            item["facility"] = "Stogit HUB2 (Collalto)"
    return events


def fetch_umms(gie):
    rows, page = [], 1
    while True:
        body = gie.get("iip", balancingZone=ITALY_ZONE, page=page)
        rows += body.get("data", [])
        if page >= ((body.get("meta") or {}).get("last_page") or 1):
            break
        page += 1
    events = umm_events(rows)
    print(f"  IIP: {len(rows)} messages, {len(events)} events")
    return events


def load_existing():
    if not os.path.exists(OUTPUT_PATH):
        return {}
    payload = compact.load(OUTPUT_PATH)
    return {group: {row["date"]: row["value"] for row in by_resolution.get("daily", [])}
            for group, by_resolution in payload.get("series", {}).items()}


def build_output(series, events, latest_day):
    return {
        "source": "GIE (Gas Infrastructure Europe): AGSI+ storage, ALSI LNG, IIP urgent market messages",
        "description": __doc__.split("\n\n")[0].strip(),
        "entities": {code: {"kind": kind, "name": label} for code, (kind, label, _) in ENTITIES.items()},
        "latest_gas_day": latest_day,
        "umm": events,
        "series": {group: {"daily": compact.encode_series(
            [{"date": day, "value": value} for day, value in sorted(values.items())], "daily")}
            for group, values in sorted(series.items()) if values},
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch Italy's gas storage and LNG data from GIE.")
    parser.add_argument("--start", help="First gas day to (re)fetch (YYYY-MM-DD); default: the last 40 days")
    parser.add_argument("--full-history", action="store_true", help="Re-read everything from 2025 (Italy's storage from 2020)")
    args = parser.parse_args()

    key = os.environ.get("GIE_KEY")
    if not key:
        raise RuntimeError("GIE_KEY must be set.")
    gie = Gie(key)
    today = date.today()
    series = {} if args.full_history else load_existing()

    for code in ENTITIES:
        start = SEASONAL_START if code == "IT" else HISTORY_START
        have = series.get(f"{code}|full" if ENTITIES[code][0] == "storage" else f"{code}|send_out", {})
        if args.start:
            first = max(start, date.fromisoformat(args.start))
        elif have and min(have) <= start.isoformat():
            first = today - timedelta(days=LOOKBACK_DAYS)
        else:
            first = start  # history missing: from the start
        for name, values in fetch_entity(gie, code, first, today).items():
            group = series.setdefault(f"{code}|{name}", {})
            for day in [day for day in group if day >= first.isoformat()]:
                del group[day]
            group.update(values)

    events = fetch_umms(gie)
    latest_day = max((day for day in series.get("IT|full", {})), default=None)
    compact.dump(build_output(series, events, latest_day), OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH} ({os.path.getsize(OUTPUT_PATH) // 1024:,} KB), {gie.calls} GIE calls, "
          f"latest gas day {latest_day}")


if __name__ == "__main__":
    main()
