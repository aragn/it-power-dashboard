"""Temporary probe of the GIE APIs (AGSI, ALSI, IIP) for Italy; prints structure, never the key."""

import json
import os
import time

import requests

KEY = os.environ["GIE_KEY"]
HOSTS = {"agsi": "https://agsi.gie.eu", "alsi": "https://alsi.gie.eu", "iip": "https://iip.gie.eu"}


def get(host, path, **params):
    time.sleep(1.2)  # 60 calls a minute at most
    response = requests.get(HOSTS[host] + path, params=params, headers={"x-key": KEY}, timeout=120)
    try:
        body = response.json()
    except ValueError:
        body = response.text[:300]
    return response.status_code, body


def show(title, value, limit=2500):
    text = json.dumps(value, ensure_ascii=False, indent=1) if not isinstance(value, str) else value
    print(f"\n===== {title}\n{text[:limit]}")


for host in ("agsi", "alsi"):
    status, listing = get(host, "/api/about", show="listing")
    italy = [entry for entry in (listing if isinstance(listing, list) else listing.get("data", listing) if isinstance(listing, dict) else [])
             if isinstance(entry, dict) and str(entry.get("country", "")).upper() == "IT"]
    show(f"{host} listing status {status}: {type(listing).__name__}, Italian companies {len(italy)}",
         [{k: v for k, v in entry.items() if k != "image"} for entry in italy], 6000)
    if not italy and isinstance(listing, (list, dict)):
        show(f"{host} listing sample", listing if isinstance(listing, dict) else listing[:2], 1500)

    status, body = get(host, "/api", country="IT", size=2)
    show(f"{host} country IT latest (status {status})", body, 3500)
    status, body = get(host, "/api", country="IT", **{"from": "2025-01-01", "to": "2025-01-03"}, size=300)
    show(f"{host} country IT 2025-01-01..03 (status {status})",
         {k: v for k, v in body.items() if k != "data"} if isinstance(body, dict) else body, 600)
    if isinstance(body, dict):
        for row in body.get("data", [])[:1]:
            show(f"{host} IT row keys", sorted(row), 1500)
            show(f"{host} IT children", [{k: v for k, v in child.items() if k != "children"} for child in row.get("children", [])][:6], 4000)

    for company in italy:
        status, body = get(host, "/api", country="IT", company=company["eic"], size=1)
        rows = body.get("data", []) if isinstance(body, dict) else []
        show(f"{host} company {company.get('short_name') or company.get('name')} (status {status})",
             rows[0] if rows else body, 2500)
        for facility in company.get("facilities", [])[:3]:
            status, body = get(host, "/api", country="IT", company=company["eic"], facility=facility["eic"], size=1)
            rows = body.get("data", []) if isinstance(body, dict) else []
            show(f"{host}   facility {facility.get('name')} (status {status})", rows[0] if rows else body, 1500)

    status, body = get(host, "/api/unavailability", country="IT", size=5)
    show(f"{host} unavailability IT (status {status})",
         {k: (v[:3] if k == "data" else v) for k, v in body.items()} if isinstance(body, dict) else body, 4000)
    status, body = get(host, "/api/unavailability", country="IT", start="2025-01-01", size=300)
    if isinstance(body, dict):
        show(f"{host} unavailability IT since 2025 totals", {k: v for k, v in body.items() if k != "data"}, 400)

    for path in ("/api/tariffs", "/api/tariff", "/api/transparency", "/api/tariffs?country=IT"):
        status, body = get(host, path)
        show(f"{host} {path} -> {status}", body if isinstance(body, str) else str(body)[:400], 500)

status, body = get("iip", "/api", size=3)
show(f"iip latest (status {status})", body, 4000)
for name, params in (("balancingZone=ital", {"balancingZone": "ital"}), ("balancingZone=psv", {"balancingZone": "psv"}),
                     ("reportingEntity=snam", {"reportingEntity": "snam"}), ("reportingEntity=stogit", {"reportingEntity": "stogit"}),
                     ("reportingEntity=edison", {"reportingEntity": "edison"}), ("reportingEntity=adriatic", {"reportingEntity": "adriatic"}),
                     ("reportingEntity=olt", {"reportingEntity": "olt"}), ("reportingEntity=gnl", {"reportingEntity": "gnl"})):
    status, body = get("iip", "/api", size=2, **params)
    if isinstance(body, dict):
        data = body.get("data", [])
        show(f"iip {name} (status {status}) total {body.get('total')} last_page {body.get('last_page')}",
             [{k: entry.get(k) for k in ("submitted", "reportingEntity", "message", "marketParticipant", "asset",
                                         "balancingZone", "unavailable", "technical", "unavailabilityReason")}
              for entry in data[:2]], 3000)
    else:
        show(f"iip {name} (status {status})", body, 300)
