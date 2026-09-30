"""Checks for fetch_balancing.  Run: python -m pytest tests"""

import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_balancing as fb  # noqa: E402

NS = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:4"


def timeseries(points, curve="A03", resolution="PT15M", start="2026-09-21T22:00Z", end="2026-09-21T23:00Z",
               extra=""):
    body = "".join(f"<Point><position>{pos}</position>{value}</Point>" for pos, value in points)
    return ET.fromstring(
        f'<TimeSeries xmlns="{NS}"><curveType>{curve}</curveType>{extra}<Period><timeInterval>'
        f"<start>{start}</start><end>{end}</end></timeInterval><resolution>{resolution}</resolution>"
        f"{body}</Period></TimeSeries>")


def test_a03_values_hold_until_the_next_listed_position():
    ts = timeseries([(1, "<quantity>5</quantity>"), (3, "<quantity>7</quantity>")])
    values = [value for _, _, value in fb.expand_points(ts, lambda p: fb.number(p, "quantity"))]
    assert values == [5, 5, 7, 7]


def test_a03_point_without_value_ends_the_previous_one():
    # CBMP: a point without a price means no price from there on.
    ts = timeseries([(1, ""), (2, "<activation_Price.amount>177</activation_Price.amount>"), (4, "")],
                    resolution="PT15M")
    values = [(i.minute, v) for i, _, v in fb.expand_points(ts, lambda p: fb.number(p, "activation_Price.amount"))]
    assert values == [(15, 177), (30, 177)]


def test_quarter_records_use_italian_market_time():
    from datetime import datetime, timezone
    records = fb.quarter_records("NORD|x", {datetime(2026, 9, 21, 22, 15, tzinfo=timezone.utc): 2.0}, scale=4)
    assert records == [{"group": "NORD|x", "date": "2026-09-22", "time": "00:15", "minutes": 15, "value": 8.0}]


def test_merit_curves_merge_prices_and_sort_by_direction():
    bids = {"rr_up": {"12:00": [(250, 10), (217, 5), (250, 2.5)]},
            "rr_down": {"12:00": [(120, 1), (160, 4), (-5, 3)]}}
    curves = fb.merit_curves(bids)
    assert curves["rr_up"]["12:00"] == [217, 5, 250, 12.5]
    assert curves["rr_down"]["12:00"] == [160, 4, 120, 1, -5, 3]


def test_bid_products_split_picasso_standard_bids():
    standard = ET.fromstring(f'<Bid_TimeSeries xmlns="{NS}"><flowDirection.direction>A01</flowDirection.direction>'
                             "<standard_MarketProduct.marketProductType>A01</standard_MarketProduct.marketProductType>"
                             "</Bid_TimeSeries>")
    local = ET.fromstring(f'<Bid_TimeSeries xmlns="{NS}"><flowDirection.direction>A02</flowDirection.direction>'
                          "</Bid_TimeSeries>")
    assert fb.bid_product(standard, "afrr") == "afrr_picasso_up"
    assert fb.bid_product(local, "afrr") == "afrr_down"
    assert fb.bid_product(local, "rr") == "rr_down"


def test_terna_local_times_repeat_on_the_autumn_dst_day():
    rows = [{"reference_date": "2026-10-25 02:00:00", "macrozone": "NORD", "v": "1"},
            {"reference_date": "2026-10-25 02:00:00", "macrozone": "NORD", "v": "2"},
            {"reference_date": "2026-10-25 03:00:00", "macrozone": "NORD", "v": "3"}]
    records = fb.local_records(rows, "reference_date", lambda r: "NORD|x", lambda r: r["v"])
    assert [(r["time"], r["value"]) for r in records] == [("02:00", 1.0), ("03:00", 2.0), ("04:00", 3.0)]


def test_requirements_keep_the_latest_session():
    records = [{"group": "NORD|rr_requirement", "date": "2026-09-22", "time": "12:00", "value": 100},
               {"group": "NORD|rr_requirement", "date": "2026-09-22", "time": "12:00", "value": 80}]
    assert [r["value"] for r in fb.latest_session(records)] == [80]


