"""
Italy's capacity market (mercato della capacità): the results of Terna's
main auctions (asta madre), one per delivery year, written to
app/policy/capacity_market.json for the Policy section.

Terna publishes each auction's results as a PDF report (Rendiconto degli
esiti).  It names the new capacity unit by unit (participant, area, plant,
technology, MW, for storage its hours), but the existing capacity only as
totals per participant and area, so:

  units        the new capacity, from each report's lists (for the 2022 and
               2023 auctions, from Terna's list of the units nominated after
               the auction, March 2021): text tables, read from the PDFs.
  national     the MW per area and participant, existing and new: text
               tables in the 2022 and 2023 reports, images from 2024, typed
               in etl/capacity_market_transcribed.json.
  foreign      the MW per foreign area and participant (capacity abroad
               that delivers through the borders), the same way.
  auctions     each auction's prices, totals and costs, from its report's
               summary (AUCTIONS below), checked against the tables.

MW are CDP (capacità disponibile in probabilità), the derated capacity Terna
credits a unit with; a premium is € per MW of CDP a year.  CDP × premium is
before the reliability option (the holder pays back the day-ahead price
above the strike price) and any penalty.  Terna gives no UP codes: a unit
is matched to GME's units by its name, area and size.

The 2022-2024 auctions bought new capacity already authorised and not yet
authorised, at different prices in 2024; the lists do not say which unit is
which, so it is inferred from the participants' MW of each (per area in
2024): a unit is marked when it falls on the same side in every split of
its participant's units that matches them.

Run when a report is out: its auction goes into AUCTIONS, its image tables
into the transcribed file.  Needs pdfplumber (py -m pip install pdfplumber).
  py etl/build_capacity_market.py
"""

import argparse
import json
import os
import re
import tempfile
import urllib.parse
from datetime import datetime, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(ROOT, "app", "policy", "capacity_market.json")
TRANSCRIBED_PATH = os.path.join(ROOT, "etl", "capacity_market_transcribed.json")
USER_AGENT = "it-power-dashboard/1.0 (+https://github.com/aragn/it-power-dashboard)"

TERNA = "https://download.terna.it/terna/"
NOMINATED_2022_2023 = TERNA + "2021_03_26_Capacit%C3%A0%20Nuova%202022%202023_V1_8d8f2ec81e10b99.pdf"

