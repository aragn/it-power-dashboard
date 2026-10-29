"""
Stack model, phase 2 (first version): the MGP rebuilt from real bids moved to
the day (STACK_MODEL_PLAN.md, section 5).  GME publishes a day's offers about
8 days after the market, so on X-1, before the 12:00 gate, the latest are
X-9's.  Day X is cleared from the offers and bids of a reference day R, the
latest day of the same kind out by then (reference_day: the same weekday,
no holiday; a Sunday or a holiday for one), moved to X with what is out by
CUTOFF_HOUR on X-1:

  renewables  each solar and wind unit's offered MW moved part of the way
              (SOLAR_WEIGHT, WIND_WEIGHT) to the zone's weather forecast for
              X over its forecast for R (etl/stack_res.py), hour by hour
  demand      each Italian zone's purchase bids moved part of the way
              (LOAD_WEIGHT) to its load forecast for X over its forecast for R
              (Terna's, on ENTSO-E early on X-1)
  thermal     each gas, coal and oil unit's offers above its zero block moved
              by the change in fuel and carbon cost at the unit's efficiency
              (the stack-unit table's middle value), the prices lagged as in
              the price forecast; its MW cut to what the outages published
              by the cutoff leave (stack_classic.Inputs.unavailable)
  replacing   with replace, the thermal capacity the outages cut comes
              back from units available on X that offered nothing on R but
              did on another of the similar days (BidInputs.replace_cut)
  the rest    as offered on R: storage, hydro, imports and exports on the
              borders not coupled with Europe, blocks with their ids
  grid        GME's limits of X-1 (else of the latest day before with
              limits on file), except a link out of service on X or back
              in: there X's day-ahead capacities as ENTSO-E has them
              before the gate (11.1.A, Terna's); the coupled borders' net flow at its
              profile of the 7 days to X-2 (as stack_classic), or, with
              elastic, the coupled neighbours as import offers and export
              bids around their price (BidInputs.neighbours)

With analogs > 1, the reference days are the most similar of that kind
(residual load: load less wind and solar forecasts, a small penalty per week
back), each cleared on its own, and the median of their prices is the
forecast; with composite_days, their moved offers averaged into one day
(composite) and cleared once: closer on the 36 sample days (15.4 against
15.9 EUR/MWh) and about five times cheaper.  The load ratio is bounded (LOAD_RATIO): beyond, a reference day's
supply says little.

Cleared on hourly curves (stack_clear.to_hourly, stack_clear.clear): the
clearing's own error is about 1.5-3.5 EUR/MWh on hourly curves (replays of
real days with their own bids).

Used by the backtest (forecast_backtest.py, model "stack-bids"):
point_forecasts(data, days, horizon), days in parallel; by the daily forecast
(forecast_daily.py): forecast_day(data, day), in the process, with what is on
file at the time (stack_classic.LIVE_CUTOFF_HOUR), and with merit the
composite day's merit order (merit_order) for the Stack model card.  On its
own:

  stack_bids.py --date 2026-10-15 [--date ...]
"""

import argparse
import functools
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stack_classic  # noqa: E402
import stack_clear  # noqa: E402
from fetch_mgp_merit import unit_sources  # noqa: E402
from forecast_data import ZONES, Data, is_holiday  # noqa: E402
from zones import zone_path  # noqa: E402

PUBLISHED_LAG = 9           # on X-1 the latest offers GME has out are X-9's
REFERENCE_WEEKS = 6         # how far back the reference day is looked for
SOLAR_SOURCES = ("solar",)
WIND_SOURCES = ("wind_onshore", "wind_offshore")
THERMAL_SOURCES = ("gas", "coal", "oil", "thermal")
ZERO_BLOCK = 20.0           # EUR/MWh: thermal offers at or below this are the zero block, left as they are
PRICE_CAP = 1000.0          # EUR/MWh: offers above this (scarcity) are left as they are
RATIO_CAP = 5.0             # at most this many times the reference day's wind or solar
# How far solar and wind offers follow the weather forecasts' ratio (1 = all
# the way, 0 = as offered on R): the forecasts are noisy hour by hour, and on
# the 35 sample days of 2025-26 the offered MW came closest at these.
SOLAR_WEIGHT = 0.5
WIND_WEIGHT = 0.75
LOAD_WEIGHT = 0.75          # the same for the demand bids and the load forecasts' ratio
PRICE_ROUNDING = 0.5        # EUR/MWh: a composite day's prices rounded to this, so near-equal steps merge
LOAD_RATIO = (0.8, 1.25)    # the load ratio's bounds: beyond, a reference day's supply says little
ANALOGS = 3                 # reference days per day (similar days; the median of their prices)
RECENCY_PENALTY = 0.01      # a week older costs as much as 1% of the load in residual-load difference
COUPLED_PAIRS = ("NORD|COUP", "SUD|XGRE")
# The neighbours coupled with Italy in the European day-ahead market: their
# zone, the series of their price on data's axis, and of their capacity into
# and out of Italy (JAO's day-ahead NTC; Greece's from GME's limits).
NEIGHBOURS = {"FR": "NORD", "AT": "NORD", "SI": "NORD", "GR": "SUD"}
# Switzerland is not coupled: its imports and exports are nominated on capacity
# bought in explicit auctions and offered in the MGP on GME's virtual zone SVIZ.
# With swiss, those offers and bids give way to Switzerland as one more
# neighbour around its price, up to JAO's NTC.
SWISS_ZONE = "SVIZ"
JAO_PATH = os.path.join(stack_classic.DATA_DIR, "jao_capacity.json")
# GME's virtual zones of the borders not coupled with Europe, as the zones'
# cross-border files name them (etl/fetch_crossborder.py ZONE_BORDERS).
ENTSOE_BORDERS = {"XGRE": "GR", "GREC": "GR", "MONT": "ME", "MALT": "MT"}
WORKERS = 6


