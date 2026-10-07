"""Checks for fetch_market_offers (MSD, MB, MI-XBID).  Run: python -m pytest tests"""

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_market_offers as mo  # noqa: E402

UNITS = {"UP_GAS_1": ("gas", "OP", "SUD", "production")}


def msd(unit, period, purpose, scope, status, quantity, accepted=0, price="100", type_cd="REG"):
    return {"UNIT_REFERENCE_NO": unit, "PERIOD": str(period), "PURPOSE_CD": purpose, "SCOPE": scope,
            "STATUS_CD": status, "QUANTITY_NO": str(quantity), "AWARDED_QUANTITY_NO": str(accepted),
            "ENERGY_PRICE_NO": price, "AWARDED_PRICE_NO": price, "TYPE_CD": type_cd, "ZONE_CD": "SUD"}


def test_balancing_offers_events_and_what_is_left_out():
    rows = [
        msd("UP_GAS_1", 49, "OFF", "GR1", "ACC", 50, 20, "180", "STND"),
        msd("UP_GAS_1", 49, "BID", "GR2", "REJ", 30, 0, "40"),
        msd("UP_GAS_1", 49, "OFF", "ACC", "ACC", 0, 0, "15000"),     # an accepted start-up
        msd("UP_GAS_1", 49, "OFF", "CA", "SUB", 0, 0, "900"),        # only submitted: out
        msd("UP_GAS_1", 49, "OFF", "GR3", "REP", 10, 0, "200"),      # replaced: out
        msd("UP_GAS_1", 49, "OFF", "GR1", "REJ", 0, 0, "1"),         # no MW: out
    ]
    data = mo.build_balancing(rows, UNITS, date(2026, 9, 17))
    quarter, = data["quarters"]
    assert quarter["time"] == "12:00"
    records = [quarter["offers"][i:i + 8] for i in range(0, len(quarter["offers"]), 8)]
    assert records == [[0, 0, 0, 50.0, 180.0, 0, 20.0, 1], [0, 1, 1, 30.0, 40.0, 1, 0, 0]]
    assert quarter["events"] == [0, 0, 15000.0]
    assert data["units"][0][:2] == ["UP_GAS_1", "gas"]


def test_xbid_products():
    day = date(2026, 9, 17)
    assert mo.product_slot("20260917-H02-QH05-NORD", day) == ("quarter", 4)
    assert mo.product_slot("20260917-H10-SVIZ", day) == ("hour", 9)
    assert mo.product_slot("20260918-H01-QH01-NORD", day) is None


def xbid(product, purpose, mw, price, stamp, status="ACC", unit="UP_GAS_1"):
    return {"UNIT_REFERENCE_NO": unit, "PRODOTTO": product, "PURPOSE_CD": purpose, "STATUS_CD": status,
            "AWARDED_QUANTITY_NO": str(mw), "AWARDED_PRICE_NO": str(price), "TIMESTAMP": stamp}


def test_xbid_fills_grouped_by_price_and_minute():
    day = date(2026, 9, 17)
    rows = [
        # QH49 = 12:00-12:15 local (10:00 UTC); traded at 11:00 local: 60 minutes before.
        xbid("20260917-H13-QH49-SUD", "OFF", 2.0, 150.5, "2026-09-17T11:00:10+02:00"),
        xbid("20260917-H13-QH49-SUD", "OFF", 1.5, 150.5, "2026-09-17T10:59:50+02:00"),
        xbid("20260917-H13-QH49-SUD", "BID", 3.0, 151.0, "2026-09-17T11:30:00+02:00"),
        xbid("20260917-H13-QH49-SUD", "OFF", 9.0, 140.0, "2026-09-17T11:10:00+02:00", status="REV"),
        xbid("20260917-H05-SVIZ", "OFF", 10.0, 120.0, "2026-09-16T20:00:00+02:00", unit="UPV_SWGI1O"),
    ]
    data = mo.build_xbid(rows, day, UNITS)
    quarter, = data["quarters"]
    fills = [quarter["fills"][i:i + 5] for i in range(0, len(quarter["fills"]), 5)]
    assert quarter["time"] == "12:00"
    assert fills == [[0, 0, 3.5, 150.5, 60], [0, 1, 3.0, 151.0, 30]]
    hour, = data["hours"]
    assert hour["time"] == "04:00" and hour["fills"][1:] == [0, 10.0, 120.0, 480]
    assert data["units"][1][1] == "interconnection"     # a virtual import unit


def test_index(tmp_path):
    mo.write_day(str(tmp_path / "MSD"), {"date": "2026-09-17", "market": "MSD", "units": [],
                                         "quarters": [{"time": "12:00", "offers": [], "events": []}]})
    mo.write_day(str(tmp_path / "XBID"), {"date": "2026-09-17", "market": "XBID", "units": [],
                                          "quarters": [{"time": "00:15", "fills": []}],
                                          "hours": [{"time": "01:00", "fills": []}]})
    index = mo.write_index(str(tmp_path))
    assert index["MSD"]["days"]["2026-09-17"] == ["12:00"]
    assert index["XBID"]["days"]["2026-09-17"] == ["00:15", "01:00", "01:15", "01:30", "01:45"]
    assert index["XBID"]["files"]["2026-09-17"] == "XBID/2026-09-17.json.gz"
    assert json.loads((tmp_path / "index.json").read_text())["MSD"]["files"]
    assert mo.on_file(str(tmp_path), "MSD") == {"2026-09-17"}
    mo.write_day(str(tmp_path / "MB"), {"date": "2026-08-15", "market": "MB", "units": [], "quarters": []})
    mo.write_index(str(tmp_path))
    assert mo.on_file(str(tmp_path), "MB") == set()          # an empty day is read again


def test_mb_rows_give_the_hour_and_its_quarter():
    row = msd("UP_GAS_1", 1, "OFF", "AS", "ACC", 0, 31.5, "300")
    del row["PERIOD"]
    row.update({"INTERVAL_NO": "16", "QUARTER_NO": "2"})
    quarter, = mo.build_balancing([row], UNITS, date(2026, 9, 17))["quarters"]
    assert quarter["time"] == "15:15"                       # hour 16 = 15:00-16:00, its second quarter
    assert quarter["offers"][3] == 31.5                     # 0 MW offered: what was taken


def test_the_autumn_clock_change_in_msd_and_xbid(tmp_path):
    autumn = date(2026, 10, 25)
    # MSD period 13 is the second 02:00 (clock 02:00*); XBID hour 4 (H04) is the repeated hour.
    quarter, = mo.build_balancing([msd("UP_GAS_1", 13, "OFF", "GR1", "ACC", 10, 10, "200")], UNITS, autumn)["quarters"]
    assert (quarter["time"], quarter["period"]) == ("02:00*", 13)
    data = mo.build_xbid([xbid("20261025-H04-SUD", "OFF", 5.0, 90.0, "2026-10-24T20:00:00+02:00")], autumn, UNITS)
    hour, = data["hours"]
    assert (hour["time"], hour["period"]) == ("02:00*", 4)
    # Delivery starts 3 hours after local midnight (01:00 UTC); traded 18:00 UTC the day before.
    assert hour["fills"][4] == 7 * 60
    mo.write_day(str(tmp_path / "XBID"), {**data, "date": "2026-10-25", "market": "XBID"})
    times = mo.write_index(str(tmp_path))["XBID"]["days"]["2026-10-25"]
    assert times == ["02:00*", "02:15*", "02:30*", "02:45*"]