# Each auction's figures as its report states them.  premium: €/MW a year
# (new_not_authorised per area when it differs); mw: CDP awarded; cost: M€
# (earlier_new: the new capacity of the earlier auctions still paid in the
# delivery year, after the contracts terminated); offered: CDP qualified and
# offered, national and foreign (in GW to one decimal in 2022 and 2023).
# units: the pages (and table on the page) of the new-capacity lists, with
# what a list leaves out.  contract: how many delivery years a new unit's
# contract runs (existing capacity's: the delivery year alone), and who says so.
AUCTIONS = [
    {
        "year": 2022, "date": "2019-11-06", "rules": "DM 28 June 2019",
        "report": TERNA + "2019_12_06_Rendiconto%20EsitiAsta%202022_PUBBLICATO_8d7c06cc9f8470b.pdf",
        "units": [{"url": NOMINATED_2022_2023, "page": 2}],
        "participant_pages": [9, 10], "area_pages": [12, 13],
        "premium": {"existing": 33000, "new": 75000, "new_not_authorised": 75000,
                    "foreign": {"NORD": 4400, "CSUD": 3449, "SUD": 4000}},
        "mw": {"existing": 34758, "new": 1401, "new_not_authorised": 366, "foreign": {"NORD": 4241, "CSUD": 104, "SUD": 49}},
        "cost": {"total": 1299, "foreign": 19.2, "earlier_new": 0},
        "offered": {"national_qualified": 42800, "national_offered": 36500, "foreign_qualified": 21800, "foreign_offered": 4800,
                    "rounded": True},
        "contract": {"years": 15, "confirmed": True, "source": TERNA + "2019_12_06_Rendiconto%20EsitiAsta%202022_PUBBLICATO_8d7c06cc9f8470b.pdf", "note": "Terna's report: new capacity's delivery period lasts 15 years"},
    },
    {
        "year": 2023, "date": "2019-11-28", "rules": "DM 28 June 2019",
        "report": TERNA + "Rendiconto%20EsitiAsta%202023_8d78adbacbbe508.pdf",
        "units": [{"url": NOMINATED_2022_2023, "page": 3}, {"url": NOMINATED_2022_2023, "page": 4}],
        "participant_pages": [9, 10], "area_pages": [12, 13],
        "premium": {"existing": 33000, "new": 75000, "new_not_authorised": 75000,
                    "foreign": {"NORD": 4400, "CSUD": 4949, "SUD": 3999}},
        "mw": {"existing": 35013, "new": 528, "new_not_authorised": 3476, "foreign": {"NORD": 4241, "CSUD": 104, "SUD": 49}},
        "cost": {"total": 1475, "foreign": 19.4, "earlier_new": 133},
        "offered": {"national_qualified": 45300, "national_offered": 39000, "foreign_qualified": 19000, "foreign_offered": 4700,
                    "rounded": True},
        "contract": {"years": 15, "confirmed": True, "source": "https://www.teleborsa.it/News/2019/12/05/a2a-si-aggiudica-5gw-nella-seconda-asta-mercato-della-capacita-35.html", "note": "A2A on the auction: 75,000 €/MW for 15 years for the capacity to be built"},
    },
    {
        "year": 2024, "date": "2022-02-21", "rules": "DM 28 October 2021",
        "report": TERNA + "Mercato_Capacit%C3%A0_Rendiconto_esiti_Asta_madre_2024_8da140f8874afcd.pdf",
        "units": [{"page": 8, "technology": "ACCUMULO ELETTROCHIMICO"}, {"page": 9}],
        "premium": {"existing": 33000, "new": 70000,
                    "new_not_authorised": {"NORD": 48110, "CSUD": 33467, "SICI": 33467, "SARD_N": 51012, "SARD_S": 51012},
                    "foreign": {"NORD": 33000, "CSUD": 17010, "SUD": 25075}},
        "mw": {"existing": 34153, "new": 1504, "new_not_authorised": 2274, "foreign": {"NORD": 3445, "CSUD": 113, "SUD": 52}},
        "cost": {"total": 1459.3, "foreign": 116.9, "earlier_new": 366, "terminated_mw": 886},
        "offered": {"national_qualified": 50234, "national_offered": 41502, "foreign_qualified": 10470, "foreign_offered": 3744},
        "contract": {"years": 15, "confirmed": True, "source": "https://corporate.enel.it/content/dam/enel-common/press/it/2022-febbraio/CS%20capacity%20market%20asta%202024.pdf", "note": "Enel and A2A on the auction: one-year contracts for existing capacity, 15-year for new"},
    },
    {
        "year": 2025, "date": "2024-07-25", "rules": "DM 9 May 2024",
        "report": TERNA + "Rendiconto_Esiti_Asta_Madre_2025_8dcf4ef84018b81.pdf",
        "units": [{"page": 8, "table": 0}, {"page": 8, "table": 1, "initiative": "Ripotenziamento"}],
        "premium": {"existing": 45000, "new": 67500, "foreign": {"NORD": 4788, "CSUD": 3968, "SUD": 3491}},
        "mw": {"existing": 37581, "new": 174, "new_not_authorised": 0, "foreign": {"NORD": 4200, "CSUD": 113, "SUD": 52}},
        "cost": {"total": 1723.6, "foreign": 20.74, "earlier_new": 550, "terminated_mw": 1500},
        "offered": {"national_qualified": 40189, "national_offered": 37959, "foreign_qualified": 18179, "foreign_offered": 6348},
        "contract": {"years": 15, "confirmed": False, "source": None, "note": "Not confirmed: no statement found for this auction; assumed 15 years like the auctions before and after it"},
    },
    {
        "year": 2026, "date": "2024-12-18", "rules": "DM 17 October 2024",
        "report": TERNA + "Rendiconto_Esiti_Asta_Madre_2026_8dd4c48aa98baa4.pdf",
        "units": [{"page": 9, "table": 0}, {"page": 9, "table": 1, "initiative": "Ripotenziamento"}],
        "premium": {"existing": 46000, "new": 56160, "foreign": {"NORD": 11200, "CSUD": 7199, "SUD": 4500}},
        "mw": {"existing": 38265, "new": 140, "new_not_authorised": 0, "foreign": {"NORD": 4200, "CSUD": 113, "SUD": 52}},
        "cost": {"total": 1816.1, "foreign": 48.09, "earlier_new": 565, "terminated_mw": 1500},
        "offered": {"national_qualified": 40887, "national_offered": 38414, "foreign_qualified": 16492, "foreign_offered": 5959},
        "contract": {"years": 15, "confirmed": True, "source": "https://www.energy-storage.news/?p=85454", "note": "Neoen (Broni battery, this auction): a 15-year capacity contract"},
    },
    {
        "year": 2027, "date": "2025-02-26", "rules": "DM 17 October 2024",
        "report": TERNA + "Rendiconto-Esiti-Asta-Madre_2027_8dd97982c75d724.pdf",
        "units": [{"page": 9}, {"page": 10}],
        "premium": {"existing": 47000, "new": 47000, "foreign": {"NORD": 7199, "CSUD": 5579, "SUD": 6500}},
        "mw": {"existing": 38047, "new": 594, "new_not_authorised": 0, "foreign": {"NORD": 4200, "CSUD": 113, "SUD": 52}},
        "cost": {"total": 1847.3, "foreign": 31.2, "earlier_new": 572, "terminated_mw": 1500},
        "offered": {"national_qualified": 41886, "national_offered": 39239, "foreign_qualified": 15281, "foreign_offered": 5791},
        "contract": {"years": 15, "confirmed": True, "source": "https://www.power-technology.com/news/aer-soleir-rondissone-bess-project/", "note": "Aer Soléir (Rondissone battery, AS Storage, this auction): a 15-year capacity contract"},
    },
]

