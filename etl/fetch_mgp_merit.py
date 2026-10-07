"""
MGP merit order: the days' public offers (GME Offers_PublicDomain, MGP),
written to app/data/mgp_merit/<date>.json.gz (gzipped: a full day is about
6 MB of JSON, 1.4 MB gzipped) with the index of days, quarter-hours and
files, app/data/mgp_merit/index.json.

Per quarter-hour:
  prices   the zonal price (AWARDED_PRICE_NO of the accepted PT15 offers)
  supply   every sale offer (OFF) that stood at the close: accepted (ACC),
           rejected (REJ) or paradoxically rejected block (PREJ), as
           [unit, MW, bid price, status, MW accepted, price awarded, flags]
  demand   the purchase bids (BID) the same way, grouped by bid price:
           [price, MW accepted, MW not accepted]
  purchases  the accepted purchase bids per unit (consumption units, storage
           and pumping buying, units buying back), as [unit, MW accepted,
           price awarded, flags] (since FORMAT 2)
  others   the sale offers that did not stand, per unit and status:
           [unit, status, MW], status 3 replaced (REP), 4 revoked (REV),
           5 invalid (INC); with "supply", the MW offered by status (the page
           totals them by the unit's source as the database has it now)

Hourly orders (GRANULARITY PT60) are still allowed in the quarter-hourly
MGP: the same MW in the hour's four quarter-hours, cleared at the average
of their four prices (their AWARDED_PRICE_NO).  They go into each of the
four quarter-hours with that price, flagged.

The unit sources come from the market-unit database (gme_units.json).

Backfill and daily update (no arguments, or --from/--to): the days from
BACKFILL_FROM up to PUBLISHED_AFTER days ago not on file yet as full days,
oldest first, at most MAX_DAYS a run, one request at a time with a pause
between, so the GME API is not loaded.  GME publishes a day's offers about
8 days after the market: the first day not out yet ends the run (the later
ones are not out either), so once the range is filled a run reads the new
day, or asks once and stops.

  fetch_mgp_merit.py [--from 2026-08-01 --to 2026-09-30 --max-days 8]
  fetch_mgp_merit.py --date 2026-09-17 [--start 11:00 --end 19:45]
      [--dump rows.csv.gz]   also write the offer rows read
      [--rows rows.csv.gz]   read the rows from such a file, not from GME

Credentials: GME_API_LOGIN / GME_API_PASSWORD.
"""

import argparse
import csv
import gzip
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_gme_units import REQUEST_PAUSE_SECONDS, get_token, number, request_offers, rows_of  # noqa: E402
from build_gme_units import infer_source, kind_of  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "app", "data", "mgp_merit")
UNITS_PATH = os.path.join(ROOT, "app", "data", "gme_units.json")

STANDING = {"ACC": 0, "REJ": 1, "PREJ": 2}          # status codes in "supply"
OTHERS = {"REP": 3, "REV": 4, "INC": 5}             # status codes in "others"
STATUSES = ["ACC", "REJ", "PREJ", "REP", "REV", "INC"]
HOURLY, BILATERAL, BLOCK = 1, 2, 4                  # flags in "supply"
ZONES = ["NORD", "CNOR", "CSUD", "SUD", "CALA", "SICI", "SARD"]

BACKFILL_FROM = "2026-08-01"   # the first day the scheduled runs fill in
PUBLISHED_AFTER = 7            # days after the market before GME may have the offers out
MAX_DAYS = 8                   # days a run (each file is 500-650 MB)
COMPLETE = 92                  # quarter-hours of a full day (92 on the spring DST day)
FORMAT = 2                     # day files of an older format are read again (2: purchases per unit)


def period_of(row):
    return int(number(row.get("PERIOD") or row.get("INTERVAL_NO")) or 0)


def quarters_of(row):
    """The day's quarter-hours (0-95) a row covers: one for PT15, the
    hour's four for PT60 (period = hour 1-24)."""
    period = period_of(row)
    if row.get("GRANULARITY") == "PT60":
        return range((period - 1) * 4, period * 4)
    return range(period - 1, period)


def quarter(text):
    hours, minutes = (int(part) for part in text.split(":"))
    return hours * 4 + minutes // 15


def label(index):
    return f"{index // 4:02d}:{index % 4 * 15:02d}"


def round_mw(value):
    return round(value, 3)


