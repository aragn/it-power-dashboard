"""
Backtest of the day-ahead price forecast: the seven Italian zones' hourly
MGP prices of each day X, forecast as on day X-1 before the auction, over a
test period (by default the last 365 days with prices), and written to
app/data/forecast/backtest/ for the Forecast tab:

  summary.json        the models, their scores per zone and month, the
                      battery trading on each model's forecast
  <ZONE>.json         each day's actual prices and every model's forecast
                      (median, and the 5/25/75/95% quantiles)

The models:
  naive        last week's prices on Monday, Saturday and Sunday, the day
               before's otherwise (Lago et al., 2021)
  lear         LEAR (Lago et al., 2021): a lasso regression per zone and hour on
               the prices of X-1, X-2, X-3 and X-7, the load forecasts of X,
               X-1 and X-7, the wind and solar forecasts of X-1 and X-7, the
               weekday, holidays and the gas and EUA prices; 728 days of data,
               refitted every 7 days
  timestral-*  Mistral's Timestral, zero-shot: the seven zones' prices
               forecast together from their history, with each zone's load
               forecast, the gas and EUA prices and the calendar known for day
               X, and the neighbours' prices (FR, CH, AT, SI, GR) and Italy's
               wind and solar forecasts as past covariates (up to X-1); "-ar"
               models see the prices alone
  ensemble     the average of LEAR's and Timestral's quantiles
  *-res        the same with the wind and solar forecasts of day X too
  *-wx         the same with weather forecasts for day X made before the gate
               (Open-Meteo, ECMWF IFS runs up to 00 UTC of X-1, etl/fetch_weather.py):
               the wind speed at the wind farms and the irradiance at the
               solar farms (and, for Timestral, the temperature in the cities)

Only inputs out before the MGP's 12:00 gate on X-1: Terna's day-ahead load
forecast for X is on ENTSO-E by about 11:30, its wind and solar forecasts only
at about 18:00 (seen on the dashboard's update runs, October 2026), so the
"-res" models have inputs a trader did not have from ENTSO-E: they show what a
morning wind and solar forecast would be worth.  The files also hold the
forecasts' latest revisions.  naive and lear get quantiles from their own
errors over the previous 182 days (conformal prediction, per zone and hour).

Each model's forecasts are cached (app/data/forecast/cache/<model>.npz), so
models can be run one at a time and the report rebuilt without rerunning them:

  forecast_backtest.py --models naive,lear,timestral-28m
  forecast_backtest.py --models timestral-209m
  forecast_backtest.py --report-only

Needs numpy, scipy, scikit-learn, torch and timestral (requirements-forecast.txt).
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compact  # noqa: E402
from forecast_data import NEIGHBOURS, ZONES, Data, from_slots, to_slots  # noqa: E402
from zones import DATA_DIR, ZONE_NAMES  # noqa: E402

OUTPUT_DIR = os.path.join(DATA_DIR, "forecast", "backtest")
CACHE_DIR = os.path.join(DATA_DIR, "forecast", "cache")

TEST_DAYS = 365
HORIZON = 25  # hours of the longest day; shorter days use the first 23 or 24

LEVELS = (0.01, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5,
          0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99)
MEDIAN = LEVELS.index(0.5)
# Quantiles kept in the zone files for the chart's bands.
BAND_LEVELS = (0.05, 0.25, 0.75, 0.95)

CONFORMAL_DAYS = 182
LEAR_WINDOW = 728
LEAR_REFIT_DAYS = 7

MODELS = {
    "naive": {"label": "Naive", "description": "Last week's prices on Mon, Sat, Sun; the day before's otherwise"},
    "lear": {"label": "LEAR", "res_ahead": False,
             "description": "Lasso regression per zone and hour (Lago et al., 2021), inputs out before the auction"},
    "timestral-28m-ar": {"label": "Timestral 28M, prices only", "size": "28M", "covariates": False, "context": 8192,
                         "description": "Timestral-28M zero-shot, the zones' prices alone, 8,192 hours of history"},
    "timestral-28m": {"label": "Timestral 28M", "size": "28M", "covariates": True, "res_ahead": False, "context": 4096,
                      "description": "Timestral-28M zero-shot with inputs out before the auction, 4,096 hours of history"},
    "timestral-28m-long": {"label": "Timestral 28M, long history", "size": "28M", "covariates": True, "res_ahead": False,
                           "context": 8192,
                           "description": "Timestral-28M zero-shot with inputs out before the auction, 8,192 hours of history"},
    "timestral-209m": {"label": "Timestral 209M", "size": "209M", "covariates": True, "res_ahead": False, "context": 4096,
                       "description": "Timestral-209M zero-shot with inputs out before the auction, 4,096 hours of history"},
    "ensemble": {"label": "LEAR + Timestral 209M", "parts": ("lear", "timestral-209m"),
                 "description": "Average of the quantiles of LEAR and Timestral 209M"},
    "lear-res": {"label": "LEAR, with the day's wind and solar", "res_ahead": True,
                 "description": "LEAR with the wind and solar forecasts of the day, published after the auction"},
    "timestral-28m-res": {"label": "Timestral 28M, with the day's wind and solar", "size": "28M", "covariates": True,
                          "res_ahead": True, "context": 4096,
                          "description": "Timestral-28M with the wind and solar forecasts of the day, published after the auction"},
    "lear-wx": {"label": "LEAR, with weather", "res_ahead": False, "weather": True,
                "description": "LEAR with the zone's wind and solar weather forecasts for the day, made before the auction"},
    "timestral-28m-wx": {"label": "Timestral 28M, with weather", "size": "28M", "covariates": True, "res_ahead": False,
                         "weather": True, "context": 4096,
                         "description": "Timestral-28M with wind, irradiance and temperature forecasts for the day, made before the auction"},
}

# Zones whose wind moves prices (the north's and the centre-north's is small).
WIND_ZONES = ("CSUD", "SUD", "CALA", "SICI", "SARD")
WEATHER_POINTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_points.json")


def weather_weights():
    """Each zone's solar MW and city population in etl/weather_points.json."""
    with open(WEATHER_POINTS, encoding="utf-8") as handle:
        points = json.load(handle)
    solar = {zone: sum(p["mw"] for p in items) for zone, items in points["solar"].items()}
    people = {zone: sum(p["population_m"] for p in items) for zone, items in points["cities"].items()}
    return solar, people


