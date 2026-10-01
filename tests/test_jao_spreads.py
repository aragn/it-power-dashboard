"""Checks for fetch_jao_spreads.  Run: python -m pytest tests"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_jao_spreads as fj  # noqa: E402


def row(utc, it_at, it_gr=0.0):
    return {"dateTimeUtc": utc, "border_IT_AT": it_at, "border_AT_IT": -it_at, "border_IT_FR": 1.0,
            "border_IT_SI": None, "border_IT_GR": it_gr}


def test_spreads_are_neighbour_minus_italy_in_market_time():
    # JAO's IT->AT field is Austria's price minus Italy North's.
    records = fj.spread_records("DA", [row("2026-09-27T22:00:00Z", 9.11), row("2026-09-27T22:15:00Z", 6.21)])
    austria = [(r["date"], r["time"], r["minutes"], r["value"]) for r in records if r["group"] == "DA|AT"]
    assert austria == [("2026-09-28", "00:00", 15, 9.11), ("2026-09-28", "00:15", 15, 6.21)]
    # Missing values are left out, not stored as zero.
    assert not [r for r in records if r["group"] == "DA|SI"]


def test_a_day_with_only_whole_hours_is_hourly():
    records = fj.spread_records("DA", [row("2025-03-01T00:00:00Z", 5.0), row("2025-03-01T01:00:00Z", 6.0)])
    assert {r["minutes"] for r in records} == {60}


def test_requests_stay_within_two_days_across_the_clock_change():
    from datetime import date
    chunks = list(fj.request_chunks(date(2025, 10, 24), date(2025, 10, 29)))
    assert chunks == [(date(2025, 10, 24), date(2025, 10, 25)), (date(2025, 10, 26), date(2025, 10, 26)),
                      (date(2025, 10, 27), date(2025, 10, 27)), (date(2025, 10, 28), date(2025, 10, 29))]
