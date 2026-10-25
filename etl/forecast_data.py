"""
The price forecast's data, as arrays: every series of
app/data/forecast/inputs.json (etl/fetch_forecast_inputs.py) laid out on one
unbroken hourly axis from 1 January 2021, Italian market time, plus the gas
and EUA prices and the calendar on the same axis.

A market day has 23, 24 or 25 hours (DST); its hours are labelled by elapsed
time since local midnight, so the days laid end to end are consecutive UTC
hours.  Data.day_start[d] is the axis position of day d's first hour.

Used by etl/forecast_backtest.py and the daily forecast.
"""

import os
from datetime import date, datetime, timedelta, timezone

import numpy as np

import compact
from entsoe_api import local_midnight_utc
from zones import DATA_DIR, ZONE_DOMAINS

INPUTS_PATH = os.path.join(DATA_DIR, "forecast", "inputs.json")
GAS_PATH = os.path.join(DATA_DIR, "gas_prices.json")
WEATHER_PATH = os.path.join(DATA_DIR, "forecast", "weather.json")

START = date(2021, 1, 1)
ZONES = list(ZONE_DOMAINS)
NEIGHBOURS = ("FR", "CH", "AT", "SI", "GR")

# Fuel and carbon prices enter with this lag in days: the forecast for day X
# is made on X-1 before the auction, when X-2's settled price is the latest
# sure to be out (Lipiecki and Weron, 2026, lag them the same way).
FUEL_LAG_DAYS = 2

# Days over which solar_share fits each zone's solar output to irradiance.
SOLAR_WINDOW_DAYS = 60


