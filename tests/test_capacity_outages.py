"""Checks for etl/fetch_outages.py and etl/fetch_capacity.py.  Run: python -m pytest tests"""

import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_capacity  # noqa: E402
import fetch_outages  # noqa: E402

NS = "urn:iec62325.351:tc57wg16:451-6:outagedocument:3:0"


def outage_doc(status="A05", revision=1, business="A54", points=((1, 0),), start="2026-08-14T22:00Z", end="2026-08-15T22:00Z"):
    pts = "".join(f"<Point><position>{p}</position><quantity>{q}</quantity></Point>" for p, q in points)
    return ET.fromstring(
        f'<Unavailability_MarketDocument xmlns="{NS}"><mRID>abc</mRID><revisionNumber>{revision}</revisionNumber>'
        f"<docStatus><value>{status}</value></docStatus><TimeSeries><businessType>{business}</businessType>"
        "<biddingZone_Domain.mRID>10Y1001A1001A73I</biddingZone_Domain.mRID>"
        "<production_RegisteredResource.pSRType.psrType>B04</production_RegisteredResource.pSRType.psrType>"
        "<production_RegisteredResource.pSRType.powerSystemResources.name>UP_TEST_1</production_RegisteredResource.pSRType.powerSystemResources.name>"
        "<production_RegisteredResource.pSRType.powerSystemResources.nominalP>400</production_RegisteredResource.pSRType.powerSystemResources.nominalP>"
        f"<Available_Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
        f"<resolution>PT60M</resolution>{pts}</Available_Period></TimeSeries></Unavailability_MarketDocument>"
    )


def test_outage_becomes_unavailable_mw_per_group_and_day():
    # 400 MW unit: fully out for 12 hours, then 150 MW available (250 out) for 12 hours.
    outage = fetch_outages.parse_document(outage_doc(points=((1, 0), (13, 150))))
    assert outage["zone"] == "NORD" and outage["psr"] == "B04" and outage["business"] == "A54"
    start = datetime(2026, 8, 14, 22, tzinfo=timezone.utc)
    end = datetime(2026, 8, 15, 22, tzinfo=timezone.utc)
    totals = fetch_outages.quarter_totals([outage], start, end)
    rows = fetch_outages.to_rows(totals["NORD|B04|A54"])
    assert rows["quarter_hourly"][0] == {"date": "2026-08-15", "time": "00:00", "value": 400.0}
    assert rows["hourly"][12] == {"date": "2026-08-15", "time": "12:00", "value": 250.0}
    assert rows["daily"] == [{"date": "2026-08-15", "value": 325.0}]


def test_overlapping_outages_of_one_unit_count_once():
    # Planned outage leaves 100 MW available, a forced one at the same time 0 MW:
    # the unit is 400 MW out (not 300 + 400), attributed to the forced outage.
    planned = fetch_outages.parse_document(outage_doc(business="A53", points=((1, 100),)))
    forced = fetch_outages.parse_document(outage_doc(business="A54", points=((1, 0),)))
    forced["mrid"] = "other"
    start = datetime(2026, 8, 14, 22, tzinfo=timezone.utc)
    end = datetime(2026, 8, 15, 22, tzinfo=timezone.utc)
    totals = fetch_outages.quarter_totals([planned, forced], start, end)
    assert set(totals) == {"NORD|B04|A54"}
    assert fetch_outages.to_rows(totals["NORD|B04|A54"])["daily"] == [{"date": "2026-08-15", "value": 400.0}]


def test_kw_documents_are_rescaled_and_retyped():
    doc = outage_doc(points=((1, 0), (13, 150000)))
    doc.find(".//{*}production_RegisteredResource.pSRType.powerSystemResources.nominalP").text = "395500"
    doc.find(".//{*}production_RegisteredResource.pSRType.psrType").text = "B09"
    outage = fetch_outages.parse_document(doc)
    fetch_outages.repair([outage], {"UP_TEST_1": "B04"})
    assert outage["nominal"] == 395.5 and outage["psr"] == "B04"
    assert [a for _, _, a in outage["periods"]] == [0.0, 150.0]
    assert fetch_outages.plausible(outage)


def test_geothermal_label_without_a_known_type_becomes_other():
    doc = outage_doc()
    doc.find(".//{*}production_RegisteredResource.pSRType.psrType").text = "B09"
    outage = fetch_outages.parse_document(doc)
    fetch_outages.repair([outage], {})
    assert outage["psr"] == "B20" and outage["nominal"] == 400.0


def test_window_clips_outages():
    outage = fetch_outages.parse_document(outage_doc(start="2026-08-01T22:00Z", end="2026-09-30T22:00Z"))
    start = datetime(2026, 8, 14, 22, tzinfo=timezone.utc)
    end = datetime(2026, 8, 15, 22, tzinfo=timezone.utc)
    rows = fetch_outages.to_rows(fetch_outages.quarter_totals([outage], start, end)["NORD|B04|A54"])
    assert [r["date"] for r in rows["daily"]] == ["2026-08-15"]
    assert len(rows["quarter_hourly"]) == 96


def test_capacity_splits_pumped_hydro_by_unit_shares():
    terna = {2025: {"NORD": {"hydro": 12000.0, "solar": 14000.0}, "SICI": {"hydro": 700.0}}}
    entsoe = {2026: {"B10": 7000.0, "B16": 11872.0}}
    units = {2026: {"NORD": 6000.0, "SICI": 1000.0}}
    years = fetch_capacity.build(terna, entsoe, units)
    nord = years["2025"]["zones"]["NORD"]
    assert nord["pumped"] == 6000.0 and nord["hydro"] == 6000.0 and nord["solar"] == 14000.0
    assert years["2025"]["zones"]["SICI"]["pumped"] == 1000.0
    assert years["2025"]["entsoe_date"] == "2026-01-01"


def test_region_names_with_hyphens_map_to_zones():
    assert fetch_capacity.region_zone("Emilia-Romagna") == "NORD"
    assert fetch_capacity.region_zone("Friuli-Venezia Giulia") == "NORD"
    assert fetch_capacity.region_zone("Trentino-Alto Adige") == "NORD"
    assert fetch_capacity.region_zone("Molise") == "SUD"
