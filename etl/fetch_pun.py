"""
etl/fetch_pun.py

Pulls the PUN Index GME (national single price) from the GME API and writes
app/data/pun.json in the schema the frontend expects: [{"date": "YYYY-MM-DD", "pun": float}, ...]

GME API reference (GME's API Service - Technical Guide, 2025-10-15):
  Endpoint:        https://api.mercatoelettrico.org/request
  Auth:            POST /api/v1/Auth          {"Login": ..., "Password": ...} -> JWT
  Data request:    POST /api/v1/RequestData   Bearer <JWT>
  DataName used:   ME_ZonalPrices
  Segment used:    MGP
  Response fields: FlowDate (yyyyMMdd), Hour (1-25), Market, Zone, Price, Period
                    -> the PUN row is the one where Zone == "PUN"
  Response is returned as base64-encoded .zip containing a .json file.

Credentials come from environment variables (see .env.example):
  GME_API_LOGIN, GME_API_PASSWORD

Usage:
  python etl/fetch_pun.py --start 20240101 --end 20261231
"""
import argparse
import base64
import io
import json
import os
import zipfile
from collections import defaultdict
from datetime import datetime

import requests

API_BASE = "https://api.mercatoelettrico.org/request"
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "data", "pun.json")


def get_token(login: str, password: str) -> str:
    resp = requests.post(f"{API_BASE}/api/v1/Auth", json={"Login": login, "Password": password})
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"GME auth failed. Response from GME: {payload}")
    return payload["token"]


def request_data(token: str, interval_start: str, interval_end: str) -> list:
    """Calls ME_ZonalPrices / MGP for the given yyyyMMdd interval and returns the parsed rows."""
    body = {
        "Platform": "PublicMarketResults",
        "Segment": "MGP",
        "DataName": "ME_ZonalPrices",
        "IntervalStart": interval_start,
        "IntervalEnd": interval_end,
        "Attributes": {},
    }
    resp = requests.post(
        f"{API_BASE}/api/v1/RequestData",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    resp.raise_for_status()
    payload = resp.json()

# GME currently returns these fields in lowercase
    result_request = payload.get("resultRequest", payload.get("ResultRequest"))
    content_response = payload.get("contentResponse", payload.get("ContentResponse"))

    if result_request and result_request != "OK":
        raise RuntimeError(f"GME data request failed. Response: {payload}")

    if not content_response:
    # Do not print the token or other sensitive data
        safe_payload = {k: v for k, v in payload.items() if k.lower() != "token"}
        raise RuntimeError(f"GME returned no data content. Response: {safe_payload}")

    raw_zip = base64.b64decode(content_response)
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        json_name = next(n for n in zf.namelist() if n.endswith(".json"))
        with zf.open(json_name) as f:
            return json.load(f)


def to_daily_pun(rows: list) -> list:
    """Filters to Zone == 'PUN' rows and averages the 24 hourly prices into one daily value."""
    by_day = defaultdict(list)
    for row in rows:
        if row.get("Zone") != "PUN":
            continue
        flow_date = str(row["FlowDate"])
        by_day[flow_date].append(float(row["Price"]))

    out = []
    for flow_date, prices in sorted(by_day.items()):
        dt = datetime.strptime(flow_date, "%Y%m%d").date().isoformat()
        out.append({"date": dt, "pun": round(sum(prices) / len(prices), 2)})
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="yyyyMMdd")
    parser.add_argument("--end", required=True, help="yyyyMMdd")
    args = parser.parse_args()

    login = os.environ["GME_API_LOGIN"]
    password = os.environ["GME_API_PASSWORD"]

    token = get_token(login, password)
    rows = request_data(token, args.start, args.end)
    daily = to_daily_pun(rows)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"source": "GME - PUN Index GME (MGP)", "series": daily}, f, ensure_ascii=False)

    print(f"Wrote {len(daily)} daily PUN points to {OUT_PATH}")


if __name__ == "__main__":
    main()
