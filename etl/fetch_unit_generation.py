"""
Actual generation per generation unit (ENTSO-E 16.1.A, documentType A73,
processType A16): the metered output of each Italian unit of 100 MW or
more, per day (asked for Italy's control area: the API answers nothing
per bidding zone), written to
app/data/unit_generation/<date>.json.gz:

  {"date", "source", "quarters": N (96; 92 / 100 on the clock-change days),
   "units": [[generating unit EIC, name, psrType], ...],
   "values": [[MW per quarter-hour or null], ...]}   (one row per unit)

Quarter-hours are numbered by the time elapsed since local midnight, like
the GME periods (entsoe_api.point_label); an hourly point fills its hour's
four quarter-hours.  ENTSO-E publishes these weeks after the day, so a day
with no data at all is not written (it is asked again on the next run).

  fetch_unit_generation.py --from 2026-07-01 --to 2026-07-31

Credentials: ENTSOE_API_KEY, else asked for at the terminal (not shown).
"""

import argparse
import getpass
import gzip
import json
import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from entsoe_api import (REQUEST_PAUSE_SECONDS, label_minutes, local_midnight_utc, parse_points,  # noqa: E402
                        request_entsoe, to_api_datetime)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "app", "data", "unit_generation")
SOURCE = "ENTSO-E Transparency Platform, 16.1.A Actual Generation per Generation Unit (A73)"
CONTROL_AREA = "10YIT-GRTN-----B"


def day_quarters(day):
    return int((local_midnight_utc(day + timedelta(days=1)) - local_midnight_utc(day)).total_seconds() // 900)


def unit_key(ts):
    def text(path):
        node = ts.find(path)
        return node.text.strip() if node is not None and node.text else ""
    return "|".join([text(".//{*}MktPSRType/{*}PowerSystemResources/{*}mRID"),
                     text(".//{*}MktPSRType/{*}PowerSystemResources/{*}name"),
                     text(".//{*}MktPSRType/{*}psrType")])


def read_day(token, day):
    quarters = day_quarters(day)
    root = request_entsoe(token, {"documentType": "A73", "processType": "A16", "in_Domain": CONTROL_AREA,
                                  "periodStart": to_api_datetime(day),
                                  "periodEnd": to_api_datetime(day, end_of_day=True)})
    time.sleep(REQUEST_PAUSE_SECONDS)
    rows = {}
    for record in parse_points(root, group_of=unit_key):
        if record["date"] != day.isoformat():
            continue
        row = rows.setdefault(record["group"], [None] * quarters)
        # "minutes" is the point's resolution: an hourly point covers its hour's four quarter-hours.
        first, span = label_minutes(record["time"]) // 15, max(1, record["minutes"] // 15)
        for index in range(first, min(first + span, quarters)):
            row[index] = round(record["value"], 3)
    units = [key.split("|") for key in sorted(rows)]
    return {"date": day.isoformat(), "source": SOURCE, "quarters": quarters, "units": units,
            "values": [rows[key] for key in sorted(rows)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="first", required=True)
    parser.add_argument("--to", dest="last", required=True)
    parser.add_argument("--again", action="store_true", help="read days already on file again")
    parser.add_argument("--out", default=OUT_DIR)
    args = parser.parse_args()

    token = os.environ.get("ENTSOE_API_KEY") or (sys.stdin.isatty() and getpass.getpass("ENTSO-E API key: "))
    if not token:
        raise SystemExit("ENTSOE_API_KEY is not set")
    os.makedirs(args.out, exist_ok=True)
    day, last = date.fromisoformat(args.first), date.fromisoformat(args.last)
    while day <= last:
        path = os.path.join(args.out, f"{day}.json.gz")
        if os.path.exists(path) and not args.again:
            day += timedelta(days=1)
            continue
        try:
            data = read_day(token, day)
        except Exception as error:          # the message can hold the URL: no token in it
            print(f"  {day}: {str(error).replace(token, '***')}")
            day += timedelta(days=1)
            continue
        if data["units"]:
            payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
            with open(path, "wb") as handle:
                handle.write(gzip.compress(payload, compresslevel=9, mtime=0))
        filled = sum(1 for row in data["values"] for value in row if value is not None)
        print(f"  {day}: {len(data['units'])} units, {filled:,} quarter-hour values")
        day += timedelta(days=1)


if __name__ == "__main__":
    main()
