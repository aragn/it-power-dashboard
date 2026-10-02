"""
Fetch the grid-connection queue of Italy's transmission and distribution
networks and write app/data/connections.json:

  Terna         Econnextion (dati.terna.it Download Center): the requests
                to connect to the transmission grid (RTN), per region and
                per province, by kind (renewables "FER", storage
                "Accumuli", consumers "Utenti di Consumo"), source and
                stage, with MW and number of requests.  Terna publishes
                the latest month only, so every month read is kept as a
                national and regional snapshot ("history").
  e-distribuzione  Aree Critiche (the map on e-distribuzione.it): per
                quarter, each province's criticality level (1 very low to
                4 high, 0 where e-distribuzione is not the distributor),
                the power of its valid connection quotes ("preventivi"),
                its primary substations' rating and minimum power; for the
                latest quarter also the critical municipalities, the
                AT/MT sections (primary substation transformers) with
                reverse flow in the year before, and each section's
                saturation (green, yellow, orange, red).

Terna stages, in order: "STMG da accettare" (connection solution offered,
not yet accepted), "STMG accettate", "Progetti in valutazione" (under
authorisation), "Progetti con nulla osta" (authorised), "STMD/Contratti"
(detailed solution or connection contract).
"""

import argparse
import json
import os
import re
import time
from datetime import date, datetime, timezone

import requests

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "data", "connections.json")
USER_AGENT = "it-power-dashboard/1.0 (+https://github.com/aragn/it-power-dashboard)"
RETRIES = 4

# ---------- Terna Econnextion ----------

TERNA_URL = "https://dati.terna.it/api/sitecore/dati/downloadcenter/recordsv2"
TERNA_KINDS = {"FER": "renewables", "Accumuli": "storage", "Utenti di Consumo": "consumers"}
TERNA_VIEWS = {"Region": "regions", "Province": "provinces"}
TERNA_STAGES = ["STMG da accettare", "STMG accettate", "Progetti in valutazione",
                "Progetti con nulla osta", "STMD/Contratti"]

# ---------- e-distribuzione Aree Critiche ----------

ED_TOKEN_URL = "https://www.e-distribuzione.it/content/e-distribuzione/it/homepage/_jcr_content.tokenservice.html"
ED_API_URL = "https://xs-portale-pubblico-api-mngmt.de-c1.eu1.cloudhub.io/aui/v1/AreeCritiche/"
ED_SERVED_URL = "https://xs-portale-pubblico-api-mngmt.de-c1.eu1.cloudhub.io/v1/precar/determina40/comuniServiti"
# The service's own region codes: not ISTAT's (Liguria is 03, Lombardia 04,
# Marche 10, Umbria 11), and not those of the links on its own page, which
# open Liguria for "Lombardia".  Valle d'Aosta and Trentino-Alto Adige come
# back without values (e-distribuzione does not serve them).
ED_REGIONS = {"01": "Piemonte", "02": "Valle d'Aosta", "03": "Liguria", "04": "Lombardia",
              "05": "Trentino-Alto Adige", "06": "Veneto", "07": "Friuli-Venezia Giulia",
              "08": "Emilia-Romagna", "09": "Toscana", "10": "Marche", "11": "Umbria", "12": "Lazio",
              "13": "Abruzzo", "14": "Molise", "15": "Campania", "16": "Puglia", "17": "Basilicata",
              "18": "Calabria", "19": "Sicilia", "20": "Sardegna"}
ED_HISTORY_START = (2019, 3)
SATURATION = {"VERDE": 1, "GIALLO": 2, "ARANCIONE": 3, "ROSSO": 4}


class Client:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.calls = 0

    def post(self, url, payload, raw=False, retries=RETRIES):
        for attempt in range(retries):
            try:
                self.calls += 1
                response = self.session.post(
                    url, data=payload if raw else json.dumps(payload), timeout=90,
                    headers={"Content-Type": "application/json; charset=utf-8"})
                if response.status_code == 200:
                    return response.json()
                print(f"  {url}: HTTP {response.status_code}, retrying")
            except (requests.RequestException, ValueError) as error:
                print(f"  {url}: {error}, retrying")
            time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"{url} failed {retries} times")


