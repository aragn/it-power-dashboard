"""
Stack model: the MGP's clearing as a linear programme (PyPSA, solved by
HiGHS), a day at a time, its periods (quarter-hours from October 2025, hours
before) as snapshots.  Welfare is maximised over the stepwise sale offers and
purchase bids of every zone, the flows between zones within GME's
transmission limits; each zone's price is the dual of its balance.  Since
2025 purchase bids pay zonal prices (the PUN Index is worked out
afterwards), so there is no PUN constraint.

  buses       the Italian zones and GME's virtual zones of the borders not
              coupled with Europe (SVIZ, AUST, FRAN, SLOV, GREC, MONT, MALT,
              CORS, COAC), where the explicit imports and exports are offered
  links       GME's pairs of zones, limits each way period by period
              (transits.json); no limit where GME sets none
  coupled     the borders coupled with Europe (Italia Coupling's hub on the
              North, Greece's XGRE on the South) clear inside the European
              coupling, so their net flow comes in as a fixed exchange
  generators  the simple sale offers summed per zone and price; the purchase
              bids as negative generators (they take power when the price is
              at or below their bid)
  linked      a block order (GME's BLOCK_ID; in files without it, a unit's
              offers at one price over a run of periods), and an hourly
              order in a quarter-hourly day (the same
              MW in its hour's four quarter-hours, paid their mean price), as
              one variable for all its periods: an extendable generator held
              to its profile.  A block with a minimum acceptance ratio is
              accepted at least at that ratio or not at all with
              integer_blocks (a mixed-integer step, ~4 times slower); by
              default every block may be cut (the linear relaxation).
              Neither reproduces the paradoxically rejected blocks of the
              European coupling's algorithm (a block in the money rejected)

Replay (this module's command line): the day's own offers and bids
(app/data/stack/offers/<date>.json.gz) and actual coupled flows, against
GME's prices, hour by hour; what is left is the clearing's own error (the
relaxed blocks, rounding, prices on a vertical step).

  stack_clear.py --date 2026-10-15 [--date ...] [--integer-blocks]
"""

import argparse
import gzip
import json
import os
import sys
from collections import defaultdict
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compact  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFFERS_DIR = os.path.join(ROOT, "app", "data", "stack", "offers")
TRANSITS_PATH = os.path.join(ROOT, "app", "data", "transits.json")
ITALY = ["NORD", "CNOR", "CSUD", "SUD", "CALA", "SICI", "SARD"]
COUPLED_HUBS = {"COUP", "XGRE", "XAUS", "XFRA", "BSP"}   # sides of the borders coupled with Europe
HOURLY, BLOCK = 1, 4                                    # offers-file flags
NO_LIMIT = 1e5
# A coupled neighbour's import offer and export bid: equal steps this far
# (EUR/MWh) above (imports) or below (exports) its price, so the flow grows
# with the price gap and the two never trade with each other.
NEIGHBOUR_STEPS = (0.0, 5.0, 10.0, 20.0, 40.0)
MIP_SECONDS = 300                                       # time limit of the blocks' mixed-integer step


def read_offers(day, offers_dir=OFFERS_DIR):
    with gzip.open(os.path.join(offers_dir, f"{day.isoformat()}.json.gz"), "rt", encoding="utf-8") as handle:
        return json.load(handle)


def layout(offers):
    """(periods, minutes per period, the steps' period column) of an offers
    file: format 2 has the MGP's own periods; format 1's are hours."""
    if "periods" in offers:
        return offers["periods"], offers["period_minutes"], "period"
    return offers["hours"], 60, "hour"


