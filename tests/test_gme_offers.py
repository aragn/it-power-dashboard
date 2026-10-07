"""Checks for update_gme_offers.  Run: python -m pytest tests"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_mgp_merit as mm  # noqa: E402
import update_gme_offers as go  # noqa: E402

TODAY = date(2026, 10, 5)


def test_days_to_read_newest_first_and_nothing_gme_cannot_have_out(tmp_path):
    out = str(tmp_path)
    full = [{"time": mm.label(q, date(2026, 9, 27))} for q in range(96)]
    mm.write_day(out, {"date": "2026-09-27", "version": mm.FORMAT, "units": [], "quarters": full})
    mm.write_index(out)
    state = {"processed": ["2026-09-26", "2026-09-28"], "units": {}}
    days, merit, units = go.days_to_read(state, TODAY, date(2026, 9, 25), out)
    last = TODAY - timedelta(days=mm.PUBLISHED_AFTER)                   # 2026-09-28
    assert days[0] == last and days == sorted(days, reverse=True)
    assert merit == {date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 28)}
    # The database: its window (not read yet) and every day read for the merit order.
    assert date(2026, 9, 27) in units and date(2026, 9, 25) in units
    assert date(2026, 9, 26) not in units and date(2026, 9, 28) not in units
    assert min(units) == TODAY - timedelta(days=20)


def test_each_day_is_downloaded_once_for_both(tmp_path, monkeypatch):
    asked, parsed = [], []
    monkeypatch.setenv("GME_API_LOGIN", "x")
    monkeypatch.setenv("GME_API_PASSWORD", "y")
    monkeypatch.setattr(go.units_etl, "get_token", lambda login, password: "token")
    monkeypatch.setattr(go.units_etl, "REQUEST_PAUSE_SECONDS", 0)

    def offers(token, day):
        asked.append(day)
        if day == date(2026, 9, 28):
            raise RuntimeError("No data for 2026-09-28")
        return "x.xml", b"<xml/>"

    def rows(name, content):
        parsed.append(name)
        return iter([])

    monkeypatch.setattr(go.units_etl, "request_offers", offers)
    monkeypatch.setattr(go.units_etl, "rows_of", rows)
    monkeypatch.setattr(go.merit_etl, "unit_sources", lambda: {})
    monkeypatch.setattr(go.units_etl, "summarise_day", lambda rows: {"units": {}})
    monkeypatch.setattr(go.units_etl, "merge_units", lambda store, day, summary: None)
    state = {"processed": [], "units": {}}
    days = [date(2026, 9, 28), date(2026, 9, 27), date(2026, 9, 26), date(2026, 9, 25)]
    done = go.read_days(days, {date(2026, 9, 27), date(2026, 9, 25)}, set(days), state, str(tmp_path), 2)
    assert done == [date(2026, 9, 27), date(2026, 9, 26)]           # the day not out is skipped; 2 at most
    assert asked == [date(2026, 9, 28), date(2026, 9, 27), date(2026, 9, 26)]
    assert len(parsed) == 3                                            # 27th: merit order and units; 26th: units
    assert state["processed"] == ["2026-09-27", "2026-09-26"]
    assert os.path.exists(os.path.join(str(tmp_path), "2026-09-27.json.gz"))
