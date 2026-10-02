"""Checks for fetch_mase.  Run: python -m pytest tests"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_mase as fm  # noqa: E402


def test_numbers_in_italian_and_english_formats():
    assert fm.to_number("19,830") == 19.83
    assert fm.to_number("1.004,24") == 1004.24
    assert fm.to_number("79.61") == 79.61
    assert fm.to_number("1.200") == 1200
    assert fm.to_number("79.610") == 79.61  # 79,610 MW would be no plant


def test_capacity_is_the_total_not_a_turbine_an_old_plant_or_the_storage():
    cases = {
        "impianto eolico costituito da 10 aerogeneratori, ciascuno di potenza nominale pari a 7,2 MW, "
        "per una potenza complessiva di 72 MW": (72, None),
        "con dismissione degli attuali 7 aerogeneratori di potenza unitaria di 2,5 MW, per una potenza attuale "
        "pari a 17,5 MW, e installazione di 4 aerogeneratori di potenza unitaria 6,6 MW per una potenza "
        "complessiva di 26,4 MW": (26.4, None),
        "costituito da n. 8 aerogeneratori di potenza unitaria 6 MW": (48, None),
        "Impianto fotovoltaico di potenza nominale pari a 19,830 MWp, integrato da un sistema di accumulo di 20 MW": (19.83, 20),
        "impianto agrivoltaico, della potenza complessiva di 75 MW, con un impianto di accumulo elettrochimico "
        "(B.E.S.S.) della potenza di 10,5 MW": (75, 10.5),
        "parco eolico dalla potenza di 45 MWp, costituito da 9 aereogeneratori dalla potenza di 5 MW ciascuno": (45, None),
        "costituito da 56 aerogeneratori di potenza pari a 670 MW": (670, None),
        "centrale termica esistente con potenza termica pari a 258,5 MW": (None, None),
        "impianto fotovoltaico da 18.419,10 kWp": (18.419, None),
        "relativa al progetto Argenta 2 168.461,3 kWp e relative opere": (168.461, None),
        "Realizzazione di un nuovo Data Center": (None, None),
    }
    for text, expected in cases.items():
        assert fm.capacities(text) == expected, text


def test_terna_codes_and_changes():
    assert fm.terna_codes("Codice pratica MY TERNA n. 202100075. Codice pratica 202001101") == ["202001101", "202100075"]
    assert fm.terna_codes("Progetto di un impianto da 20 MW") == []
    assert fm.is_change("Variante locale dell'Elettrodotto aereo")
    assert fm.is_change("Progetto di modifica della centrale termoelettrica di Monfalcone (GO)")
    assert not fm.is_change("Progetto di un impianto agrivoltaico")


def test_technology_follows_the_type_checked_against_the_description():
    assert fm.technology("solar", "impianto fotovoltaico") == "solar"
    assert fm.technology("solar", "Progetto di un impianto eolico denominato IR8") == "wind_onshore"
    assert fm.technology("wind_onshore", "Impianto agrifotovoltaico a terra") == "agrivoltaic"
    assert fm.technology("power_plant", "Progettazione nuovo Data Center Liscate") == "data_centre"
    assert fm.technology("power_plant", "Centrale Termoelettrica di Cassano d'Adda: nuovo ciclo combinato") == "thermal"
    assert fm.technology("hydro", "Impianto di accumulo idroelettrico mediante pompaggio") == "pumped_hydro"
    assert fm.technology("power_plant", "Sistema di accumulo elettrochimico BESS da 200 MW") == "storage"


def procedure(kind, status, outcome=None, start="2024-01-01"):
    entry = {"type": kind, "code": "1", "start": start, "status": status}
    if outcome:
        entry["outcome"] = outcome
    return entry


def test_stage_from_the_latest_assessment():
    via = "Valutazione Impatto Ambientale (PNIEC-PNRR)"
    assert fm.project_stage([procedure(via, "Istruttoria tecnica CTPNRR-PNIEC")]) == "assessment"
    assert fm.project_stage([procedure(via, "Conclusa", "Positivo con prescrizioni/raccomandazioni")]) == "approved"
    assert fm.project_stage([procedure(via, "Conclusa", "Negativo")]) == "rejected"
    assert fm.project_stage([procedure(via, "Archiviata")]) == "withdrawn"
    assert fm.project_stage([procedure("Valutazione preliminare", "Non necessita di ulteriori valutazioni ambientali")]) == "screening"
    assert fm.project_stage([procedure("Verifica di Ottemperanza", "Conclusa", "Ottemperata")]) == "approved"
    # A screening that asks for a full VIA: under way until the VIA decides.
    screening = procedure("Verifica di Assoggettabilità a VIA", "Conclusa", "Da assoggettare a VIA")
    assert fm.project_stage([screening]) == "assessment"
    assert fm.project_stage([screening, procedure(via, "Conclusa", "Positivo", start="2025-01-01")]) == "approved"
    # Compliance checks after an approval do not change it.
    approved = procedure(via, "Conclusa", "Positivo", start="2023-01-01")
    assert fm.project_stage([approved, procedure("Verifica di Ottemperanza (PNIEC-PNRR)", "Istruttoria tecnica CTPNRR-PNIEC")]) == "approved"


PAGE = """
<div class="line_small_title"><h2>Progetto</h2></div>
<p><strong>Opera</strong>: Impianto fotovoltaico nel comune di Grottole</p>
<p><strong>Progetto</strong>: Progetto di un impianto fotovoltaico di potenza nominale pari a 19,830 MWp. Codice pratica MY TERNA n. 202100075.</p>
<p><strong>Proponente</strong>: BLUSOLAR GROTTOLE 1 S.r.l.</p>
<p><strong>Tipologia di opera</strong>: Agrivoltaici</p>
<p><strong>Regioni:</strong> Basilicata</p>
<p><strong>Province:</strong> Matera</p>
<p><strong>Comuni:</strong> Grottole, Matera</p>
<p><strong>Aree marine:</strong> Nessuna area marina </p>
<table><tbody>
<tr class="trProcedura"><td>Valutazione Impatto Ambientale (PNIEC-PNRR)</td><td></td><td>8536</td><td>07/06/2022</td><td>Conclusa</td>
<td class="procedura"><a>Dettagli</a></td><td></td></tr>
<tr class="datiAmministrativi" style="display: none"><td>Data presentazione istanza:</td><td colspan="6">07/06/2022</td></tr>
<tr class="datiAmministrativi" style="display: none"><td>Data Decreto VIA:</td><td colspan="6">03/07/2026</td></tr>
<tr class="datiAmministrativi" style="display: none"><td>Esito Decreto VIA:</td><td colspan="6">Positivo con prescrizioni/raccomandazioni</td></tr>
<tr class="datiAmministrativi" style="display: none"><td>Responsabile del procedimento:</td><td colspan="6">Name - tel. 06</td></tr>
<tr class="datiAmministrativi" style="display: none"><td>Stato procedura:</td><td colspan="6">Conclusa</td></tr>
</tbody></table>
"""


def test_project_page_is_parsed_without_the_official_in_charge():
    project = fm.parse_project(PAGE, 8783)
    assert project["name"] == "Impianto fotovoltaico nel comune di Grottole"
    assert project["proponent"] == "BLUSOLAR GROTTOLE 1 S.r.l."
    assert project["regions"] == ["Basilicata"] and project["municipalities"] == ["Grottole", "Matera"]
    [via] = project["procedures"]
    assert via == {"type": "Valutazione Impatto Ambientale (PNIEC-PNRR)", "code": "8536", "start": "2022-06-07",
                   "status": "Conclusa", "submitted": "2022-06-07", "decree_date": "2026-07-03",
                   "outcome": "Positivo con prescrizioni/raccomandazioni"}
    project["typology_id"] = 41
    fm.enrich(project)
    assert (project["technology"], project["mw"], project["stage"]) == ("agrivoltaic", 19.83, "approved")
    assert project["terna_codes"] == ["202100075"] and project["decision"] == "2026-07-03" and project["current"] == 0


def test_listing_page_ids_and_page_count():
    page = ('<a href="https://va.mite.gov.it/it-IT/Oggetti/Info/12179" class="x">info</a>'
            '<a href="https://va.mite.gov.it/it-IT/Oggetti/Documentazione/12179/1">doc</a>'
            '<a href="https://va.mite.gov.it/it-IT/Oggetti/Info/12170" class="x">info</a>'
            '<li class="etichettaRicerca">Pagina 1 di 38</li>')
    assert fm.parse_listing(page) == ([12179, 12170], 38)


def test_refresh_takes_new_projects_then_the_oldest_checked_open_ones():
    projects = {1: {"id": 1, "stage": "assessment", "checked": "2026-09-01"},
                2: {"id": 2, "stage": "assessment", "checked": "2026-08-01"},
                3: {"id": 3, "stage": "approved", "checked": "2026-07-01"}}
    assert fm.to_refresh(projects, [9], open_budget=1, closed_budget=1) == [9, 2, 3]


def test_short_name_is_the_quoted_name():
    assert fm.short_name("Costruzione di un impianto eolico denominato \"Monte Croce di Ferro\"", "") == "Monte Croce di Ferro"
    assert fm.short_name("Impianto MANFREDONIA", "repowering denominato \"Manfredonia\", con") == "Manfredonia"
    assert fm.short_name("ELETTRODOTTO 150 KV CINECITTA' - BANCA D'ITALIA", "") == "ELETTRODOTTO 150 KV CINECITTA' - BANCA D'ITALIA"
