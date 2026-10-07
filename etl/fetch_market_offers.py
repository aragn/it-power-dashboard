"""
GME public offers of the markets without a merit order: MSD ex-ante, MB
(balancing, pay-as-bid) and MI-XBID (continuous intraday), written to
app/data/market_offers/<MARKET>/<date>.json.gz, with the index of days,
quarter-hours and files, app/data/market_offers/index.json.

MSD and MB (Offers_PublicDomain, segments "MSD" and "MB"), per quarter-hour:
  offers   every energy offer (scopes GR1-GR4, AS, RS, in EUR/MWh) with MW,
           accepted or rejected, as [unit, direction, scope, MW offered,
           price, status, MW accepted, flags]: direction 0 up (the unit
           sells to Terna, PURPOSE_CD OFF), 1 down (it buys back, BID);
           status 0 accepted, 1 rejected; flags 1 standard product (STND)
  events   the accepted start-ups (ACC) and changes of mode (CA), priced
           in EUR per event: [unit, scope, price]
  Offers that did not stand (replaced, or only submitted) are left out.
  The MB file holds the accepted offers only, and comes out more than a
  month after the market (MSD's about a week after).

MI-XBID (segment "XBID"), the trades of each product: an order's fills
(STATUS_CD ACC) as [unit, side, MW, price, minutes before delivery]: side 0
sale (OFF), 1 purchase (BID); fills of an order at one price and minute
together.  "quarters": the 15-minute products, "hours": the hourly ones
(each covers its hour's four quarter-hours).  Orders never filled are left
out.

  fetch_market_offers.py --date 2026-09-17 [--markets MSD XBID]
  fetch_market_offers.py --from 2026-09-14 --to 2026-09-20 [--max-days 7]

Credentials: GME_API_LOGIN / GME_API_PASSWORD.
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_gme_units import infer_source, kind_of  # noqa: E402
from fetch_gme_units import REQUEST_PAUSE_SECONDS, get_token, number, request_offers, rows_of  # noqa: E402
from fetch_mgp_merit import PUBLISHED_AFTER, label, read_day, round_mw, unit_sources, write_day  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "app", "data", "market_offers")
ROME = ZoneInfo("Europe/Rome")

MARKETS = ["MSD", "MB", "XBID"]
SEGMENTS = {"MSD": "MSD", "MB": "MB", "XBID": "XBID"}
ENERGY_SCOPES = ["GR1", "GR2", "GR3", "GR4", "AS", "RS"]     # EUR/MWh
EVENT_SCOPES = ["ACC", "CA"]                                 # EUR per start-up / change of mode
STATUSES = {"ACC": 0, "REJ": 1}
STANDARD = 1
MAX_DAYS = 7


class Units:
    """A day file's unit list: code -> index; source, operator, zone, kind
    from the unit database (else from the code and the row)."""

    def __init__(self, sources):
        self.sources, self.index, self.list = sources, {}, []

    def of(self, row):
        code = row.get("UNIT_REFERENCE_NO") or ""
        if code not in self.index:
            source, operator, zone, kind = self.sources.get(code) or (None, None, None, None)
            if not source and kind is None:
                source = infer_source(code, {})[0]
            kind = kind or kind_of(code)
            if not source and kind in ("import", "export"):
                source = "interconnection"
            self.index[code] = len(self.list)
            self.list.append([code, source or None, operator or row.get("OPERATORE") or None,
                              zone or row.get("ZONE_CD") or None, kind])
        return self.index[code]


def period_of(row):
    """The quarter-hour (1-96) of an MSD row (PERIOD) or an MB row (hour
    INTERVAL_NO and QUARTER_NO in it)."""
    if row.get("PERIOD"):
        return int(number(row["PERIOD"]) or 0)
    hour, quarter = number(row.get("INTERVAL_NO")), number(row.get("QUARTER_NO"))
    return int((hour - 1) * 4 + quarter) if hour and quarter else 0


def build_balancing(rows, sources):
    """MSD or MB: the quarter-hours' energy offers and accepted events."""
    units = Units(sources)
    offers, events = defaultdict(list), defaultdict(list)
    for row in rows:
        period = period_of(row)
        if not 1 <= period <= 100:
            continue
        status, scope = row.get("STATUS_CD"), row.get("SCOPE")
        quantity = number(row.get("QUANTITY_NO")) or 0.0
        accepted = number(row.get("AWARDED_QUANTITY_NO")) or 0.0
        price = number(row.get("AWARDED_PRICE_NO") if status == "ACC" else row.get("ENERGY_PRICE_NO"))
        if scope in EVENT_SCOPES:
            if status == "ACC":
                events[period - 1].append([units.of(row), EVENT_SCOPES.index(scope), price])
            continue
        if scope not in ENERGY_SCOPES or status not in STATUSES or (quantity <= 0 and accepted <= 0):
            continue
        # MB's accepted offers can show 0 MW offered: at least what was taken.
        offers[period - 1].append([units.of(row), 0 if row.get("PURPOSE_CD") == "OFF" else 1,
                                   ENERGY_SCOPES.index(scope), round_mw(max(quantity, accepted)), price, STATUSES[status],
                                   round_mw(accepted) if status == "ACC" else 0,
                                   STANDARD if row.get("TYPE_CD") == "STND" else 0])
    quarters = []
    for index in sorted(set(offers) | set(events)):
        quarters.append({"time": label(index),
                         "offers": [value for record in offers[index] for value in record],
                         "events": [value for record in events[index] for value in record]})
    return {"units": units.list, "quarters": quarters}


