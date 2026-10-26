"""
Fetch the standalone battery storage plants (BESS) authorised by the
Ministry of the Environment and Energy Security (MASE) and write
app/data/mase_storage.json.

Standalone storage of 10 MW and more outside industrial sites gets a
single authorisation ("autorizzazione unica") from MASE itself (DL 7/2002,
D.Lgs. 387/2003, since 2025 D.Lgs. 190/2024), with no environmental impact
assessment, so these plants are not in the VIA portal (etl/fetch_mase.py).
MASE publishes each decree on the page "Autorizzazioni uniche - Produzione
energia elettrica" (www.mase.gov.it/portale/autorizzazioni-uniche): a
listing, 20 decrees a page, newest first, and a page per decree with its
subject in one sentence and the PDF.  The same page carries the decrees of
thermal plants over 300 MWt and of pumped hydro.  Decrees to mid-2023 link
straight to the PDF: their subject is the request ("istanza") the decree
recites, read from the PDF's text.

The subject names the company, the plant ("denominato ..."), its MW (and
MWh when stated), the municipality and province.  Decrees that change an
authorisation (a transfer to another company, a variant, an extension, a
correction) quote the decree they change; they are attached to it, so each
plant is one row: its authorisation and what came after.  Partial
transfers hand the connection works to Terna or the distributor and leave
the plant with its holder.  A position comes from the municipalities
(etl/municipalities.json), as in fetch_mase.py.

Routine runs read the listing until a page brings no new decree, then the
new decrees' pages; --full reads every listing page and decree page again;
--rederive rebuilds the plants from the saved decrees without the network.
"""

import argparse
import html
import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from fetch_mase import QUOTED, Gazetteer, place_key, to_number

BASE_URL = "https://www.mase.gov.it"
LIST_URL = BASE_URL + "/portale/autorizzazioni-uniche"
PORTLET = "com_liferay_asset_publisher_web_portlet_AssetPublisherPortlet_INSTANCE_qmdh"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "data", "mase_storage.json")
USER_AGENT = "it-power-dashboard/1.0 (+https://github.com/aragn/it-power-dashboard)"
PARALLEL_REQUESTS = 3
RETRIES = 4

MONTHS = {name: number for number, name in enumerate(
    ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
     "settembre", "ottobre", "novembre", "dicembre"], 1)}

# Province code -> (name, region).  The Sardinian provinces of 2016 (SU) and
# of 2025 (CI, MD, OG, OT, GS) are both here, as decrees use either.
PROVINCES = {code: (name, region) for region, entries in {
    "Abruzzo": "AQ L'Aquila, CH Chieti, PE Pescara, TE Teramo",
    "Basilicata": "MT Matera, PZ Potenza",
    "Calabria": "CS Cosenza, CZ Catanzaro, KR Crotone, RC Reggio Calabria, VV Vibo Valentia",
    "Campania": "AV Avellino, BN Benevento, CE Caserta, NA Napoli, SA Salerno",
    "Emilia-Romagna": "BO Bologna, FC Forlì-Cesena, FE Ferrara, MO Modena, PC Piacenza, PR Parma, RA Ravenna, "
                      "RE Reggio Emilia, RN Rimini",
    "Friuli-Venezia Giulia": "GO Gorizia, PN Pordenone, TS Trieste, UD Udine",
    "Lazio": "FR Frosinone, LT Latina, RI Rieti, RM Roma, VT Viterbo",
    "Liguria": "GE Genova, IM Imperia, SP La Spezia, SV Savona",
    "Lombardia": "BG Bergamo, BS Brescia, CO Como, CR Cremona, LC Lecco, LO Lodi, MB Monza e Brianza, MI Milano, "
                 "MN Mantova, PV Pavia, SO Sondrio, VA Varese",
    "Marche": "AN Ancona, AP Ascoli Piceno, FM Fermo, MC Macerata, PU Pesaro e Urbino",
    "Molise": "CB Campobasso, IS Isernia",
    "Piemonte": "AL Alessandria, AT Asti, BI Biella, CN Cuneo, NO Novara, TO Torino, VB Verbano-Cusio-Ossola, VC Vercelli",
    "Puglia": "BA Bari, BR Brindisi, BT Barletta-Andria-Trani, FG Foggia, LE Lecce, TA Taranto",
    "Sardegna": "CA Cagliari, NU Nuoro, OR Oristano, SS Sassari, SU Sud Sardegna, CI Sulcis Iglesiente, "
                "MD Medio Campidano, OG Ogliastra, OT Gallura Nord-Est Sardegna",
    "Sicilia": "AG Agrigento, CL Caltanissetta, CT Catania, EN Enna, ME Messina, PA Palermo, RG Ragusa, SR Siracusa, "
               "TP Trapani",
    "Toscana": "AR Arezzo, FI Firenze, GR Grosseto, LI Livorno, LU Lucca, MS Massa-Carrara, PI Pisa, PO Prato, "
               "PT Pistoia, SI Siena",
    "Trentino-Alto Adige": "BZ Bolzano, TN Trento",
    "Umbria": "PG Perugia, TR Terni",
    "Valle d'Aosta": "AO Aosta",
    "Veneto": "BL Belluno, PD Padova, RO Rovigo, TV Treviso, VE Venezia, VI Vicenza, VR Verona",
}.items() for code, name in (entry.split(" ", 1) for entry in entries.split(", "))}


