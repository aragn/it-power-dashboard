"""Checks for publicise_gme (the public versions of GME's files).  Run: python -m pytest tests"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import compact  # noqa: E402
import publicise_gme as pg  # noqa: E402


import pytest  # noqa: E402


@pytest.fixture
def rounded(monkeypatch):
    """The rounding as it would be if GME asks for it."""
    for name, size in (("PRICE_STEP", 5), ("VOLUME_STEP", 1), ("OFFER_STEP", 0.1), ("ESTIMATE_STEP", 100)):
        monkeypatch.setattr(pg, name, size)


def series(values, step=15):
    return {"step": step, "field": "price", "start": "2026-09-01", "days": [values, None]}


def test_numbers_stay_as_they_are_for_now():
    payload = {"zones": {"NORD": {"quarter_hourly": series([101.23, None, 188.64])}}}
    assert pg.round_series(payload, lambda path: "price") == payload
    public = pg.publicise_day(DAY, {}, "secret")
    assert public["quarters"][0]["prices"]["NORD"] == 188.64
    assert not any(unit[0].startswith("UP_") for unit in public["units"])


def test_prices_are_rounded_and_gaps_kept(rounded):
    out = pg.round_series({"zones": {"NORD": {"quarter_hourly": series([101.23, None, 188.64, -0.01])}}},
                          lambda path: "price")
    assert out["zones"]["NORD"]["quarter_hourly"]["days"] == [[100, None, 190, 0], None]


def test_coupling_flows_and_prices_and_balancing_series_by_kind(rounded):
    kind = pg.series_kind("coupling.json")
    assert kind("/flows/quarter_hourly/BSP/export_flow") == "volume"
    assert kind("/prices/quarter_hourly/NORD") == "price"
    assert pg.balancing_kind("/series/hourly/NORD|msd_price_up") == "price"
    assert pg.balancing_kind("/series/hourly/IT|sum_mb_volume_up_rs") == "volume"
    assert pg.balancing_kind("/series/daily/IT|cost_msd_up") == "estimate"
    assert pg.step(12345.0, pg.ESTIMATE_STEP) == 12300 and pg.step(0.04, pg.OFFER_STEP) == 0


DAY = {
    "date": "2026-09-17", "market": "MGP", "source": "x",
    "units": [["UP_GAS_A_1", "gas", "OP A", "NORD", "production"], ["UP_GAS_B_1", "gas", "OP B", "SUD", "production"],
              ["UVZi_00001_1_NOC", "solar", "GSE", "NORD", "aggregate_injection"]],
    "quarters": [{
        "time": "12:00", "prices": {"NORD": 188.64, "SICI": 162.0},
        "supply": [0, 300.04, 150.31, 0, 300.04, 188.64, 0,
                   1, 200.0, 401.0, 1, 0, None, 0,
                   2, 1000.0, 0.0, 0, 1000.0, 185.71, 1],
        "demand": [4000.0, 1200.0, 0.0, 3998.0, 100.0, 0.0, -500.0, 0.0, 40.0],
        "others": [0, 3, 50.0, 2, 4, 10.0],
    }],
}


def test_a_merit_order_day_loses_codes_operators_and_exact_numbers(rounded):
    public = pg.publicise_day(DAY, {"UP_GAS_B_1": ["battery", "x", "y"]}, "secret")
    names = [unit[0] for unit in public["units"]]
    assert not any(name.startswith(("UP_", "UVZ")) for name in names)
    assert all(unit[2] is None and unit[4] is None for unit in public["units"])     # no operator, no code family
    assert {unit[0]: unit[3] for unit in public["units"]}["Solar plant 1"] == "NORD"  # the zone stays
    # The research's source wins over the day file's: B is a battery now.
    assert sorted(names) == ["Battery 1", "Gas plant 1", "Solar plant 1"]
    quarter = public["quarters"][0]
    assert quarter["prices"] == {"NORD": 190, "SICI": 160}
    records = [quarter["supply"][i:i + 7] for i in range(0, len(quarter["supply"]), 7)]
    by_name = {public["units"][record[0]][0]: record for record in records}
    assert by_name["Gas plant 1"][1:6] == [300, 150, 0, 300, 190]
    assert by_name["Battery 1"][1:6] == [200, 400, 1, 0, None]
    assert by_name["Solar plant 1"][6] == 1                       # flags kept (hourly order)
    assert quarter["demand"] == [4000, 1300, 0, -500, 0, 40]      # 3998 rounds into 4000's step
    others = [quarter["others"][i:i + 3] for i in range(0, len(quarter["others"]), 3)]
    assert sorted((public["units"][unit][0], status, mw) for unit, status, mw in others) == [
        ("Gas plant 1", 3, 50), ("Solar plant 1", 4, 10)]


def test_the_names_follow_the_salt_not_the_codes():
    # Unit i offers i + 1 MW: which name each plant gets depends on the salt.
    many = {"date": "2026-09-17", "units": [[f"UP_GAS_{n}_1", "gas", None, None, None] for n in range(30)],
            "quarters": [{"time": "12:00", "prices": {}, "demand": [],
                          "supply": [n for i in range(30) for n in (i, i + 1, 1, 0, i + 1, 1, 0)]}]}

    def names(secret):
        public = pg.publicise_day(many, {}, secret)
        supply = public["quarters"][0]["supply"]
        return {supply[i + 1]: public["units"][supply[i]][0] for i in range(0, len(supply), 7)}

    assert names("one") == names("one")
    assert names("one") != names("two")
    assert sorted(names("one").values()) == sorted(f"Gas plant {n}" for n in range(1, 31))


def test_files_in_place(tmp_path, monkeypatch, rounded):
    merit = tmp_path / "mgp_merit"
    merit.mkdir()
    pg.write_json(DAY, str(merit / "2026-09-17.json.gz"))
    pg.write_json({"UP_GAS_A_1": ["gas", "OP A", "NORD"]}, str(merit / "units.json"))
    pg.write_json({"days": {"2026-09-17": ["12:00"]}}, str(merit / "index.json"))
    salt_file = tmp_path / "public_salt.txt"
    monkeypatch.setattr(pg, "SALT_PATH", str(salt_file))
    monkeypatch.setattr(pg, "NAMES_DIR", str(tmp_path / "public_names"))
    pg.new_salt(str(salt_file))
    assert pg.publicise(str(merit)) == "1 day(s)"
    assert not (merit / "units.json").exists() and (merit / "index.json").exists()
    day = pg.read_json(str(merit / "2026-09-17.json.gz"))
    assert day["source"] == pg.NOTE and not any(unit[0].startswith("UP_") for unit in day["units"])

    units = tmp_path / "gme_units.json"
    units.write_text("{}")
    assert pg.publicise(str(units)).startswith("removed") and not units.exists()

    prices = str(tmp_path / "zonal_prices.json")
    compact.dump({"zones": {"NORD": {"hourly": compact.encode_series(
        [{"date": "2026-09-17", "time": "00:00", "price": 188.64}], "hourly", "price")}}}, prices)
    pg.publicise(prices)
    assert compact.load(prices)["zones"]["NORD"]["hourly"] == [{"date": "2026-09-17", "time": "00:00", "price": 190.0}]


def test_a_unit_keeps_its_name_in_every_market_of_the_day(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "NAMES_DIR", str(tmp_path / "public_names"))
    store = pg.load_names("2026-09-17")
    mgp = {"date": "2026-09-17", "units": [["UP_GAS_A_1", "gas", "OP", "NORD", None], ["UP_GAS_B_1", "gas", "OP", "SUD", None]],
           "quarters": [{"time": "12:00", "prices": {}, "demand": [], "supply": [0, 10, 1, 0, 10, 1, 0, 1, 20, 1, 0, 20, 1, 0]}]}
    public_mgp = pg.publicise_day(mgp, {}, "salt", store)
    names = {unit[3]: unit[0] for unit in public_mgp["units"]}          # by zone
    pg.save_names("2026-09-17", store)
    # Later, MSD that day: B again and a new unit C.
    msd = {"date": "2026-09-17", "market": "MSD",
           "units": [["UP_GAS_C_1", "gas", "OP", "CSUD", None], ["UP_GAS_B_1", "gas", "OP", "SUD", None]],
           "quarters": [{"time": "12:00", "offers": [1, 0, 0, 5.0, 180.0, 0, 5.0, 0, 0, 1, 0, 8.0, 40.0, 1, 0, 0],
                         "events": [0, 0, 15000.0]}]}
    public_msd = pg.publicise_offers_day(msd, {}, "salt", pg.load_names("2026-09-17"))
    msd_names = {unit[3]: unit[0] for unit in public_msd["units"]}
    assert msd_names["SUD"] == names["SUD"]                             # the same name as in the MGP
    assert msd_names["CSUD"] == "Gas plant 3"                           # a new unit: the next number
    offers = public_msd["quarters"][0]["offers"]
    by_name = {public_msd["units"][offers[i]][0]: offers[i + 1:i + 8] for i in range(0, len(offers), 8)}
    assert by_name[names["SUD"]] == [0, 0, 5.0, 180.0, 0, 5.0, 0]
    assert not any(unit[0].startswith("UP_") for unit in public_msd["units"])


def test_xbid_fills_are_renamed_and_sorted():
    xbid = {"date": "2026-09-17", "market": "XBID", "units": [["UPV_X_1", None, "OP", "SVIZ", "import"]],
            "quarters": [{"time": "12:00", "fills": [0, 0, 2.0, 150.0, 30, 0, 1, 1.0, 151.0, 90]}], "hours": []}
    public = pg.publicise_offers_day(xbid, {}, "salt")
    assert public["units"][0][0] == "Cross-border unit 1"
    assert public["quarters"][0]["fills"] == [0, 1, 1.0, 151.0, 90, 0, 0, 2.0, 150.0, 30]   # the oldest first


def test_purchases_are_renamed_and_consumption_units_named_as_such():
    day = {"date": "2026-09-17", "units": [["UC_0000001_01", None, "BUYER", "NORD", "consumption"],
                                           ["UP_BESS_1", "battery", "OP", "SUD", "production"]],
           "quarters": [{"time": "12:00", "prices": {}, "demand": [], "supply": [],
                         "purchases": [1, 20.0, 100.0, 0, 0, 900.0, 101.0, 2]}]}
    public = pg.publicise_day(day, {}, "salt")
    names = [unit[0] for unit in public["units"]]
    assert sorted(names) == ["Battery 1", "Consumption unit 1"]
    flat = public["quarters"][0]["purchases"]
    rows = {public["units"][flat[i]][0]: flat[i + 1:i + 4] for i in range(0, len(flat), 4)}
    assert rows == {"Battery 1": [20.0, 100.0, 0], "Consumption unit 1": [900.0, 101.0, 2]}
