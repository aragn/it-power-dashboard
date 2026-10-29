"""
Stack model, phase 0: every revision of the Italian generation outages
(ENTSO-E 15.1.A/B, documentType A80), with the time each was published, so a
backtest can take a unit's availability as it was known on the morning
before the auction.  outages.json keeps only the latest revision.

ENTSO-E answers a query by period with the latest revision of each outage
(each mRID) only, and a query by mRID with all of them (probed 25 October
2026: 206 revisions of one hydro outage, one a day).  So: the outages of the
range, per zone and month, then the earlier revisions of those revised.

Written to app/data/stack/outage_revisions.json:

  units   code -> {zone, psr, nominal}
  outages mRID -> {unit, business (A53 planned, A54 forced), revisions:
          [[created, revision, status, [[start, end, MW available], ...]], ...]}
          (UTC, oldest revision first; status A09 cancelled, A13 withdrawn,
          null active)

Resumable: each zone's scan is saved as it ends, and the mRIDs whose
revisions are on file are not asked again unless the scan shows a newer
revision.  Progress goes to app/data/stack/outage_revisions.log too.

  fetch_outage_revisions.py --from 2025-06-01 --to 2026-10-31

For the live stack (the daily forecast job), each outage's latest revision
only, as ENTSO-E has it now, added to the file (no earlier revisions read):

  fetch_outage_revisions.py --latest [--from D --to D]   (default: tomorrow)

Credentials: ENTSOE_API_KEY, else asked for at the terminal (not shown).
"""

import argparse
import getpass
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from entsoe_api import REQUEST_PAUSE_SECONDS, local_midnight_utc, market_today, request_entsoe  # noqa: E402
from fetch_outages import PAGE_SIZE, WINDOW_DAYS, ZONES, parse_document  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "app", "data", "stack", "outage_revisions.json")
SAVE_EVERY = 200         # mRIDs between saves
MAX_WINDOW_DAYS = 365    # ENTSO-E answers at most a year a query


def documents(root):
    if root is None:
        return []
    items = [root] if root.tag.endswith("Unavailability_MarketDocument") else list(root)
    return [doc for doc in items if doc.tag.endswith("Unavailability_MarketDocument")]


def api_time(day):
    return local_midnight_utc(day).strftime("%Y%m%d%H%M")


def window_outages(token, eic, start, end):
    """The outages of a zone overlapping start..end (at most WINDOW_DAYS),
    parsed: a query by period answers with each outage's latest revision,
    in pages of PAGE_SIZE."""
    out, offset = [], 0
    while True:
        docs = documents(request_entsoe(token, {
            "documentType": "A80", "BiddingZone_Domain": eic, "offset": offset,
            "periodStart": api_time(start), "periodEnd": api_time(end + timedelta(days=1))}))
        out += [outage for outage in map(parse_document, docs) if outage]
        time.sleep(REQUEST_PAUSE_SECONDS)
        if len(docs) < PAGE_SIZE:
            return out
        offset += PAGE_SIZE


def windows(first, last):
    start = first
    while start <= last:
        end = min(start + timedelta(days=WINDOW_DAYS - 1), last)
        yield start, end
        start = end + timedelta(days=1)


def latest_per_mrid(token, first, last, data, path):
    """mRID -> (zone, latest revision) of the outages overlapping first..last;
    each zone's scan is saved in the output ("scan"), so a run stopped half
    way does not scan the zones it has done again."""
    scan = data.setdefault("scan", {})
    if scan.get("range") != [first.isoformat(), last.isoformat()]:
        scan.clear()
        scan["range"] = [first.isoformat(), last.isoformat()]
        scan["zones"] = {}
    for zone, eic in ZONES.items():
        if zone in scan["zones"]:
            continue
        found = {}
        for start, end in windows(first, last):
            for outage in window_outages(token, eic, start, end):
                if outage["revision"] > found.get(outage["mrid"], (None, -1))[1]:
                    found[outage["mrid"]] = (zone, outage["revision"])
            say(f"  {zone} {start} - {end}: {len(found):,} outages so far")
        scan["zones"][zone] = {mrid: revision for mrid, (_zone, revision) in found.items()}
        save(path, data)
    return {mrid: (zone, revision) for zone, found in scan["zones"].items() for mrid, revision in found.items()}


def revisions(token, mrid, zone, first, last):
    """Every revision of one outage, oldest first.  ENTSO-E takes at most a
    year a query: the range's first year, and, when the outage is not there
    and the range is longer, its last year."""
    end = last + timedelta(days=1)
    windows = [(first, min(first + timedelta(days=MAX_WINDOW_DAYS), end))]
    if end > windows[0][1]:
        windows.append((end - timedelta(days=MAX_WINDOW_DAYS), end))
    found = {}
    for number, (start, stop) in enumerate(windows):
        if found:
            break
        if number:
            time.sleep(REQUEST_PAUSE_SECONDS)
        # Pages of PAGE_SIZE documents: an outage revised daily has more
        # revisions than one answer holds (ENTSO-E refuses it without offset).
        offset = 0
        while True:
            docs = documents(request_entsoe(token, {
                "documentType": "A80", "BiddingZone_Domain": ZONES[zone], "mRID": mrid, "offset": offset,
                "periodStart": api_time(start), "periodEnd": api_time(stop)}))
            for outage in map(parse_document, docs):
                if outage:
                    found[outage["revision"]] = outage
            if len(docs) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            time.sleep(REQUEST_PAUSE_SECONDS)
    return [found[revision] for revision in sorted(found)]