class Portal:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.calls = 0

    def get(self, url, binary=False, **params):
        for attempt in range(RETRIES):
            try:
                self.calls += 1
                response = self.session.get(url, params=params, timeout=90)
                if response.status_code == 200:
                    response.encoding = "utf-8"
                    return response.content if binary else response.text
                if response.status_code == 404:
                    return None
                print(f"  {url} {params}: HTTP {response.status_code}, retrying")
            except requests.RequestException as error:
                print(f"  {url} {params}: {error}, retrying")
            time.sleep(10 * (attempt + 1))
        raise RuntimeError(f"MASE: {url} {params} failed {RETRIES} times")


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def italian_date(text):
    """"22 ottobre 2026", "1° luglio 2025", "dell'8 ottobre 2026", "10.06.2021" -> ISO date; else None."""
    found = re.search(r"(\d{1,2})\s*[°º]?\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", (text or "").lower())
    if found:
        return f"{found.group(3)}-{MONTHS[found.group(2)]:02d}-{int(found.group(1)):02d}"
    found = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", text or "")
    return f"{found.group(3)}-{int(found.group(2)):02d}-{int(found.group(1)):02d}" if found else None


# ---------- Crawl ----------

def parse_listing(page):
    """([(published, url, title)] of a listing page, number of pages)."""
    rows = re.findall(r'field-content">\s*([^<]+?)\s*</div>\s*</div>\s*<div class="views-field views-field-field-link-'
                      r'esterno-energia">\s*<div class="field-content"><a href="([^"]+)">(.*?)</a>', page, re.S)
    pages = re.search(r"Pagina \d+ di (\d+)", page)
    return ([(italian_date(published), BASE_URL + url if url.startswith("/") else url, clean(title))
             for published, url, title in rows], int(pages.group(1)) if pages else 1)


def list_page(portal, number):
    return parse_listing(portal.get(LIST_URL, **{"p_p_id": PORTLET, f"_{PORTLET}_delta": 20,
                                                  f"_{PORTLET}_cur": number}) or "")


def parse_decree(page):
    """A decree page -> (subject, PDF url): the text under the title, before its documents."""
    found = re.search(r'<h2 class="page-header">.*?</h2>\s*<div class="content">(.*?)<h3', page, re.S)
    if not found:
        return None, None
    content = found.group(1)
    pdf = re.search(r'href="(/portale/documents/[^"]+)"', content)
    subject = clean(re.sub(r"<ul.*?</ul>", " ", content, flags=re.S))
    half = len(subject) // 2
    if half > 40 and subject[:half].strip() == subject[half:].strip():  # pasted twice on some pages
        subject = subject[:half].strip()
    return subject or None, BASE_URL + pdf.group(1) if pdf else None


