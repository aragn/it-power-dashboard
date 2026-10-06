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


def test_recent_version_cuts_every_series_and_keeps_the_rest():
    from datetime import date
    daily = compact.encode_series([{"date": f"2026-01-0{d}", "value": d} for d in range(1, 6)], "daily")
    quarter = compact.encode_series(quarter_rows("2026-01-02", 4) + quarter_rows("2026-01-04", 4), "quarter_hourly", "pun")
    payload = {"source": "x", "zones": {"NORD": {"daily": daily, "quarter_hourly": quarter}}, "notes": [1, 2]}
    cut = compact.recent(payload, "2026-01-03")
    assert cut["source"] == "x" and cut["notes"] == [1, 2]
    assert compact.decode_series(cut["zones"]["NORD"]["daily"]) == [
        {"date": f"2026-01-0{d}", "value": float(d)} for d in (3, 4, 5)]
    rows = compact.decode_series(cut["zones"]["NORD"]["quarter_hourly"])
    assert {row["date"] for row in rows} == {"2026-01-04"} and len(rows) == 4
    # A series starting after the cut is left as it is.
    assert compact.recent(payload, "2025-12-01") == payload


def test_dump_writes_a_recent_version_of_the_long_series_only(tmp_path):
    from datetime import date, timedelta
    today = date(2026, 10, 5)
    rows = [{"date": (today - timedelta(days=n)).isoformat(), "value": n} for n in range(200)]
    payload = {"series": compact.encode_series(rows, "daily")}
    full, other = str(tmp_path / "zonal_prices.json"), str(tmp_path / "eua.json")
    compact.dump(payload, full, today=today)
    compact.dump(payload, other, today=today)
    assert not os.path.exists(str(tmp_path / "eua.recent.json"))
    with open(compact.recent_path(full), encoding="utf-8") as handle:
        import json
        cut = json.load(handle)
    assert cut["recent_from"] == (today - timedelta(days=compact.RECENT_DAYS)).isoformat()
    assert len(compact.decode_series(cut["series"])) == compact.RECENT_DAYS + 1
    assert len(compact.load(full)["series"]) == 200
