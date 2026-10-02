"""Checks for fetch_connections.  Run: python -m pytest tests"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_connections as fc  # noqa: E402


def test_numbers_names_and_dates():
    assert fc.number("1.004,2404") == 1004.2404
    assert fc.number("-115,5892") == -115.5892
    assert fc.number(None) is None and fc.number("") is None
    assert fc.title("BARLETTA-ANDRIA-TRANI") == "Barletta-Andria-Trani"
    assert fc.title("L'AQUILA") == "L'Aquila"
    assert fc.title("VALLE D'AOSTA") == "Valle d'Aosta"
    assert fc.title("PESARO E URBINO") == "Pesaro e Urbino"
    assert fc.terna_date("/Date(1788127200000+0200)/") == "2026-08-31"


def test_quarters_and_the_latest_period_to_try():
    assert list(fc.quarters((2025, 9), (2026, 3))) == ["09/2025", "12/2025", "03/2026"]
    # e-distribuzione's map shows June's quarter from September to November.
    assert fc.latest_possible_period(date(2026, 10, 2)) == (2026, 9)
    assert fc.latest_possible_period(date(2026, 1, 15)) == (2025, 12)
    assert fc.latest_possible_period(date(2026, 4, 1)) == (2026, 3)
    assert fc.period_key("06/2026") == "2026-06"


def test_terna_snapshot_sums_by_kind_source_stage_and_region():
    regions = [["Puglia", "renewables", "Solare", "STMG accettate", 100.0, 2],
               ["Sicilia", "renewables", "Solare", "STMG accettate", 50.0, 1],
               ["Puglia", "storage", "Accumulo stand-alone", "STMG accettate", 30.0, 1]]
    snapshot = fc.terna_snapshot(regions)
    assert ["renewables", "Solare", "STMG accettate", 150.0, 3] in snapshot["national"]
    assert ["Puglia", "renewables", "STMG accettate", 100.0, 2] in snapshot["regions"]
    assert len(snapshot["regions"]) == 3


class FakeService:
    def municipalities(self, period, province):
        return [{"CodiceComune": "075002", "Descrizione": "Alessano", "LivelloCriticitaComune": 4,
                 "PotPreventivi": "8,89699", "PotVirtComune": "7,3032", "PotMinimoRegionale": "-2,0221"}]

    def reverse_flows(self, period, province):
        return [{"Codice": "DW1", "Descrizione": "CASARANO CP", "Montante": "272", "InvFlusso1": "S",
                 "InvFlusso5": "N", "IdTrasf": "RO"}]

    def served(self, province):
        if province == "001":
            raise RuntimeError("HTTP 500")
        return [{"Descrizione": "ALESSANO", "ElencoTrasformatori": [
                    {"CodCabinaPrimaria": "DW1", "CodTrasformatore": "RO", "IdSezioneATMT": "272",
                     "LivSaturTraformatore": "ROSSO", "NomeCabinaPrimaria": "CASARANO CP"},
                    {"CodCabinaPrimaria": "DW2", "CodTrasformatore": "VE", "IdSezioneATMT": "273",
                     "LivSaturTraformatore": "GIALLO", "NomeCabinaPrimaria": "TRICASE CP"}]},
                {"Descrizione": "ALEZIO", "ElencoTrasformatori": [
                    {"CodCabinaPrimaria": "DW1", "CodTrasformatore": "RO", "IdSezioneATMT": "272",
                     "LivSaturTraformatore": "ROSSO", "NomeCabinaPrimaria": "CASARANO CP"}]}]


def test_sections_join_reverse_flow_and_saturation_and_skip_failing_provinces():
    provinces = [["Puglia", "075", "Lecce", 4, 1481.6, 1395, -369.1], ["Piemonte", "001", "Torino", 3, 500.0, 900, -10.0]]
    municipalities, sections, incomplete = fc.fetch_detail(FakeService(), "06/2026", provinces)
    assert municipalities[0][:5] == ["075", "075002", "Alessano", 4, 8.89699]
    casarano = next(row for row in sections if row[1] == "DW1")
    assert casarano == ["075", "DW1", "Casarano CP", "RO", "272", 4, True, False, ["Alessano", "Alezio"]]
    tricase = next(row for row in sections if row[1] == "DW2")
    assert tricase[5] == 2 and tricase[6] is False
    assert incomplete == ["001"]


class FakeRegions:
    def provinces(self, period, region):
        return {"03": [{"CodiceProvincia": "010", "Descrizione": "GENOVA", "LivelloCriticita": 3, "PotPreventivi": "120,5",
                        "PotNomCP": "1.100", "PotMinimoRegionale": "-5,1"}],
                "04": [{"CodiceProvincia": "914", "Descrizione": "AEM TIRANO", "LivelloCriticita": 1, "PotPreventivi": None,
                        "PotNomCP": None, "PotMinimoRegionale": None}]}.get(region, [])


def test_provinces_take_the_services_own_region_codes_and_skip_areas_without_values():
    rows = fc.province_rows(FakeRegions(), "06/2026")
    assert rows == [["Liguria", "010", "Genova", 3, 120.5, 1100.0, -5.1]]
    assert fc.has_values(rows) and not fc.has_values([])