def reference_day(day, on_file=None):
    """The day whose bids stand for day X: the latest day out by X-1 of the
    same kind.  A working day takes the same weekday that is no holiday; a
    Sunday or a holiday takes a Sunday or a holiday.  on_file: a test for
    the days with offers (all, by default)."""
    holiday_like = day.weekday() == 6 or is_holiday(day)
    candidates = [day - timedelta(days=back) for back in range(PUBLISHED_LAG, 7 * REFERENCE_WEEKS + 1)]
    for candidate in candidates:
        if on_file and not on_file(candidate):
            continue
        if holiday_like:
            if candidate.weekday() == 6 or is_holiday(candidate):
                return candidate
        elif candidate.weekday() == day.weekday() and not is_holiday(candidate):
            return candidate
    return None


def same_kind(day, candidate, pool="weekday"):
    """Whether a candidate day is of the same kind as day.  A Sunday or
    holiday takes another Sunday or holiday; a Saturday another Saturday; a
    working day, with pool "weekday", the same weekday, with "working", any
    working day (Monday to Friday, no holiday)."""
    if day.weekday() == 6 or is_holiday(day):
        return candidate.weekday() == 6 or is_holiday(candidate)
    if is_holiday(candidate):
        return False
    if pool == "working" and day.weekday() < 5:
        return candidate.weekday() < 5
    return candidate.weekday() == day.weekday()


def candidates(day, on_file=None, pool="weekday"):
    """The days of the same kind out by X-1, newest first."""
    out = []
    for back in range(PUBLISHED_LAG, 7 * REFERENCE_WEEKS + 1):
        candidate = day - timedelta(days=back)
        if same_kind(day, candidate, pool) and (not on_file or on_file(candidate)):
            out.append(candidate)
    return out


@functools.lru_cache(maxsize=8)
def hourly_offers(day):
    """A day's offers as hourly curves (kept for the few days a forecast uses again)."""
    return stack_clear.to_hourly(stack_clear.read_offers(day))


def offers_on_file(day):
    return os.path.exists(os.path.join(stack_clear.OFFERS_DIR, f"{day.isoformat()}.json.gz"))


