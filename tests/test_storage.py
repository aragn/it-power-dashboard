"""Checks for build_storage (the storage units' file of the Storage tab).  Run: python -m pytest tests"""

import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import pytest  # noqa: E402

import build_storage as bs  # noqa: E402

UNITS = [["UP_BESS_1", "battery", "BESS SPA", "SUD", "production"],
         ["UP_POMPA_1", None, "ENEL", "NORD", "production"],
         ["UP_SOLE_1", "solar", "SOLE", "SUD", "production"]]


def write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "DATA", str(tmp_path))
    database = {"columns": ["code", "codes", "source", "operator", "zone", "capacity_mw", "offered_mw", "category"],
                "units": [["UP_BESS_1", None, "battery", "BESS SPA", "SUD", None, 50.0, None],
                          ["UP_POMPA_1", None, "pumped_hydro", "ENEL", "NORD", 1000.0, 980.0, "portfolio"],
                          ["UP_SOLE_1", None, "solar", "SOLE", "SUD", 10.0, 10.0, None]]}
    with open(tmp_path / "gme_units.json", "w", encoding="utf-8") as handle:
        json.dump(database, handle)
    monkeypatch.setattr(bs, "UNITS_PATH", str(tmp_path / "gme_units.json"))
    return tmp_path


def mgp_day(bids=True):
    # 12:00 (period 49): the battery buys 20 MW at 50 (bid 30 MW at 60), offers 40 MW at 200 (not taken);
    # the pumped hydro sells 100 MW at 50 (offered 100 MW at 10); the solar plant sells (not storage).
    quarter = {"time": "12:00", "period": 49, "prices": {"SUD": 50.0, "NORD": 50.0},
               "supply": [0, 40.0, 200.0, 1, 0, None, 0,
                          1, 100.0, 10.0, 0, 100.0, 50.0, 0,
                          2, 30.0, 0.0, 0, 30.0, 50.0, 0],
               "demand": [], "others": [], "purchases": [0, 20.0, 50.0, 0]}
    if bids:
        quarter["bids"] = [0, 30.0, 60.0, 0, 20.0, 0]
    evening = {"time": "19:00", "period": 77, "prices": {"SUD": 150.0},
               "supply": [0, 40.0, 120.0, 0, 40.0, 150.0, 0, 0, 10.0, 300.0, 1, 0, None, 0],
               "demand": [], "others": [], "purchases": []}
    return {"date": "2026-09-17", "market": "MGP", "version": 3 if bids else 2, "units": UNITS,
            "quarters": [quarter, evening]}


