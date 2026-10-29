"""
Stack model inputs: the MGP's offers and bids as curves period by period,
from GME's public offers (Offers_PublicDomain, MGP), a file a day:
app/data/stack/offers/<date>.json.gz, with app/data/stack/offers/index.json.

The stack model (STACK_MODEL_PLAN.md) learns each unit's offers from these
and rebuilds the auction from them.  Periods are the MGP's own: quarter-hours
from 1 October 2025 (92, 96 or 100 a day), hours before (23, 24 or 25), by
elapsed time from local midnight as in the rest of the dashboard
("period_minutes" says which).  An hourly order (PT60) in a quarter-hourly
day is in each of its hour's four quarter-hours, flagged.  Format 1 (25-26
Oct 2026) averaged quarter-hours into hours ("hour" columns), format 2 had
no block ids; both are read again.

Only the offers that stood at the close count: accepted (ACC), rejected
(REJ) and paradoxically rejected blocks (PREJ); replaced (REP), revoked
(REV) and invalid (INC) ones would repeat the same MW.

A day's file, each table a list per column, rows sorted:

  units    [code, kind, zone, operator]: the units in the day's offers; the
           kind from the unit database (gme_units.json), else from the code;
           the operator GME names most often, "Bilateralista" (the
           schedules of bilateral contracts) only when it is the only one
  prices   zone -> each period's MGP price (EUR/MWh; Italian zones and the
           foreign virtual zones)
  offers   the sale offers, one step per unit, period, price and flags:
           unit, period, price, mw, accepted (MW)
  bids     the purchase bids of the units whose bids are kept one by one
           (storage charging, pumping, production units buying back,
           storage aggregates): the same columns
  demand   the other purchase bids (consumption units and aggregates, the
           virtual units of the foreign zones) per zone, period and price:
           zone, period, price, mw, accepted
  block    in offers and bids: the block order a step belongs to (1.., an
           index into "blocks"), 0 for a simple order
  blocks   [unit, GME's BLOCK_ID, minimum acceptance ratio] of each block
           order (a ratio of 1: accepted whole or not at all)
  flags    1 an hourly order in a quarter-hourly day, 2 bilateral, 4 block

The days of a range not on file yet, oldest first, one request at a time
with a pause, GME's offers being out about 8 days after the market:

  fetch_stack_offers.py --from 2025-07-01 --to 2026-10-21 [--max-days N]
      [--dates 2025-07-01,2026-10-15]   these days first (checks)

A day that fails (a dropped connection, the laptop asleep) is left for the
next run, which reads only the days still missing.

The daily job (update_gme_offers.py --stack-keep 45) writes the same files
from the offers it downloads anyway, for the live stack: only the last days
the stack's reference days come from (recent_missing, prune), and no
index.json, so the data branch's files never replace a local backfill's index.

Credentials: GME_API_LOGIN / GME_API_PASSWORD, else asked for at the
terminal (the password not shown); never printed.
"""

import argparse
import getpass
import gzip
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_gme_units import kind_of  # noqa: E402
from fetch_gme_units import REQUEST_PAUSE_SECONDS, get_token, number, request_offers, rows_of  # noqa: E402
from fetch_mgp_merit import PUBLISHED_AFTER, day_quarters, unit_sources  # noqa: E402

import requests  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "app", "data", "stack", "offers")

FORMAT = 3                                 # 2: the MGP's own periods; 3: block orders' ids
QUARTER_HOURLY_FROM = date(2025, 10, 1)   # the first day of the quarter-hourly MGP
STANDING = {"ACC", "REJ", "PREJ"}
HOURLY, BILATERAL, BLOCK = 1, 2, 4
BILATERAL_OPERATOR = "Bilateralista"
STORAGE_LETTERS = ("K", "J")               # the storage aggregates' technology letters
OUTAGE_WAITS = (600, 1200, 1800, 3600)     # seconds to wait after GME server errors, in turn
OUTAGE_HOURS = 8                           # then the run stops
SOURCE = "GME public offers (Offers_PublicDomain, MGP)"