class BidInputs:
    """What moving a reference day's bids to day X needs, built once."""

    def __init__(self, data, days, elastic=False, swiss=False, cutoff_hour=stack_classic.CUTOFF_HOUR):
        self.data = data
        self.swiss = swiss              # Switzerland as a price-elastic neighbour (needs elastic)
        self.elastic = elastic          # coupled neighbours as price-elastic offers (neighbours()), not fixed flows
        references = {r for day in days for r in candidates(day, offers_on_file, "working")}
        self.classic = stack_classic.Inputs(data, sorted(set(days) | references), cutoff_hour)
        self.sources = {code: info[0] for code, info in unit_sources().items()}
        self.units = {unit["code"]: unit for unit in self.classic.units}
        self.coupled, self.pair_limits = self.read_transits()
        self.ntc = self.read_ntc() if elastic else {}
        self.capacities = self.read_capacities()

    def read_ntc(self):
        """country -> (into Italy, out of Italy) MW on the axis: JAO's
        day-ahead NTC of the northern borders, GME's SUD|XGRE limits for Greece."""
        series = stack_classic.compact.load(JAO_PATH)["series"]
        out = {}
        for country in ("FR", "AT", "SI", "CH"):
            out[country] = (self.classic.axis(series.get(f"ntc_da|{country}", {}).get("hourly", [])),
                            self.classic.axis(series.get(f"ntc_da_out|{country}", {}).get("hourly", [])))
        hourly = stack_classic.compact.load(stack_classic.TRANSITS_PATH)["flows"]["hourly"]["SUD|XGRE"]
        # SUD|XGRE: forward = from SUD to Greece (out of Italy), back = into Italy.
        out["GR"] = (self.classic.axis(hourly.get("limit_back", [])), self.classic.axis(hourly.get("limit_forward", [])))
        return out

    def neighbours(self, day):
        """The coupled neighbours for clearing day X as of X-1: each one's price
        forecast (Lago's naive rule: last week's on Monday, Saturday and Sunday,
        the day before's otherwise; both out by X-1) and its capacity each way
        on X-1 (the last surely out), hour by hour.  Not X's where a border is
        out or back in, as for the zones' links: on 2025-26 that was worse on
        all four (an hour at 0 MW is routine on these borders)."""
        data = self.data
        hours = data.hours[day]
        back = 7 if day.weekday() in (0, 5, 6) else 1
        source = day - timedelta(days=back)
        previous = day - timedelta(days=1)

        def fit(values):
            values = np.asarray(values, float)
            return np.concatenate([values, np.repeat(values[-1:], max(0, hours - len(values)))])[:hours]

        out = []
        countries = dict(NEIGHBOURS, CH="NORD") if self.swiss else NEIGHBOURS
        for country, zone in countries.items():
            price = fit(data.series[f"{country}|price"][data.day_slice(source)])
            into, out_of = self.ntc[country]
            out.append({"name": country, "zone": zone, "price": price,
                        "import": np.nan_to_num(fit(into[data.day_slice(previous)])),
                        "export": np.nan_to_num(fit(out_of[data.day_slice(previous)]))})
        return out

    def read_transits(self):
        """The coupled borders' net flow on the axis (positive from the pair's
        first zone), and every other pair's limits each way (the Italian zones'
        and the borders not coupled with Europe, NaN where GME sets none)."""
        hourly = stack_classic.compact.load(stack_classic.TRANSITS_PATH)["flows"]["hourly"]
        coupled = {pair: np.nan_to_num(self.classic.axis(hourly.get(pair, {}).get("flow", []))) for pair in COUPLED_PAIRS}
        limits = {}
        for pair, series in hourly.items():
            if any(zone in stack_clear.COUPLED_HUBS for zone in pair.split("|")):
                continue
            limits[pair] = (self.classic.axis(series.get("limit_forward", [])),
                            self.classic.axis(series.get("limit_back", [])))
        return coupled, limits

    def read_capacities(self):
        """ENTSO-E's day-ahead capacities (11.1.A) on the axis, from the zones'
        cross-border files (etl/fetch_crossborder.py --zone; NaN where none on
        file): pair -> (forward, back) for each pair GME sets limits for.  The
        files hold them as revised after the auction (GME's limits of the day
        in 99% of the hours of 2025-26); Malta has none."""
        files = {}

        def capacity(zone, border):
            if zone not in files:
                path = zone_path(zone, "crossborder.json")
                files[zone] = (stack_classic.compact.load(path).get("capacity_day_ahead", {}).get("hourly", {})
                               if os.path.exists(path) else {})
            return tuple(self.classic.axis(files[zone].get(direction, {}).get(border, []))
                         for direction in ("imports", "exports"))

        pairs = {}
        for pair, (forward, back) in self.pair_limits.items():
            first, second = pair.split("|")
            home = first if first in ZONES else second if second in ZONES else None
            if home is None or not (np.isfinite(forward).any() or np.isfinite(back).any()):
                continue            # GME sets no limit there
            other = second if home == first else first
            imports, exports = capacity(home, ENTSOE_BORDERS.get(other, other))
            pairs[pair] = (exports, imports) if home == first else (imports, exports)
        return pairs

    def day_or_before(self, own, before, day, previous, fit):
        """A link's capacity on X, hour by hour: its value on previous, except
        where X's own day-ahead capacity on file is 0 (the link out of
        service) or previous's was 0 (back in): there X's own.  What ENTSO-E
        has before the gate is cautious (flat, a few hundred MW under the
        limits GME clears with, which replace it after the auction: the files
        hold those), so only whether a link is out is taken from it."""
        values = fit(self.hours_series(before, previous))
        own = fit(self.hours_series(own, day))
        return np.where(np.isfinite(own) & ((own < 1) | (values < 1)), own, values)

    def capacities_of(self, day):
        """The links that took X's own day-ahead capacity in some hour (out of
        service, or back in), and those without X's on file (not out yet)."""
        hours = self.data.hours[day]

        def fit(values):
            values = np.asarray(values, float)
            return np.concatenate([values, np.repeat(values[-1:], max(0, hours - len(values)))])[:hours]

        limits_day = self.limits_day(day)
        links = [(pair, self.capacities[pair], self.pair_limits[pair], limits_day) for pair in self.capacities]
        own, missing = [], []
        for name, published, before, previous in sorted(links, key=lambda link: link[0]):
            if (any(np.isfinite(series).any() for series in published)       # ENTSO-E publishes it (not Malta)
                    and not all(np.isfinite(self.hours_series(series, day)).all() for series in published)):
                missing.append(name)
            if any(not np.array_equal(self.day_or_before(mine, theirs, day, previous, fit),
                                      fit(self.hours_series(theirs, previous)), equal_nan=True)
                   for mine, theirs in zip(published, before)):
                own.append(name)
        return own, missing

    def hours_series(self, series, day):
        return series[self.data.day_slice(day)]

    def limits_day(self, day):
        """The day whose GME limits stand for X's: X-1 (the last surely out
        by the gate), else the latest day before it with limits on file (they
        come with the private zonal-prices job, which GitHub may start late)."""
        for back in range(1, 8):
            candidate = day - timedelta(days=back)
            if candidate in self.data.day_start and any(
                    np.isfinite(self.hours_series(series, candidate)).any()
                    for pair in self.pair_limits.values() for series in pair):
                return candidate
        return day - timedelta(days=1)

    def ratio(self, numerator, denominator):
        with np.errstate(divide="ignore", invalid="ignore"):
            out = np.where(denominator > 1.0, numerator / denominator, 1.0)
        return np.clip(np.nan_to_num(out, nan=1.0), 0.0, RATIO_CAP)

    def residual_load(self, day):
        """Italy's hourly load forecast less its wind and solar forecast, and the load."""
        span = self.data.day_slice(day)
        load = sum(np.nan_to_num(self.data.series[f"{zone}|load_forecast"][span]) for zone in ZONES)
        res = sum(np.nan_to_num(self.classic.solar[zone][span]) + np.nan_to_num(self.classic.wind[zone][span])
                  for zone in ZONES)
        return load - res, load

    def analogs(self, day, count=ANALOGS, pool="weekday"):
        """The count candidate days most like day: the smallest mean hourly gap
        in residual load (as a share of day's mean load), a little more for
        each week back."""
        target, load = self.residual_load(day)
        scale = max(float(np.mean(load)), 1.0)
        scored = []
        for candidate in candidates(day, offers_on_file, pool):
            other, _ = self.residual_load(candidate)
            n = min(len(target), len(other))
            gap = float(np.mean(np.abs(target[:n] - other[:n]))) / scale
            scored.append((gap + RECENCY_PENALTY * (day - candidate).days / 7, candidate))
        return [candidate for _, candidate in sorted(scored)[:count]]

    def move_price(self, code, source, price, fuel_change):
        """A thermal offer's price moved by the change in fuel and carbon cost at the unit's efficiency."""
        if source not in THERMAL_SOURCES or not ZERO_BLOCK < price < PRICE_CAP:
            return price
        info = self.units.get(code) or {}
        efficiency = (info.get("efficiency") or (0.45, 0.50, 0.55))[1]
        emissions = info.get("emissions") or 0.25
        fuel = fuel_change["gas"] if source == "gas" else 0.0
        return price + (fuel + emissions * fuel_change["eua"]) / efficiency

    def fuel_change(self, day, reference):
        data = self.data
        change = {"gas": data.gas[data.day_slice(day)][0] - data.gas[data.day_slice(reference)][0],
                  "eua": data.eua[data.day_slice(day)][0] - data.eua[data.day_slice(reference)][0]}
        return {key: 0.0 if np.isnan(value) else value for key, value in change.items()}

    def replace_cut(self, offers, day, reference, others, cut, offered_codes):
        """Bring in the thermal units that take the place of those the outages
        cut: units with no outage known on X that offered (almost) nothing on
        the reference day but did on another of the similar days, with that
        day's offers moved to X, cheapest first, until their MW match the cut's
        daily mean."""
        hours = self.data.hours[day]
        best = {}                               # code -> (mean MW, other day, rows)
        for other in others:
            if other == reference:
                continue
            o = fit_hours(hourly_offers(other), hours)
            rows = defaultdict(list)
            steps = o["offers"]
            for values in zip(steps["unit"], steps["period"], steps["price"], steps["flags"],
                              steps.get("block", [0] * len(steps["mw"])), steps["mw"]):
                code = o["units"][values[0]][0]
                if self.sources.get(code) in THERMAL_SOURCES and code not in offered_codes:
                    rows[code].append(values)
            for code, unit_rows in rows.items():
                mean_mw = sum(row[5] for row in unit_rows) / hours
                if mean_mw < 1 or self.classic.unavailable(code, day).max() > 0:
                    continue
                if mean_mw > best.get(code, (0,))[0]:
                    best[code] = (mean_mw, other, o, unit_rows)
        ranked = sorted(best.items(), key=lambda item: np.average([row[2] for row in item[1][3]],
                                                                    weights=[row[5] for row in item[1][3]]))
        target, added = float(np.mean(cut)), 0.0
        steps = offers["offers"]
        columns = {key: list(values) for key, values in steps.items()}
        units, blocks = list(offers["units"]), list(offers.get("blocks", []))
        for code, (mean_mw, other, o, unit_rows) in ranked:
            if added >= target:
                break
            change = self.fuel_change(day, other)
            source = self.sources.get(code)
            unit_index = len(units)
            units.append(list(o["units"][unit_rows[0][0]]))
            block_map = {}
            for _unit, period, price, flags, block, mw in unit_rows:
                if block:
                    if block not in block_map:
                        blocks.append([unit_index, *o["blocks"][block - 1][1:]])
                        block_map[block] = len(blocks)
                    block = block_map[block]
                columns["unit"].append(unit_index)
                columns["period"].append(period)
                columns["price"].append(round(self.move_price(code, source, price, change), 2))
                columns["flags"].append(flags)
                if "block" in columns:
                    columns["block"].append(block)
                columns["mw"].append(mw)
                columns["accepted"].append(0.0)
            added += mean_mw
        offers["offers"], offers["units"], offers["blocks"] = columns, units, blocks
        return added

    def day(self, day, reference=None, others=()):
        """(offers, transits) of day X, hourly, from a reference day's bids
        (by default reference_day's).  others: the other similar days, whose
        thermal units replace those the outages cut (replace_cut)."""
        data = self.data
        reference = reference or reference_day(day, offers_on_file)
        if reference is None:
            raise FileNotFoundError(f"no reference day for {day}")
        hours = data.hours[day]
        offers = dict(fit_hours(hourly_offers(reference), hours))
        units = offers["units"]

        def fit(values):
            values = np.asarray(values, float)
            return np.concatenate([values, np.repeat(values[-1:], max(0, hours - len(values)))])[:hours]

        solar = {zone: self.ratio(fit(self.hours_series(self.classic.solar[zone], day)),
                                  fit(self.hours_series(self.classic.solar[zone], reference))) for zone in ZONES}
        wind = {zone: self.ratio(fit(self.hours_series(self.classic.wind[zone], day)),
                                 fit(self.hours_series(self.classic.wind[zone], reference))) for zone in ZONES}
        load = {zone: np.clip(self.ratio(fit(self.hours_series(data.series[f"{zone}|load_forecast"], day)),
                                         fit(self.hours_series(data.series[f"{zone}|load_forecast"], reference))),
                              *LOAD_RATIO) for zone in ZONES}
        fuel_change = self.fuel_change(day, reference)

        # Each thermal unit's MW offered per hour on R, against what it has on X.
        offered = defaultdict(lambda: np.zeros(hours))
        steps = offers["offers"]
        for unit, hour, mw in zip(steps["unit"], steps["period"], steps["mw"]):
            if self.sources.get(units[unit][0]) in THERMAL_SOURCES:
                offered[unit][hour] += mw
        scale = {}
        for unit, total in offered.items():
            info = self.units.get(units[unit][0])
            if not info:
                continue
            available = np.clip(info["mw"] - self.classic.unavailable(info["code"], day), 0, None)
            with np.errstate(divide="ignore", invalid="ignore"):
                scale[unit] = np.where(total > available, available / np.where(total > 0, total, 1), 1.0)

        new_mw, new_price = [], []
        for unit, hour, price, mw in zip(steps["unit"], steps["period"], steps["price"], steps["mw"]):
            code, _kind, zone, _operator = units[unit]
            source = self.sources.get(code)
            if source in SOLAR_SOURCES and zone in solar:
                mw *= 1 - SOLAR_WEIGHT + SOLAR_WEIGHT * solar[zone][hour]
            elif source in WIND_SOURCES and zone in wind:
                mw *= 1 - WIND_WEIGHT + WIND_WEIGHT * wind[zone][hour]
            elif source in THERMAL_SOURCES:
                if unit in scale:
                    mw *= scale[unit][hour]
                price = self.move_price(code, source, price, fuel_change)
            new_mw.append(round(mw, 3))
            new_price.append(round(price, 2))
        offers["offers"] = dict(steps, mw=new_mw, price=new_price)
        if others:
            cut = np.zeros(hours)
            for unit, total in offered.items():
                if unit in scale:
                    cut += total * (1 - scale[unit])
            offered_codes = {units[unit][0] for unit, total in offered.items()
                             if total.mean() > 0.1 * (self.units.get(units[unit][0]) or {}).get("mw", 0)}
            if cut.mean() > 1:
                self.replace_cut(offers, day, reference, others, cut, offered_codes)

        demand = offers["demand"]
        offers["demand"] = dict(demand, mw=[round(mw * (1 - LOAD_WEIGHT + LOAD_WEIGHT * load[zone][hour]), 3)
                                            if zone in load else mw
                                            for zone, hour, mw in zip(demand["zone"], demand["period"], demand["mw"])])
        if self.swiss:
            drop_zone(offers, SWISS_ZONE)

        transits = {}
        limits_day = self.limits_day(day)
        for pair, (forward, back) in self.pair_limits.items():
            published = self.capacities.get(pair)
            transits[pair] = {}
            for index, (name, series) in enumerate((("limit_forward", forward), ("limit_back", back))):
                values = (self.day_or_before(published[index], series, day, limits_day, fit) if published
                          else fit(self.hours_series(series, limits_day)))
                # NaN (no limit set by GME) is left out: stack_clear sets no limit there.
                transits[pair][name] = {hour: value for hour, value in enumerate(values) if np.isfinite(value)}
        if not self.elastic:
            for pair, flow in self.coupled.items():
                transits[pair] = {"flow": dict(enumerate(self.classic.profile(flow, day)))}
        return offers, transits


