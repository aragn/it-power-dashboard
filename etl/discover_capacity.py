"""TEMPORARY: inspect installed capacity and unavailability data (ENTSO-E + Terna)."""

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
IT = "10YIT-GRTN-----B"
TOKEN = os.environ["ENTSOE_API_KEY"]


def txt(e, tag):
    c = e.find(f".//{{*}}{tag}")
    return c.text if c is not None else None


def entsoe(params):
    r = requests.get(API, params={**params, "securityToken": TOKEN}, timeout=180)
    time.sleep(1)
    if r.status_code != 200:
        return r.status_code, r.text[:300]
    if r.content[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(r.content))
        return 200, [ET.fromstring(z.read(n)) for n in z.namelist()]
    return 200, [ET.fromstring(r.content)]


def capacity_by_type():
    print("=" * 90, "\nENTSO-E 14.1.A installed capacity per production type (A68/A33)")
    for year in (2025, 2026):
        for zone, eic in [("IT", IT)] + list(ZONES.items()):
            status, docs = entsoe({"documentType": "A68", "processType": "A33", "in_Domain": eic,
                                   "periodStart": f"{year}01010000", "periodEnd": f"{year}01020000"})
            if status != 200:
                print(year, zone, status, docs[:120]); continue
            caps = {}
            for d in docs:
                for ts in d.findall(".//{*}TimeSeries"):
                    caps[txt(ts, "psrType")] = float(txt(ts, "quantity"))
            print(year, zone, "total", round(sum(caps.values())), dict(sorted(caps.items())))


def capacity_by_unit():
    print("=" * 90, "\nENTSO-E 14.1.B installed capacity per production unit (A71/A33)")
    status, docs = entsoe({"documentType": "A71", "processType": "A33", "in_Domain": IT,
                           "periodStart": "202601010000", "periodEnd": "202601020000"})
    print("status", status)
    if status != 200:
        print(docs); return
    tss = [ts for d in docs for ts in d.findall(".//{*}TimeSeries")]
    print("units:", len(tss))
    if tss:
        print("first TimeSeries XML:", ET.tostring(tss[0], encoding="unicode")[:1500])
    by = defaultdict(float); n = Counter()
    for ts in tss:
        by[txt(ts, "psrType")] += float(txt(ts, "quantity") or 0); n[txt(ts, "psrType")] += 1
    print("per type MW:", {k: round(v) for k, v in sorted(by.items())}, "counts:", dict(n))


def outages():
    print("=" * 90, "\nENTSO-E 15.1 unavailability (A80 generation units, A77 production units), 10-20 Aug 2026")
    for doc_type in ("A80", "A77"):
        total_docs = 0; types = Counter(); zones = Counter(); business = Counter(); sample = None
        for zone, eic in ZONES.items():
            status, docs = entsoe({"documentType": doc_type, "BiddingZone_Domain": eic,
                                   "periodStart": "202608100000", "periodEnd": "202608200000"})
            if status != 200:
                print(doc_type, zone, status, str(docs)[:160]); continue
            total_docs += len(docs)
            for d in docs:
                types[txt(d, "psrType")] += 1; zones[zone] += 1; business[txt(d, "businessType")] += 1
                sample = sample or d
        print(doc_type, "docs:", total_docs, "psrType:", dict(types), "zones:", dict(zones), "businessType:", dict(business))
        if sample is not None:
            print("sample:", ET.tostring(sample, encoding="unicode")[:2500])


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
        if r.status_code in (429, 403) and "Qps" in r.text or r.status_code == 429:
            time.sleep(5 * (attempt + 1)); continue
        time.sleep(3)
        if r.status_code != 200:
            return {"error": r.status_code, "text": r.text[:300]}
        return r.json()


def terna_capacity():
    print("=" * 90, "\nTERNA installed capacity")
    for year in (2024, 2025, 2026):
        res = terna("/generation/v2.0/installed-capacity", {"year": year})
        print(year, "installed-capacity:", res.get("error") or res.get("installed_capacity"))
    for path, key in (("/generation/v2.0/renewable-source-capacity", "renewable_source_capacity"),
                      ("/generation/v2.0/thermoelectric-capacity", "thermoelectric_capacity"),
                      ("/generation/v2.0/generation-plants", "generation_plants")):
        for year in (2025, 2024):
            res = terna(path, {"year": year, "capacityType": "Netta"})
            if res.get("error"):
                print(path, year, res); continue
            recs = next((v for k, v in res.items() if isinstance(v, list)), [])
            print(path, year, len(recs), "records; keys:", list(res.keys()), "first:", recs[:2])
            by = defaultdict(float)
            for r in recs:
                v = str(r.get("efficient_power_MW", "0")).replace(".", "").replace(",", ".")
                try:
                    by[r.get("source") or r.get("subcategory") or r.get("category")] += float(v)
                except ValueError:
                    pass
            print("   MW by source:", {k: round(v) for k, v in by.items()})
            if recs:
                break


def terna_outages():
    print("=" * 90, "\nTERNA generation unit unavailability 10-20 Aug 2026")
    res = terna("/outages/v1.0/generation-unit-unavailability", {"dateFrom": "10/08/26", "dateTo": "20/08/26"})
    if res.get("error"):
        print(res); return
    recs = next((v for k, v in res.items() if isinstance(v, list)), [])
    print(len(recs), "records; keys:", list(res.keys()))
    for r in recs[:3]:
        print("  ", r)
    print("plant_type:", Counter(r.get("plant_type") for r in recs))
    print("zones:", Counter(r.get("biddingZone") for r in recs))
    print("outage_type:", Counter(r.get("outage_type") for r in recs))
    print("status:", Counter(r.get("unavailable_status") for r in recs))
    caps = sorted(float(str(r.get("installed_capacity") or 0).replace(",", ".")) for r in recs)
    print("installed_capacity min/median/max:", caps[:1], caps[len(caps)//2:len(caps)//2+1], caps[-1:])


if __name__ == "__main__":
    for step in (capacity_by_type, capacity_by_unit, outages, terna_capacity, terna_outages):
        try:
            step()
        except Exception as error:  # keep going: this is a discovery run
            print("STEP FAILED", step.__name__, repr(error))
