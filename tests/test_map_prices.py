"""Checks for etl/fetch_map_prices.py.  Run: python -m pytest tests"""

import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import compact  # noqa: E402
import entsoe_api  # noqa: E402
import fetch_map_prices as map_prices  # noqa: E402


def imbalance_series(start, end, points, resolution="PT15M"):
    """points: (position, category, price)."""
    body = "".join(
        f"<Point><position>{p}</position><imbalance_Price.amount>{v}</imbalance_Price.amount>"
        f"<imbalance_Price.category>{c}</imbalance_Price.category></Point>"
        for p, c, v in points
    )
    return (f"<TimeSeries><curveType>A01</curveType><Period><timeInterval><start>{start}</start><end>{end}</end>"
            f"</timeInterval><resolution>{resolution}</resolution>{body}</Period></TimeSeries>")


def price_series(start, end, resolution, points, contract="A01", sequence=None):
    sequence_xml = (f"<classificationSequence_AttributeInstanceComponent.position>{sequence}"
                    "</classificationSequence_AttributeInstanceComponent.position>" if sequence is not None else "")
    body = "".join(f"<Point><position>{p}</position><price.amount>{v}</price.amount></Point>" for p, v in points)
    return (f"<TimeSeries><contract_MarketAgreement.type>{contract}</contract_MarketAgreement.type>{sequence_xml}"
            f"<currency_Unit.name>EUR</currency_Unit.name><curveType>A01</curveType>"
            f"<Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
            f"<resolution>{resolution}</resolution>{body}</Period></TimeSeries>")


def document(*series):
    return ET.fromstring('<Document xmlns="urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:4">'
                         + "".join(series) + "</Document>")


def values(records, group):
    return [(r["time"], r["value"]) for r in records if r["group"] == group]


def test_single_price_area_has_one_series():
    # 15 October 2025 00:00-00:30 local; both categories at the same price.
    root = document(imbalance_series("2025-10-14T22:00Z", "2025-10-14T22:30Z",
                                     [(1, "A04", 80), (1, "A05", 80), (2, "A04", 95), (2, "A05", 95)]))
    records = map_prices.imbalance_records(root, "FR")
    assert values(records, "FR|imbalance") == [("00:00", 80.0), ("00:15", 95.0)]
    assert not values(records, "FR|imbalance_short")


def test_dual_price_area_keeps_the_short_price_too():
    root = document(imbalance_series("2025-10-14T22:00Z", "2025-10-14T22:30Z",
                                     [(1, "A04", 60), (1, "A05", 140), (2, "A05", 150)]))
    records = map_prices.imbalance_records(root, "CH")
    # The excess balance price, the insufficient one filling its gap.
    assert values(records, "CH|imbalance") == [("00:00", 60.0), ("00:15", 150.0)]
    assert values(records, "CH|imbalance_short") == [("00:00", 140.0), ("00:15", 150.0)]


def test_no_matching_data_gives_no_records():
    assert map_prices.imbalance_records(None, "ME") == []
    assert map_prices.day_ahead_records(None, "NORD") == []


def test_zone_day_ahead_keeps_the_auction_at_its_finest_resolution():
    root = document(
        price_series("2025-10-14T22:00Z", "2025-10-14T23:00Z", "PT60M", [(1, 60)]),
        price_series("2025-10-14T22:00Z", "2025-10-14T23:00Z", "PT15M", [(1, 40), (2, 50), (3, 70), (4, 80)]),
        price_series("2025-10-14T22:00Z", "2025-10-14T23:00Z", "PT15M", [(1, 999)], contract="A07"),
    )
    records = map_prices.day_ahead_records(root, "NORD")
    assert values(records, "NORD|day_ahead") == [("00:00", 40.0), ("00:15", 50.0), ("00:30", 70.0), ("00:45", 80.0)]


def test_output_round_trips_through_the_compact_format():
    root = document(imbalance_series("2025-10-14T22:00Z", "2025-10-14T23:00Z",
                                     [(1, "A04", 40), (2, "A04", 50), (3, "A04", 70), (4, "A04", 80)]))
    existing = {resolution: {} for resolution in map_prices.RESOLUTIONS}
    series = entsoe_api.merge_resolutions(existing, map_prices.imbalance_records(root, "AT"),
                                          map_prices.RESOLUTIONS, daily="mean")
    payload = map_prices.build_output(series, {"AT|imbalance"})
    assert payload["areas"]["AT"] == "Austria" and payload["areas"]["CALA"] == "Calabria" and payload["areas"]["MT"] == "Malta"
    assert payload["history_read"] == ["AT|imbalance"]
    decoded = compact.decode_tree(payload)["series"]
    assert decoded["hourly"]["AT|imbalance"] == [{"date": "2025-10-15", "time": "00:00", "value": 60.0}]
    assert decoded["daily"]["AT|imbalance"] == [{"date": "2025-10-15", "value": 60.0}]


