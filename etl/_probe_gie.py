"""Temporary probe of the GIE APIs (AGSI, ALSI, IIP) for Italy; prints structure, never the key."""

import json
import os
import time
from collections import Counter

import requests

KEY = os.environ["GIE_KEY"]
HOSTS = {"agsi": "https://agsi.gie.eu", "alsi": "https://alsi.gie.eu", "iip": "https://iip.gie.eu"}
ZONE = "21Y---A001A010-A"


def get(host, path, **params):
    time.sleep(1.2)  # 60 calls a minute at most
    response = requests.get(HOSTS[host] + path, params=params, headers={"x-key": KEY}, timeout=120)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text[:300]


def show(title, value, limit=2500):
    text = json.dumps(value, ensure_ascii=False, indent=1) if not isinstance(value, str) else value
    print(f"\n===== {title}\n{text[:limit]}")


def brief(rows, keys):
    return [{key: row.get(key) for key in keys} for row in rows]


STORAGE = ("gasDayStart", "gasInStorage", "full", "injection", "withdrawal", "workingGasVolume", "status")
LNG = ("gasDayStart", "inventory", "sendOut", "dtmi", "dtrs", "status")

status, body = get("agsi", "/api", country="IT", **{"from": "2020-01-01", "to": "2020-01-03"}, size=10)
show("agsi IT 2020", brief(body.get("data", []), STORAGE) if isinstance(body, dict) else body)
status, body = get("agsi", "/api", country="IT", **{"from": "2025-01-01", "to": "2026-10-01"}, size=300)
show("agsi IT since 2025 paging", {k: v for k, v in body.items() if k != "data"} if isinstance(body, dict) else body, 300)
for company, facility, label in (("21X000000001250I", "21Z000000000274I", "HUB1"), ("21X0000000013651", "21W000000000095N", "Edison"),
                                 ("21X000000001250I", "21W000000000095N", "HUB2 Snam")):
    status, body = get("agsi", "/api", country="IT", company=company, facility=facility,
                       **{"from": "2025-02-26", "to": "2025-03-03"}, size=10)
    show(f"agsi {label} around 1 Mar 2025 (status {status})", brief(body.get("data", []), STORAGE) if isinstance(body, dict) else body)

for company, facility, label in (("26X00000117915-0", "59W0000000000011", "Panigaglia GNL Italia"),
                                 ("59XFSRUITALIASTY", "59W0000000000011", "Panigaglia SNAM LNG"),
                                 ("59XFSRUITALIASTY", "59WBWSINGAPORERX", "Ravenna")):
    status, body = get("alsi", "/api", country="IT", company=company, facility=facility,
                       **{"from": "2025-02-26", "to": "2025-03-03"}, size=10)
    show(f"alsi {label} around 1 Mar 2025 (status {status})", brief(body.get("data", []), LNG) if isinstance(body, dict) else body)
status, body = get("alsi", "/api", country="IT", company="59XFSRUITALIASTY", facility="59WBWSINGAPORERX",
                   **{"from": "2025-04-25", "to": "2025-06-05"}, size=60)
show("alsi Ravenna start", brief(body.get("data", []), LNG)[-6:] if isinstance(body, dict) else body, 3000)

status, body = get("alsi", "/api/unavailability", country="IT", start="2025-01-01", size=300)
if isinstance(body, dict):
    data = body.get("data", [])
    show("alsi unavailability IT since 2025", {"last_page": body.get("last_page"), "total": body.get("total"), "rows": len(data),
                                               "facilities": Counter(row["facility"]["name"] for row in data),
                                               "types": Counter(row["type"] for row in data)}, 2000)

# IIP: Italy's balancing zone.
pages, rows = None, []
for page in range(1, 40):
    status, body = get("iip", "/api", balancingZone=ZONE, size=300, page=page)
    if not isinstance(body, dict):
        show("iip error", body)
        break
    pages = body.get("meta", {}).get("last_page")
    rows += body.get("data", [])
    if page >= (pages or 1):
        break
show("iip Italy meta", body.get("meta") if isinstance(body, dict) else body, 500)
print("rows", len(rows))
print("entity types", Counter(row["reportingEntity"]["type"] for row in rows))
print("entities", Counter(row["reportingEntity"]["name"] for row in rows).most_common(20))
print("message types", Counter(row["message"]["messageType"] for row in rows).most_common(20))
print("report types", Counter(row["message"]["reportType"] for row in rows))
print("status", Counter(row["status"] for row in rows))
print("units", Counter((row.get("unavailable") or {}).get("unit") for row in rows))
print("directions", Counter(row.get("direction") for row in rows))
print("years", Counter(str(row.get("from"))[:4] for row in rows))
print("assets", Counter((row.get("asset") or {}).get("name") for row in rows).most_common(40))
ids = Counter(row["message"]["messageId"].rsplit("_", 1)[0] for row in rows)
print("events", len(ids), "with several versions listed", sum(1 for count in ids.values() if count > 1))
print("prev lengths", Counter(len(row.get("prev") or []) for row in rows))
tso = [row for row in rows if row["reportingEntity"]["type"] == "TSO"][:3]
show("iip TSO samples", [{k: v for k, v in row.items() if k not in ("rss", "prev")} for row in tso], 5000)
latest = sorted(rows, key=lambda row: row["submitted"])[-2:]
show("iip latest Italian", [{k: v for k, v in row.items() if k not in ("rss",)} for row in latest], 4000)
