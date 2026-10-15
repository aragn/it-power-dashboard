"""
Fetch Italy's daily gas prices from GME's API and write app/data/gas_prices.json,
for the Analysis tab's spark spreads:

  igi              GAS_IGIndex (segment IGI): the IG Index, GME's daily index of
                   the price of gas at the PSV, EUR/MWh; one series per product
                   GME publishes ("igi" is the main one, the product with the
                   most days)
  mgp_gas          GAS_MGASAuctionResults (MGP-GAS): the day-ahead gas auction's
                   price, EUR/MWh, and its accepted volume (mgp_gas_volume, MWh)
  imbalance        GAS_ImbalancePrice (segment GAS): the weighted average price of
                   Snam's balancing trades on the MGAS platform (the basis of the
                   gas imbalance price), EUR/MWh, with Snam's highest purchase
                   (imbalance_max_buy) and lowest sale (imbalance_min_sell)

Daily values by gas day (FlowDate).  GME's data: kept in the private
repository's data branch, like the power prices.

  fetch_gas_prices.py                    the last 14 days and the days after
                                         the file's last one
  fetch_gas_prices.py --full-history     from 1 January 2025
  fetch_gas_prices.py --start 20250101 --end 20251231

Credentials: GME_API_LOGIN / GME_API_PASSWORD.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compact  # noqa: E402
from fetch_zonal import request_data  # noqa: E402

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "data", "gas_prices.json")
HISTORY_START = date(2025, 1, 1)
RECENT_DAYS = 14

DATASETS = [
    ("IGI", "GAS_IGIndex"),
    ("MGP-GAS", "GAS_MGASAuctionResults"),
    ("GAS", "GAS_ImbalancePrice"),
]


def flow_date(value):
    """GME's FlowDate (yyyyMMdd, as a number or text) to an ISO date."""
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return datetime.fromisoformat(text[:10]).date().isoformat()


def number(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.replace(",", ".")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def field(row, *names):
    """A row's value under any of the names (GME's casing varies)."""
    lowered = {key.lower(): key for key in row}
    for name in names:
        key = lowered.get(name.lower())
        if key is not None:
            return row[key]
    return None


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_") or "igi"


def igi_values(rows):
    """IG Index rows -> {series name: {date: price}}; the product with the most days is "igi"."""
    by_product = defaultdict(dict)
    for row in rows:
        price = number(field(row, "Price"))
        day = field(row, "FlowDate")
        if price is None or day is None:
            continue
        by_product[str(field(row, "Product") or "IGI")][flow_date(day)] = price
    if not by_product:
        return {}
    main = max(by_product, key=lambda product: len(by_product[product]))
    out = {"igi": by_product[main]}
    for product, values in by_product.items():
        if product != main:
            out[f"igi_{slug(product)}"] = values
    return out


def mgp_gas_values(rows):
    """MGP-GAS auction rows -> price and volume per gas day (several products a day: volume-weighted)."""
    weighted = defaultdict(lambda: [0.0, 0.0])
    plain = {}
    for row in rows:
        price = number(field(row, "Price"))
        day = field(row, "FlowDate")
        if price is None or day is None:
            continue
        key = flow_date(day)
        volume = number(field(row, "TotalVolumesMWh")) or 0.0
        weighted[key][0] += price * volume
        weighted[key][1] += volume
        plain.setdefault(key, price)
    prices, volumes = {}, {}
    for key, (value, volume) in weighted.items():
        prices[key] = value / volume if volume > 0 else plain[key]
        volumes[key] = volume
    return {"mgp_gas": prices, "mgp_gas_volume": volumes}


def imbalance_values(rows):
    out = {"imbalance": {}, "imbalance_max_buy": {}, "imbalance_min_sell": {}}
    for row in rows:
        day = field(row, "FlowDate")
        if day is None:
            continue
        key = flow_date(day)
        for name, source in (("imbalance", "WeightedAveragePrice"), ("imbalance_max_buy", "MaxPurchaseSRGPrice"),
                             ("imbalance_min_sell", "MinSaleSRGPrice")):
            value = number(field(row, source))
            if value is not None:
                out[name][key] = value
    return out


PARSERS = {"GAS_IGIndex": igi_values, "GAS_MGASAuctionResults": mgp_gas_values, "GAS_ImbalancePrice": imbalance_values}


def load_existing(path=OUTPUT_PATH):
    """The file on disk as {series: {date: value}}."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    out = {}
    for name, series in (payload.get("series") or {}).items():
        start = date.fromisoformat(series["start"]) if series.get("start") else None
        values = series.get("values") or []
        out[name] = {(start + timedelta(days=index)).isoformat(): value for index, value in enumerate(values) if value is not None}
    return out


def build_output(series):
    encoded = {}
    for name in sorted(series):
        rows = [{"date": day, "value": value} for day, value in sorted(series[name].items())]
        encoded[name] = compact.encode_series(rows, "daily", field="value")
    return {
        "source": "GME API: GAS_IGIndex (IGI), GAS_MGASAuctionResults (MGP-GAS), GAS_ImbalancePrice",
        "description": "Daily gas prices at the PSV by gas day, EUR/MWh: igi = the IG Index (GME's daily "
                       "index of the PSV price); mgp_gas = the MGP-GAS day-ahead auction price, with its "
                       "accepted volume mgp_gas_volume (MWh); imbalance = the weighted average price of "
                       "Snam's balancing trades on the MGAS platform, with Snam's highest purchase "
                       "(imbalance_max_buy) and lowest sale (imbalance_min_sell).",
        "series": encoded,
    }


def merge(existing, new):
    merged = {name: dict(values) for name, values in existing.items()}
    for name, values in new.items():
        merged.setdefault(name, {}).update(values)
    return merged


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", help="YYYYMMDD")
    parser.add_argument("--end", help="YYYYMMDD")
    parser.add_argument("--full-history", action="store_true")
    parser.add_argument("--show", action="store_true", help="print the first rows of each dataset")
    args = parser.parse_args(argv)

    login, password = os.environ.get("GME_API_LOGIN"), os.environ.get("GME_API_PASSWORD")
    if not login or not password:
        raise SystemExit("GME_API_LOGIN and GME_API_PASSWORD are needed.")

    existing = {} if args.full_history else load_existing()
    today = date.today()
    end = datetime.strptime(args.end, "%Y%m%d").date() if args.end else today + timedelta(days=1)
    if args.start:
        start = datetime.strptime(args.start, "%Y%m%d").date()
    elif args.full_history or not existing.get("igi"):
        start = HISTORY_START
    else:
        last = max(existing["igi"])
        start = min(date.fromisoformat(last), today) - timedelta(days=RECENT_DAYS)

    new = {}
    for segment, data_name in DATASETS:
        try:
            rows = request_data(login, password, start, end, segment=segment, data_name=data_name)
        except Exception as error:  # one dataset failing does not hold back the others
            print(f"  {data_name} {segment}: failed ({error})")
            continue
        if args.show:
            print(f"  {data_name} {segment}: {len(rows)} rows; first: {json.dumps(rows[:3], ensure_ascii=False)[:800]}")
            print(f"    products: {Counter(str(field(row, 'Product')) for row in rows).most_common(6)}")
        new.update(PARSERS[data_name](rows))

    series = merge(existing, new)
    if not series.get("igi") and not series.get("mgp_gas"):
        raise SystemExit("No gas prices returned.")
    compact.dump(build_output(series), OUTPUT_PATH)
    for name, values in sorted(series.items()):
        print(f"  {name}: {len(values)} days, {min(values)} to {max(values)}")


if __name__ == "__main__":
    main()
