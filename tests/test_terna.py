"""Checks for etl/fetch_terna.py.  Run: python -m pytest tests"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import compact  # noqa: E402
import entsoe_api  # noqa: E402
import fetch_terna  # noqa: E402


def record(local, offset, value):
    return {"date": local, "date_tz": "Europe/Rome", "date_offset": offset,
            "actual_generation_GWh": value, "primary_source": "Geothermal"}


def test_labels_follow_market_time_across_the_autumn_dst_change():
    # 02:00 local happens twice on 26 Oct 2025: first at +02:00, then +01:00.
    assert fetch_terna.record_label(record("2025-10-26 00:00:00", "+02:00", "0.6")) == ("2025-10-26", "00:00")
    assert fetch_terna.record_label(record("2025-10-26 02:00:00", "+02:00", "0.6")) == ("2025-10-26", "02:00")
    assert fetch_terna.record_label(record("2025-10-26 02:00:00", "+01:00", "0.6")) == ("2025-10-26", "03:00")
    assert fetch_terna.record_label(record("2025-10-26 23:45:00", "+01:00", "0.6")) == ("2025-10-26", "24:45")


def test_gw_values_become_mw_with_hourly_average_and_daily_energy():
    spec = fetch_terna.SERIES["geothermal"]
    points = fetch_terna.parse_records([
        record("2026-09-28 00:00:00", "+02:00", "0.56"),
        record("2026-09-28 00:15:00", "+02:00", "0.58"),
        record("2026-09-28 00:30:00", "+02:00", "0.60"),
        record("2026-09-28 00:45:00", "+02:00", "0.62"),
        record("2026-09-28 01:00:00", "+02:00", None),
    ], spec)
    assert [p["value"] for p in points] == [560.0, 580.0, 600.0, 620.0]
    merged = entsoe_api.merge_resolutions({}, points, fetch_terna.RESOLUTIONS)
    decoded = compact.decode_tree(fetch_terna.build_output({"geothermal": merged, "total_load": fetch_terna.empty()}))
    assert decoded["geothermal"]["hourly"]["TOTAL"] == [{"date": "2026-09-28", "time": "00:00", "value": 590.0}]
    assert decoded["geothermal"]["daily"]["TOTAL"] == [{"date": "2026-09-28", "value": 590.0}]


def response(status, reason="", body="", payload=None):
    import requests
    out = requests.Response()
    out.status_code, out.reason = status, reason
    out._content = (body if payload is None else __import__("json").dumps(payload)).encode()
    return out


def fake_terna(monkeypatch, refuse):
    """Token and data calls; `refuse(client_id, path)` gives the refusal, if any."""
    calls = []

    def post(url, data, timeout):
        calls.append((data["client_id"], "token"))
        refusal = refuse(data["client_id"], "token")
        return refusal if refusal is not None else response(200, payload={"access_token": data["client_id"]})

    def get(url, params, timeout, headers):
        key = headers["Authorization"].split()[-1]
        calls.append((key, "data"))
        refusal = refuse(key, "data")
        return refusal if refusal is not None else response(200, payload={"rows": [key]})

    monkeypatch.setattr(fetch_terna.requests, "post", post)
    monkeypatch.setattr(fetch_terna.requests, "get", get)
    monkeypatch.setattr(fetch_terna.time, "sleep", lambda seconds: None)
    for name, value in (("TERNA_KEY_3", "third"), ("TERNA_SECRET_3", "s3"), ("TERNA_KEY", "main"),
                        ("TERNA_SECRET", "s1")):
        monkeypatch.setenv(name, value)
    return calls


def test_client_goes_on_with_the_next_key_when_the_quota_is_used_up(monkeypatch):
    # The token call names the reason in the status line only.
    calls = fake_terna(monkeypatch, lambda key, call: response(403, "Developer Over Rate")
                       if key == "third" else None)
    client = fetch_terna.Client(fetch_terna.THIRD_KEY, fetch_terna.MAIN_KEY)
    assert client.get("/x", {}) == {"rows": ["main"]}
    assert calls == [("third", "token"), ("main", "token"), ("main", "data")]
    assert client.key_name == "TERNA_KEY" and client.first_key == "TERNA_KEY_3"


def test_client_switches_keys_on_a_data_call_and_on_a_key_not_accepted(monkeypatch):
    spent = set()
    calls = fake_terna(monkeypatch, lambda key, call: response(403, "", '{"message": "Developer Over Rate"}')
                       if key in spent and call == "data" else None)
    client = fetch_terna.Client(fetch_terna.THIRD_KEY, fetch_terna.MAIN_KEY)
    assert client.get("/x", {}) == {"rows": ["third"]}
    spent.add("third")
    assert client.get("/x", {}) == {"rows": ["main"]}
    assert calls[-3:] == [("third", "data"), ("main", "token"), ("main", "data")]

    fake_terna(monkeypatch, lambda key, call: response(401, "Unauthorized") if key == "third" else None)
    assert fetch_terna.Client(fetch_terna.THIRD_KEY, fetch_terna.MAIN_KEY).get("/x", {}) == {"rows": ["main"]}


def test_client_with_its_last_key_used_up_fails(monkeypatch):
    import pytest
    import requests
    fake_terna(monkeypatch, lambda key, call: response(403, "Developer Over Rate"))
    monkeypatch.delenv("TERNA_KEY_3")
    client = fetch_terna.Client(fetch_terna.THIRD_KEY, fetch_terna.MAIN_KEY)
    assert client.keys == [fetch_terna.MAIN_KEY]
    with pytest.raises(requests.HTTPError):
        client.get("/x", {})
    monkeypatch.delenv("TERNA_KEY")
    with pytest.raises(RuntimeError):
        fetch_terna.Client(fetch_terna.THIRD_KEY, fetch_terna.MAIN_KEY)


def test_client_with_every_key_used_up_says_so_and_counts_the_requests(monkeypatch):
    import pytest
    fake_terna(monkeypatch, lambda key, call: response(403, "Developer Over Rate") if call == "data" else None)
    client = fetch_terna.Client(fetch_terna.THIRD_KEY, fetch_terna.MAIN_KEY)
    with pytest.raises(fetch_terna.QuotaUsedUp, match="TERNA_KEY_3, TERNA_KEY"):
        client.get("/x", {})
    assert client.used == {"TERNA_KEY_3": 2, "TERNA_KEY": 2}  # a token and a data call each


def run_main(monkeypatch, newest_load_day, today="2026-10-04"):
    """fetch_terna.main with every key used up and the total load up to newest_load_day."""
    import sys
    from datetime import date

    def download(client, name, start, end):
        raise fetch_terna.QuotaUsedUp("403 Developer Over Rate")

    existing = {name: fetch_terna.empty() for name in fetch_terna.SERIES}
    existing["total_load"]["daily"]["TOTAL"] = [{"date": newest_load_day, "time": "00:00", "value": 1.0}]
    written = []
    monkeypatch.setattr(fetch_terna, "Client", lambda *keys: type("C", (), {"usage": lambda self: "none"})())
    monkeypatch.setattr(fetch_terna, "download", download)
    monkeypatch.setattr(fetch_terna, "load_existing", lambda: existing)
    monkeypatch.setattr(fetch_terna, "market_today", lambda: date.fromisoformat(today))
    monkeypatch.setattr(fetch_terna.compact, "dump", lambda payload, path: written.append(path))
    monkeypatch.setattr(sys, "argv", ["fetch_terna.py"])
    fetch_terna.main()
    return written


def test_used_up_quota_keeps_the_data_until_it_falls_behind(monkeypatch):
    import pytest
    assert run_main(monkeypatch, "2026-10-02") == [fetch_terna.OUTPUT_PATH]
    with pytest.raises(RuntimeError, match="stops at 2026-09-30"):
        run_main(monkeypatch, "2026-09-30")
