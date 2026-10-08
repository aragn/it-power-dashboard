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


def test_point_records_use_italian_market_time_and_slot_length():
    from datetime import datetime, timezone
    quarter = fb.point_records("NORD|x", [(datetime(2026, 9, 21, 22, 15, tzinfo=timezone.utc), 900, 2.0)])
    assert quarter == [{"group": "NORD|x", "date": "2026-09-22", "time": "00:15", "minutes": 15, "value": 2.0}]
    # An hourly slot covers its quarter-hours; energy per slot becomes MW.
    hourly = fb.point_records("IT|y", [(datetime(2025, 1, 5, 11, 0, tzinfo=timezone.utc), 3600, 30.0)], energy=True)
    assert hourly == [{"group": "IT|y", "date": "2025-01-05", "time": "12:00", "minutes": 60, "value": 30.0}]
    energy = fb.point_records("IT|y", [(datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc), 900, 30.0)], energy=True)
    assert energy[0]["value"] == 120.0


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
    assert fb.is_mean("NORD|price_picasso_up") and fb.is_mean("NORD|rr_requirement")
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
              for r in fb.activated_volumes("t", "NORD", "eic", process, "", "")}
    assert values == {"NORD|activated_afrr_down": 0, "NORD|activated_picasso_down": 45.825}


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


def test_zone_files_hold_the_zone_and_its_areas():
    groups = {"NORD|imbalance_price", "NORD|imbalance_volume", "SUD|imbalance_volume", "SUD|price_rr_up",
              "CNOR|price_rr_up", "Continente+Sicilia|fcr_price_up", "Sardegna|fcr_price_up",
              "Continent|afrr_requirement", "Sardinia|afrr_requirement", "IT|picasso_price_up"}
    assert fb.file_groups("CNOR", groups) == sorted(["CNOR|price_rr_up", "Continent|afrr_requirement",
                                                     "Continente+Sicilia|fcr_price_up", "SUD|imbalance_volume"])
    assert fb.file_groups("SARD", groups) == sorted(["SUD|imbalance_volume", "Sardegna|fcr_price_up",
                                                     "Sardinia|afrr_requirement"])
    assert fb.file_groups("IT", groups) == sorted(groups - {"SUD|price_rr_up", "CNOR|price_rr_up"})


def test_msd_results_by_period_or_by_hour():
    rows = [{"FlowDate": "20260922", "Hour": "12", "Period": "45", "Zone": "NORD", "VolumesSold": "4",
             "VolumesPurchased": "140", "AverageSellingPrice": "370", "AveragePurchasingPrice": "0"},
            {"FlowDate": "20250105", "Hour": "12", "Period": None, "Zone": "NORD", "VolumesSold": "30",
             "VolumesPurchased": "0", "AverageSellingPrice": "300", "AveragePurchasingPrice": "null"},
            {"FlowDate": "20260922", "Hour": "12", "Period": "45", "Zone": "FRAN", "VolumesSold": "1"}]
    records = {(r["group"], r["date"], r["time"], r["minutes"]): r["value"] for r in fb.msd_records(rows)}
    assert records == {
        ("NORD|msd_volume_up", "2026-09-22", "11:00", 15): 16.0,
        ("NORD|msd_volume_down", "2026-09-22", "11:00", 15): 560.0,
        ("NORD|msd_price_up", "2026-09-22", "11:00", 15): 370.0,
        # Offers to buy back accepted at 0 EUR/MWh: a price of 0, not "none".
        ("NORD|msd_price_down", "2026-09-22", "11:00", 15): 0.0,
        ("NORD|msd_volume_up", "2025-01-05", "11:00", 60): 30.0,
        ("NORD|msd_volume_down", "2025-01-05", "11:00", 60): 0.0,
        ("NORD|msd_price_up", "2025-01-05", "11:00", 60): 300.0,
    }


def test_macrozonal_imbalance_falls_back_to_hourly_values():
    hourly = [{"reference_date": "2025-01-05 12:00:00", "data_type": "Orario", "macrozone": "NORD",
               "zonal_aggregate_unbalance_MWh": "-80"}]
    quarter = hourly + [{"reference_date": "2025-01-05 12:00:00", "data_type": "Quarto Orario", "macrozone": "NORD",
                         "zonal_aggregate_unbalance_MWh": "-20"}]
    assert [(r["minutes"], r["value"]) for r in fb.imbalance_records(hourly)] == [(60, -80.0)]
    assert [(r["minutes"], r["value"]) for r in fb.imbalance_records(quarter)] == [(15, -80.0)]