def merit_order(offers, network, neighbours, sources):
    """A cleared day's merit order per Italian zone and hour, for the Stack
    model card: the sale offers summed by price step (PRICE_ROUNDING) and
    source, the purchase bids by price step, the coupled neighbours' import
    offers and export bids around their price, each with the MW the clearing
    accepted (a block order is accepted across its hours, so at a price above
    the zone's in some); each zone's price and its net import over the links
    (from the other zones and the borders' virtual zones; positive into the
    zone).  Per zone and hour, supply as flat [price, MW, source index, MW
    accepted, ...] and demand as [price, MW, MW accepted, ...], prices rising."""
    hours = offers["periods"]
    units = offers["units"]
    meta = network.meta.get("steps", {})
    dispatch = network.generators_t.p

    def step(price):
        return round(round(price / PRICE_ROUNDING) * PRICE_ROUNDING, 2)

    def series(name):
        return dispatch[name].to_numpy() if name in dispatch else np.zeros(hours)

    supply = {zone: [defaultdict(lambda: [0.0, 0.0]) for _ in range(hours)] for zone in ZONES}
    demand = {zone: [defaultdict(lambda: [0.0, 0.0]) for _ in range(hours)] for zone in ZONES}
    # The simple orders are one generator per zone and price (stack_clear.build): its dispatch is
    # shared among the sources at that price by their MW.
    simple = {"supply": defaultdict(lambda: np.zeros(hours)), "demand": defaultdict(lambda: np.zeros(hours))}
    rows = []
    for table, side in (("offers", "supply"), ("bids", "demand")):
        steps = offers[table]
        for unit, hour, price, flags, mw in zip(steps["unit"], steps["period"], steps["price"], steps["flags"], steps["mw"]):
            zone = units[unit][2]
            if zone not in supply or mw <= 0:
                continue
            source = sources.get(units[unit][0]) or "unknown"
            key = (step(price), source) if side == "supply" else step(price)
            (supply if side == "supply" else demand)[zone][hour][key][0] += mw
            if not flags & stack_clear.BLOCK:
                simple[side][(zone, price)][hour] += mw
                rows.append((side, zone, hour, price, key, mw))
    steps = offers["demand"]
    for zone, hour, price, mw in zip(steps["zone"], steps["period"], steps["price"], steps["mw"]):
        if zone in demand and mw > 0:
            demand[zone][hour][step(price)][0] += mw
            simple["demand"][(zone, price)][hour] += mw
            rows.append(("demand", zone, hour, price, step(price), mw))

    accepted = {}
    for name, info in meta.items():
        if info[0] in ("supply", "demand"):
            accepted[(info[0], info[1], info[2])] = np.abs(series(name))
        elif info[0] == "linked":
            _, zone, unit, price = info
            if zone not in supply:
                continue
            got = series(name)
            for hour in range(hours):
                if got[hour] > 0:
                    supply[zone][hour][(step(price), sources.get(units[unit][0]) or "unknown")][1] += got[hour]
                elif got[hour] < 0:
                    demand[zone][hour][step(price)][1] -= got[hour]
    for side, zone, hour, price, key, mw in rows:
        total = simple[side][(zone, price)][hour]
        got = accepted.get((side, zone, price))
        if got is not None and total > 0:
            (supply if side == "supply" else demand)[zone][hour][key][1] += mw * min(1.0, got[hour] / total)

    borders = {border["name"]: border for border in neighbours or ()}
    share = 1.0 / len(stack_clear.NEIGHBOUR_STEPS)
    for name, info in meta.items():
        if info[0] != "neighbour" or info[4] not in borders or info[1] not in supply:
            continue
        _, zone, direction, spread, country = info
        border, got = borders[country], series(name)
        for hour in range(hours):
            price = float(border["price"][hour])
            if not np.isfinite(price):
                continue
            if direction == "import" and border["import"][hour] > 0:
                cell = supply[zone][hour][(step(price + spread), "interconnection")]
                cell[0] += border["import"][hour] * share
                cell[1] += max(got[hour], 0.0)
            elif direction == "export" and border["export"][hour] > 0:
                cell = demand[zone][hour][step(price - spread)]
                cell[0] += border["export"][hour] * share
                cell[1] += max(-got[hour], 0.0)

    net = {zone: np.zeros(hours) for zone in ZONES}
    flows = network.links_t.p0
    for name, link in network.links.iterrows():
        if name in flows:
            if link.bus1 in net:
                net[link.bus1] += flows[name].to_numpy()
            if link.bus0 in net:
                net[link.bus0] -= flows[name].to_numpy()
    for name in network.loads.index:
        zone = name.split(" ", 1)[1] if name.startswith("coupled ") else None
        if zone in net and name in network.loads_t.p_set:
            net[zone] -= network.loads_t.p_set[name].to_numpy()       # a coupled border's flow in: a negative load
    prices = network.buses_t.marginal_price

    names = sorted({source for zone in ZONES for table in supply[zone] for _, source in table})
    index = {source: i for i, source in enumerate(names)}
    out = {}
    for zone in ZONES:
        out[zone] = {
            "price": [round(float(value), 2) for value in prices[zone].to_numpy()],
            "net_import": [round(float(value), 1) for value in net[zone]],
            "supply": [[value for (price, source), (mw, got) in sorted(table.items()) if mw >= 0.05
                        for value in (price, round(mw, 1), index[source], round(min(got, mw), 1))] for table in supply[zone]],
            "demand": [[value for price, (mw, got) in sorted(table.items()) if mw >= 0.05
                        for value in (price, round(mw, 1), round(min(got, mw), 1))] for table in demand[zone]],
        }
    return {"hours": hours, "price_step": PRICE_ROUNDING, "sources": names, "zones": out}