def generation_series(start, end, resolution, psr, points, consumption=False):
    domain = "outBiddingZone_Domain.mRID" if consumption else "inBiddingZone_Domain.mRID"
    body = "".join(f"<Point><position>{p}</position><quantity>{v}</quantity></Point>" for p, v in points)
    return (f"<TimeSeries><{domain}>10YFR-RTE------C</{domain}><curveType>A01</curveType>"
            f"<MktPSRType><psrType>{psr}</psrType></MktPSRType>"
            f"<Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
            f"<resolution>{resolution}</resolution>{body}</Period></TimeSeries>")


def test_generation_sums_the_types_up_to_the_last_quarter_every_type_reached():
    root = document(
        # Gas every quarter-hour 00:00-00:45, nuclear hourly (all four quarter-hours).
        generation_series("2025-10-14T22:00Z", "2025-10-14T23:00Z", "PT15M", "B04", [(1, 100), (2, 110), (3, 120), (4, 130)]),
        generation_series("2025-10-14T22:00Z", "2025-10-14T23:00Z", "PT60M", "B14", [(1, 1000)]),
        # Solar only reported to 00:15: the day stops there, not with a dip at 00:30.
        generation_series("2025-10-14T22:00Z", "2025-10-14T22:30Z", "PT15M", "B16", [(1, 5), (2, 6)]),
        # Pumping (consumption) is not generation.
        generation_series("2025-10-14T22:00Z", "2025-10-14T23:00Z", "PT15M", "B10", [(1, 999)], consumption=True),
    )
    records = map_prices.generation_records(root, "FR")
    assert values(records, "FR|generation") == [("00:00", 1105.0), ("00:15", 1116.0)]
    # It is storage charging, the load's missing part.
    assert values(records, "FR|storage") == [("00:00", 999.0)]


def test_storage_sums_pumping_and_batteries_and_leaves_other_consumption_out():
    root = document(
        generation_series("2025-10-14T22:00Z", "2025-10-14T22:30Z", "PT15M", "B10", [(1, 500), (2, 400)], consumption=True),
        generation_series("2025-10-14T22:00Z", "2025-10-14T22:30Z", "PT15M", "B25", [(1, 20), (2, 30)], consumption=True),
        # Some areas report other types' consumption (station supply): not storage.
        generation_series("2025-10-14T22:00Z", "2025-10-14T22:30Z", "PT15M", "B16", [(1, 3), (2, 3)], consumption=True),
    )
    records = map_prices.generation_records(root, "AT")
    assert values(records, "AT|storage") == [("00:00", 520.0), ("00:15", 430.0)]
    assert values(records, "AT|generation") == []


def test_a_type_missing_before_the_last_quarter_counts_as_zero():
    root = document(
        generation_series("2025-10-14T22:00Z", "2025-10-14T22:45Z", "PT15M", "B04", [(1, 100), (2, 100), (3, 100)]),
        generation_series("2025-10-14T22:00Z", "2025-10-14T22:45Z", "PT15M", "B16", [(1, 5), (3, 7)]),
    )
    assert values(map_prices.generation_records(root, "AT"), "AT|generation") == [("00:00", 105.0), ("00:15", 100.0), ("00:30", 107.0)]


def da(zone, day, points):
    """The zone's quarter-hourly day-ahead point records: (time, price)."""
    return [{"group": f"{zone}|day_ahead", "date": day, "time": t, "minutes": 15, "value": v} for t, v in points]


def border(day, points):
    """A JAO border's view of its zone's price: (time, price)."""
    return {(day, t): v for t, v in points}


DAY = "2026-10-14"
# ENTSO-E's prices on 14 October 2026: South, Calabria and Sicily +0.01 where the auction cleared at -0.01.
SOUTH = [("11:45", 0.0), ("12:00", 0.01), ("12:15", 0.01), ("15:15", 0.0)]


def test_jao_border_view_is_the_neighbours_price_minus_the_spread():
    spreads = {"GR": [{"date": DAY, "time": "12:00", "value": 0.02}, {"date": DAY, "time": "12:15", "value": 0.03}]}
    prices = {"GR": [{"date": DAY, "time": "12:00", "value": 0.01}], "FR": [{"date": DAY, "time": "12:00", "value": 9}]}
    views = map_prices.border_views(spreads, prices)
    assert views[("GR", "SUD")] == {(DAY, "12:00"): -0.01}
    assert views[("FR", "NORD")] == {}


