"""
Fetch the environmental-assessment pipeline of Italy's power projects
from the Ministry of the Environment and Energy Security (MASE, portal
va.mite.gov.it) and write app/data/mase_projects.json.

MASE runs the state-level environmental impact assessment (VIA) of the
larger projects: wind farms, solar and agrivoltaic plants, offshore wind,
hydro, geothermal and thermal power plants, power lines; projects below
the state thresholds go to the regions and are not here.  The portal lists
the projects by type ("Tipologia di opera"); each project's page gives the
proponent, the regions, provinces and municipalities, and every procedure
with its dates, status and, for decisions, the decree's outcome.  The
portal's GIS services (WMS/WFS) are disabled, so the pages are read.

Per project: name, description, proponent, technology, MW and storage MW
read from the description (when it states them), territories, the Terna
connection codes the description quotes ("Codice pratica MY TERNA"), the
procedures and a stage derived from them (see project_stage).

Routine runs read each type's listing (newest first) until a page brings
no new project, then the pages of new projects and of the ones checked
longest ago, open ones first.  --full reads every listing page (and drops
projects no longer listed).
"""

import argparse
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

BASE_URL = "https://va.mite.gov.it"
LIST_URL = BASE_URL + "/it-IT/Ricerca/ViaTipologia"
INFO_URL = BASE_URL + "/it-IT/Oggetti/Info/{id}"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "data", "mase_projects.json")
USER_AGENT = "it-power-dashboard/1.0 (+https://github.com/aragn/it-power-dashboard)"

# MASE "Tipologia di opera" -> the technology it holds (refined from the
# description for power plants and hydro, see technology()).
TYPOLOGIES = {
    38: "solar",
    41: "agrivoltaic",
    36: "wind_onshore",
    34: "wind_offshore",
    35: "hydro",
    32: "geothermal",
    6: "power_plant",
    5: "grid",
}
PARALLEL_REQUESTS = 3
REFRESH_OPEN = 450      # project pages re-read per routine run: open ones...
REFRESH_CLOSED = 150    # ...and decided or withdrawn ones
RETRIES = 4


class Portal:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.calls = 0

    def get(self, url, **params):
        for attempt in range(RETRIES):
            try:
                self.calls += 1
                response = self.session.get(url, params=params, timeout=90)
                if response.status_code == 200:
                    return response.text
                if response.status_code == 404:
                    return None
                print(f"  {url} {params}: HTTP {response.status_code}, retrying")
            except requests.RequestException as error:
                print(f"  {url} {params}: {error}, retrying")
            time.sleep(10 * (attempt + 1))
        raise RuntimeError(f"MASE portal: {url} {params} failed {RETRIES} times")


