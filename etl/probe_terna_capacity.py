"""Temporary: what Terna's available capacity endpoints return (ranges, fields, categories)."""
import collections
import os
from datetime import date, timedelta

from fetch_terna import Client

client = Client(os.environ["TERNA_KEY"], os.environ["TERNA_SECRET"])
today = date.today()
fmt = lambda d: d.strftime("%d/%m/%Y")  # noqa: E731
cases = [
    ("aggregate-effective-available-capacity", today - timedelta(days=1), today - timedelta(days=1)),
    ("aggregate-effective-available-capacity", today - timedelta(days=31), today - timedelta(days=1)),
    ("aggregate-effective-available-capacity", date(2025, 1, 1), date(2025, 1, 7)),
    ("aggregate-effective-available-capacity", today, today + timedelta(days=2)),
    ("expected-available-capacity", today, today),
    ("expected-available-capacity", today, today + timedelta(days=14)),
    ("expected-available-capacity", today + timedelta(days=15), today + timedelta(days=60)),
    ("expected-available-capacity", today - timedelta(days=10), today - timedelta(days=9)),
    ("expected-available-capacity", date(2025, 1, 1), date(2025, 1, 2)),
]
for path, first, last in cases:
    print(f"\n== {path} {first} -> {last}")
    try:
        payload = client.get(f"/adequacy/v1.0/{path}", {"dateFrom": fmt(first), "dateTo": fmt(last)})
    except Exception as error:  # noqa: BLE001
        print("  ERROR", repr(error)[:300], getattr(getattr(error, "response", None), "text", "")[:300])
        continue
    keys = [k for k in payload if k != "result"]
    print("  keys", list(payload), payload.get("result"))
    rows = [r for k in keys if isinstance(payload[k], list) for r in payload[k]]
    print("  rows", len(rows))
    if not rows:
        continue
    print("  first", rows[0])
    dates = sorted(r.get("market_date", "") for r in rows)
    print("  market_date", dates[0], "->", dates[-1], "distinct hours", len(set(dates)))
    for field in ("macroarea", "plant_type", "prevailing_fuel", "date_tz"):
        print(f"  {field}", dict(collections.Counter(r.get(field) for r in rows)))
    if "macrouser_name" in rows[0]:
        print("  operators", len({r["macrouser_name"] for r in rows}))
    totals = collections.defaultdict(float)
    for r in rows:
        totals[r["market_date"]] += float(str(r["available_capacity_MW"]).replace(",", "."))
    for d in dates[:1] + dates[-1:]:
        print("  total MW at", d, round(totals[d]))
