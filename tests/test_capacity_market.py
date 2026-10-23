"""Checks for Terna's capacity market results (etl/build_capacity_market.py).  Run: python -m pytest tests"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import pytest  # noqa: E402

import build_capacity_market as cm  # noqa: E402


def test_terna_numbers():
    assert cm.number("1.767") == 1767
    assert cm.number("64,5") == 64.5
    assert cm.number(" ") is None
    assert cm.is_number("3.445") and cm.is_number("0,5") and not cm.is_number("Totale")


def test_the_same_participant_across_tables():
    assert cm.participant_key("UNIGRA'") == cm.participant_key("UNIGRA' S.R.L.")
    assert cm.participant_key("METAENERGIAPRODUZIONE") == cm.participant_key("METAENERGIA PRODUZIONE")
    assert cm.participant_key("ERG POWER GENERATION S.P.A") == cm.participant_key("ERG POWER GENERATION S.P.A.")
    assert cm.participant_key("EP C. T. MONTANASO") == cm.participant_key("EP CENTRALE TAVAZZANO MONTANASO")


def test_groups():
    assert cm.group_of("ENEL PRODUZIONE SPA") == "Enel"
    assert cm.group_of("ENI PLENITUDE SPA SOCIETA' BENEFIT") == "Eni"
    assert cm.group_of("EPP2 SRL") == cm.group_of("EP COMMODITIES, A.S.") == "EPH (EP Produzione)"
    assert cm.group_of("ERGOSUD S.P.A.") == "Ergosud" and cm.group_of("ERG POWER SRL") == "ERG"
    assert cm.group_of("MYT APULIA STORAGE 1 S.R.L.") == "Metlen (MYT)"
    assert cm.group_of("LA RICCIA SRL") == "La Riccia"
    assert cm.group_of("B2G SICILY SRL") == "B2G Sicily"


def test_areas():
    assert [cm.area_code(name) for name in ("CENTRO SUD", "CSUD", "SARD_NORD", "SARDEGNA SUD")] == ["CSUD", "CSUD", "SARD_N", "SARD_S"]
    assert cm.zone_of("SARD_N") == "SARD" and cm.zone_of("CALA") == "CALA"
    with pytest.raises(cm.CheckError):
        cm.area_code("CORSICA")


def units(*mws):
    return [{"mw": mw} for mw in mws]


def test_authorised_or_not_from_the_participants_split():
    found = units(32, 67, 270, 1, 17, 6)
    cm.infer_authorised(found, 361, 32)
    assert [unit["authorised"] for unit in found] == [False, True, True, True, True, True]
    # Rounded totals: the exact split wins over one a MW off.
    found = units(49, 33, 42, 40, 64.5, 64.5, 81)
    cm.infer_authorised(found, 293, 81)
    assert [unit["authorised"] for unit in found] == [True] * 6 + [False]
    # Two splits fit equally: neither unit is known.
    found = units(14, 14, 10)
    cm.infer_authorised(found, 24, 14)
    assert [unit["authorised"] for unit in found] == [None, None, True]
    found = units(5, 7)
    cm.infer_authorised(found, 0, 12)
    assert [unit["authorised"] for unit in found] == [False, False]


def test_unit_lists_and_their_totals():
    state = {"running": 0.0}
    rows = [["ASTA MADRE 2022", "", "", "", "", ""],
            ["PARTECIPANTE", "AREA", "DENOMINAZIONE", "TIPOLOGIA", "TIPO INIZIATIVA", "CDP [MW]"],
            ["A2A SPA", "NORD", "CASSANO", "TERMICO COMBINATO", "Ripotenziamento", "64,0"],
            ["ACEA ENERGIA SPA", "SUD", "ROSSI 1", "SOLARE", "Impianto Nuovo", "3"],
            ["Totale", "", "", "", "", "67"]]
    found = cm.parse_unit_rows(rows, {}, state)
    assert [(unit["area"], unit["technology"], unit["initiative"], unit["mw"]) for unit in found] == \
        [("NORD", "ccgt", "repowering", 64.0), ("SUD", "solar", "new", 3.0)]
    assert state["running"] == 0
    storage = [["PARTECIPANTE", "AREA", "DENOMINAZIONE", "CDP ASSEGNATA E NOMINATA [MW]", "CAPACITÀ ENERGETICA EQUIVALENTE [h]"],
               ["ENEL PRODUZIONE SPA", "SARD_SUD", "BESS_SULCIS", "82", "4,04"],
               ["Totale", "", "", "90", ""]]
    with pytest.raises(cm.CheckError):
        cm.parse_unit_rows(storage, {"technology": "ACCUMULO ELETTROCHIMICO"}, {"running": 0.0})


def test_transcribed_tables_add_up():
    with open(cm.TRANSCRIBED_PATH, encoding="utf-8") as handle:
        transcribed = json.load(handle)
    for year in ("2024", "2025", "2026", "2027"):
        national, foreign = cm.read_transcribed(int(year), transcribed)
        auction = next(item for item in cm.AUCTIONS if item["year"] == int(year))
        assert sum(row[2] for row in national) == auction["mw"]["existing"]
        assert sum(row[3] + row[4] for row in national) == auction["mw"]["new"] + auction["mw"]["new_not_authorised"]
        for border, mw in auction["mw"]["foreign"].items():
            assert sum(row[2] for row in foreign if row[0] == border) == mw
    broken = json.loads(json.dumps(transcribed))
    broken["2026"]["national"]["NORD"][0][1] += 1
    with pytest.raises(cm.CheckError):
        cm.read_transcribed(2026, broken)


def test_the_published_file():
    with open(cm.OUTPUT_PATH, encoding="utf-8") as handle:
        data = json.load(handle)
    columns = data["units_columns"]
    found = [dict(zip(columns, row)) for row in data["units"]]
    assert [auction["year"] for auction in data["auctions"]] == [2022, 2023, 2024, 2025, 2026, 2027]
    for auction in data["auctions"]:
        mw = auction["mw"]
        listed = sum(unit["mw"] for unit in found if unit["year"] == auction["year"])
        assert abs(listed - mw["new"] - mw["new_not_authorised"]) <= 2
        assert abs(sum(row[2] for row in auction["national"]) - mw["existing"]) < 0.5
        assert abs(sum(row[3] for row in auction["national"]) - mw["new"] - mw["new_not_authorised"]) < 0.5
        payments = sum(row[4] for row in auction["national"]) + sum(row[3] for row in auction["foreign"])
        assert abs(payments / 1e6 - auction["cost"]["total"]) <= 0.005 * auction["cost"]["total"]
    assert all(row[1] in data["groups"] for auction in data["auctions"] for row in auction["national"] + auction["foreign"])
    assert all(unit["participant"] in data["groups"] for unit in found)