def test_picasso_prices_split_the_25_hour_day(monkeypatch):
    from datetime import date
    windows = []
    monkeypatch.setattr(fb, "entsoe_get", lambda token, params: windows.append(
        (params["periodStart"], params["periodEnd"])))
    fb.picasso_prices("t", date(2025, 10, 26))
    assert windows == [("202510252200", "202510262200"), ("202510262200", "202510262300")]
    windows.clear()
    fb.picasso_prices("t", date(2026, 9, 28))
    assert windows == [("202609272200", "202609282200")]


def test_terna_gap_lists_days_without_imbalance_volume():
    from datetime import date
    series = {"daily": {"NORD|imbalance_volume": [{"date": "2025-01-02", "value": 1.0}]}}
    assert fb.terna_gap(series, date(2025, 1, 4)) == [date(2025, 1, 1), date(2025, 1, 3)]


def test_standard_aFRR_before_local_selection_comes_from_the_aFRR_document(monkeypatch):
    # Until late June 2026 the standard product's activations were in A51.
    doc = ET.fromstring(
        f'<Balancing_MarketDocument xmlns="{NS}"><TimeSeries><businessType>A14</businessType>'
        "<standard_MarketProduct.marketProductType>A01</standard_MarketProduct.marketProductType>"
        "<flowDirection.direction>A01</flowDirection.direction><curveType>A03</curveType><Period><timeInterval>"
        "<start>2026-05-10T10:00Z</start><end>2026-05-10T10:15Z</end></timeInterval><resolution>PT15M</resolution>"
        "<Point><position>1</position><quantity>400</quantity><secondaryQuantity>73</secondaryQuantity></Point>"
        "</Period></TimeSeries></Balancing_MarketDocument>")
    monkeypatch.setattr(fb, "entsoe_get", lambda token, params: doc)
    assert [(r["group"], r["value"]) for r in fb.activated_volumes("t", "NORD", "eic", "A51", "", "")] == [
        ("NORD|activated_picasso_up", 73.0)]


def test_national_sums_add_the_zones_activations():
    rows = lambda value: [{"date": "2026-09-28", "time": "11:00", "value": value}]  # noqa: E731
    series = {"quarter_hourly": {"NORD|activated_picasso_down": rows(45.825), "SUD|activated_picasso_down": rows(14.27)},
              "hourly": {}, "daily": {"NORD|activated_rr_up": [{"date": "2026-09-28", "value": 100.0}]}}
    fb.national_sums(series)
    assert series["quarter_hourly"]["IT|sum_activated_picasso_down"] == [
        {"date": "2026-09-28", "time": "11:00", "value": round(45.825 + 14.27, 2)}]
    assert series["daily"]["IT|sum_activated_rr_up"] == [{"date": "2026-09-28", "value": 100.0}]
    assert series["hourly"]["IT|sum_activated_afrr_up"] == []


class FakeTernaClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, path, params):
        import requests
        status, text = self.responses.pop(0)
        if status == 200:
            return {"rows": text}
        response = requests.Response()
        response.status_code, response._content = status, text.encode()
        raise requests.HTTPError(response=response)


def test_terna_stops_at_once_when_the_daily_quota_is_used_up(monkeypatch):
    import pytest
    monkeypatch.setattr(fb.time, "sleep", lambda seconds: pytest.fail("waited"))
    client = FakeTernaClient((403, '{"message": "Developer Over Rate"}'))
    with pytest.raises(fb.TernaRefused):
        fb.terna_get(client, "/x", {})
    assert client.calls == 1


def test_terna_quota_named_in_the_status_line_only_stops_at_once(monkeypatch):
    import pytest
    import requests
    monkeypatch.setattr(fb.time, "sleep", lambda seconds: pytest.fail("waited"))
    client = FakeTernaClient()
    response = requests.Response()
    response.status_code, response.reason, response._content = 403, "Developer Over Rate", b""
    client.get = lambda path, params: (_ for _ in ()).throw(requests.HTTPError(response=response))
    with pytest.raises(fb.TernaRefused):
        fb.terna_get(client, "/x", {})


def test_terna_waits_out_other_refusals(monkeypatch):
    waits = []
    monkeypatch.setattr(fb.time, "sleep", waits.append)
    client = FakeTernaClient((403, "Forbidden"), (200, [1]))
    assert fb.terna_get(client, "/x", {}) == {"rows": [1]}
    assert waits == [fb.TERNA_REFUSAL_WAIT_SECONDS] and client.calls == 2