def keeps_bids(code, kind):
    """Whether a unit's purchase bids are kept one by one (not summed into
    its zone's demand): all but consumption units and aggregates and the
    virtual units; storage aggregates are kept."""
    if kind in ("import", "export") or "consumption" in kind:
        return False
    if "withdrawal" in kind:
        return code.endswith(STORAGE_LETTERS)
    return True


def periods_of(row, quarterly_day):
    """The day's periods (indexes) a row covers: a quarter-hourly offer
    (PT15) its quarter-hour, an hourly order (PT60) in a quarter-hourly day
    its hour's four quarter-hours (the same MW in each); before the
    quarter-hourly MGP, the hour."""
    period = int(number(row.get("PERIOD") or row.get("INTERVAL_NO")) or 1)
    if quarterly_day and row.get("GRANULARITY") != "PT15":
        return range((period - 1) * 4, period * 4)
    return range(period - 1, period)


def day_curves(day, rows, units):
    """The day's curves from GME's offer rows, period by period (see the module notes)."""
    quarterly_day = day >= QUARTER_HOURLY_FROM
    periods = day_quarters(day) if quarterly_day else day_quarters(day) // 4
    unit_index, unit_list, operators = {}, [], defaultdict(Counter)
    block_index, block_list = {}, []                  # GME's BLOCK_ID -> 1.., [unit, BLOCK_ID, minimum ratio]
    offers = defaultdict(lambda: [0.0, 0.0])          # (unit, period, price, flags) -> MW, accepted
    bids = defaultdict(lambda: [0.0, 0.0])
    demand = defaultdict(lambda: [0.0, 0.0])          # (zone, period, price) -> MW, accepted
    period_prices = defaultdict(Counter)              # (period, zone) -> awarded prices
    hourly_prices = defaultdict(Counter)              # (hour, zone) -> hourly orders' awarded prices
    granularity = Counter()

    def unit_of(code, row):
        if code not in unit_index:
            _source, _operator, zone, kind = units.get(code) or (None, None, None, None)
            unit_index[code] = len(unit_list)
            unit_list.append([code, kind or kind_of(code), zone or row.get("ZONE_CD") or None, None])
        operators[unit_index[code]][row.get("OPERATORE") or ""] += 1
        return unit_index[code]

    for row in rows:
        status = row.get("STATUS_CD")
        if status not in STANDING:
            continue
        covered = [index for index in periods_of(row, quarterly_day) if 0 <= index < periods]
        if not covered:
            continue
        granularity[row.get("GRANULARITY") or ""] += 1
        purpose = row.get("PURPOSE_CD")
        price = number(row.get("ENERGY_PRICE_NO"))
        price = round(price, 2) if price is not None else 0.0
        quantity = number(row.get("QUANTITY_NO")) or 0.0
        accepted = (number(row.get("AWARDED_QUANTITY_NO")) or 0.0) if status == "ACC" else 0.0
        awarded_price = number(row.get("AWARDED_PRICE_NO"))
        hourly_order = quarterly_day and row.get("GRANULARITY") != "PT15"
        if status == "ACC" and awarded_price is not None:
            # Prices from the day's own periods; an hourly order in a
            # quarter-hourly day is paid the mean of its hour's four prices,
            # kept for the zones with no quarter-hourly price.
            if hourly_order:
                hourly_prices[(covered[0] // 4, row.get("ZONE_CD"))][awarded_price] += 1
            else:
                period_prices[(covered[0], row.get("ZONE_CD"))][awarded_price] += 1
        flags = (HOURLY if hourly_order else 0) | (BILATERAL if row.get("BILATERAL_IN") == "true" else 0) | \
            (BLOCK if row.get("OFFER_TYPE") == "B" else 0)
        code = row.get("UNIT_REFERENCE_NO") or ""
        unit = unit_of(code, row) if purpose in ("OFF", "BID") else None
        block = 0
        if flags & BLOCK:
            # A block order's rows share GME's BLOCK_ID (else its transaction reference).
            block_id = row.get("BLOCK_ID") or row.get("TRANSACTION_REFERENCE_NO") or f"{code}|{price}"
            if block_id not in block_index:
                block_list.append([unit, block_id, number(row.get("MINIMUM_ACCEPTANCE_RATIO"))])
                block_index[block_id] = len(block_list)
            block = block_index[block_id]
        if purpose == "OFF":
            keys, table = [(unit, index, price, flags, block) for index in covered], offers
        elif purpose == "BID":
            if keeps_bids(code, unit_list[unit][1]):
                keys, table = [(unit, index, price, flags, block) for index in covered], bids
            else:
                zone = unit_list[unit][2] or row.get("ZONE_CD")
                keys, table = [(zone, index, price) for index in covered], demand
        else:
            continue
        for key in keys:
            step = table[key]
            step[0] += quantity
            step[1] += accepted

    for index, counter in operators.items():
        named = Counter({name: n for name, n in counter.items() if name and name != BILATERAL_OPERATOR})
        unit_list[index][3] = (named or counter).most_common(1)[0][0] or None

    prices = {}
    for (index, zone), counts in period_prices.items():
        prices.setdefault(zone, [None] * periods)[index] = round(counts.most_common(1)[0][0], 2)
    for (hour, zone), counts in hourly_prices.items():
        series = prices.setdefault(zone, [None] * periods)
        for index in range(hour * 4, min(hour * 4 + 4, periods)):
            if series[index] is None:      # a zone priced by hourly orders only
                series[index] = round(counts.most_common(1)[0][0], 2)

    def columns(table, names, key_names):
        items = sorted(table.items(), key=lambda item: tuple("" if v is None else v for v in item[0]))
        data = {name: [] for name in names}
        for key, (mw, acc) in items:
            if mw <= 0 and acc <= 0:
                continue
            for name, value in zip(key_names, key):
                data[name].append(value)
            data["mw"].append(round(mw, 3))
            data["accepted"].append(round(acc, 3))
        return data

    step_columns = ["unit", "period", "price", "flags", "block", "mw", "accepted"]
    return {
        "date": day.isoformat(), "market": "MGP", "version": FORMAT, "source": SOURCE,
        "periods": periods, "period_minutes": 15 if quarterly_day else 60, "hours": day_quarters(day) // 4,
        "quarter_hourly": quarterly_day, "granularity": dict(granularity),
        "units_columns": ["code", "kind", "zone", "operator"], "units": unit_list,
        "prices": dict(sorted(prices.items())),
        "offers": columns(offers, step_columns, step_columns[:5]),
        "bids": columns(bids, step_columns, step_columns[:5]),
        "blocks_columns": ["unit", "id", "minimum_ratio"], "blocks": block_list,
        "demand": columns(demand, ["zone", "period", "price", "mw", "accepted"], ["zone", "period", "price"]),
    }


def day_format(day):
    """The format a day's file must have (every day: blocks are in both MGPs)."""
    return FORMAT


def write_day(out_dir, data):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{data['date']}.json.gz")
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(gzip.compress(payload, compresslevel=9, mtime=0))
    return path


def load_index(out_dir):
    try:
        with open(os.path.join(out_dir, "index.json"), encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {"version": FORMAT, "days": {}}


def save_index(out_dir, index):
    os.makedirs(out_dir, exist_ok=True)
    index["days"] = dict(sorted(index["days"].items()))
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as handle:
        json.dump(index, handle, separators=(",", ":"))


def recent_missing(today, keep_days, out_dir=None):
    """The daily job's days with no file yet: from keep_days ago to the last
    GME may have out (PUBLISHED_AFTER days ago), newest first."""
    out_dir = out_dir or OUT_DIR
    days = [today - timedelta(days=back) for back in range(PUBLISHED_AFTER, keep_days + 1)]
    return [day for day in days if not os.path.exists(os.path.join(out_dir, f"{day.isoformat()}.json.gz"))]


def prune(today, keep_days, out_dir=None):
    """Delete the day files older than keep_days (the daily job's window); the names of those deleted."""
    out_dir = out_dir or OUT_DIR
    first = (today - timedelta(days=keep_days)).isoformat()
    gone = []
    for name in sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []:
        if name.endswith(".json.gz") and name[:10] < first:
            os.remove(os.path.join(out_dir, name))
            gone.append(name)
    return gone


def missing_days(first, last, out_dir):
    on_file = load_index(out_dir)["days"]
    days, day = [], first
    while day <= last:
        entry = on_file.get(day.isoformat())
        if not entry or entry.get("version", 0) < day_format(day) or \
                not os.path.exists(os.path.join(out_dir, entry["file"])):
            days.append(day)
        day += timedelta(days=1)
    return days


def credentials():
    """GME's login and password: from the environment, else asked for at a
    terminal (the password not shown, neither kept)."""
    login, password = os.environ.get("GME_API_LOGIN"), os.environ.get("GME_API_PASSWORD")
    if sys.stdin.isatty():
        login = login or input("GME API login: ")
        password = password or getpass.getpass("GME API password: ")
    login, password = clean(login), clean(password)
    if not login or not password:
        raise SystemExit("GME_API_LOGIN / GME_API_PASSWORD are not set")
    return login, password


def clean(text):
    """Typed or pasted text without the terminal's paste markers and spaces at either end."""
    return re.sub(r"\x1b\[20[01]~", "", text or "").strip()


def login_token(login, password):
    try:
        return get_token(login, password)
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code in (401, 403):
            raise SystemExit("GME refused the login (HTTP 401): the API login or password is wrong. They are the "
                             "API account's (the GME_API_LOGIN / GME_API_PASSWORD secrets), not the website's.")
        raise


def say(message, log_path=None):
    """Print a progress line, and add it to the log file (no credentials ever go there)."""
    print(message, flush=True)
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def keep_awake():
    """Ask Windows not to sleep while this process runs (idle sleep only:
    closing the lid or choosing Sleep still sleeps; the request ends with the
    process).  Elsewhere, nothing."""
    if os.name == "nt":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def server_side(error):
    """Whether an error is GME's or the network's (worth waiting for), not the day's."""
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    response = getattr(error, "response", None)
    return response is not None and response.status_code >= 500


def backfill(first, last, out_dir, max_days, pause, before=(), log_path=None, outage_hours=OUTAGE_HOURS):
    """The days of first..last not on file, oldest first, after the days
    "before" (read first even when on file: checks).  When GME answers with
    server errors (an outage, or its limits), the run waits, longer each
    time (OUTAGE_WAITS), and tries the same day again; after outage_hours of
    it, it stops.  Other failures are tried once more at the end; a later
    run reads whatever is still missing."""
    days = list(before) + [day for day in missing_days(first, last, out_dir) if day not in before]
    say(f"{first} - {last}: {len(days)} day(s) to read" + (f", at most {max_days} this run" if max_days else ""),
        log_path)
    if not days:
        return
    login, password = credentials()
    keep_awake()
    waited = 0.0
    for attempt in range(1000):
        try:
            token = login_token(login, password)
            break
        except Exception as error:
            if not server_side(error) or waited > outage_hours * 3600:
                raise
            wait = OUTAGE_WAITS[min(attempt, len(OUTAGE_WAITS) - 1)]
            say(f"  GME login: {type(error).__name__}: {error}; waiting {wait // 60} min", log_path)
            time.sleep(wait)
            waited += wait
    units = unit_sources()
    index = load_index(out_dir)
    done, failed = [], []
    started = time.time()

    def read(day, token):
        try:
            name, content = request_offers(token, day)
        except PermissionError:          # the token expired: log in again
            token = get_token(login, password)
            name, content = request_offers(token, day)
        data = day_curves(day, rows_of(name, content), units)
        del content
        return data, token

    queue = list(days)
    retries = []
    position = 0
    outage_since, outage_wait = None, 0
    while queue:
        day = queue.pop(0)
        if max_days and len(done) >= max_days:
            break
        if position:
            time.sleep(pause)
        position += 1
        began = time.time()
        try:
            data, token = read(day, token)
            outage_since, outage_wait = None, 0
        except Exception as error:   # the other days go on
            say(f"  {day}: {type(error).__name__}: {error}", log_path)
            if server_side(error):
                outage_since = outage_since or time.time()
                if time.time() - outage_since > outage_hours * 3600:
                    say(f"GME has answered with server errors for {outage_hours} h: stopping; "
                        f"run again later for the {len(queue) + 1} day(s) left.", log_path)
                    break
                wait = OUTAGE_WAITS[min(outage_wait, len(OUTAGE_WAITS) - 1)]
                outage_wait += 1
                say(f"  GME server error: waiting {wait // 60} min, then {day} again", log_path)
                time.sleep(wait)
                queue.insert(0, day)
                continue
            if day in retries:
                failed.append(day)
            else:
                retries.append(day)
                queue.append(day)    # once more, at the end
            continue
        path = write_day(out_dir, data)
        index["days"][day.isoformat()] = {"file": os.path.basename(path), "version": FORMAT, "periods": data["periods"],
                                          "offers": len(data["offers"]["mw"]), "bytes": os.path.getsize(path)}
        save_index(out_dir, index)
        done.append(day)
        left = len(queue)
        rate = (time.time() - started) / position
        say(f"  {day}: {len(data['offers']['mw']):,} offer steps, {len(data['units']):,} units, "
            f"{os.path.getsize(path) / 1e6:.2f} MB, {time.time() - began:.0f} s; {left} left, "
            f"~{left * rate / 3600:.1f} h", log_path)
    say(f"Stack offers: {len(done)} day(s) read, {len(failed)} failed "
        f"({', '.join(d.isoformat() for d in failed[:10])}{'...' if len(failed) > 10 else ''}); "
        f"{len(load_index(out_dir)['days'])} day(s) on file.", log_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="first", default="2025-07-01")
    parser.add_argument("--to", dest="last", help=f"last day (default: {PUBLISHED_AFTER} days ago)")
    parser.add_argument("--max-days", type=int, default=0, help="days at most this run (0: all)")
    parser.add_argument("--pause", type=float, default=REQUEST_PAUSE_SECONDS, help="seconds between requests")
    parser.add_argument("--dates", default="", help="days read first, comma-separated (checks)")
    parser.add_argument("--out", default=OUT_DIR)
    parser.add_argument("--check", action="store_true", help="only log in to GME, to check the credentials")
    parser.add_argument("--outage-hours", type=float, default=OUTAGE_HOURS,
                        help="hours of GME server errors to wait through before stopping")
    parser.add_argument("--log", default=os.path.join(os.path.dirname(OUT_DIR), "offers_backfill.log"),
                        help="progress log (no credentials)")
    args = parser.parse_args()
    last = date.fromisoformat(args.last) if args.last else date.today() - timedelta(days=PUBLISHED_AFTER)
    if args.check:
        login_token(*credentials())
        print("GME login OK.")
        return
    before = [date.fromisoformat(text) for text in args.dates.split(",") if text.strip()]
    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    backfill(date.fromisoformat(args.first), last, args.out, args.max_days, args.pause, before, args.log,
             args.outage_hours)


if __name__ == "__main__":
    main()
