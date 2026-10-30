"""
The capacity market for the Storage tab: the battery and pumped hydro
units' capacity contracts (Terna's auctions, matched to their GME codes)
and the strike price of each day, for the tab's capacity market switch
(the premium received, the variable payment returned to Terna).

app/data/capacity_market_storage.json (PRIVATE: GME codes, a price from
GME's gas data):

  units   code -> {"contracts": [[first year, last year, CDP MW, premium
          EUR/MW/year, basis]], "terna": names, "note"}; basis "listed":
          a unit Terna names as new capacity (its asta madre reports,
          app/policy/capacity_market.json), its contract running the
          auction's length from its delivery year; "existing": a unit Terna
          never names (existing capacity is reported per participant and
          area only), taken as committed for the year at its MW x the year's
          derating when its owner held at least that much existing capacity
          in its area (all its units there together).  The match:
          etl/capacity_market_units.json (private research).
  daily   date -> code -> [premium EUR, variable payment EUR at the MGP
          price, more for the balancing markets EUR, [[period, MGP EUR,
          balancing EUR], ...] of the quarter-hours with a payment]: each
          day of the storage files (etl/build_storage.py) from the unit's
          first day on file to its last.
  strike  {"start", "values": [[before 06:00, from 06:00], ...]}: EUR/MWh
          per calendar day; the gas price is the gas day's (06:00 to 06:00),
          so the hours before 06:00 take the day before's.
  inputs  the gas price (SAP, EUR/MWh by gas day) and the EUA price
          (EUR/t, the day before's) of each day.

The premium: CDP x premium / the days of the year.  The variable payment
(corrispettivo variabile, Disciplina article 51, Terna's DTF n. 1 table 1),
estimated per quarter-hour on the CDP of the day's contracts: the part
accepted upward in MSD and MB (net, up to the CDP) at the larger of the
zonal MGP price and its average accepted price, the rest at the MGP price,
each MWh paying what that price passes the strike by:

  CDP x max(MGP - strike, 0) / 4
  + upward balancing MW x (max(MGP, its price) - strike, 0 - max(MGP - strike, 0)) / 4

Left out: Terna's monthly nominations (a holder spreads its CDP over its
units, and can lower it where nothing was accepted), the offers made but
not accepted above the strike, the energy limit of storage (a quarter of
the day's largest energy counted as offered in the ISP), the mFRR
platform's marginal price, exemptions and penalties.

The strike (prezzo di esercizio, ARERA 199/2024/R/eel article 3, for the
delivery years 2025-2028; 263/2026/R/eel: 303 Smc/MWh from 2028) is the
standard variable cost of an open-cycle gas turbine:

  gas        HEAT_RATE Smc/MWh x (the System Average Price of the gas day,
             EUR/Smc at 0.0381 GJ/Smc + CCR + transport and system charges
             + excise + I_GN 0.05 EUR/Smc)
  emissions  0.7162 tCO2/MWh x the EUA price of the day before
  disposal   0.006 EUR/MWh
  dispatch   the larger of 3 EUR/MWh and 2% of the three above
  other      5 EUR/MWh

The System Average Price: GME's weighted average price of the gas day's
trades (gas_prices.json "imbalance", GAS_ImbalancePrice); the EUA price:
the EEX primary auction last held on or before the day before (eua.json;
ARERA averages ICE Endex's daily futures and the auctions, 424/2020).
Charges besides the gas price (GAS_CHARGES) are an estimate: CCR about
0.034 EUR/Smc (TIVG, Q2 2025), Snam's daily exit capacity, variable and
metering charges about 0.025, the gas system charges for power plants
about 0.007, excise 0.0005.  Checked on ARERA's annual averages under the
2023-2024 rules (340 Smc/MWh, other 15 EUR/MWh; ARERA 302/2025: 265 EUR/MWh
in 2023, 231 in 2024): the same inputs give 263 and 231 with 0.047 EUR/Smc
of charges, the transport then counted only in part (and in "other").
Terna tells each holder the strike it used a month after; it is not
published.  A day without a gas or EUA price takes the last day's (3.8).

  build_capacity_market_storage.py
"""

import gzip
import json
import os
import sys
from calendar import isleap
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compact  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "app", "data")
MARKET_PATH = os.path.join(ROOT, "app", "policy", "capacity_market.json")
MATCH_PATH = os.path.join(HERE, "capacity_market_units.json")
STORAGE_DIR = os.path.join(DATA, "storage")
STORAGE_PATH = os.path.join(STORAGE_DIR, "index.json")
ZONAL_PATH = os.path.join(DATA, "zonal_prices.json")
GAS_PATH = os.path.join(DATA, "gas_prices.json")
EUA_PATH = os.path.join(DATA, "eua.json")
OUTPUT_PATH = os.path.join(DATA, "capacity_market_storage.json")

