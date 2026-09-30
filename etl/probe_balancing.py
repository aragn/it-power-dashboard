"""
TEMPORARY: discover which balancing data ENTSO-E, Terna and GME publish
for the Italian bidding zones.  Saves every raw response under probe/ and
prints a summary; the workflow uploads probe/ as an artifact.
"""

import base64
import io
import json
import os
import sys
import time
import traceback
import zipfile
from collections import Counter, defaultdict

import requests

from entsoe_api import API_URL, parse_response
from fetch_outages import IT_DOMAIN, ZONES
from fetch_terna import Client
from fetch_zonal import DATA_URL, get_token

OUT = "probe"
START, END = "202609212200", "202609292200"          # 22-29 Sept, Italian days
DAY_START, DAY_END = "202609212200", "202609222200"  # 22 Sept
DOMAINS = {"IT": IT_DOMAIN, **ZONES}

summary = []


def save(folder, name, content):
    path = os.path.join(OUT, folder, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


def describe_entsoe(root):
    """Signatures of the TimeSeries: every simple field but the ids and periods."""
    sigs = Counter()
    resolutions = Counter()
    points = 0
    sample = None
    for ts in root.iter():
        if not ts.tag.endswith("}TimeSeries") and ts.tag != "TimeSeries":
            continue
        fields = []
        for child in ts:
            tag = child.tag.split("}")[-1]
            if tag in ("mRID", "Period") or len(child):
                if len(child) and tag not in ("Period",):
                    fields.append(tag + "{" + ",".join(
                        f"{g.tag.split('}')[-1]}={(g.text or '').strip()}" for g in child if not len(g)) + "}")
                continue
            fields.append(f"{tag}={(child.text or '').strip()}")
        sigs["; ".join(fields)] += 1
        for period in ts.iter():
            if period.tag.split("}")[-1] == "Period":
                res = period.find("{*}resolution")
                resolutions[res.text if res is not None else "?"] += 1
                pts = [p for p in period if p.tag.split("}")[-1] == "Point"]
                points += len(pts)
                if sample is None and pts:
                    sample = [{g.tag.split("}")[-1]: (g.text or "").strip() for g in p.iter() if not len(g)} for p in pts[:3]]
    return {"timeseries": sum(sigs.values()), "signatures": sigs.most_common(12),
            "resolutions": dict(resolutions), "points": points, "sample_points": sample}


def entsoe(name, params, token):
    for domain_name, eic in DOMAINS.items():
        query = {k: (v.replace("{D}", eic) if isinstance(v, str) else v) for k, v in params.items()}
        label = f"{name}__{domain_name}"
        pages = [0] if "offset" not in query else range(0, 1000, 100)
        for offset in pages:
            if "offset" in query:
                query["offset"] = offset
            try:
                response = requests.get(API_URL, params={**query, "securityToken": token}, timeout=180)
            except Exception as error:
                summary.append({"source": "entsoe", "name": label, "error": repr(error)})
                break
            time.sleep(0.7)
            tag = f"{label}__o{offset}" if "offset" in query else label
            ext = "zip" if response.content[:2] == b"PK" else "xml"
            if response.status_code != 200:
                text = response.text
                reason = text[text.find("<text>") + 6:text.find("</text>")] if "<text>" in text else text[:300]
                summary.append({"source": "entsoe", "name": tag, "status": response.status_code, "reason": reason})
                break
            save("entsoe", f"{tag}.{ext}", response.content)
            try:
                info = describe_entsoe(parse_response(response.content))
            except Exception as error:
                info = {"parse_error": repr(error)}
            summary.append({"source": "entsoe", "name": tag, "status": 200, **info})
            if "offset" in query and info.get("timeseries", 0) < 100:
                break


def run_entsoe():
    token = os.environ["ENTSOE_API_KEY"]
    period = {"periodStart": START, "periodEnd": END}
    day = {"periodStart": DAY_START, "periodEnd": DAY_END}
    entsoe("A85_imbalance_prices", {"documentType": "A85", "controlArea_Domain": "{D}", **period}, token)
    entsoe("A86_imbalance_volumes", {"documentType": "A86", "controlArea_Domain": "{D}", **period}, token)
    entsoe("A86_B33_balancing_state", {"documentType": "A86", "businessType": "B33", "area_Domain": "{D}",
                                       "curveType": "A03", **period}, token)
    entsoe("A84_prices_all", {"documentType": "A84", "processType": "A16", "controlArea_Domain": "{D}", **period}, token)
    for bt, product in (("A96", "aFRR"), ("A98", "RR"), ("A97", "mFRR")):
        entsoe(f"A84_prices_{product}", {"documentType": "A84", "processType": "A16", "businessType": bt,
                                         "controlArea_Domain": "{D}", **period}, token)
        entsoe(f"A83_volumes_{product}", {"documentType": "A83", "processType": "A16", "businessType": bt,
                                          "controlArea_Domain": "{D}", **period}, token)
    entsoe("A84_A67_cbmp_aFRR", {"documentType": "A84", "processType": "A67", "businessType": "A96",
                                 "Standard_MarketProduct": "A01", "controlArea_Domain": "{D}", **day}, token)
    for process, product in (("A51", "aFRR"), ("A46", "RR"), ("A47", "mFRR"), ("A67", "aFRR_CS")):
        entsoe(f"A24_aggregated_bids_{product}", {"documentType": "A24", "processType": process, "area_Domain": "{D}",
                                                  "curveType": "A03", **period}, token)
    for process, product in (("A51", "aFRR"), ("A46", "RR"), ("A47", "mFRR")):
        entsoe(f"A37_bids_{product}", {"documentType": "A37", "businessType": "B74", "processType": process,
                                       "connecting_Domain": "{D}", "offset": 0, **day}, token)
    for process, product in (("A51", "aFRR"), ("A46", "RR"), ("A47", "mFRR"), ("A52", "FCR")):
        entsoe(f"A15_procured_capacity_{product}", {"documentType": "A15", "processType": process, "area_Domain": "{D}",
                                                    "Type_MarketAgreement.Type": "A01", "offset": 0, **period}, token)
        entsoe(f"A81_contracted_{product}", {"documentType": "A81", "businessType": "B95", "processType": process,
                                             "Type_MarketAgreement.Type": "A01", "controlArea_Domain": "{D}",
                                             "offset": 0, **period}, token)
    for process, product in (("A63", "IN"), ("A67", "aFRR"), ("A60", "mFRR_sched"), ("A61", "mFRR_direct")):
        entsoe(f"B17_netted_exchanged_{product}", {"documentType": "B17", "processType": process,
                                                   "Acquiring_Domain": "{D}", "Connecting_Domain": "{D}", **day}, token)


def run_terna():
    client = Client(os.environ["TERNA_KEY"], os.environ["TERNA_SECRET"])
    dates = {"dateFrom": "22/09/2026", "dateTo": "29/09/2026"}
    calls = [
        ("preliminary_prices", "/fees/v1.0/preliminary-prices", {**dates, "dataType": "Quarto Orario"}),
        ("daily_prices", "/fees/v1.0/daily-prices", {**dates, "dataType": "Quarto Orario"}),
        ("daily_macrozonal_imbalance", "/fees/v1.0/daily-macrozonal-imbalance", {**dates, "dataType": "Quarto Orario"}),
        ("preliminary_macrozonal_imbalance", "/fees/v1.0/preliminary-macrozonal-imbalance", {**dates, "dataType": "Quarto Orario"}),
        ("commercial_balance_prices", "/fees/v1.0/commercial-balance-prices", dates),
        ("macrozonal_no_arbitrage_prices", "/fees/v1.0/macrozonal-no-arbitrage-prices", dates),
    ]
    for session in ("MSD1", "MSD2", "MB", "MB1", "MSD", "MBP"):
        calls.append((f"market_prices_{session}", "/market/v1.0/output/prices", {**dates, "sessionType": session}))
        calls.append((f"market_quantity_{session}", "/market/v1.0/output/quantity", {**dates, "sessionType": session}))
    for name, path, params in calls:
        try:
            data = client.get(path, params)
        except Exception as error:
            body = getattr(getattr(error, "response", None), "text", "")[:300]
            summary.append({"source": "terna", "name": name, "error": repr(error)[:200], "body": body})
            continue
        save("terna", f"{name}.json", json.dumps(data).encode())
        lists = {k: v for k, v in data.items() if isinstance(v, list)}
        info = {}
        for key, rows in lists.items():
            columns = defaultdict(Counter)
            for row in rows:
                for field, value in row.items():
                    if field not in ("reference_date", "publication_date", "reference_minute") and not any(
                            u in field for u in ("EURx", "_MWh", "price", "quantity", "value")):
                        columns[field][value] += 1
            info[key] = {"rows": len(rows), "first": rows[:2], "last": rows[-1:],
                         "categories": {f: dict(c.most_common(10)) for f, c in columns.items()}}
        summary.append({"source": "terna", "name": name, "result": data.get("result"), **info})


def gme_raw(token, segment, data_name, start, end):
    body = {"Platform": "PublicMarketResults", "Segment": segment, "DataName": data_name,
            "IntervalStart": start, "IntervalEnd": end, "Attributes": {}}
    for attempt in range(6):
        response = requests.post(DATA_URL, json=body, timeout=300,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        if response.status_code == 429:
            time.sleep(30 * (attempt + 1))
            continue
        response.raise_for_status()
        payload = response.json()
        content = payload.get("contentResponse") or payload.get("ContentResponse")
        if not content:
            raise RuntimeError(f"no content: {str(payload)[:300]}")
        return base64.b64decode(content)
    raise RuntimeError("rate limited")


def run_gme():
    token = get_token(os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"])
    calls = [("MSD", "ME_MSDExAnteResults", "20260922", "20260929"),
             ("MB", "ME_MBResults", "20260922", "20260929"),
             ("MSD", "ME_MSDExPostResults", "20260922", "20260929")]
    calls += [(segment, "Offers_PublicDomain", "20260922", "20260922")
              for segment in ("AFRR", "AFRE", "MRR", "MRRTerna", "MB", "MSD")]
    for segment, data_name, start, end in calls:
        name = f"{data_name}_{segment}"
        try:
            raw = gme_raw(token, segment, data_name, start, end)
        except Exception as error:
            summary.append({"source": "gme", "name": name, "error": repr(error)[:300]})
            time.sleep(20)
            continue
        save("gme", f"{name}.zip", raw)
        info = {}
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                info["files"] = [(n, archive.getinfo(n).file_size) for n in archive.namelist()][:10]
                first = archive.read(archive.namelist()[0])
                if first[:2] == b"PK":
                    with zipfile.ZipFile(io.BytesIO(first)) as inner:
                        info["inner_files"] = inner.namelist()[:10]
                        first = inner.read(inner.namelist()[0])
                info["head"] = first[:1500].decode("utf-8", "replace")
                if archive.namelist()[0].lower().endswith(".json"):
                    rows = json.loads(first)
                    rows = rows if isinstance(rows, list) else next(v for v in rows.values() if isinstance(v, list))
                    info["rows"] = len(rows)
                    info["zones"] = dict(Counter(r.get("Zone") for r in rows))
                    info["other"] = {k: dict(Counter(str(r.get(k)) for r in rows).most_common(8))
                                     for k in rows[0] if k in ("ServiceType", "Hour", "Period") }
        except Exception as error:
            info["parse_error"] = repr(error)
        summary.append({"source": "gme", "name": name, **info})
        time.sleep(20)


def main():
    os.makedirs(OUT, exist_ok=True)
    for part in sys.argv[1:] or ("entsoe", "terna", "gme"):
        try:
            {"entsoe": run_entsoe, "terna": run_terna, "gme": run_gme}[part]()
        except Exception:
            summary.append({"source": part, "fatal": traceback.format_exc()[-1500:]})
        with open(os.path.join(OUT, "summary.json"), "w") as f:
            json.dump(summary, f, indent=1, default=str)
    for item in summary:
        print(json.dumps(item, default=str)[:600])


if __name__ == "__main__":
    main()
