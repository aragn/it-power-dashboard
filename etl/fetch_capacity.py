"""
Installed generation capacity of Italy per year, source and bidding zone,
written to app/data/capacity.json.

Sources (no public source has commissioning dates, so this is yearly):
  - Terna /generation/v2.0/generation-plants: net efficient power (MW) at
    the end of each year, per region/province and source (solar, wind,
    hydro, geothermal, thermal incl. bioenergy, stand-alone storage).
    Regions are mapped to bidding zones.  Solar includes small/rooftop
    plants, like the solar generation series.  Hydro includes pumped
    storage.
  - ENTSO-E 14.1.A (A68/A33): installed capacity per production type for
    Italy at the start of each year (for comparison; zone queries return
    nothing for Italy).
  - ENTSO-E 14.1.B (A71/A33): units of 100 MW or more, with their zone;
    used for pumped storage (B10), which Terna does not split out.

Terna's year-end Y and ENTSO-E's 1 January Y+1 describe the same moment,
so year Y pairs Terna Y with ENTSO-E Y+1.  Hydro = Terna hydro - pumped.

Credentials: TERNA_KEY / TERNA_SECRET and ENTSOE_API_KEY.
"""

import os
from collections import defaultdict
from datetime import date

import requests

import compact
from entsoe_api import request_entsoe
from fetch_terna import Client

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "data", "capacity.json")

IT_DOMAIN = "10YIT-GRTN-----B"
FIRST_YEAR = 2019
MAX_UNIT_MW = 5000

ZONE_EIC = {
    "10Y1001A1001A73I": "NORD", "10Y1001A1001A70O": "CNOR", "10Y1001A1001A71M": "CSUD",
    "10Y1001A1001A788": "SUD", "10Y1001C--00096J": "CALA", "10Y1001A1001A75E": "SICI",
    "10Y1001A1001A74G": "SARD",
}
ZONES = ["NORD", "CNOR", "CSUD", "SUD", "CALA", "SICI", "SARD"]

REGION_ZONE = {
    "valle d'aosta": "NORD", "piemonte": "NORD", "liguria": "NORD", "lombardia": "NORD",
    "trentino alto adige": "NORD", "veneto": "NORD", "friuli venezia giulia": "NORD",
    "emilia romagna": "NORD", "toscana": "CNOR", "marche": "CNOR", "lazio": "CSUD",
    "abruzzo": "CSUD", "campania": "CSUD", "umbria": "CSUD", "molise": "SUD", "puglia": "SUD",
    "basilicata": "SUD", "calabria": "CALA", "sicilia": "SICI", "sardegna": "SARD",
}

TERNA_SOURCES = {
    "Termoelettrico": "thermal", "Geotermoelettrico": "geothermal", "Idrico": "hydro",
    "Eolico": "wind", "Fotovoltaico": "solar", "Accumulo stand alone": "battery",
    # Part of thermal in Terna's plant statistics; listed apart in some tables.
    "Bioenergie": "thermal",
}


def nearest(by_year, year):
    """The entry of by_year closest to year (the later one on a tie), or {}."""
    if not by_year:
        return {}
    return by_year[min(by_year, key=lambda y: (abs(y - year), -y))]


def region_zone(region):
    key = str(region or "").strip().lower().replace("-", " ")
    return REGION_ZONE.get(key)


def terna_year(client, year):
    """{zone: {source: MW}} or None when Terna has not published the year."""
    payload = client.get("/generation/v2.0/generation-plants", {"year": year, "capacityType": "Netta"})
    records = payload.get("generation_plants") or []
    if not records:
        return None
    zones = defaultdict(lambda: defaultdict(float))
    for record in records:
        zone = region_zone(record.get("region"))
        source = TERNA_SOURCES.get(record.get("source"))
        if zone is None or source is None:
            raise ValueError(f"Unmapped Terna record: {record}")
        zones[zone][source] += float(record.get("efficient_power_MW") or 0)
    return {zone: {s: round(v, 1) for s, v in sources.items()} for zone, sources in zones.items()}


def entsoe_types(token, year):
    """{psrType: MW} of ENTSO-E 14.1.A for Italy at 1 January of year."""
    root = request_entsoe(token, {"documentType": "A68", "processType": "A33", "in_Domain": IT_DOMAIN,
                                  "periodStart": f"{year}01010000", "periodEnd": f"{year}01020000"})
    if root is None:
        return None
    caps = {}
    for ts in root.findall(".//{*}TimeSeries"):
        psr = ts.find(".//{*}psrType").text
        caps[psr] = float(ts.find(".//{*}quantity").text)
    return caps or None


