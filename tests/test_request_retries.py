"""A reply cut off midway is tried again, like a dropped connection.  Run: python -m pytest tests"""

import os
import sys
from datetime import date

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import entsoe_api  # noqa: E402
import fetch_jao_spreads  # noqa: E402
import fetch_weather  # noqa: E402
import fetch_zonal  # noqa: E402

DAY = date(2026, 11, 6)


class SecondCall(Exception):
    """Raised by the second request: the first was tried again."""


class Flaky:
    """Cut off on the first request; the second ends the test."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise requests.exceptions.ChunkedEncodingError("Connection broken")
        raise SecondCall

    get = post = __call__


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)


def test_gme(monkeypatch):
    flaky = Flaky()
    monkeypatch.setattr(fetch_zonal.requests, "post", flaky)
    with pytest.raises(SecondCall):
        fetch_zonal.request_chunk("token", DAY, DAY)


def test_entsoe(monkeypatch):
    flaky = Flaky()
    monkeypatch.setattr(entsoe_api.requests, "get", flaky)
    with pytest.raises(SecondCall):
        entsoe_api.request_entsoe("token", {"documentType": "A44"})


def test_jao():
    with pytest.raises(SecondCall):
        fetch_jao_spreads.jao_get(Flaky(), "shadowAuctionSpreads", DAY, DAY)


def test_open_meteo():
    with pytest.raises(SecondCall):
        fetch_weather.request_points(Flaky(), [{"lat": 45, "lon": 9}], "wind_speed_100m", DAY, DAY)
