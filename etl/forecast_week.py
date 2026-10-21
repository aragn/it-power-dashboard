"""
The week ahead: the seven Italian zones' hourly day-ahead (MGP) prices of the
next 7 days, D+1 .. D+7, forecast on day D before the auction, with weather
forecasts (etl/fetch_weather.py) as the inputs known beyond tomorrow.

What is known on the morning of D, for each day of the week:
  - the weather: for day D+j the forecast made j days (24 h each) before each
    hour, from the runs up to 00 UTC of D (forecast_data.weather_ahead); live,
    the latest run.  Open-Meteo keeps forecasts up to 7 days before the hour,
    so D+7's hours after 06 UTC take the run 7 days before them, up to 18 h
    after that one: the backtest's D+7 has slightly fresher weather than a
    trader had;
  - Terna's load forecast: for D+1 only;
  - the gas and EUA prices: the last known on D, held over the week;
  - the calendar.

  forecast_week.py --backtest            the last 365 decision days -> backtest/week.json
  forecast_week.py --backtest --models naive-week

forecast_daily.py adds the week to latest.json with week_forecast().
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import forecast_backtest as fb  # noqa: E402
from forecast_data import NEIGHBOURS, ZONES, Data  # noqa: E402
from zones import ZONE_NAMES  # noqa: E402

DAYS = 7
# The longest week: seven days, one of them 25 hours long.
HORIZON = 7 * 24 + 1
CONTEXT = 4096
OUTPUT_PATH = os.path.join(fb.OUTPUT_DIR, "week.json")

MODELS = {
    "naive-week": {"label": "Naive", "description": "The same weekday's prices of the last week known"},
    "timestral-28m-ar-week": {"label": "Timestral 28M, prices only", "size": "28M", "weather": False,
                              "description": "Timestral-28M zero-shot, the zones' prices alone"},
    "timestral-28m-wx-week": {"label": "Timestral 28M, with weather", "size": "28M", "weather": True,
                              "description": "Timestral-28M zero-shot with the week's weather forecasts, tomorrow's load "
                                             "forecast, the gas and EUA prices and the calendar"},
}


def week_days(data, day):
    """The decision day's next DAYS days, and their axis span."""
    days = [day + timedelta(days=j) for j in range(1, DAYS + 1)]
    start = data.day_start[days[0]]
    return days, start, data.day_start[days[-1]] + data.hours[days[-1]]


def weather_for(data, zone, name, lead):
    """A weather series as forecast `lead` days ahead (see the module's note on D+7)."""
    if lead < 7 or data.latest_from is not None:
        return data.weather_ahead(zone, name, lead)
    return data.weather.get(f"{zone}|{name}|d7", np.full(data.length, np.nan))


def week_inputs(data, day, context=CONTEXT, weather=True, prices=None):
    """Context (zones, context), known (C, context + HORIZON) and past
    (C, context) rows for the week after `day`, as known on its morning."""
    prices = data.prices() if prices is None else prices
    days, start, end = week_days(data, day)
    ctx = fb.window(prices, start - context, context)
    if not weather:
        return ctx, None, None
    solar_mw, people = fb.weather_weights()
    total = context + HORIZON

    def row(history, by_lead):
        """History over the context; over the week, by_lead(j) on day D+j."""
        out = fb.window(history, start - context, total).copy()
        out[context:] = np.nan
        for j, target in enumerate(days, start=1):
            first = data.day_start[target] - start
            span = slice(context + first, context + first + data.hours[target])
            values = by_lead(j)
            if values is not None:
                out[span] = fb.window(values, data.day_start[target], data.hours[target])
        return out

    def italy(name, weights, lead):
        return sum(weather_for(data, zone, name, lead) * w for zone, w in weights.items()) / sum(weights.values())

    known = []
    for zone in ZONES:
        load = data.series[f"{zone}|load_forecast"]
        known.append(row(load, lambda j, load=load: load if j == 1 else None))
    for zone in fb.WIND_ZONES:
        known.append(row(data.weather_ahead(zone, "wind_speed"), lambda j, zone=zone: weather_for(data, zone, "wind_speed", j)))
    known.append(row(fb.weighted_italy(data, "ghi", solar_mw), lambda j: italy("ghi", solar_mw, j)))
    known.append(row(fb.weighted_italy(data, "temperature", people), lambda j: italy("temperature", people, j)))
    # Gas and EUA: tomorrow's value (the last known on D, lagged) held over the week.
    for daily in (data.gas, data.eua):
        held = np.full(data.length, daily[start])
        known.append(row(daily, lambda j, held=held: held))
    for name in ("holiday", "saturday", "sunday"):
        known.append(row(data.calendar[name], lambda j, name=name: data.calendar[name]))
    known = np.stack(known)
    # Beyond the week's own hours (a 167- or 168-hour week): masked.
    known[:, context + (end - start):] = np.nan

    wind = np.nansum([data.series[f"{zone}|wind_forecast"] for zone in ZONES], axis=0)
    solar = np.nansum([data.series[f"{zone}|solar_forecast"] for zone in ZONES], axis=0)
    past = np.stack([data.series[f"{code}|price"] for code in NEIGHBOURS] + [wind, solar])
    return ctx, known, fb.window(past, start - context, context)