def unit_sources(path=UNITS_PATH):
    """code -> (source, operator, zone, kind) from the unit database."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        print(f"  {path} missing: sources read from the codes only")
        return {}
    columns = data["columns"]
    units = {}
    for values in data["units"]:
        row = dict(zip(columns, values))
        entry = (row.get("source"), row.get("operator"), row.get("zone"), row.get("kind"))
        units[row["code"]] = entry
        for code in row.get("codes") or []:
            units.setdefault(code, entry)
    return units


def build(rows, first, last, units, registry=None):
    """The day's merit order data for the quarter-hours first..last.
    registry: (code -> index, unit list) shared by several markets' builds
    (the MI-A auctions of a day share one unit list); else a new one."""
    wanted = set(range(first, last + 1))
    prices = defaultdict(Counter)                     # (quarter, zone) -> awarded prices
    supply = defaultdict(list)
    demand = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    others = defaultdict(lambda: defaultdict(float))  # quarter -> (unit, status) -> MW
    purchases = defaultdict(list)
    unit_index, unit_list = registry if registry is not None else ({}, [])

    def unit_of(row):
        code = row.get("UNIT_REFERENCE_NO") or ""
        if code not in unit_index:
            source, operator, zone, kind = units.get(code) or (None, None, None, None)
            if not source and kind is None:
                source = infer_source(code, {})[0]
            kind = kind or kind_of(code)
            if not source and kind in ("import", "export"):
                source = "interconnection"     # virtual units (UPV, UCV), e.g. the TSOs' in the MI-A coupling
            unit_index[code] = len(unit_list)
            unit_list.append([code, source or None, operator or row.get("OPERATORE") or None,
                              zone or row.get("ZONE_CD") or None, kind])
        return unit_index[code]

    for row in rows:
        covered = [index for index in quarters_of(row) if index in wanted]
        if not covered:
            continue
        code_status = row.get("STATUS_CD")
        purpose = row.get("PURPOSE_CD")
        quantity = number(row.get("QUANTITY_NO")) or 0.0
        awarded = number(row.get("AWARDED_QUANTITY_NO")) or 0.0
        price = number(row.get("ENERGY_PRICE_NO"))
        awarded_price = number(row.get("AWARDED_PRICE_NO"))
        hourly = row.get("GRANULARITY") == "PT60"
        if code_status == "ACC" and not hourly and awarded_price is not None:
            prices[(covered[0], row.get("ZONE_CD"))][awarded_price] += 1
        if purpose == "OFF":
            unit = unit_of(row)
            if code_status not in STANDING:
                if code_status in OTHERS:
                    for index in covered:
                        others[index][(unit, OTHERS[code_status])] += quantity
                continue
            flags = (HOURLY if hourly else 0) | (BILATERAL if row.get("BILATERAL_IN") == "true" else 0) | \
                (BLOCK if row.get("OFFER_TYPE") == "B" else 0)
            record = [unit, round_mw(quantity), price, STANDING[code_status],
                      round_mw(awarded) if code_status == "ACC" else 0,
                      awarded_price if code_status == "ACC" else None, flags]
            for index in covered:
                supply[index].append(record)
        elif purpose == "BID" and code_status in STANDING:
            accepted = awarded if code_status == "ACC" else 0.0
            for index in covered:
                step = demand[index][price if price is not None else 0.0]
                step[0] += accepted
                step[1] += quantity - accepted
            if accepted > 0:
                record = [unit_of(row), round_mw(accepted), awarded_price,
                          (HOURLY if hourly else 0) | (BILATERAL if row.get("BILATERAL_IN") == "true" else 0)]
                for index in covered:
                    purchases[index].append(record)

    quarters = []
    for index in sorted(wanted):
        if not supply[index] and not demand[index]:
            continue
        zone_prices = {}
        for zone in ZONES + ["SVIZ", "FRAN", "MONT", "MALT", "COAC", "CORS"]:
            counts = prices.get((index, zone))
            if counts:
                zone_prices[zone] = counts.most_common(1)[0][0]
        steps = sorted(demand[index].items(), key=lambda item: -item[0])
        quarters.append({
            "time": label(index),
            "prices": zone_prices,
            "supply": [value for record in supply[index] for value in record],
            "demand": [value for bid_price, (accepted, rest) in steps
                       for value in (bid_price, round_mw(accepted), round_mw(rest))],
            "others": [value for (unit, code), mw in sorted(others[index].items())
                       for value in (unit, code, round_mw(mw))],
            "purchases": [value for record in purchases[index] for value in record],
        })
    return {"units": unit_list, "quarters": quarters}


def read_csv(path):
    with gzip.open(path, "rt", newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)


def dumped(rows, path, first, last):
    """Pass the rows on, writing those of the quarter-hours to a CSV."""
    wanted = set(range(first, last + 1))
    with gzip.open(path, "wt", newline="", encoding="utf-8") as handle:
        writer = None
        for row in rows:
            if writer is None:
                writer = csv.DictWriter(handle, fieldnames=list(row.keys()), extrasaction="ignore")
                writer.writeheader()
            if any(index in wanted for index in quarters_of(row)):
                writer.writerow(row)
            yield row


def write_day(out_dir, data):
    """The day's file, gzipped (the same bytes for the same data, so an
    unchanged day is not published again); a plain .json of it goes."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{data['date']}.json.gz")
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(gzip.compress(payload, compresslevel=9, mtime=0))
    plain = os.path.join(out_dir, f"{data['date']}.json")
    if os.path.exists(plain):
        os.remove(plain)
    return path


