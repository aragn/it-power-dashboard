"""TEMPORARY: inspect installed capacity and unavailability data (ENTSO-E + Terna), pass 2."""

import io
import os
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict

import requests

API = "https://web-api.tp.entsoe.eu/api"
ZONES = {
    "NORD": "10Y1001A1001A73I", "CNOR": "10Y1001A1001A70O", "CSUD": "10Y1001A1001A71M",
    "SUD": "10Y1001A1001A788", "CALA": "10Y1001C--00096J", "SICI": "10Y1001A1001A75E",
    "SARD": "10Y1001A1001A74G",
}
EIC_ZONE = {v: k for k, v in ZONES.items()}
IT = "10YIT-GRTN-----B"
TOKEN = os.environ["ENTSOE_API_KEY"]
REGION_ZONE = {
    "Valle D'Aosta": "NORD", "Piemonte": "NORD", "Liguria": "NORD", "Lombardia": "NORD",
    "Trentino Alto Adige": "NORD", "Veneto": "NORD", "Friuli Venezia Giulia": "NORD", "Emilia Romagna": "NORD",
    "Toscana": "CNOR", "Marche": "CNOR", "Lazio": "CSUD", "Abruzzo": "CSUD", "Campania": "CSUD", "Umbria": "CSUD",
    "Molise": "SUD", "Puglia": "SUD", "Basilicata": "SUD", "Calabria": "CALA", "Sicilia": "SICI", "Sardegna": "SARD",
}


def txt(e, tag):
    c = e.find(f".//{{*}}{tag}")
    return c.text if c is not None else None


def entsoe(params):
    r = requests.get(API, params={**params, "securityToken": TOKEN}, timeout=180)
    time.sleep(1)
    if r.status_code != 200:
        return r.status_code, r.text[:600]
    if r.content[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(r.content))
        return 200, [ET.fromstring(z.read(n)) for n in z.namelist()]
    return 200, [ET.fromstring(r.content)]


def entsoe_capacity_years():
    print("=" * 90, "\nENTSO-E 14.1.A Italy per year (MW)")
    for year in range(2019, 2027):
        status, docs = entsoe({"documentType": "A68", "processType": "A33", "in_Domain": IT,
                               "periodStart": f"{year}01010000", "periodEnd": f"{year}01020000"})
        caps = {}
        for d in docs if status == 200 else []:
            for ts in d.findall(".//{*}TimeSeries"):
                caps[txt(ts, "psrType")] = float(txt(ts, "quantity"))
        print(year, {k: round(v) for k, v in sorted(caps.items()) if v})


def entsoe_units_master():
    print("=" * 90, "\nENTSO-E A95 production and generation units (master data)")
    status, docs = entsoe({"documentType": "A95", "businessType": "B11", "BiddingZone_Domain": ZONES["SICI"],
                           "Implementation_DateAndOrTime": "2026-01-01"})
    print("status", status, str(docs)[:300] if status != 200 else "")
    if status == 200:
        tss = [ts for d in docs for ts in d.findall(".//{*}TimeSeries")]
        print("units:", len(tss))
        for ts in tss[:2]:
            print(ET.tostring(ts, encoding="unicode")[:1500])
        dates = Counter((txt(ts, "implementation_DateAndOrTime.date") or "")[:4] for ts in tss)
        print("implementation years:", dict(sorted(dates.items())))


def entsoe_outages(start, end):
    """{unit: (zone, psr, businessType, nominal, [(start,end,available)])} for A80 in a window."""
    units = {}
    for zone, eic in ZONES.items():
        offset = 0
        while True:
            status, docs = entsoe({"documentType": "A80", "BiddingZone_Domain": eic,
                                   "periodStart": start, "periodEnd": end, "offset": offset})
            if status != 200:
                print("  A80", zone, "offset", offset, status, docs[:400]); break
            real = [d for d in docs if d.tag.endswith("Unavailability_MarketDocument")]
            for d in real:
                if txt(d, "docStatus/{*}value") == "A13":  # withdrawn
                    continue
                ts = d.find(".//{*}TimeSeries")
                name = txt(ts, "production_RegisteredResource.pSRType.powerSystemResources.name")
                units.setdefault((name, txt(d, "mRID")), {
                    "zone": zone, "psr": txt(ts, "production_RegisteredResource.pSRType.psrType"),
                    "business": txt(ts, "businessType"),
                    "nominal": float(txt(ts, "production_RegisteredResource.pSRType.powerSystemResources.nominalP") or 0),
                    "points": [(txt(p, "start"), txt(p, "end"), float(txt(p, "quantity"))) for p in ts.findall(".//{*}Available_Period")
                               for _ in [0]] or [],
                    "start": txt(ts, "start_DateAndOrTime.date"), "end": txt(ts, "end_DateAndOrTime.date"),
                })
            if len(docs) < 200 or not real:
                break
            offset += 200
    return units


TERNA_TOKEN = {"v": None, "t": 0}


