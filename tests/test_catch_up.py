"""Checks for catch_up.  Run: python -m pytest tests"""

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import catch_up  # noqa: E402


def write(tmp_path, data):
    path = tmp_path / "file.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_last_day_is_the_latest_series_end(tmp_path):
    path = write(tmp_path, {"format": "compact-v1", "zones": {
        "NORD": {"hourly": {"step": 60, "start": "2026-10-01", "days": [[1] * 24] * 10},
                 "daily": {"step": 1440, "start": "2026-10-01", "values": [1] * 12}},
        # A series that stopped long ago does not count.
        "OLD": {"quarter_hourly": {"step": 15, "start": "2025-01-01", "values": [1] * 96 * 3}},
    }})
    assert catch_up.last_day(path) == date(2026, 10, 12)


def test_start_moves_back_to_the_last_day_on_file(tmp_path):
    path = write(tmp_path, {"s": {"step": 1440, "start": "2026-10-01", "values": [1] * 10}})  # to 10 Oct
    assert catch_up.start(path, date(2026, 10, 24)) == date(2026, 10, 10)
    # Within the usual window: unchanged.
    assert catch_up.start(path, date(2026, 10, 8)) == date(2026, 10, 8)


def test_start_goes_back_at_most_max_gap_days(tmp_path):
    path = write(tmp_path, {"s": {"step": 1440, "start": "2025-01-01", "values": [1] * 10}})
    assert catch_up.start(path, date(2026, 10, 24)) == date(2026, 9, 24)


def test_start_without_a_file_is_the_usual_one(tmp_path):
    assert catch_up.start(str(tmp_path / "missing.json"), date(2026, 10, 24)) == date(2026, 10, 24)
    assert catch_up.start(write(tmp_path, {"no": "series"}), date(2026, 10, 24)) == date(2026, 10, 24)
