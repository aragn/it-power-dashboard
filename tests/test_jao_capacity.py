"""Checks for fetch_jao_capacity.  Run: python -m pytest tests"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_jao_capacity as fc  # noqa: E402


def values(records, group):
    return [(r["date"], r["time"], r["minutes"], r["value"]) for r in records if r["group"] == group]


def test_northern_import_ntc_and_limited_by_shares():
    rows = [{"dateTimeUtc": "2026-09-27T22:00:00Z", "ttc_LimitedBy": "Smoothing ramp", "border_AT_IT": 555,
             "border_CH_IT": 3001, "border_FR_IT": 3170, "border_SI_IT": 433},
            {"dateTimeUtc": "2026-09-27T22:15:00Z", "ttc_LimitedBy": "Something new", "border_AT_IT": 500,
             "border_CH_IT": None, "border_FR_IT": 3000, "border_SI_IT": 400}]
    records = fc.ntc_records(rows, "da")
    # A border missing leaves the total out rather than understating it.
    assert values(records, "ntc_da|import") == [("2026-09-28", "00:00", 15, 7159.0)]
    assert [r["value"] for r in records if r["group"] == "limit_da|ramp"] == [1.0, 0.0]
    assert [r["value"] for r in records if r["group"] == "limit_da|other"] == [0.0, 1.0]


def test_congestion_income_is_a_rate_per_hour_over_both_directions():
    rows = [{"dateTimeUtc": "2026-09-27T22:00:00Z", "grossBorder_FR_IT": 17522.2, "grossBorder_IT_FR": 0.0},
            {"dateTimeUtc": "2026-09-27T22:15:00Z", "grossBorder_FR_IT": 0.0, "grossBorder_IT_FR": 100.0},
            # JAO publishes every quarter-hour, with zeros where there was no spread.
            {"dateTimeUtc": "2026-09-27T22:30:00Z", "grossBorder_FR_IT": 0.0, "grossBorder_IT_FR": 0.0},
            {"dateTimeUtc": "2026-09-27T22:45:00Z", "grossBorder_FR_IT": 0.0, "grossBorder_IT_FR": 0.0}]
    records = fc.income_records(rows, "ci_da")
    assert values(records, "ci_da|FR")[:2] == [("2026-09-28", "00:00", 15, 70088.8), ("2026-09-28", "00:15", 15, 400.0)]
    # Summed per day: the day's income, not an average.
    merged = fc.merge({resolution: {} for resolution in fc.RESOLUTIONS}, records)
    assert merged["daily"]["ci_da|FR"][0]["value"] == 17622.2


def test_allocation_constraint_none_is_missing():
    rows = [{"dateTimeUtc": "2026-09-27T22:00:00Z", "allocationConstraintImport": 11200, "totalLoad": 23304,
             "totalNonDisp": 8299, "minDispNeeded": 350},
            {"dateTimeUtc": "2026-09-27T23:00:00Z", "allocationConstraintImport": 99999, "totalLoad": 22000,
             "totalNonDisp": 8000, "minDispNeeded": 350}]
    records = fc.constraint_records(rows, "da", inputs=True)
    assert values(records, "ac_da|import") == [("2026-09-28", "00:00", 60, 11200.0)]
    assert len(values(records, "ac_da|load")) == 2


def test_limiting_elements_and_their_country():
    rows = [{"dateTimeUtc": "2026-09-28T08:30:00Z", "limiting": True, "cneName": "PST DIVACA - DIR",
             "hubFrom": "SI", "contingencies": [{"name": "N-2 Pradella-Nauders"}]},
            {"dateTimeUtc": "2026-09-28T08:45:00Z", "limiting": False, "cneName": "Other"}]
    assert fc.limiting_rows(rows) == [{"date": "2026-09-28", "time": "10:30", "minutes": 15, "cne": "PST DIVACA - DIR",
                                       "contingency": "N-2 Pradella-Nauders", "hub": "SI"}]
    assert fc.element_area("[CH-IT] Robbia-Fiorano [DIR] [CH]", []) == "CH"
    assert fc.element_area("[IT-IT] Bovisio-Verderio [DIR][IT]", []) == "IT"
    assert fc.element_area("PST DIVACA - DIR", ["SI", "SI"]) == "SI"
