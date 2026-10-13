"""
The Italian bidding zones on the ENTSO-E Transparency Platform, and where
each zone's files go: app/data/zones/<ZONE>/<name>, in the same layout as
Italy's file of that name (app/data/<name>), so the page reads a zone's
file as it reads Italy's.

The ENTSO-E scripts take --zone: a zone's code, or "all" for the seven
zones one after the other; without it they fetch Italy (the control area).
"""

import os

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "data")

IT_DOMAIN = "10YIT-GRTN-----B"

ZONE_DOMAINS = {
    "NORD": "10Y1001A1001A73I",
    "CNOR": "10Y1001A1001A70O",
    "CSUD": "10Y1001A1001A71M",
    "SUD": "10Y1001A1001A788",
    "CALA": "10Y1001C--00096J",
    "SICI": "10Y1001A1001A75E",
    "SARD": "10Y1001A1001A74G",
}

ZONE_NAMES = {
    "NORD": "North", "CNOR": "Centre-North", "CSUD": "Centre-South", "SUD": "South",
    "CALA": "Calabria", "SICI": "Sicily", "SARD": "Sardinia",
}


def zone_path(zone, name):
    return os.path.join(DATA_DIR, "zones", zone, name)


def targets(zone, italy_path, name):
    """(label, EIC code, output path) of each area a run fetches: Italy
    without a zone, else the zone or ("all") every zone."""
    if not zone:
        return [("IT", IT_DOMAIN, italy_path)]
    codes = list(ZONE_DOMAINS) if zone.lower() == "all" else [zone.upper()]
    unknown = [code for code in codes if code not in ZONE_DOMAINS]
    if unknown:
        raise ValueError(f"Unknown bidding zone(s): {unknown}; expected one of {list(ZONE_DOMAINS)} or 'all'.")
    return [(code, ZONE_DOMAINS[code], zone_path(code, name)) for code in codes]


def area_label(label, domain):
    return f"{'IT' if label == 'IT' else 'IT-' + ZONE_NAMES[label]} ({domain})"


def run_areas(areas, fetch):
    """fetch(label, domain, path) for each area.  With several zones, one
    that fails (e.g. ENTSO-E has nothing for it yet) is logged and the others
    go on, so their files are still written; the run fails only when every
    area failed."""
    failed = []
    for label, domain, path in areas:
        try:
            fetch(label, domain, path)
        except Exception as error:
            if len(areas) == 1:
                raise
            print(f"  {label} failed, left for a later run: {error}")
            failed.append(label)
    if failed and len(failed) == len(areas):
        raise RuntimeError(f"Every area failed: {failed}")
    return failed
