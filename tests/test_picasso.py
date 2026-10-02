"""Checks for fetch_picasso.  Run: python -m pytest tests"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_picasso as fp  # noqa: E402

HEADER = "Time (ISO 8601);TERNA_POS;TERNA_NEG;APG_POS;APG_NEG;SG_POS;SG_NEG"


def csv_text(*rows):
    return "\n".join([HEADER, *rows]) + "\n"


def test_cycles_are_split_by_direction_and_compared_with_italy():
    # 22:00Z is midnight in Rome (CEST): the market day's first quarter-hour.
    text = csv_text(
        "2026-09-27T22:00:00Z;100;N/A;100;N/A;N/A;N/A",   # up, same as Italy
        "2026-09-27T22:00:04Z;120;N/A;90;N/A;N/A;N/A",    # up, Austria apart
        "2026-09-27T22:00:08Z;N/A;-50;N/A;-50;N/A;N/A",   # down, same
        "2026-09-27T22:00:12Z;70;70;N/A;20;N/A;N/A",      # no activation in Italy
        "2026-09-27T22:15:00Z;N/A;N/A;80;N/A;N/A;N/A",    # Italy without a price
    )
    rows = fp.series_rows(fp.quarter_sums(text))
    first = {group: by_resolution["quarter_hourly"][0] for group, by_resolution in rows.items()}
    assert first["IT|share_up"] == {"date": "2026-09-28", "time": "00:00", "value": 50.0}
    assert first["IT|share_down"]["value"] == 25.0 and first["IT|share_none"]["value"] == 25.0
    assert first["IT|price_up"]["value"] == 110.0 and first["IT|price_down"]["value"] == -50.0
    assert first["AT|coupled"]["value"] == 50.0
    # Austria's 00:15 cycle has no Italian price to compare with.
    assert [row["time"] for row in rows["AT|coupled"]["quarter_hourly"]] == ["00:00"]
    assert [row["time"] for row in rows["AT|share_up"]["quarter_hourly"]] == ["00:00", "00:15"]
    # Hours and days add the cycles up rather than averaging the quarter-hours.
    assert rows["AT|share_up"]["hourly"] == [{"date": "2026-09-28", "time": "00:00", "value": 60.0}]
    assert rows["AT|price_up"]["daily"] == [{"date": "2026-09-28", "value": 90.0}]
    assert not any(group.startswith("CH|") for group in rows)
    assert "IT|coupled" not in rows


def test_hours_are_labelled_by_elapsed_time_on_the_autumn_clock_change():
    # 25 October 2026: 02:00 local happens twice, the second time at 01:00Z.
    text = csv_text("2026-10-25T01:00:00Z;10;N/A;10;N/A;N/A;N/A")
    rows = fp.series_rows(fp.quarter_sums(text))
    assert rows["IT|share_up"]["hourly"] == [{"date": "2026-10-25", "time": "03:00", "value": 100.0}]


def test_a_refetched_day_replaces_its_rows_only():
    day_one = fp.series_rows(fp.quarter_sums(csv_text("2026-09-27T22:00:00Z;100;N/A;100;N/A;N/A;N/A")))
    day_two = fp.series_rows(fp.quarter_sums(csv_text("2026-09-28T22:00:00Z;N/A;5;N/A;5;N/A;N/A")))
    series = fp.merge({}, {date(2026, 9, 28): day_one, date(2026, 9, 29): day_two})
    again = fp.series_rows(fp.quarter_sums(csv_text("2026-09-27T22:00:00Z;N/A;-5;N/A;-5;N/A;N/A")))
    series = fp.merge(series, {date(2026, 9, 28): again})
    assert series["IT|share_up"]["daily"] == [{"date": "2026-09-28", "value": 0.0}, {"date": "2026-09-29", "value": 0.0}]
    # The old up price of 28 September is gone, not left behind.
    assert series["IT|price_up"]["daily"] == []
    assert series["IT|price_down"]["daily"] == [{"date": "2026-09-28", "value": -5.0}, {"date": "2026-09-29", "value": 5.0}]
    assert fp.italy_days(series) == {"2026-09-28", "2026-09-29"}