def catch_up_fixture(monkeypatch, calls_per_block, refuse_at=None, published=lambda day: True):
    from datetime import timedelta
    blocks = []
    client = FakeTernaClient()

    def fetch(first, last, client):
        blocks.append((first, last))
        client.calls += calls_per_block
        client.refused = refuse_at is not None and len(blocks) >= refuse_at
        days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
        return [{"group": "NORD|imbalance_volume", "date": day.isoformat(), "time": "00:00", "minutes": 15,
                 "value": 1.0} for day in days if published(day)]

    monkeypatch.setenv("TERNA_KEY_2", "k")
    monkeypatch.setenv("TERNA_SECRET_2", "s")
    monkeypatch.setattr(fb, "terna_client", lambda keys: client)
    monkeypatch.setattr(fb, "fetch_terna", fetch)
    return blocks


def empty_series():
    return {resolution: {} for resolution in fb.RESOLUTIONS}


def test_catch_up_goes_on_block_after_block_until_the_call_budget(monkeypatch):
    from datetime import date
    monkeypatch.setattr(fb, "HISTORY_START", date(2025, 1, 1))
    monkeypatch.setattr(fb, "TERNA_CATCH_UP_CALLS", 100)
    blocks = catch_up_fixture(monkeypatch, calls_per_block=40)
    series = fb.terna_catch_up(empty_series(), date(2025, 6, 1))
    assert blocks == [(date(2025, 5, 4), date(2025, 5, 31)), (date(2025, 4, 6), date(2025, 5, 3)),
                      (date(2025, 3, 9), date(2025, 4, 5))]
    assert fb.terna_gap(series, date(2025, 6, 1))[-1] == date(2025, 3, 8)


def test_catch_up_stops_when_terna_refuses_or_the_history_is_complete(monkeypatch):
    from datetime import date
    monkeypatch.setattr(fb, "HISTORY_START", date(2025, 1, 1))
    blocks = catch_up_fixture(monkeypatch, calls_per_block=1, refuse_at=2)
    fb.terna_catch_up(empty_series(), date(2025, 6, 1))
    assert len(blocks) == 2
    blocks = catch_up_fixture(monkeypatch, calls_per_block=1)
    fb.terna_catch_up(empty_series(), date(2025, 2, 1))
    assert blocks == [(date(2025, 1, 4), date(2025, 1, 31)), (date(2025, 1, 1), date(2025, 1, 3))]


def test_catch_up_does_not_ask_again_for_a_day_terna_never_published(monkeypatch):
    from datetime import date
    monkeypatch.setattr(fb, "HISTORY_START", date(2025, 1, 1))
    blocks = catch_up_fixture(monkeypatch, calls_per_block=1, published=lambda day: day != date(2025, 1, 20))
    fb.terna_catch_up(empty_series(), date(2025, 2, 1))
    assert blocks == [(date(2025, 1, 4), date(2025, 1, 31)), (date(2025, 1, 1), date(2025, 1, 3))]


def test_terna_rows_off_the_slot_grid_are_skipped():
    rows = [{"reference_date": "2026-03-10 02:00:00", "v": 1}, {"reference_date": "2026-03-10 02:01:00", "v": 2}]
    records = fb.local_records(rows, "reference_date", lambda r: "NORD|imbalance_volume", lambda r: r["v"])
    assert [(r["time"], r["value"]) for r in records] == [("02:00", 1.0)]


def test_terna_marks_the_repeated_autumn_hour_one_minute_late():
    # 26 Oct 2025: 02:00-02:59 twice; Terna writes the second pass as 02:01, 02:16, ...
    rows = [{"reference_date": f"2025-10-26 {label}:00", "v": value}
            for label, value in (("01:45", 1), ("02:00", 2), ("02:45", 3), ("02:01", 4), ("02:46", 5), ("03:00", 6))]
    records = fb.local_records(rows, "reference_date", lambda r: "NORD|imbalance_volume", lambda r: r["v"])
    # Elapsed time since midnight: the second 02:00 is 03:00, local 03:00 is 04:00.
    assert sorted((r["time"], r["value"]) for r in records) == [
        ("01:45", 1.0), ("02:00", 2.0), ("02:45", 3.0), ("03:00", 4.0), ("03:45", 5.0), ("04:00", 6.0)]


