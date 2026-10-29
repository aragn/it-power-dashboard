"""
Tomorrow's day-ahead (MGP) price forecast for the seven Italian bidding zones,
for the Forecast tab: app/data/forecast/latest.json, and the forecast kept for
its day in app/data/forecast/history.json, scored once the day's prices are out.
latest.json also holds the week ahead (etl/forecast_week.py): the next 7 days,
with the weather forecasts as their known inputs.

Made with only what is out on the morning before the auction (12:00 Italian
time): the zones' prices up to today (yesterday's auction), Terna's load
forecast for tomorrow when it is out (on ENTSO-E by about 07:15), the latest
weather run for tomorrow (etl/fetch_weather.py), the gas and EUA prices, the
neighbours' prices and the calendar; the model is the backtest's
(etl/forecast_backtest.py, MODELS).  Run after fetch_forecast_inputs.py and
fetch_weather.py; a later run the same morning replaces the forecast, but one
made after the gate never replaces one made before it.

MODEL blends Timestral with the stack model (etl/stack_bids.py: the MGP
rebuilt from the real bids of similar recent days, GME's offers being out
about 8 days late), 0.9 and 0.1 of their quantiles.  The stack also needs
its offer files (app/data/stack/offers, from update_gme_offers.py), the units
table (app/data/stack/units.json), the outages ENTSO-E has out now
(fetch_outage_revisions.py --latest), GME's limits (transits.json) and the
zones' generation (zones/*/generation_load.json, the public data branch).
Its band is its errors over the previous days (as in the backtest), from its
forecasts kept in stack_history.json.  When the stack cannot be made, the
forecast is Timestral's alone, and latest.json says so.

  forecast_daily.py                      tomorrow, with MODEL
  forecast_daily.py --day 2026-10-22 --model timestral-28m

Needs requirements-forecast.txt.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import forecast_backtest as backtest  # noqa: E402
import forecast_week  # noqa: E402
from entsoe_api import MARKET_TZ, market_today  # noqa: E402
from forecast_data import FUEL_LAG_DAYS, ZONES, Data  # noqa: E402
from zones import DATA_DIR, ZONE_NAMES  # noqa: E402

LATEST_PATH = os.path.join(DATA_DIR, "forecast", "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "forecast", "history.json")
STACK_HISTORY_PATH = os.path.join(DATA_DIR, "forecast", "stack_history.json")
STACK_MERIT_PATH = os.path.join(DATA_DIR, "forecast", "stack_merit.json")

MODEL = "hybrid"
# The stack whose merit order the Stack model card shows (one clearing, so one
# merit order; the blend's stack is the median of five clearings).
COMPOSITE = "stack-bids-v3"
WEEK_MODEL = "timestral-209m-week"
# Quantiles in latest.json's week.
WEEK_LEVELS = (0.05, 0.25, 0.5, 0.75, 0.95)
# Quantiles kept in the history (latest.json has all 21).
HISTORY_LEVELS = (0.05, 0.5, 0.95)
# The stack's own quantiles in latest.json's "stack" (the Stack model card).
STACK_LEVELS = (0.05, 0.25, 0.5, 0.75, 0.95)
GATE_HOUR = 12


def gate(day):
    """The MGP's gate for `day`: 12:00 Italian time the day before, in UTC."""
    before = day - timedelta(days=1)
    return datetime(before.year, before.month, before.day, GATE_HOUR, tzinfo=MARKET_TZ).astimezone(timezone.utc)


def rounded(values, digits=2):
    return [None if not np.isfinite(v) else round(float(v), digits) for v in values]


def timestral(data, day, model):
    quantiles = backtest.timestral_quantiles(data, [day], backtest.MODELS[model], batch=1)[day]
    return quantiles[:, :data.hours[day], :]  # (zones, hours, levels)


