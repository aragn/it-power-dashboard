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
