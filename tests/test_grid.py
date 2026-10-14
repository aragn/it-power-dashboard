"""Checks for fetch_grid and match_grid_units.  Run: python -m pytest tests"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_grid as fg  # noqa: E402
import match_grid_units as mg  # noqa: E402


def test_voltages_are_volts_unless_marked_kv():
    assert fg.voltages_kv("380000;132000") == [380, 132]
    assert fg.voltages_kv("150000") == [150]
    assert fg.voltages_kv("220 kV") == [220]
    # A distribution cabin's 400 V is not 400 kV; nor a DC electrode line's 0.
    assert fg.voltages_kv("400") == []
    assert fg.voltages_kv("200000;0") == [200]
    assert fg.voltages_kv(None) == []


def test_plant_output_in_any_unit():
    assert fg.megawatts("1200 MW") == 1200
    assert fg.megawatts("1,2 GW") == 1200
    assert fg.megawatts("500 kW") == 0.5
    assert fg.megawatts("12.5 MWp") == 12.5
    assert fg.megawatts("yes") is None


def test_coordinates_round_trip_in_steps():
    points = [(12.5, 41.9), (12.5012, 41.9007), (12.6, 42.0)]
    coords = fg.encode(points)
    x = y = 0
    decoded = []
    for index in range(0, len(coords), 2):
        x += coords[index]
        y += coords[index + 1]
        decoded.append((x / fg.SCALE, y / fg.SCALE))
    assert decoded == [(12.5, 41.9), (12.5012, 41.9007), (12.6, 42.0)]
    # Points closer than a step are one point.
    assert len(fg.encode([(12.5, 41.9), (12.50001, 41.90001)])) == 2


def test_simplify_keeps_the_ends_and_the_corners():
    line = [(0, 0), (1, 0.00001), (2, 0), (2, 1)]
    assert fg.simplify(line, 0.001) == [(0, 0), (2, 0), (2, 1)]


def test_ways_meeting_end_to_end_are_joined_but_branches_stay_apart():
    def way(way_id, first, last, points, name="A - B"):
        return {"id": way_id, "tags": {"name": name}, "status": "service", "kind": "line", "kv": [380],
                "points": points, "first": first, "last": last}
    ways = [way(1, 10, 11, [(0, 0), (1, 0)]), way(2, 12, 11, [(2, 0), (1, 0)]), way(3, 12, 13, [(2, 0), (3, 0)])]
    joined = fg.join_ways(ways)
    assert len(joined) == 1
    assert joined[0]["points"] == [(0, 0), (1, 0), (2, 0), (3, 0)]
    assert sorted(joined[0]["ids"]) == [1, 2, 3]
    # Three ways at one node: no join there.
    branches = fg.join_ways([way(1, 10, 11, [(0, 0), (1, 0)]), way(2, 11, 12, [(1, 0), (2, 0)]),
                             way(3, 11, 13, [(1, 0), (1, 1)])])
    assert len(branches) == 3


def test_interconnector_ends_abroad_or_off_a_foreign_coast():
    countries = fg.Countries(fg.NEIGHBOURS_PATH)
    assert countries.at(6.95, 45.6) == "FR"          # Tarentaise
    assert countries.at(9.0, 46.3) == "CH"           # Ticino
    assert countries.at(12.5, 41.9) is None          # Rome
    assert countries.near(20.01, 39.68, fg.SEA_LANDING_KM) == "GR"   # GRITA's landing
    assert countries.near(13.0, 40.5, fg.SEA_LANDING_KM) is None     # the open Tyrrhenian


def test_gme_stems():
    assert mg.stem_of("UP_TORVISCOSA_1") == "TORVISCOSA"
    assert mg.stem_of("UP_PREM-GROSIO_1") == "PREMGROSIO"
    assert mg.stem_of("UP_TERMOLI1_1") == "TERMOLI"
    assert mg.stem_of("UP_LA_CASELL_3") == "LACASELL"


def test_names_and_abbreviations():
    assert mg.name_score("SERMIDE", ["Centrale termoelettrica di Sermide"]) == 1.0
    assert mg.name_score("FIUMESANT", ["Centrale termoelettrica di Fiume Santo"]) == 1.0
    assert mg.name_score("TRRVLDLIGA", ["Centrale termoelettrica di Torrevaldaliga Sud"]) >= mg.NAME_MIN
    assert mg.name_score("NPWRBRNDSI", ["Brindisi Enipower Power Plant"]) >= mg.NAME_MIN
    # A plain name matches only as a word: Levane is not Napoli Levante.
    assert mg.name_score("LEVANE", ["Centrale Napoli Levante"]) < mg.NAME_MIN
    # Common words of place names and generic names match nothing.
    assert mg.name_score("MNTDLLDFSA", ["Monte Moio"]) < mg.NAME_MIN
    assert mg.name_score("CNTRLTRMLT", ["Centrale TermoElettrica Edison"]) == 0
    assert mg.research_score(["Monte Grighine wind farm"], ["Parco eolico Monte Grighine"]) >= mg.NAME_MIN
    assert mg.research_score(["Monte Grighine wind farm"], ["Monte Moio"]) == 0


def test_match_needs_a_name_or_place_and_capacity(tmp_path):
    def osm(osm_id, name, technology, mw, lon, lat):
        return {"id": osm_id, "name": name, "names": [name], "technology": technology, "mw": mw, "estimated": False,
                "lon": lon, "lat": lat, "zones": {"NORD"}}
    plants = [osm("w1", "Centrale di Sermide", "gas", 1234, 11.02, 45.02),
              osm("w2", "Centrale X", "gas", 800, 9.70, 45.30),
              osm("w3", "Centrale Y", "hydro", 800, 9.70, 45.30)]
    sermide = {"stem": "SERMIDE", "zone": "NORD", "codes": ["UP_SERMIDE_3"], "source": "gas", "mw": 1212,
               "lon": 11.0, "lat": 45.0, "names": []}
    assert mg.match(sermide, plants, set())[2]["id"] == "w1"
    # No name, but the same technology and capacity next door.
    unnamed = {"stem": "QQQQQ", "zone": "NORD", "codes": ["UP_QQQQQ_1"], "source": "gas", "mw": 790,
               "lon": 9.71, "lat": 45.31, "names": []}
    best = mg.match(unnamed, plants, set())
    assert best[2]["id"] == "w2" and best[1] == "place and capacity"
    # ...unless a name has taken that plant already.
    assert mg.match(unnamed, plants, {"w2"}) is None
    # Another zone, far away: no match.
    assert mg.match({**sermide, "zone": "SICI"}, [{**plants[0], "zones": {"NORD"}}], set()) is None


def test_plans_file_is_consistent():
    with open(fg.PLANS_PATH, encoding="utf-8") as handle:
        plans = json.load(handle)
    ids = [project["id"] for project in plans["projects"]]
    assert len(ids) == len(set(ids))
    for project in plans["projects"]:
        assert project["phase"] in (None, *plans["phases"])
        assert project["kind"] in ("hvdc", "ac", "station")
        assert all(source in plans["sources"] for source in project["sources"])
        assert project["paths"] or project["stations"]
        for path in project["paths"]:
            assert len(path) >= 2
            assert all(5 <= lon <= 21 and 36 <= lat <= 47.5 for lon, lat in path)
