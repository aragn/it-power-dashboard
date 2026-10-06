"""Checks for the per-unit intervals (fetch_outages) and check_outages_monthly.  Run: python -m pytest tests"""

import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import check_outages_monthly as monthly  # noqa: E402
import fetch_outages  # noqa: E402
from test_capacity_outages import outage_doc  # noqa: E402

START = datetime(2026, 8, 14, 22, tzinfo=timezone.utc)   # 15 Aug 00:00 Italian time
END = datetime(2026, 8, 15, 22, tzinfo=timezone.utc)


def test_unit_intervals_merge_equal_quarter_hours():
    # 400 MW unit: out for 12 hours, then 250 MW out for 12 hours.
    outage = fetch_outages.parse_document(outage_doc(points=((1, 0), (13, 150))))
    units = fetch_outages.unit_intervals([outage], START, END)
    assert units == {"UP_TEST_1": {"zone": "NORD", "psr": "B04", "nominal": 400.0, "intervals": [
        ["2026-08-14T22:00Z", "2026-08-15T10:00Z", 400.0, "A54"],
        ["2026-08-15T10:00Z", "2026-08-15T22:00Z", 250.0, "A54"],
    ]}}


def test_merge_units_replaces_only_the_window():
    existing = {"UP_A": {"zone": "NORD", "psr": "B04", "nominal": 400.0,
                         "intervals": [["2026-08-10T00:00Z", "2026-08-20T00:00Z", 400.0, "A53"]]}}
    new = {}  # the outage was cancelled inside the window
    merged = fetch_outages.merge_units(existing, new, START, END)
    assert merged["UP_A"]["intervals"] == [
        ["2026-08-10T00:00Z", "2026-08-14T22:00Z", 400.0, "A53"],
        ["2026-08-15T22:00Z", "2026-08-20T00:00Z", 400.0, "A53"],
    ]


def test_month_totals_and_unit_energy():
    groups = {"NORD|B04|A53": {"daily": [{"date": "2026-08-01", "value": 300.0}, {"date": "2026-08-02", "value": 100.0}]},
              "NORD|B04|A54": {"daily": [{"date": "2026-08-01", "value": 50.0}]}}
    assert monthly.month_totals(groups) == {"2026-08": {"planned": 200, "forced": 25}}
    units = {"UP_A": {"zone": "NORD", "psr": "B04", "nominal": 400.0,
                      "intervals": [["2026-08-14T22:00Z", "2026-08-15T10:00Z", 400.0, "A54"]]}}
    assert monthly.unit_months(units, "2026-08") == {"UP_A": {"2026-08": 4800}}


def test_revisions_between_snapshots():
    previous = {"checked_at": "2026-09-08T05:30Z", "months": {"2026-08": {"planned": 9715, "forced": 3857}},
                "units": {"UP_A": {"2026-08": 9600}, "UP_B": {"2026-08": 5000}}, "units_months": ["2026-08"]}
    snapshot = {"months": {"2026-08": {"planned": 9700, "forced": 3500}},
                "units": {"UP_A": {"2026-08": 2400}, "UP_C": {"2026-08": 3000}}, "units_months": ["2026-08"]}
    result = monthly.revisions(previous, snapshot, {})
    assert result["months"] == [{"month": "2026-08", "planned": [9715, 9700], "forced": [3857, 3500]}]
    changes = {u["unit"]: u["change"] for u in result["units"]}
    assert changes == {"UP_A": "reduced", "UP_B": "cancelled", "UP_C": "added"}
    assert monthly.revisions(None, snapshot, {}) is None


def test_compare_classifies_unit_hours():
    units = {"UP_A": {"zone": "NORD", "psr": "B04", "nominal": 400.0}}
    installed = {"UP_A": 400.0, "UP_B": 300.0}
    entsoe = {("UP_A", "2026-08-15", "00:00"): 400.0,   # ENTSO-E: fully out
              ("UP_A", "2026-08-15", "01:00"): 400.0,
              ("UP_A", "2026-08-15", "02:00"): 400.0,
              ("UP_A", "2026-08-15", "03:00"): 400.0}
    terna = {("UP_A", "2026-08-15", "00:00"): 0.0,       # agrees
             ("UP_A", "2026-08-15", "01:00"): 380.0,     # Terna: available
             ("UP_A", "2026-08-15", "02:00"): 380.0,
             ("UP_A", "2026-08-15", "03:00"): 380.0,
             ("UP_B", "2026-08-15", "00:00"): 100.0}     # reduced, no ENTSO-E outage
    result = monthly.compare(units, installed, entsoe, terna)
    assert result["hours_with_outage"] == 4 and result["agree_share"] == 0.25
    assert [(r["unit"], r["hours"], r["mwh"]) for r in result["overstated"]] == [("UP_A", 3, 1140)]
    assert result["understated"] == []
    assert result["terna_only"] == {"units": 1, "mwh": 200, "largest": [{"unit": "UP_B", "mwh": 200}]}


def test_terna_codes_map_onto_entsoe_names():
    terna = {("UP_S.F._DEL_5", "2026-08-15", "00:00"): 0.0}
    assert monthly.rename_terna(terna, {"UP_S.F._DEL_5", "UP_A"}) == terna
    assert monthly.rename_terna({("UP_SF_DEL_5", "2026-08-15", "00:00"): 1.0}, {"UP_S.F._DEL_5"}) == {
        ("UP_S.F._DEL_5", "2026-08-15", "00:00"): 1.0}


def test_default_month_is_the_previous_calendar_month():
    assert monthly.default_month(date(2026, 10, 8)) == "2026-09"
    assert monthly.default_month(date(2027, 1, 8)) == "2026-12"
