"""
GME MGP public offers, each day downloaded once for both products:

  the market-unit database   (fetch_gme_units.py, build_gme_units.py)
      every day read that it has not read yet, plus the days of the last
      LOOKBACK_DAYS it has not read
  the merit order             (fetch_mgp_merit.py)
      the full days from BACKFILL_FROM not on file yet

A day file is 500-650 MB, so: at most MAX_DAYS downloads a run, one request
at a time with a pause between, newest first, and only days GME may have
published (it puts a day's offers out about 8 days after the market:
nothing newer than PUBLISHED_AFTER days ago is asked for).  A day not out
yet is skipped.  The XML is parsed once for each product that needs it.

Then the unit database is built (new codes in the run's summary) and the
merit order's unit map (units.json: each code's source, operator and zone
from that database, so research reaches the days already on file) and index
are written.

Credentials: GME_API_LOGIN / GME_API_PASSWORD, ENTSOE_API_KEY (weekly unit
lists for the database).
"""

import argparse
import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_gme_units as units_etl  # noqa: E402
import fetch_mgp_merit as merit_etl  # noqa: E402


def days_to_read(state, today, merit_from, merit_dir):
    """(days newest first, the merit order's days, the database's days)."""
    last = today - timedelta(days=merit_etl.PUBLISHED_AFTER)
    merit = set(merit_etl.missing_days(merit_from, last, merit_dir))
    window = [today - timedelta(days=n) for n in range(merit_etl.PUBLISHED_AFTER, units_etl.LOOKBACK_DAYS + 1)]
    processed = set(state["processed"])
    units = {day for day in window if day.isoformat() not in processed}
    # A day read for the merit order goes into the database too.
    units |= {day for day in merit if day.isoformat() not in processed}
    return sorted(merit | units, reverse=True), merit, units


def read_days(days, merit_days, unit_days, state, merit_dir, max_days):
    login, password = os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"]
    token = units_etl.get_token(login, password)
    sources = merit_etl.unit_sources()
    done = []
    for index, day in enumerate(days):
        if len(done) >= max_days:
            break
        if index:
            time.sleep(units_etl.REQUEST_PAUSE_SECONDS)
        try:
            try:
                name, content = units_etl.request_offers(token, day)
            except PermissionError:
                token = units_etl.get_token(login, password)
                name, content = units_etl.request_offers(token, day)
        except Exception as error:  # not out yet, or a failure: the other days go on
            print(f"  {day}: {error}")
            continue
        parts = []
        if day in merit_days:
            data = merit_etl.day_data(day, units_etl.rows_of(name, content), sources)
            merit_etl.write_day(merit_dir, data)
            parts.append(f"merit order {len(data['quarters'])} quarter-hours")
        if day in unit_days:
            summary = units_etl.summarise_day(units_etl.rows_of(name, content))
            units_etl.merge_units(state["units"], day, summary)
            state["processed"].append(day.isoformat())
            parts.append(f"units {len(summary['units']):,}")
        del content
        print(f"  {day}: {name}, " + ", ".join(parts))
        done.append(day)
    return done


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-days", type=int, default=merit_etl.MAX_DAYS)
    parser.add_argument("--from", dest="first", default=merit_etl.BACKFILL_FROM,
                        help="first day of the merit order")
    args = parser.parse_args()
    today = date.today()

    state = units_etl.load_state()
    before = set(state["units"])
    if not state["units"]:
        # No database yet: its own start (random sample days since 2025).
        units_etl.daily(today)
        state = units_etl.load_state()
        before = set(state["units"])

    days, merit_days, unit_days = days_to_read(state, today, date.fromisoformat(args.first), merit_etl.OUT_DIR)
    print(f"{len(days)} day(s) to read ({len(merit_days)} for the merit order, {len(unit_days)} for the unit "
          f"database), at most {args.max_days} this run")
    done = read_days(days, merit_days, unit_days, state, merit_etl.OUT_DIR, args.max_days) if days else []
    if not state.get("daily_from") and any(day in unit_days for day in done):
        state["daily_from"] = min(day for day in done if day in unit_days).isoformat()

    units_etl.finish_units(state, before, today)
    merit_etl.write_units_map(merit_etl.OUT_DIR, merit_etl.unit_sources())
    on_file = merit_etl.write_index(merit_etl.OUT_DIR)
    left = len(days) - len(done)
    message = (f"GME offers: {len(done)} day(s) read, {left} still to read; merit order {len(on_file)} day(s) on file, "
               f"unit database {len(state['processed'])} day(s) read.")
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(message + "\n")


if __name__ == "__main__":
    main()
