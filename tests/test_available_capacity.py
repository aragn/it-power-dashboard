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


class FakeClient:
    def __init__(self, first_key="TERNA_KEY_3"):
        self.first_key = self.key_name = first_key


def history_fixture(monkeypatch, published_from):
    """Effective capacity from 2025-12-02 on; Terna has days from published_from."""
    from datetime import date, timedelta
    asked = []

    def fetch(fetcher, kind, first, last):
        asked.append((kind, first, last))
        fetcher.calls += 1
        days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
        return [{"group": f"{kind}|NORD|gas", "date": day.isoformat(), "time": "00:00", "minutes": 60,
                 "value": 1.0} for day in days if day >= published_from]

    monkeypatch.setattr(fa, "fetch", fetch)
    monkeypatch.setattr(fa, "HISTORY_START", date(2025, 1, 1))
    series = {resolution: {} for resolution in fa.RESOLUTIONS}
    series = fa.merge(series, [{"group": "effective|NORD|gas", "date": "2025-12-02", "time": "00:00",
                                "minutes": 60, "value": 1.0},
                               {"group": "expected|NORD|gas", "date": "2026-09-01", "time": "00:00",
                                "minutes": 60, "value": 1.0}])
    return asked, series


def test_history_goes_back_from_the_first_day_until_terna_has_nothing(monkeypatch):
    from datetime import date
    asked, series = history_fixture(monkeypatch, published_from=date(2025, 11, 20))
    history_start = {}
    fetcher = fa.Fetcher(FakeClient())
    series = fa.catch_up(fetcher, series, history_start, budget=100)
    assert asked[:3] == [("effective", date(2025, 11, 25), date(2025, 12, 1)),
                         ("effective", date(2025, 11, 18), date(2025, 11, 24)),
                         ("effective", date(2025, 11, 13), date(2025, 11, 19))]
    assert history_start == {"effective": "2025-11-20", "expected": "2025-11-20"}
    assert fa.first_day(series, "effective") == date(2025, 11, 20)
    # Not asked again.
    asked.clear()
    fa.catch_up(fa.Fetcher(FakeClient()), series, history_start, budget=100)
    assert asked == []


def test_history_stops_at_the_budget_or_on_the_fallback_key(monkeypatch):
    from datetime import date
    asked, series = history_fixture(monkeypatch, published_from=date(2025, 1, 1))
    fa.catch_up(fa.Fetcher(FakeClient()), series, {}, budget=3)
    assert len(asked) == 3
    asked.clear()
    client = FakeClient()
    client.key_name = "TERNA_KEY"
    fa.catch_up(fa.Fetcher(client), series, {}, budget=100)
    assert asked == []
