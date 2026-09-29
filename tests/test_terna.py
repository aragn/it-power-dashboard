"""Checks for etl/fetch_terna.py.  Run: python -m pytest tests"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import compact  # noqa: E402
import entsoe_api  # noqa: E402
import fetch_terna  # noqa: E402


def record(local, offset, value):
    return {"date": local, "date_tz": "Europe/Rome", "date_offset": offset,
            "actual_generation_GWh": value, "primary_source": "Geothermal"}


def test_labels_follow_market_time_across_the_autumn_dst_change():
    # 02:00 local happens twice on 26 Oct 2025: first at +02:00, then +01:00.
    assert fetch_terna.record_label(record("2025-10-26 00:00:00", "+02:00", "0.6")) == ("2025-10-26", "00:00")
    assert fetch_terna.record_label(record("2025-10-26 02:00:00", "+02:00", "0.6")) == ("2025-10-26", "02:00")
    assert fetch_terna.record_label(record("2025-10-26 02:00:00", "+01:00", "0.6")) == ("2025-10-26", "03:00")
    assert fetch_terna.record_label(record("2025-10-26 23:45:00", "+01:00", "0.6")) == ("2025-10-26", "24:45")


def test_gw_values_become_mw_with_hourly_average_and_daily_energy():
    spec = fetch_terna.SERIES["geothermal"]
    points = fetch_terna.parse_records([
        record("2026-09-28 00:00:00", "+02:00", "0.56"),
        record("2026-09-28 00:15:00", "+02:00", "0.58"),
        record("2026-09-28 00:30:00", "+02:00", "0.60"),
        record("2026-09-28 00:45:00", "+02:00", "0.62"),
        record("2026-09-28 01:00:00", "+02:00", None),
    ], spec)
    assert [p["value"] for p in points] == [560.0, 580.0, 600.0, 620.0]
    merged = entsoe_api.merge_resolutions({}, points, fetch_terna.RESOLUTIONS)
    decoded = compact.decode_tree(fetch_terna.build_output({"geothermal": merged, "total_load": fetch_terna.empty()}))
    assert decoded["geothermal"]["hourly"]["TOTAL"] == [{"date": "2026-09-28", "time": "00:00", "value": 590.0}]
    assert decoded["geothermal"]["daily"]["TOTAL"] == [{"date": "2026-09-28", "value": 590.0}]
