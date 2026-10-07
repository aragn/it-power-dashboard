"""
TEMPORARY probe: what GME's public offers (Offers_PublicDomain) hold for
the markets without a merit order yet (MSD, MB, MI-XBID).  Run only in the
private repository (the log shows GME's data).

  probe_gme_offers.py 2026-09-17 MSD MB XBID MI-XBID
"""

import json
import os
import sys
import time
from collections import Counter
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_gme_units import REQUEST_PAUSE_SECONDS, get_token, request_offers, rows_of  # noqa: E402

KEYS = ("STATUS_CD", "PURPOSE_CD", "GRANULARITY", "SCOPE_CD", "SCOPE", "MARKET_CD", "OFFER_TYPE", "BILATERAL_IN",
        "ZONE_CD", "TYPE_CD", "SUBMARKET_CD", "SESSION", "PRODUCT", "SOURCE_CD", "ADJ_QUANTITY_NO")


def probe(token, day, segment):
    name, content = request_offers(token, day, segment=segment)
    print(f"\n=== {segment}: {name}, {len(content) / 1e6:.0f} MB")
    count, fields, counters, samples, numeric = 0, Counter(), {}, [], {}
    periods = Counter()
    cross, products = Counter(), Counter()
    for row in rows_of(name, content):
        cross[(row.get("STATUS_CD"), row.get("PURPOSE_CD"), row.get("SCOPE"), row.get("TYPE_CD"))] += 1
        if row.get("PRODOTTO"):
            parts = row["PRODOTTO"].split("-")
            products["-".join("QH" if p.startswith("QH") else "H" if p.startswith("H") else ("D" if p.isdigit() else p)
                              for p in parts)] += 1
        count += 1
        fields.update(row.keys())
        for key, value in row.items():
            if key in KEYS or (len(value) < 12 and key.endswith(("_CD", "_IN", "_TYPE"))):
                counters.setdefault(key, Counter())[value] += 1
            try:
                number = float(value.replace(",", "."))
                low, high = numeric.get(key, (number, number))
                numeric[key] = (min(low, number), max(high, number))
            except (ValueError, AttributeError):
                pass
        periods[row.get("PERIOD") or row.get("INTERVAL_NO") or ""] += 1
        if len(samples) < 6 and (row.get("STATUS_CD") == "ACC" or len(samples) < 3):
            samples.append(row)
    print(f"rows {count:,}")
    print("fields", sorted(fields))
    for key, counter in sorted(counters.items()):
        print(f"  {key}: {counter.most_common(25)}")
    print("numeric ranges", json.dumps({k: v for k, v in sorted(numeric.items())}))
    print("status/purpose/scope/type", cross.most_common(40))
    print("product shapes", products.most_common(20))
    print("periods", len(periods), sorted(periods.items(), key=lambda item: item[0])[:6], "...")
    for sample in samples:
        print("  sample", json.dumps(sample, ensure_ascii=False))


def main():
    """Arguments: a date, then segments; SEGMENT@YYYY-MM-DD for another date."""
    day = date.fromisoformat(sys.argv[1])
    token = get_token(os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"])
    for index, segment in enumerate(sys.argv[2:]):
        if index:
            time.sleep(REQUEST_PAUSE_SECONDS)
        when = day
        if "@" in segment:
            segment, other = segment.split("@")
            when = date.fromisoformat(other)
        try:
            print(f"\n--- {segment} {when}")
            probe(token, when, segment)
        except Exception as error:
            print(f"\n=== {segment}: {error}")


if __name__ == "__main__":
    main()
