"""Checks for etl/fetch_neighbour_prices.py.  Run: python -m pytest tests"""

import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import entsoe_api  # noqa: E402
import fetch_neighbour_prices as prices  # noqa: E402


def price_series(start, end, resolution, points, curve="A01", contract="A01", sequence=None):
    sequence_xml = (
        f"<classificationSequence_AttributeInstanceComponent.position>{sequence}"
        "</classificationSequence_AttributeInstanceComponent.position>"
        if sequence is not None else ""
    )
    body = "".join(
        f"<Point><position>{p}</position><price.amount>{v}</price.amount></Point>"
        for p, v in points
    )
    return (
        f"<TimeSeries><contract_MarketAgreement.type>{contract}</contract_MarketAgreement.type>"
        f"{sequence_xml}<currency_Unit.name>EUR</currency_Unit.name><curveType>{curve}</curveType>"
        f"<Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
        f"<resolution>{resolution}</resolution>{body}</Period></TimeSeries>"
    )


def document(*series):
    return ET.fromstring(
        '<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">'
        + "".join(series) + "</Publication_MarketDocument>"
    )


def parse(root):
    return prices.finest_resolution(
        entsoe_api.parse_points(root, group_of=lambda ts: "FR", keep=prices.is_day_ahead)
    )


def test_price_amount_points_and_compressed_curve():
    # 15 October 2025 00:00-01:00 local; A03 repeats 50 until position 3.
    root = document(price_series("2025-10-14T22:00Z", "2025-10-14T23:00Z", "PT15M", [(1, 50), (3, 70)], curve="A03"))
    assert [(r["time"], r["value"]) for r in parse(root)] == [
        ("00:00", 50.0), ("00:15", 50.0), ("00:30", 70.0), ("00:45", 70.0)
    ]


def test_only_the_day_ahead_auction_is_kept():
    root = document(
        price_series("2025-07-14T22:00Z", "2025-07-14T23:00Z", "PT60M", [(1, 100)]),
        price_series("2025-07-14T22:00Z", "2025-07-14T23:00Z", "PT60M", [(1, 999)], contract="A07"),
        price_series("2025-07-14T22:00Z", "2025-07-14T23:00Z", "PT60M", [(1, 888)], sequence=2),
    )
    assert [r["value"] for r in parse(root)] == [100.0]


def test_finest_resolution_wins_for_a_day():
    root = document(
        price_series("2025-10-14T22:00Z", "2025-10-14T23:00Z", "PT60M", [(1, 60)]),
        price_series("2025-10-14T22:00Z", "2025-10-14T23:00Z", "PT15M", [(1, 40), (2, 50), (3, 70), (4, 80)]),
    )
    rows = parse(root)
    assert {r["minutes"] for r in rows} == {15}
    series = entsoe_api.build_resolutions(rows, daily="mean")
    assert series["hourly"]["FR"] == [{"date": "2025-10-15", "time": "00:00", "value": 60.0}]


def test_daily_price_is_the_average_not_the_sum():
    root = document(price_series("2025-07-14T22:00Z", "2025-07-15T00:00Z", "PT60M", [(1, 100), (2, 300)]))
    series = entsoe_api.build_resolutions(parse(root), daily="mean")
    assert series["daily"]["FR"] == [{"date": "2025-07-15", "value": 200.0}]
