"""Checks for etl/fetch_xbid.py.  Run: python -m pytest tests"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import compact  # noqa: E402
import fetch_xbid  # noqa: E402


def row(day, zone, hour, period, price, phase="0"):
    # GME sends every field as a string, "null" where there were no trades.
    return {
        "FlowDate": day, "Hour": str(hour), "Period": str(period), "Zone": zone, "Phase": phase,
        "Purchased": "1.000", "Sold": "1.000",
        "ReferencePrice": "null" if price is None else f"{price:.10f}",
        "MinPrice": "null", "MaxPrice": "null", "FirstPrice": "null", "LastPrice": "null",
        "LastHourPrice": "null",
    }


def test_period_zero_is_the_60_minute_product_and_the_rest_15_minute():
    rows = [
        row("20260928", "NORD", 1, 0, 100.1234560000),
        row("20260928", "NORD", 1, 1, 104.5000000000),
        row("20260928", "NORD", 1, 2, 101.3000000000),
        row("20260928", "NORD", 2, 5, 99.7500000000),
    ]
    series = fetch_xbid.build_series(fetch_xbid.extract_prices(rows))
    assert series["XBID-60"]["NORD"]["hourly"] == [{"date": "2026-09-28", "time": "00:00", "price": 100.12}]
    assert [r["time"] for r in series["XBID-15"]["NORD"]["quarter_hourly"]] == ["00:00", "00:15", "01:00"]
    # Hourly = average of the traded quarter-hours of that hour.
    assert series["XBID-15"]["NORD"]["hourly"] == [
        {"date": "2026-09-28", "time": "00:00", "price": round((104.5 + 101.3) / 2, 2)},
        {"date": "2026-09-28", "time": "01:00", "price": 99.75},
    ]
    assert series["XBID-15"]["NORD"]["daily"] == [
        {"date": "2026-09-28", "price": round((104.5 + 101.3 + 99.75) / 3, 2)}
    ]


def test_only_total_phase_italian_zones_and_traded_products_are_kept():
    rows = [
        row("20260928", "NORD", 1, 0, 150.0, phase="1"),
        row("20260928", "NORD", 1, 0, 160.0, phase="2"),
        row("20260928", "AUST", 1, 0, 170.0),
        row("20260928", "SARD", 1, 0, None),
        row("20260928", "SICI", 1, 0, 190.0),
    ]
    prices = fetch_xbid.extract_prices(rows)
    assert list(prices) == [("XBID-60", "SICI", "2026-09-28")]


def test_long_dst_day_reaches_hour_25_and_period_100():
    rows = [row("20251026", "NORD", 25, 0, 90.0), row("20251026", "NORD", 25, 100, 95.0)]
    series = fetch_xbid.build_series(fetch_xbid.extract_prices(rows))
    assert series["XBID-60"]["NORD"]["hourly"][0]["time"] == "24:00"
    assert series["XBID-15"]["NORD"]["quarter_hourly"][0]["time"] == "24:45"
    assert series["XBID-15"]["NORD"]["hourly"][0]["time"] == "24:00"


def test_output_round_trips_through_compact_format():
    rows = [row("20260928", "CSUD", (p - 1) // 4 + 1, p, 100 + p) for p in range(1, 97)]
    rows += [row("20260928", "CSUD", h, 0, 200 + h) for h in range(1, 25)]
    markets = fetch_xbid.build_series(fetch_xbid.extract_prices(rows))
    decoded = compact.decode_tree(fetch_xbid.build_output(markets))
    csud = decoded["markets"]["XBID-15"]["CSUD"]
    assert len(csud["quarter_hourly"]) == 96
    assert csud["hourly"][0] == {"date": "2026-09-28", "time": "00:00", "price": 102.5}
    assert decoded["markets"]["XBID-60"]["CSUD"]["daily"] == [{"date": "2026-09-28", "price": 212.5}]
    assert "quarter_hourly" not in decoded["markets"]["XBID-60"]["CSUD"]


def test_15_minute_volumes_kept_traded_or_not():
    rows = [
        row("20260928", "NORD", 1, 0, 100.0),
        {**row("20260928", "NORD", 1, 1, 104.5), "Sold": "341.600", "Purchased": "256.600"},
        {**row("20260928", "NORD", 1, 2, None), "Sold": "0.000", "Purchased": "0.000"},
        {**row("20260928", "NORD", 1, 3, 101.0, phase="1"), "Sold": "9.000"},
        {**row("20260928", "FRAN", 1, 1, 90.0)},
    ]
    volumes = fetch_xbid.build_volumes(fetch_xbid.extract_volumes(rows))
    assert volumes["NORD"]["sold"] == [
        {"date": "2026-09-28", "time": "00:00", "mw": 342},
        {"date": "2026-09-28", "time": "00:15", "mw": 0},
    ]
    assert volumes["NORD"]["bought"][0]["mw"] == 257
    decoded = compact.decode_tree(fetch_xbid.build_output(fetch_xbid.empty_markets(), volumes))
    assert decoded["volumes"]["XBID-15"]["NORD"]["sold"][0]["mw"] == 342