def test_daily_rule_per_series():
    assert fb.is_mean("NORD|imbalance_price") and fb.is_mean("Sardegna|fcr_price_up")
    assert fb.is_mean("NORD|offered_rr_up") and fb.is_mean("NORD|rr_requirement")
    assert not fb.is_mean("NORD|activated_rr_up") and not fb.is_mean("SUD|imbalance_volume")
    assert not fb.is_mean("IT|igcc_import") and not fb.is_mean("NORD|msd_volume_up")


def test_zero_prices_are_kept_only_with_an_activation():
    records = [
        {"group": "NORD|price_picasso_up", "date": "2026-09-28", "time": "11:00", "value": 0.0},
        {"group": "NORD|price_picasso_down", "date": "2026-09-28", "time": "11:00", "value": 0.0},
        {"group": "NORD|price_rr_up", "date": "2026-09-28", "time": "11:00", "value": 288.0},
        {"group": "NORD|activated_picasso_down", "date": "2026-09-28", "time": "11:00", "value": 45.825},
        {"group": "NORD|activated_picasso_up", "date": "2026-09-28", "time": "11:00", "value": 0.0},
        {"group": "IT|picasso_price_up", "date": "2026-09-28", "time": "11:00", "value": 0.0},
    ]
    kept = {record["group"] for record in fb.drop_idle_prices(records)}
    assert kept == {"NORD|price_picasso_down", "NORD|price_rr_up", "NORD|activated_picasso_down",
                    "NORD|activated_picasso_up", "IT|picasso_price_up"}


def test_standard_aFRR_activations_come_from_local_selection(monkeypatch):
    def document(*series):
        body = "".join(
            f"<TimeSeries><businessType>A14</businessType>{product}<flowDirection.direction>A02</flowDirection.direction>"
            "<curveType>A03</curveType><Period><timeInterval><start>2026-09-28T09:00Z</start><end>2026-09-28T09:15Z</end>"
            f"</timeInterval><resolution>PT15M</resolution><Point><position>1</position><quantity>{offered}</quantity>"
            f"<secondaryQuantity>{activated}</secondaryQuantity></Point></Period></TimeSeries>"
            for product, offered, activated in series)
        return ET.fromstring(f'<Balancing_MarketDocument xmlns="{NS}">{body}</Balancing_MarketDocument>')

    standard = "<standard_MarketProduct.marketProductType>A01</standard_MarketProduct.marketProductType>"
    specific = "<original_MarketProduct.marketProductType>A02</original_MarketProduct.marketProductType>"
    responses = {"A51": document((standard, 560, 0), (specific, 1765.75, 0)), "A68": document((standard, 0, 45.825))}
    monkeypatch.setattr(fb, "entsoe_get", lambda token, params: responses[params["processType"]])
    values = {r["group"]: r["value"] for process in ("A51", "A68")
              for r in fb.aggregated_bids("t", "NORD", "eic", process, "", "")}
    assert values == {"NORD|offered_picasso_down": 560, "NORD|offered_afrr_down": 1765.75,
                      "NORD|activated_afrr_down": 0, "NORD|activated_picasso_down": 45.825}


def test_hourly_activation_prices_are_weighted_by_energy():
    def rows(values):
        return [{"date": "2026-09-28", "time": time, "value": value}
                for time, value in zip(("08:30", "08:45", "09:00", "09:15"), values)]
    series = {"quarter_hourly": {"NORD|price_picasso_up": rows([433, 514237, 191, 200]),
                                 "NORD|activated_picasso_up": rows([10, 0.001, 30, 0])},
              "hourly": {}, "daily": {}}
    fb.weight_activation_prices(series)
    hourly = {row["time"]: row["value"] for row in series["hourly"]["NORD|price_picasso_up"]}
    assert hourly == {"08:00": round((433 * 10 + 514237 * 0.001) / 10.001, 2), "09:00": 191.0}
    assert series["daily"]["NORD|price_picasso_up"] == [
        {"date": "2026-09-28", "value": round((433 * 10 + 514237 * 0.001 + 191 * 30) / 40.001, 2)}]