def weighted_italy(data, name, weights):
    """Italy's value of a weather series: the zones' weighted average."""
    total = sum(weights.values())
    return sum(data.weather_ahead(zone, name) * weight for zone, weight in weights.items()) / total

# Battery for the trading test: 1 MW / 2 MWh, 85% round trip, half full at
# midnight, at most one full cycle a day, scheduled on the forecast's median
# and paid the actual prices.
BATTERY = {"power_mw": 1.0, "energy_mwh": 2.0, "round_trip": 0.85, "soc_start": 0.5, "cycles_per_day": 1.0}


# ============================================================================
# DAILY MATRICES (24 clock-hour slots)
# ============================================================================


def slot_matrix(data, values):
    """(days, 24) slots of an hourly array over the axis."""
    return np.stack([to_slots(values[data.day_slice(day)]) for day in data.days])


def ffill_days(matrix):
    """Fill a day's missing slot with the day before's."""
    out = matrix.copy()
    for i in range(1, len(out)):
        missing = np.isnan(out[i])
        out[i, missing] = out[i - 1, missing]
    return out


def actual_prices(data, day):
    """(zones, hours) of a day."""
    return data.prices()[:, data.day_slice(day)]


def pad_hours(values, axis=-1):
    """Pad a day's hours to HORIZON with NaN along `axis`."""
    pad = HORIZON - values.shape[axis]
    if pad <= 0:
        return values
    widths = [(0, 0)] * values.ndim
    widths[axis] = (0, pad)
    return np.pad(values, widths, constant_values=np.nan)


# ============================================================================
# NAIVE AND LEAR
# ============================================================================


def naive_point(data, days):
    """{day: (zones, HORIZON)} point forecasts."""
    index = {day: i for i, day in enumerate(data.days)}
    prices = np.stack([slot_matrix(data, data.series[f"{zone}|price"]) for zone in ZONES], axis=1)  # (D, Z, 24)
    out = {}
    for day in days:
        i = index[day]
        source = i - 7 if day.weekday() in (0, 5, 6) else i - 1
        out[day] = pad_hours(from_slots(prices[source], data.hours[day]))
    return out


