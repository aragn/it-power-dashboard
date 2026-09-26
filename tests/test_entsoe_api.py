"""Checks for etl/entsoe_api.py parsing and time handling.  Run: python -m pytest tests"""

import os
import sys
import xml.etree.ElementTree as ET
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import entsoe_api  # noqa: E402


def document(series_xml):
    return ET.fromstring(
        '<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">'
        f"{series_xml}</Publication_MarketDocument>"
    )


def timeseries(start, end, resolution, points, curve="A01"):
    body = "".join(
        f"<Point><position>{p}</position><quantity>{q}</quantity></Point>" for p, q in points
    )
    return (
        f"<TimeSeries><curveType>{curve}</curveType><Period>"
        f"<timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
        f"<resolution>{resolution}</resolution>{body}</Period></TimeSeries>"
    )


def test_api_period_boundaries_are_italian_midnight():
    # CET in winter (UTC+1), CEST in summer (UTC+2).
    assert entsoe_api.to_api_datetime(date(2025, 1, 15)) == "202501142300"
    assert entsoe_api.to_api_datetime(date(2025, 7, 15)) == "202507142200"
    assert entsoe_api.to_api_datetime(date(2025, 7, 15), end_of_day=True) == "202507152200"


def test_utc_points_get_italian_market_labels():
    # 22:00Z on 14 July is 00:00 local on 15 July.
    root = document(timeseries("2025-07-14T22:00Z", "2025-07-15T00:00Z", "PT60M", [(1, 5), (2, 7)]))
    rows = entsoe_api.parse_points(root)
    assert [(r["date"], r["time"], r["value"]) for r in rows] == [
        ("2025-07-15", "00:00", 5.0),
        ("2025-07-15", "01:00", 7.0),
    ]


def test_spring_dst_day_has_23_consecutive_hours():
    # 30 March 2025: local midnight 23:00Z, next local midnight 22:00Z.
    points = [(p, p) for p in range(1, 24)]
    root = document(timeseries("2025-03-29T23:00Z", "2025-03-30T22:00Z", "PT60M", points))
    rows = entsoe_api.parse_points(root)
    assert len(rows) == 23
    assert {r["date"] for r in rows} == {"2025-03-30"}
    assert rows[-1]["time"] == "22:00"  # period-based, like GME


def test_autumn_dst_day_reaches_24_00():
    points = [(p, p) for p in range(1, 26)]
    root = document(timeseries("2025-10-25T22:00Z", "2025-10-26T23:00Z", "PT60M", points))
    rows = entsoe_api.parse_points(root)
    assert len(rows) == 25
    assert rows[-1]["time"] == "24:00"


def test_compressed_series_repeat_values_until_next_point():
    # A03 lists only changes: positions 1..4 = 10, 5..8 = 20.
    root = document(timeseries("2025-10-14T22:00Z", "2025-10-15T00:00Z", "PT15M", [(1, 10), (5, 20)], "A03"))
    rows = entsoe_api.parse_points(root)
    assert [r["value"] for r in rows] == [10.0] * 4 + [20.0] * 4
    assert rows[-1]["time"] == "01:45"


def test_uncompressed_gaps_stay_missing():
    root = document(timeseries("2025-10-14T22:00Z", "2025-10-15T00:00Z", "PT15M", [(1, 10), (5, 20)]))
    rows = entsoe_api.parse_points(root)
    assert [r["time"] for r in rows] == ["00:00", "01:00"]


def test_resolutions_expand_hourly_and_aggregate():
    root = document(timeseries("2025-07-14T22:00Z", "2025-07-15T00:00Z", "PT60M", [(1, 100), (2, 300)]))
    series = entsoe_api.build_resolutions(entsoe_api.parse_points(root))
    quarter = series["quarter_hourly"]["TOTAL"]
    assert [r["time"] for r in quarter] == ["00:00", "00:15", "00:30", "00:45", "01:00", "01:15", "01:30", "01:45"]
    assert [r["value"] for r in series["hourly"]["TOTAL"]] == [100.0, 300.0]
    assert series["daily"]["TOTAL"] == [{"date": "2025-07-15", "value": 400.0}]


def test_merge_orders_24_hour_labels_after_23():
    merged = entsoe_api.merge_group_series(
        {"A": [{"date": "2025-10-26", "time": "24:00", "value": 1}]},
        {"A": [{"date": "2025-10-26", "time": "23:00", "value": 2}]},
    )
    assert [r["time"] for r in merged["A"]] == ["23:00", "24:00"]