def entsoe_pumped_by_zone(token, year):
    """{zone: MW} of pumped-storage units (B10) in ENTSO-E 14.1.B."""
    root = request_entsoe(token, {"documentType": "A71", "processType": "A33", "in_Domain": IT_DOMAIN,
                                  "periodStart": f"{year}01010000", "periodEnd": f"{year}01020000"})
    if root is None:
        return None
    zones = defaultdict(float)
    for ts in root.findall(".//{*}TimeSeries"):
        if ts.find(".//{*}psrType").text != "B10":
            continue
        zone = ZONE_EIC.get(ts.find("{*}inBiddingZone_Domain.mRID").text)
        quantity = float(ts.find(".//{*}quantity").text)
        # A year not yet published has come back with absurd values
        # (978 GW in NORD for 2027); no single unit is anywhere near 5 GW.
        if zone is None or quantity > MAX_UNIT_MW:
            continue
        zones[zone] += quantity
    return dict(zones) or None


def build(terna, entsoe, units):
    """
    {year: {"zones": {zone: {layer: MW}}, "entsoe": {psr: MW}}}.  Pumped per
    zone = ENTSO-E 14.1.A national B10 split by the 14.1.B unit shares.
    """
    years = {}
    for year, zones in sorted(terna.items()):
        moment = entsoe.get(year + 1) or {}
        # ENTSO-E may lack a year; the pumped fleet barely changes, so the
        # nearest year stands in rather than dropping pumped hydro to zero.
        national_pumped = moment.get("B10") or nearest(entsoe, year + 1).get("B10")
        shares = nearest(units, year + 1)
        share_total = sum(shares.values())
        out = {}
        for zone in ZONES:
            sources = dict(zones.get(zone, {}))
            pumped = 0.0
            if national_pumped and share_total:
                pumped = national_pumped * shares.get(zone, 0) / share_total
            sources["pumped"] = round(pumped, 1)
            sources["hydro"] = round(max(sources.get("hydro", 0.0) - pumped, 0.0), 1)
            out[zone] = sources
        years[str(year)] = {"zones": out, "entsoe": moment, "entsoe_date": f"{year + 1}-01-01"}
    return years


def main():
    terna_key, terna_secret = os.environ.get("TERNA_KEY"), os.environ.get("TERNA_SECRET")
    entsoe_token = os.environ.get("ENTSOE_API_KEY")
    if not (terna_key and terna_secret and entsoe_token):
        raise RuntimeError("TERNA_KEY, TERNA_SECRET and ENTSOE_API_KEY must be set.")

    client = Client(terna_key, terna_secret)
    last_year = date.today().year
    terna, entsoe, units = {}, {}, {}
    for year in range(FIRST_YEAR, last_year + 1):
        found = terna_year(client, year)
        if found:
            terna[year] = found
        print(f"Terna {year}: " + ("no data" if not found else
              str({s: round(sum(z.get(s, 0) for z in found.values())) for s in TERNA_SOURCES.values()})))
    for year in range(FIRST_YEAR, last_year + 2):
        try:
            caps = entsoe_types(entsoe_token, year)
            pumped = entsoe_pumped_by_zone(entsoe_token, year)
        except requests.HTTPError as error:
            # A year ENTSO-E cannot serve is only missing from the comparison.
            print(f"ENTSO-E {year}: {error}")
            continue
        if caps:
            entsoe[year] = caps
        # Unit data only for years ENTSO-E has published (14.1.A present).
        if caps and pumped:
            units[year] = pumped
        print(f"ENTSO-E {year}: {'no data' if not caps else round(sum(caps.values()))} MW, pumped units by zone {pumped}")

    if not terna:
        raise RuntimeError("No Terna capacity downloaded; not writing output.")

    compact.dump({
        "source": "Terna generation-plants (net, year-end); ENTSO-E 14.1.A/14.1.B",
        "description": (
            "Installed net capacity (MW) per year-end, bidding zone and source. "
            "Terna for all sources except pumped storage (ENTSO-E 14.1.A, split "
            "by zone with the 14.1.B units); hydro excludes pumped storage. "
            "entsoe: ENTSO-E 14.1.A per production type for Italy at 1 January "
            "of the following year (the same moment), for comparison."
        ),
        "years": build(terna, entsoe, units),
    }, OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