# Terna's area names and codes -> the areas here: the bidding zones, with
# Sardinia split in two from the 2024 auction.
AREAS = {
    "NORD": "NORD", "CENTRO NORD": "CNOR", "CNOR": "CNOR", "CENTRO SUD": "CSUD", "CSUD": "CSUD", "SUD": "SUD",
    "CALABRIA": "CALA", "CALA": "CALA", "SICILIA": "SICI", "SICI": "SICI", "SARDEGNA": "SARD", "SARD": "SARD",
    "SARDEGNA NORD": "SARD_N", "SARD_NORD": "SARD_N", "SARDEGNA SUD": "SARD_S", "SARD_SUD": "SARD_S",
}
FOREIGN_AREAS = {"ESTERO NORD": "NORD", "ESTERO CSUD": "CSUD", "ESTERO CENTRO SUD": "CSUD", "ESTERO SUD": "SUD"}

TECHNOLOGIES = {
    "ACCUMULO ELETTROCHIMICO": "battery", "ACCUMULO": "battery", "ACCUMULO + FV": "battery_solar",
    "TERMICO COMBINATO": "ccgt", "TERMICO TURBOGAS": "ocgt", "TERMICO COMBUSTIONE INTERNA": "engine",
    "TERMICO TRADIZIONALE": "thermal", "TERMICO MISTO": "thermal",
    "IDRICO SERBATOIO": "hydro", "SOLARE": "solar", "EOLICO": "wind",
}
INITIATIVES = {"IMPIANTO NUOVO": "new", "RIPOTENZIAMENTO": "repowering"}
# A list's footnotes, put in the plant's name.
PLANT_NOTES = {(2027, "Più impianti*"): "Più impianti: Castenedolo, Fiesse1, Fiesse2, Sospiro, Remedello"}