def product_slot(product, day):
    """("quarter", index) or ("hour", index) of an XBID product of day
    (20260917-H02-QH05-NORD, 20260917-H02-SVIZ); None for another day's."""
    parts = product.split("-")
    if not parts or parts[0] != day.strftime("%Y%m%d"):
        return None
    quarter = next((part for part in parts if part.startswith("QH") and part[2:].isdigit()), None)
    if quarter:
        return "quarter", int(quarter[2:]) - 1
    hour = next((part for part in parts[1:] if part.startswith("H") and part[1:].isdigit()), None)
    return ("hour", int(hour[1:]) - 1) if hour else None


def delivery_start(day, kind, index):
    """The product's start as an aware time (elapsed time since midnight)."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=ROME)
    minutes = index * (15 if kind == "quarter" else 60)
    return (midnight.astimezone(ZoneInfo("UTC")) + timedelta(minutes=minutes))


def build_xbid(rows, day, sources):
    units = Units(sources)
    fills = {"quarter": defaultdict(lambda: defaultdict(float)), "hour": defaultdict(lambda: defaultdict(float))}
    for row in rows:
        if row.get("STATUS_CD") != "ACC":
            continue
        slot = product_slot(row.get("PRODOTTO") or "", day)
        mw = number(row.get("AWARDED_QUANTITY_NO")) or 0.0
        price = number(row.get("AWARDED_PRICE_NO"))
        if not slot or mw <= 0 or price is None:
            continue
        kind, index = slot
        try:
            stamp = datetime.fromisoformat(row["TIMESTAMP"])
        except (KeyError, ValueError):
            continue
        before = round((delivery_start(day, kind, index) - stamp).total_seconds() / 60)
        side = 0 if row.get("PURPOSE_CD") == "OFF" else 1
        fills[kind][index][(units.of(row), side, price, before)] += mw

    def flat(by_key):
        return [value for (unit, side, price, before), mw in sorted(by_key.items(), key=lambda item: item[0][3], reverse=True)
                for value in (unit, side, round_mw(mw), price, before)]

    return {"units": units.list,
            "quarters": [{"time": label(index), "fills": flat(fills["quarter"][index])} for index in sorted(fills["quarter"])],
            "hours": [{"time": f"{index:02d}:00", "fills": flat(fills["hour"][index])} for index in sorted(fills["hour"])]}


def day_data(market, day, rows, sources):
    data = build_xbid(rows, day, sources) if market == "XBID" else build_balancing(rows, sources)
    return {"date": day.isoformat(), "market": market,
            "source": f"GME public offers (Offers_PublicDomain, {SEGMENTS[market]}); unit sources from gme_units.json",
            **data}


def write_index(out_dir):
    """index.json: per market, the days on file with their quarter-hours
    (XBID: those with trades of a 15-minute or hourly product) and files."""
    index = {}
    for market in MARKETS:
        folder = os.path.join(out_dir, market)
        if not os.path.isdir(folder):
            continue
        days, files = {}, {}
        for name in sorted(os.listdir(folder)):
            if not name.endswith((".json", ".json.gz")):
                continue
            data = read_day(os.path.join(folder, name))
            times = {item["time"] for item in data.get("quarters", [])}
            for hour in data.get("hours", []):
                start = int(hour["time"][:2]) * 4
                times |= {label(start + offset) for offset in range(4)}
            days[data["date"]] = sorted(times)
            files[data["date"]] = f"{market}/{name}"
        index[market] = {"days": days, "files": files}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as handle:
        json.dump(index, handle, separators=(",", ":"))
    return index


def on_file(out_dir, market):
    """The days of a market on file with at least one quarter-hour."""
    path = os.path.join(out_dir, "index.json")
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as handle:
        days = json.load(handle).get(market, {}).get("days", {})
    return {day for day, times in days.items() if times}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="one day (else the range)")
    parser.add_argument("--from", dest="first", help="first day of the range")
    parser.add_argument("--to", dest="last", help=f"last day (default: {PUBLISHED_AFTER} days ago)")
    parser.add_argument("--markets", nargs="+", default=MARKETS, choices=MARKETS)
    parser.add_argument("--max-days", type=int, default=MAX_DAYS)
    parser.add_argument("--out", default=OUT_DIR)
    args = parser.parse_args()

    if args.date:
        days = [date.fromisoformat(args.date)]
    elif args.first:
        last = date.fromisoformat(args.last) if args.last else date.today() - timedelta(days=PUBLISHED_AFTER)
        days, day = [], date.fromisoformat(args.first)
        while day <= last:
            days.append(day)
            day += timedelta(days=1)
    else:
        parser.error("--date or --from")

    login, password = os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"]
    token, sources = get_token(login, password), unit_sources()
    read, asked = [], False
    for market in args.markets:
        have = set() if args.date else on_file(args.out, market)
        todo = [day for day in days if day.isoformat() not in have][:args.max_days]
        print(f"{market}: {len(todo)} day(s) to read")
        for day in todo:
            if asked:
                time.sleep(REQUEST_PAUSE_SECONDS)
            asked = True
            try:
                try:
                    name, content = request_offers(token, day, segment=SEGMENTS[market])
                except PermissionError:
                    token = get_token(login, password)
                    name, content = request_offers(token, day, segment=SEGMENTS[market])
            except Exception as error:  # not out yet, or a failure: the other days go on
                print(f"  {market} {day}: {error}")
                continue
            data = day_data(market, day, rows_of(name, content), sources)
            size = len(content) / 1e6
            del content
            path = write_day(os.path.join(args.out, market), data)
            slots = len(data["quarters"]) + len(data.get("hours", []))
            print(f"  {market} {day}: {name}, {size:.0f} MB, {slots} products/quarter-hours, "
                  f"{len(data['units']):,} units, {os.path.getsize(path) / 1e6:.1f} MB")
            read.append((market, day))
    index = write_index(args.out)
    message = (f"Market offers: {len(read)} file(s) read; on file: " +
               ", ".join(f"{market} {len(entry['days'])} day(s)" for market, entry in index.items()) + ".")
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    if args.date and not read:
        raise SystemExit("No offers read")


if __name__ == "__main__":
    main()