def clean(text):
    """Visible text of an HTML fragment, whitespace collapsed."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_listing(page):
    """(project ids on a listing page, number of pages)."""
    ids = [int(found) for found in re.findall(r"/it-IT/Oggetti/Info/(\d+)\"", page)]
    pages = re.search(r"Pagina \d+ di (\d+)", page)
    return list(dict.fromkeys(ids)), int(pages.group(1)) if pages else 1


def parse_date(text):
    """"07/06/2022" -> "2022-06-07"; anything else -> None."""
    found = re.match(r"(\d{2})/(\d{2})/(\d{4})", (text or "").strip())
    return f"{found.group(3)}-{found.group(2)}-{found.group(1)}" if found else None


def field(page, label):
    found = re.search(r"<strong>\s*" + re.escape(label) + r"\s*:?\s*</strong>\s*:?(.*?)</p>", page, re.S)
    return clean(found.group(1)) if found else ""


def split_list(text):
    if not text or text.lower().startswith("nessun"):
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


# Labels of the procedure details kept (the rest, e.g. the official in
# charge and their phone number, is left out).
DETAIL_FIELDS = {
    "Data presentazione istanza": "submitted",
    "Data avvio consultazione pubblica": "consultation",
    "Data Decreto VIA": "decree_date",
    "Data Provvedimento": "decree_date",
    "Data provvedimento": "decree_date",
    "Data Determina": "decree_date",
    "Data Parere": "opinion_date",
    "Esito Decreto VIA": "outcome",
    "Esito Provvedimento": "outcome",
    "Esito provvedimento": "outcome",
    "Esito Determina": "outcome",
    "Esito": "outcome",
    "Esito Parere": "outcome",
    "Stato procedura": "status",
}


def parse_procedures(page):
    """The procedures of a project page, oldest first."""
    procedures = []
    for row in re.split(r'<tr class="trProcedura">', page)[1:]:
        cells = [clean(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row.split("</tr>")[0], re.S)]
        if len(cells) < 5:
            continue
        procedure = {"type": cells[0], "code": cells[2], "start": parse_date(cells[3]), "status": cells[4]}
        for label, value in re.findall(
                r'<tr class="datiAmministrativi"[^>]*>\s*<td>(.*?)</td>\s*<td[^>]*>(.*?)</td>', row, re.S):
            key = DETAIL_FIELDS.get(clean(label).rstrip(":").strip())
            value = clean(value)
            if key in ("submitted", "consultation", "decree_date", "opinion_date"):
                value = parse_date(value)
            if key and value and (key not in procedure or key == "status"):
                procedure[key] = value
        procedures.append(procedure)
    return sorted(procedures, key=lambda procedure: (procedure["start"] or "", procedure["code"]))


def parse_project(page, project_id):
    """A project page -> project dict, or None if the page has no project."""
    description = field(page, "Progetto")
    if not description and "trProcedura" not in page:
        return None
    return {
        "id": project_id,
        "name": field(page, "Opera") or description[:120],
        "description": description,
        "proponent": field(page, "Proponente"),
        "regions": split_list(field(page, "Regioni")),
        "provinces": split_list(field(page, "Province")),
        "municipalities": split_list(field(page, "Comuni")),
        "procedures": parse_procedures(page),
    }


# ---------- What the description says ----------

UNIT_MW = {"gw": 1000, "mw": 1, "mwp": 1, "mwe": 1, "mwac": 1, "mwdc": 1, "kw": 0.001, "kwp": 0.001}
NUMBER = r"(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?)"
UNIT = r"\s*(gw|mwp|mwe|mwac|mwdc|mw|kwp|kw)\b"


def to_number(text):
    """Italian or English number: "1.004,24", "19,830", "79.61", "1.200"."""
    if "." in text and "," in text:
        return float(text.replace(".", "").replace(",", "."))
    if "," in text:
        return float(text.replace(",", "."))
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        thousands = float(text.replace(".", ""))
        # "1.200 MW" is 1,200 MW, but "79.610 MWp" is 79.61 MW.
        return thousands if thousands <= 4000 else float(text)
    return float(text)


CAPACITY = re.compile(NUMBER + UNIT)
STORAGE_WORDS = re.compile(r"accumulo|storage|bess|b\.e\.s\.s|batteri")
UNIT_WORDS = re.compile(r"unitari|ciascun|cadaun|singol|\bogni\b")
TURBINE_WORDS = re.compile(r"generator|turbin|wtg|moduli|pannell")
TOTAL_WORDS = re.compile(r"complessiv|total|global|in immissione|rtn")
EXISTING_WORDS = re.compile(r"attual|esistent|dismess|dismission|preesist")
MAX_UNIT_MW = 25  # above this a "turbine rating" is the plant's total


def capacities(text):
    """
    (MW, storage MW) stated in a project description, None where it says
    nothing.  Each "<number> MW" is judged by the words before it: a
    turbine's or module's rating, an existing plant's, a storage system's
    or thermal MW are not the plant's capacity; a "complessiva"/"totale"
    figure beats a plain one; failing both, turbines x unit rating.
    """
    text = html.unescape(text or "").lower().replace("\xa0", " ")
    total = plain = storage = unit = None
    for match in CAPACITY.finditer(text):
        value = to_number(match.group(1)) * UNIT_MW[match.group(2)]
        before = text[max(0, match.start() - 60):match.start()]
        after = text[match.end():match.end() + 15]
        if STORAGE_WORDS.search(before[-55:]):
            storage = value if storage is None else storage
        elif re.search(r"termic", before[-35:]):
            continue
        elif value <= MAX_UNIT_MW and (
                UNIT_WORDS.search(before[-35:]) or UNIT_WORDS.match(after.strip(" ,"))
                or (TURBINE_WORDS.search(before[-40:]) and not TOTAL_WORDS.search(before[-40:]))):
            unit = value if unit is None else unit
        elif EXISTING_WORDS.search(before[-40:]):
            continue
        elif TOTAL_WORDS.search(before[-45:]):
            total = value if total is None else total
        else:
            plain = value if plain is None else plain
    mw = total if total is not None else plain
    if mw is None and unit is not None:
        turbines = re.findall(r"(\d+)\s+(?:nuovi\s+)?(?:aerogenerator|aereogenerator|turbin|wtg)", text)
        if turbines:
            mw = int(turbines[-1]) * unit
    return (round(mw, 3) if mw is not None else None), (round(storage, 3) if storage is not None else None)


def is_change(text):
    """A change to a project (variant, modification, extension of a permit)."""
    return bool(re.match(r"\W*(?:progetto di |istanza di |istanza per )?(?:modifica|variant|proroga|aggiornamento|"
                         r"rinnovo|integrazion|ottimizzazion)", (text or "").lower()))


QUOTED = r"[\"“”«»]\s*([^\"“”«»]{2,60}?)\s*[\"“”«»]"  # not ': Italian apostrophes


def short_name(name, description):
    """The project's own name where it has one ("denominato "Monte Croce""), else MASE's work name."""
    for text in (name, description):
        found = re.search(r"denominat[oai]\s+(?:\w+\s+){0,2}?" + QUOTED, text or "", re.I)
        if found:
            return found.group(1).strip()
    found = re.search(QUOTED, name or "")
    if found and len(found.group(1)) > 3:
        return found.group(1).strip()
    return name


def terna_codes(text):
    """Terna connection request codes quoted in the description."""
    return sorted(set(re.findall(r"(?:my\s*terna|codice\s+pratica)[^0-9]{0,30}(\d{9})", (text or "").lower())))


def technology(typology_key, text):
    """The technology: MASE's type, checked against the description."""
    text = (text or "").lower()
    wind = re.search(r"eolic|aerogenerator", text)
    solar = re.search(r"fotovoltai|agrivoltai|agrovoltai|solare", text)
    if typology_key in ("solar", "agrivoltaic") and wind and not solar:
        return "wind_offshore" if re.search(r"off-?shore", text) else "wind_onshore"
    if typology_key in ("wind_onshore", "wind_offshore") and solar and not wind:
        return "agrivoltaic" if re.search(r"agri|agro", text) else "solar"
    if typology_key in ("hydro", "power_plant") and "pompaggio" in text:
        return "pumped_hydro"
    if typology_key != "power_plant":
        return typology_key
    if re.search(r"data\s*cent", text):
        return "data_centre"
    if re.search(r"accumulo|bess|batteri", text) and not re.search(r"fotovoltaic|eolic|termoelettric", text):
        return "storage"
    if "idroelettric" in text:
        return "hydro"
    if re.search(r"termoelettric|ciclo combinato|turbogas|turbin\w* a gas|motori a gas|cogenera|centrale", text):
        return "thermal"
    return "other"


