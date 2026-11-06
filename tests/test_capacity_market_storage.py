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


def test_a_programme_covering_the_cdp_pays_the_mgp_price_only():
    # Sold 100 MW, CDP 100: nothing is left for the ISP steps, whatever was offered.
    assert cms.payment_steps(100, 100, [(500, 50)], [(600, 50)], 250, 300, 700) == (0, 0, 0)


def test_accepted_above_the_strike_pays_the_offer_and_at_or_below_the_mgp_price():
    # 40 MW at 400 over a strike of 300 and an MGP price of 250: (400 - 300) x 40; the rest not offered, top 0.
    assert cms.payment_steps(40, 0, [(400, 40)], [], 250, 300, 0) == (4000, 0, 0)
    assert cms.payment_steps(40, 0, [(290, 40)], [], 250, 300, 0) == (0, 0, 0)
    # Above the strike the MGP price's payment is in the first column already: only the excess here.
    assert cms.payment_steps(40, 0, [(400, 40)], [], 350, 300, 0) == (40 * 50, 0, 0)


def test_offered_and_not_accepted_above_the_strike_stops_at_the_top_isp_price():
    assert cms.payment_steps(40, 0, [], [(500, 40)], 250, 300, 380) == (0, 40 * 80, 0)
    assert cms.payment_steps(40, 0, [], [(350, 40)], 250, 300, 380) == (0, 40 * 50, 0)
    # A top price under the strike: nothing, and offers at or below the strike never pay.
    assert cms.payment_steps(40, 0, [], [(500, 40)], 250, 300, 280) == (0, 0, 0)
    assert cms.payment_steps(40, 0, [], [(290, 40)], 250, 300, 380) == (0, 0, 0)


def test_not_offered_pays_the_top_isp_price_and_nothing_without_one():
    assert cms.payment_steps(40, 0, [], [], 250, 300, 380) == (0, 0, 40 * 80)
    assert cms.payment_steps(40, 0, [], [], 250, 300, None) == (0, 0, 0)


def test_steps_fill_the_cdp_in_order_the_dearest_offers_first():
    # CDP 100: 30 sold, then the accepted offers, then the offered ones up to the CDP.
    steps = cms.payment_steps(100, 30, [(350, 30), (400, 30)], [(500, 20)], 250, 300, 380)
    # Accepted: 30 at 400, 30 at 350; offered: the 10 MW left of the 20, at the top price.
    assert steps == (30 * 100 + 30 * 50, 10 * 80, 0)
    # Negative programmes (charging) leave the whole CDP for the steps.
    assert cms.payment_steps(40, -30, [], [], 250, 300, 380) == (0, 0, 40 * 80)


def test_top_isp_price_is_the_90th_percentile_by_mw():
    assert cms.isp_top_price([]) is None
    assert cms.isp_top_price([(100, 10)]) == 100
    assert cms.isp_top_price([(100, 85), (200, 10), (300, 5)]) == 200
    assert cms.isp_top_price([(300, 5), (100, 95)]) == 100