def read_transits(day, minutes, transits=None):
    """pair -> {"flow", "limit_forward", "limit_back"}: {period index: MW}."""
    transits = transits or compact.load(TRANSITS_PATH)
    series_by_pair = transits["flows"]["quarter_hourly" if minutes == 15 else "hourly"]
    iso = day.isoformat()
    out = {}
    for pair, series in series_by_pair.items():
        entry = {}
        for name, rows in series.items():
            values = {}
            for row in rows:
                if row["date"] == iso:
                    hours, mins = row["time"].split(":")
                    values[(int(hours) * 60 + int(mins)) // minutes] = row["value"]
            entry[name] = values
        out[pair] = entry
    return out


def runs(profile):
    """The profile split into runs of consecutive periods with MW."""
    out, run = [], np.zeros_like(profile)
    for index in range(len(profile) + 1):
        if index < len(profile) and profile[index] > 0:
            run[index] = profile[index]
        elif run.any():
            out.append(run)
            run = np.zeros_like(profile)
    return out


def build(day, offers, transits, fixed_flows=True, neighbours=None):
    """The day's PyPSA network (not solved)."""
    import pypsa

    periods, minutes, column = layout(offers)
    zone_of = [unit[2] for unit in offers["units"]]
    network = pypsa.Network()
    network.set_snapshots(range(periods))

    supply = defaultdict(lambda: np.zeros(periods))     # (zone, price) -> MW per period
    demand = defaultdict(lambda: np.zeros(periods))
    linked = defaultdict(lambda: np.zeros(periods))     # (sign, zone, unit, price, kind[, hour]) -> MW per period
    has_ids = "block" in offers["offers"]               # format 3: GME's block ids
    for table, sign in (("offers", 1), ("bids", -1)):
        steps = offers[table]
        blocks = steps["block"] if has_ids else [0] * len(steps["mw"])
        for unit, period, price, flags, mw, block in zip(steps["unit"], steps[column], steps["price"], steps["flags"],
                                                         steps["mw"], blocks):
            zone = zone_of[unit]
            if flags & BLOCK:
                linked[(sign, zone, unit, price, "block", block)][period] += mw
            elif flags & HOURLY and minutes == 15:
                linked[(sign, zone, unit, price, "hourly", period // 4)][period] += mw
            else:
                (supply if sign > 0 else demand)[(zone, price)][period] += mw
    for zone, period, price, mw in zip(offers["demand"]["zone"], offers["demand"][column], offers["demand"]["price"],
                                       offers["demand"]["mw"]):
        demand[(zone, price)][period] += mw

    buses = sorted({zone for zone, _ in supply} | {zone for zone, _ in demand} | set(ITALY))
    buses = [bus for bus in buses if bus and bus not in COUPLED_HUBS]
    network.add("Carrier", "AC")
    network.add("Bus", buses, carrier="AC")

    def add_steps(table, kind):
        names, bus, p_nom, cost, profile = [], [], [], [], {}
        for index, ((zone, price), mw) in enumerate(sorted(table.items(), key=lambda item: (item[0][0] or "", item[0][1]))):
            if zone not in buses or mw.max() <= 0:
                continue
            name = f"{kind} {zone} {index}"
            names.append(name)
            bus.append(zone)
            p_nom.append(mw.max())
            cost.append(price)
            profile[name] = mw / mw.max()
        frame = pd.DataFrame(profile, index=network.snapshots)
        if kind == "supply":
            network.add("Generator", names, bus=bus, p_nom=p_nom, marginal_cost=cost, p_max_pu=frame, p_min_pu=0.0)
        else:
            # A purchase bid: a generator running negative, worth its bid price.
            network.add("Generator", names, bus=bus, p_nom=p_nom, marginal_cost=cost, p_max_pu=0.0, p_min_pu=-frame)

    add_steps(supply, "supply")
    add_steps(demand, "demand")

    names, bus, p_nom, cost, profile, ratios = [], [], [], [], {}, {}
    for key, mw in linked.items():
        sign, zone, _unit, price, kind = key[:5]
        if zone not in buses:
            continue
        # A block's minimum acceptance ratio (format 3; GME's 1 = whole or not
        # at all); an hourly order, or a block of an older file, may be cut.
        ratio = offers["blocks"][key[5] - 1][2] if kind == "block" and has_ids and key[5] else 0.0
        # Without block ids (formats 1 and 2), a unit's blocks at one price
        # are told apart by their runs of periods only.
        for run in (runs(mw) if kind == "block" and not has_ids else [mw]):
            name = f"{kind} {zone} {len(names)}"
            names.append(name)
            bus.append(zone)
            p_nom.append(run.max())
            cost.append(price)
            profile[name] = sign * run / run.max()
            ratios[name] = ratio or 0.0
    if names:
        frame = pd.DataFrame(profile, index=network.snapshots)
        network.add("Generator", names, bus=bus, p_nom_extendable=True, p_nom_min=0.0, p_nom_max=p_nom,
                    marginal_cost=cost, p_max_pu=frame, p_min_pu=frame)

    fixed = defaultdict(lambda: np.zeros(periods))
    for pair, series in transits.items():
        first, second = pair.split("|")
        flows = series.get("flow", {})
        if first in COUPLED_HUBS or second in COUPLED_HUBS:
            if not fixed_flows:
                continue
            italian, sign = (second, 1) if first in COUPLED_HUBS else (first, -1)
            if italian not in buses:
                continue
            for period, value in flows.items():
                if period < periods and value is not None:
                    fixed[italian][period] += sign * value     # positive: into Italy
            continue
        if first not in buses or second not in buses:
            continue
        forward = np.array([series.get("limit_forward", {}).get(p, NO_LIMIT) or NO_LIMIT for p in range(periods)], float)
        back = np.array([series.get("limit_back", {}).get(p, NO_LIMIT) or NO_LIMIT for p in range(periods)], float)
        top = max(forward.max(), back.max(), 1.0)
        network.add("Link", f"{first}-{second}", bus0=first, bus1=second, p_nom=top, efficiency=1.0, carrier="AC",
                    p_max_pu=pd.Series(forward / top, index=network.snapshots),
                    p_min_pu=pd.Series(-back / top, index=network.snapshots))
    # The coupled neighbours, when given: each border an import offer and an
    # export bid in steps around the neighbour's price (NEIGHBOUR_STEPS), up to
    # the border's capacity each way, in place of a fixed flow.
    for border in neighbours or ():
        zone, price = border["zone"], np.asarray(border["price"], float)
        if zone not in buses:
            continue
        for direction, capacity, sign in (("import", border["import"], 1), ("export", border["export"], -1)):
            capacity = np.asarray(capacity, float) / len(NEIGHBOUR_STEPS)
            top = max(float(np.nanmax(capacity)), 1.0)
            for step, spread in enumerate(NEIGHBOUR_STEPS):
                profile = pd.Series(np.nan_to_num(capacity) / top, index=network.snapshots)
                cost = pd.Series(np.nan_to_num(price + sign * spread, nan=0.0), index=network.snapshots)
                name = f"{border['name']} {direction} {step}"
                if sign > 0:
                    network.add("Generator", name, bus=zone, p_nom=top, marginal_cost=cost, p_max_pu=profile,
                                p_min_pu=0.0)
                else:
                    network.add("Generator", name, bus=zone, p_nom=top, marginal_cost=cost, p_max_pu=0.0,
                                p_min_pu=-profile)
    for zone, values in fixed.items():
        network.add("Load", f"coupled {zone}", bus=zone, p_set=pd.Series(-values, index=network.snapshots))
    return network, pd.Series(ratios, dtype=float)


def clear(day, offers, transits, fixed_flows=True, integer_blocks=False, neighbours=None):
    """Zonal prices {zone: array over the periods}, and the network.

    integer_blocks: the blocks with a minimum acceptance ratio are accepted
    at least at that ratio or not at all (a yes/no variable each: a
    mixed-integer programme); the prices then come from the linear
    programme with those choices fixed, as in the European coupling's
    algorithm.  Without it, every block may be cut (the linear relaxation)."""
    import xarray as xr

    network, ratios = build(day, offers, transits, fixed_flows, neighbours)
    options = {"output_flag": False}
    lumpy = ratios[ratios > 0]
    if integer_blocks and len(lumpy):
        model = network.optimize.create_model(include_objective_constant=False)
        size = model.variables["Generator-p_nom"]
        names = pd.Index(lumpy.index, name=size.dims[0])
        on = model.add_variables(binary=True, coords=[names], name="block_on")
        top = xr.DataArray(network.generators.p_nom_max[names].to_numpy(), coords=[names])
        least = xr.DataArray((lumpy * network.generators.p_nom_max[names]).to_numpy(), coords=[names])
        model.add_constraints(size.loc[names] - top * on <= 0, name="block_upper")
        model.add_constraints(size.loc[names] - least * on >= 0, name="block_lower")
        network.optimize.solve_model(solver_name="highs", solver_options={**options, "mip_rel_gap": 1e-6,
                                                                          "time_limit": MIP_SECONDS})
        accepted = on.solution.to_series() > 0.5
        generators = network.generators
        generators.loc[accepted.index[accepted], "p_nom_min"] = least.to_series()[accepted]
        generators.loc[accepted.index[~accepted], "p_nom_max"] = 0.0
    network.optimize(solver_name="highs", solver_options=options, include_objective_constant=False)
    prices = network.buses_t.marginal_price
    return {zone: prices[zone].to_numpy() for zone in prices.columns}, network


def to_hourly(offers):
    """A quarter-hourly offers file as hourly curves: each step's MW the mean
    over its hour's four quarter-hours (blocks keep their ids, hourly orders
    become simple ones), prices the hour's mean.  Clearing hourly curves is
    about four times faster; an hourly file is returned as it is."""
    periods, minutes, column = layout(offers)
    if minutes == 60:
        return offers
    hours = periods // 4

    def tables(steps, keys):
        summed = defaultdict(lambda: [0.0, 0.0])
        for values in zip(*(steps[key] for key in keys), steps[column], steps["mw"], steps["accepted"]):
            *rest, period, mw, accepted = values
            rest = [value & ~HOURLY if key == "flags" else value for key, value in zip(keys, rest)]
            step = summed[(*rest, period // 4)]
            step[0] += mw / 4
            step[1] += accepted / 4
        out = {key: [] for key in keys + ["period", "mw", "accepted"]}
        for key_values, (mw, accepted) in summed.items():
            for key, value in zip(keys + ["period"], key_values):
                out[key].append(value)
            out["mw"].append(round(mw, 3))
            out["accepted"].append(round(accepted, 3))
        return out

    step_keys = ["unit", "price", "flags"] + (["block"] if "block" in offers["offers"] else [])
    result = dict(offers)
    result.update({
        "periods": hours, "period_minutes": 60,
        "offers": tables(offers["offers"], step_keys), "bids": tables(offers["bids"], step_keys),
        "demand": tables(offers["demand"], ["zone", "price"]),
        "prices": {zone: [None if any(v is None for v in values[h * 4:h * 4 + 4]) else
                          round(sum(values[h * 4:h * 4 + 4]) / 4, 2) for h in range(hours)]
                   for zone, values in offers["prices"].items()},
    })
    return result


def hourly(values, minutes):
    """Period values -> hourly means."""
    values = np.asarray(values, float)
    per_hour = 60 // minutes
    return values.reshape(-1, per_hour).mean(axis=1) if per_hour > 1 else values


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", action="append", required=True)
    parser.add_argument("--hourly", action="store_true", help="clear hourly curves (quarter-hours averaged)")
    parser.add_argument("--integer-blocks", action="store_true",
                        help="blocks at least at their minimum acceptance ratio or not at all (slower: ~4x)")
    args = parser.parse_args()
    import logging
    import warnings
    logging.getLogger("pypsa").setLevel(logging.ERROR)
    logging.getLogger("linopy").setLevel(logging.ERROR)
    warnings.simplefilter("ignore", FutureWarning)
    transits = compact.load(TRANSITS_PATH)
    errors = defaultdict(list)
    for text in args.date:
        day = date.fromisoformat(text)
        offers = read_offers(day)
        if args.hourly:
            offers = to_hourly(offers)
        _periods, minutes, _column = layout(offers)
        prices, _ = clear(day, offers, read_transits(day, minutes, transits), integer_blocks=args.integer_blocks)
        line = []
        for zone in ITALY:
            actual = [np.nan if v is None else v for v in offers["prices"].get(zone, [])]
            model = prices.get(zone)
            if model is None or not len(actual):
                continue
            error = np.abs(hourly(model, minutes) - hourly(actual, minutes))
            errors[zone].extend(error[np.isfinite(error)])
            line.append(f"{zone} {np.nanmean(error):6.2f}")
        print(f"{day} ({minutes}-minute periods): replay MAE of the hourly prices " + "  ".join(line), flush=True)
    print("all: " + "  ".join(f"{zone} MAE {np.mean(values):.2f}, within 1 EUR {np.mean(np.array(values) < 1):.0%}"
                              for zone, values in errors.items()))


if __name__ == "__main__":
    main()