def lear_point(data, days, res_ahead=False, weather=False):
    """{day: (zones, HORIZON)} point forecasts, refitted every LEAR_REFIT_DAYS."""
    from joblib import Parallel, delayed

    results = Parallel(n_jobs=len(ZONES), verbose=0)(
        delayed(_lear_zone)(data, zone, days, res_ahead, weather) for zone in ZONES
    )
    out = {}
    for day in days:
        slots = np.stack([results[z][day] for z in range(len(ZONES))])
        out[day] = pad_hours(from_slots(slots, data.hours[day]))
    return out


def _lear_features(data, zone, res_ahead=False, weather=False):
    """(days, features) matrix and (days, 24) target of one zone; feature rows
    for day i use only what is known on day i-1 before the auction (and, with
    res_ahead, day i's wind and solar forecast, published after it)."""
    price = slot_matrix(data, data.series[f"{zone}|price"])
    load = ffill_days(slot_matrix(data, data.series[f"{zone}|load_forecast"]))
    res = ffill_days(slot_matrix(data, np.nan_to_num(data.series[f"{zone}|wind_forecast"])
                                 + np.nan_to_num(data.series[f"{zone}|solar_forecast"])))
    n = len(data.days)

    def lag(matrix, k):
        out = np.full_like(matrix, np.nan)
        out[k:] = matrix[:n - k]
        return out

    weekday = np.eye(7)[data.weekday]
    holiday = np.array([[float(data.calendar["holiday"][data.day_start[day]])] for day in data.days])
    fuel = np.column_stack([data._lagged(data.gas_daily), data._lagged(data.eua_daily)])
    res_lags = [res] if res_ahead else []
    if weather:
        res_lags += [slot_matrix(data, data.weather_ahead(zone, "wind_speed")),
                     slot_matrix(data, data.weather_ahead(zone, "ghi"))]
    continuous = np.hstack([lag(price, 1), lag(price, 2), lag(price, 3), lag(price, 7),
                            load, lag(load, 1), lag(load, 7), *res_lags, lag(res, 1), lag(res, 7), fuel])
    return continuous, np.hstack([weekday, holiday]), price


def _scale(train, apply):
    """Median/MAD scaling then asinh (the 'invariant' transform of Lago et al.)."""
    median = np.nanmedian(train, axis=0)
    mad = np.nanmedian(np.abs(train - median), axis=0) * 1.4826
    mad[~np.isfinite(mad) | (mad < 1e-6)] = 1.0
    return np.arcsinh((apply - median) / mad), median, mad


def _lear_zone(data, zone, days, res_ahead=False, weather=False):
    import warnings

    from sklearn.exceptions import ConvergenceWarning
    from sklearn.linear_model import Lasso, LassoLarsIC

    warnings.filterwarnings("ignore", category=ConvergenceWarning)
    continuous, dummies, price = _lear_features(data, zone, res_ahead, weather)
    index = {day: i for i, day in enumerate(data.days)}
    out = {}
    first = index[days[0]]
    for refit in range(first, index[days[-1]] + 1, LEAR_REFIT_DAYS):
        train = np.arange(max(0, refit - LEAR_WINDOW), refit)
        ok = train[~np.isnan(continuous[train]).any(axis=1) & ~np.isnan(price[train]).any(axis=1)]
        predict = [i for i in range(refit, min(refit + LEAR_REFIT_DAYS, index[days[-1]] + 1))]
        x_train, med, mad = _scale(continuous[ok], continuous[ok])
        x_new = np.arcsinh((np.nan_to_num(continuous[predict]) - med) / mad)
        x_train = np.hstack([x_train, dummies[ok]])
        x_new = np.hstack([x_new, dummies[predict]])
        y_train, y_med, y_mad = _scale(price[ok], price[ok])
        forecast = np.zeros((len(predict), 24))
        for h in range(24):
            alpha = LassoLarsIC(criterion="aic", max_iter=2500).fit(x_train, y_train[:, h]).alpha_
            model = Lasso(alpha=alpha, max_iter=2500).fit(x_train, y_train[:, h])
            forecast[:, h] = np.sinh(model.predict(x_new)) * y_mad[h] + y_med[h]
        for row, i in enumerate(predict):
            out[data.days[i]] = forecast[row]
    return out