# ---------- Stage ----------

PRE_PROCEDURES = ("Valutazione preliminare", "Definizione contenuti SIA", "Definizione livello elaborati",
                  "Procedura art.6 comma 10")
POST_PROCEDURES = ("Verifica di Ottemperanza", "Verifica di Attuazione", "Varianti", "Proroga",
                   "Osservatorio", "Piano di Utilizzo")
WITHDRAWN = re.compile(r"archiviat|interrott|improcedibil|ritirat|rinunci|decadut|estint", re.I)
NEGATIVE = re.compile(r"negativ|non favorevol|diniego|rigett", re.I)
REFERRED = re.compile(r"da assoggettare|assoggettament|sottopo\w* a via|^necessita di ulteriori", re.I)
CLOSED = re.compile(r"^(conclusa|approvat|chiusa|-$)", re.I)


def procedure_kind(procedure):
    name = procedure["type"]
    if name.startswith(PRE_PROCEDURES):
        return "pre"
    if name.startswith(POST_PROCEDURES):
        return "post"
    return "main"


def procedure_result(procedure):
    """"open", "approved", "rejected", "referred" (to a full VIA) or "withdrawn"."""
    status = procedure.get("status", "")
    outcome = procedure.get("outcome", "")
    if WITHDRAWN.search(status) or WITHDRAWN.search(outcome):
        return "withdrawn"
    if not CLOSED.search(status) and not outcome:
        return "open"
    if NEGATIVE.search(outcome) or NEGATIVE.search(status):
        return "rejected"
    if REFERRED.search(outcome) or REFERRED.search(status):
        return "referred"
    if CLOSED.search(status) or outcome:
        return "approved"
    return "open"


