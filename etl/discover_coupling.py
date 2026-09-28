"""TEMPORARY: print what GME returns for market coupling and zonal prices."""

import os
import time
from collections import Counter, defaultdict
from datetime import date

from fetch_zonal import get_token, request_chunk

token = get_token(os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"])

for data_name, day in (
    ("ME_MarketCoupling", date(2026, 9, 27)),
    ("ME_MarketCoupling", date(2025, 3, 15)),
    ("ME_ZonalPrices", date(2026, 9, 27)),
):
    rows = request_chunk(token, day, day, data_name=data_name)
    print("=" * 70)
    print(data_name, day, len(rows), "rows")
    if not rows:
        continue
    print("fields:", list(rows[0].keys()))
    zones = Counter(str(row.get("Zone")) for row in rows)
    print("zones:", dict(zones))

    if data_name == "ME_MarketCoupling":
        for zone in zones:
            sample = [row for row in rows if str(row.get("Zone")) == zone]
            periods = sorted({int(row["Period"]) for row in sample})
            print(f"  {zone}: periods {periods[0]}..{periods[-1]} ({len(periods)})")
            for row in sample[:2] + sample[40:42]:
                print("   ", row)
            for field in ("ImportLimit", "ExportLimit", "ImportFlow", "ExportFlow"):
                values = [float(row[field]) for row in sample if row.get(field) is not None]
                if values:
                    print(f"    {field}: avg {sum(values) / len(values):.1f} min {min(values)} max {max(values)}")
    else:
        prices = defaultdict(list)
        for row in rows:
            if row.get("Price") is not None:
                prices[str(row["Zone"])].append(float(row["Price"]))
        periods = Counter(str(row.get("Zone")) for row in rows)
        for zone, values in sorted(prices.items()):
            print(f"  {zone}: {len(values)} periods, daily average {sum(values) / len(values):.2f}")
        print("  zones without prices:", sorted(set(periods) - set(prices)))

    time.sleep(20)
