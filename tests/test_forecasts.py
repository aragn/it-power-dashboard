"""Checks for etl/fetch_forecasts.py.  Run: python -m pytest tests"""

import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import compact  # noqa: E402
import entsoe_api  # noqa: E402
import fetch_forecasts as forecasts  # noqa: E402


def document(*series):
    return ET.fromstring(
        '<GL_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0">'
        + "".join(series) + "</GL_MarketDocument>"
    )


def series(domain_tag, points):
    body = "".join(f"<Point><position>{p}</position><quantity>{q}</quantity></Point>" for p, q in points)
    return (
        f"<TimeSeries><{domain_tag}>10YIT-GRTN-----B</{domain_tag}><curveType>A01</curveType>"
        "<Period><timeInterval><start>2026-09-27T22:00Z</start><end>2026-09-27T23:00Z</end></timeInterval>"
        f"<resolution>PT15M</resolution>{body}</Period></TimeSeries>"
    )


def test_generation_forecast_skips_scheduled_consumption():
    root = document(
        series("inBiddingZone_Domain.mRID", [(1, 20000), (2, 20100), (3, 20200), (4, 20300)]),
        series("outBiddingZone_Domain.mRID", [(1, 500), (2, 500), (3, 500), (4, 500)]),
    )
    rows = entsoe_api.parse_points(root, keep=forecasts.is_generation)
    assert [r["value"] for r in rows] == [20000.0, 20100.0, 20200.0, 20300.0]
    assert rows[0]["date"] == "2026-09-28" and rows[0]["time"] == "00:00"


def test_forecast_output_round_trips_with_hourly_average_and_daily_energy():
    root = document(series("inBiddingZone_Domain.mRID", [(1, 100), (2, 200), (3, 300), (4, 400)]))
    merged = entsoe_api.merge_resolutions({}, entsoe_api.parse_points(root), forecasts.RESOLUTIONS)
    payload = forecasts.build_output({"load_forecast": merged, "generation_forecast": forecasts.empty()})
    decoded = compact.decode_tree(payload)
    assert decoded["load_forecast"]["hourly"]["TOTAL"] == [{"date": "2026-09-28", "time": "00:00", "value": 250.0}]
    assert decoded["load_forecast"]["daily"]["TOTAL"] == [{"date": "2026-09-28", "value": 250.0}]
    assert decoded["generation_forecast"]["daily"] == {}