# Derating: the share of a plant's power Terna credits as CDP (1 - the
# derating rate), from Terna's yearly annex to its technical rules (DTF 2)
# and, for 2028, the ministry's decree approving it.  For 2022-2024 Terna
# published only preliminary ranges, so those units get none.
DERATING = {
    "sources": [
        {"years": "2027", "url": TERNA + "Allegato_3_alla_DTF2_Anno_di_consegna_2027_8dd20da26b4b11d.pdf",
         "note": "Terna, Allegato 3 alla DTF n. 2, delivery 2027 (tables 9-11)"},
        {"years": "2025-2026", "url": "https://www.staffettaonline.com/articolo.aspx?id=391859",
         "note": "Staffetta Quotidiana: the 2027 annex confirms the 2025 and 2026 auctions' derating rates"},
        {"years": "2028", "url": "https://www.mase.gov.it/portale/documents/d/guest/dm_327_16-10-2026-pdf",
         "note": "MASE decree 327 of 16 October 2026, Allegato 4 to DTF n. 2 (tables 9-11)"},
    ],
    # Batteries: CDP as % of power for 1 to 8 hours of storage.
    "batteries": {
        "2025-2027": {"zones": "Italy", "values": [24, 44, 56, 67, 74, 81, 86, 90]},
        "2028": {
            "NORD_CNOR": [11, 21, 30, 40, 44, 49, 52, 55],
            "CSUD": [13, 26, 39, 51, 58, 64, 68, 71],
            "SUD_CALA": [7, 13, 17, 21, 24, 26, 28, 29],
            "SICI_SARD": [7, 13, 17, 21, 24, 27, 30, 33],
        },
    },
    # Derating rates (%): renewables by source, the other technologies' expected averages, new thermal plants.
    "technologies": {
        "2027": {"solar": 88, "wind": 84, "run_of_river": 67, "thermal": 23, "geothermal": 25, "hydro": 41, "pumped": 39,
                 "new_thermal": 10, "new_ccgt_water": 20},
        "2028": {"solar": 91.7, "wind": 84, "run_of_river": 67, "thermal": 21, "geothermal": 27, "hydro": 43, "pumped": 40,
                 "new_thermal": 10, "new_ccgt_water": 20},
    },
}
DERATED_YEARS = (2025, 2026, 2027)


def unit_rating(unit):
    """The share of the unit's power counted as CDP, and how it was found:
    batteries by their hours (between two whole hours, interpolated), solar
    and wind by source; None where the tables do not say."""
    if unit["year"] not in DERATED_YEARS:
        return None, None
    if unit["technology"] in ("solar", "wind"):
        return round(1 - DERATING["technologies"]["2027"][unit["technology"]] / 100, 4), "source"
    if unit["technology"] == "battery" and unit["hours"]:
        values = DERATING["batteries"]["2025-2027"]["values"]
        hours = min(max(unit["hours"], 1), 8)
        low = int(hours)
        if hours == low:
            return values[low - 1] / 100, "hours"
        share = values[low - 1] + (values[low] - values[low - 1]) * (hours - low)
        return round(share / 100, 4), "interpolated"
    return None, None

# Participants -> the group they belong to (the author's reading of the
# names, for the totals by group); the rest stand alone.
GROUPS = [
    (r"^ENEL", "Enel"), (r"^A2A", "A2A"), (r"^EDISON", "Edison"), (r"^ENI ", "Eni"),
    (r"^(EP |EPP\d|EP\b)", "EPH (EP Produzione)"), (r"^SORGENIA", "Sorgenia"), (r"^IREN", "Iren"),
    (r"^TIRRENO POWER", "Tirreno Power"), (r"^ENGIE", "Engie"), (r"^AXPO", "Axpo"), (r"^DOLOMITI", "Dolomiti Energia"),
    (r"^ERGOSUD", "Ergosud"), (r"^ERG ", "ERG"), (r"^ALPIQ", "Alpiq"), (r"^ALPERIA", "Alperia"), (r"^(CVA|C\.V\.A\.)", "CVA"),
    (r"^DUFERCO", "Duferco"), (r"^METAENERGIA", "Metaenergia"), (r"^MYT ", "Metlen (MYT)"), (r"^RENANTIS", "Renantis"),
    (r"^BKW", "BKW"), (r"^REPOWER", "Repower"), (r"^ATLAS STORAGE", "Atlas Storage"), (r"^SUNPRIME", "Sunprime"),
    (r"^ITALIAN SMART STORAGE", "Italian Smart Storage"), (r"^EGO ", "Ego Energy"), (r"^HERA ", "Hera"), (r"^ACEA ", "Acea"),
    (r"^NEOEN", "Neoen"), (r"^SARAS", "Saras"),
]
# Kept in capitals when a name is put in title case.
ACRONYMS = {"ACL", "AS", "B2G", "DXT", "IGS", "SET", "SPV"}
# Names a list spells differently from the tables (as participant_key gives them).
ALIASES = {"EPCTMONTANASO": "EPCENTRALETAVAZZANOMONTANASO"}
LEGAL_SUFFIX = re.compile(r"(?:\s+|,\s*)(S\.?P\.?A\.?|S\.?R\.?L\.?S?\.?|SRL\.|S\.?A\.?|A\.?G\.?|A\.?S\.?|SOCIETA' BENEFIT)$", re.IGNORECASE)


class CheckError(Exception):
    """A total that does not add up: the parsing or the typing is wrong."""


def check(condition, message):
    if not condition:
        raise CheckError(message)