def terna(path, params):
    if not TERNA_TOKEN["v"] or time.time() - TERNA_TOKEN["t"] > 240:
        r = requests.post("https://api.terna.it/public-api/access-token", data={
            "client_id": os.environ["TERNA_KEY"], "client_secret": os.environ["TERNA_SECRET"],
            "grant_type": "client_credentials"}, timeout=60)
        r.raise_for_status(); TERNA_TOKEN.update(v=r.json()["access_token"], t=time.time()); time.sleep(3)
    for attempt in range(6):
        r = requests.get("https://api.terna.it" + path, params=params, timeout=120,
                         headers={"Authorization": f"Bearer {TERNA_TOKEN['v']}", "Accept": "application/json"})
        if r.status_code == 429 or (r.status_code == 403 and "Qps" in r.text):
            time.sleep(5 * (attempt + 1)); continue
        time.sleep(3)
        if r.status_code != 200:
            return {"error": r.status_code, "text": r.text[:300]}
        return r.json()


def records(res):
    return next((v for v in res.values() if isinstance(v, list)), [])


def terna_capacity_years():
    print("=" * 90, "\nTERNA generation-plants (net, MW) per year and source; per zone for the latest")
    res = terna("/generation/v2.0/installed-capacity", {"year": 2024})
    print("installed-capacity 2024 raw:", str(res)[:600])
    latest = None
    for year in range(2019, 2027):
        res = terna("/generation/v2.0/generation-plants", {"year": year, "capacityType": "Netta"})
        recs = records(res) if not res.get("error") else []
        by = defaultdict(float)
        for r in recs:
            by[r["source"]] += float(r["efficient_power_MW"])
        print(year, res.get("error") or "", {k: round(v) for k, v in sorted(by.items())}, "total", round(sum(by.values())))
        if recs:
            latest = (year, recs)
    if latest:
        year, recs = latest
        zone = defaultdict(lambda: defaultdict(float)); unknown = Counter()
        for r in recs:
            z = REGION_ZONE.get(r["region"])
            if not z:
                unknown[r["region"]] += 1; continue
            zone[z][r["source"]] += float(r["efficient_power_MW"])
        print(f"{year} per zone:")
        for z in ZONES:
            print("  ", z, {k: round(v) for k, v in sorted(zone[z].items())})
        print("   unmapped regions:", dict(unknown))
    res = terna("/generation/v2.0/generation-plants", {"year": 2025, "capacityType": "Lorda"})
    by = defaultdict(float)
    for r in records(res):
        by[r["source"]] += float(r["efficient_power_MW"])
    print("2025 gross (Lorda):", {k: round(v) for k, v in sorted(by.items())})


def terna_outages(date_from, date_to):
    res = terna("/outages/v1.0/generation-unit-unavailability", {"dateFrom": date_from, "dateTo": date_to})
    if res.get("error"):
        print("terna outages", res); return []
    recs = records(res)
    print("TERNA outages", date_from, date_to, len(recs), "records; keys:", list(res.keys()))
    for r in recs[:3]:
        print("  ", r)
    for field in ("plant_type", "biddingZone", "outage_type", "unavailable_status"):
        print("  ", field, dict(Counter(r.get(field) for r in recs)))
    return recs


def compare_outages():
    print("=" * 90, "\nOUTAGES 15 Aug 2026: ENTSO-E A80 vs Terna")
    ent = entsoe_outages("202608142200", "202608152200")
    print("ENTSO-E A80 documents:", len(ent))
    print("  psr:", dict(Counter(u["psr"] for u in ent.values())), " business:", dict(Counter(u["business"] for u in ent.values())),
          " zones:", dict(Counter(u["zone"] for u in ent.values())))
    sample = next(iter(ent.values()), None)
    print("  sample:", sample)
    ter = terna_outages("15/08/2026", "15/08/2026")
    ent_names = {name for name, _ in ent}
    ter_names = {r.get("unit_name") for r in ter}
    print("unit names in both:", len(ent_names & ter_names), " only ENTSO-E:", len(ent_names - ter_names),
          " only Terna:", len(ter_names - ent_names))
    print("  e.g. only ENTSO-E:", sorted(ent_names - ter_names)[:8])
    print("  e.g. only Terna:", sorted(n for n in ter_names - ent_names if n)[:8])


if __name__ == "__main__":
    for a, b in (("22/09/2026", "29/09/2026"), ("29/09/2026", "15/10/2026"), ("01/08/2026", "31/08/2026"), ("01/01/2026", "31/01/2026")):
        recs = terna_outages(a, b)
        if recs:
            pub = sorted(r.get("publication_date") for r in recs)
            starts = sorted(r.get("start_date") for r in recs)
            print("   publication", pub[0], "->", pub[-1], " start", starts[0], "->", starts[-1])
            caps = sorted(float(r.get("installed_capacity") or 0) for r in recs)
            print("   installed_capacity min/max", caps[0], caps[-1])
