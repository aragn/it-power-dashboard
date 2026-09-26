"""Round-trip checks for etl/compact.py.  Run with: python -m pytest tests"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import compact  # noqa: E402


def quarter_rows(day, count, value=100.0):
    rows = []
    for period in range(1, count + 1):
        minutes = (period - 1) * 15
        rows.append({
            "date": day,
            "time": f"{minutes // 60:02d}:{minutes % 60:02d}",
            "period": period,
            "pun": value + period,
        })
    return rows


def roundtrip(rows, resolution, field="value"):
    return compact.decode_series(compact.encode_series(rows, resolution, field))


def test_dst_days_keep_period_based_labels():
    # 23-hour and 25-hour days: GME labels the 25th hour "24:00".
    rows = quarter_rows("2025-03-30", 92) + quarter_rows("2025-10-26", 100)
    assert roundtrip(rows, "quarter_hourly", "pun") == rows
    assert rows[-1]["time"] == "24:45"


def test_gaps_and_missing_days_are_preserved():
    rows = [
        {"date": "2025-01-01", "time": "00:00", "value": 5.0},
        {"date": "2025-01-01", "time": "03:15", "value": 2.5},
        {"date": "2025-01-04", "time": "23:45", "value": 0.0},
    ]
    assert roundtrip(rows, "quarter_hourly") == rows


def test_hourly_and_daily():
    hourly = [{"date": "2025-01-01", "time": "01:00", "hour": 2, "price": 1.25}]
    daily = [{"date": "2025-01-01", "price": 3.0}, {"date": "2025-01-03", "price": -4.5}]
    assert roundtrip(hourly, "hourly", "price") == hourly
    assert roundtrip(daily, "daily", "price") == daily


def test_empty_series():
    assert roundtrip([], "quarter_hourly") == []
    assert roundtrip([], "daily") == []


def test_skip_before_drops_earlier_days():
    rows = quarter_rows("2025-09-30", 4) + quarter_rows("2025-10-01", 4)
    encoded = compact.encode_series(rows, "quarter_hourly", "pun", skip_before="2025-10-01")
    assert compact.decode_series(encoded) == rows[4:]


def test_refuses_data_it_cannot_represent():
    duplicate = [{"date": "2025-01-01", "time": "00:00", "value": 1.0}] * 2
    with pytest.raises(ValueError):
        compact.encode_series(duplicate, "quarter_hourly")

    mismatched_period = [{"date": "2025-01-01", "time": "00:15", "period": 1, "pun": 1.0}]
    with pytest.raises(ValueError):
        compact.encode_series(mismatched_period, "quarter_hourly", "pun")


def test_dump_and_load(tmp_path):
    path = str(tmp_path / "data.json")
    rows = quarter_rows("2025-10-01", 96)
    compact.dump({"source": "test", "quarter_hourly": compact.encode_series(rows, "quarter_hourly", "pun")}, path)
    assert compact.load(path) == {"format": compact.FORMAT, "source": "test", "quarter_hourly": rows}
    assert not os.path.exists(path + ".tmp")