def stack(data, day, model, points):
    """The stack's quantiles for the day (zones, hours, levels), its point
    forecast (zones, HORIZON) and what it was made from (stack_bids.forecast_day):
    stack_bids with what is on file now, the band the backtest's (conformal:
    its errors over the previous days, from its point forecasts kept, `points`)."""
    import stack_bids
    point, info = stack_bids.forecast_day(data, day, backtest.HORIZON, **backtest.stack_options(backtest.MODELS[model]))
    if point is None:
        raise RuntimeError("no reference day on file, or the clearing failed")
    if not np.isfinite(point[:, :data.hours[day]]).all():
        raise RuntimeError("the clearing left hours without a price")
    quantiles = backtest.conformal(data, {**points, day: point}, [day]).get(day)
    if quantiles is None:
        raise RuntimeError(f"fewer than {backtest.CONFORMAL_DAYS // 2} of its forecasts of the last "
                           f"{backtest.CONFORMAL_DAYS} days kept, for its band")
    return quantiles[:, :data.hours[day], :], point, info


def forecast(data, day, model, stack_points):
    """The day's quantiles (zones, hours, levels), the model they are from,
    a blend's parts (each one's weight and median, for latest.json) and the
    stack's {model, quantiles, point, info}, or {model, error} when it could
    not be made (None for a model without the stack).  A blend of Timestral
    and the stack (the backtest's "parts" and "weights") whose stack cannot
    be made (an input missing, the clearing failing) is Timestral's alone."""
    spec = backtest.MODELS[model]
    if "size" in spec:
        return timestral(data, day, model), model, [], None
    if "parts" not in spec or not any("size" in backtest.MODELS[part] for part in spec["parts"]):
        raise SystemExit(f"{model} is neither a Timestral model nor a blend with one.")
    made, stack_made, failed = {}, None, None
    for part in spec["parts"]:
        if "size" in backtest.MODELS[part]:
            made[part] = timestral(data, day, part)
            continue
        print(f"Forecasting {day} with {part}", flush=True)
        try:
            made[part], point, info = stack(data, day, part, stack_points)
            stack_made = {"model": part, "quantiles": made[part], "point": point, "info": info}
        except Exception as error:   # the forecast goes out without it
            failed = f"{type(error).__name__}: {error}"
            stack_made = {"model": part, "error": failed}
    if failed:
        alone = next(part for part in spec["parts"] if "size" in backtest.MODELS[part])
        message = f"The stack could not be made ({stack_made['model']}: {failed}); the forecast is {alone}'s alone."
        print(f"  {message}", flush=True)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        return made[alone], alone, [], stack_made
    weights = spec.get("weights", [1.0 / len(spec["parts"])] * len(spec["parts"]))
    quantiles = sum(weight * made[part] for part, weight in zip(spec["parts"], weights))
    parts = [{"id": part, "label": backtest.MODELS[part]["label"], "weight": weight,
              "median": {zone: rounded(made[part][z, :, backtest.MEDIAN]) for z, zone in enumerate(ZONES)}}
             for part, weight in zip(spec["parts"], weights)]
    return quantiles, model, parts, stack_made


def composite_merit(data, day, made_at):
    """The composite stack's forecast for the day (zones, HORIZON) and its
    merit order, written to stack_merit.json (the Stack model card: the
    forecast day's only, replaced each run), with the day's prices when they
    are out; None, the file left as it was, when it cannot be made."""
    import stack_bids
    spec = backtest.MODELS[COMPOSITE]
    print(f"Forecasting {day} with {COMPOSITE} (its merit order)", flush=True)
    try:
        point, info = stack_bids.forecast_day(data, day, backtest.HORIZON, merit=True, **backtest.stack_options(spec))
        merit = info.pop("merit", None)
        if point is None or merit is None:
            raise RuntimeError("no reference day on file, or the clearing failed")
    except Exception as error:   # the forecast goes out without it
        message = f"The composite stack's merit order could not be made ({type(error).__name__}: {error})."
        print(f"  {message}", flush=True)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        return None
    span = data.day_slice(day)
    for zone in ZONES:
        actual = data.series[f"{zone}|price"][span]
        merit["zones"][zone]["actual"] = rounded(actual) if np.isfinite(actual).all() else None
    out = {"day": day.isoformat(), "made_at": made_at, "model": COMPOSITE, "label": spec["label"], **info, **merit,
           "description": "The composite stack's simulated MGP for the day: per zone and hour, the sale offers summed "
                          "by price step and source (the moved real bids of the reference days, each at 1/5 of its MW, "
                          "and the coupled neighbours' import offers), the purchase bids by price step, the cleared "
                          "price and the net import over the links; the actual price once out (ENTSO-E)."}
    with open(STACK_MERIT_PATH, "w", encoding="utf-8") as handle:
        json.dump(out, handle, separators=(",", ":"))
    return point