def conformal(data, points, days):
    """Quantiles from the point forecast plus the quantiles of its errors over
    the previous CONFORMAL_DAYS days (per zone and clock hour).  Days without
    enough history are left out."""
    errors = {}
    for day, point in points.items():
        actual = actual_prices(data, day)
        n = data.hours[day]
        errors[day] = to_slots(actual - point[:, :n])  # (zones, 24)
    out = {}
    for day in days:
        past = [errors[d] for d in (day - timedelta(days=k) for k in range(1, CONFORMAL_DAYS + 1))
                if d in errors and not np.isnan(errors[d]).all()]
        if len(past) < CONFORMAL_DAYS // 2:
            continue
        q = np.nanquantile(np.stack(past), LEVELS, axis=0)  # (Q, zones, 24)
        q = from_slots(np.moveaxis(q, 0, -1).swapaxes(-1, -2), data.hours[day])  # (zones, Q, hours)
        q = np.moveaxis(q, 1, -1)  # (zones, hours, Q)
        out[day] = pad_hours(points[day][:, :data.hours[day], None] + q, axis=1)
    return out


# ============================================================================
# TIMESTRAL
# ============================================================================


def timestral_rows(data, res_ahead=False, weather=False):
    """(known, past) covariate rows over the whole axis: each zone's load
    forecast, the gas and EUA prices and the calendar known ahead; the
    neighbours' prices and Italy's wind and solar forecasts up to the day
    before (known ahead too with res_ahead)."""
    wind = np.nansum([data.series[f"{zone}|wind_forecast"] for zone in ZONES], axis=0)
    solar = np.nansum([data.series[f"{zone}|solar_forecast"] for zone in ZONES], axis=0)
    known = [data.series[f"{zone}|load_forecast"] for zone in ZONES]
    past = [data.series[f"{code}|price"] for code in NEIGHBOURS]
    (known if res_ahead else past).extend([wind, solar])
    if weather:
        solar_mw, people = weather_weights()
        known += [data.weather_ahead(zone, "wind_speed") for zone in WIND_ZONES]
        known += [weighted_italy(data, "ghi", solar_mw), weighted_italy(data, "temperature", people)]
    known += [data.gas, data.eua, data.calendar["holiday"], data.calendar["saturday"], data.calendar["sunday"]]
    return np.stack(known), np.stack(past)


def window(values, start, length):
    """values[..., start:start+length], NaN where it reaches before or after the axis."""
    end = start + length
    inside = values[..., max(start, 0):min(end, values.shape[-1])]
    before = np.full(values.shape[:-1] + (max(0, -start),), np.nan)
    after = np.full(values.shape[:-1] + (max(0, end - values.shape[-1]),), np.nan)
    return np.concatenate([before, inside, after], axis=-1)


def timestral_inputs(data, day, context, covariates, prices=None, rows=None, res_ahead=False):
    """Context (zones, context) and, with covariates, known (C, context+HORIZON)
    and past (C, context) rows for forecasting `day`."""
    prices = data.prices() if prices is None else prices
    start = data.day_start[day]
    ctx = window(prices, start - context, context)
    if not covariates:
        return ctx, None, None
    known_all, past_all = timestral_rows(data, res_ahead) if rows is None else rows
    known = window(known_all, start - context, context + HORIZON).copy()
    # Beyond the day's own hours the next day begins: unknown, masked.
    known[:, context + data.hours[day]:] = np.nan
    past = window(past_all, start - context, context)
    return ctx, known, past