def drop_zone(offers, zone):
    """An offers file without the offers and bids of a zone's units and its demand bids."""
    units = offers["units"]
    for table in ("offers", "bids"):
        steps = offers[table]
        keep = [i for i, unit in enumerate(steps["unit"]) if units[unit][2] != zone]
        offers[table] = {key: [values[i] for i in keep] for key, values in steps.items()}
    steps = offers["demand"]
    keep = [i for i, z in enumerate(steps["zone"]) if z != zone]
    offers["demand"] = {key: [values[i] for i in keep] for key, values in steps.items()}


def composite(days_offers):
    """One offers file from several days' (moved) offers, each at an equal
    share of its MW: the days' curves averaged (units, blocks and demand
    kept apart, so blocks stay whole within each day's share); prices
    rounded to PRICE_ROUNDING, so the clearing's steps (one per zone and
    price) stay few."""
    share = 1.0 / len(days_offers)
    out = {key: value for key, value in days_offers[0].items() if key not in ("offers", "bids", "demand", "units", "blocks")}
    out["units"], out["blocks"] = [], []
    tables = {name: defaultdict(list) for name in ("offers", "bids", "demand")}
    for offers in days_offers:
        unit_offset, block_offset = len(out["units"]), len(out["blocks"])
        out["units"] += [list(unit) for unit in offers["units"]]
        out["blocks"] += [[unit + unit_offset, *rest] for unit, *rest in offers.get("blocks", [])]
        for name in ("offers", "bids", "demand"):
            steps = offers[name]
            for key, values in steps.items():
                if key == "unit":
                    values = [value + unit_offset for value in values]
                elif key == "block":
                    values = [value + block_offset if value else 0 for value in values]
                elif key in ("mw", "accepted"):
                    values = [value * share for value in values]
                elif key == "price" and PRICE_ROUNDING:
                    values = [round(value / PRICE_ROUNDING) * PRICE_ROUNDING for value in values]
                tables[name][key] += list(values)
    for name, steps in tables.items():
        out[name] = dict(steps)
    return out