def test_sign_put_back_from_jao_and_carried_to_the_zones_at_the_same_price():
    records = (da("SUD", DAY, SOUTH) + da("CALA", DAY, SOUTH) + da("SICI", DAY, SOUTH)
               + da("CSUD", DAY, [("12:00", 0.01), ("12:15", 5.0)]) + da("SARD", DAY, [("12:00", 0.01)])
               + da("NORD", DAY, [("12:00", 0.01)]))
    views = {("GR", "SUD"): border(DAY, [("11:45", 0.0), ("12:00", -0.01), ("12:15", -0.02), ("15:15", 0.0)]),
             # North's own border gives its price as ENTSO-E has it.
             ("FR", "NORD"): border(DAY, [("12:00", 0.01)])}
    output, restored = map_prices.restore_signs(records, views)
    assert values(output, "SUD|day_ahead") == [("11:45", 0.0), ("12:00", -0.01), ("12:15", -0.01), ("15:15", 0.0)]
    assert values(output, "SICI|day_ahead") == values(output, "SUD|day_ahead")
    # Centre-South, next to the South, at the same published price at 12:00 only; Sardinia through it.
    assert values(output, "CSUD|day_ahead") == [("12:00", -0.01), ("12:15", 5.0)]
    assert values(output, "SARD|day_ahead") == [("12:00", -0.01)]
    # The North has a border of its own that says +0.01.
    assert values(output, "NORD|day_ahead") == [("12:00", 0.01)]
    assert restored == {"SUD|day_ahead": {DAY: ["12:00", "12:15"]}, "CALA|day_ahead": {DAY: ["12:00", "12:15"]},
                        "SICI|day_ahead": {DAY: ["12:00", "12:15"]}, "CSUD|day_ahead": {DAY: ["12:00"]},
                        "SARD|day_ahead": {DAY: ["12:00"]}}
    # The records given are not changed.
    assert values(records, "SUD|day_ahead") == SOUTH


def test_a_border_day_that_misses_a_quarter_hour_puts_nothing_back():
    # JAO's Greek spread wrong at 15:15 (as on many days until August 2026): the day does not count.
    records = da("SUD", DAY, SOUTH) + da("CALA", DAY, SOUTH)
    views = {("GR", "SUD"): border(DAY, [("11:45", 0.0), ("12:00", -0.01), ("12:15", -0.01), ("15:15", 3.5)])}
    output, restored = map_prices.restore_signs(records, views)
    assert output == records and restored == {}


def test_borders_that_disagree_put_nothing_back():
    records = da("NORD", DAY, [("12:00", 0.01), ("12:15", 0.01)])
    views = {("AT", "NORD"): border(DAY, [("12:00", -0.01), ("12:15", -0.01)]),
             ("FR", "NORD"): border(DAY, [("12:00", 0.01), ("12:15", -0.01)])}
    output, restored = map_prices.restore_signs(records, views)
    assert values(output, "NORD|day_ahead") == [("12:00", 0.01), ("12:15", -0.01)]
    assert restored == {"NORD|day_ahead": {DAY: ["12:15"]}}


def test_a_cent_off_and_prices_already_negative_stay_as_they_are():
    records = da("SUD", DAY, [("12:00", 0.01), ("12:15", -0.05), ("12:30", 40.0)])
    views = {("GR", "SUD"): border(DAY, [("12:00", 0.0), ("12:15", -0.05), ("12:30", 40.01)])}
    output, restored = map_prices.restore_signs(records, views)
    assert output == records and restored == {}


def test_sign_restored_on_file_follows_the_days_downloaded_again():
    before = {"SUD|day_ahead": {DAY: ["12:00"], "2026-10-20": ["13:00"]}, "CALA|day_ahead": {DAY: ["12:00"]}}
    # 14 October downloaded again: ENTSO-E has the sign now in Calabria; 20 October not downloaded.
    records = da("SUD", DAY, SOUTH) + da("CALA", DAY, SOUTH)
    merged = map_prices.merge_restored(before, {"SUD|day_ahead": {DAY: ["12:15", "12:00"]}}, records)
    assert merged == {"SUD|day_ahead": {DAY: ["12:00", "12:15"], "2026-10-20": ["13:00"]}}


def test_restored_signs_reach_the_hourly_and_daily_prices():
    records, restored = map_prices.restore_signs(
        da("SUD", DAY, [("12:00", 0.01), ("12:15", 0.01), ("12:30", 0.01), ("12:45", 0.03)]),
        {("GR", "SUD"): border(DAY, [("12:00", -0.01), ("12:15", -0.01), ("12:30", -0.01), ("12:45", 0.03)])})
    existing = {resolution: {} for resolution in map_prices.RESOLUTIONS}
    series = entsoe_api.merge_resolutions(existing, records, map_prices.RESOLUTIONS, daily="mean")
    assert series["hourly"]["SUD|day_ahead"] == [{"date": DAY, "time": "12:00", "value": 0.0}]
    payload = compact.decode_tree(map_prices.build_output(series, set(), restored))
    assert payload["sign_restored"] == {"SUD|day_ahead": {DAY: ["12:00", "12:15", "12:30"]}}
    assert [row["value"] for row in payload["series"]["quarter_hourly"]["SUD|day_ahead"]] == [-0.01, -0.01, -0.01, 0.03]
