"""Checks for etl/fetch_res_forecasts.py.  Run: python -m pytest tests"""

import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import compact  # noqa: E402
import entsoe_api  # noqa: E402
import fetch_res_forecasts as res  # noqa: E402


def series(psr_type, points):
    body = "".join(f"<Point><position>{p}</position><quantity>{q}</quantity></Point>" for p, q in points)
    return (
        "<TimeSeries><inBiddingZone_Domain.mRID>10YIT-GRTN-----B</inBiddingZone_Domain.mRID>"
        f"<curveType>A01</curveType><MktPSRType><psrType>{psr_type}</psrType></MktPSRType>"
        "<Period><timeInterval><start>2026-09-27T22:00Z</start><end>2026-09-27T23:00Z</end></timeInterval>"
        f"<resolution>PT15M</resolution>{body}</Period></TimeSeries>"
    )


def document(*items):
    return ET.fromstring(
        '<GL_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0">'
        + "".join(items) + "</GL_MarketDocument>"
    )


def test_keeps_onshore_wind_and_solar_by_production_type():
    root = document(
        series("B19", [(1, 1000), (2, 1100), (3, 1200), (4, 1300)]),
        series("B16", [(1, 0), (2, 0), (3, 0), (4, 0)]),
        series("B18", [(1, 5), (2, 5), (3, 5), (4, 5)]),
    )
    rows = entsoe_api.parse_points(root, group_of=res.production_type, keep=res.is_wind_or_solar)
    assert {r["group"] for r in rows} == {"B19", "B16"}
    wind = [r for r in rows if r["group"] == "B19"]
    assert wind[0]["date"] == "2026-09-28" and wind[0]["time"] == "00:00"

    merged = entsoe_api.merge_resolutions({}, rows, res.RESOLUTIONS)
    payload = res.build_output({"day_ahead": merged, "intraday": res.empty()})
    decoded = compact.decode_tree(payload)
    assert decoded["day_ahead"]["hourly"]["B19"] == [{"date": "2026-09-28", "time": "00:00", "value": 1150.0}]
    assert decoded["day_ahead"]["daily"]["B16"] == [{"date": "2026-09-28", "value": 0.0}]
    assert decoded["intraday"]["daily"] == {}