def fit_hours(offers, hours):
    """An hourly offers file fitted to a day of `hours` hours (a clock change
    between the reference day and X): later hours dropped, or the last one
    repeated."""
    have = offers["periods"]
    if have == hours:
        return offers
    out = dict(offers, periods=hours)
    for table in ("offers", "bids", "demand"):
        steps = offers[table]
        keys = list(steps)
        rows = [dict(zip(keys, values)) for values in zip(*(steps[key] for key in keys))]
        kept = [row for row in rows if row["period"] < hours]
        if hours > have:
            kept += [dict(row, period=hour) for row in rows if row["period"] == have - 1 for hour in range(have, hours)]
        out[table] = {key: [row[key] for row in kept] for key in keys}
    out["prices"] = {zone: (values + values[-1:] * (hours - len(values)))[:hours] for zone, values in offers["prices"].items()}
    return out


_STATE = {}


def _setup(data, days, analogs, pool, combine="median", elastic=False, replace=False, swiss=False,
           composite_days=False, cutoff_hour=stack_classic.CUTOFF_HOUR):
    _STATE["composite"] = composite_days
    _STATE["analogs"], _STATE["pool"], _STATE["combine"], _STATE["replace"] = analogs, pool, combine, replace
    stack_classic.quiet()
    _STATE["data"] = data
    _STATE["inputs"] = BidInputs(data, days, elastic, swiss, cutoff_hour)


