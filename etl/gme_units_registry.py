"""
The ENTSO-E unit lists, per GME unit code (UP_...): production type,
installed MW, connection voltage, bidding zone and municipality.

  14.1.B   installed capacity per production unit (A71 / A33), units of
           100 MW or more
  A95/B11  production and generation units master data, per bidding zone:
           also the location as an ISTAT code, RRR + PPPCCC (region,
           province and municipality); for some units the municipality is
           the province's capital

Both name the unit as "IM_<plant>UP_<unit>" (registeredResource.name), the
part from "UP_" on being the GME code.  The nominal power is sometimes in kW
(368000 for a 368 MW unit) and the production type of some gas and coal
units is given as B09, geothermal (see fetch_outages.repair): the 14.1.B
type wins, and B09 outside Tuscany is dropped.
"""

import re
from collections import Counter, defaultdict

PSR_SOURCES = {
    "B01": "bioenergy", "B02": "coal", "B03": "coal", "B04": "gas", "B05": "coal", "B06": "oil",
    "B09": "geothermal", "B10": "pumped_hydro", "B11": "hydro_ror", "B12": "hydro_reservoir",
    "B15": "other_res", "B16": "solar", "B17": "waste", "B18": "wind_offshore", "B19": "wind_onshore",
    "B20": "other", "B25": "battery",
}
TUSCANY = "009"
MAX_UNIT_MW = 5000


def unit_code(record):
    match = re.search(r"(UP_.*)$", record.get("registeredResource.name") or "")
    return match.group(1) if match else None


def megawatts(value):
    try:
        mw = float(value)
    except (TypeError, ValueError):
        return None
    if mw > MAX_UNIT_MW:
        mw /= 1000
    return mw or None


def entsoe_units(registries):
    """{GME code: {"source", "psr", "capacity_mw", "voltage_kv", "zone_eic", "location", "eic"}}."""
    records = defaultdict(list)
    entsoe = (registries or {}).get("entsoe", {})
    for kind in ("installed_14_1_B", "master_A95"):
        for record in entsoe.get(kind, {}).values():
            code = unit_code(record)
            if code:
                records[code].append((kind, record))
    units = {}
    for code, items in records.items():
        installed = [r for kind, r in items if kind == "installed_14_1_B"]
        master = sorted((r for kind, r in items if kind == "master_A95"),
                        key=lambda r: r.get("implementation_DateAndOrTime.date") or "")
        location = next((r.get("registeredResource.location.name") for r in reversed(master)
                         if len(r.get("registeredResource.location.name") or "") == 9), None)
        psrs = [r.get("MktPSRType.psrType") for r in installed] or \
               [r.get("MktPSRType.psrType") for r in master]
        psrs = [p for p in psrs if p and (p != "B09" or (location or "").startswith(TUSCANY))]
        psr = Counter(psrs).most_common(1)[0][0] if psrs else None
        capacity = None
        for record in reversed(installed + master):
            capacity = (megawatts(record.get("installed_MW"))
                        or megawatts(record.get("MktPSRType.nominalIP_PowerSystemResources.nominalP"))
                        or megawatts(record.get("MktPSRType.GeneratingUnit_PowerSystemResources.nominalP")))
            if capacity:
                break
        voltage = next((r.get("MktPSRType.production_PowerSystemResources.highVoltageLimit")
                        for r in reversed(installed + master)
                        if r.get("MktPSRType.production_PowerSystemResources.highVoltageLimit")), None)
        eic = next((r.get("registeredResource.mRID") for r in installed + master if r.get("registeredResource.mRID")), None)
        units[code] = {
            "source": PSR_SOURCES.get(psr), "psr": psr, "capacity_mw": capacity,
            "voltage_kv": float(voltage) if voltage else None, "location": location, "eic": eic,
        }
    return units
