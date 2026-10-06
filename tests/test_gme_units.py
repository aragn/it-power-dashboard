"""Checks for fetch_gme_units, gme_units_registry and build_gme_units.  Run: python -m pytest tests"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import build_gme_units as bu  # noqa: E402
import fetch_gme_units as fg  # noqa: E402
import gme_units_registry as gr  # noqa: E402


def offer(unit, period, quantity, status="ACC", purpose="OFF", price="0", granularity="PT15", operator="OP"):
    return {"UNIT_REFERENCE_NO": unit, "PURPOSE_CD": purpose, "STATUS_CD": status, "QUANTITY_NO": str(quantity),
            "AWARDED_QUANTITY_NO": "0", "ENERGY_PRICE_NO": price, "PERIOD": str(period), "GRANULARITY": granularity,
            "BILATERAL_IN": "false", "OPERATORE": operator, "ZONE_CD": "SUD"}


def test_xml_rows_skip_a_header_block():
    xml = (b"<NewDataSet><Header><A>1</A><B>2</B><C>3</C></Header>"
           + b"".join(f"<Offerta><UNIT_REFERENCE_NO>UP_X_1</UNIT_REFERENCE_NO><PERIOD>{p}</PERIOD>"
                      f"<QUANTITY_NO>5</QUANTITY_NO></Offerta>".encode() for p in (1, 2, 3))
           + b"</NewDataSet>")
    rows = list(fg.rows_of("x.xml", xml))
    assert [row["PERIOD"] for row in rows] == ["1", "2", "3"]
    assert rows[0]["UNIT_REFERENCE_NO"] == "UP_X_1"


def test_replaced_and_revoked_offers_are_not_counted():
    rows = [offer("UP_X_1", 1, 10), offer("UP_X_1", 1, 5), offer("UP_X_1", 1, 99, status="REP"),
            offer("UP_X_1", 1, 99, status="REV"), offer("UP_X_1", 2, 4, status="REJ")]
    summary = fg.summarise_day(rows)
    sale = summary["units"]["UP_X_1"]["purposes"]["OFF"]
    assert sale["max_period"] == 15
    assert sale["offered"] == 19
    assert summary["periods_per_hour"] == 4
    assert sale["hourly_offered"][0] == 19 / 4


def test_hour_of_quarter_hour_and_hourly_periods():
    assert fg.hour_of({"PERIOD": "1", "GRANULARITY": "PT15"}) == 0
    assert fg.hour_of({"PERIOD": "96", "GRANULARITY": "PT15"}) == 23
    assert fg.hour_of({"PERIOD": "24", "GRANULARITY": "PT60"}) == 23


def test_sample_days_are_mostly_recent_and_unique():
    days = fg.sample_days(20, date(2026, 10, 5))
    assert len(days) == len(set(days)) == 20
    assert sum(day >= date(2026, 7, 1) for day in days) >= 10
    assert 1 <= sum(day.year == 2025 for day in days) <= 6
    assert max(days) <= date(2026, 9, 25)


def test_kinds_from_the_code():
    assert bu.kind_of("UP_LIRO_1") == "production"
    assert bu.kind_of("UP_DI0182_CALA_A") == "legacy_production"
    assert bu.kind_of("UC_DP0114_NORD") == "legacy_consumption"
    assert bu.kind_of("UC_0000066_01") == "consumption"
    assert bu.kind_of("UVZi_00001_2634_NOC") == "aggregate_injection"
    assert bu.kind_of("UVZp_00001_0156_CSQ") == "aggregate_withdrawal"
    assert bu.kind_of("UPV_SWGD108373O") == "import"
    assert bu.kind_of("UCV_SWGD003171O") == "export"


def purposes(hourly, sale=100.0, low=1.0, buys=0.0, cap=50.0):
    out = {"OFF": {"days": 1, "max_period": {"PT15": cap}, "hourly_offered": hourly, "offered": sale,
                   "at_or_below_10": sale * low}}
    if buys:
        out["BID"] = {"days": 1, "max_period": {"PT15": cap}, "offered": sale * buys}
    return out


def test_type_from_the_name_then_only_solar_and_storage_from_the_bids():
    flat = [10.0] * 24
    solar = [0.0] * 7 + [10.0] * 12 + [0.0] * 5
    assert bu.infer_source("UP_PRCOEOLICO_1", purposes(flat)) == ("wind_onshore", "name")
    assert bu.infer_source("UP_TRINOBESS2_1", purposes(flat)) == ("battery", "name")
    assert bu.infer_source("UP_BAGNORE4_1", purposes(flat)) == ("geothermal", "name")
    assert bu.infer_source("UP_TRMVLRZZTR_3", purposes(flat)) == ("waste", "name")
    assert bu.infer_source("UP_XYZ_1", purposes(solar)) == ("solar", "bids")
    assert bu.infer_source("UP_XYZ_1", purposes(flat, buys=0.9)) == ("battery", "bids")
    # Flat at 0 EUR/MWh: wind, run-of-river or a gas plant under a bilateral contract.
    assert bu.infer_source("UP_XYZ_1", purposes(flat)) == (None, None)


def test_entsoe_records_per_gme_code():
    registries = {"entsoe": {
        "installed_14_1_B": {"2026|26W": {"registeredResource.name": "IM_0066050UP_TORINONORD_1",
                                          "registeredResource.mRID": "26W", "MktPSRType.psrType": "B04",
                                          "installed_MW": "368",
                                          "MktPSRType.production_PowerSystemResources.highVoltageLimit": "220"}},
        "master_A95": {
            "NORD|26W|2026-01-01|": {"registeredResource.name": "IM_0066050UP_TORINONORD_1", "registeredResource.mRID": "26W",
                                     "registeredResource.location.name": "001001272", "MktPSRType.psrType": "B09",
                                     "implementation_DateAndOrTime.date": "2026-01-01",
                                     "MktPSRType.nominalIP_PowerSystemResources.nominalP": "368000"},
        }}}
    unit = gr.entsoe_units(registries)["UP_TORINONORD_1"]
    # B09 (geothermal) outside Tuscany is a mislabel; the 14.1.B type wins.
    assert unit["source"] == "gas"
    assert unit["capacity_mw"] == 368
    assert unit["voltage_kv"] == 220
    assert unit["location"] == "001001272"


def test_place_from_the_entsoe_location():
    by_code = {"001272": {"istat": "001272", "name": "Torino", "lat": 45.07, "lon": 7.68, "current": True}}
    place = bu.place("001001272", by_code, {"001": "Torino"})
    assert place["region"] == "Piemonte"
    assert place["municipality"] == "Torino"
    assert place["province"] == "Torino"


def test_foreign_and_bilateral_only_units_are_grouped():
    def unit(zone, operators, purpose="OFF"):
        return {"days": ["2026-09-18"], "values": {"ZONE_CD": {zone: 1}, "OPERATORE": operators},
                "purposes": {purpose: {"days": 1, "max_period": {"PT15": 10.0}, "offered": 10, "priced": 10,
                                       "price_quantity": 100, "hourly_offered": [1.0] * 24}}}
    store = {
        "UPV_SWGD1O": unit("SVIZ", {"A": 1}), "UPV_SWGD2O": unit("SVIZ", {"B": 1}),
        "UVZp_00001_0001_NOQ": unit("NORD", {"Bilateralista": 3}, "BID"),
        "UVZp_00001_0002_NOQ": unit("NORD", {"Bilateralista": 2}, "BID"),
        "UP_LIRO_1": unit("NORD", {"A2A SPA": 5, "Bilateralista": 9}),
    }
    output = bu.build(store, {}, {"units": {"UP_LIRO_1": {"source": "hydro", "owner": "A2A"}}})
    rows = [dict(zip(output["columns"], row)) for row in output["units"]]
    by_kind = {row["kind"]: row for row in rows}
    assert by_kind["import"]["codes"] == ["UPV_SWGD1O", "UPV_SWGD2O"]
    assert by_kind["import"]["source"] == "interconnection"
    assert by_kind["bilateral_aggregate_withdrawal"]["codes"] == ["UVZp_00001_0001_NOQ", "UVZp_00001_0002_NOQ"]
    liro = by_kind["production"]
    assert liro["operator"] == "A2A SPA"
    assert (liro["source"], liro["source_origin"], liro["owner"]) == ("hydro", "research", "A2A")
