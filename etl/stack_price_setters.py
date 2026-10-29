"""
Stack model, phase 0: who set each quarter-hour's MGP price in a zone, from
the merit order day files (app/data/mgp_merit, GME's public offers).

For a zone and quarter-hour, the price area is the zones with the same
price (Italian zones and, through the coupling, the neighbours whose
day-ahead price is the same).  The order at the margin is, in this order:

  supply        a sale offer in the area at the price, partly accepted
  hourly order  an hourly sale offer (PT60) at the hour's mean price, partly
                accepted (it is paid that mean, and moves all four prices)
  purchase      a purchase bid at the price, partly accepted (storage
                charging, pumping, exports, consumption: the merit files keep
                Italy's consumption bids summed by price, so no zone)
  step edge     a sale offer at the price fully accepted, or one at the
                price rejected: the price sits on the edge of a step
  abroad        none of these, and a coupled neighbour has the same price:
                set by an order outside Italy
  unexplained   none of these

Prints the shares per zone and, for the supply side, per source; with --by-hour
the same per hour of the day.

  stack_price_setters.py [--from 2026-08-01 --to 2026-10-17] [--zones NORD,SUD,SICI]
"""

import argparse
import gzip
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compact  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MERIT_DIR = os.path.join(ROOT, "app", "data", "mgp_merit")
NEIGHBOUR_PATH = os.path.join(ROOT, "app", "data", "neighbour_prices.json")
ITALY = ["NORD", "CNOR", "CSUD", "SUD", "CALA", "SICI", "SARD"]
COUPLED = ["FR", "AT", "SI", "GR"]
HOURLY = 1
TOLERANCE = 0.011        # EUR/MWh
MW_TOLERANCE = 0.05


def neighbour_prices():
    """(date, time, country) -> quarter-hourly day-ahead price."""
    data = compact.load(NEIGHBOUR_PATH)["prices"]["quarter_hourly"]
    return {(row["date"], row["time"], country): row["value"]
            for country, rows in data.items() if country in COUPLED for row in rows}


def classify(quarter, units, zone, hour_prices, abroad):
    prices = quarter["prices"]
    price = prices.get(zone)
    if price is None:
        return None, None
    area = {z for z in ITALY if prices.get(z) is not None and abs(prices[z] - price) < TOLERANCE}
    supply = quarter["supply"]
    edge = False
    best = None
    for i in range(0, len(supply), 7):
        unit, mw, offer, status, accepted, _awarded, flags = supply[i:i + 7]
        if units[unit][3] not in area:
            continue
        hourly = flags & HOURLY
        target = hour_prices.get(units[unit][3]) if hourly else price
        if target is None or abs(offer - target) >= TOLERANCE:
            continue
        if MW_TOLERANCE < accepted < mw - MW_TOLERANCE:
            kind = "hourly order" if hourly else "supply"
            if best is None or (kind == "supply" and best[0] != "supply") or (kind == best[0] and mw > best[2]):
                best = (kind, units[unit][1] or units[unit][4] or "not known", mw)
        elif not hourly:
            edge = True
    if best:
        return best[0], best[1]
    demand = quarter["demand"]
    for i in range(0, len(demand), 3):
        bid, accepted, rest = demand[i:i + 3]
        if abs(bid - price) < TOLERANCE and accepted > MW_TOLERANCE and rest > MW_TOLERANCE:
            return "purchase", None
    if edge:
        return "step edge", None
    if abroad:
        return "abroad", None
    return "unexplained", None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="first", default="2026-08-01")
    parser.add_argument("--to", dest="last", default="9999")
    parser.add_argument("--zones", default="NORD,CSUD,SUD,SICI,SARD")
    parser.add_argument("--by-hour", action="store_true")
    args = parser.parse_args()
    zones = args.zones.split(",")
    foreign = neighbour_prices()
    days = sorted(name[:10] for name in os.listdir(MERIT_DIR)
                  if name.endswith(".json.gz") and args.first <= name[:10] <= args.last)
    counts = {zone: Counter() for zone in zones}
    sources = {zone: Counter() for zone in zones}
    by_hour = {zone: defaultdict(Counter) for zone in zones}
    total = 0
    for day in days:
        with gzip.open(os.path.join(MERIT_DIR, f"{day}.json.gz"), "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        units = data["units"]
        quarters = data["quarters"]
        # Each hour's mean zonal price (the hourly orders are paid it).
        hour_means = defaultdict(lambda: defaultdict(list))
        for quarter in quarters:
            for zone, value in quarter["prices"].items():
                hour_means[(quarter["period"] - 1) // 4][zone].append(value)
        for quarter in quarters:
            total += 1
            hour = (quarter["period"] - 1) // 4
            hour_prices = {zone: sum(values) / len(values) for zone, values in hour_means[hour].items()}
            for zone in zones:
                price = quarter["prices"].get(zone)
                abroad = price is not None and any(
                    (value := foreign.get((day, quarter["time"].rstrip("*"), country))) is not None
                    and abs(value - price) < TOLERANCE for country in COUPLED)
                kind, source = classify(quarter, units, zone, hour_prices, abroad)
                if kind is None:
                    continue
                counts[zone][kind] += 1
                by_hour[zone][hour][kind] += 1
                if source:
                    sources[zone][source] += 1
    print(f"{len(days)} days {days[0]} - {days[-1]}, {total} quarter-hours")
    order = ["supply", "hourly order", "purchase", "step edge", "abroad", "unexplained"]
    for zone in zones:
        n = sum(counts[zone].values())
        print(f"{zone}: " + ", ".join(f"{kind} {100 * counts[zone][kind] / n:.0f}%" for kind in order))
        marginal = sum(sources[zone].values())
        print("   supply and hourly orders by source: " +
              ", ".join(f"{source} {100 * count / marginal:.0f}%" for source, count in sources[zone].most_common(8)))
        if args.by_hour:
            for hour in sorted(by_hour[zone]):
                row = by_hour[zone][hour]
                m = sum(row.values())
                print(f"   {hour:02d}h " + " ".join(f"{kind[:6]} {100 * row[kind] / m:3.0f}%" for kind in order))


if __name__ == "__main__":
    main()
