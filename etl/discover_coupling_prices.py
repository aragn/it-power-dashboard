"""TEMPORARY: compare GME's "... Coupling" zone prices with the Italian side."""

import os
import time
from collections import defaultdict
from datetime import date

from fetch_zonal import get_token, request_chunk

token = get_token(os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"])

PAIRS = {"BSP": "COUP", "XAUS": "COUP", "XFRA": "COUP", "XGRE": "SUD"}

for start, end in (
    (date(2025, 3, 14), date(2025, 3, 15)),
    (date(2025, 9, 10), date(2025, 9, 10)),
    (date(2026, 9, 26), date(2026, 9, 28)),
):
    rows = request_chunk(token, start, end)
    prices = defaultdict(dict)
    for row in rows:
        if row.get("Price") is None:
            continue
        key = (str(row["FlowDate"]), int(row.get("Hour") or 0), int(float(row.get("Period") or 0)))
        prices[str(row["Zone"])][key] = float(row["Price"])

    print("=" * 70)
    print(start, "->", end, len(rows), "rows")
    for foreign, italian in PAIRS.items():
        keys = sorted(prices[foreign].keys() & prices[italian].keys())
        differ = [k for k in keys if abs(prices[foreign][k] - prices[italian][k]) > 0.005]
        differ_nord = [k for k in keys if abs(prices[foreign][k] - prices["NORD"].get(k, 1e9)) > 0.005]
        print(f"  {foreign} vs {italian}: {len(keys)} periods, differ in {len(differ)}; vs NORD differ in {len(differ_nord)}")
        for k in differ[:3]:
            print(f"    {k}: {foreign} {prices[foreign][k]} {italian} {prices[italian][k]}")

    # Hourly averages at a few hours, to compare with ENTSO-E locally.
    for zone in ("COUP", "NORD", "SUD", "BSP", "XAUS", "XFRA", "XGRE"):
        by_hour = defaultdict(list)
        for (day, hour, _), value in prices[zone].items():
            by_hour[(day, hour)].append(value)
        sample = {f"{day} h{hour}": round(sum(v) / len(v), 2)
                  for (day, hour), v in sorted(by_hour.items()) if hour in (1, 7, 13, 19)}
        print(f"  {zone}: {sample}")

    time.sleep(20)
