"""Checks for fetch_mi_merit and the MI-A files' public version.  Run: python -m pytest tests"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_mi_merit as mi  # noqa: E402
import publicise_gme as pg  # noqa: E402
from test_mgp_merit import UNITS, row  # noqa: E402


def test_the_three_auctions_share_one_unit_list():
    offers = {
        "MI-A1": iter([row("UP_SOLE_1", 49, 30, awarded=30), row("UP_GAS_1", 49, 100, awarded=0, status="REJ")]),
        "MI-A3": iter([row("UP_GAS_1", 49, 50, awarded=50, awarded_price="120")]),
    }
    data = mi.day_data(mi.date(2026, 9, 17), offers, UNITS)
    assert data["market"] == "MI-A" and list(data["markets"]) == ["MI-A1", "MI-A3"]   # MI-A2: no offers
    codes = [unit[0] for unit in data["units"]]
    assert codes == ["UP_SOLE_1", "UP_GAS_1"]
    a1, a3 = data["markets"]["MI-A1"][0], data["markets"]["MI-A3"][0]
    assert a1["time"] == a3["time"] == "12:00"
    assert a3["supply"][0] == codes.index("UP_GAS_1") and a3["prices"] == {"SUD": 120.0}


def test_an_auction_keeps_the_purchase_bids_per_unit():
    units = {**UNITS, "UP_BESS_1": ("battery", "BESS SPA", "SUD", "production")}
    offers = {"MI-A2": iter([row("UP_BESS_1", 49, 20, awarded=15, purpose="BID", price="60", awarded_price="55"),
                             row("UP_BESS_1", 49, 10, status="REJ", purpose="BID", price="20", awarded_price="")])}
    data = mi.day_data(mi.date(2026, 9, 17), offers, units)
    assert data["version"] == mi.FORMAT == 4
    quarter = data["markets"]["MI-A2"][0]
    bids = sorted(quarter["bids"][index:index + 6] for index in range(0, len(quarter["bids"]), 6))
    assert bids == [[0, 10, 20.0, 1, 0, 0], [0, 20, 60.0, 0, 15, 0]]


def test_index_and_missing_days(tmp_path):
    day = {"date": "2026-09-17", "market": "MI-A", "version": mi.FORMAT, "units": [],
           "markets": {"MI-A1": [{"time": "00:00"}], "MI-A2": [{"time": "00:00"}], "MI-A3": [{"time": "12:00"}]}}
    mi.write_day(str(tmp_path), day)
    mi.write_day(str(tmp_path), {**day, "date": "2026-09-18", "markets": {"MI-A1": [{"time": "00:00"}]}})
    days = mi.write_index(str(tmp_path))
    assert days["2026-09-17"] == {"MI-A1": ["00:00"], "MI-A2": ["00:00"], "MI-A3": ["12:00"]}
    index = json.loads((tmp_path / "index.json").read_text())
    assert index["files"]["2026-09-18"] == "2026-09-18.json.gz"
    # 18 Sept lacks two auctions: read again; 17 Sept is complete.
    assert mi.missing_days(mi.date(2026, 9, 17), mi.date(2026, 9, 19), str(tmp_path)) == [
        mi.date(2026, 9, 18), mi.date(2026, 9, 19)]


def test_an_auction_still_missing_two_weeks_on_was_not_held(tmp_path):
    full = {"MI-A1": [{"time": "00:00"}], "MI-A2": [{"time": "00:00"}], "MI-A3": [{"time": "12:00"}]}
    no_a1 = {"MI-A2": [{"time": "00:00"}], "MI-A3": [{"time": "12:00"}]}
    for key, markets, read, version in (("2026-08-04", no_a1, "2026-08-19", mi.FORMAT),   # read 15 days on: settled
                                        ("2026-08-05", no_a1, "2026-08-12", mi.FORMAT),   # 7 days on: may come yet
                                        ("2026-08-06", full, "2026-08-14", 3),            # an older format
                                        ("2026-08-07", no_a1, None, mi.FORMAT)):          # no read date: read again
        day = {"date": key, "market": "MI-A", "version": version, "units": [], "markets": markets}
        mi.write_day(str(tmp_path), {**day, "read": read} if read else day)
    mi.write_index(str(tmp_path))
    assert mi.missing_days(mi.date(2026, 8, 4), mi.date(2026, 8, 7), str(tmp_path)) == [
        mi.date(2026, 8, 5), mi.date(2026, 8, 6), mi.date(2026, 8, 7)]


def test_the_job_keeps_the_purchase_bids(tmp_path, monkeypatch):
    units = {**UNITS, "UP_BESS_1": ("battery", "BESS SPA", "SUD", "production")}
    rows = {"MI-A2": [row("UP_BESS_1", 49, 20, awarded=15, purpose="BID", price="60", awarded_price="55")]}
    monkeypatch.setenv("GME_API_LOGIN", "user")
    monkeypatch.setenv("GME_API_PASSWORD", "test")
    monkeypatch.setattr(mi, "get_token", lambda login, password: "token")
    monkeypatch.setattr(mi, "unit_sources", lambda: units)
    monkeypatch.setattr(mi, "REQUEST_PAUSE_SECONDS", 0)

    def request_market(token, day, market):
        if market not in rows:
            raise RuntimeError("no offers")
        return market, f"{market}.csv", market

    monkeypatch.setattr(mi, "request_market", request_market)
    monkeypatch.setattr(mi, "rows_of", lambda name, content: iter(rows[content]))
    assert mi.read_days([mi.date(2026, 9, 17)], str(tmp_path), 1) == [mi.date(2026, 9, 17)]
    data = mi.read_day(str(tmp_path / "2026-09-17.json.gz"))
    assert data["version"] == mi.FORMAT and data["read"] == mi.date.today().isoformat()
    assert data["markets"]["MI-A2"][0]["bids"] == [0, 20, 60.0, 0, 15, 0]


def test_an_mi_day_is_publicised_with_one_name_per_unit():
    day = {"date": "2026-09-17", "market": "MI-A",
           "units": [["UP_GAS_A_1", "gas", "OP A", "NORD", "production"],
                     ["UP_BESS_1", "battery", "OP B", "SUD", "production"]],
           "markets": {
               "MI-A1": [{"time": "12:00", "prices": {"NORD": 101.5}, "demand": [200.0, 10.0, 0.0],
                          "supply": [0, 50.0, 90.0, 0, 50.0, 101.5, 0, 1, 20.0, 80.0, 0, 20.0, 101.5, 0],
                          "others": []}],
               "MI-A2": [{"time": "12:00", "prices": {"NORD": 99.0}, "demand": [],
                          "supply": [1, 5.0, 70.0, 0, 5.0, 99.0, 0], "others": [0, 3, 12.0]}],
           }}
    public = pg.publicise_day(day, {}, "secret")
    names = [unit[0] for unit in public["units"]]
    assert sorted(names) == ["Battery 1", "Gas plant 1"] and "quarters" not in public
    assert all(unit[2] is None for unit in public["units"])                   # no operators
    a1, a2 = public["markets"]["MI-A1"][0], public["markets"]["MI-A2"][0]
    battery = names.index("Battery 1")
    # The battery has one index in both auctions.
    assert battery in a1["supply"][0::7] and a2["supply"][0] == battery
    assert a2["others"] == [names.index("Gas plant 1"), 3, 12.0]
    assert a1["prices"] == {"NORD": 101.5}                                    # numbers as they are for now
