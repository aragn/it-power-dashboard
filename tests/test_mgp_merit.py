"""Checks for fetch_mgp_merit.  Run: python -m pytest tests"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import pytest  # noqa: E402

import fetch_mgp_merit as mm  # noqa: E402

DAY = date(2026, 9, 17)
SPRING, AUTUMN = date(2026, 3, 29), date(2026, 10, 25)        # the clock-change days of 2026

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
    data = mm.build(rows, mm.quarter("11:00", DAY), mm.quarter("11:30", DAY), UNITS, DAY)
    assert [item["time"] for item in data["quarters"]] == ["11:00", "11:15", "11:30"]
    unit, mw, price, status, accepted, awarded_price, flags = records(data["quarters"][0])[0]
    assert data["units"][unit][:2] == ["UP_SOLE_1", "solar"]
    assert (mw, status, accepted, awarded_price, flags) == (40, 0, 40, 95.5, mm.HOURLY)


def test_zonal_prices_come_from_quarter_hourly_offers_only():
    rows = [row("UP_SOLE_1", 45, 10, awarded=10, awarded_price="120"),
            row("UP_SOLE_1", 12, 5, awarded=5, granularity="PT60", awarded_price="99")]
    data = mm.build(rows, 44, 44, UNITS, DAY)
    assert data["quarters"][0]["prices"] == {"SUD": 120.0}


def test_only_standing_offers_go_into_the_curve_but_every_status_is_counted():
    rows = [row("UP_SOLE_1", 45, 10, awarded=6),
            row("UP_SOLE_1", 45, 7, status="REJ", price="300", awarded_price=""),
            row("UP_SOLE_1", 45, 3, status="PREJ", price="250", awarded_price="", offer_type="B"),
            row("UP_SOLE_1", 45, 10, status="REP"),
            row("UP_SOLE_1", 45, 2, status="REV"),
            row("UP_NUOVO_1", 45, 1, status="INC")]
    quarter = mm.build(rows, 44, 44, UNITS, DAY)["quarters"][0]
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
    quarter = mm.build(rows, 44, 44, UNITS, DAY)["quarters"][0]
    assert quarter["demand"] == [4000.0, 65.0, 5.0, -500.0, 0.0, 8.0]
    assert quarter["supply"] == []


def test_a_unit_missing_from_the_database_gets_its_kind_from_the_code():
    data = mm.build([row("UVZi_00001_9999_SUC", 45, 4, awarded=4)], 44, 44, UNITS, DAY)
    code, source, operator, zone, kind = data["units"][0]
    assert (code, operator, zone, kind) == ("UVZi_00001_9999_SUC", "OP", "SUD", "aggregate_injection")


def test_quarter_labels():
    assert mm.quarter("19:45", DAY) == 79
    assert mm.label(79, DAY) == "19:45"
    assert list(mm.quarters_of({"PERIOD": "80", "GRANULARITY": "PT15"})) == [79]
    assert list(mm.quarters_of({"PERIOD": "20", "GRANULARITY": "PT60"})) == [76, 77, 78, 79]


def test_days_are_written_gzipped_and_indexed(tmp_path):
    out = str(tmp_path)
    open(os.path.join(out, "2026-09-17.json"), "w").write("{}")       # an older plain file of the day
    full = {"date": "2026-09-17", "units": [], "quarters": [{"time": mm.label(q, DAY)} for q in range(96)]}
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
    out = str(tmp_path)
    full = [{"time": mm.label(q, DAY)} for q in range(96)]
    mm.write_day(out, {"date": "2026-08-02", "version": mm.FORMAT, "units": [], "quarters": full})
    mm.write_day(out, {"date": "2026-08-03", "version": mm.FORMAT, "units": [], "quarters": [{"time": "11:00"}]})
    mm.write_day(out, {"date": "2026-08-05", "units": [], "quarters": full})     # full, but of the old format
    mm.write_index(out)
    assert mm.missing_days(date(2026, 8, 1), date(2026, 8, 5), out) == [
        date(2026, 8, 1), date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]


def test_a_run_stops_at_the_first_day_not_published(tmp_path, monkeypatch):
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


def test_accepted_purchases_are_kept_per_unit():
    rows = [row("UC_0000001_01", 49, 300, awarded=250, purpose="BID", price="3000", awarded_price="120"),
            row("UP_SOLE_1", 49, 40, awarded=0, purpose="BID", status="REJ", price="10"),
            row("UC_0000001_01", 13, 100, awarded=100, purpose="BID", granularity="PT60", price="3000", awarded_price="118.5")]
    data = mm.build(rows, mm.quarter("12:00", DAY), mm.quarter("12:00", DAY), UNITS, DAY)
    quarter, = data["quarters"]
    assert quarter["purchases"] == [0, 100.0, 118.5, mm.HOURLY, 0, 250.0, 120.0, 0] or \
        quarter["purchases"] == [0, 250.0, 120.0, 0, 0, 100.0, 118.5, mm.HOURLY]
    assert quarter["demand"][0] == 3000.0                     # the curve stays as it was
    assert data["units"][0][0] == "UC_0000001_01"


def test_the_clock_change_days_have_92_and_100_quarter_hours():
    assert mm.day_quarters(DAY) == 96
    assert mm.day_quarters(SPRING) == 92 and mm.day_quarters(AUTUMN) == 100
    assert mm.day_quarters(date(2025, 10, 26)) == 100 and mm.day_quarters(date(2027, 3, 28)) == 92
    assert mm.day_quarters(date(2026, 3, 28)) == mm.day_quarters(date(2026, 10, 26)) == 96


def test_quarter_hours_are_labelled_by_the_clock_on_the_clock_change_days():
    spring = [mm.label(index, SPRING) for index in range(92)]
    assert spring[6:10] == ["01:30", "01:45", "03:00", "03:15"]      # 02:00-03:00 skipped
    assert spring[-1] == "23:45"
    autumn = [mm.label(index, AUTUMN) for index in range(100)]
    assert autumn[8:18] == ["02:00", "02:15", "02:30", "02:45", "02:00*", "02:15*", "02:30*", "02:45*",
                            "03:00", "03:15"]
    assert autumn[-1] == "23:45" and len(set(autumn)) == 100
    assert [mm.quarter(text, AUTUMN) for text in autumn] == list(range(100))
    assert mm.quarter("23:45", SPRING) == 91 and mm.quarter("03:00", SPRING) == 8
    with pytest.raises(ValueError):
        mm.quarter("02:15", SPRING)                                      # not on the clock that day
    with pytest.raises(ValueError):
        mm.quarter("02:15*", DAY)


def hourly_times(quarters):
    return {time for time, item in quarters.items() if any(r[6] == mm.HOURLY for r in records(item))}


def test_every_period_of_the_autumn_day_is_read_and_labelled():
    # PT15 periods 1-100, PT60 1-25: the hour 02:00-03:00 is periods 9-12 and
    # 13-16 (PT60 3 and 4), each quarter-hour keeping its period.
    rows = [row("UP_SOLE_1", period, 10, awarded=10, awarded_price=str(period)) for period in range(1, 101)]
    rows += [row("UP_SOLE_1", 4, 5, awarded=5, granularity="PT60", awarded_price="7"),
             row("UP_SOLE_1", 25, 6, awarded=6, granularity="PT60", awarded_price="8")]
    data = mm.day_data(AUTUMN, rows, UNITS)
    quarters = {item["time"]: item for item in data["quarters"]}
    assert len(data["quarters"]) == 100 and [item["period"] for item in data["quarters"]] == list(range(1, 101))
    assert quarters["02:00"]["period"] == 9 and quarters["02:00*"]["period"] == 13
    assert quarters["03:00"]["period"] == 17 and quarters["23:45"]["period"] == 100
    assert quarters["02:00*"]["prices"] == {"SUD": 13.0} and quarters["23:45"]["prices"] == {"SUD": 100.0}
    assert hourly_times(quarters) == {"02:00*", "02:15*", "02:30*", "02:45*", "23:00", "23:15", "23:30", "23:45"}


def test_the_spring_day_skips_two_in_the_morning():
    rows = [row("UP_SOLE_1", period, 10, awarded=10, awarded_price=str(period)) for period in range(1, 93)]
    rows += [row("UP_SOLE_1", 3, 5, awarded=5, granularity="PT60", awarded_price="7")]
    data = mm.day_data(SPRING, rows, UNITS)
    quarters = {item["time"]: item for item in data["quarters"]}
    assert len(data["quarters"]) == 92 and "02:00" not in quarters
    assert quarters["03:00"]["period"] == 9 and quarters["03:00"]["prices"] == {"SUD": 9.0}
    assert quarters["23:45"]["period"] == 92
    assert hourly_times(quarters) == {"03:00", "03:15", "03:30", "03:45"}


def test_a_window_of_the_autumn_day_by_clock_times():
    rows = [row("UP_SOLE_1", period, 10, awarded=10) for period in range(1, 101)]
    data = mm.day_data(AUTUMN, rows, UNITS, mm.quarter("02:30", AUTUMN), mm.quarter("03:00", AUTUMN))
    assert [item["time"] for item in data["quarters"]] == [
        "02:30", "02:45", "02:00*", "02:15*", "02:30*", "02:45*", "03:00"]


def test_the_period_finds_the_pun_quarter_hour():
    # pun.json labels a quarter-hour by its period (time elapsed since
    # midnight): the page looks the PUN up at (period - 1) * 15 minutes.
    import fetch_pun
    for day in (DAY, SPRING, AUTUMN):
        rows = [row("UP_SOLE_1", period, 1, awarded=1) for period in range(1, mm.day_quarters(day) + 1)]
        for item in mm.day_data(day, rows, UNITS)["quarters"]:
            minutes = (item["period"] - 1) * 15
            assert fetch_pun.market_time_from_period(item["period"], 15) == f"{minutes // 60:02d}:{minutes % 60:02d}"
    assert fetch_pun.market_time_from_period(13, 15) == "03:00"          # the autumn day's "02:00*"
    assert fetch_pun.market_time_from_period(100, 15) == "24:45"         # its "23:45"


def test_the_autumn_day_is_full_only_with_its_100_quarter_hours(tmp_path):
    out = str(tmp_path)
    for day, count in ((SPRING, 92), (AUTUMN, 96)):
        mm.write_day(out, {"date": day.isoformat(), "version": mm.FORMAT, "units": [],
                           "quarters": [{"time": mm.label(q, day)} for q in range(count)]})
    mm.write_index(out)
    assert mm.missing_days(SPRING, SPRING, out) == []
    assert mm.missing_days(AUTUMN, AUTUMN, out) == [AUTUMN]


def test_the_backfill_reads_the_whole_autumn_day(tmp_path, monkeypatch):
    monkeypatch.setenv("GME_API_LOGIN", "x")
    monkeypatch.setenv("GME_API_PASSWORD", "y")
    monkeypatch.setattr(mm, "get_token", lambda login, password: "token")
    monkeypatch.setattr(mm, "request_offers", lambda token, day: ("x.xml", b""))
    monkeypatch.setattr(mm, "rows_of", lambda name, content: iter(
        [row("UP_SOLE_1", period, 1, awarded=1) for period in range(1, 101)]))
    monkeypatch.setattr(mm, "unit_sources", lambda: {})
    mm.backfill(AUTUMN, AUTUMN, str(tmp_path), 8)
    times = mm.read_day(os.path.join(str(tmp_path), "index.json"))["days"]["2026-10-25"]
    assert len(times) == 100 and times[12] == "02:00*" and times[-1] == "23:45"


def test_the_purchase_bids_of_storage_and_production_units_are_kept_per_unit():
    units = {**UNITS, "UP_BESS_1": ("battery", "BESS SPA", "SUD", "production")}
    rows = [row("UP_BESS_1", 45, 20, awarded=15, purpose="BID", price="60", awarded_price="55"),
            row("UP_BESS_1", 45, 10, status="REJ", purpose="BID", price="20", awarded_price=""),
            row("UP_BESS_1", 45, 5, status="REV", purpose="BID", price="10"),
            row("UC_0000001_01", 45, 400, awarded=400, purpose="BID", price="3000", zone="NORD"),
            row("UC_0000001_01", 46, 300, status="REJ", purpose="BID", price="1", zone="NORD")]
    data = mm.build(rows, 44, 45, units, DAY, bids=True)
    first, second = data["quarters"]
    bids = [first["bids"][index:index + 6] for index in range(0, len(first["bids"]), 6)]
    bess = [code for code, *_ in data["units"]].index("UP_BESS_1")
    assert sorted(bids) == [[bess, 10, 20.0, 1, 0, 0], [bess, 20, 60.0, 0, 15, 0]]    # standing bids only
    assert second["bids"] == []                                                        # consumption: not kept
    assert [code for code, *_ in data["units"]] == ["UP_BESS_1", "UC_0000001_01"]    # the rejected UC bid adds no unit
    assert "bids" not in mm.build(rows, 44, 44, units, DAY)["quarters"][0]           # MI-A: no bids
    assert mm.day_data(DAY, rows, units)["version"] == mm.FORMAT == 3
