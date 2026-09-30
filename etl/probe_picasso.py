"""
TEMPORARY: find the per-zone standard-product (PICASSO) aFRR activation
volumes and prices for North on 28 Sept 2026 (09:00Z: 45.825 MW down at
154 EUR/MWh on balancing.services).  Saves raw responses under probe/.
"""

import base64
import json
import os
import time
import traceback

import requests

from entsoe_api import API_URL, parse_response
from fetch_balancing import expand_points, number
from fetch_outages import IT_DOMAIN, ZONES
from fetch_terna import Client
from fetch_zonal import DATA_URL, get_token

OUT = "probe"
NORD = ZONES["NORD"]
DAY = {"periodStart": "202609272200", "periodEnd": "202609282200"}
TARGET = "2026-09-28T09:00"
summary = []


def save(name, content):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(content)


def values_at_target(root):
    """Every TimeSeries: its simple fields and all point values at 09:00Z."""
    found = []
    for ts in root.iter():
        if not ts.tag.endswith("TimeSeries"):
            continue
        fields = {c.tag.split("}")[-1]: (c.text or "").strip() for c in ts if not len(c)}
        fields.pop("mRID", None)

        def all_values(point):
            return {g.tag.split("}")[-1]: g.text for g in point.iter() if not len(g) and g.tag.split("}")[-1] != "position"}

        try:
            hits = [v for instant, _, v in expand_points(ts, all_values)
                    if instant.strftime("%Y-%m-%dT%H:%M") == TARGET]
        except Exception as error:
            hits = [repr(error)]
        found.append({"fields": fields, "at_0900Z": hits[:2]})
    return found


def entsoe(name, params):
    token = os.environ["ENTSOE_API_KEY"]
    response = requests.get(API_URL, params={**params, **DAY, "securityToken": token}, timeout=180)
    time.sleep(0.6)
    text = response.text
    if response.status_code != 200:
        reason = text[text.find("<text>") + 6:text.find("</text>")] if "<text>" in text else text[:200]
        summary.append({"name": name, "status": response.status_code, "reason": reason})
        return
    save(f"{name}.{'zip' if response.content[:2] == b'PK' else 'xml'}", response.content)
    root = parse_response(response.content)
    if root.tag.endswith("Acknowledgement_MarketDocument"):
        reason = root.find(".//{*}text")
        summary.append({"name": name, "status": "no data", "reason": reason.text if reason is not None else ""})
        return
    series = values_at_target(root)
    blob = json.dumps(series)
    summary.append({"name": name, "status": 200, "timeseries": len(series),
                    "HIT": ("45.825" in blob) or ("154" in blob), "series": series[:12]})


def run_entsoe():
    for proc in ("A16", "A67", "A68", "A60", "A61", None):
        for std in ("A01", None):
            for dom in ("controlArea_Domain", "area_Domain"):
                params = {"documentType": "A84", "businessType": "A96", dom: NORD}
                if proc:
                    params["processType"] = proc
                if std:
                    params["Standard_MarketProduct"] = std
                entsoe(f"A84_{proc}_{std}_{dom}", params)
    for proc in ("A16", "A67", "A68", None):
        for business in ("A96", None):
            for dom in ("controlArea_Domain", "area_Domain"):
                params = {"documentType": "A83", dom: NORD}
                if proc:
                    params["processType"] = proc
                if business:
                    params["businessType"] = business
                entsoe(f"A83_{proc}_{business}_{dom}", params)
    for proc in ("A67", "A68", "A51"):
        for std in ("A01", None):
            params = {"documentType": "A24", "processType": proc, "area_Domain": NORD, "curveType": "A03"}
            if std:
                params["Standard_MarketProduct"] = std
            entsoe(f"A24_{proc}_{std}", params)
    for proc in ("A67", "A68"):
        entsoe(f"B17_{proc}_NORD", {"documentType": "B17", "processType": proc,
                                    "Acquiring_Domain": NORD, "Connecting_Domain": NORD})
        entsoe(f"B17_{proc}_IT", {"documentType": "B17", "processType": proc,
                                  "Acquiring_Domain": IT_DOMAIN, "Connecting_Domain": IT_DOMAIN})
        entsoe(f"A30_{proc}_IT_NORD", {"documentType": "A30", "processType": proc,
                                       "Acquiring_Domain": IT_DOMAIN, "Connecting_Domain": NORD})


def run_terna():
    client = Client(os.environ["TERNA_KEY"], os.environ["TERNA_SECRET"])
    dates = {"dateFrom": "28/09/2026", "dateTo": "28/09/2026"}
    for path in ("/market/v1.0/output/prices", "/market/v1.0/output/quantity"):
        for session in ("aFRR", "AFRR", "MRR", "RR", "MB-aFRR", "MB-RS", "RS", "MB"):
            name = f"terna{path.replace('/', '_')}_{session}"
            try:
                data = client.get(path, {**dates, "sessionType": session})
                lists = {k: (len(v), v[:3]) for k, v in data.items() if isinstance(v, list)}
                summary.append({"name": name, "keys": list(data), "result": data.get("result"), "lists": lists})
            except Exception as error:
                summary.append({"name": name, "error": repr(error)[:200]})


def run_gme():
    token = get_token(os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"])
    for segment, start, end in (("AFRE", "20260928", "20260928"), ("AFRE", "20260927", "20260928"),
                                ("AFRR", "20260921", "20260921")):
        body = {"Platform": "PublicMarketResults", "Segment": segment, "DataName": "Offers_PublicDomain",
                "IntervalStart": start, "IntervalEnd": end, "Attributes": {}}
        name = f"gme_{segment}_{start}_{end}"
        try:
            response = requests.post(DATA_URL, json=body, timeout=300,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            payload = response.json()
            content = payload.get("contentResponse") or payload.get("ContentResponse") or ""
            summary.append({"name": name, "status": response.status_code,
                            "meta": {k: v for k, v in payload.items() if k.lower() != "contentresponse"},
                            "bytes": len(content)})
            if content:
                save(f"{name}.zip", base64.b64decode(content))
        except Exception as error:
            summary.append({"name": name, "error": repr(error)[:300]})
        time.sleep(20)


def main():
    for part in (run_entsoe, run_terna, run_gme):
        try:
            part()
        except Exception:
            summary.append({"fatal": traceback.format_exc()[-1200:]})
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1, default=str)
    for item in summary:
        print(json.dumps(item, default=str)[:700])


if __name__ == "__main__":
    main()
