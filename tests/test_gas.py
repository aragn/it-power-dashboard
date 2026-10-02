"""Checks for fetch_gas.  Run: python -m pytest tests"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_gas as fg  # noqa: E402


def storage_row(day, full, status="C"):
    return {"gasDayStart": day, "gasInStorage": "10.5", "full": full, "injection": "0", "withdrawal": "70.2",
            "workingGasVolume": "11", "injectionCapacity": "-", "status": status}


def test_storage_and_lng_rows_skip_missing_values_and_days_without_data():
    rows = [storage_row("2025-02-28", "28.99"), storage_row("2025-03-01", "-", status="N"), storage_row("2024-12-31", "1")]
    out = fg.entity_rows("storage", rows, date(2025, 1, 1), date(2025, 3, 31))
    assert out["full"] == {"2025-02-28": 28.99}
    assert out["withdrawal"] == {"2025-02-28": 70.2}
    assert "injection_capacity" not in out
    lng = fg.entity_rows("lng", [{"gasDayStart": "2026-09-30", "inventory": {"lng": "59.51", "gwh": "400.36"},
                                  "sendOut": "167", "dtmi": {"lng": "137.16", "gwh": "922.76"}, "dtrs": "183.4",
                                  "status": "C"}], date(2026, 9, 1), date(2026, 9, 30))
    assert lng["inventory"] == {"2026-09-30": 400.36} and lng["inventory_lng"] == {"2026-09-30": 59.51}
    assert lng["send_out"] == {"2026-09-30": 167.0} and lng["dtrs"] == {"2026-09-30": 183.4}


class FakeGie:
    def __init__(self, by_company):
        self.by_company = by_company
        self.asked = []

    def pages(self, host, **params):
        self.asked.append((params.get("company"), params["from"], params["to"]))
        return self.by_company[params.get("company")]


def test_a_site_that_changed_operator_is_joined_at_the_change():
    gie = FakeGie({
        "21X0000000013651": [storage_row("2025-02-28", "28.99"), storage_row("2025-03-01", "-", status="N")],
        "21X000000001250I": [storage_row("2025-02-28", "-", status="N"), storage_row("2025-03-01", "28.44")],
    })
    out = fg.fetch_entity(gie, "HUB2", date(2025, 2, 1), date(2025, 3, 31))
    assert out["full"] == {"2025-02-28": 28.99, "2025-03-01": 28.44}
    assert gie.asked == [("21X0000000013651", "2025-02-01", "2025-02-28"), ("21X000000001250I", "2025-03-01", "2025-03-31")]
    # A window after the change asks the new operator only.
    gie.asked.clear()
    fg.fetch_entity(gie, "HUB2", date(2025, 6, 1), date(2025, 6, 30))
    assert [company for company, _, _ in gie.asked] == ["21X000000001250I"]


def umm(message_id, status="Active", planned="Planned", capacity="29.96", unit="GWh/d", code="21X000000001360B"):
    return {
        "submitted": "2026-09-19 12:00:39", "published": "2026-09-19 12:00:39",
        "reportingEntity": {"name": "Terminale GNL Adriatico S.r.l.", "code": code, "type": "LSO"},
        "message": {"messageId": message_id, "messageType": "Regasification plant unavailability",
                    "unavailabilityType": planned},
        "status": status, "from": "2026-09-19 12:00:00", "to": "2026-09-20 04:00:00",
        "asset": {"name": "Terminale GNL Adriatico Srl", "code": code},
        "unavailable": {"capacity": capacity, "unit": unit}, "technical": {"capacity": "293.45", "unit": "GWh/d"},
        "unavailabilityReason": " Unplanned unscheduled event ",
    }


def test_umm_events_keep_the_latest_version_and_convert_units():
    rows = [
        umm("26091921X000000001360B001_001", planned="Unplanned"),
        umm("26091921X000000001360B001_002", status="Inactive", planned="Unplanned"),
        umm("26091921X000000001360B002_001", status="Dismissed"),
        umm("26050421X000000001360B001_001", capacity="1914000", unit="kWh/h", code="59X4-IGSTORAGE-T"),
    ]
    events = fg.merge_events([], fg.umm_events(rows))
    assert [event["id"] for event in events] == ["26050421X000000001360B001", "26091921X000000001360B001"]
    storage, lng = events
    assert lng["version"] == 2 and lng["status"] == "Inactive" and lng["planned"] is False
    assert lng["facility"] == "ROVIGO" and lng["reason"] == "Unplanned unscheduled event"
    assert lng["message_type"] == "Regasification plant unavailability"
    assert lng["unavailable"] == 29.96 and lng["from"] == "2026-09-19 12:00"
    assert storage["facility"] == "CORNEGLIANO" and storage["planned"] is True
    assert storage["unavailable"] == round(1914000 * 24e-6, 3)


def test_outages_are_spread_over_the_gas_days_they_cover():
    # 12:00 UTC on 19 Sep to 04:00 UTC on 20 Sep: 16 h of the gas day of the 19th
    # (04:00 UTC = 06:00 in Rome), so two thirds of 24 GWh/d.
    events = [{"facility": "ROVIGO", "planned": False, "unavailable": 24.0,
               "from": "2026-09-19 12:00", "to": "2026-09-20 04:00"},
              {"facility": "OLT", "planned": True, "unavailable": 48.0,
               "from": "2026-09-19 04:00", "to": "2026-09-21 04:00"},
              {"facility": "OLT", "planned": True, "unavailable": None, "from": "2026-09-19 04:00", "to": "2026-09-20 04:00"}]
    daily = fg.umm_daily(events)
    assert daily["UMM|ROVIGO|unplanned"] == {"2026-09-19": 16.0}
    assert daily["UMM|OLT|planned"] == {"2026-09-19": 48.0, "2026-09-20": 48.0}
    # The autumn clock change: the gas day of 24 Oct 2026 lasts 25 hours (04:00 UTC to 05:00 UTC).
    autumn = fg.umm_daily([{"facility": "OLT", "planned": True, "unavailable": 25.0,
                            "from": "2026-10-24 04:00", "to": "2026-10-25 05:00"}])
    assert autumn["UMM|OLT|planned"] == {"2026-10-24": 25.0}


def test_saved_events_survive_a_short_answer_and_dismissals_drop_them():
    saved = fg.merge_events([], fg.umm_events([umm("26091921X000000001360B001_001"), umm("26091921X000000001360B002_001")]))
    assert len(saved) == 2
    # The next answer misses the first event and dismisses the second.
    merged = fg.merge_events(saved, fg.umm_events([umm("26091921X000000001360B002_002", status="Dismissed")]))
    assert [event["id"] for event in merged] == ["26091921X000000001360B001"]