def clean(text):
    return re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip()


def number(text):
    """Terna's numbers: '1.767' is 1767, '64,5' is 64.5; blank is None."""
    text = clean(text)
    if not text:
        return None
    return float(text.replace(".", "").replace(",", "."))


def is_number(text):
    return bool(re.fullmatch(r"\d[\d.]*(,\d+)?", clean(text)))


def participant_key(name):
    """A participant's name without punctuation and legal form, to match
    the same company across tables ('UNIGRA'' and 'UNIGRA' S.R.L.')."""
    key = re.sub(r"[^A-Z0-9]", "", name.upper())
    while True:
        stripped = re.sub(r"(SPA|SRLS|SRL)$", "", key)
        if stripped == key:
            return ALIASES.get(key, key)
        key = stripped


def group_of(name):
    upper = name.upper()
    for pattern, group in GROUPS:
        if re.search(pattern, upper):
            return group
    # The name without its legal form, in title case.
    base = name
    while LEGAL_SUFFIX.search(base):
        base = LEGAL_SUFFIX.sub("", base).strip()
    return " ".join(word if word in ACRONYMS or any(char.isdigit() for char in word) or not word.isupper() else word.capitalize()
                    for word in base.split())


def area_code(name):
    code = AREAS.get(clean(name).upper())
    check(code is not None, f"unknown area {name!r}")
    return code


def foreign_code(name):
    code = FOREIGN_AREAS.get(clean(name).upper())
    check(code is not None, f"unknown foreign area {name!r}")
    return code


def zone_of(area):
    return "SARD" if area.startswith("SARD") else area


# ---------- PDFs ----------

def download(url, cache_dir):
    name = urllib.parse.unquote(url.rsplit("/", 1)[1]).replace(" ", "_")
    path = os.path.join(cache_dir, name)
    if not os.path.exists(path):
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=120)
        response.raise_for_status()
        with open(path, "wb") as handle:
            handle.write(response.content)
    return path


def page_tables(path, page):
    import pdfplumber  # only this script needs it
    with pdfplumber.open(path) as pdf:
        return [[[clean(cell) for cell in row] for row in table] for table in pdf.pages[page - 1].extract_tables()]


# ---------- New-capacity lists ----------

UNIT_COLUMNS = [("PARTECIPANTE", "participant"), ("AREA", "area"), ("DENOMINAZIONE", "plant"),
                ("TIPO INIZIATIVA", "initiative"), ("TIPOLOGIA", "technology"), ("CDP", "mw"), ("ENERGETICA", "hours")]


def unit_columns(header):
    columns = {}
    for index, cell in enumerate(header):
        for key, field in UNIT_COLUMNS:
            if key in cell.upper() and field not in columns:
                columns[field] = index
                break
    check({"participant", "area", "plant", "mw"} <= set(columns), f"unit table header not understood: {header}")
    return columns


def parse_unit_rows(rows, spec, state):
    """The units of one table; state carries the sum since the last
    'Totale' row (a list can run over two pages) and checks it there."""
    units = []
    columns = None
    for cells in rows:
        if cells and cells[0].upper().startswith("PARTECIPANTE"):
            columns = unit_columns(cells)
            continue
        if columns is None or not any(cells):
            continue
        if any(cell.upper() == "TOTALE" for cell in cells):
            total = number(next(cell for cell in cells if is_number(cell)))
            check(abs(total - state["running"]) <= 1, f"unit list total {total} but the units add up to {state['running']}")
            state["running"] = 0.0
            continue
        get = lambda field: cells[columns[field]] if field in columns else ""
        technology = clean(get("technology") or spec.get("technology", "")).upper()
        initiative = clean(get("initiative") or spec.get("initiative", "Impianto Nuovo")).upper()
        check(technology in TECHNOLOGIES, f"unknown technology {technology!r}")
        check(initiative in INITIATIVES, f"unknown initiative {initiative!r}")
        mw = number(get("mw"))
        check(mw is not None and mw > 0, f"unit without MW: {cells}")
        units.append({
            "participant": get("participant"), "area": area_code(get("area")), "plant": get("plant"),
            "technology": TECHNOLOGIES[technology], "technology_it": technology, "initiative": INITIATIVES[initiative],
            "mw": mw, "hours": number(get("hours")),
        })
        state["running"] += mw
    return units