def pdf_subject(data):
    """What a decree PDF decides: its "Art. 1" after "DECRETA", without the companies' addresses
    and tax codes; else the request it recites ("con cui la società X ha presentato istanza ...").
    When Art. 1 gives no MW, the request (or the first "potenza ... MW") follows it."""
    from pypdf import PdfReader
    text = " ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\b([BCDFGLMNPRSTVZ]) ([a-zà-ù]{2,})", r"\1\2", text)  # "C omune", "P roduzione"
    text = re.sub(r"(\d)\s*/\s*(\d)", r"\1/\2", text)  # "N°55/ 01/2022"
    article = re.search(r"D\s?E\s?C\s?R\s?E\s?T\s?A\s*(?:Art\.?\s*1\b\.?\s*(?:[A-Z][a-z]+\s+)?(?:1\.\s+)?)?"
                        r"(.{40,1500}?)(?=\s+2\.\s|\s+Art\.?\s*2\b)", text)
    request = re.search(r"con cui\s+(.{10,900}?ha presentato.{0,700}?)(?=\s+e ha allegato|;|\s+(?:CONSIDERAT|PRESO ATTO|VIST))",
                        text)
    parts = [found.group(1) for found in (article, request) if found]
    if not parts:
        return None
    if article and capacity(parts[0])[0] is None:
        # A storage system's, not "potenza superiore a 300 MW termici" (the legal threshold every
        # preamble quotes) nor the power station's own MW.
        stated = next((found for found in re.finditer(r"potenza[^.;]{0,80}?\d+(?:[.,]\d+)?\s*(?:MW|MVA)\b[^.;]{0,40}", text)
                       if not re.search(r"termic|superior|lorda", found.group(0), re.I)
                       and BATTERY.search(text[max(0, found.start() - 150):found.end()])), None)
        parts = parts[:1] + ([parts[1]] if len(parts) > 1 and capacity(parts[1])[0] is not None else
                             [stated.group(0)] if stated else [])
    else:
        parts = parts[:1]
    subject = " — ".join(parts)
    subject = re.sub(r"^\d+\s+Art\.?\s*1\b\.?\s*(?:[A-Z][a-z]+\s+)?(?:1\.\s+)?", "", subject)  # a page number first
    subject = re.sub(r",?\s*(?:avente|con)\s+sede.{0,300}?\d{11}(?:\s*,?\s*iscritt.{0,120}?\d{5,}(?=\D))?\s*,?", " ", subject)
    subject = re.sub(r"\s*\(di seguito:?\s*[“\"]?il P\s?roponente[”\"]?\)", "", subject, flags=re.I)
    return re.sub(r"\s+", " ", subject).strip()


def fetch_decree(portal, published, url, title):
    """A listed decree with its subject, and the request its PDF recites when
    the subject leaves out a battery's MW.  The oldest entries (to 2021, thermal
    plants) link to the former Ministry's site, now offline: their title is all there is."""
    decree = {"url": url, "title": title, "published": published, "subject": None, "request": None, "pdf": None}
    try:
        if not url.startswith(BASE_URL):
            decree["subject"] = re.split(r"\s+[-–]\s+", title, maxsplit=1)[-1]
            return decree
        if "/documents/" in url:
            decree["pdf"] = url
        else:
            decree["subject"], decree["pdf"] = parse_decree(portal.get(url) or "")
        subject = decree["subject"]
        if decree["pdf"] and (not subject or decree_technology(subject) == "battery" and capacity(subject)[0] is None):
            data = portal.get(decree["pdf"], binary=True)
            request = pdf_subject(data) if data else None
            if subject:
                decree["request"] = request
            else:
                decree["subject"] = request
    except RuntimeError as error:  # left without a subject, read again next run
        print(f"  {error}")
    return decree


# ---------- What a decree says ----------

def decree_key(number, date=None):
    """"55/05/2023" -> "55/5/2023"; "55-21-2021" -> "55/21/2021"; "311" of 2026 -> "311/2026"."""
    parts = [str(int(part)) for part in re.split(r"[/.-]", number) if part.isdigit()]
    if len(parts) == 3 and len(parts[2]) == 2:
        parts[2] = "20" + parts[2]
    if len(parts) == 1 and date:
        parts.append(date[:4])
    return "/".join(parts)