def test_a_catch_up_block_cut_short_stays_in_the_gap(monkeypatch):
    from datetime import date
    monkeypatch.setattr(fb, "HISTORY_START", date(2025, 1, 1))
    catch_up_fixture(monkeypatch, calls_per_block=1, refuse_at=1)
    series = fb.terna_catch_up(empty_series(), date(2025, 2, 1))
    assert fb.terna_gap(series, date(2025, 2, 1))[-1] == date(2025, 1, 31)


def mb_row(service, sold, bought, avg_sell, avg_buy, max_sell="0", min_buy="0", revoked_sold="0", period="1"):
    return {"FlowDate": "20260928", "Hour": "1", "Period": period, "Zone": "NORD", "ServiceType": service,
            "VolumesSoldNotRevoked": sold, "VolumesSoldRevoked": revoked_sold, "VolumesPurchasedNotRevoked": bought,
            "VolumesPurchasedRevoked": "0", "AverageSellingPrice": avg_sell, "AveragePurchasingPrice": avg_buy,
            "MaximumSellingPrice": max_sell, "MinimumPurchasingPrice": min_buy}


def test_mb_results_split_services_and_weight_the_prices():
    rows = [mb_row("RS", "10", "0", "200", "0", max_sell="300", revoked_sold="2"),
            mb_row("AS", "30", "5", "100", "50", max_sell="150", min_buy="20"),
            mb_row("TT", "40", "5", "null", "null")]
    records = {r["group"]: r["value"] for r in fb.mb_records(rows)}
    # MWh per quarter-hour as MW.
    assert records["NORD|mb_volume_up_rs"] == 40.0 and records["NORD|mb_volume_up_as"] == 120.0
    assert records["NORD|mb_volume_down_as"] == 20.0 and records["NORD|mb_volume_up_revoked"] == 8.0
    assert records["NORD|mb_price_up_rs"] == 200.0 and records["NORD|mb_price_up_as"] == 100.0
    assert records["NORD|mb_price_up"] == 125.0  # (10 x 200 + 30 x 100) / 40
    assert records["NORD|mb_price_up_max"] == 300.0 and records["NORD|mb_price_down_min"] == 20.0
    # No secondary reserve bought back: no price for it.
    assert "NORD|mb_price_down_rs" not in records


def test_extreme_prices_keep_the_extreme_and_cost_is_against_the_day_ahead_price():
    rows = lambda values: [{"date": "2026-09-28", "time": t, "value": v} for t, v in values]  # noqa: E731
    series = {resolution: {} for resolution in fb.RESOLUTIONS}
    series["quarter_hourly"] = {
        "NORD|msd_price_up_max": rows([("00:00", 300.0), ("00:15", 500.0)]),
        "NORD|msd_volume_up": rows([("00:00", 40.0), ("00:15", 0.0)]),
        "NORD|msd_price_up": rows([("00:00", 200.0)]),
        "NORD|msd_volume_down": rows([("00:00", 20.0)]),
        "NORD|msd_price_down": rows([("00:00", 50.0)]),
    }
    fb.weight_activation_prices(series)
    assert series["hourly"]["NORD|msd_price_up_max"] == [{"date": "2026-09-28", "time": "00:00", "value": 500.0}]
    prices = ({("NORD", "2026-09-28", "00:00"): 120.0}, {})
    fb.dispatch_costs(series, prices)
    # Up: 40 MW x (200 - 120) EUR/MWh = 3,200 EUR/h, a quarter-hour of it in the day.
    assert series["quarter_hourly"]["IT|cost_msd_up"] == [{"date": "2026-09-28", "time": "00:00", "value": 3200}]
    assert series["daily"]["IT|cost_msd_up"] == [{"date": "2026-09-28", "value": 800}]
    # Down: 20 MW x (120 - 50) = 1,400 EUR/h.
    assert series["hourly"]["IT|cost_msd_down"] == [{"date": "2026-09-28", "time": "00:00", "value": 350}]


def test_imbalance_day_by_day_stops_at_today_and_asks_today_for_preliminary_values_only(monkeypatch):
    from datetime import date
    asked = []

    def rows(client, path, key, first, last):
        asked.append((path.rsplit("/", 1)[-1], first.isoformat()))
        return []

    monkeypatch.setattr(fb, "terna_rows", rows)
    monkeypatch.setattr(fb, "market_today", lambda: date(2026, 10, 4))
    fb.imbalance_rows(None, date(2026, 10, 3), date(2026, 10, 5))
    assert asked == [("daily-macrozonal-imbalance", "2026-10-03"),
                     ("daily-macrozonal-imbalance", "2026-10-03"), ("preliminary-macrozonal-imbalance", "2026-10-03"),
                     ("preliminary-macrozonal-imbalance", "2026-10-04")]