def _init(days, *options):
    _setup(Data(), days, *options)


def _forecast(day):
    import io
    import contextlib
    data, inputs = _STATE["data"], _STATE["inputs"]
    try:
        references = inputs.analogs(day, _STATE["analogs"], _STATE["pool"]) if _STATE["analogs"] > 1 else [None]
        if not references:
            return day, None
        _STATE["references"] = [reference or reference_day(day, offers_on_file) for reference in references]
        cleared = []
        neighbours = inputs.neighbours(day) if inputs.elastic else None
        if _STATE.get("composite") and len(references) > 1:
            moved = [inputs.day(day, reference, references if _STATE.get("replace") else ()) for reference in references]
            merged = composite([offers for offers, _ in moved])
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                prices, network = stack_clear.clear(day, merged, moved[0][1], neighbours=neighbours)
            if _STATE.get("merit"):
                _STATE["merit_order"] = merit_order(merged, network, neighbours, inputs.sources)
            return day, np.stack([prices[zone] for zone in ZONES])
        for reference in references:
            offers, transits = inputs.day(day, reference, references if _STATE.get("replace") else ())
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                prices, _ = stack_clear.clear(day, offers, transits, neighbours=neighbours)
            cleared.append(np.stack([prices[zone] for zone in ZONES]))
        combine = np.mean if _STATE.get("combine") == "mean" else np.median
        return day, combine(np.stack(cleared), axis=0)
    except FileNotFoundError:
        return day, None
    except Exception as error:   # a day the clearing cannot do: no forecast for it
        print(f"    stack-bids {day}: {type(error).__name__}: {error}", flush=True)
        return day, None


