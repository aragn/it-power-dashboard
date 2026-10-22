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

MODEL = "timestral-209m-wx"
WEEK_MODEL = "timestral-209m-week"
# Quantiles in latest.json's week.
WEEK_LEVELS = (0.05, 0.25, 0.5, 0.75, 0.95)
# Quantiles kept in the history (latest.json has all 21).
HISTORY_LEVELS = (0.05, 0.5, 0.95)
GATE_HOUR = 12


def gate(day):
    """The MGP's gate for `day`: 12:00 Italian time the day before, in UTC."""
    before = day - timedelta(days=1)
    return datetime(before.year, before.month, before.day, GATE_HOUR, tzinfo=MARKET_TZ).astimezone(timezone.utc)


def rounded(values, digits=2):
    return [None if not np.isfinite(v) else round(float(v), digits) for v in values]


def forecast(data, day, model):
    spec = backtest.MODELS[model]
    if "size" not in spec:
        raise SystemExit(f"{model} is not a Timestral model; the daily forecast runs Timestral.")
    quantiles = backtest.timestral_quantiles(data, [day], spec, batch=1)[day]
    return quantiles[:, :data.hours[day], :]  # (zones, hours, levels)


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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--day", help="day to forecast YYYY-MM-DD (default: tomorrow)")
    parser.add_argument("--model", default=MODEL, help=f"one of the backtest's Timestral models (default {MODEL})")
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

    model = args.model
    print(f"Forecasting {day} with {model}", flush=True)
    quantiles = forecast(data, day, model)
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

    medians = {zone: float(np.mean(quantiles[z, :, backtest.MEDIAN])) for z, zone in enumerate(ZONES)}
    print(f"  Made {latest['made_at']} ({'before' if before_gate else 'after'} the gate {latest['gate']})")
    print(f"  Inputs: {json.dumps(status)}")
    print("  Baseload median: " + ", ".join(f"{zone} {value:.1f}" for zone, value in medians.items()))


if __name__ == "__main__":
    main()