def timestral_quantiles(data, days, spec, batch=4):
    """{day: (zones, HORIZON, Q)} quantiles."""
    import torch
    from timestral import TimestralForecaster

    torch.set_num_threads(max(1, os.cpu_count() // 2))
    forecaster = TimestralForecaster.from_pretrained(f"mistralai/Timestral-{spec['size']}")
    assert tuple(round(level, 2) for level in forecaster.quantile_levels) == LEVELS
    prices = data.prices()
    rows = timestral_rows(data, spec["res_ahead"], spec.get("weather", False)) if spec["covariates"] else None
    out = {}
    started = time.time()
    for i in range(0, len(days), batch):
        chunk = days[i:i + batch]
        inputs = [timestral_inputs(data, day, spec["context"], spec["covariates"], prices, rows) for day in chunk]
        kwargs = {}
        if spec["covariates"]:
            kwargs = {"known_covariates": np.stack([k for _, k, _ in inputs]),
                      "past_covariates": np.stack([p for _, _, p in inputs])}
        forecast = forecaster.forecast(np.stack([c for c, _, _ in inputs]), HORIZON, **kwargs)
        quantiles = forecast.quantiles.cpu().numpy()
        for j, day in enumerate(chunk):
            q = quantiles[j].astype(np.float64)
            q[:, data.hours[day]:] = np.nan
            out[day] = q
        done = i + len(chunk)
        rate = (time.time() - started) / done
        print(f"    {done}/{len(days)} days, {rate:.1f} s a day, {rate * (len(days) - done) / 60:.0f} min left", flush=True)
    return out


# ============================================================================
# RUN AND CACHE
# ============================================================================


def cache_path(model):
    return os.path.join(CACHE_DIR, f"{model}.npz")


def save_cache(model, point, quantiles):
    os.makedirs(CACHE_DIR, exist_ok=True)
    days = sorted(point)
    np.savez_compressed(
        cache_path(model),
        days=np.array([day.isoformat() for day in days]),
        point=np.stack([point[day] for day in days]),
        quantiles=np.stack([quantiles.get(day, np.full((len(ZONES), HORIZON, len(LEVELS)), np.nan)) for day in days]),
    )


def load_cache(model):
    path = cache_path(model)
    if not os.path.exists(path):
        return None, None
    with np.load(path) as stored:
        days = [date.fromisoformat(d) for d in stored["days"]]
        point = dict(zip(days, stored["point"]))
        quantiles = {day: q for day, q in zip(days, stored["quantiles"]) if not np.isnan(q).all()}
    return point, quantiles


def run_model(data, model, days):
    spec = MODELS[model]
    print(f"  {model}: {len(days)} days", flush=True)
    started = time.time()
    if model == "naive":
        warm = [data.days[data.days.index(days[0]) - k] for k in range(CONFORMAL_DAYS, 0, -1)] + days
        point = naive_point(data, warm)
        quantiles = conformal(data, point, days)
    elif model.startswith("lear"):
        warm = [data.days[data.days.index(days[0]) - k] for k in range(CONFORMAL_DAYS, 0, -1)] + days
        point = lear_point(data, warm, spec["res_ahead"], spec.get("weather", False))
        quantiles = conformal(data, point, days)
    elif "parts" in spec:
        parts = [load_cache(part) for part in spec["parts"]]
        if any(p is None for p, _ in parts):
            raise SystemExit(f"{model} needs {spec['parts']} run first.")
        quantiles = {day: np.mean([q[day] for _, q in parts], axis=0) for day in days
                     if all(day in q for _, q in parts)}
        point = {day: q[..., MEDIAN] for day, q in quantiles.items()}
    else:
        quantiles = timestral_quantiles(data, days, spec)
        point = {day: q[..., MEDIAN] for day, q in quantiles.items()}
    point = {day: point[day] for day in days if day in point}
    save_cache(model, point, quantiles)
    print(f"  {model}: done in {(time.time() - started) / 60:.1f} min", flush=True)


# ============================================================================
# SCORES
# ============================================================================


def pinball(actual, quantiles):
    """Mean pinball loss per level: actual (...), quantiles (..., Q) -> (Q,)."""
    levels = np.array(LEVELS)
    diff = actual[..., None] - quantiles
    loss = np.maximum(levels * diff, (levels - 1) * diff)
    return np.nanmean(loss.reshape(-1, len(LEVELS)), axis=0)


def crps(actual, quantiles):
    """CRPS approximated from the 21 quantiles: twice the pinball loss
    integrated over the levels (trapezoid rule)."""
    loss = pinball(actual, quantiles)
    return float(2 * np.trapezoid(loss, LEVELS))


def battery_matrices(hours):
    """The battery LP's constraints for a day of `hours` hours:
    variables [charge (n), discharge (n), state of charge (n)]."""
    n = hours
    b = BATTERY
    eta = b["round_trip"] ** 0.5
    soc0 = b["soc_start"] * b["energy_mwh"]
    a_eq = np.zeros((n + 1, 3 * n))
    b_eq = np.zeros(n + 1)
    for t in range(n):
        a_eq[t, 2 * n + t] = 1.0
        if t > 0:
            a_eq[t, 2 * n + t - 1] = -1.0
        a_eq[t, t] = -eta
        a_eq[t, n + t] = 1.0 / eta
    b_eq[0] = soc0
    a_eq[n, 3 * n - 1] = 1.0  # back to the starting charge at midnight
    b_eq[n] = soc0
    a_ub = np.zeros((1, 3 * n))
    a_ub[0, n:2 * n] = 1.0
    b_ub = np.array([b["cycles_per_day"] * b["energy_mwh"]])
    bounds = [(0, b["power_mw"])] * (2 * n) + [(0, b["energy_mwh"])] * n
    return a_eq, b_eq, a_ub, b_ub, bounds


def battery_schedule(prices, matrices):
    """Net discharge (MW per hour) maximising revenue at `prices`."""
    from scipy.optimize import linprog

    n = len(prices)
    a_eq, b_eq, a_ub, b_ub, bounds = matrices
    cost = np.concatenate([prices, -prices, np.zeros(n)])
    result = linprog(cost, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if not result.success:
        return np.zeros(n)
    return result.x[n:2 * n] - result.x[:n]


def score(data, days, forecasts):
    """{model: {zone or "ALL": scores}} and the monthly MAE."""
    matrices = {n: battery_matrices(n) for n in (23, 24, 25)}
    oracle = {}
    for day in days:
        actual = actual_prices(data, day)
        oracle[day] = [float(actual[z] @ battery_schedule(actual[z], matrices[data.hours[day]]))
                       for z in range(len(ZONES))]

    metrics, monthly = {}, {}
    for model, (point, quantiles) in forecasts.items():
        per_zone = defaultdict(lambda: defaultdict(list))
        by_month = defaultdict(lambda: defaultdict(list))
        for day in days:
            if day not in point:
                continue
            actual = actual_prices(data, day)
            n = data.hours[day]
            pred = point[day][:, :n]
            q = quantiles.get(day)
            for z, zone in enumerate(ZONES):
                if np.isnan(pred[z]).any():
                    continue
                errors = actual[z] - pred[z]
                trade = float(actual[z] @ battery_schedule(pred[z], matrices[n]))
                for key in (zone, "ALL"):
                    bucket = per_zone[key]
                    bucket["abs"].extend(np.abs(errors))
                    bucket["sq"].extend(errors ** 2)
                    bucket["trade"].append(trade)
                    bucket["oracle"].append(oracle[day][z])
                    if q is not None:
                        bucket["actual"].extend(actual[z])
                        bucket["q"].extend(q[z, :n])
                by_month[day.strftime("%Y-%m")][zone].extend(np.abs(errors))
                by_month[day.strftime("%Y-%m")]["ALL"].extend(np.abs(errors))

        metrics[model] = {}
        for key, bucket in per_zone.items():
            entry = {
                "mae": round(float(np.mean(bucket["abs"])), 2),
                "rmse": round(float(np.sqrt(np.mean(bucket["sq"]))), 2),
                "days": len(bucket["trade"]) if key != "ALL" else len(bucket["trade"]) // len(ZONES),
                "battery_eur_mw_year": round(float(np.mean(bucket["trade"])) * 365 / BATTERY["power_mw"]),
                "battery_capture": round(float(np.sum(bucket["trade"]) / np.sum(bucket["oracle"])), 4),
            }
            if bucket["q"]:
                actual_arr = np.array(bucket["actual"])
                q_arr = np.array(bucket["q"])
                entry["crps"] = round(crps(actual_arr, q_arr), 2)
                inside = (actual_arr >= q_arr[:, LEVELS.index(0.05)]) & (actual_arr <= q_arr[:, LEVELS.index(0.95)])
                entry["coverage90"] = round(float(np.mean(inside)), 4)
            metrics[model][key] = entry
        monthly[model] = {month: {key: round(float(np.mean(v)), 2) for key, v in zones.items()}
                          for month, zones in sorted(by_month.items())}

    oracle_entry = {}
    for z, zone in enumerate(ZONES):
        oracle_entry[zone] = round(float(np.mean([oracle[d][z] for d in days])) * 365 / BATTERY["power_mw"])
    oracle_entry["ALL"] = round(float(np.mean([np.mean(oracle[d]) for d in days])) * 365 / BATTERY["power_mw"])
    return metrics, monthly, oracle_entry


# ============================================================================
# REPORT
# ============================================================================


def hour_rows(iso, times, values, digits=1):
    """Rows of one day's hourly values, leaving out the missing ones."""
    return [{"date": iso, "time": t, "value": round(float(v), digits)}
            for t, v in zip(times, values) if np.isfinite(v)]


def write_report(data, days, models):
    forecasts = {}
    for model in models:
        point, quantiles = load_cache(model)
        if point is not None:
            forecasts[model] = (point, quantiles)
    if not forecasts:
        raise SystemExit("No cached forecasts: run some models first.")
    # Every model is scored on the same days: those all of them forecast.
    common = [day for day in days if all(day in point for point, _ in forecasts.values())
              and not np.isnan(actual_prices(data, day)).any()]
    print(f"  Scoring {len(forecasts)} models on {len(common)} days", flush=True)
    metrics, monthly, oracle = score(data, common, forecasts)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    summary = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "description": (
            "Backtest of the day-ahead (MGP) price forecast of the seven Italian bidding zones: each "
            "day's 24 hourly prices forecast as on the day before, scored against the ENTSO-E prices."
        ),
        "test_start": common[0].isoformat(),
        "test_end": common[-1].isoformat(),
        "days": len(common),
        "zones": [{"code": zone, "name": ZONE_NAMES[zone]} for zone in ZONES],
        "models": [{"id": model, "label": MODELS[model]["label"], "description": MODELS[model]["description"]}
                   for model in forecasts],
        "levels": list(BAND_LEVELS),
        "battery": BATTERY,
        "battery_oracle_eur_mw_year": oracle,
        "metrics": metrics,
        "monthly_mae": monthly,
    }
    with open(os.path.join(OUTPUT_DIR, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, separators=(",", ":"))

    for z, zone in enumerate(ZONES):
        series = {"actual": []}
        for model, (point, quantiles) in forecasts.items():
            series[f"{model}|q50"] = []
            if quantiles:
                for level in BAND_LEVELS:
                    series[f"{model}|q{round(level * 100):02d}"] = []
        for day in common:
            n = data.hours[day]
            actual = actual_prices(data, day)[z]
            times = [f"{h:02d}:00" for h in range(n)]
            iso = day.isoformat()
            series["actual"].extend(hour_rows(iso, times, actual, 2))
            for model, (point, quantiles) in forecasts.items():
                series[f"{model}|q50"].extend(hour_rows(iso, times, point[day][z, :n]))
                q = quantiles.get(day)
                if q is None:
                    continue
                for level in BAND_LEVELS:
                    series[f"{model}|q{round(level * 100):02d}"].extend(
                        hour_rows(iso, times, q[z, :n, LEVELS.index(level)]))
        payload = {
            "zone": zone,
            "description": f"Hourly day-ahead prices of {ZONE_NAMES[zone]} (EUR/MWh): actual and forecast.",
            "series": {name: compact.encode_series(rows, "hourly") for name, rows in series.items() if rows},
        }
        compact.dump(payload, os.path.join(OUTPUT_DIR, f"{zone}.json"))

    width = max(len(model) for model in metrics)
    print(f"  {'model':<{width}}  MAE   RMSE  CRPS  cover90  battery capture")
    for model, scores in metrics.items():
        s = scores["ALL"]
        print(f"  {model:<{width}}  {s['mae']:5.2f} {s['rmse']:6.2f} {s.get('crps', float('nan')):5.2f}"
              f"  {s.get('coverage90', float('nan')):6.3f}   {s['battery_capture']:.3f}")


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", default="naive,lear,timestral-28m-ar,timestral-28m,timestral-209m,ensemble",
                        help=f"comma-separated, of {', '.join(MODELS)}")
    parser.add_argument("--start", help="first test day YYYY-MM-DD (default: TEST_DAYS before the last)")
    parser.add_argument("--end", help="last test day YYYY-MM-DD (default: the last day with prices)")
    parser.add_argument("--report-only", action="store_true", help="rebuild the report from the cache")
    args = parser.parse_args()

    data = Data()
    end = date.fromisoformat(args.end) if args.end else data.last_price_day()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=TEST_DAYS - 1)
    days = [day for day in data.days if start <= day <= end]
    print(f"Test period {start} to {end} ({len(days)} days)", flush=True)

    if not args.report_only:
        for model in args.models.split(","):
            run_model(data, model.strip(), days)
    write_report(data, days, [model for model in MODELS if os.path.exists(cache_path(model))])


if __name__ == "__main__":
    main()