TITLE_NUMBER = re.compile(r"\bn\s*[.°º]?\s*°?\s*(\d+\.\d+\.\d{4}|\d+(?:\s*[/-]\s*\d+){0,2})\s*[/_ ]?\s*(VL|VR|PR|RT)?\b", re.I)
REFERENCE = re.compile(
    r"(?:decret[oi]|d\.\s?d\.)\s+(?:direttorial[ei]\s+|interministeriale\s+|di\s+(?:voltura|rettifica|autorizzazione unica)\s+)*"
    r"(?:n\s*[.°º]?\s*°?\s*)?(\d+(?:[/-]\d+){0,2})(?:\s*[/_ ]?\s*(?:VL|VR|PR|RT)\b)?"
    r"(?:\s*,?\s*(?:del|dell['’]|dello)\s*(\d{1,2}\s*[°º]?\s*[a-z]+\s+\d{4}|\d{1,2}\.\d{1,2}\.\d{4}))?", re.I)
COMPANY = (r"((?:(?:[A-Z0-9À-Ý][\w&.’'\-]*|&)\s+){0,7}?(?:[A-Za-z0-9][\w&.’'\-]*\s*)?(?:S\.?\s?R\.?\s?L\.?|S\.?\s?P\.?\s?A\.?|"
           r"S\.?A\.?S\.?|GMBH|S\.?R\.?L\.?S\.?)(?![\w]))")
BATTERY = re.compile(r"accumulo\s+elettrochimico|\bbess\b|batteri|immagazzinamento di energia|accumulo (?:di|dell['’]\s?)energia"
                     r"|(?:impianto|sistema|sistemi|progetto) di accumulo|\bstorage\b", re.I)
NOT_BATTERY = re.compile(r"accumulo termico|thermal energy storage|\btes\b", re.I)
PUMPED = re.compile(r"pompaggio", re.I)
THERMAL = re.compile(r"centrale|ciclo combinato|termoelettric|turbin", re.I)


def decree_number(title):
    """(key, suffix, date) from a listing title: "Decreto Direttoriale n. 55/13/2023/VL del 7 luglio 2025"."""
    date = italian_date(title)
    found = TITLE_NUMBER.search(title)
    if not found:
        return None, None, date
    return decree_key(found.group(1).replace(" ", ""), date), (found.group(2) or "").upper() or None, date


def references(text, own):
    """Keys of the decrees a subject quotes, its own left out."""
    keys = []
    for number, date in REFERENCE.findall(text or ""):
        if "/" not in number and "-" not in number and not date:
            continue  # "n. 7" of a law, a bare number without its date
        key = decree_key(number, italian_date(date) if date else None)
        if key != own and key not in keys:
            keys.append(key)
    return keys


def decree_kind(title, subject, suffix):
    text = f"{title} {subject or ''}".lower()
    if "accesso alle aree" in text:
        return "access"
    if suffix == "VL" or "voltura" in text:
        return "partial_transfer" if "parzial" in text else "transfer"
    if suffix == "RT" or re.search(r"\brettific|\bintegrazione (?:del|dell)", text):
        return "correction"
    if suffix == "PR" or "proroga" in text:
        return "extension"
    if re.search(r"diniego|rigetto|improcedibil|decadenz|revoca", text):
        return "other"
    if suffix == "VR" or re.search(r"modifica dell['’]autorizzazione|variant", text):
        return "variant"
    return "authorisation"


def decree_technology(text):
    if BATTERY.search(text or "") and not NOT_BATTERY.search(text or ""):
        return "battery"
    if PUMPED.search(text or ""):
        return "pumped_hydro"
    if THERMAL.search(text or ""):
        return "thermal"
    return None


def company_after(text, words):
    """The first company name after one of `words` ("alla", "a favore della", ...)."""
    found = re.search(r"\b(?:" + words + r")\s+(?:(?:societ[àa]|soc\.)\s+)?(?:à\s+)?" + COMPANY, text or "", re.I)
    if not found:
        return None
    name = re.sub(r"\s+", " ", found.group(1)).strip(" ,.")
    return re.sub(r"^(?:la|alla|della|societ[àa])\s+", "", name, flags=re.I)


