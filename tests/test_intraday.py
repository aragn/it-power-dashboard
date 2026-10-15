"""Checks for etl/fetch_intraday.py series building.  Run: python -m pytest tests"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import compact  # noqa: E402
import fetch_intraday  # noqa: E402


def gme_rows(day, zone, prices):
    return [
        {"FlowDate": day, "Zone": zone, "Period": str(p), "Hour": (p - 1) // 4 + 1, "Price": price}
        for p, price in prices
    ]


def test_quarter_hour_day_builds_hourly_and_daily_averages():
    rows = gme_rows("20261001", "NORD", [(1, 100), (2, 110), (3, 120), (4, 130), (5, 200), (26, 50)])
    series = fetch_intraday.build_series(rows)["NORD"]
    assert [r["time"] for r in series["quarter_hourly"]][:2] == ["00:00", "00:15"]
    assert series["hourly"][:2] == [
        {"date": "2026-10-01", "time": "00:00", "price": 115.0},
        {"date": "2026-10-01", "time": "01:00", "price": 200.0},
    ]
    assert series["daily"] == [{"date": "2026-10-01", "price": round(710 / 6, 2)}]


def test_hourly_day_before_15_minute_mtu():
    rows = gme_rows("20250601", "SICI", [(13, 90), (14, 95)])
    series = fetch_intraday.build_series(rows)["SICI"]
    assert series["quarter_hourly"] == []
    assert [r["time"] for r in series["hourly"]] == ["12:00", "13:00"]


def test_foreign_zones_and_cancelled_auctions_are_skipped():
    rows = gme_rows("20261001", "FRAN", [(1, 10)]) + [
        {"FlowDate": "20261001", "Zone": "NORD", "Period": "1", "Price": None, "Notes": "Auction cancelled"}
    ]
    series = fetch_intraday.build_series(rows)
    assert all(not s["daily"] for s in series.values())


def test_output_round_trips_through_compact_format():
    markets = {m: fetch_intraday.empty_market() for m in fetch_intraday.MARKETS}
    markets["MI-A1"] = {**markets["MI-A1"], **fetch_intraday.build_series(
        gme_rows("20261001", "NORD", [(p, p) for p in range(1, 97)]))}
    decoded = compact.decode_tree(fetch_intraday.build_output(markets))
    nord = decoded["markets"]["MI-A1"]["NORD"]
    assert len(nord["quarter_hourly"]) == 96
    assert nord["hourly"][0] == {"date": "2026-10-01", "time": "00:00", "price": 2.5}


def volume_rows(day, zone, periods, market="MI-A1"):
    return [
        {"FlowDate": day, "Hour": str((p - 1) // 4 + 1), "Period": str(p), "Market": market, "Zone": zone,
         "Purchased": f"{bought:.4f}", "Sold": "null" if sold is None else f"{sold:.4f}", "Notes": "null"}
        for p, sold, bought in periods
    ]


def test_volumes_by_side_quarter_hourly_italian_zones_only():
    rows = volume_rows("20261010", "NORD", [(1, 1050.4, 1190.0), (2, None, 1012.6), (26, 839.5, 900.0)])
    rows += volume_rows("20261010", "TOTALE", [(1, 5000, 5000)]) + volume_rows("20261010", "FRAN", [(1, 10, 10)])
    volumes = fetch_intraday.build_volumes(rows)
    assert volumes["NORD"]["sold"] == [
        {"date": "2026-10-10", "time": "00:00", "mw": 1050.4},
        {"date": "2026-10-10", "time": "06:15", "mw": 839.5},
    ]
    assert [r["time"] for r in volumes["NORD"]["bought"]] == ["00:00", "00:15", "06:15"]
    assert volumes["SICI"] == {"sold": [], "bought": []}
    assert "TOTALE" not in volumes


def test_output_carries_the_volumes():
    markets = {m: fetch_intraday.empty_market() for m in fetch_intraday.MARKETS}
    volumes = {m: fetch_intraday.empty_volumes() for m in fetch_intraday.MARKETS}
    volumes["MI-A2"] = {**volumes["MI-A2"], **fetch_intraday.build_volumes(
        volume_rows("20261010", "SICI", [(p, p * 1.0, p * 2.0) for p in range(1, 97)], "MI-A2"))}
    decoded = compact.decode_tree(fetch_intraday.build_output(markets, volumes))
    sici = decoded["volumes"]["MI-A2"]["SICI"]
    assert len(sici["sold"]) == 96
    assert sici["bought"][1] == {"date": "2026-10-10", "time": "00:15", "mw": 4.0}


def test_an_hourly_day_repeats_each_hour_over_its_quarter_hours():
    # GME's hourly rows: Period 0, the hour in Hour.
    rows = [{**row, "Period": "0", "Hour": str(hour)} for hour, row in
            enumerate(volume_rows("20250601", "SUD", [(1, 2000.0, 1800.0), (2, 2100.0, 1900.0)], "MGP"), start=1)]
    volumes = fetch_intraday.build_volumes(rows)["SUD"]
    assert [r["time"] for r in volumes["sold"]] == ["00:00", "00:15", "00:30", "00:45", "01:00", "01:15", "01:30", "01:45"]
    assert [r["mw"] for r in volumes["bought"]][3:5] == [1800.0, 1900.0]