def read_units(auction, cache_dir):
    state = {"running": 0.0}
    units = []
    for spec in auction["units"]:
        path = download(spec.get("url", auction["report"]), cache_dir)
        tables = page_tables(path, spec["page"])
        indexes = [spec["table"]] if "table" in spec else range(len(tables))
        for index in indexes:
            units += parse_unit_rows(tables[index], spec, state)
    check(state["running"] == 0, f"{auction['year']}: the unit list has no closing total")
    return units


# ---------- Tables by participant and area ----------

def read_area_table(path, pages):
    """2022 and 2023: [area, participant, existing, new] per row, each area
    and the country checked against their 'Totale' rows."""
    rows, area, sums = [], None, {"existing": 0.0, "new": 0.0}
    area_sums = {"existing": 0.0, "new": 0.0}
    for page in pages:
        for table in page_tables(path, page):
            for cells in table:
                if not any(cells) or cells[0].upper() == "AREA":
                    continue
                if cells[0].upper().startswith("TOTALE"):
                    values = [number(cell) for cell in cells if is_number(cell)]
                    target = sums if cells[0].upper() == "TOTALE ITALIA" else area_sums
                    check(abs(values[0] - target["existing"]) < 0.5 and abs(values[1] - target["new"]) < 0.5,
                          f"{cells[0]}: {values} against {target}")
                    area_sums = {"existing": 0.0, "new": 0.0}
                    continue
                if cells[0]:
                    area = area_code(cells[0])
                existing, new, total = (number(cell) for cell in cells[2:5])
                check(abs(existing + new - total) < 0.5, f"row does not add up: {cells}")
                rows.append([area, cells[1], existing, new])
                for key, value in (("existing", existing), ("new", new)):
                    sums[key] += value
                    area_sums[key] += value
    return rows


PARTICIPANT_COLUMNS = {"ESISTENTE": "existing", "NUOVA AUTORIZZATA": "new", "NUOVA NON AUTORIZZATA": "new_not_authorised",
                       "ESTERO NORD": "NORD", "ESTERO CENTRO SUD": "CSUD", "ESTERO SUD": "SUD", "TOTALE": "total"}


def read_participant_table(path, pages):
    """2022 and 2023: each participant's MW by kind and foreign area."""
    rows, sums = [], {}
    for page in pages:
        for table in page_tables(path, page):
            columns = None
            for cells in table:
                if cells and cells[0].upper() == "PARTECIPANTE":
                    columns = {PARTICIPANT_COLUMNS[cell.upper()]: index for index, cell in enumerate(cells) if cell.upper() in PARTICIPANT_COLUMNS}
                    continue
                if columns is None or not cells[0]:
                    continue
                values = {key: number(cells[index]) or 0.0 for key, index in columns.items()}
                if cells[0].upper() == "TOTALE":
                    for key, value in values.items():
                        check(abs(value - sums.get(key, 0.0)) < 0.5, f"participants' {key}: total {value}, rows {sums.get(key)}")
                    return rows
                check(abs(sum(value for key, value in values.items() if key != "total") - values["total"]) < 0.5,
                      f"participant row does not add up: {cells}")
                rows.append({"participant": cells[0], **values})
                for key, value in values.items():
                    sums[key] = sums.get(key, 0.0) + value
    raise CheckError("participant table without a total")


def read_transcribed(year, transcribed):
    """2024 on: the typed image tables, each area checked against its total.
    National rows: [area, participant, existing, new authorised, new not authorised]."""
    data = transcribed[str(year)]
    national = []
    for area_name, rows in data["national"].items():
        area = area_code(area_name)
        totals = data["area_totals"][area_name]
        width = len(totals)
        for row in rows:
            check(len(row) == width + 1, f"{year} {area_name}: row {row} has not {width} values")
        for column in range(width):
            check(sum(row[column + 1] for row in rows) == totals[column],
                  f"{year} {area_name}: column {column} adds up to {sum(row[column + 1] for row in rows)}, not {totals[column]}")
        national += [[area, row[0], row[1], row[2], row[3] if width == 3 else 0] for row in rows]
    foreign = [[foreign_code(area), name, mw] for area, rows in data["foreign"].items() for name, mw in rows]
    return national, foreign


# ---------- Authorised or not ----------

