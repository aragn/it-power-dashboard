"""
Probe (temporary): what GME's PublicMarketResults items hold for one day:
zones, pairs of zones, fields, samples, in the log only.  Run only in the
private repository (the log shows GME's data), through
update-market-offers.yml in probe mode, with markets RESULTS:<Segment>:<DataName>.
"""

import json
import time
from collections import Counter

from fetch_zonal import REQUEST_PAUSE_SECONDS, request_chunk


def probe_results(token, day, segment, data_name, granularity=None):
    rows = request_chunk(token, day, day, granularity=granularity, segment=segment, data_name=data_name)
    print(f"\n=== {segment} {data_name} {granularity or ''}: {len(rows)} rows")
    if not rows:
        return
    print("fields", sorted(rows[0].keys()))
    zones = Counter(str(row.get("Zone")) for row in rows if "Zone" in row)
    if zones:
        print("zones", sorted(zones.items()))
        first = {}
        for row in rows:
            zone = str(row.get("Zone"))
            first.setdefault(zone, row)
        for zone, row in sorted(first.items()):
            print("  first", zone, json.dumps(row, ensure_ascii=False))
    pairs = Counter((str(row.get("From")), str(row.get("To"))) for row in rows if "From" in row)
    if pairs:
        print("pairs", sorted(pairs.items()))
        first = {}
        for row in rows:
            first.setdefault((row.get("From"), row.get("To")), row)
        for pair, row in sorted(first.items(), key=lambda item: str(item[0])):
            print("  first", pair, json.dumps(row, ensure_ascii=False))
    periods = Counter(str(row.get("Period")) for row in rows)
    print("periods", len(periods), sorted(periods)[:5], "hours", sorted(Counter(str(row.get("Hour")) for row in rows))[:5])
    notes = Counter(str(row.get("Notes")) for row in rows)
    print("notes", notes.most_common(5))


def dump_transits(start, end):
    """The transits file of a range, gzipped and in base64 in the log (for a
    preview of the zone pages before the job publishes it)."""
    import base64
    import gzip
    import json
    import os
    from datetime import date

    import compact
    import fetch_transits
    flows, prices = fetch_transits.fetch(os.environ["GME_API_LOGIN"], os.environ["GME_API_PASSWORD"],
                                         date.fromisoformat(start), date.fromisoformat(end),
                                         (fetch_transits.empty(), fetch_transits.empty()))
    payload = {"format": compact.FORMAT, **fetch_transits.build_output(flows, prices)}
    text = base64.b64encode(gzip.compress(json.dumps(payload, separators=(",", ":")).encode())).decode()
    print(f"TRANSITSDUMP-BEGIN {len(text)}")
    for offset in range(0, len(text), 4000):
        print("TRANSITSDUMP " + text[offset:offset + 4000])
    print("TRANSITSDUMP-END")


def run(token, day, items):
    for index, item in enumerate(items):
        if item.startswith("TRANSITS:"):
            _, start, end = item.split(":")
            dump_transits(start, end)
            continue
        if index:
            time.sleep(REQUEST_PAUSE_SECONDS)
        parts = item.split(":")
        segment, data_name = parts[1], parts[2]
        granularity = parts[3] if len(parts) > 3 else None
        try:
            probe_results(token, day, segment, data_name, granularity)
        except Exception as error:
            print(f"\n=== {item}: {error}")
