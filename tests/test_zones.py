"""Checks for the bidding-zone files of the ENTSO-E scripts (etl/zones.py).  Run: python -m pytest tests"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import pytest  # noqa: E402

import fetch_crossborder as crossborder  # noqa: E402
import fetch_forecasts as forecasts  # noqa: E402
import zones  # noqa: E402


def test_without_a_zone_the_scripts_fetch_italy():
    assert zones.targets(None, "italy.json", "x.json") == [("IT", zones.IT_DOMAIN, "italy.json")]


def test_a_zone_or_all_of_them_write_under_zones():
    [(label, domain, path)] = zones.targets("sard", "italy.json", "crossborder.json")
    assert (label, domain) == ("SARD", "10Y1001A1001A74G")
    assert path.endswith(os.path.join("app", "data", "zones", "SARD", "crossborder.json"))
    assert [target[0] for target in zones.targets("all", "italy.json", "x.json")] == list(zones.ZONE_DOMAINS)
    with pytest.raises(ValueError):
        zones.targets("CORS", "italy.json", "x.json")


def test_one_zone_failing_does_not_stop_the_others():
    done = []

    def fetch(label, domain, path):
        if label == "CNOR":
            raise RuntimeError("nothing published")
        done.append(label)

    failed = zones.run_areas(zones.targets("all", "italy.json", "x.json"), fetch)
    assert failed == ["CNOR"] and len(done) == 6
    with pytest.raises(RuntimeError):
        zones.run_areas(zones.targets("CNOR", "italy.json", "x.json"), fetch)


def test_a_forecast_is_asked_for_the_zone():
    params = forecasts.area_params(forecasts.FORECASTS["load_forecast"]["params"], zones.ZONE_DOMAINS["SUD"])
    assert params["outBiddingZone_Domain"] == "10Y1001A1001A788" and params["documentType"] == "A65"


def test_zone_borders_include_the_other_zones_and_corsica():
    assert list(crossborder.ZONE_BORDERS["SARD"]) == ["CSUD", "CORS", "COAC"]
    assert crossborder.ZONE_BORDERS["SARD"]["CORS"][1] == "10Y1001A1001A893"     # ENTSO-E's IT-SACO DC
    assert list(crossborder.ZONE_BORDERS["NORD"]) == ["CNOR", "FR", "CH", "AT", "SI"]
    # Imports into the zone: the zone receives.
    assert crossborder.domains("imports", "X", zones.ZONE_DOMAINS["NORD"]) == {"in_Domain": "10Y1001A1001A73I", "out_Domain": "X"}
    # Every zone's border is the other zone's border too.
    for zone, borders in crossborder.ZONE_BORDERS.items():
        for other in borders:
            if other in crossborder.ZONE_BORDERS:
                assert zone in crossborder.ZONE_BORDERS[other], (zone, other)