def project_stage(procedures):
    """
    The project's stage from its procedures:
      screening   only a preliminary assessment or scoping so far
      assessment  its latest assessment (VIA, screening for VIA, single
                  environmental permit) is under way
      approved    the latest assessment ended in favour (or not needing
                  a VIA), or only compliance checks are on file
      rejected    the latest assessment ended against it
      withdrawn   the latest assessment was archived or withdrawn
    A screening that refers the project to a full VIA counts as under way
    until that VIA is filed (then the VIA decides).
    """
    main = [procedure for procedure in procedures if procedure_kind(procedure) == "main"]
    if not main:
        if any(procedure_kind(procedure) == "post" for procedure in procedures):
            return "approved"
        return "screening" if procedures else "assessment"
    result = procedure_result(main[-1])
    if result == "open" or result == "referred":
        return "assessment"
    return result


# ---------- Output ----------

PROCEDURE_KEYS = ["type", "code", "start", "status", "submitted", "consultation", "decree_date", "outcome"]
PROJECT_KEYS = ["id", "label", "name", "description", "proponent", "typology_id", "technology", "change", "mw", "storage_mw",
                "regions", "provinces", "municipalities", "terna_codes", "stage", "current", "start", "decision",
                "procedures", "checked"]


def enrich(project):
    """Add the fields read from the description and procedures."""
    text = f"{project['description']} {project['name']}"
    project["technology"] = technology(TYPOLOGIES[project["typology_id"]], text)
    project["label"] = short_name(project["name"], project["description"])
    project["change"] = is_change(project["description"])
    project["mw"], project["storage_mw"] = capacities(project["description"])
    project["terna_codes"] = terna_codes(project["description"])
    procedures = project["procedures"]
    project["stage"] = project_stage(procedures)
    # The procedure the stage comes from: the latest assessment, else the latest one.
    main = [index for index, procedure in enumerate(procedures) if procedure_kind(procedure) == "main"]
    project["current"] = main[-1] if main else (len(procedures) - 1 if procedures else None)
    project["start"] = min((procedure["start"] for procedure in procedures if procedure["start"]), default=None)
    decisions = [procedure.get("decree_date") for procedure in procedures
                 if procedure_kind(procedure) == "main" and procedure.get("decree_date")]
    project["decision"] = max(decisions, default=None)
    return project


def build_output(projects, listed_at):
    rows = []
    for project in sorted(projects.values(), key=lambda project: project["id"], reverse=True):
        row = [project.get(key) for key in PROJECT_KEYS]
        row[PROJECT_KEYS.index("procedures")] = [[procedure.get(key) for key in PROCEDURE_KEYS]
                                                 for procedure in project["procedures"]]
        rows.append(row)
    return {
        "source": "MASE, Valutazioni e Autorizzazioni Ambientali (va.mite.gov.it): state-level VIA projects",
        "description": __doc__.split("\n\n")[0].strip(),
        "updated": listed_at,
        "columns": PROJECT_KEYS,
        "procedure_columns": PROCEDURE_KEYS,
        "projects": rows,
    }


def load_existing():
    if not os.path.exists(OUTPUT_PATH):
        return {}
    with open(OUTPUT_PATH, encoding="utf-8") as handle:
        payload = json.load(handle)
    projects = {}
    for row in payload.get("projects", []):
        project = dict(zip(payload["columns"], row))
        project["procedures"] = [{key: value for key, value in zip(payload["procedure_columns"], procedure)
                                  if value is not None} for procedure in project["procedures"]]
        projects[project["id"]] = project
    return projects