def stack_latest(stack_made, composite, hours):
    """latest.json's "stack", for the Stack model card: the stack's own
    forecast (STACK_LEVELS per zone and hour) and what it was made from, or
    why it could not be made; and the composite stack's forecast."""
    if not stack_made:
        return None
    model = stack_made["model"]
    out = {"model": model, "label": backtest.MODELS[model]["label"]}
    if composite is not None:
        out["composite"] = {"model": COMPOSITE, "label": backtest.MODELS[COMPOSITE]["label"],
                            "median": {zone: rounded(composite[z, :hours]) for z, zone in enumerate(ZONES)}}
    if "error" in stack_made:
        return dict(out, error=stack_made["error"])
    quantiles = stack_made["quantiles"]
    return dict(out, **stack_made["info"], levels=list(STACK_LEVELS),
                forecast={zone: [rounded(quantiles[z, :, backtest.LEVELS.index(level)]) for level in STACK_LEVELS]
                          for z, zone in enumerate(ZONES)})


def load_stack_history():
    if not os.path.exists(STACK_HISTORY_PATH):
        return {"forecasts": {}}
    with open(STACK_HISTORY_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def stack_points(history):
    """{day: (zones, HORIZON)} of the stack's forecasts kept."""
    out = {}
    for key, entry in history["forecasts"].items():
        if "point" not in entry:
            continue
        point = np.full((len(ZONES), backtest.HORIZON), np.nan)
        for z, zone in enumerate(ZONES):
            values = np.array([np.nan if v is None else v for v in entry["point"][zone]], dtype=float)
            point[z, :len(values)] = values
        out[date.fromisoformat(key)] = point
    return out


def inputs_status(data, day):
    """What the forecast had for the day: hours of each zone's load forecast and
    of the weather, the last day of prices, gas and EUA."""
    span = data.day_slice(day)
    load = {zone: int(np.isfinite(data.series[f"{zone}|load_forecast"][span]).sum()) for zone in ZONES}
    weather = int(np.isfinite(data.weather.get("SUD|wind_speed|d0", np.full(data.length, np.nan))[span]).sum())

    # Fuel prices enter two days late (forecast_data.FUEL_LAG_DAYS): the day of the price used.
    latest_usable = (day - timedelta(days=FUEL_LAG_DAYS)).isoformat()

    def last_day(prices):
        known = [d for d in prices if d <= latest_usable]
        return max(known) if known else None

    return {
        "hours": data.hours[day],
        "load_forecast_hours": load,
        "weather_hours": weather,
        "last_price_day": data.last_price_day().isoformat(),
        "gas_day": last_day(data.gas_prices),
        "eua_day": last_day(data.eua_prices),
    }


def score(entry, actual):
    """MAE of the median and share of hours inside the 5-95% band, per zone and all."""
    errors, inside = [], []
    out = {}
    for zone in ZONES:
        median = np.array(entry["q50"][zone], dtype=float)
        low, high = np.array(entry["q05"][zone], dtype=float), np.array(entry["q95"][zone], dtype=float)
        truth = actual[zone]
        if len(truth) != len(median) or not np.isfinite(truth).all():
            return None
        e = np.abs(truth - median)
        i = (truth >= low) & (truth <= high)
        out[zone] = {"mae": round(float(e.mean()), 2), "coverage90": round(float(i.mean()), 3)}
        errors.extend(e)
        inside.extend(i)
    out["ALL"] = {"mae": round(float(np.mean(errors)), 2), "coverage90": round(float(np.mean(inside)), 3)}
    return out


def week(data, decision_day):
    """The week after `decision_day`, from forecast_week's model, for latest.json."""
    spec = forecast_week.MODELS[WEEK_MODEL]
    print(f"Forecasting the week after {decision_day} with {WEEK_MODEL}", flush=True)
    weather, prices_only = (forecast_week.timestral_week(data, [decision_day], forecast_week.MODELS[part], batch=1)[decision_day]
                            for part in spec["parts"])
    quantiles = forecast_week.combine(data, decision_day, weather, prices_only, spec["weather_days"])
    days, _, _ = forecast_week.week_days(data, decision_day)
    return {
        "model": {"id": WEEK_MODEL, "label": spec["label"], "description": spec["description"]},
        "days": [d.isoformat() for d in days],
        "hours": [data.hours[d] for d in days],
        "levels": list(WEEK_LEVELS),
        # Per zone: one list of the week's hourly values per level.
        "forecast": {zone: [rounded(quantiles[z, :, backtest.LEVELS.index(level)]) for level in WEEK_LEVELS]
                     for z, zone in enumerate(ZONES)},
    }


def keep_stack(stack_history, day, stack_made, made_at, before_gate, data, last_prices, composite=None):
    """The stack's forecast for the day (and the composite stack's, the merit
    order's) into stack_history.json, kept as history.json's are (one made
    after the gate never replaces one before), and the days whose prices are
    now out scored (MAE per zone and all: scores, composite_scores)."""
    entries = stack_history.setdefault("forecasts", {})
    previous = entries.get(day.isoformat())
    hours = data.hours[day]
    made = stack_made and "point" in stack_made
    if (made or composite is not None) and not (previous and previous.get("before_gate") and not before_gate):
        entry = {"made_at": made_at, "before_gate": before_gate}
        if made:
            entry.update(model=stack_made["model"], reference_days=stack_made["info"]["reference_days"],
                         point={zone: rounded(stack_made["point"][z, :hours]) for z, zone in enumerate(ZONES)})
        if composite is not None:
            entry["composite"] = {zone: rounded(composite[z, :hours]) for z, zone in enumerate(ZONES)}
        entries[day.isoformat()] = entry
    for key, entry in entries.items():
        past = date.fromisoformat(key)
        if past not in data.day_start or past > last_prices:
            continue
        for field, scores in (("point", "scores"), ("composite", "composite_scores")):
            if field not in entry or scores in entry:
                continue
            errors = []
            for zone in ZONES:
                actual = data.series[f"{zone}|price"][data.day_slice(past)]
                point = np.array([np.nan if v is None else v for v in entry[field][zone]], dtype=float)
                if len(point) != len(actual) or not np.isfinite(actual).all():
                    break
                errors.append(np.abs(point - actual))
            else:
                entry[scores] = {zone: round(float(np.nanmean(e)), 2) for zone, e in zip(ZONES, errors)}
                entry[scores]["ALL"] = round(float(np.nanmean(np.concatenate(errors))), 2)
    stack_history["forecasts"] = dict(sorted(entries.items()))
    stack_history["description"] = (
        "The stack model's price forecast kept for each day (the last made before the 12:00 gate), EUR/MWh per zone "
        "and hour, with the reference days it was cleared from, the composite stack's forecast (composite: the "
        "Stack model card's merit order) and, once the day's prices are out, their mean absolute errors per zone "
        "and all zones (scores, composite_scores): the stack's band in the daily forecast comes from its errors "
        "over the previous days.  Entries marked backtest were made as the backtest's (inputs as of the morning "
        "before).")
    with open(STACK_HISTORY_PATH, "w", encoding="utf-8") as handle:
        json.dump(stack_history, handle, separators=(",", ":"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--day", help="day to forecast YYYY-MM-DD (default: tomorrow)")
    parser.add_argument("--model", default=MODEL,
                        help=f"one of the backtest's Timestral models, or a blend with one (default {MODEL})")
    parser.add_argument("--no-week", action="store_true", help="tomorrow only, not the week ahead")
    args = parser.parse_args()

    day = date.fromisoformat(args.day) if args.day else market_today() + timedelta(days=1)
    made_at = datetime.now(timezone.utc)
    # The axis reaches the week's last day, before any input for it is out.
    data = Data(until=day + timedelta(days=forecast_week.DAYS - 1))
    last_prices = data.last_price_day()
    if last_prices is None or last_prices < day - timedelta(days=1):
        raise SystemExit(f"The prices up to {day - timedelta(days=1)} are not on file (last: {last_prices}).")
    # The day's own weather: the latest run's, as at the time of the forecast.
    data.latest_from = data.day_start[day]

    print(f"Forecasting {day} with {args.model}", flush=True)
    stack_history = load_stack_history()
    quantiles, model, parts, stack_made = forecast(data, day, args.model, stack_points(stack_history))
    # The composite stack's merit order, with a blend that has the stack in it.
    composite = composite_merit(data, day, made_at.strftime("%Y-%m-%dT%H:%MZ")) if stack_made else None
    status = inputs_status(data, day)
    before_gate = made_at < gate(day)
    today = day - timedelta(days=1)

    latest = {
        "day": day.isoformat(),
        "made_at": made_at.strftime("%Y-%m-%dT%H:%MZ"),
        "gate": gate(day).strftime("%Y-%m-%dT%H:%MZ"),
        "before_gate": before_gate,
        "model": {"id": model, "label": backtest.MODELS[model]["label"],
                  "description": backtest.MODELS[model]["description"]},
        "inputs": status,
        "levels": list(backtest.LEVELS),
        "zones": [{"code": zone, "name": ZONE_NAMES[zone]} for zone in ZONES],
        # Per zone: one list of hourly values per level.
        "forecast": {zone: [rounded(quantiles[z, :, i]) for i in range(len(backtest.LEVELS))]
                     for z, zone in enumerate(ZONES)},
        # A blend's models: each one's weight and median per zone.
        "parts": parts,
        # The stack's own forecast and inputs (the Stack model card).
        "stack": stack_latest(stack_made, composite, data.hours[day]),
        # Today's prices (ENTSO-E), to draw the forecast after them.
        "today": {"day": today.isoformat(),
                  "prices": {zone: rounded(data.series[f"{zone}|price"][data.day_slice(today)]) for zone in ZONES}},
    }
    if not args.no_week:
        latest["week"] = week(data, today)
    os.makedirs(os.path.dirname(LATEST_PATH), exist_ok=True)

    history = {"forecasts": {}}
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, encoding="utf-8") as handle:
            history = json.load(handle)
    entries = history.setdefault("forecasts", {})
    previous = entries.get(day.isoformat())
    if previous and previous.get("before_gate") and not before_gate:
        print(f"  Kept the forecast made before the gate ({previous['made_at']}); this one is after it.")
    else:
        entries[day.isoformat()] = {
            "made_at": latest["made_at"], "before_gate": before_gate, "model": model,
            **{f"q{round(level * 100):02d}": {zone: rounded(quantiles[z, :, backtest.LEVELS.index(level)])
                                             for z, zone in enumerate(ZONES)}
               for level in HISTORY_LEVELS},
        }
    # Score the past forecasts whose day's prices are now out.
    for key, entry in entries.items():
        past = date.fromisoformat(key)
        if "scores" in entry or past not in data.day_start or past > last_prices:
            continue
        actual = {zone: data.series[f"{zone}|price"][data.day_slice(past)] for zone in ZONES}
        scores = score(entry, actual)
        if scores:
            entry["scores"] = scores
            entry["actual"] = {zone: rounded(actual[zone]) for zone in ZONES}
    history["forecasts"] = dict(sorted(entries.items()))
    history["description"] = ("The day-ahead price forecast kept for each day (the last made before the 12:00 gate, "
                              "else the first after it): 5%, median and 95% per zone and hour, EUR/MWh; scores and "
                              "the actual prices (ENTSO-E) once out.")
    with open(LATEST_PATH, "w", encoding="utf-8") as handle:
        json.dump(latest, handle, separators=(",", ":"))
    with open(HISTORY_PATH, "w", encoding="utf-8") as handle:
        json.dump(history, handle, separators=(",", ":"))
    if stack_made or stack_history.get("forecasts"):
        keep_stack(stack_history, day, stack_made, latest["made_at"], before_gate, data, last_prices, composite)

    medians = {zone: float(np.mean(quantiles[z, :, backtest.MEDIAN])) for z, zone in enumerate(ZONES)}
    print(f"  Made {latest['made_at']} ({'before' if before_gate else 'after'} the gate {latest['gate']})")
    print(f"  Inputs: {json.dumps(status)}")
    print("  Baseload median: " + ", ".join(f"{zone} {value:.1f}" for zone, value in medians.items()))


if __name__ == "__main__":
    main()
