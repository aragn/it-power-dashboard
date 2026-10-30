"""Checks for build_capacity_market_storage (the strike price and the units' capacity market).  Run: python -m pytest tests"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import build_capacity_market_storage as cms  # noqa: E402


def test_strike_follows_arera_199_2024():
    # 320 Smc/MWh x (35 EUR/MWh at 0.0381 GJ/Smc + charges + I_GN) + 0.7162 x 70 + disposal, dispatch 2%, other 5.
    gas = 320 * (35 * 0.0381 / 3.6 + cms.GAS_CHARGES + 0.05)
    base = gas + 0.7162 * 70 + 0.006
    assert abs(cms.strike(35, 70, 2025) - (base * 1.02 + 5)) < 1e-9
    # Dispatching is at least 3 EUR/MWh.
    assert abs(cms.strike(0, 0, 2026) - (320 * (cms.GAS_CHARGES + 0.05) + 0.006 + 3 + 5)) < 1e-9


def test_strike_heat_rate_falls_to_303_from_2028():
    assert cms.strike(35, 70, 2028) < cms.strike(35, 70, 2027)
    assert cms.strike(35, 70, 2030) == cms.strike(35, 70, 2028)


def test_eua_price_is_the_last_auction_before_the_day():
    auctions = [{"date": "2025-01-07", "price": 70}, {"date": "2025-01-09", "price": 72}]
    prices = cms.eua_by_day(auctions, date(2025, 1, 7), date(2025, 1, 11))
    assert prices[date(2025, 1, 7)] is None
    assert prices[date(2025, 1, 8)] == 70
    assert prices[date(2025, 1, 9)] == 70
    assert prices[date(2025, 1, 10)] == 72


def test_active_contracts_of_a_year():
    contracts = [[2024, 2038, 136, 55000, "listed"], [2025, 2025, 10, 45000, "existing"]]
    assert cms.active(contracts, 2025) == (146, 136 * 55000 + 10 * 45000)
    assert cms.active(contracts, 2026) == (136, 136 * 55000)
    assert cms.active(contracts, 2023) == (0, 0)


def test_implied_premium_of_a_participant_s_new_capacity():
    auction = {"premium": {"existing": 33000}, "national": [["NORD", "ENEL PRODUZIONE SPA", 6033, 545, 229205370]]}
    assert round(cms.implied_premium(auction, "ENEL PRODUZIONE SPA", "NORD")) == 55259
    assert cms.implied_premium(auction, "ENEL PRODUZIONE SPA", "CSUD") is None