def capacity(text):
    """(MW, MWh) stated in a subject."""
    text = (text or "").replace("\xa0", " ")
    mw = re.search(r"(\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+(?:[.,]\d+)?)\s*(?:MW|MVA)(?:p|e|ac)?(?!\w)", text, re.I)
    mwh = re.search(r"(\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+(?:[.,]\d+)?)\s*MWh\b", text, re.I)
    return (to_number(mw.group(1)) if mw else None), (to_number(mwh.group(1)) if mwh else None)


PERMIT_CODE = re.compile(r"\bBESS-\d{4}-\d{2}[- ]\d{5,8}\b")


def plant_name(text):
    """The plant's name ("denominato "Romanazzi BESS""), without a Permitting code and company tacked on."""
    found = re.search(r"denominat[oai]\s+(?:\w+\s+){0,2}?" + QUOTED, text or "", re.I) or \
        re.search(r"[Pp]rogetto\s+" + QUOTED, text or "") or \
        re.search(r"denominat[oa]\s+([A-Z][\w\- ]{2,40}?)(?=[,.;]|\s+(?:avente|di|della|nel|sito|e)\b)", text or "")
    if not found:
        return None
    name = found.group(1).strip()
    code = PERMIT_CODE.search(name)
    if code:
        name = name[:code.start()].strip(" -–") or name
    return name


_gazetteer = None
WORD = r"[A-ZÀ-Ý][\w'’À-ÿ\-]*"
PLACE = WORD + r"(?:\s+(?:[a-zà-ÿ'’]{1,5}\s+)?" + WORD + r")*"  # "Acquaviva delle Fonti"
MUNICIPALITY_LIST = re.compile(r"Comun[ei]\s+di\s+(.+?)(?=\s+e\s+(?:delle|della|del|dell|relative)\b|\s+(?:per|da|di|della|"
                               r"avente|con|ricadent\w*|in|nonch\w*|al|alla)\s+[a-z]|[.;:]|$)")
# MASE's misspellings, and power stations named after a hamlet: the municipality.
SPELLINGS = {"cancelloarnone": "Cancello ed Arnone", "giulianoincampania": "Giugliano in Campania", "temoli": "Termoli",
             "fiumesanto": "Sassari", "fusina": "Venezia"}


def gazetteer():
    global _gazetteer
    if _gazetteer is None:
        _gazetteer = Gazetteer()
    return _gazetteer


def known_place(words, code):
    """The longest tail of `words` that is a municipality: "Toppa di Gallo nel Comune di Bisaccia" -> "Bisaccia"."""
    words = words.split()
    for start in range(len(words)):
        candidate = " ".join(words[start:])
        candidate = SPELLINGS.get(place_key(candidate), candidate)
        if not re.match(r"(?:Comun|Provinc|Localit|Region|Centrale|Citt)", candidate) and \
                gazetteer().locate(candidate, [PROVINCES.get(code, ("", ""))[0]] if code else []):
            return candidate
    return None


def places(text):
    """[(municipality, province code or None)] named in a subject: "nei Comuni di Macerata Campania,
    Marcianise e Santa Maria Capua Vetere (CE)" (a code covers the names before it), else any "Name (XX)",
    else the place of a power station ("Centrale ... di Torino")."""
    found = []
    for span in MUNICIPALITY_LIST.findall(text or ""):
        items = [item.strip() for item in re.split(r",\s*|\s+e\s+(?=[A-ZÀ-Ý])", span) if item.strip()]
        code = None
        for item in reversed(items):
            match = re.search(r"\(([A-Z]{2})\)", item)
            code = match.group(1) if match else code
            name = known_place(re.sub(r"\s*\(.*$", "", item), code)
            if name:
                found.insert(0, (name, code))
    if not found:
        for name, code in re.findall(r"(" + PLACE + r")\s*\(([A-Z]{2})\)", text or ""):
            name = known_place(name, code)
            if name:
                found.append((name, code))
    if not found:
        for name in re.findall(r"(?:centrale|impianto)[^.;]{0,60}?\bdi (" + PLACE + ")", text or "", re.I):
            name = known_place(name, None)
            if name:
                found.append((name, None))
                break
    if not found:  # "denominato Codrongianos BESS 2": the first capitalised words that are a municipality
        for phrase in re.findall(PLACE, text or ""):
            words = phrase.split()[:8]
            for end in range(len(words), 0, -1):
                name = known_place(" ".join(words[:end]), None)
                if name and len(name) > 3:
                    found.append((name, None))
                    break
            if found:
                break
    return list(dict.fromkeys(found))


