"""Checks for etl/fetch_transits.py.  Run: python -m pytest tests"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import compact  # noqa: E402
import fetch_transits as transits  # noqa: E402
from entsoe_api import merge_resolutions  # noqa: E402


def transit(day, period, start, end, value):
    return {"FlowDate": day, "Hour": str((period - 1) // 4 + 1), "Period": str(period), "Market": "MGP",
            "From": start, "To": end, "Transit": str(value), "Notes": "null"}


def limit(day, period, start, end, forward, back):
    return {"FlowDate": day, "Hour": str((period - 1) // 4 + 1), "Period": str(period), "Market": "MGP",
            "From": start, "To": end, "MaxTransmissionLimitFrom": str(forward), "MaxTransmissionLimitTo": str(back),
            "Coefficient": "0.0000"}


def test_flows_keep_the_sign_of_the_pair_as_gme_names_it():
    records, pairs = transits.transit_records([transit("20261011", 1, "CNOR", "NORD", -2650.725)])
    assert pairs == {"CNOR|NORD"}
    assert records == [{"group": "CNOR|NORD|flow", "date": "2026-10-11", "time": "00:00", "minutes": 15, "value": -2650.7}]


def test_limits_named_the_other_way_round_are_turned_to_the_pair():
    # The transits name SARD -> COAC; the limits COAC -> SARD (20 MW that way, 125 MW to Corsica).
    records = transits.limit_records([limit("20261011", 1, "COAC", "SARD", 20, 125)], {"SARD|COAC"})
    assert {(r["group"], r["value"]) for r in records} == {("SARD|COAC|limit_forward", 125.0),
                                                           ("SARD|COAC|limit_back", 20.0)}


def test_no_limit_of_gme_is_left_out():
    records = transits.limit_records([limit("20261011", 1, "NORD", "SVIZ", 10000, 12847.3)], {"NORD|SVIZ"})
    assert records == []


def test_only_the_corsica_prices_are_kept():
    rows = [{"FlowDate": "20261011", "Hour": "1", "Period": "1", "Zone": zone, "Price": "100"}
            for zone in ("CORS", "COAC", "SARD", "COUP")]
    assert sorted(r["group"] for r in transits.price_records(rows)) == ["COAC", "CORS"]


def test_output_round_trips_through_compact_format():
    flows, _ = transits.transit_records([transit("20261011", p, "CSUD", "SARD", -100.0 * p) for p in range(1, 97)])
    limits = transits.limit_records([limit("20261011", p, "CSUD", "SARD", 720, 870) for p in range(1, 97)], {"CSUD|SARD"})
    series = merge_resolutions({}, flows + limits, transits.RESOLUTIONS)
    payload = transits.build_output(series, transits.empty())
    assert payload["pairs"] == ["CSUD|SARD"]
    decoded = compact.decode_tree(payload)
    pair = decoded["flows"]["hourly"]["CSUD|SARD"]
    assert sorted(pair) == ["flow", "limit_back", "limit_forward"]
    assert pair["flow"][0] == {"date": "2026-10-11", "time": "00:00", "value": -250.0}
    assert decoded["flows"]["daily"]["CSUD|SARD"]["limit_forward"] == [{"date": "2026-10-11", "value": 720.0 * 24}]


def test_a_corsica_price_without_link_capacity_is_left_out():
    prices = transits.price_records([{"FlowDate": "20261011", "Hour": "1", "Period": str(p), "Zone": "CORS", "Price": "1700"}
                                     for p in (1, 2)])
    limits = transits.limit_records([limit("20261011", 1, "SARD", "CORS", 0, 0), limit("20261011", 2, "CNOR", "CORS", 150, 0)],
                                    {"SARD|CORS", "CNOR|CORS"})
    kept = transits.mask_prices(prices, limits)
    assert [record["time"] for record in kept] == ["00:15"]