def read_day(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def write_index(out_dir):
    """index.json: the days on file, their quarter-hours, file names and
    formats."""
    days, files, versions = {}, {}, {}
    for name in sorted(os.listdir(out_dir)):
        if name in ("index.json", "units.json") or not name.endswith((".json", ".json.gz")):
            continue
        data = read_day(os.path.join(out_dir, name))
        days[data["date"]] = [item["time"] for item in data["quarters"]]
        files[data["date"]] = name
        versions[data["date"]] = data.get("version", 1)
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as handle:
        json.dump({"days": days, "files": files, "versions": versions}, handle, separators=(",", ":"))
    return days


def write_units_map(out_dir, units):
    """units.json: each code's source, operator and zone from the unit
    database as it is now; the page takes them over the ones in the day
    files (written when the day was read), so research reaches every day."""
    os.makedirs(out_dir, exist_ok=True)
    data = {code: [source, operator, zone] for code, (source, operator, zone, _kind) in sorted(units.items())}
    with open(os.path.join(out_dir, "units.json"), "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"), ensure_ascii=False)


def missing_days(first, last, out_dir):
    """The days of first..last without a full day of this FORMAT on file."""
    on_file, versions = {}, {}
    index = os.path.join(out_dir, "index.json")
    if os.path.exists(index):
        with open(index, encoding="utf-8") as handle:
            data = json.load(handle)
        on_file, versions = data.get("days", {}), data.get("versions", {})
    days, day = [], first
    while day <= last:
        if len(on_file.get(day.isoformat(), [])) < COMPLETE or versions.get(day.isoformat(), 1) < FORMAT:
            days.append(day)
        day += timedelta(days=1)
    return days


def day_data(day, rows, first, last, units):
    data = build(rows, first, last, units)
    return {"date": day.isoformat(), "market": "MGP", "version": FORMAT,
            "source": "GME public offers (Offers_PublicDomain, MGP); unit sources from gme_units.json", **data}


def backfill(first, last, out_dir, max_days):
    days = missing_days(first, last, out_dir)
    print(f"{first} - {last}: {len(days)} day(s) to read, at most {max_days} this run")
    done = []
    if days:
        login, password = os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"]
        token, units = get_token(login, password), unit_sources()
        for index, day in enumerate(days):
            if len(done) >= max_days:
                break
            if index:
                time.sleep(REQUEST_PAUSE_SECONDS)
            try:
                try:
                    name, content = request_offers(token, day)
                except PermissionError:
                    token = get_token(login, password)
                    name, content = request_offers(token, day)
            except Exception as error:
                print(f"  {day}: {error}")
                if str(error).startswith("No data"):
                    break       # not published yet: nor are the days after it
                continue        # another failure must not stop the other days
            # The day's 96 quarter-hours.  TODO: the autumn DST day has 100
            # (02:00-03:00 twice); its later labels would be an hour off.
            data = day_data(day, rows_of(name, content), 0, 95, units)
            del content
            path = write_day(out_dir, data)
            print(f"  {day}: {name}, {len(data['quarters'])} quarter-hours, {len(data['units']):,} units, "
                  f"{os.path.getsize(path) / 1e6:.1f} MB")
            done.append(day)
    on_file = write_index(out_dir) if os.path.isdir(out_dir) else {}
    left = len(days) - len(done)
    message = (f"MGP merit order: {len(done)} day(s) read, {left} of {first} - {last} still to read; "
               f"{len(on_file)} day(s) on file.")
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(message + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="one day (else the backfill range)")
    parser.add_argument("--start", default="00:00", help="first quarter-hour of --date (local time)")
    parser.add_argument("--end", default="23:45", help="last quarter-hour of --date (local time)")
    parser.add_argument("--from", dest="first", default=BACKFILL_FROM)
    parser.add_argument("--to", dest="last", help=f"last day (default: {PUBLISHED_AFTER} days ago)")
    parser.add_argument("--max-days", type=int, default=MAX_DAYS)
    parser.add_argument("--dump", help="also write the offer rows of the quarter-hours to this .csv.gz")
    parser.add_argument("--rows", help="read the rows from a .csv.gz written by --dump")
    parser.add_argument("--out", default=OUT_DIR)
    args = parser.parse_args()

    if not args.date:
        last = date.fromisoformat(args.last) if args.last else date.today() - timedelta(days=PUBLISHED_AFTER)
        backfill(date.fromisoformat(args.first), last, args.out, args.max_days)
        return

    day = date.fromisoformat(args.date)
    first, last = quarter(args.start), quarter(args.end)
    if args.rows:
        rows = read_csv(args.rows)
    else:
        token = get_token(os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"])
        name, content = request_offers(token, day)
        print(f"{day}: {name}, {len(content) / 1e6:.0f} MB")
        rows = rows_of(name, content)
    if args.dump:
        rows = dumped(rows, args.dump, first, last)

    data = day_data(day, rows, first, last, unit_sources())
    if not data["quarters"]:
        raise SystemExit(f"No offers for {day} {args.start}-{args.end}")
    path = write_day(args.out, data)
    days = write_index(args.out)
    print(f"{path}: {len(data['quarters'])} quarter-hours, {len(data['units'])} units, "
          f"{os.path.getsize(path) / 1e6:.1f} MB; {len(days)} day(s) on file")


if __name__ == "__main__":
    main()