FIRST_DAY = date(2025, 1, 1)             # the 199/2024 rules (delivery 2025 on)
MWH_PER_SMC = 0.0381 / 3.6               # higher heating value 0.0381 GJ/Smc
HEAT_RATE = {2025: 320, 2026: 320, 2027: 320}   # Smc/MWh; 303 from 2028 (263/2026)
LATER_HEAT_RATE = 303
I_GN = 0.05                              # EUR/Smc, for running less regularly
GAS_CHARGES = 0.066                      # EUR/Smc, estimated (see above)
EMISSION_RATE = 0.7162                   # tCO2/MWh
DISPOSAL = 0.006                         # EUR/MWh (0.6 euro cents)
DISPATCH_MIN, DISPATCH_SHARE = 3.0, 0.02
OTHER = 5.0                              # EUR/MWh, other charges and risks
GAS_DAY_START = "06:00"
# The storage files' record (etl/build_storage.py): MSD and MB net EUR and net MW.
ST_MSD_EUR, ST_MB_EUR, ST_MSD_MW, ST_MB_MW = 11, 12, 16, 17


def load(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def daily(series):
    """date -> value of a compact daily series."""
    return {date.fromisoformat(row["date"]): row[series["field"]] for row in compact.decode_series(series)}


def eua_by_day(auctions, first, last):
    """date -> the price of the last auction held on or before the day before."""
    prices = sorted((date.fromisoformat(item["date"]), item["price"]) for item in auctions if item.get("price"))
    out, index, price = {}, 0, None
    day = first
    while day <= last:
        while index < len(prices) and prices[index][0] <= day - timedelta(days=1):
            price = prices[index][1]
            index += 1
        out[day] = price
        day += timedelta(days=1)
    return out


def strike(sap, eua, year):
    """EUR/MWh from a gas price (EUR/MWh) and an EUA price (EUR/t)."""
    rate = HEAT_RATE.get(year, LATER_HEAT_RATE if year > max(HEAT_RATE) else HEAT_RATE[min(HEAT_RATE)])
    gas = rate * (sap * MWH_PER_SMC + GAS_CHARGES + I_GN)
    base = gas + EMISSION_RATE * eua + DISPOSAL
    return base + max(DISPATCH_MIN, DISPATCH_SHARE * base) + OTHER


def strike_series(gas, auctions, last):
    """{"start", "values": [[before 06:00, from 06:00]]} and the inputs, from FIRST_DAY to last."""
    sap = daily(gas["series"]["imbalance"])
    eua = eua_by_day(auctions, FIRST_DAY, last)
    values, inputs = [], []
    known_sap = None
    sap_of = {}
    day = FIRST_DAY - timedelta(days=1)
    while day <= last:
        # A gas day without a price: the last day's (199/2024 comma 3.8).
        known_sap = sap.get(day, known_sap)
        sap_of[day] = known_sap
        day += timedelta(days=1)
    day = FIRST_DAY
    known_eua = None
    while day <= last:
        known_eua = eua.get(day) or known_eua
        early, late = sap_of.get(day - timedelta(days=1)), sap_of.get(day)
        if known_eua is None or late is None:
            values.append(None)
        else:
            values.append([round(strike(early if early is not None else late, known_eua, day.year), 2),
                           round(strike(late, known_eua, day.year), 2)])
        inputs.append([late, known_eua])
        day += timedelta(days=1)
    return {"start": FIRST_DAY.isoformat(), "values": values}, {"start": FIRST_DAY.isoformat(), "fields": ["sap", "eua"], "values": inputs}


def implied_premium(auction, participant, area):
    """The average premium of a participant's new capacity in an area, from Terna's
    totals (for the units it names without saying which premium they got)."""
    for row in auction["national"]:
        if row[0] == area and row[1] == participant and len(row) > 4 and row[3]:
            return (row[4] - row[2] * auction["premium"]["existing"]) / row[3]
    return None


def listed_contracts(entry, market):
    auctions = {item["year"]: item for item in market["auctions"]}
    columns = market["units_columns"]
    rows = [dict(zip(columns, values)) for values in market["units"]]
    contracts, names = [], []
    for year, participant, plant in entry["contracts"]:
        row = next((item for item in rows if item["year"] == year and item["participant"] == participant
                    and item["plant"] == plant), None)
        if row is None:
            raise SystemExit(f"capacity_market_units.json: {year} {participant} {plant} is not in Terna's units")
        auction = auctions[year]
        premium, basis = row["premium"], "listed"
        if premium is None:
            premium = implied_premium(auction, participant, row["area"])
            basis = "listed_average"
        years = (auction.get("contract") or {}).get("years") or 15
        contracts.append([year, year + years - 1, row["mw"], round(premium), basis])
        names.append(f"{plant} ({year})")
    return contracts, names


def derating(market, year, technology="pumped"):
    """% of a technology's MW counted as capacity: the year's table, else the nearest year's."""
    tables = market["derating"]["technologies"]
    years = sorted(int(key) for key, table in tables.items() if technology in table)
    nearest = min(years, key=lambda item: (abs(item - year), -item))
    return tables[str(nearest)][technology], nearest


def existing_contracts(match, market, storage_units):
    """code -> (contracts, note) for the units taken as existing capacity."""
    auctions = {item["year"]: item for item in market["auctions"]}
    held = {}
    for year, auction in auctions.items():
        for row in auction["national"]:
            held[(year, row[0], row[1])] = row[2]
    out = {}
    for year in sorted(auctions):
        if year < FIRST_DAY.year:
            continue
        share, table_year = derating(market, year)
        # The owner's units in an area together against its existing capacity there.
        groups = {}
        for code, entry in match.items():
            unit = storage_units.get(code)
            if not unit or not unit[3]:
                continue
            groups.setdefault((entry["participant"], entry["area"]), []).append((code, unit[3] * share / 100))
        for (participant, area), units in groups.items():
            total = sum(cdp for _, cdp in units)
            capacity = held.get((year, area, participant), 0)
            for code, cdp in units:
                contracts, notes = out.setdefault(code, ([], []))
                if capacity >= total:
                    contracts.append([year, year, round(cdp, 1), auctions[year]["premium"]["existing"], "existing"])
                else:
                    notes.append(f"{year}: {participant} held {capacity:,} MW of existing capacity in {area}, under its "
                                 f"units' {total:,.0f} MW there, so none taken")
                if table_year != year:
                    notes.append(f"{year}: derating {share}% (the {table_year} table)")
    return out


def zonal_prices(zonal):
    """zone -> date -> MGP prices by period (1-based in the list's order): the
    quarter-hours where on file, else each hour's price for its four."""
    out = {}
    for zone, series in zonal["zones"].items():
        days = out.setdefault(zone, {})
        hours = {}
        for row in compact.decode_series(series["hourly"]) if "hourly" in series else []:
            hours.setdefault(row["date"], {})[row["hour"]] = row["price"]
        for day, prices in hours.items():
            days[day] = [prices.get(hour) for hour in range(1, max(prices) + 1) for _ in range(4)]
        quarters = {}
        for row in compact.decode_series(series["quarter_hourly"]) if "quarter_hourly" in series else []:
            quarters.setdefault(row["date"], {})[row["period"]] = row["price"]
        for day, prices in quarters.items():
            days[day] = [prices.get(period) for period in range(1, max(prices) + 1)]
    return out


def balancing_up(path):
    """code -> period -> (MW accepted upward net in MSD and MB, their EUR) of a storage day file."""
    data = load(path)
    size = data.get("fields", 14)
    out = {}
    if size <= ST_MB_MW:
        return out
    for quarter in data["quarters"]:
        flat = quarter["rows"]
        for index in range(0, len(flat), size):
            mw = eur = 0.0
            for mw_field, eur_field in ((ST_MSD_MW, ST_MSD_EUR), (ST_MB_MW, ST_MB_EUR)):
                if (flat[index + mw_field] or 0) > 0:
                    mw += flat[index + mw_field]
                    eur += flat[index + eur_field] or 0
            if mw > 0:
                out.setdefault(data["units"][flat[index]][0], {})[quarter["period"]] = (mw, eur)
    return out


def active(contracts, year):
    """(CDP MW, EUR/year) of the contracts running in a year."""
    running = [item for item in contracts if item[0] <= year <= item[1]]
    return sum(item[2] for item in running), sum(item[2] * item[3] for item in running)


def daily_payments(units, storage, prices, strike_values):
    """date -> code -> [premium, MGP payment, balancing payment, [[period, MGP, balancing]]]."""
    first = date.fromisoformat(strike_values["start"])
    spans = {}
    for day, totals in storage["daily"].items():
        for code in totals:
            low, high = spans.get(code, (day, day))
            spans[code] = (min(low, day), max(high, day))
    out = {}
    for day in sorted(storage["days"]):
        when = date.fromisoformat(day)
        index = (when - first).days
        strike_day = strike_values["values"][index] if 0 <= index < len(strike_values["values"]) else None
        file = (storage.get("files") or {}).get(day)
        balancing = balancing_up(os.path.join(STORAGE_DIR, file)) if file else {}
        for code, entry in units.items():
            span = spans.get(code)
            unit = storage["units"].get(code)
            if not span or not unit or not span[0] <= day <= span[1]:
                continue
            cdp, value = active(entry["contracts"], when.year)
            if not cdp:
                continue
            premium = value / (366 if isleap(when.year) else 365)
            day_prices = (prices.get(unit[2]) or {}).get(day)
            quarters, energy_total, balancing_total = [], 0.0, 0.0
            if day_prices and strike_day:
                # The periods before 06:00 (the gas day before's price): 24 on a 96-quarter day.
                early = 24 + len(day_prices) - 96
                up = balancing.get(code, {})
                for period, price in enumerate(day_prices, start=1):
                    if price is None:
                        continue
                    level = strike_day[0] if period <= early else strike_day[1]
                    energy = cdp * max(price - level, 0) / 4
                    extra = 0.0
                    if period in up:
                        mw, eur = up[period]
                        accepted = min(mw, cdp)
                        offered = eur / (mw / 4)
                        extra = accepted * (max(max(price, offered) - level, 0) - max(price - level, 0)) / 4
                    if energy > 0.005 or extra > 0.005:
                        quarters.append([period, round(energy, 2), round(extra, 2)])
                        energy_total += energy
                        balancing_total += extra
            out.setdefault(day, {})[code] = [round(premium, 1), round(energy_total, 1), round(balancing_total, 1), quarters]
    return out


def build():
    market = load(MARKET_PATH)
    match = load(MATCH_PATH)
    storage = load(STORAGE_PATH)
    gas = load(GAS_PATH)
    eua = load(EUA_PATH)
    last = max(date.fromisoformat(day) for day in storage["days"])
    last = max(last, max(daily(gas["series"]["imbalance"])))
    strike_values, inputs = strike_series(gas, eua["auctions"], last)

    units = {}
    for code, entry in match["listed"].items():
        contracts, names = listed_contracts(entry, market)
        units[code] = {"contracts": contracts, "terna": names, "note": entry.get("note")}
    for code, (contracts, notes) in existing_contracts(match["existing"], market, storage["units"]).items():
        entry = match["existing"][code]
        units[code] = {"contracts": contracts, "terna": [],
                       "note": "; ".join(filter(None, [entry.get("note"), *dict.fromkeys(notes)])) or None}

    columns = market["units_columns"]
    due = [dict(zip(columns, values)) for values in market["units"]]
    due = [row for row in due if row["technology"] in ("battery", "battery_solar") and row["year"] <= last.year]
    matched = {(year, participant, plant) for entry in match["listed"].values() for year, participant, plant in entry["contracts"]}
    summary = {
        "listed_due": len(due), "listed_due_mw": sum(row["mw"] for row in due),
        "listed_matched": sum((row["year"], row["participant"], row["plant"]) in matched for row in due),
        "listed_matched_mw": sum(row["mw"] for row in due if (row["year"], row["participant"], row["plant"]) in matched),
    }
    return {
        "generated": date.today().isoformat(),
        "source": "Terna, Mercato della capacità (asta madre reports, 2022-2027 delivery years); ARERA 199/2024/R/eel art. 3 "
                  "and 263/2026/R/eel (strike); GME (gas System Average Price); EEX (EUA auctions)",
        "method": {"heat_rate": HEAT_RATE, "later_heat_rate": LATER_HEAT_RATE, "i_gn": I_GN, "gas_charges": GAS_CHARGES,
                   "emission_rate": EMISSION_RATE, "disposal": DISPOSAL, "dispatch_min": DISPATCH_MIN,
                   "dispatch_share": DISPATCH_SHARE, "other": OTHER, "gas_day_start": GAS_DAY_START},
        "summary": summary,
        "units": dict(sorted(units.items())),
        "unmatched": match.get("unmatched", []),
        "daily": daily_payments(units, storage, zonal_prices(load(ZONAL_PATH)), strike_values),
        "strike": strike_values,
        "inputs": inputs,
    }


def main():
    payload = build()
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    days = [value for value in payload["strike"]["values"] if value]
    summary = payload["summary"]
    totals = [0.0, 0.0, 0.0]
    for units in payload["daily"].values():
        for values in units.values():
            for column in range(3):
                totals[column] += values[column]
    print(f"{OUTPUT_PATH}: {len(payload['units'])} units, strike {len(days)} days "
          f"(last {days[-1][1]:.1f} EUR/MWh); Terna's batteries due: {summary['listed_matched']} of {summary['listed_due']} "
          f"matched ({summary['listed_matched_mw']:.0f} of {summary['listed_due_mw']:.0f} MW CDP); over the storage days: "
          f"premiums {totals[0] / 1e6:.1f}M EUR, variable payments {totals[1] / 1e6:.2f}M (MGP) + {totals[2] / 1e6:.2f}M (balancing)")


if __name__ == "__main__":
    main()
