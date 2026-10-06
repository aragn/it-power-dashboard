"""Checks for fetch_mgp_merit.  Run: python -m pytest tests"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_mgp_merit as mm  # noqa: E402

UNITS = {"UP_SOLE_1": ("solar", "SOLE SPA", "SUD", "production"),
         "UC_0000001_01": (None, "BUYER", "NORD", "consumption")}


def row(unit, period, quantity, awarded=0, status="ACC", purpose="OFF", price="0", awarded_price="100",
        granularity="PT15", zone="SUD", bilateral="false", offer_type="S"):
    return {"UNIT_REFERENCE_NO": unit, "PURPOSE_CD": purpose, "STATUS_CD": status, "QUANTITY_NO": str(quantity),
            "AWARDED_QUANTITY_NO": str(awarded), "ENERGY_PRICE_NO": price, "AWARDED_PRICE_NO": awarded_price,
            "PERIOD": str(period), "GRANULARITY": granularity, "BILATERAL_IN": bilateral, "OFFER_TYPE": offer_type,
            "OPERATORE": "OP", "ZONE_CD": zone}


def records(quarter):
    flat = quarter["supply"]
    return [flat[index:index + 7] for index in range(0, len(flat), 7)]


def test_an_hourly_order_is_in_each_quarter_hour_of_its_hour():
    # Period 12 of PT60 is 11:00-12:00: quarter-hours 44-47 (PT15 periods 45-48).
    rows = [row("UP_SOLE_1", 12, 40, awarded=40, granularity="PT60", awarded_price="95.5")]
    data = mm.build(rows, mm.quarter("11:00"), mm.quarter("11:30"), UNITS)
    assert [item["time"] for item in data["quarters"]] == ["11:00", "11:15", "11:30"]
    unit, mw, price, status, accepted, awarded_price, flags = records(data["quarters"][0])[0]
    assert data["units"][unit][:2] == ["UP_SOLE_1", "solar"]
    assert (mw, status, accepted, awarded_price, flags) == (40, 0, 40, 95.5, mm.HOURLY)


def test_zonal_prices_come_from_quarter_hourly_offers_only():
    rows = [row("UP_SOLE_1", 45, 10, awarded=10, awarded_price="120"),
            row("UP_SOLE_1", 12, 5, awarded=5, granularity="PT60", awarded_price="99")]
    data = mm.build(rows, 44, 44, UNITS)
    assert data["quarters"][0]["prices"] == {"SUD": 120.0}


def test_only_standing_offers_go_into_the_curve_but_every_status_is_counted():
    rows = [row("UP_SOLE_1", 45, 10, awarded=6),
            row("UP_SOLE_1", 45, 7, status="REJ", price="300", awarded_price=""),
            row("UP_SOLE_1", 45, 3, status="PREJ", price="250", awarded_price="", offer_type="B"),
            row("UP_SOLE_1", 45, 10, status="REP"),
            row("UP_SOLE_1", 45, 2, status="REV"),
            row("UP_NUOVO_1", 45, 1, status="INC")]
    quarter = mm.build(rows, 44, 44, UNITS)["quarters"][0]
    assert sorted((r[1], r[3], r[4]) for r in records(quarter)) == [(3, 2, 0), (7, 1, 0), (10, 0, 6)]
    assert [r[6] for r in records(quarter) if r[3] == 2] == [mm.BLOCK]
    others = [quarter["others"][index:index + 3] for index in range(0, len(quarter["others"]), 3)]
    sole, nuovo = 0, 1                                  # unit indexes, in order of appearance
    assert others == [[sole, 3, 10], [sole, 4, 2], [nuovo, 5, 1]]


def test_purchase_bids_are_grouped_by_price():
    rows = [row("UC_0000001_01", 45, 50, awarded=50, purpose="BID", price="4000", zone="NORD"),
            row("UC_0000001_01", 45, 20, awarded=15, purpose="BID", price="4000", zone="NORD"),
            row("UC_0000001_01", 45, 8, status="REJ", purpose="BID", price="-500", zone="NORD"),
            row("UC_0000001_01", 45, 8, status="REP", purpose="BID", price="10", zone="NORD")]
    quarter = mm.build(rows, 44, 44, UNITS)["quarters"][0]
    assert quarter["demand"] == [4000.0, 65.0, 5.0, -500.0, 0.0, 8.0]
    assert quarter["supply"] == []


def test_a_unit_missing_from_the_database_gets_its_kind_from_the_code():
    data = mm.build([row("UVZi_00001_9999_SUC", 45, 4, awarded=4)], 44, 44, UNITS)
    code, source, operator, zone, kind = data["units"][0]
    assert (code, operator, zone, kind) == ("UVZi_00001_9999_SUC", "OP", "SUD", "aggregate_injection")


def test_quarter_labels():
    assert mm.quarter("19:45") == 79
    assert mm.label(79) == "19:45"
    assert list(mm.quarters_of({"PERIOD": "80", "GRANULARITY": "PT15"})) == [79]
    assert list(mm.quarters_of({"PERIOD": "20", "GRANULARITY": "PT60"})) == [76, 77, 78, 79]


def test_days_are_written_gzipped_and_indexed(tmp_path):
    out = str(tmp_path)
    open(os.path.join(out, "2026-09-17.json"), "w").write("{}")       # an older plain file of the day
    full = {"date": "2026-09-17", "units": [], "quarters": [{"time": mm.label(q)} for q in range(96)]}
    part = {"date": "2026-09-18", "units": [], "quarters": [{"time": "11:00"}]}
    first = mm.write_day(out, full)
    assert first.endswith("2026-09-17.json.gz") and not os.path.exists(os.path.join(out, "2026-09-17.json"))
    assert open(first, "rb").read(2) == b"\x1f\x8b"
    assert mm.read_day(first) == full
    mm.write_day(out, part)
    assert open(first, "rb").read() == open(mm.write_day(out, full), "rb").read()   # same bytes again
    days = mm.write_index(out)
    assert len(days["2026-09-17"]) == 96 and days["2026-09-18"] == ["11:00"]
    index = mm.read_day(os.path.join(out, "index.json"))
    assert index["files"] == {"2026-09-17": "2026-09-17.json.gz", "2026-09-18": "2026-09-18.json.gz"}


def test_the_backfill_skips_full_days_only(tmp_path):
    from datetime import date
    out = str(tmp_path)
    mm.write_day(out, {"date": "2026-08-02", "units": [], "quarters": [{"time": mm.label(q)} for q in range(96)]})
    mm.write_day(out, {"date": "2026-08-03", "units": [], "quarters": [{"time": "11:00"}]})
    mm.write_index(out)
    assert mm.missing_days(date(2026, 8, 1), date(2026, 8, 4), out) == [
        date(2026, 8, 1), date(2026, 8, 3), date(2026, 8, 4)]


def test_a_run_stops_at_the_first_day_not_published(tmp_path, monkeypatch):
    from datetime import date
    asked = []

    def offers(token, day):
        asked.append(day)
        raise RuntimeError(f"No data for {day}: not published")

    monkeypatch.setenv("GME_API_LOGIN", "x")
    monkeypatch.setenv("GME_API_PASSWORD", "y")
    monkeypatch.setattr(mm, "get_token", lambda login, password: "token")
    monkeypatch.setattr(mm, "request_offers", offers)
    monkeypatch.setattr(mm, "unit_sources", lambda: {})
    mm.backfill(date(2026, 9, 28), date(2026, 10, 3), str(tmp_path), 8)
    assert asked == [date(2026, 9, 28)]
