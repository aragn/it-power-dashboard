"""
Monthly check of the unavailable-capacity data, run after a full rebuild of
outages.json / outage_units.json (monthly-checks.yml).  Writes
app/data/data_checks.json (shown in the dashboard's "Data checks" panel) and
a summary for the GitHub Actions run page.

1. Snapshot: average unavailable MW per month (planned A53 / forced A54) and
   each unit's unavailable energy per month (MWh).
2. Revisions: the snapshot against the previous check's - months whose
   totals moved, units whose outages were added, cancelled, reduced or
   extended.  (The daily job re-reads the last 45 days; older corrections
   only arrive with the monthly rebuild.)
3. Terna: every hour of the checked month (the previous calendar month;
   Terna's per-unit available capacity is final 7 days after the day) in
   which ENTSO-E has a unit out, compared with Terna's available capacity
   for that unit.  ENTSO-E available = installed (14.1.B) - unavailable.

Credentials: ENTSOE_API_KEY, TERNA_KEY, TERNA_SECRET.
"""

import argparse
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import compact
from entsoe_api import market_today, point_label, request_entsoe
from fetch_outages import OUTPUT_PATH as OUTAGES_PATH, STEP, UNITS_PATH
from fetch_terna import Client

REPORT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "data", "data_checks.json")

KEEP_REPORTS = 24
SNAPSHOT_MONTHS = 13            # unit-level snapshot: recent months only
MONTH_CHANGE_MW = 20            # a month's average moved by at least this much
UNIT_CHANGE_MWH = 1000          # and a unit's month by at least this much ...
UNIT_CHANGE_SHARE = 0.10        # ... and this share
MIN_HOURS = 3                   # a unit is listed after this many disagreeing hours
TOP = 25


# ============================================================================
# SNAPSHOT AND REVISIONS
# ============================================================================


