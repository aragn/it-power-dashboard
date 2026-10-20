"""Checks for etl/fetch_map_prices.py.  Run: python -m pytest tests"""

import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import compact  # noqa: E402
import entsoe_api  # noqa: E402
import fetch_map_prices as map_prices  # noqa: E402


def imbalance_series(start, end, points, resolution="PT15M"):
    """points: (position, category, price)."""
    body = "".join(
        f"<Point><position>{p}</position><imbalance_Price.amount>{v}</imbalance_Price.amount>"
        f"<imbalance_Price.category>{c}</imbalance_Price.category></Point>"
        for p, c, v in points
    )
    return (f"<TimeSeries><curveType>A01</curveType><Period><timeInterval><start>{start}</start><end>{end}</end>"
            f"</timeInterval><resolution>{resolution}</resolution>{body}</Period></TimeSeries>")


def price_series(start, end, resolution, points, contract="A01", sequence=None):
    sequence_xml = (f"<classificationSequence_AttributeInstanceComponent.position>{sequence}"
                    "</classificationSequence_AttributeInstanceComponent.position>" if sequence is not None else "")
    body = "".join(f"<Point><position>{p}</position><price.amount>{v}</price.amount></Point>" for p, v in points)
    return (f"<TimeSeries><contract_MarketAgreement.type>{contract}</contract_MarketAgreement.type>{sequence_xml}"
            f"<currency_Unit.name>EUR</currency_Unit.name><curveType>A01</curveType>"
            f"<Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
            f"<resolution>{resolution}</resolution>{body}</Period></TimeSeries>")


def document(*series):
    return ET.fromstring('<Document xmlns="urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:4">'
                         + "".join(series) + "</Document>")


def values(records, group):
    return [(r["time"], r["value"]) for r in records if r["group"] == group]


def test_single_price_area_has_one_series():
    # 15 October 2025 00:00-00:30 local; both categories at the same price.
    root = document(imbalance_series("2025-10-14T22:00Z", "2025-10-14T22:30Z",
                                     [(1, "A04", 80), (1, "A05", 80), (2, "A04", 95), (2, "A05", 95)]))
    records = map_prices.imbalance_records(root, "FR")
    assert values(records, "FR|imbalance") == [("00:00", 80.0), ("00:15", 95.0)]
    assert not values(records, "FR|imbalance_short")


def test_dual_price_area_keeps_the_short_price_too():
    root = document(imbalance_series("2025-10-14T22:00Z", "2025-10-14T22:30Z",
                                     [(1, "A04", 60), (1, "A05", 140), (2, "A05", 150)]))
    records = map_prices.imbalance_records(root, "CH")
    # The excess balance price, the insufficient one filling its gap.
    assert values(records, "CH|imbalance") == [("00:00", 60.0), ("00:15", 150.0)]
    assert values(records, "CH|imbalance_short") == [("00:00", 140.0), ("00:15", 150.0)]


def test_no_matching_data_gives_no_records():
    assert map_prices.imbalance_records(None, "ME") == []
    assert map_prices.day_ahead_records(None, "NORD") == []


def test_zone_day_ahead_keeps_the_auction_at_its_finest_resolution():
    root = document(
        price_series("2025-10-14T22:00Z", "2025-10-14T23:00Z", "PT60M", [(1, 60)]),
        price_series("2025-10-14T22:00Z", "2025-10-14T23:00Z", "PT15M", [(1, 40), (2, 50), (3, 70), (4, 80)]),
        price_series("2025-10-14T22:00Z", "2025-10-14T23:00Z", "PT15M", [(1, 999)], contract="A07"),
    )
    records = map_prices.day_ahead_records(root, "NORD")
    assert values(records, "NORD|day_ahead") == [("00:00", 40.0), ("00:15", 50.0), ("00:30", 70.0), ("00:45", 80.0)]


def test_output_round_trips_through_the_compact_format():
    root = document(imbalance_series("2025-10-14T22:00Z", "2025-10-14T23:00Z",
                                     [(1, "A04", 40), (2, "A04", 50), (3, "A04", 70), (4, "A04", 80)]))
    existing = {resolution: {} for resolution in map_prices.RESOLUTIONS}
    series = entsoe_api.merge_resolutions(existing, map_prices.imbalance_records(root, "AT"),
                                          map_prices.RESOLUTIONS, daily="mean")
    payload = map_prices.build_output(series)
    assert payload["areas"]["AT"] == "Austria" and payload["areas"]["CALA"] == "Calabria"
    decoded = compact.decode_tree(payload)["series"]
    assert decoded["hourly"]["AT|imbalance"] == [{"date": "2025-10-15", "time": "00:00", "value": 60.0}]
    assert decoded["daily"]["AT|imbalance"] == [{"date": "2025-10-15", "value": 60.0}]
