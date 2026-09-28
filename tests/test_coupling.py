"""Checks for etl/fetch_coupling.py.  Run: python -m pytest tests"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import compact  # noqa: E402
import fetch_coupling as coupling  # noqa: E402
from entsoe_api import merge_resolutions  # noqa: E402


def coupling_row(day, hour, period, zone="XFRA", flow=100.0, limit=400.0):
    return {
        "FlowDate": day, "Hour": str(hour), "Period": str(period), "Market": "MGP", "Zone": zone,
        "ImportLimit": str(limit), "ExportLimit": "200.0", "ImportFlow": str(flow), "ExportFlow": "0.0",
    }


def test_quarter_hour_periods_after_the_15_minute_mtu():
    assert coupling.slot(coupling_row("20260927", 11, 41)) == ("2026-09-27", "10:00", 15)


def test_hourly_rows_before_it_have_period_zero():
    assert coupling.slot(coupling_row("20250315", 1, 0)) == ("2025-03-15", "00:00", 60)
    assert coupling.slot(coupling_row("20251026", 25, 0)) == ("2025-10-26", "24:00", 60)


def test_flows_and_limits_become_series_per_border():
    rows = [coupling_row("20260927", 1, p, flow=100.0 * p) for p in range(1, 5)]
    rows.append(coupling_row("20260927", 1, 1, zone="XXXX"))
    series = merge_resolutions({}, coupling.coupling_records(rows), coupling.RESOLUTIONS)
    assert sorted(series["daily"]) == [
        "XFRA|export_flow", "XFRA|export_limit", "XFRA|import_flow", "XFRA|import_limit"
    ]
    assert series["hourly"]["XFRA|import_flow"] == [{"date": "2026-09-27", "time": "00:00", "value": 250.0}]
    # Daily energy: the sum of the hourly MW.
    assert series["daily"]["XFRA|import_limit"] == [{"date": "2026-09-27", "value": 400.0}]


def test_only_italian_side_prices_are_kept():
    rows = [
        {"FlowDate": "20260927", "Hour": "1", "Period": "1", "Zone": zone, "Price": price}
        for zone, price in (("COUP", "171.5"), ("SUD", "150"), ("XFRA", "171.5"), ("NORD", "171.5"))
    ]
    assert sorted(r["group"] for r in coupling.price_records(rows)) == ["COUP", "SUD"]


def test_output_round_trips_through_compact_format():
    rows = [coupling_row("20260927", (p - 1) // 4 + 1, p) for p in range(1, 97)]
    flows = merge_resolutions({}, coupling.coupling_records(rows), coupling.RESOLUTIONS)
    decoded = compact.decode_tree(coupling.build_output(flows, coupling.empty()))
    assert len(decoded["flows"]["quarter_hourly"]["XFRA"]["import_flow"]) == 96
    assert decoded["flows"]["daily"]["XFRA"]["import_flow"] == [{"date": "2026-09-27", "value": 2400.0}]