def parse_utc(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def month_totals(groups):
    """{month: {"planned": avg MW, "forced": avg MW}} from outages.json daily rows."""
    sums = defaultdict(lambda: defaultdict(float))
    days = defaultdict(set)
    for key, series in groups.items():
        business = key.rsplit("|", 1)[1]
        for row in series.get("daily", []):
            month = row["date"][:7]
            sums[month][business] += row["value"]
            days[month].add(row["date"])
    return {
        month: {"planned": round(sums[month].get("A53", 0.0) / len(days[month])),
                "forced": round(sums[month].get("A54", 0.0) / len(days[month]))}
        for month in sorted(sums)
    }


def unit_months(units, first_month):
    """{unit: {month: MWh}} from outage_units.json, months from first_month (by market date)."""
    energy = defaultdict(lambda: defaultdict(float))
    # Intervals ending well before the first month cannot count (a day of margin
    # for the UTC/Italian-time difference).
    cutoff = (date.fromisoformat(f"{first_month}-01") - timedelta(days=1)).isoformat()
    for name, info in units.items():
        for start, end, mw, _ in info["intervals"]:
            if end[:10] < cutoff:
                continue
            slot = parse_utc(start)
            stop = parse_utc(end)
            while slot < stop:
                month = point_label(slot)[0][:7]
                if month >= first_month:
                    energy[name][month] += mw / 4
                slot += STEP
    return {name: {m: round(v) for m, v in sorted(months.items())} for name, months in energy.items()}


def revisions(previous, snapshot, units):
    """Changes between two snapshots; None when there is no previous one."""
    if not previous:
        return None
    months = []
    for month, now in snapshot["months"].items():
        before = previous["months"].get(month)
        if before is None:
            continue
        if any(abs(now[k] - before[k]) >= MONTH_CHANGE_MW for k in ("planned", "forced")):
            months.append({"month": month, "planned": [before["planned"], now["planned"]],
                           "forced": [before["forced"], now["forced"]]})
    changed = []
    common_months = set(previous["units_months"]) & set(snapshot["units_months"])
    for name in set(previous["units"]) | set(snapshot["units"]):
        for month in sorted(common_months):
            before = previous["units"].get(name, {}).get(month, 0)
            after = snapshot["units"].get(name, {}).get(month, 0)
            delta = after - before
            if abs(delta) < UNIT_CHANGE_MWH or abs(delta) < UNIT_CHANGE_SHARE * max(before, after):
                continue
            kind = ("added" if before < 1 else "cancelled" if after < 1
                    else "extended" if delta > 0 else "reduced")
            info = units.get(name, {})
            changed.append({"unit": name, "zone": info.get("zone"), "psr": info.get("psr"),
                            "month": month, "before": before, "after": after, "change": kind})
    changed.sort(key=lambda c: -abs(c["after"] - c["before"]))
    return {"previous": previous["checked_at"], "months": months, "units": changed}


# ============================================================================
# TERNA CROSS-CHECK
# ============================================================================


def installed_by_unit(token, year):
    """{unit name: installed MW} from ENTSO-E 14.1.B (units of 100 MW or more)."""
    root = request_entsoe(token, {"documentType": "A71", "processType": "A33", "in_Domain": "10YIT-GRTN-----B",
                                  "periodStart": f"{year}01010000", "periodEnd": f"{year}01020000"})
    installed = {}
    for ts in [] if root is None else root.findall(".//{*}TimeSeries"):
        name = ts.find(".//{*}PowerSystemResources/{*}name")
        quantity = ts.find(".//{*}quantity")
        if name is not None and name.text and quantity is not None and float(quantity.text) <= 5000:
            installed[name.text] = float(quantity.text)
    return installed


def terna_hourly(client, first, last):
    """{(unit, date, "HH:00")}: available MW, from Terna's detail available capacity (final values)."""
    values = {}
    day = first
    while day <= last:
        stop = min(day + timedelta(days=6), last)
        payload = client.get("/adequacy/v1.0/detail-available-capacity",
                             {"dateFrom": day.strftime("%d/%m/%Y"), "dateTo": stop.strftime("%d/%m/%Y")})
        for row in next((v for v in payload.values() if isinstance(v, list)), []):
            # Hour-beginning labels in local time with their UTC offset.
            local = datetime.strptime(row["market_date"][:19], "%Y-%m-%d %H:%M:%S")
            sign = 1 if row["offset"].startswith("+") else -1
            hours, minutes = (int(p) for p in row["offset"][1:].split(":"))
            utc = (local - sign * timedelta(hours=hours, minutes=minutes)).replace(tzinfo=timezone.utc)
            market_date, label = point_label(utc)
            values[(row["unit_code"], market_date, label[:3] + "00")] = float(str(row["available_capacity_MW"]).replace(",", "."))
        day = stop + timedelta(days=1)
    return values


def entsoe_hourly(units, first, last):
    """{(unit, date, "HH:00")}: average MW unavailable over the hour, for [first, last]."""
    hourly = defaultdict(float)
    for name, info in units.items():
        for start, end, mw, _ in info["intervals"]:
            slot = parse_utc(start)
            stop = parse_utc(end)
            while slot < stop:
                market_date, label = point_label(slot)
                if first.isoformat() <= market_date <= last.isoformat():
                    hourly[(name, market_date, label[:3] + "00")] += mw / 4
                slot += STEP
    return hourly


def norm(name):
    """Canonical unit name: sources differ in the UP_ prefix and punctuation."""
    return re.sub(r"[^A-Z0-9]", "", re.sub(r"^UP_", "", str(name or "").upper()))


def rename_terna(terna, names):
    """Terna's unit codes mapped onto the ENTSO-E unit names where they match."""
    by_norm = {norm(name): name for name in names}
    return {(by_norm.get(norm(unit), unit), day, hour): value for (unit, day, hour), value in terna.items()}


def compare(units, installed, entsoe, terna):
    """Per-unit agreement between ENTSO-E and Terna in the hours ENTSO-E has the unit out."""
    stats = defaultdict(lambda: {"agree": 0, "over": 0, "under": 0, "over_mwh": 0.0, "under_mwh": 0.0,
                                 "first": None, "last": None, "entsoe": 0.0, "terna": 0.0})
    terna_only = defaultdict(float)
    agree_hours = checked_hours = 0
    for (unit, day, hour), available in terna.items():
        size = installed.get(unit) or units.get(unit, {}).get("nominal")
        if not size:
            continue
        tolerance = max(20.0, 0.1 * size)
        out = entsoe.get((unit, day, hour), 0.0)
        e_available = max(size - out, 0.0)
        if out <= tolerance:
            if available < size - tolerance:
                terna_only[unit] += size - available
            continue
        checked_hours += 1
        s = stats[unit]
        if abs(available - e_available) <= tolerance:
            s["agree"] += 1
            agree_hours += 1
            continue
        kind = "over" if available > e_available else "under"
        s[kind] += 1
        s[f"{kind}_mwh"] += abs(available - e_available)
        s["entsoe"] += e_available
        s["terna"] += available
        stamp = f"{day} {hour}"
        s["first"] = min(s["first"] or stamp, stamp)
        s["last"] = max(s["last"] or stamp, stamp)

    def listing(kind):
        rows = []
        for unit, s in stats.items():
            hours = s[kind]
            if hours < MIN_HOURS:
                continue
            info = units.get(unit, {})
            disagreeing = s["over"] + s["under"]
            rows.append({"unit": unit, "zone": info.get("zone"), "psr": info.get("psr"),
                         "installed": round(installed.get(unit) or info.get("nominal") or 0),
                         "hours": hours, "mwh": round(s[f"{kind}_mwh"]),
                         "entsoe_available": round(s["entsoe"] / disagreeing), "terna_available": round(s["terna"] / disagreeing),
                         "first": s["first"], "last": s["last"]})
        return sorted(rows, key=lambda r: -r["mwh"])[:TOP]

    largest_terna_only = sorted(terna_only.items(), key=lambda kv: -kv[1])[:10]
    return {
        "units_compared": len({u for u, _, _ in terna}),
        "hours_with_outage": checked_hours,
        "agree_share": round(agree_hours / checked_hours, 3) if checked_hours else None,
        "overstated": listing("over"),     # Terna shows more available than ENTSO-E
        "understated": listing("under"),   # Terna shows less available than ENTSO-E
        "terna_only": {"units": len(terna_only), "mwh": round(sum(terna_only.values())),
                       "largest": [{"unit": u, "mwh": round(v)} for u, v in largest_terna_only]},
    }


# ============================================================================
# REPORT
# ============================================================================


def markdown(report):
    t = report["terna"]
    lines = [f"## Unavailable capacity: monthly check ({report['checked_at'][:10]})", ""]
    if t:
        share = f"{t['agree_share'] * 100:.0f}%" if t["agree_share"] is not None else "n/a"
        lines += [f"**Terna cross-check, {report['month']}:** {t['hours_with_outage']:,} unit-hours with an ENTSO-E outage; "
                  f"Terna agrees in **{share}**. {len(t['overstated'])} units where Terna shows more available "
                  f"(ENTSO-E overstates), {len(t['understated'])} where it shows less.", ""]
        for title, key in (("ENTSO-E overstates (Terna shows more available)", "overstated"),
                           ("ENTSO-E understates (Terna shows less available)", "understated")):
            if t[key]:
                lines += [f"### {title}", "", "| Unit | Zone | Type | Installed MW | Hours | MWh | ENTSO-E avail. | Terna avail. | From | To |",
                          "|---|---|---|---:|---:|---:|---:|---:|---|---|"]
                lines += [f"| {r['unit']} | {r['zone']} | {r['psr']} | {r['installed']} | {r['hours']} | {r['mwh']:,} | "
                          f"{r['entsoe_available']} | {r['terna_available']} | {r['first']} | {r['last']} |" for r in t[key]]
                lines.append("")
    r = report["revisions"]
    if r is None:
        lines += ["**Revisions:** first check, baseline recorded.", ""]
    else:
        lines += [f"**Revisions since {r['previous'][:10]}:** {len(r['months'])} months and {len(r['units'])} unit-months changed.", ""]
        if r["months"]:
            lines += ["| Month | Planned MW before → after | Forced MW before → after |", "|---|---|---|"]
            lines += [f"| {m['month']} | {m['planned'][0]:,} → {m['planned'][1]:,} | {m['forced'][0]:,} → {m['forced'][1]:,} |" for m in r["months"]]
            lines.append("")
        if r["units"]:
            lines += ["| Unit | Zone | Type | Month | MWh before → after | Change |", "|---|---|---|---|---|---|"]
            lines += [f"| {u['unit']} | {u['zone']} | {u['psr']} | {u['month']} | {u['before']:,} → {u['after']:,} | {u['change']} |"
                      for u in r["units"][:TOP]]
            lines.append("")
    return "\n".join(lines)


def default_month(today):
    """The previous calendar month (Terna's values for it are final from the 8th)."""
    first = today.replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def main():
    parser = argparse.ArgumentParser(description="Monthly check of the unavailability data.")
    parser.add_argument("--month", default=None, help="YYYY-MM to check against Terna (default: previous month)")
    args = parser.parse_args()

    today = market_today()
    month = args.month or default_month(today)
    first = date.fromisoformat(f"{month}-01")
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    groups = compact.load(OUTAGES_PATH)["groups"]
    units = compact.load(UNITS_PATH)["units"]
    reports = []
    if os.path.exists(REPORT_PATH):
        reports = compact.load(REPORT_PATH).get("reports", [])

    all_months = sorted(month_totals(groups))
    unit_first = all_months[-SNAPSHOT_MONTHS] if len(all_months) >= SNAPSHOT_MONTHS else all_months[0]
    snapshot = {"months": month_totals(groups), "units": unit_months(units, unit_first)}
    snapshot["units_months"] = sorted({m for months in snapshot["units"].values() for m in months})
    previous = reports[0].get("snapshot") if reports else None
    if previous is not None:
        previous = {**previous, "checked_at": reports[0]["checked_at"]}

    token = os.environ["ENTSOE_API_KEY"]
    client = Client(os.environ["TERNA_KEY"], os.environ["TERNA_SECRET"])
    installed = installed_by_unit(token, first.year)
    terna = rename_terna(terna_hourly(client, first, last), set(units) | set(installed))
    print(f"Terna: {len(terna):,} unit-hours for {month}; ENTSO-E 14.1.B: {len(installed)} units")
    terna_check = compare(units, installed, entsoe_hourly(units, first, last), terna) if terna else None

    report = {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "month": month,
        "terna": terna_check,
        "revisions": revisions(previous, snapshot, units),
        "snapshot": snapshot,
    }
    # Only the newest report keeps its snapshot (the next check compares against it).
    older = [{k: v for k, v in r.items() if k != "snapshot"} for r in reports]
    compact.dump({
        "source": "Monthly check of ENTSO-E 15.1 unavailability (outages.json) against Terna's per-unit available capacity",
        "reports": ([report] + older)[:KEEP_REPORTS],
    }, REPORT_PATH)

    summary = markdown(report)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary + "\n")


if __name__ == "__main__":
    main()