def point_forecasts(data, days, horizon=25, workers=WORKERS, analogs=1, pool="weekday", combine="median",
                    elastic=False, replace=False, swiss=False, composite_days=False):
    """{day: (zones, horizon)} hourly prices (NaN past the day's hours); days
    whose reference day has no offers on file are left out.  analogs > 1:
    that many similar reference days (BidInputs.analogs), the median of their
    prices.  pool: the candidates of a working day (same_kind)."""
    from multiprocessing import Pool

    out = {}
    with Pool(workers, initializer=_init,
              initargs=(days, analogs, pool, combine, elastic, replace, swiss, composite_days)) as workers_pool:
        for count, (day, prices) in enumerate(workers_pool.imap_unordered(_forecast, days), 1):
            if prices is not None:
                padded = np.full((len(ZONES), horizon), np.nan)
                padded[:, :prices.shape[1]] = prices
                out[day] = padded
            if count % 30 == 0:
                print(f"    stack-bids: {count} of {len(days)} days", flush=True)
    return out


def forecast_day(data, day, horizon=25, analogs=1, pool="weekday", combine="median", elastic=False, replace=False,
                 swiss=False, composite_days=False, cutoff_hour=stack_classic.LIVE_CUTOFF_HOUR, merit=False):
    """(prices, what it was made from) of one day, made in this process with
    data as it is (the live forecast's: its weather run, the outages on
    file): (zones, horizon) hourly prices, and the reference days, the day
    of GME's limits, the links that took X's own day-ahead capacities
    (limits_own: out of service or back in) and those without X's on file
    (limits_missing), and the thermal capacity out (with merit and
    composite_days, also the day's merit order, merit_order); prices None
    when it cannot be made (no reference day on file, the clearing failing:
    the reason is printed)."""
    _setup(data, [day], analogs, pool, combine, elastic, replace, swiss, composite_days, cutoff_hour)
    _STATE["merit"] = merit
    try:
        _, prices = _forecast(day)
        inputs = _STATE["inputs"]
        out = {code: inputs.classic.unavailable(code, day) for code in inputs.units}
        own, missing = inputs.capacities_of(day)
        info = {"reference_days": [reference.isoformat() for reference in _STATE.get("references", [])],
                "limits_day": inputs.limits_day(day).isoformat(),
                "limits_own": own, "limits_missing": missing,
                "thermal_out_mw": round(float(sum(mw.mean() for mw in out.values()))),
                "thermal_out_units": sum(1 for mw in out.values() if mw.max() > 0)}
        if merit:
            info["merit"] = _STATE.get("merit_order")
    finally:
        _STATE.clear()
    if prices is None:
        return None, info
    padded = np.full((len(ZONES), horizon), np.nan)
    padded[:, :prices.shape[1]] = prices
    return padded, info


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", action="append", required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--analogs", type=int, default=1, help="similar reference days, median of their prices")
    parser.add_argument("--pool", default="weekday", choices=("weekday", "working"))
    parser.add_argument("--elastic", action="store_true", help="coupled neighbours as price-elastic offers")
    parser.add_argument("--replace", action="store_true", help="units that replace the thermal capacity outages cut")
    parser.add_argument("--swiss", action="store_true", help="Switzerland as a price-elastic neighbour")
    parser.add_argument("--composite", action="store_true", help="the similar days' curves averaged, cleared once")
    args = parser.parse_args()
    data = Data()
    days = [date.fromisoformat(text) for text in args.date]
    forecasts = point_forecasts(data, days, workers=args.workers, analogs=args.analogs, pool=args.pool,
                                elastic=args.elastic, replace=args.replace, swiss=args.swiss,
                                composite_days=args.composite)
    for day in days:
        if day not in forecasts:
            print(f"{day}: no forecast")
            continue
        actual = data.prices()[:, data.day_slice(day)]
        model = forecasts[day][:, :data.hours[day]]
        errors = np.abs(model - actual)
        print(f"{day}: MAE " + "  ".join(f"{zone} {np.nanmean(errors[i]):6.1f}" for i, zone in enumerate(ZONES)))
        print(f"   NORD actual {np.round(actual[0]).astype(int).tolist()}")
        print(f"   NORD stack  {np.round(model[0]).astype(int).tolist()}")


if __name__ == "__main__":
    main()
