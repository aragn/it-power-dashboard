"""TEMPORARY: unit-level check of Terna's outage records against ENTSO-E 15.1."""

import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import fetch_outages
from fetch_terna import Client


def terna_records(client, start, end):
    records, day = [], start
    while day <= end:
        stop = min(day + timedelta(days=6), end)
        payload = client.get("/outages/v1.0/generation-unit-unavailability",
                             {"dateFrom": day.strftime("%d/%m/%Y"), "dateTo": stop.strftime("%d/%m/%Y")})
        records += payload.get("unavailability_productive_units") or []
        day = stop + timedelta(days=1)
    return records


def number(value):
    return float(str(value or 0).replace(",", "."))


def main():
    start, end = date(2026, 8, 15), date(2026, 9, 29)
    client = Client(os.environ["TERNA_KEY"], os.environ["TERNA_SECRET"])
    # Terna's feed only holds recently published outages; ask in one go.
    payload = client.get("/outages/v1.0/generation-unit-unavailability",
                         {"dateFrom": start.strftime("%d/%m/%Y"), "dateTo": end.strftime("%d/%m/%Y")})
    terna = payload.get("unavailability_productive_units") or []
    print(f"Terna: {len(terna)} records {start} -> {end}")
    print("  outage_type:", dict(Counter(r.get("outage_type") for r in terna)),
          " plant type:", dict(Counter(r.get("plan_type") or r.get("plant_type") for r in terna)),
          " zones:", dict(Counter(r.get("bidding_zone") or r.get("biddingZone") for r in terna)))

    entsoe = fetch_outages.download(os.environ["ENTSOE_API_KEY"], start, end)
    by_unit = {}
    for outage in entsoe:
        by_unit.setdefault(outage["unit_name"], []).append(outage)

    matched = mismatched = missing = 0
    for record in terna:
        unit = record.get("unit_name")
        t_start = datetime.strptime(record["start_date"][:16], "%Y-%m-%d %H:%M")
        t_stop = datetime.strptime(record["stop_date"][:16], "%Y-%m-%d %H:%M")
        t_mw = number(record.get("unavailable_capacity"))
        candidates = by_unit.get(unit, [])
        best = None
        for outage in candidates:
            for p_start, p_end, available in outage["periods"]:
                # Terna times are local; compare dates, allowing a day either side.
                if p_start.date() <= t_stop.date() + timedelta(days=1) and p_end.date() >= t_start.date() - timedelta(days=1):
                    mw = outage["nominal"] - available
                    if best is None or abs(mw - t_mw) < abs(best[0] - t_mw):
                        best = (mw, outage["business"], p_start, p_end)
        if not candidates:
            missing += 1
            status = "NOT IN ENTSO-E"
        elif best is None:
            mismatched += 1
            status = "in ENTSO-E, no overlapping period"
        elif abs(best[0] - t_mw) <= max(5, 0.05 * t_mw):
            matched += 1
            status = f"match {best[1]} {best[0]:.0f} MW {best[2]:%d/%m %H:%M}-{best[3]:%d/%m %H:%M}Z"
        else:
            mismatched += 1
            status = f"MW differ: ENTSO-E {best[0]:.0f} MW {best[1]} {best[2]:%d/%m %H:%M}-{best[3]:%d/%m %H:%M}Z"
        print(f"  {unit:18} {record.get('outage_type'):4} {t_start:%d/%m %H:%M}-{t_stop:%d/%m %H:%M} "
              f"{t_mw:7.1f} MW  -> {status}")
    print(f"matched {matched}, different {mismatched}, missing from ENTSO-E {missing} (of {len(terna)})")

    # Units unavailable at one instant, to spot the same unit counted twice.
    for when in (datetime(2026, 8, 20, 10), datetime(2026, 9, 27, 10)):
        moment = when.replace(tzinfo=fetch_outages.timezone.utc)
        rows = []
        for outage in entsoe:
            for p_start, p_end, available in outage["periods"]:
                if p_start <= moment < p_end and outage["nominal"] - available > 0:
                    rows.append((outage["unit_name"], outage["unit"], outage["zone"], outage["psr"], outage["business"],
                                 round(outage["nominal"] - available), outage.get("scaled", False), outage["mrid"]))
        print(f"\nUnavailable at {when:%Y-%m-%d %H:%M} UTC: {len(rows)} documents, "
              f"{sum(r[5] for r in rows):,} MW before the per-unit max")
        names = defaultdict(set)
        for r in rows:
            names[r[0]].add(r[1])
        print("  unit names with more than one unit id:", {n: sorted(i) for n, i in names.items() if len(i) > 1})
        for r in sorted(rows, key=lambda r: -r[5])[:40]:
            print("  ", r)
        totals = fetch_outages.quarter_totals(entsoe, moment, moment + fetch_outages.STEP)
        print("  total after per-unit max:", round(sum(v for g in totals.values() for v in g.values())), "MW")


if __name__ == "__main__":
    main()
