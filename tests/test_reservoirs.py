"""Checks for fetch_reservoirs (ENTSO-E 16.1.D).  Run: python -m pytest tests"""

import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_reservoirs as fr  # noqa: E402

RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<GL_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0">
  <TimeSeries>
    <Period>
      <timeInterval><start>2025-12-28T23:00Z</start><end>2026-01-18T23:00Z</end></timeInterval>
      <resolution>P7D</resolution>
      <Point><position>1</position><quantity>2400000</quantity></Point>
      <Point><position>2</position><quantity>2310000.5</quantity></Point>
      <Point><position>3</position><quantity>2205000</quantity></Point>
    </Period>
  </TimeSeries>
</GL_MarketDocument>"""


def test_weeks_are_dated_by_their_first_day_in_italian_time():
    weeks = fr.parse_weeks(ET.fromstring(RESPONSE))
    assert weeks == {"2025-12-29": 2400000.0, "2026-01-05": 2310000.5, "2026-01-12": 2205000.0}
    assert fr.parse_weeks(None) == {}


def test_the_output_is_sorted_and_rounded(tmp_path):
    out = fr.build_output({"2026-01-12": 2205000.04, "2025-12-29": 2400000.0})
    assert out["weeks"] == [["2025-12-29", 2400000.0], ["2026-01-12", 2205000.0]] and out["unit"] == "MWh"
    path = tmp_path / "reservoirs.json"
    path.write_text(__import__("json").dumps(out))
    assert fr.load_existing(str(path)) == {"2025-12-29": 2400000.0, "2026-01-12": 2205000.0}