def province_code(name):
    """A municipality's province code from the gazetteer's province, for subjects that give none."""
    candidates = gazetteer().places.get(place_key(name)) or []
    keys = {candidate[2] for candidate in candidates if candidate[2]}
    if len(keys) != 1:
        return None
    key = keys.pop()
    return next((code for code, (province, _) in PROVINCES.items()
                 if place_key(province) and (place_key(province) in key or key in place_key(province))), None)


def position(found):
    points = [gazetteer().locate(name, [PROVINCES.get(code, ("", ""))[0]] if code else []) for name, code in found]
    points = [point for point in points if point]
    if not points:
        return None, None
    return (round(sum(point[0] for point in points) / len(points), 4),
            round(sum(point[1] for point in points) / len(points), 4))


def describe(decree):
    """Add what the title and subject say to a decree."""
    title, subject = decree["title"], decree.get("subject") or ""
    decree["key"], suffix, decree["date"] = decree_number(title)
    decree["date"] = decree["date"] or decree.get("published")
    if decree["key"] and suffix:
        decree["key"] += "/" + suffix
    decree["kind"] = decree_kind(title, subject, suffix)
    decree["technology"] = decree_technology(f"{title} {subject}")
    # A transfer "55/13/2023/VL" quotes "55/13/2023", the authorisation it transfers.
    decree["refers"] = references(subject, decree["key"])
    return decree


# ---------- Plants ----------

PLANT_KEYS = ["id", "name", "holder", "applicant", "mw", "mwh", "municipalities", "provinces", "region", "lat", "lon",
              "at_plant", "permit_code", "authorised", "decree", "url", "pdf", "subject", "events"]
EVENT_KEYS = ["date", "kind", "decree", "url", "subject"]
DECREE_KEYS = ["key", "date", "published", "kind", "technology", "title", "subject", "request", "url", "pdf"]


def build_plants(decrees):
    """One plant per battery authorisation, with the decrees that changed it."""
    plants, alias = {}, {}
    for decree in sorted(decrees, key=lambda item: (item["date"] or "", item["key"] or "")):
        subject = decree.get("subject") or ""
        targets = list(dict.fromkeys(alias[key] for key in decree["refers"] if key in alias))
        if decree["kind"] in ("authorisation", "variant") and decree["technology"] == "battery" and (
                decree["kind"] == "authorisation" or not targets):
            request = decree.get("request") or ""
            found = places(subject) or places(request)
            found = [(name, code or province_code(name)) for name, code in found]
            lat, lon = position(found)
            codes = list(dict.fromkeys(code for _, code in found if code))
            regions = list(dict.fromkeys(PROVINCES[code][1] for code in codes if code in PROVINCES))
            mw, mwh = capacity(subject)
            if mw is None:
                mw, mwh = capacity(request)[0], mwh or capacity(request)[1]
            applicant = company_after(subject, r"autorizzazione (?:del|della|alla|dell['’])|alla|della|la") or \
                company_after(f"{subject} {decree['title']}", r"termoelettrica|centrale|societ[àa]")
            code = PERMIT_CODE.search(subject)
            plant = {
                "id": decree["key"], "name": plant_name(subject), "holder": applicant, "applicant": applicant,
                "mw": mw, "mwh": mwh, "municipalities": [name for name, _ in found], "provinces": codes,
                "region": regions[0] if len(regions) == 1 else (" / ".join(regions) or None), "lat": lat, "lon": lon,
                "at_plant": bool(re.search(r"\bcentral[ei]\b", subject, re.I)),
                "permit_code": code.group(0) if code else None, "authorised": decree["date"],
                "decree": decree["key"], "url": decree["url"], "pdf": decree.get("pdf"), "subject": subject, "events": [],
            }
            plants[decree["key"]] = plant
            alias[decree["key"]] = decree["key"]
            continue
        for target in targets:
            plant = plants[target]
            plant["events"].append({"date": decree["date"], "kind": decree["kind"], "decree": decree["key"],
                                    "url": decree["url"], "subject": subject})
            if decree["kind"] == "transfer":
                plant["holder"] = company_after(subject, r"a favore del(?:la)?|alla") or plant["holder"]
            elif decree["kind"] in ("variant", "correction"):
                mw, mwh = capacity(subject)
                plant["mw"], plant["mwh"] = mw or plant["mw"], mwh or plant["mwh"]
                holder = company_after(subject, r"con la")
                if holder and "sostituisce la titolare" in subject:
                    plant["holder"] = holder
        if decree["key"]:
            for target in targets:
                alias[decree["key"]] = target
    return list(plants.values())