def timestral_week(data, decision_days, spec, batch=2, forecaster=None):
    """{decision day: (zones, week hours, Q)} quantiles."""
    import torch
    from timestral import TimestralForecaster

    torch.set_num_threads(max(1, os.cpu_count() // 2))
    forecaster = forecaster or TimestralForecaster.from_pretrained(f"mistralai/Timestral-{spec['size']}")
    prices = data.prices()
    out = {}
    started = time.time()
    for i in range(0, len(decision_days), batch):
        chunk = decision_days[i:i + batch]
        inputs = [week_inputs(data, day, CONTEXT, spec["weather"], prices) for day in chunk]
        kwargs = {}
        if spec["weather"]:
            kwargs = {"known_covariates": np.stack([k for _, k, _ in inputs]),
                      "past_covariates": np.stack([p for _, _, p in inputs])}
        forecast = forecaster.forecast(np.stack([c for c, _, _ in inputs]), HORIZON, **kwargs)
        quantiles = forecast.quantiles.cpu().numpy()
        for j, day in enumerate(chunk):
            _, start, end = week_days(data, day)
            out[day] = quantiles[j][:, :end - start].astype(np.float64)
        done = i + len(chunk)
        if done % 20 < batch or done == len(decision_days):
            rate = (time.time() - started) / done
            print(f"    {done}/{len(decision_days)} days, {rate:.1f} s a day, "
                  f"{rate * (len(decision_days) - done) / 60:.0f} min left", flush=True)
    return out


def naive_week(data, decision_days):
    """The same weekday of the last week known: day D+j takes D+j-7."""
    prices = data.prices()
    out = {}
    for day in decision_days:
        days, start, end = week_days(data, day)
        parts = []
        for target in days:
            source = target - timedelta(days=7)
            values = fb.to_slots(prices[:, data.day_slice(source)])
            parts.append(fb.from_slots(values, data.hours[target]))
        point = np.concatenate(parts, axis=1)
        out[day] = np.repeat(point[:, :, None], len(fb.LEVELS), axis=2)  # a single value: every quantile
    return out


# ============================================================================
# BACKTEST
# ============================================================================


def cache_path(model):
    return os.path.join(fb.CACHE_DIR, f"{model}.npz")


def save(model, forecasts):
    os.makedirs(fb.CACHE_DIR, exist_ok=True)
    days = sorted(forecasts)
    width = max(forecasts[d].shape[1] for d in days)
    stacked = np.full((len(days), len(ZONES), width, len(fb.LEVELS)), np.nan)
    for i, d in enumerate(days):
        stacked[i, :, :forecasts[d].shape[1]] = forecasts[d]
    np.savez_compressed(cache_path(model), days=np.array([d.isoformat() for d in days]), quantiles=stacked)


def load(model, data):
    if not os.path.exists(cache_path(model)):
        return None
    from datetime import date
    with np.load(cache_path(model)) as stored:
        out = {}
        for d, q in zip(stored["days"], stored["quantiles"]):
            day = date.fromisoformat(d)
            _, start, end = week_days(data, day)
            out[day] = q[:, :end - start]
    return out


def score(data, decision_days, forecasts):
    """{model: {lead: {zone or ALL: scores}}}."""
    metrics = {}
    for model, by_day in forecasts.items():
        buckets = defaultdict(lambda: defaultdict(lambda: {"abs": [], "sq": [], "actual": [], "q": []}))
        for day in decision_days:
            q = by_day.get(day)
            if q is None:
                continue
            days, start, _ = week_days(data, day)
            for j, target in enumerate(days, start=1):
                first = data.day_start[target] - start
                actual = fb.actual_prices(data, target)
                part = q[:, first:first + data.hours[target]]
                if np.isnan(actual).any() or np.isnan(part[..., fb.MEDIAN]).any():
                    continue
                for z, zone in enumerate(ZONES):
                    errors = actual[z] - part[z, :, fb.MEDIAN]
                    for key in (zone, "ALL"):
                        b = buckets[j][key]
                        b["abs"].extend(np.abs(errors))
                        b["sq"].extend(errors ** 2)
                        b["actual"].extend(actual[z])
                        b["q"].extend(part[z])
        metrics[model] = {}
        for j, by_zone in sorted(buckets.items()):
            metrics[model][j] = {}
            for key, b in by_zone.items():
                actual, q = np.array(b["actual"]), np.array(b["q"])
                inside = (actual >= q[:, fb.LEVELS.index(0.05)]) & (actual <= q[:, fb.LEVELS.index(0.95)])
                metrics[model][j][key] = {
                    "mae": round(float(np.mean(b["abs"])), 2),
                    "rmse": round(float(np.sqrt(np.mean(b["sq"]))), 2),
                    "crps": round(fb.crps(actual, q), 2),
                    "coverage90": round(float(np.mean(inside)), 4),
                }
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--models", default=",".join(MODELS))
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    if not args.backtest:
        parser.error("--backtest is the only mode; the live week is made by forecast_daily.py")

    data = Data()
    # Decision days whose whole week has prices.
    last = data.last_price_day() - timedelta(days=DAYS)
    decision_days = [day for day in data.days if last - timedelta(days=fb.TEST_DAYS - 1) <= day <= last]
    print(f"Decision days {decision_days[0]} to {decision_days[-1]} ({len(decision_days)})", flush=True)

    if not args.report_only:
        for model in args.models.split(","):
            spec = MODELS[model]
            print(f"  {model}", flush=True)
            started = time.time()
            forecasts = naive_week(data, decision_days) if model == "naive-week" else timestral_week(data, decision_days, spec)
            save(model, forecasts)
            print(f"  {model}: done in {(time.time() - started) / 60:.1f} min", flush=True)

    forecasts = {model: load(model, data) for model in MODELS}
    forecasts = {model: f for model, f in forecasts.items() if f is not None}
    common = [day for day in decision_days if all(day in f for f in forecasts.values())]
    metrics = score(data, common, forecasts)
    summary = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "description": ("The week ahead: each zone's hourly MGP prices of the next 7 days, forecast each morning before "
                        "the auction, scored by days ahead."),
        "decision_start": common[0].isoformat(), "decision_end": common[-1].isoformat(), "days": len(common),
        "leads": list(range(1, DAYS + 1)),
        "zones": [{"code": zone, "name": ZONE_NAMES[zone]} for zone in ZONES],
        "models": [{"id": m, "label": MODELS[m]["label"], "description": MODELS[m]["description"]} for m in forecasts],
        "metrics": metrics,
    }
    os.makedirs(fb.OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, separators=(",", ":"))
    print("  MAE by days ahead (all zones):")
    for model, by_lead in metrics.items():
        print(f"  {model:24s} " + " ".join(f"{by_lead[j]['ALL']['mae']:6.2f}" for j in sorted(by_lead)))


if __name__ == "__main__":
    main()