def number(text):
    """e-distribuzione's Italian numbers: "1.004,2404" -> 1004.2404."""
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)):
        return float(text)
    return float(str(text).replace(".", "").replace(",", "."))


def title(text):
    """"BARLETTA-ANDRIA-TRANI" -> "Barletta-Andria-Trani", "L'AQUILA" -> "L'Aquila"."""
    words = re.split(r"([\s\-'/])", (text or "").lower())
    small = {"d", "di", "del", "della", "e", "nel", "in", "sul", "dei", "delle"}
    return "".join(word if word in small and index else "CP" if word == "cp" else word[:1].upper() + word[1:]
                   for index, word in enumerate(words))


# ---------- Terna ----------

def terna_date(text):
    """"/Date(1788127200000+0200)/" -> "2026-08-31"."""
    found = re.search(r"\((\d+)([+-]\d{4})?\)", text or "")
    if not found:
        return None
    offset = int(found.group(2) or "+0000")
    seconds = int(found.group(1)) / 1000 + (offset // 100) * 3600 + (offset % 100) * 60
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d")


def fetch_terna(client):
    """{view: rows} with rows [region, province?, kind, source, stage, MW, requests], and the date."""
    tables, as_of = {}, None
    for view, name in TERNA_VIEWS.items():
        rows = []
        for dataset, kind in TERNA_KINDS.items():
            payload = client.post(TERNA_URL, {
                "filterDataset": dataset, "filterViewBy": view, "orderByColumn": "Potenza (MW)",
                "orderByDir": "desc", "pageSize": "50000", "pageIndex": "0", "db": "enti"})
            columns = list(payload.get("Columns") or {})
            for values in payload.get("Data") or []:
                record = dict(zip(columns, values))
                rows.append([
                    title(record.get("Regione")),
                    title(record.get("Provincia")) if view == "Province" else None,
                    kind,
                    (record.get("Fonte") or "").strip().capitalize(),
                    record.get("Stato Connessione"),
                    round(float(record.get("Potenza (MW)") or 0), 3),
                    int(record.get("Numero Pratiche") or 0),
                ])
            as_of = terna_date(payload.get("LastDate")) or as_of
            print(f"  Terna {dataset} by {view}: {len(rows)} rows so far, data of {as_of}")
        tables[name] = [row if view == "Province" else row[:1] + row[2:] for row in rows]
    return tables, as_of


def terna_snapshot(regions):
    """National and regional totals of a month: {"national": [[kind, source, stage, MW, n]], "regions": [...]}"""
    national, regional = {}, {}
    for region, kind, source, stage, mw, count in regions:
        key = (kind, source, stage)
        total = national.setdefault(key, [0.0, 0])
        total[0] += mw
        total[1] += count
        key = (region, kind, stage)
        total = regional.setdefault(key, [0.0, 0])
        total[0] += mw
        total[1] += count
    return {
        "national": [list(key) + [round(mw, 1), count] for key, (mw, count) in sorted(national.items())],
        "regions": [list(key) + [round(mw, 1), count] for key, (mw, count) in sorted(regional.items())],
    }


# ---------- e-distribuzione ----------

class EDistribuzione:
    """The Aree Critiche services: each call needs a token for its own parameters."""

    def __init__(self, client):
        self.client = client
        self.requested = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        self.requested = self.requested[:-2] + ":" + self.requested[-2:]

    def call(self, url, retries=RETRIES, **params):
        message = "DataRichiesta:" + self.requested + "".join(f"{key}:{value}" for key, value in params.items())
        token = self.client.post(ED_TOKEN_URL, {"data": message, "service": "areecritiche"})
        if str(token.get("responseCode")) != "0":
            raise RuntimeError(f"e-distribuzione token refused: {token}")
        body = {"Token": token["responseData"]["token"], "DataRichiesta": self.requested, **params}
        answer = self.client.post(url, body, retries=retries)
        if str(answer.get("CodiceEsito")) != "0":
            raise RuntimeError(f"e-distribuzione {url} {params}: {answer.get('DescrizioneEsito')}")
        return answer

    def provinces(self, period, region):
        answer = self.call(ED_API_URL + "livelloProvince.NTH", Periodo=period, Regione=region)
        return (answer.get("Province") or {}).get("Provincia") or []

    def municipalities(self, period, province):
        answer = self.call(ED_API_URL + "livelloComuni.NTH", Periodo=period, Provincia=province)
        return (answer.get("Comuni") or {}).get("Comune") or []

    def reverse_flows(self, period, province):
        answer = self.call(ED_API_URL + "livelloCP.NTH", Periodo=period, Provincia=province)
        return (answer.get("CabinePrimarie") or {}).get("CabinaPrimaria") or []

    def served(self, province):
        # Fails (HTTP 500) for the largest provinces, e.g. Torino: few retries.
        return self.call(ED_SERVED_URL, retries=2, Provincia=province).get("ElencoComuni") or []


def quarters(first, last):
    """Periods "MM/YYYY" (quarter ends) from `first` to `last` ((year, month) pairs)."""
    year, month = first
    while (year, month) <= last:
        yield f"{month:02d}/{year}"
        month += 3
        if month > 12:
            year, month = year + 1, month - 12


def latest_possible_period(today):
    """The quarter e-distribuzione's current map shows (see its setDataTrimestri)."""
    month = (today.month - 1) // 3 * 3      # months 0-2 -> 0, 3-5 -> 3, ...
    year = today.year
    period_month = {0: 9, 3: 12, 6: 3, 9: 6}[month]
    if month < 6:
        year -= 1
    # The next quarter is sometimes published early; try it too.
    next_month = period_month + 3
    return (year + 1, next_month - 12) if next_month > 12 else (year, next_month)


def period_key(period):
    month, year = period.split("/")
    return f"{year}-{month}"


def province_rows(ed, period):
    rows = []
    for code, region in ED_REGIONS.items():
        for item in ed.provinces(period, code):
            if item.get("PotPreventivi") is None and item.get("PotNomCP") is None:
                continue  # not e-distribuzione's (Valle d'Aosta, Trentino-Alto Adige, AEM Tirano)
            rows.append([region, item.get("CodiceProvincia"), title(item.get("Descrizione")),
                         item.get("LivelloCriticita"), number(item.get("PotPreventivi")),
                         number(item.get("PotNomCP")), number(item.get("PotMinimoRegionale"))])
    return rows


def has_values(rows):
    return any(row[4] is not None for row in rows)


def fetch_detail(ed, period, provinces):
    """Critical municipalities, reverse-flow sections and section saturation of the latest quarter."""
    municipalities, sections, incomplete = [], {}, []
    for region, code, name, *_ in provinces:
        for item in ed.municipalities(period, code):
            municipalities.append([code, item.get("CodiceComune"), title(item.get("Descrizione")),
                                   item.get("LivelloCriticitaComune"), number(item.get("PotPreventivi")),
                                   number(item.get("PotVirtComune")), number(item.get("PotMinimoRegionale"))])
        for item in ed.reverse_flows(period, code):
            key = (item.get("Codice"), item.get("IdTrasf"))
            section = sections.setdefault(key, {"province": code, "cp": item.get("Codice"),
                                                "name": title(item.get("Descrizione")), "transformer": item.get("IdTrasf"),
                                                "section": item.get("Montante"), "saturation": None, "municipalities": set()})
            section["reverse_1"] = item.get("InvFlusso1") == "S"
            section["reverse_5"] = item.get("InvFlusso5") == "S"
        try:
            towns = ed.served(code)
        except RuntimeError as error:
            print(f"  e-distribuzione {name}: no section saturation ({error})")
            incomplete.append(code)
            towns = []
        for town in towns:
            for item in town.get("ElencoTrasformatori") or []:
                key = (item.get("CodCabinaPrimaria"), item.get("CodTrasformatore"))
                section = sections.setdefault(key, {"province": code, "cp": item.get("CodCabinaPrimaria"),
                                                    "name": title(item.get("NomeCabinaPrimaria")),
                                                    "transformer": item.get("CodTrasformatore"),
                                                    "section": item.get("IdSezioneATMT"), "saturation": None,
                                                    "municipalities": set()})
                section["saturation"] = SATURATION.get((item.get("LivSaturTraformatore") or "").upper(),
                                                       section["saturation"])
                section["municipalities"].add(title(town.get("Descrizione")))
        print(f"  e-distribuzione {name}: {len(municipalities)} municipalities, {len(sections)} sections so far")
    section_rows = [[section["province"], section["cp"], section["name"], section["transformer"], section["section"],
                     section["saturation"], section.get("reverse_1", False), section.get("reverse_5", False),
                     sorted(section["municipalities"])]
                    for section in sorted(sections.values(), key=lambda section: (section["province"], section["name"],
                                                                                   section["transformer"] or ""))]
    return municipalities, section_rows, incomplete


# ---------- Output ----------

def load_existing():
    if not os.path.exists(OUTPUT_PATH):
        return {}
    with open(OUTPUT_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def main():
    parser = argparse.ArgumentParser(description="Fetch Italy's grid-connection queue (Terna, e-distribuzione).")
    parser.add_argument("--full-history", action="store_true", help="Re-read every e-distribuzione quarter")
    parser.add_argument("--skip-detail", action="store_true", help="Testing: skip e-distribuzione's per-province detail")
    args = parser.parse_args()

    client = Client()
    existing = load_existing()

    terna_tables, as_of = fetch_terna(client)
    history = (existing.get("terna") or {}).get("history") or {}
    if as_of:
        history[as_of] = terna_snapshot(terna_tables["regions"])
    terna = {
        "as_of": as_of,
        "stages": TERNA_STAGES,
        "region_columns": ["region", "kind", "source", "stage", "mw", "requests"],
        "province_columns": ["region", "province", "kind", "source", "stage", "mw", "requests"],
        "regions": terna_tables["regions"],
        "provinces": terna_tables["provinces"],
        "history_columns": {"national": ["kind", "source", "stage", "mw", "requests"],
                            "regions": ["region", "kind", "stage", "mw", "requests"]},
        "history": dict(sorted(history.items())),
    }

    ed = EDistribuzione(client)
    old = existing.get("edistribuzione") or {}
    by_period = {} if args.full_history else dict(old.get("history") or {})
    latest = latest_possible_period(date.today())
    for period in quarters(ED_HISTORY_START, latest):
        key = period_key(period)
        if key in by_period and key < max(by_period, default=""):
            continue  # an older quarter already read: e-distribuzione does not revise them
        rows = province_rows(ed, period)
        if has_values(rows):
            by_period[key] = rows
            print(f"  e-distribuzione {period}: {len(rows)} provinces")
        else:
            print(f"  e-distribuzione {period}: not published")
    latest_key = max(by_period)
    latest_period = f"{latest_key[5:]}/{latest_key[:4]}"
    if args.skip_detail and old.get("period") == latest_key:
        municipalities, sections, incomplete = old.get("municipalities", []), old.get("sections", []), old.get("incomplete", [])
    elif args.skip_detail:
        municipalities, sections, incomplete = [], [], []
    else:
        municipalities, sections, incomplete = fetch_detail(ed, latest_period, by_period[latest_key])
    edistribuzione = {
        "period": latest_key,
        "province_columns": ["region", "code", "province", "level", "quotes_mw", "substations_mva", "minimum_mw"],
        "history": dict(sorted(by_period.items())),
        "municipality_columns": ["province", "code", "name", "level", "quotes_mw", "virtual_mw", "minimum_mw"],
        "municipalities": municipalities,
        "section_columns": ["province", "substation", "name", "transformer", "section", "saturation",
                            "reverse_1", "reverse_5", "municipalities"],
        "sections": sections,
        "incomplete": incomplete,  # provinces whose section saturation the service did not give
    }

    output = {
        "source": "Terna (Econnextion, dati.terna.it); e-distribuzione (Aree Critiche)",
        "description": __doc__.split("\n\n")[0].strip(),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "terna": terna,
        "edistribuzione": edistribuzione,
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUTPUT_PATH} ({os.path.getsize(OUTPUT_PATH) // 1024:,} KB), {client.calls} requests; "
          f"Terna data of {as_of}, e-distribuzione quarter {latest_period}")


if __name__ == "__main__":
    main()