def build_output(decrees, listed_at):
    plants = sorted(build_plants(decrees), key=lambda plant: (plant["authorised"] or "", plant["id"]), reverse=True)
    rows = []
    for plant in plants:
        row = [plant.get(key) for key in PLANT_KEYS]
        row[PLANT_KEYS.index("events")] = [[event.get(key) for key in EVENT_KEYS] for event in plant["events"]]
        rows.append(row)
    return {
        "source": "MASE, Autorizzazioni uniche - Produzione energia elettrica (www.mase.gov.it/portale/autorizzazioni-uniche)",
        "description": __doc__.split("\n\n")[0].strip(),
        "updated": listed_at,
        "columns": PLANT_KEYS,
        "event_columns": EVENT_KEYS,
        "plants": rows,
        "decree_columns": DECREE_KEYS,
        "decrees": [[decree.get(key) for key in DECREE_KEYS]
                    for decree in sorted(decrees, key=lambda item: (item["date"] or "", item["key"] or ""), reverse=True)],
    }


def load_existing():
    if not os.path.exists(OUTPUT_PATH):
        return {}
    with open(OUTPUT_PATH, encoding="utf-8") as handle:
        payload = json.load(handle)
    return {row[payload["decree_columns"].index("url")]: dict(zip(payload["decree_columns"], row))
            for row in payload.get("decrees", [])}


def write_output(decrees, listed_at, portal):
    for decree in decrees:
        describe(decree)
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    payload = build_output(decrees, listed_at)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUTPUT_PATH} ({os.path.getsize(OUTPUT_PATH) // 1024:,} KB): {len(payload['plants'])} battery plants "
          f"from {len(decrees)} decrees, {portal.calls} MASE requests")


def main():
    parser = argparse.ArgumentParser(description="Fetch the battery storage plants authorised by MASE.")
    parser.add_argument("--full", action="store_true", help="Read every listing page and decree page again")
    parser.add_argument("--rederive", action="store_true", help="Rebuild the plants from the saved decrees")
    args = parser.parse_args()

    portal = Portal()
    known = load_existing()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    if args.rederive:
        write_output(list(known.values()), now, portal)
        return
    full = args.full or not known

    listed, page_number, pages = [], 1, 1
    while page_number <= pages:
        rows, pages = list_page(portal, page_number)
        listed += rows
        if not full and rows and all(url in known and known[url].get("subject") for _, url, _ in rows):
            break
        page_number += 1
    to_read = [row for row in listed if full or row[1] not in known or not known[row[1]].get("subject")]
    print(f"{len(listed)} decrees listed, reading {len(to_read)}")

    with ThreadPoolExecutor(PARALLEL_REQUESTS) as pool:
        for decree in pool.map(lambda row: fetch_decree(portal, *row), to_read):
            known[decree["url"]] = decree
    if full:  # drop decrees no longer listed
        urls = {url for _, url, _ in listed}
        known = {url: decree for url, decree in known.items() if url in urls}
    write_output(list(known.values()), now, portal)


if __name__ == "__main__":
    main()