LOG_PATH = os.path.join(os.path.dirname(OUT_PATH), "outage_revisions.log")


def say(message):
    """Print a progress line and add it to the log, the API key masked
    (an HTTP error's message holds the request's URL, key included)."""
    message = re.sub(r"securityToken=[^&\s]+", "securityToken=***", str(message))
    print(message, flush=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}" + chr(10))


def record(outage):
    runs = [[start.strftime("%Y-%m-%dT%H:%MZ"), end.strftime("%Y-%m-%dT%H:%MZ"), round(mw, 1)]
            for start, end, mw in outage["periods"]]
    return [outage["created"], outage["revision"], outage["status"], runs]


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {"source": "ENTSO-E Transparency Platform, 15.1.A/B (A80), every revision",
                "units": {}, "outages": {}}


def store(data, mrid, found):
    """An outage's revisions (oldest first) into data, its unit too."""
    newest = found[-1]
    data["units"].setdefault(newest["unit_name"] or newest["unit"],
                             {"zone": newest["zone"], "psr": newest["psr"], "nominal": newest["nominal"]})
    data["outages"][mrid] = {"unit": newest["unit_name"] or newest["unit"], "business": newest["business"],
                             "revisions": [record(outage) for outage in found]}


def add_latest(token, first, last, data):
    """Each outage overlapping first..last at its latest revision, as
    ENTSO-E has it now, added after the revisions on file (none read in
    between): what the live stack needs.  The zones are asked at the same
    time.  The number of outages."""
    def zone_latest(item):
        zone, eic = item
        latest = {}
        for start, end in windows(first, last):
            for outage in window_outages(token, eic, start, end):
                if outage["revision"] > latest.get(outage["mrid"], {"revision": -1})["revision"]:
                    latest[outage["mrid"]] = outage
        return zone, latest

    count = 0
    with ThreadPoolExecutor(max_workers=len(ZONES)) as pool:
        found = list(pool.map(zone_latest, ZONES.items()))
    for zone, latest in found:
        for mrid, outage in latest.items():
            item = data["outages"].get(mrid)
            if item is None:
                store(data, mrid, [outage])
            elif item["revisions"][-1][1] < outage["revision"]:
                item["revisions"].append(record(outage))
        say(f"  {zone} {first} - {last}: {len(latest):,} outages")
        count += len(latest)
    return count


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="first", help="first day (default: 2025-06-01; --latest: tomorrow)")
    parser.add_argument("--to", dest="last", help="last day (default: today; --latest: the first day)")
    parser.add_argument("--latest", action="store_true",
                        help="each outage's latest revision only, added to the file (the live stack)")
    parser.add_argument("--out", default=OUT_PATH)
    parser.add_argument("--workers", type=int, default=6, help="requests at a time")
    args = parser.parse_args()
    token = os.environ.get("ENTSOE_API_KEY") or (sys.stdin.isatty() and getpass.getpass("ENTSO-E API key: "))
    if not token:
        raise SystemExit("ENTSOE_API_KEY is not set")
    tomorrow = market_today() + timedelta(days=1)
    first = date.fromisoformat(args.first) if args.first else tomorrow if args.latest else date(2025, 6, 1)
    last = date.fromisoformat(args.last) if args.last else first if args.latest else date.today()

    data = load(args.out)
    if args.latest:
        count = add_latest(token, first, last, data)
        save(args.out, data)
        say(f"{args.out}: the latest revision of {count:,} outages in {first} - {last} added")
        return
    from fetch_stack_offers import keep_awake
    keep_awake()
    latest = latest_per_mrid(token, first, last, data, args.out)
    todo = [(mrid, zone, revision) for mrid, (zone, revision) in latest.items()
            if not data["outages"].get(mrid) or data["outages"][mrid]["revisions"][-1][1] < revision]
    say(f"{len(latest):,} outages in {first} - {last}, {len(todo):,} to read revision by revision")
    started = time.time()

    def read(item):
        mrid, zone, _revision = item
        try:
            return mrid, revisions(token, mrid, zone, first, last), None
        except Exception as error:   # left for the next run
            return mrid, None, error

    # A few requests at a time: ENTSO-E answers an outage's revisions in
    # seconds, and allows 400 requests a minute.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for count, (mrid, found, error) in enumerate(pool.map(read, todo), 1):
            if error is not None:
                say(f"  {mrid}: {type(error).__name__}: {error}")
            elif found:
                store(data, mrid, found)
            if count % SAVE_EVERY == 0:
                save(args.out, data)
                rate = (time.time() - started) / count
                say(f"  {count:,} of {len(todo):,} read, ~{(len(todo) - count) * rate / 60:.0f} min left")
    save(args.out, data)
    total = sum(len(item["revisions"]) for item in data["outages"].values())
    say(f"{args.out}: {len(data['outages']):,} outages, {total:,} revisions, {len(data['units']):,} units")


if __name__ == "__main__":
    main()
