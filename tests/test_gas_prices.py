import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "etl"))

import fetch_gas_prices as gas  # noqa: E402


def test_flow_date_accepts_numbers_and_text():
    assert gas.flow_date(20260915) == "2026-09-15"
    assert gas.flow_date("20260915") == "2026-09-15"
    assert gas.flow_date("2026-09-15T00:00:00") == "2026-09-15"


def test_igi_main_product_is_the_one_with_most_days():
    rows = [
        {"FlowDate": 20260901, "Product": "IGI", "Price": "35,5"},
        {"FlowDate": 20260902, "Product": "IGI", "Price": 36.1},
        {"FlowDate": 20260901, "Product": "IGI Weekend", "Price": 34.0},
    ]
    out = gas.igi_values(rows)
    assert out["igi"] == {"2026-09-01": 35.5, "2026-09-02": 36.1}
    assert out["igi_igi_weekend"] == {"2026-09-01": 34.0}


def test_mgp_gas_volume_weighted_across_products():
    rows = [
        {"FlowDate": "20260905", "Market": "MGP", "Product": "A", "Price": 30, "TotalVolumesMWh": 100},
        {"FlowDate": "20260905", "Market": "MGP", "Product": "B", "Price": 40, "TotalVolumesMWh": 300},
        {"FlowDate": "20260906", "Market": "MGP", "Product": "A", "Price": 33, "TotalVolumesMWh": 0},
    ]
    out = gas.mgp_gas_values(rows)
    assert out["mgp_gas"]["2026-09-05"] == 37.5
    assert out["mgp_gas"]["2026-09-06"] == 33
    assert out["mgp_gas_volume"]["2026-09-05"] == 400


def test_imbalance_fields():
    rows = [{"FlowDate": 20260910, "WeightedAveragePrice": 38.2, "MaxPurchaseSRGPrice": 39, "MinSaleSRGPrice": 37.5, "MGASVolumes": 10}]
    out = gas.imbalance_values(rows)
    assert out["imbalance"] == {"2026-09-10": 38.2}
    assert out["imbalance_max_buy"] == {"2026-09-10": 39}
    assert out["imbalance_min_sell"] == {"2026-09-10": 37.5}


def test_output_round_trip(tmp_path):
    series = {"igi": {"2026-09-01": 35.5, "2026-09-03": 36.0}, "mgp_gas": {"2026-09-02": 35.9}}
    path = tmp_path / "gas_prices.json"
    gas.compact.dump(gas.build_output(series), str(path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["format"] == "compact-v1"
    assert payload["series"]["igi"]["start"] == "2026-09-01"
    assert payload["series"]["igi"]["values"] == [35.5, None, 36]
    assert gas.load_existing(str(path)) == {"igi": {"2026-09-01": 35.5, "2026-09-03": 36}, "mgp_gas": {"2026-09-02": 35.9}}


def test_merge_keeps_old_days_and_overwrites_new():
    merged = gas.merge({"igi": {"2026-09-01": 30, "2026-09-02": 31}}, {"igi": {"2026-09-02": 32, "2026-09-03": 33}})
    assert merged["igi"] == {"2026-09-01": 30, "2026-09-02": 32, "2026-09-03": 33}