def infer_authorised(units, authorised_mw, other_mw, tolerance=1.0):
    """Marks each unit of one participant (or participant and area) as
    authorised (True), not (False) or unknown (None) from its MW of each.
    The tables round each total to the MW, so a split may miss by up to
    `tolerance`; the closest splits count."""
    if other_mw < tolerance / 2:
        for unit in units:
            unit["authorised"] = True
        return
    if authorised_mw < tolerance / 2:
        for unit in units:
            unit["authorised"] = False
        return
    for unit in units:
        unit["authorised"] = None
    if len(units) > 20:
        return
    total = sum(unit["mw"] for unit in units)
    splits = []
    for mask in range(1 << len(units)):
        chosen = sum(unit["mw"] for index, unit in enumerate(units) if mask >> index & 1)
        error = abs(chosen - authorised_mw) + abs(total - chosen - other_mw)
        if abs(chosen - authorised_mw) <= tolerance and abs(total - chosen - other_mw) <= tolerance:
            splits.append((error, mask))
    best = min((error for error, _ in splits), default=0)
    sides = [mask for error, mask in splits if error <= best + 0.25]
    for index, unit in enumerate(units):
        if sides and all(mask >> index & 1 for mask in sides):
            unit["authorised"] = True
        elif sides and not any(mask >> index & 1 for mask in sides):
            unit["authorised"] = False


def unit_premium(auction, unit):
    premium = auction["premium"]
    other = premium.get("new_not_authorised", premium["new"])
    if isinstance(other, dict):
        other = other.get(unit["area"])
    if unit["authorised"] is None:
        return premium["new"] if other == premium["new"] else None
    return premium["new"] if unit["authorised"] else other


# ---------- One auction ----------