def day_hours(day):
    return int((local_midnight_utc(day + timedelta(days=1)) - local_midnight_utc(day)).total_seconds() // 3600)


def easter(year):
    """Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


def is_holiday(day):
    """Italy's national holidays (4 October again from 2026, law 151/2025)."""
    fixed = {(1, 1), (1, 6), (4, 25), (5, 1), (6, 2), (8, 15), (11, 1), (12, 8), (12, 25), (12, 26)}
    if day.year >= 2026:
        fixed.add((10, 4))
    return (day.month, day.day) in fixed or day == easter(day.year) + timedelta(days=1)


class Data:
    """
    days        market days from START to the last day with any input
    day_start   {day: axis position of its first hour}
    hours       {day: its number of hours}
    series      {"NORD|price": float array over the axis, NaN = missing, ...}
    gas, eua    daily prices lagged FUEL_LAG_DAYS, held over the day's hours
    calendar    {"holiday", "saturday", "sunday"}: 0/1 arrays over the axis
    weather     {"<ZONE>|wind_speed|d<k>", ...}: Open-Meteo forecasts made k days
                before each hour (etl/fetch_weather.py), when on file

    until: a day the axis reaches at least (the day to forecast, before any
    input for it is out).  latest_from: an axis position from which the
    weather is the latest run's (d0) instead of the one made the day before:
    the live forecast's own day.
    """

    def __init__(self, inputs_path=INPUTS_PATH, gas_path=GAS_PATH, weather_path=WEATHER_PATH, until=None):
        payload = compact.load(inputs_path)
        hourly = payload["hourly"]
        last = max(date.fromisoformat(rows[-1]["date"]) for rows in hourly.values() if rows)
        if until and until > last:
            last = until
        self.days = [START + timedelta(days=i) for i in range((last - START).days + 1)]

        self.day_start, self.hours = {}, {}
        position = 0
        for day in self.days:
            self.day_start[day] = position
            self.hours[day] = day_hours(day)
            position += self.hours[day]
        self.length = position
        by_iso = {day.isoformat(): self.day_start[day] for day in self.days}

        self.series = {}
        for name, rows in hourly.items():
            values = np.full(self.length, np.nan, dtype=np.float64)
            for row in rows:
                start = by_iso.get(row["date"])
                if start is not None:
                    hours, minutes = row["time"].split(":")
                    values[start + int(hours) + int(minutes) // 60] = row["value"]
            self.series[name] = values

        eua = {row["date"]: row["value"] for row in payload.get("daily", {}).get("eua", [])}
        self.eua_prices = eua
        self.gas_prices = load_gas(gas_path)
        self.eua_daily = self._daily_filled(eua)
        self.gas_daily = self._daily_filled(self.gas_prices)
        self.gas = self._hourly(self._lagged(self.gas_daily))
        self.eua = self._hourly(self._lagged(self.eua_daily))

        weekday = np.array([day.weekday() for day in self.days])
        self.calendar = {
            "holiday": self._hourly(np.array([float(is_holiday(day)) for day in self.days])),
            "saturday": self._hourly((weekday == 5).astype(float)),
            "sunday": self._hourly((weekday == 6).astype(float)),
        }
        self.weekday = weekday

        self.weather = {}
        if weather_path and os.path.exists(weather_path):
            for name, rows in compact.load(weather_path).get("hourly", {}).items():
                values = np.full(self.length, np.nan, dtype=np.float64)
                for row in rows:
                    start = by_iso.get(row["date"])
                    if start is not None:
                        hours, minutes = row["time"].split(":")
                        values[start + int(hours) + int(minutes) // 60] = row["value"]
                self.weather[name] = values
        self.weather_cache = {}
        self.latest_from = None

    def weather_ahead(self, zone, name, days_ahead=1):
        """A weather series as forecast `days_ahead` days before each day, before
        its 12:00 auction gate, so from the runs up to 00 UTC of that day: an
        hour takes the forecast made days_ahead days (24 h each) before it
        until 06 UTC of its day, and the one a day older after.  NaN where not
        on file; from latest_from on, the latest run's."""
        key = (zone, name, days_ahead, self.latest_from)
        if key not in self.weather_cache:
            early = self.weather.get(f"{zone}|{name}|d{days_ahead}")
            late = self.weather.get(f"{zone}|{name}|d{days_ahead + 1}")
            out = np.full(self.length, np.nan)
            if early is not None and late is not None:
                for day in self.days:
                    start, end = self.day_start[day], self.day_start[day] + self.hours[day]
                    cut = start + hours_to_six_utc(day)
                    out[start:cut] = early[start:cut]
                    out[cut:end] = late[cut:end]
            latest = self.weather.get(f"{zone}|{name}|d0")
            if self.latest_from is not None and latest is not None:
                out[self.latest_from:] = latest[self.latest_from:]
            self.weather_cache[key] = out
        return self.weather_cache[key]

    def solar_share(self, zone, days_ahead=1, window_days=SOLAR_WINDOW_DAYS):
        """The zone's expected solar output as a share of its load forecast,
        hour by hour: irradiance as forecast before the gate (weather_ahead)
        times a scale fitted, for each day, on Terna's day-ahead solar forecast
        against that irradiance over the window_days days before it (all out by
        the morning before the day's auction), over Terna's load forecast.
        Near zero at night; high at sunny midday in a light-load zone, the hours
        that clear near zero."""
        key = ("solar_share", zone, days_ahead, window_days, self.latest_from)
        if key in self.weather_cache:
            return self.weather_cache[key]
        ghi = self.weather_ahead(zone, "ghi", days_ahead)
        solar = self.series[f"{zone}|solar_forecast"]
        load = self.series[f"{zone}|load_forecast"]
        both = np.isfinite(ghi) & np.isfinite(solar) & (ghi > 0)
        sg, gg = np.where(both, solar * ghi, 0.0), np.where(both, ghi * ghi, 0.0)
        starts = [self.day_start[day] for day in self.days]
        day_sg = np.add.reduceat(sg, starts)
        day_gg = np.add.reduceat(gg, starts)
        cum_sg, cum_gg = np.concatenate([[0], np.cumsum(day_sg)]), np.concatenate([[0], np.cumsum(day_gg)])
        out = np.full(self.length, np.nan)
        for i, day in enumerate(self.days):
            # Days i - window_days .. i - 1 (the day before is out on the morning of the auction).
            first = max(0, i - window_days)
            denominator = cum_gg[i] - cum_gg[first]
            if denominator <= 0:
                continue
            scale = (cum_sg[i] - cum_sg[first]) / denominator
            span = self.day_slice(day)
            with np.errstate(divide="ignore", invalid="ignore"):
                out[span] = scale * ghi[span] / load[span]
        self.weather_cache[key] = out
        return out

    def _daily_filled(self, values):
        """{iso date: value} -> array over self.days, carried forward over
        days without a value (weekends, days without an auction)."""
        out = np.full(len(self.days), np.nan)
        last = np.nan
        for i, day in enumerate(self.days):
            value = values.get(day.isoformat())
            if value is not None:
                last = value
            out[i] = last
        return out

    @staticmethod
    def _lagged(daily):
        out = np.full_like(daily, np.nan)
        out[FUEL_LAG_DAYS:] = daily[:-FUEL_LAG_DAYS]
        return out

    def _hourly(self, daily):
        return np.repeat(daily, [self.hours[day] for day in self.days])

    def day_slice(self, day):
        start = self.day_start[day]
        return slice(start, start + self.hours[day])

    def prices(self, zones=ZONES):
        return np.stack([self.series[f"{zone}|price"] for zone in zones])

    def last_price_day(self):
        """The last day every zone has a full day of prices."""
        for day in reversed(self.days):
            block = self.prices()[:, self.day_slice(day)]
            if not np.isnan(block).any():
                return day
        return None


def hours_to_six_utc(day):
    """Hours from local midnight of `day` to 06:00 UTC of that date."""
    six = datetime(day.year, day.month, day.day, 6, tzinfo=timezone.utc)
    return int((six - local_midnight_utc(day)).total_seconds() // 3600)


def load_gas(path=GAS_PATH):
    """PSV price {iso gas day: EUR/MWh}: GME's IG Index, else the MGP-GAS
    auction price (app/data/gas_prices.json)."""
    if not os.path.exists(path):
        return {}
    series = compact.load(path).get("series", {})
    prices = {row["date"]: row["value"] for row in series.get("mgp_gas", [])}
    prices.update({row["date"]: row["value"] for row in series.get("igi", [])})
    return prices


# ============================================================================
# 24-SLOT DAYS
# ============================================================================
# The regression baselines work on 24 clock hours a day: the spring day's
# missing hour is the average of its neighbours, the autumn day's repeated
# hour the average of its two.


def to_slots(values):
    """A day's hours (last axis) -> 24 clock-hour slots."""
    values = np.asarray(values, dtype=float)
    n = values.shape[-1]
    if n == 24:
        return values
    if n == 23:
        middle = (values[..., 1:2] + values[..., 2:3]) / 2
        return np.concatenate([values[..., :2], middle, values[..., 2:]], axis=-1)
    if n == 25:
        middle = (values[..., 2:3] + values[..., 3:4]) / 2
        return np.concatenate([values[..., :2], middle, values[..., 4:]], axis=-1)
    raise ValueError(f"A day of {n} hours")


def from_slots(slots, n):
    """24 clock-hour slots (last axis) -> a day of n hours."""
    slots = np.asarray(slots, dtype=float)
    if n == 24:
        return slots
    if n == 23:
        return np.concatenate([slots[..., :2], slots[..., 3:]], axis=-1)
    if n == 25:
        return np.concatenate([slots[..., :3], slots[..., 2:]], axis=-1)
    raise ValueError(f"A day of {n} hours")