# ---------- Crawl ----------

def list_projects(portal, typology, known, full):
    """{project id: typology} from a type's listing pages, newest first."""
    found = {}
    page_number, pages = 1, 1
    while page_number <= pages:
        page = portal.get(LIST_URL, tipologiaID=typology, testo="", pagina=page_number)
        ids, pages = parse_listing(page or "")
        found.update({project_id: typology for project_id in ids})
        if not full and ids and all(project_id in known for project_id in ids):
            break
        page_number += 1
    return found


def fetch_project(portal, project_id, typology):
    page = portal.get(INFO_URL.format(id=project_id))
    project = parse_project(page or "", project_id)
    if project is None:
        return None
    project["typology_id"] = typology
    project["checked"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return enrich(project)


def to_refresh(projects, new_ids, open_budget, closed_budget):
    """Project ids to (re)read: new ones, then the ones checked longest ago."""
    chosen = list(new_ids)
    old = sorted((project for project_id, project in projects.items() if project_id not in new_ids),
                 key=lambda project: project.get("checked") or "")
    chosen += [project["id"] for project in old if project["stage"] in ("screening", "assessment")][:open_budget]
    chosen += [project["id"] for project in old if project["stage"] not in ("screening", "assessment")][:closed_budget]
    return chosen


def main():
    parser = argparse.ArgumentParser(description="Fetch Italy's power projects under state VIA from MASE.")
    parser.add_argument("--full", action="store_true", help="Read every listing page (and drop projects no longer listed)")
    parser.add_argument("--refresh-open", type=int, default=REFRESH_OPEN, help="Open projects to re-read")
    parser.add_argument("--refresh-closed", type=int, default=REFRESH_CLOSED, help="Decided projects to re-read")
    parser.add_argument("--limit-pages", type=int, help="Testing: read at most this many listing pages per type")
    parser.add_argument("--rederive", action="store_true",
                        help="Re-derive technology, MW and stage of the saved projects without reading the portal")
    args = parser.parse_args()

    portal = Portal()
    projects = load_existing()
    if args.rederive:
        for project in projects.values():
            enrich(project)
        write_output(projects, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), portal)
        return
    full = args.full or not projects

    listed = {}
    for typology, key in TYPOLOGIES.items():
        if args.limit_pages:
            found, page_number, pages = {}, 1, 1
            while page_number <= min(pages, args.limit_pages):
                ids, pages = parse_listing(portal.get(LIST_URL, tipologiaID=typology, testo="", pagina=page_number) or "")
                found.update({project_id: typology for project_id in ids})
                page_number += 1
        else:
            found = list_projects(portal, typology, projects, full)
        print(f"  {key}: {len(found)} listed")
        listed.update(found)

    if full and not args.limit_pages:
        dropped = [project_id for project_id in projects if project_id not in listed]
        for project_id in dropped:
            del projects[project_id]
        print(f"  {len(listed)} projects listed, {len(dropped)} no longer listed")

    typology_of = {project_id: project.get("typology_id") for project_id, project in projects.items()}
    typology_of.update(listed)
    new_ids = [project_id for project_id in listed if project_id not in projects]
    refresh = to_refresh(projects, new_ids, args.refresh_open, args.refresh_closed) if not full else list(listed)
    refresh = [project_id for project_id in refresh if typology_of.get(project_id) in TYPOLOGIES]
    print(f"Reading {len(refresh)} project pages ({len(new_ids)} new)")

    def read(project_id):
        return project_id, fetch_project(portal, project_id, typology_of[project_id])

    with ThreadPoolExecutor(PARALLEL_REQUESTS) as pool:
        for count, (project_id, project) in enumerate(pool.map(read, refresh), 1):
            if project:
                projects[project_id] = project
            if count % 250 == 0:
                print(f"  {count}/{len(refresh)} project pages read")

    write_output(projects, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), portal)


def write_output(projects, listed_at, portal):
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(build_output(projects, listed_at), handle, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUTPUT_PATH} ({os.path.getsize(OUTPUT_PATH) // 1024:,} KB): {len(projects)} projects, "
          f"{portal.calls} MASE requests")


if __name__ == "__main__":
    main()