def test_fcr_days_are_past_days_with_both_areas(monkeypatch):
    from datetime import date
    monkeypatch.setattr(fb, "market_today", lambda: date(2026, 10, 4))
    days = lambda *names: [{"date": name, "value": 1.0} for name in names]  # noqa: E731
    series = {"daily": {"Continente+Sicilia|fcr_price_up": days("2026-10-02", "2026-10-03", "2026-10-04"),
                        "Sardegna|fcr_price_up": days("2026-10-03", "2026-10-04")}}
    assert fb.fcr_days(series) == {"2026-10-03"}
    assert fb.fcr_days({"daily": {}}) == set()


def test_gme_series_go_to_their_own_files(tmp_path, monkeypatch):
    import compact
    public, gme = tmp_path / "balancing", tmp_path / "balancing_gme"
    monkeypatch.setattr(fb, "PARTS", {"public": str(public), "gme": str(gme)})
    monkeypatch.setattr(fb, "LEGACY_PATH", str(tmp_path / "balancing.json"))
    monkeypatch.setattr(fb, "bid_days", lambda: {})
    rows = [{"date": "2026-09-22", "time": "00:00", "value": 10.0}]
    series = {resolution: {} for resolution in fb.RESOLUTIONS}
    for group in ("NORD|imbalance_price", "NORD|activated_rr_up", "NORD|msd_price_up", "NORD|msd_volume_up",
                  "IT|cost_msd_up"):
        series["quarter_hourly"][group] = rows
    fb.write_outputs(series, "all")

    def groups(directory, name):
        return {group for group, rows in compact.load(str(directory / f"{name}.json"))["series"]["quarter_hourly"].items()
                if rows}

    assert groups(public, "NORD") == {"NORD|imbalance_price", "NORD|activated_rr_up"}
    assert groups(gme, "NORD") == {"NORD|msd_price_up", "NORD|msd_volume_up"}
    assert {"IT|cost_msd_up", "IT|sum_msd_volume_up"} <= groups(gme, "IT")
    assert "IT|sum_activated_rr_up" in groups(public, "IT")
    assert not any(fb.is_gme(group) for group in groups(public, "IT"))
    # Each part reads back its own series only.
    assert not any(fb.is_gme(group) for group in fb.load_existing("public")["quarter_hourly"])
    assert fb.load_existing("gme")["quarter_hourly"] and all(
        fb.is_gme(group) for group in fb.load_existing("gme")["quarter_hourly"])


def test_bid_files_are_gzipped_kept_400_days_and_the_old_plain_ones_converted(tmp_path, monkeypatch):
    import gzip
    import json
    from datetime import date, timedelta
    monkeypatch.setattr(fb, "BIDS_DIR", str(tmp_path))
    today = date(2026, 10, 8)
    (tmp_path / "NORD").mkdir()
    payload = {"zone": "NORD", "date": "2026-10-01", "products": {"rr_up": {"12:00": [250, 10]}}}
    (tmp_path / "NORD" / "2026-10-01.json").write_text(json.dumps(payload))            # a plain file of before
    old = (today - timedelta(days=fb.BID_RETENTION_DAYS + 1)).isoformat()
    fb.write_bid_file(str(tmp_path / "NORD" / f"{old}.json.gz"), {**payload, "date": old})
    kept = (today - timedelta(days=fb.BID_RETENTION_DAYS - 1)).isoformat()
    fb.write_bid_file(str(tmp_path / "NORD" / f"{kept}.json.gz"), {**payload, "date": kept})
    assert fb.BID_RETENTION_DAYS == 400
    fb.prune_bids(today)
    assert sorted(p.name for p in (tmp_path / "NORD").iterdir()) == [f"{kept}.json.gz", "2026-10-01.json.gz"]
    with gzip.open(tmp_path / "NORD" / "2026-10-01.json.gz", "rt") as f:
        assert json.load(f) == payload
    assert fb.bid_days() == {"NORD": [kept, "2026-10-01"]}
    assert fb.bid_path("NORD", today).endswith("2026-10-08.json.gz")