def test_a_day_of_all_markets(data):
    write(str(data / "mgp_merit" / "2026-09-17.json.gz"), mgp_day())
    # MI-A1 12:00: the battery buys 10 MW at 40 (no period: found by the time).
    write(str(data / "mi_merit" / "2026-09-17.json.gz"),
          {"date": "2026-09-17", "units": UNITS, "markets": {"MI-A1": [
              {"time": "12:00", "supply": [], "demand": [], "purchases": [0, 10.0, 40.0, 0]}]}})
    # MI-XBID: the pumped hydro sells 8 MW at 100 in the hourly product of 19:00 (period 20).
    write(str(data / "market_offers" / "XBID" / "2026-09-17.json.gz"),
          {"date": "2026-09-17", "units": UNITS, "quarters": [],
           "hours": [{"time": "19:00", "period": 20, "fills": [1, 0, 8.0, 100.0, 60]}]})
    # MSD 19:00: the battery up 5 MW at 300 (accepted), down 4 MW (RS, a band: left out).
    write(str(data / "market_offers" / "MSD" / "2026-09-17.json.gz"),
          {"date": "2026-09-17", "units": UNITS, "quarters": [
              {"time": "19:00", "period": 77, "offers": [0, 0, 0, 6.0, 300.0, 0, 5.0, 0, 0, 1, 5, 4.0, 20.0, 0, 4.0, 0],
               "events": []}]})
    built, index = bs.build(str(data / "storage"))
    assert built == ["2026-09-17"]
    day = json.load(gzip.open(data / "storage" / "2026-09-17.json.gz", "rt"))
    assert day["markets"] == ["MGP", "MI-A", "XBID", "MSD"] and day["bids"] is True
    assert [unit[:2] for unit in day["units"]] == [["UP_BESS_1", "battery"], ["UP_POMPA_1", "pumped_hydro"]]
    assert day["units"][0][4] == 50.0 and day["units"][1][4] == 1000.0      # largest offered, installed
    rows = {(item["time"], day["units"][item["rows"][i]][0]): item["rows"][i:i + bs.FIELDS]
            for item in day["quarters"] for i in range(0, len(item["rows"]), bs.FIELDS)}
    bess = rows[("12:00", "UP_BESS_1")]
    assert bess[1:9] == [0.0, 0.0, 20.0, 250.0, 40.0, 200.0, 30.0, 60.0]
    assert bess[9] == -100.0                                                # MI-A1: 10 MW x 40 x 0.25
    assert bess[13] == 40.0 + 30.0 + 10.0                                   # offered + bid + MI bought
    assert bess[14:16] == [-10.0, 0.0]                                      # net MW: MI-A bought 10
    pump = rows[("12:00", "UP_POMPA_1")]
    assert pump[1:7] == [100.0, 1250.0, 0.0, 0.0, 100.0, 10.0]
    assert rows[("19:00", "UP_POMPA_1")][10] == 200.0                       # 8 x 100 x 0.25 each quarter-hour
    assert rows[("19:00", "UP_POMPA_1")][15] == 8.0                         # MI-XBID net MW sold
    assert day["fields"] == bs.FIELDS == 18 and day["version"] == 3
    evening = rows[("19:00", "UP_BESS_1")]
    assert evening[1:3] == [40.0, 1500.0] and evening[5:7] == [50.0, (40 * 120 + 10 * 300) / 50]
    assert evening[11] == 375.0                                             # MSD 5 x 300 x 0.25; RS left out
    assert evening[16:18] == [5.0, 0.0]                                     # MSD net MW up; MB none
    assert evening[13] == 50.0 + 6.0
    totals = index["daily"]["2026-09-17"]
    assert totals["UP_BESS_1"][:3] == [round(-250.0 - 100.0 + 1500.0 + 375.0, 1), round((80.0 + 56.0) * 0.25, 1), 1250.0]
    # By market; then discharged (19:00: 40 MW sold, 1500 EUR) and charged (12:00: 20 MW in the MGP + 10 in MI-A, 350 EUR).
    assert totals["UP_BESS_1"][3:] == [-100.0, 0.0, 375.0, 0.0, 10.0, 1500.0, 7.5, 350.0]
    assert index["totals"] == bs.TOTALS
    assert index["units"]["UP_POMPA_1"] == ["pumped_hydro", "ENEL", "NORD", 1000.0, False]    # a portfolio: no benchmark
    assert index["units"]["UP_BESS_1"][4] is True
    assert "UP_SOLE_1" not in index["units"]


def test_a_day_is_built_again_only_when_its_inputs_change(data):
    write(str(data / "mgp_merit" / "2026-09-17.json.gz"), mgp_day(bids=False))
    assert bs.build(str(data / "storage"))[0] == ["2026-09-17"]
    assert bs.build(str(data / "storage"))[0] == []
    day = json.load(gzip.open(data / "storage" / "2026-09-17.json.gz", "rt"))
    assert day["bids"] is False and day["markets"] == ["MGP"]
    bess = next(item["rows"][:bs.FIELDS] for item in day["quarters"] if item["time"] == "12:00")
    assert bess[7] is None and bess[8] is None and bess[13] == 40.0 + 20.0  # no bids: the purchases
    write(str(data / "mgp_merit" / "2026-09-17.json.gz"), mgp_day(bids=True))
    assert bs.build(str(data / "storage"))[0] == ["2026-09-17"]
