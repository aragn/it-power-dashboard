"""TEMPORARY: inspect the structure of GME ME_XBIDResults (public data)."""

import os
import time
from collections import Counter, defaultdict
from datetime import date

from fetch_zonal import get_token, request_chunk


def summarise(rows, label):
    print()
    print("=" * 70)
    print(label, "-", len(rows), "rows")
    print("=" * 70)
    if not rows:
        return
    print("first row:", rows[0])
    keys = sorted({k for r in rows for k in r})
    print("keys:", keys)
    for key in keys:
        values = Counter(str(r.get(key)) for r in rows)
        if len(values) <= 30:
            print(f"  {key}: {dict(sorted(values.items()))}")
        else:
            print(f"  {key}: {len(values)} distinct, e.g. {list(values)[:8]}")

    # Periods per (date, zone, phase, market-ish fields) to spot 60 vs 15 min.
    extra = [k for k in keys if k not in (
        "FlowDate", "Hour", "Period", "Zone", "Phase", "Purchased", "Sold", "ReferencePrice",
        "MinPrice", "MaxPrice", "FirstPrice", "LastPrice", "LastHourPrice")]
    print("extra keys:", extra)
    groups = defaultdict(list)
    for r in rows:
        groups[(r.get("FlowDate"), r.get("Zone"), r.get("Phase"), *[r.get(k) for k in extra])].append(r)
    for key in sorted(groups, key=str)[:40]:
        periods = sorted(int(r["Period"]) for r in groups[key])
        hours = sorted({int(r["Hour"]) for r in groups[key]})
        print(f"  {key}: {len(periods)} rows, periods {periods[0]}..{periods[-1]}, hours {hours[0]}..{hours[-1]}")


def nord_table(rows, flow_date):
    nord = [r for r in rows if str(r.get("Zone")).upper() == "NORD" and str(r.get("FlowDate")) == flow_date]
    nord.sort(key=lambda r: (str(r.get("Phase")), int(r["Hour"]), int(r["Period"])))
    print()
    print(f"NORD {flow_date}: {len(nord)} rows (Phase, Hour, Period, Ref, Min, Max, First, Last, Purchased, Sold, other)")
    for r in nord[:60] + (nord[-30:] if len(nord) > 90 else []):
        other = {k: v for k, v in r.items() if k not in (
            "FlowDate", "Zone", "Phase", "Hour", "Period", "ReferencePrice", "MinPrice", "MaxPrice",
            "FirstPrice", "LastPrice", "Purchased", "Sold", "LastHourPrice")}
        print(f"  {r.get('Phase')} h{r['Hour']:>2} p{r['Period']:>3}  ref {r.get('ReferencePrice')}  "
              f"min {r.get('MinPrice')} max {r.get('MaxPrice')} first {r.get('FirstPrice')} last {r.get('LastPrice')}  "
              f"buy {r.get('Purchased')} sell {r.get('Sold')}  {other}")


def main():
    token = get_token(os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"])

    recent = request_chunk(token, date(2026, 9, 27), date(2026, 9, 29), segment="XBID", data_name="ME_XBIDResults")
    summarise(recent, "XBID 2026-09-27 .. 2026-09-29")
    nord_table(recent, "20260928")
    today = [r for r in recent if str(r.get("FlowDate")) == "20260929"]
    print()
    print("today (20260929) rows:", len(today), "max hour:", max((int(r["Hour"]) for r in today), default=None))

    time.sleep(20)
    early = request_chunk(token, date(2025, 1, 1), date(2025, 1, 2), segment="XBID", data_name="ME_XBIDResults")
    summarise(early, "XBID 2025-01-01 .. 2025-01-02")

    time.sleep(20)
    dst = request_chunk(token, date(2025, 10, 26), date(2025, 10, 26), segment="XBID", data_name="ME_XBIDResults")
    summarise(dst, "XBID 2025-10-26 (25-hour day)")


if __name__ == "__main__":
    main()
