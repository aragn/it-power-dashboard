"""Compact on-disk format for the dashboard's time series.

The ETL scripts work with lists of row dicts such as
    {"date": "2025-01-01", "time": "00:15", "period": 2, "pun": 138.7}
Writing those rows verbatim repeats the date, time and field names for every
single value, which made app/data/*.json tens of megabytes.

On disk each series is instead stored as one list of values per calendar day:

    {
      "step": 15,              # minutes per slot (15, 60, or 1440 for daily)
      "field": "pun",          # name of the value field in each row
      "index": "period",       # optional 1-based slot counter ("hour"/"period")
      "start": "2025-01-01",   # first calendar day
      "days": [[v0, v1, ...], null, ...]
    }

A value's position in its day gives its time label: slot i -> i * step
minutes after midnight (formatted "HH:MM", which can reach "24:00" on the
25-hour DST day, matching GME's period-based labels). null marks a missing
slot or a day without data. Daily series (step 1440) store a flat "values"
list instead of "days".

decode_tree() turns a whole payload back into exactly the row lists the ETL
wrote; app/index.html contains the matching JavaScript decoder.
"""

import json
import os
from datetime import date, timedelta

FORMAT = "compact-v1"

STEP_MINUTES = {"quarter_hourly": 15, "hourly": 60, "daily": 1440}


def _slot(time_label, step):
    hours, minutes = (int(part) for part in time_label.split(":"))
    total = hours * 60 + minutes
    if total % step:
        raise ValueError(f"Time {time_label!r} is not aligned to {step}-minute slots")
    return total // step


def _time_label(slot, step):
    total = slot * step
    return f"{total // 60:02d}:{total % 60:02d}"


def _pack_number(value):
    # 24.0 -> 24 keeps the file shorter; decode turns it back into a float.
    value = float(value)
    return int(value) if value.is_integer() else value


def encode_series(rows, resolution, field="value", skip_before=None):
    """Encode a list of row dicts into the compact series format.

    Raises instead of silently dropping anything the format cannot represent
    (duplicate slots, unaligned times, index fields that don't match the slot).
    """
    step = STEP_MINUTES[resolution]

    if skip_before is not None:
        rows = [row for row in rows if row["date"] >= skip_before]

    series = {"step": step, "field": field}

    index_fields = {
        key for row in rows for key in row if key not in ("date", "time", field)
    }
    if len(index_fields) > 1:
        raise ValueError(f"Unexpected extra fields in rows: {sorted(index_fields)}")
    index_field = index_fields.pop() if index_fields else None
    if index_field:
        series["index"] = index_field

    if not rows:
        series["start"] = None
        series["values" if step == 1440 else "days"] = []
        return series

    first = date.fromisoformat(min(row["date"] for row in rows))
    last = date.fromisoformat(max(row["date"] for row in rows))
    day_count = (last - first).days + 1
    series["start"] = first.isoformat()

    if step == 1440:
        values = [None] * day_count
        for row in rows:
            i = (date.fromisoformat(row["date"]) - first).days
            if values[i] is not None:
                raise ValueError(f"Duplicate daily row for {row['date']}")
            values[i] = _pack_number(row[field])
        series["values"] = values
        return series

    days = [None] * day_count
    for row in rows:
        i = (date.fromisoformat(row["date"]) - first).days
        slot = _slot(row["time"], step)
        if index_field and row[index_field] != slot + 1:
            raise ValueError(
                f"{index_field}={row[index_field]} does not match time "
                f"{row['time']} on {row['date']}"
            )
        day = days[i]
        if day is None:
            day = days[i] = []
        if len(day) <= slot:
            day.extend([None] * (slot + 1 - len(day)))
        if day[slot] is not None:
            raise ValueError(f"Duplicate row for {row['date']} {row['time']}")
        day[slot] = _pack_number(row[field])

    series["days"] = days
    return series


def decode_series(series):
    """Turn a compact series back into the list of row dicts."""
    step = series["step"]
    field = series["field"]
    index_field = series.get("index")
    start = series.get("start")
    rows = []

    if start is None:
        return rows

    first = date.fromisoformat(start)

    if step == 1440:
        for i, value in enumerate(series["values"]):
            if value is not None:
                rows.append({"date": (first + timedelta(days=i)).isoformat(),
                             field: float(value)})
        return rows

    for i, day in enumerate(series["days"]):
        if not day:
            continue
        day_key = (first + timedelta(days=i)).isoformat()
        for slot, value in enumerate(day):
            if value is None:
                continue
            row = {"date": day_key, "time": _time_label(slot, step)}
            if index_field:
                row[index_field] = slot + 1
            row[field] = float(value)
            rows.append(row)
    return rows


def is_series(value):
    return isinstance(value, dict) and "step" in value and "field" in value


def decode_tree(payload):
    """Recursively replace every compact series in a payload with its rows."""
    if is_series(payload):
        return decode_series(payload)
    if isinstance(payload, dict):
        return {key: decode_tree(value) for key, value in payload.items()}
    return payload


def load(path):
    """Read a data file, returning row lists whether it is compact or legacy."""
    with open(path, "r", encoding="utf-8") as f:
        return decode_tree(json.load(f))


def dump(payload, path):
    """Write a payload atomically, so a crash never leaves a truncated file."""
    payload = {"format": FORMAT, **payload}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, path)
