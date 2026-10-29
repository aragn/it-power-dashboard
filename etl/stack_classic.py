"""
Stack model, phase 1: the classic merit order (STACK_MODEL_PLAN.md, section
5), the cost-based baseline the bid-calibrated stack has to beat.  Each day X
is cleared hour by hour from what is out by CUTOFF_HOUR on X-1, before the
MGP's 12:00 gate:

  demand      each zone's load forecast (Terna's day-ahead, on ENTSO-E early
              on X-1) plus pumping and battery charging, at their profile of
              the PROFILE_DAYS days to X-2
  renewables  wind and solar from the weather forecast (etl/stack_res.py)
  must-run    biomass, geothermal, run-of-river, waste and other output, and
              the hydro reservoirs', pumped hydro's and batteries' output, at
              their profile of the same days: all at no cost, so they can be
              curtailed when there is more than the zone can take
  imports     each zone's net flow over the borders (GME's transits), at its
              profile of the same days
  thermal     every gas, coal and oil unit of the stack-unit table
              (app/data/stack/units.json) at its short-run cost: fuel over
              efficiency, plus CO2 at the EUA price over efficiency, plus
              O&M; its capacity less the outages ENTSO-E had published by
              the cutoff (app/data/stack/outage_revisions.json).  Within a
              technology the efficiencies run from the range's high (the
              largest unit) to its low (the smallest): the stack slopes
  grid        the links between the zones within GME's limits of X-1 (the
              last ones out: X's come about 30 minutes before the gate)

Prices are the duals of the zones' balances (stack_clear's clearing, without
bids).  Fuel and carbon prices come in lagged as in the price forecast
(forecast_data.FUEL_LAG_DAYS); coal and oil, with no price series on the
dashboard, at fixed prices.

Used by the backtest (forecast_backtest.py, model "stack-classic"):
point_forecasts(data, days).  On its own, a few days' prices against GME's:

  stack_classic.py --date 2026-10-15 [--date ...]
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compact  # noqa: E402
import stack_res  # noqa: E402
from entsoe_api import local_midnight_utc  # noqa: E402
from fetch_outages import MAX_UNIT_MW  # noqa: E402
from forecast_data import ZONES, Data  # noqa: E402
from zones import DATA_DIR  # noqa: E402

UNITS_PATH = os.path.join(DATA_DIR, "stack", "units.json")
OUTAGES_PATH = os.path.join(DATA_DIR, "stack", "outage_revisions.json")
TRANSITS_PATH = os.path.join(DATA_DIR, "transits.json")

CUTOFF_HOUR = 10            # Italian time on X-1: the inputs out by then
LIVE_CUTOFF_HOUR = 12       # the live forecast's: the gate (all it has on file was out before the run)
PROFILE_DAYS = 7            # days (to X-2) whose hourly profile stands for X's
MUST_RUN = ("B01", "B09", "B11", "B15", "B17", "B20")   # biomass, geothermal, run-of-river, other RES, waste, other
STORAGE_OUT = ("B10", "B12", "B25")                     # pumped hydro, reservoirs, batteries: output
STORAGE_IN = ("B10", "B25")                             # pumped hydro and batteries: charging
THERMAL = ("gas", "coal", "oil", "thermal")
VOM = {"ccgt": 3.0, "ocgt": 5.0, "gas_steam": 4.0, "gas_mid": 3.5, "gas_small": 5.0, "coal": 5.0, "oil": 5.0,
       "thermal": 5.0}                                   # EUR/MWh
GAS_NETWORK = 1.0           # EUR/MWh of gas on top of the PSV (transport, exit)
COAL_PRICE = 12.0           # EUR/MWh of coal (about 100 USD/t)
OIL_PRICE = 50.0            # EUR/MWh of fuel oil
VOLL = 4000.0               # EUR/MWh: demand not met (the MGP's ceiling)
BIG = 1e5


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def utc(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


class Inputs:
    """Everything the classic stack needs, laid out on data's hourly axis."""

    def __init__(self, data, days, cutoff_hour=CUTOFF_HOUR):
        self.data = data
        self.cutoff_hour = cutoff_hour
        self.must_run, self.storage_out, self.storage_in = {}, {}, {}
        for zone in ZONES:
            self.must_run[zone] = stack_res.actual_generation(data, zone, MUST_RUN)
            self.storage_out[zone] = stack_res.actual_generation(data, zone, STORAGE_OUT)
            self.storage_in[zone] = self.consumption(zone, STORAGE_IN)
        self.solar = {zone: stack_res.solar_forecast(data, zone, days, stack_res.actual_generation(
            data, zone, stack_res.SOLAR)) for zone in ZONES}
        self.wind = {zone: stack_res.wind_forecast(data, zone, days, stack_res.actual_generation(
            data, zone, stack_res.WIND)) for zone in ZONES}
        self.imports, self.limits = self.read_transits()
        self.units = self.read_units()
        self.outages = self.read_outages()

    def axis(self, rows):
        out = np.full(self.data.length, np.nan)
        by_iso = {day.isoformat(): self.data.day_start[day] for day in self.data.days}
        for row in rows:
            start = by_iso.get(row["date"])
            if start is None or row["value"] is None:
                continue
            hours, minutes = row["time"].split(":")
            out[start + int(hours) + int(minutes) // 60] = row["value"]
        return out

    def consumption(self, zone, types):
        """A zone's pumping and battery charging, MW on the axis (0 where not reported)."""
        hourly = compact.load(os.path.join(DATA_DIR, "zones", zone, "generation_load.json"))["consumption"]["hourly"]
        out = np.zeros(self.data.length)
        for kind in types:
            values = self.axis((hourly.get(kind) or {}).get("points", []))
            out += np.nan_to_num(values)
        return out

    def read_transits(self):
        """Each Italian zone's net flow in from abroad, and the limits between
        Italian zones each way ({(first, second): (forward, back)})."""
        hourly = compact.load(TRANSITS_PATH)["flows"]["hourly"]
        imports = {zone: np.zeros(self.data.length) for zone in ZONES}
        limits = {}
        for pair, series in hourly.items():
            first, second = pair.split("|")
            if first in ZONES and second in ZONES:
                limits[(first, second)] = (self.axis(series.get("limit_forward", [])),
                                           self.axis(series.get("limit_back", [])))
            elif first in ZONES or second in ZONES:
                flow = np.nan_to_num(self.axis(series.get("flow", [])))
                if first in ZONES:
                    imports[first] -= flow       # positive from first to second: out of Italy
                else:
                    imports[second] += flow
        return imports, limits

    def read_units(self):
        """The thermal units: code, zone, technology, MW, cost parameters; the
        efficiencies spread within each technology from high (largest unit)
        to low (smallest)."""
        table = load(UNITS_PATH)
        rows = [dict(zip(table["columns"], row)) for row in table["units"]]
        units = [row for row in rows if row["source"] in THERMAL and row["zone"] in ZONES and row["mw"]]
        by_tech = defaultdict(list)
        for unit in units:
            by_tech[unit["technology"]].append(unit)
        for tech, group in by_tech.items():
            group.sort(key=lambda unit: -unit["mw"])
            for rank, unit in enumerate(group):
                low, _mid, high = unit["efficiency"] or (0.35, 0.40, 0.45)
                share = rank / (len(group) - 1) if len(group) > 1 else 0.5
                unit["eff"] = high - (high - low) * share
        return units

    def read_outages(self):
        """unit code -> [(nominal, [(created, status, runs)] oldest first)].
        Some documents from September 2026 give the nominal power in kW, the
        available capacity in MW (fetch_outages.repair): rescaled here."""
        stored = load(OUTAGES_PATH)
        nominal = {name: info["nominal"] / 1000 if info["nominal"] and MAX_UNIT_MW < info["nominal"] <= MAX_UNIT_MW * 1000
                   else info["nominal"] for name, info in stored["units"].items()}
        out = defaultdict(list)
        for item in stored["outages"].values():
            cap = nominal.get(item["unit"])
            revisions = [(datetime.fromisoformat(created.replace("Z", "+00:00")), status,
                          [(utc(start), utc(end), mw / 1000 if cap and mw > cap and mw / 1000 <= cap else mw)
                           for start, end, mw in runs])
                         for created, _revision, status, runs in item["revisions"] if created]
            revisions.sort(key=lambda revision: revision[0])
            out[item["unit"]].append((nominal.get(item["unit"]), revisions))
        return out

    def profile(self, series, day):
        """The day's hours from the mean of the same hours over PROFILE_DAYS days to day-2."""
        hours = self.data.hours[day]
        rows = []
        for back in range(2, 2 + PROFILE_DAYS):
            past = day - timedelta(days=back)
            if past not in self.data.day_start:
                continue
            values = series[self.data.day_slice(past)]
            values = np.concatenate([values, np.repeat(values[-1:], max(0, hours - len(values)))])[:hours]
            rows.append(values)
        if not rows or np.isnan(rows).all():
            return np.zeros(hours)
        return np.nan_to_num(np.nanmean(np.where(np.isnan(rows), 0.0, rows) if np.isnan(rows).all(axis=0).any()
                                        else rows, axis=0))

    def unavailable(self, code, day):
        """MW of the unit out in each hour of the day, as published by the cutoff hour of the day before."""
        hours = self.data.hours[day]
        cutoff = local_midnight_utc(day - timedelta(days=1)) + timedelta(hours=self.cutoff_hour)
        start = local_midnight_utc(day)
        out = np.zeros(hours)
        for nominal, revisions in self.outages.get(code, []):
            known = [rev for rev in revisions if rev[0] <= cutoff]
            if not known or nominal is None:
                continue
            _created, status, runs = known[-1]
            if status in ("A09", "A13"):           # cancelled, withdrawn
                continue
            out_mw = np.zeros(hours)
            for hour in range(hours):
                begin, end = start + timedelta(hours=hour), start + timedelta(hours=hour + 1)
                for run_start, run_end, available in runs:
                    overlap = (min(end, run_end) - max(begin, run_start)).total_seconds()
                    if overlap > 0:
                        out_mw[hour] += max(0.0, nominal - available) * overlap / 3600
            out = np.maximum(out, out_mw)
        return out

    def day(self, day):
        data = self.data
        span = data.day_slice(day)
        hours = data.hours[day]
        previous = day - timedelta(days=1)
        gas = data.gas[span][0]
        eua = data.eua[span][0]
        inputs = {"hours": hours, "zones": {}, "limits": {}, "units": []}
        for zone in ZONES:
            load_forecast = data.series[f"{zone}|load_forecast"][span]
            if np.isnan(load_forecast).any():
                load_forecast = np.where(np.isnan(load_forecast), self.profile(data.series[f"{zone}|load_forecast"], day),
                                         load_forecast)
            inputs["zones"][zone] = {
                "demand": load_forecast + self.profile(self.storage_in[zone], day),
                "zero_cost": (np.nan_to_num(self.solar[zone][span]) + np.nan_to_num(self.wind[zone][span])
                              + self.profile(self.must_run[zone], day) + self.profile(self.storage_out[zone], day)),
                "imports": self.profile(self.imports[zone], day),
            }
        for pair, (forward, back) in self.limits.items():
            last = data.day_slice(previous)
            f, b = forward[last], back[last]
            f = np.concatenate([f, np.repeat(f[-1:], max(0, hours - len(f)))])[:hours]
            b = np.concatenate([b, np.repeat(b[-1:], max(0, hours - len(b)))])[:hours]
            inputs["limits"][pair] = (np.nan_to_num(f, nan=BIG), np.nan_to_num(b, nan=BIG))
        for unit in self.units:
            fuel = {"gas": gas + GAS_NETWORK, "coal": COAL_PRICE, "oil": OIL_PRICE}.get(unit["source"], gas + GAS_NETWORK)
            emissions = unit["emissions"] or 0.25
            cost = (fuel + emissions * eua) / unit["eff"] + VOM.get(unit["technology"], 4.0)
            available = np.clip(unit["mw"] - self.unavailable(unit["code"], day), 0, unit["mw"])
            inputs["units"].append((unit["code"], unit["zone"], unit["mw"], cost, available))
        return inputs


def clear_day(inputs):
    """Zonal prices (zones, hours) of a day's classic stack."""
    import pypsa

    hours = inputs["hours"]
    network = pypsa.Network()
    network.set_snapshots(range(hours))
    network.add("Carrier", "AC")
    network.add("Bus", ZONES, carrier="AC")
    index = network.snapshots
    for zone, parts in inputs["zones"].items():
        network.add("Load", f"demand {zone}", bus=zone,
                    p_set=pd.Series(parts["demand"] - parts["imports"], index=index))
        zero = np.maximum(parts["zero_cost"], 0)
        top = max(zero.max(), 1.0)
        network.add("Generator", f"zero-cost {zone}", bus=zone, p_nom=top, marginal_cost=0.0,
                    p_max_pu=pd.Series(zero / top, index=index))
        # More than the zone can use (imports and must-run): spilled at no cost.
        network.add("Generator", f"spill {zone}", bus=zone, p_nom=BIG, marginal_cost=0.0, p_max_pu=0.0, p_min_pu=-1.0)
        network.add("Generator", f"shortage {zone}", bus=zone, p_nom=BIG, marginal_cost=VOLL)
    names = [f"{code} {i}" for i, (code, *_rest) in enumerate(inputs["units"])]
    if names:
        profile = pd.DataFrame({name: available / mw for name, (_c, _z, mw, _cost, available)
                                in zip(names, inputs["units"])}, index=index)
        network.add("Generator", names, bus=[unit[1] for unit in inputs["units"]],
                    p_nom=[unit[2] for unit in inputs["units"]], marginal_cost=[unit[3] for unit in inputs["units"]],
                    p_max_pu=profile, p_min_pu=0.0)
    for (first, second), (forward, back) in inputs["limits"].items():
        top = max(forward.max(), back.max(), 1.0)
        network.add("Link", f"{first}-{second}", bus0=first, bus1=second, p_nom=top, carrier="AC",
                    p_max_pu=pd.Series(forward / top, index=index), p_min_pu=pd.Series(-back / top, index=index))
    network.optimize(solver_name="highs", solver_options={"output_flag": False}, include_objective_constant=False)
    prices = network.buses_t.marginal_price
    return np.stack([prices[zone].to_numpy() for zone in ZONES])


def quiet():
    import logging
    import warnings
    for name in ("pypsa", "linopy"):
        logging.getLogger(name).setLevel(logging.ERROR)
    warnings.simplefilter("ignore", FutureWarning)


def point_forecasts(data, days, horizon=25):
    """{day: (zones, horizon)} hourly prices of the classic stack (NaN past the day's hours)."""
    quiet()
    inputs = Inputs(data, days)
    out = {}
    for count, day in enumerate(days, 1):
        prices = clear_day(inputs.day(day))
        padded = np.full((len(ZONES), horizon), np.nan)
        padded[:, :prices.shape[1]] = prices
        out[day] = padded
        if count % 30 == 0:
            print(f"    stack-classic: {count} of {len(days)} days", flush=True)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", action="append", required=True)
    args = parser.parse_args()
    data = Data()
    days = [date.fromisoformat(text) for text in args.date]
    forecasts = point_forecasts(data, days)
    for day in days:
        actual = data.prices()[:, data.day_slice(day)]
        model = forecasts[day][:, :data.hours[day]]
        errors = np.abs(model - actual)
        print(f"{day}: MAE " + "  ".join(f"{zone} {np.nanmean(errors[i]):6.1f}" for i, zone in enumerate(ZONES)))
        print(f"   NORD actual {np.round(actual[0]).astype(int).tolist()}")
        print(f"   NORD stack  {np.round(model[0]).astype(int).tolist()}")


if __name__ == "__main__":
    main()
