"""Checks for etl/build_grid_ties.py and etl/grid_ties.json.  Run: python -m pytest tests"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import build_grid_ties as ties  # noqa: E402
import fetch_crossborder  # noqa: E402

# The map's borders (ZMAP_BORDERS in app/index.html).
BORDERS = {"NORD-CNOR", "CNOR-CSUD", "CSUD-SUD", "SUD-CALA", "CALA-SICI", "CSUD-SARD", "CNOR-CORS", "SARD-CORS", "SARD-COAC",
           "NORD-FR", "NORD-CH", "NORD-AT", "NORD-SI", "CSUD-ME", "SUD-GR", "SICI-MT"}


def line(points, kv=380, dc=False):
    return {"points": points, "voltages": [kv], "dc": dc, "km": sum(ties.km(a, b) for a, b in zip(points, points[1:]))}


def test_a_chain_of_lines_meeting_end_to_end_is_the_route():
    lines = [line([(10.0, 45.0), (10.2, 45.0)]),
             # Mapped the other way round: still joins.
             line([(10.4, 45.0), (10.2, 45.001)]),
             # Another voltage on the same route is not this line.
             line([(10.0, 45.0), (10.4, 45.0)], kv=132)]
    points, how = ties.route(lines, (10.0, 45.0), (10.4, 45.0), 380, False)
    assert how == "osm"
    assert points[0] == (10.0, 45.0) and points[-1] == (10.4, 45.0) and len(points) == 4


def test_a_route_stopping_short_ends_straight_and_none_is_schematic():
    lines = [line([(10.0, 45.0), (10.8, 45.0)])]
    points, how = ties.route(lines, (10.0, 45.0), (11.0, 45.0), 380, False)
    assert how == "partly" and points[-1] == (11.0, 45.0)
    # Direct current only with direct current.
    assert ties.route(lines, (10.0, 45.0), (11.0, 45.0), 380, True) == (None, "schematic")


def test_400_kv_lines_count_as_the_380_kv_level():
    assert ties.fits(line([(0, 0), (1, 1)], kv=400), 380, False)
    assert not ties.fits(line([(0, 0), (1, 1)], kv=220), 380, False)


def test_coordinates_round_trip():
    points = [(10.12346, 45.6789), (10.2, 45.7), (10.2, 45.7)]
    assert ties.decode(ties.encode(points), ties.SCALE) == [(10.1235, 45.6789), (10.2, 45.7)]


def test_the_curated_list_names_known_stations_and_borders():
    with open(ties.CURATED_PATH, encoding="utf-8") as handle:
        curated = json.load(handle)
    for cable in curated["cables"]:
        assert cable["a"] in curated["stations"] and cable["b"] in curated["stations"], cable
        assert set(cable["borders"]) <= BORDERS, cable
    # Every border on the map has its lines.
    assert {border for cable in curated["cables"] for border in cable["borders"]} == BORDERS
    # Terna's A.24: five lines to France, twelve to Switzerland.
    count = lambda border: sum(border in cable["borders"] for cable in curated["cables"])
    assert (count("NORD-FR"), count("NORD-CH"), count("NORD-CNOR")) == (5, 12, 9)


def test_crossborder_reads_the_day_ahead_transfer_capacity():
    assert fetch_crossborder.DATASETS["capacity_day_ahead"] == {"documentType": "A61", "contract_MarketAgreement.Type": "A01"}
