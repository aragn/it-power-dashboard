"""Checks for fetch_available_capacity.  Run: python -m pytest tests"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_available_capacity as fa  # noqa: E402


def test_sources_from_plant_type_and_fuel():
    assert fa.source_of("TERMICO CICLO COMBINATO", "GAS NATURALE") == "gas"
    assert fa.source_of("TERMICO TURBOGAS", "ALTRO") == "gas"
    assert fa.source_of("TERMICO TRADIZIONALE", "CARBONE") == "coal"
    assert fa.source_of("TERMICO TRADIZIONALE", "DERIVATI DEL PETROLIO") == "other"
    assert fa.source_of("IDROELETTRICO", "ALTRO") == "hydro"
    assert fa.source_of("ALTRO", "ALTRO") == "other"


def test_rows_are_summed_per_macro_area_and_source_in_market_time():
    rows = [
        {"market_date": "2026-09-30 23:00:00", "offset": "+02:00", "macroarea": "Sud_Isole",
         "plant_type": "TERMICO CICLO COMBINATO", "prevailing_fuel": "GAS NATURALE", "available_capacity_MW": "1000.5"},
        {"market_date": "2026-09-30 23:00:00", "offset": "+02:00", "macroarea": "Sud_Isole",
         "plant_type": "TERMICO TURBOGAS", "prevailing_fuel": "ALTRO", "available_capacity_MW": "200"},
        {"market_date": "2026-09-30 23:00:00", "offset": "+02:00", "macroarea": "Nord",
         "plant_type": "IDROELETTRICO", "prevailing_fuel": "ALTRO", "available_capacity_MW": "1494,9"},
    ]
    out = {(r["group"], r["date"], r["time"]): r["value"] for r in fa.records("effective", rows)}
    assert out == {("effective|SUD|gas", "2026-09-30", "23:00"): 1200.5,
                   ("effective|NORD|hydro", "2026-09-30", "23:00"): 1494.9}


def test_daily_values_are_averages():
    rows = [{"market_date": f"2026-09-30 {h:02d}:00:00", "offset": "+02:00", "macroarea": "Nord",
             "plant_type": "IDROELETTRICO", "prevailing_fuel": "ALTRO", "available_capacity_MW": str(100 + h)}
            for h in range(24)]
    series = fa.merge({resolution: {} for resolution in fa.RESOLUTIONS}, fa.records("expected", rows))
    assert series["daily"]["expected|NORD|hydro"] == [{"date": "2026-09-30", "value": 111.5}]
    assert len(series["hourly"]["expected|NORD|hydro"]) == 24