def build_auction(auction, transcribed, cache_dir):
    year = auction["year"]
    premium, mw = auction["premium"], auction["mw"]
    units = read_units(auction, cache_dir)
    for unit in units:
        unit["year"] = year
        unit["plant"] = PLANT_NOTES.get((year, unit["plant"]), unit["plant"])
        unit["rating"], unit["rating_basis"] = unit_rating(unit)
        # From 2025 all new capacity had to be authorised; before, the
        # tables tell (units they do not cover stay unknown).
        unit["authorised"] = True if not mw["new_not_authorised"] else None

    if year in (2022, 2023):
        report = download(auction["report"], cache_dir)
        participants = read_participant_table(report, auction["participant_pages"])
        national = [[area, name, existing, new, None] for area, name, existing, new in read_area_table(report, auction["area_pages"])]
        foreign = [[border, row["participant"], row[border]] for row in participants for border in ("NORD", "CSUD", "SUD") if row[border]]
        # The area table does not split new capacity, the participant table does.
        for row in participants:
            key = participant_key(row["participant"])
            infer_authorised([unit for unit in units if participant_key(unit["participant"]) == key],
                             row["new"], row["new_not_authorised"])
        # Each participant's existing MW, by area and in all.
        for row in participants:
            key = participant_key(row["participant"])
            by_area = sum(item[2] for item in national if participant_key(item[1]) == key)
            check(abs(by_area - row["existing"]) < 0.5, f"{year} {row['participant']}: existing {row['existing']} but {by_area} by area")
    else:
        national, foreign = read_transcribed(year, transcribed)
        for area, name, existing, new, other in national:
            key = participant_key(name)
            infer_authorised([unit for unit in units if participant_key(unit["participant"]) == key and unit["area"] == area], new, other)

    # The tables against the summary.
    existing = sum(row[2] for row in national)
    new = sum(row[3] + (row[4] or 0) for row in national)
    check(abs(existing - mw["existing"]) < 0.5, f"{year}: existing {existing} against {mw['existing']}")
    check(abs(new - mw["new"] - mw["new_not_authorised"]) < 0.5, f"{year}: new {new} against the summary")
    for border, target in mw["foreign"].items():
        total = sum(row[2] for row in foreign if row[0] == border)
        check(abs(total - target) < 0.5, f"{year}: foreign {border} {total} against {target}")
    listed_mw = sum(unit["mw"] for unit in units)
    check(abs(listed_mw - mw["new"] - mw["new_not_authorised"]) <= 2, f"{year}: units add up to {listed_mw}")

    # Each participant's new MW in an area against its units there.
    mismatches = []
    for area, name, _, new_mw, other in national:
        key = participant_key(name)
        listed = sum(unit["mw"] for unit in units if participant_key(unit["participant"]) == key and unit["area"] == area)
        if abs(listed - new_mw - (other or 0)) > 1:
            mismatches.append(f"{name} {area}: table {new_mw + (other or 0)}, units {listed}")
    if mismatches:
        print(f"  {year}: new MW by participant and area differ from the units: " + "; ".join(mismatches))

    # Money: CDP × premium, € a year.
    other_premium = premium.get("new_not_authorised", premium["new"])
    for unit in units:
        unit["premium"] = unit_premium(auction, unit)
    rows = []
    for area, name, existing_mw, new_mw, other in national:
        if other is None:  # 2022-2023: one price for all new capacity
            payment = existing_mw * premium["existing"] + new_mw * premium["new"]
        else:
            area_other = other_premium.get(area, 0) if isinstance(other_premium, dict) else other_premium
            check(not other or area_other, f"{year} {area}: no price for new capacity not authorised")
            payment = existing_mw * premium["existing"] + new_mw * premium["new"] + other * area_other
        rows.append([area, name, existing_mw, new_mw + (other or 0), round(payment)])
    foreign_rows = [[border, name, value, round(value * premium["foreign"][border])] for border, name, value in foreign]

    costs = {
        "existing": mw["existing"] * premium["existing"] / 1e6,
        "new": mw["new"] * premium["new"] / 1e6,
        "new_not_authorised": (sum(row[4] * other_premium.get(row[0], 0) for row in national if row[4]) if isinstance(other_premium, dict)
                               else mw["new_not_authorised"] * other_premium) / 1e6,
        "foreign": sum(value * premium["foreign"][border] for border, value in mw["foreign"].items()) / 1e6,
    }
    stated = auction["cost"]
    check(abs(costs["foreign"] - stated["foreign"]) <= max(0.1, 0.005 * stated["foreign"]),
          f"{year}: foreign cost {costs['foreign']:.2f} against {stated['foreign']}")
    check(abs(sum(costs.values()) - stated["total"]) <= 0.005 * stated["total"],
          f"{year}: cost {sum(costs.values()):.1f} against {stated['total']}")

    status = [unit["authorised"] for unit in units]
    print(f"  {year}: {len(units)} units, {listed_mw:.0f} MW new; authorised {status.count(True)}, not {status.count(False)}, "
          f"unknown {status.count(None)}; {len(rows)} participant-area rows, {len(foreign_rows)} foreign")

    out = {key: auction[key] for key in ("year", "date", "rules", "report", "premium", "mw", "offered", "contract")}
    out["units_source"] = sorted({spec.get("url", auction["report"]) for spec in auction["units"]})
    out["cost"] = {**{key: round(value, 2) for key, value in costs.items()}, "total": stated["total"],
                   "earlier_new": stated["earlier_new"], **({"terminated_mw": stated["terminated_mw"]} if "terminated_mw" in stated else {})}
    out["national"] = rows
    out["foreign"] = foreign_rows
    return out, units


def build(cache_dir):
    with open(TRANSCRIBED_PATH, encoding="utf-8") as handle:
        transcribed = json.load(handle)
    auctions, units = [], []
    for auction in AUCTIONS:
        built, auction_units = build_auction(auction, transcribed, cache_dir)
        auctions.append(built)
        units += auction_units
    names = sorted({row[1] for auction in auctions for row in auction["national"] + auction["foreign"]} | {unit["participant"] for unit in units})
    unit_fields = ["year", "participant", "area", "plant", "technology", "technology_it", "initiative", "authorised", "mw", "hours", "premium",
                   "rating", "rating_basis"]
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Terna, Mercato della capacità: Rendiconto degli esiti (asta madre), 2022-2027 delivery years",
        "units_columns": unit_fields,
        "units": [[unit[field] for field in unit_fields] for unit in units],
        "auctions": auctions,
        "groups": {name: group_of(name) for name in names},
        "derating": DERATING,
    }


def main():
    parser = argparse.ArgumentParser(description="Build the capacity market file from Terna's auction reports.")
    parser.add_argument("--cache", default=os.path.join(tempfile.gettempdir(), "capacity_market_pdfs"),
                        help="folder for the downloaded reports (kept between runs)")
    args = parser.parse_args()
    os.makedirs(args.cache, exist_ok=True)
    data = build(args.cache)
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUTPUT_PATH}: {len(data['units'])} units, {len(data['auctions'])} auctions.")


if __name__ == "__main__":
    main()
