"""
Probe (temporary): what ENTSO-E publishes per Italian bidding zone and on the
borders between them, for one day: series, production types, points.
"""

import os
import sys
import time
from collections import Counter
from datetime import date

from entsoe_api import REQUEST_PAUSE_SECONDS, parse_points, request_entsoe, to_api_datetime

ZONES = {
    "NORD": "10Y1001A1001A73I", "CNOR": "10Y1001A1001A70O", "CSUD": "10Y1001A1001A71M",
    "SUD": "10Y1001A1001A788", "CALA": "10Y1001C--00096J", "SICI": "10Y1001A1001A75E",
    "SARD": "10Y1001A1001A74G",
}
OTHER = {
    "SACOAC": "10Y1001A1001A885", "SACODC": "10Y1001A1001A893", "IT-MALTA": "10Y1001A1001A877",
    "IT-GR": "10Y1001A1001A66F", "IT-NORTH-AT": "10Y1001A1001A80L", "IT-NORTH-CH": "10Y1001A1001A68B",
    "IT-NORTH-FR": "10Y1001A1001A81J", "IT-NORTH-SI": "10Y1001A1001A67D",
    "FR": "10YFR-RTE------C", "AT": "10YAT-APG------L", "CH": "10YCH-SWISSGRIDZ", "SI": "10YSI-ELES-----O",
    "GR": "10YGR-HTSO-----Y", "ME": "10YCS-CG-TSO---S", "MT": "10Y1001A1001A93C",
}
AREAS = {**ZONES, **OTHER}
PAIRS = [("NORD", "CNOR"), ("CNOR", "CSUD"), ("CSUD", "SUD"), ("SUD", "CALA"), ("CALA", "SICI"),
         ("CNOR", "SARD"), ("CSUD", "SARD"), ("SARD", "SACOAC"), ("SARD", "SACODC"), ("CNOR", "SACODC"),
         ("SACOAC", "SACODC"), ("SICI", "IT-MALTA"), ("SICI", "MT"), ("SUD", "GR"), ("SUD", "IT-GR"),
         ("CSUD", "ME"), ("NORD", "FR"), ("NORD", "AT"), ("NORD", "CH"), ("NORD", "SI"), ("SICI", "SUD"),
         ("CALA", "SUD")]


def psr(ts):
    elem = ts.find("{*}MktPSRType/{*}psrType")
    return elem.text if elem is not None else "-"


def summary(root, label):
    if root is None:
        print(f"  {label}: no data")
        return
    series = root.findall(".//{*}TimeSeries")
    kinds = Counter()
    for ts in series:
        side = "in" if ts.find("{*}inBiddingZone_Domain.mRID") is not None else (
            "out" if ts.find("{*}outBiddingZone_Domain.mRID") is not None else "-")
        res = ts.find(".//{*}resolution")
        kinds[(psr(ts), side, res.text if res is not None else "-")] += 1
    points = parse_points(root, group_of=psr)
    total = Counter()
    for point in points:
        total[point["group"]] += point["value"] * point["minutes"] / 60
    print(f"  {label}: {len(series)} series {sorted(kinds.items())} MWh {dict((k, round(v)) for k, v in sorted(total.items()))}")


def ask(token, label, params):
    try:
        summary(request_entsoe(token, params), label)
    except Exception as error:
        print(f"  {label}: ERROR {str(error)[:300]}")
    time.sleep(REQUEST_PAUSE_SECONDS)


def main():
    day = date.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else "2026-10-11")
    token = os.environ["ENTSOE_API_KEY"]
    span = {"periodStart": to_api_datetime(day), "periodEnd": to_api_datetime(day, end_of_day=True)}
    for zone, eic in list(ZONES.items()) + [("SACOAC", OTHER["SACOAC"]), ("SACODC", OTHER["SACODC"])]:
        print(f"\n### {zone} {eic}")
        ask(token, "A75 generation", {"documentType": "A75", "processType": "A16", "in_Domain": eic, **span})
        ask(token, "A65 load actual", {"documentType": "A65", "processType": "A16", "outBiddingZone_Domain": eic, **span})
        ask(token, "A65 load day-ahead", {"documentType": "A65", "processType": "A01", "outBiddingZone_Domain": eic, **span})
        ask(token, "A71 generation forecast", {"documentType": "A71", "processType": "A01", "in_Domain": eic, **span})
        ask(token, "A69 wind/solar day-ahead", {"documentType": "A69", "processType": "A01", "in_Domain": eic, **span})
        ask(token, "A69 wind/solar intraday", {"documentType": "A69", "processType": "A40", "in_Domain": eic, **span})
        ask(token, "A44 price", {"documentType": "A44", "in_Domain": eic, "out_Domain": eic, **span})
        ask(token, "A72 reservoirs (week)", {"documentType": "A72", "processType": "A16", "in_Domain": eic,
                                             "periodStart": to_api_datetime(date(2026, 9, 1)),
                                             "periodEnd": to_api_datetime(day, end_of_day=True)})
    print("\n### Borders")
    for a, b in PAIRS:
        for out_zone, in_zone in ((a, b), (b, a)):
            pair = {"out_Domain": AREAS[out_zone], "in_Domain": AREAS[in_zone], **span}
            ask(token, f"A11 flow {out_zone}>{in_zone}", {"documentType": "A11", **pair})
            ask(token, f"A09 DA {out_zone}>{in_zone}", {"documentType": "A09", "contract_MarketAgreement.Type": "A01", **pair})
            ask(token, f"A09 total {out_zone}>{in_zone}", {"documentType": "A09", "contract_MarketAgreement.Type": "A05", **pair})
            ask(token, f"A61 NTC DA {out_zone}>{in_zone}", {"documentType": "A61", "contract_MarketAgreement.Type": "A01", **pair})
            ask(token, f"A31 offered DA {out_zone}>{in_zone}", {"documentType": "A31", "auction.Type": "A01",
                                                               "contract_MarketAgreement.Type": "A01", **pair})


if __name__ == "__main__":
    main()
