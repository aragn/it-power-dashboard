"""
Stack model: each zone's wind and solar output for the day to forecast,
from the weather forecast made before the gate (Terna's own wind and solar
forecasts come out only after it, so the stack cannot use them).

  solar   per UTC hour, a scale fitted on the zone's actual solar output
          against the irradiance forecast over the WINDOW_DAYS before
          (output = scale x irradiance): the hour's sun angle and the
          panels' tilt are in the scale, and so is the capacity added
  wind    a cubic in the forecast wind speed at the zone's wind farms
          (clipped at WIND_CLIP m/s), fitted the same way, bounded by the
          largest output of the window

The actual output is ENTSO-E's (16.1.B&C, zones/<ZONE>/generation_load.json,
hourly from 2025); a day's fit uses the days up to two days before it (the
day before is not complete on the morning of the auction).

  stack_res.py [--from 2025-10-22 --to 2026-10-21]   scores against Terna's
      day-ahead forecasts (ENTSO-E 14.1.D, out after the gate) and prints the
      MAE per zone
"""

import argparse
import os
import sys
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compact  # noqa: E402
from entsoe_api import local_midnight_utc  # noqa: E402
from forecast_data import START, ZONES, Data  # noqa: E402
from zones import DATA_DIR  # noqa: E402

WINDOW_DAYS = 60
WIND_CLIP = 20.0
SOLAR, WIND = ("B16",), ("B19", "B18")   # ENTSO-E production types: solar; wind onshore, offshore


def actual_generation(data, zone, types):
    """The zone's actual output of the production types, MW on data's axis (NaN where missing)."""
    path = os.path.join(DATA_DIR, "zones", zone, "generation_load.json")
    hourly = compact.load(path)["generation"]["hourly"]
    by_iso = {day.isoformat(): data.day_start[day] for day in data.days}
    out = np.full(data.length, np.nan)
    for kind in types:
        for point in (hourly.get(kind) or {}).get("points", []):
            start = by_iso.get(point["date"])
            if start is None or point["value"] is None:
                continue
            hours, minutes = point["time"].split(":")
            position = start + int(hours) + int(minutes) // 60
            out[position] = (0.0 if np.isnan(out[position]) else out[position]) + point["value"]
    return out


def utc_hours(data):
    """The UTC hour of each axis position (the axis starts at START's local midnight)."""
    return (local_midnight_utc(START).hour + np.arange(data.length)) % 24


def fit_days(data, day, window_days=WINDOW_DAYS):
    """Axis slice of the days fitted for `day`: window_days days ending two days before it."""
    last = day - timedelta(days=2)
    first = last - timedelta(days=window_days - 1)
    if first < data.days[0]:
        return None
    return slice(data.day_start[first], data.day_start[last] + data.hours[last])


def solar_forecast(data, zone, days, actual):
    ghi = data.weather_ahead(zone, "ghi", 1)
    hour = utc_hours(data)
    out = np.full(data.length, np.nan)
    for day in days:
        window = fit_days(data, day)
        if window is None:
            continue
        span = data.day_slice(day)
        g, y, h = ghi[window], actual[window], hour[window]
        ok = np.isfinite(g) & np.isfinite(y) & (g > 0)
        num = np.bincount(h[ok], weights=g[ok] * y[ok], minlength=24)
        den = np.bincount(h[ok], weights=g[ok] * g[ok], minlength=24)
        scale = np.divide(num, den, out=np.zeros(24), where=den > 0)
        out[span] = np.where(ghi[span] > 0, scale[hour[span]] * ghi[span], 0.0)
    return out


def wind_forecast(data, zone, days, actual):
    speed = np.minimum(data.weather_ahead(zone, "wind_speed", 1), WIND_CLIP)
    out = np.full(data.length, np.nan)
    for day in days:
        window = fit_days(data, day)
        if window is None:
            continue
        s, y = speed[window], actual[window]
        ok = np.isfinite(s) & np.isfinite(y)
        if ok.sum() < 24 * 20:
            continue
        x = np.stack([np.ones(ok.sum()), s[ok], s[ok] ** 2, s[ok] ** 3], axis=1)
        coef, *_ = np.linalg.lstsq(x, y[ok], rcond=None)
        span = data.day_slice(day)
        v = speed[span]
        out[span] = np.clip(coef[0] + coef[1] * v + coef[2] * v ** 2 + coef[3] * v ** 3, 0, np.nanmax(y[ok]) * 1.05)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="first", default="2025-10-22")
    parser.add_argument("--to", dest="last", default="2026-10-21")
    args = parser.parse_args()
    data = Data()
    first, last = date.fromisoformat(args.first), date.fromisoformat(args.last)
    days = [day for day in data.days if first <= day <= last]
    span = slice(data.day_start[days[0]], data.day_start[days[-1]] + data.hours[days[-1]])
    print(f"{days[0]} - {days[-1]}: MAE in MW (and as a share of the mean output); "
          "Terna's forecast is out only after the gate")
    for kind, types, model in (("solar", SOLAR, solar_forecast), ("wind", WIND, wind_forecast)):
        print(f"{kind}:")
        for zone in ZONES:
            actual = actual_generation(data, zone, types)
            ours = model(data, zone, days, actual)
            terna = data.series[f"{zone}|{kind}_forecast"]
            a, o, t = actual[span], ours[span], terna[span]
            ok = np.isfinite(a) & np.isfinite(o) & np.isfinite(t)
            if not ok.any():
                print(f"  {zone}: no data")
                continue
            mean = a[ok].mean()
            mae_o, mae_t = np.abs(o[ok] - a[ok]).mean(), np.abs(t[ok] - a[ok]).mean()
            print(f"  {zone:5s} mean {mean:7.0f}  ours {mae_o:6.0f} ({mae_o / mean:5.1%})  "
                  f"Terna {mae_t:6.0f} ({mae_t / mean:5.1%})  hours {ok.sum()}")


if __name__ == "__main__":
    main()
