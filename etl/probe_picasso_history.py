"""TEMPORARY: where are North's standard-product (PICASSO) aFRR activations before June 2026?"""
import os
from collections import defaultdict

import fetch_balancing as fb

token = os.environ["ENTSOE_API_KEY"]
NORD = fb.ZONES["NORD"]
for label, start, end in (("Feb 2026", "202602092300", "202602162300"), ("May 2026", "202605102200", "202605172200"),
                          ("Jun 2026", "202606202200", "202606302200")):
    print("=====", label)
    for doc, process in (("A24", "A51"), ("A24", "A68"), ("A24", "A67"), ("A84", "A16"), ("A84", "A68"), ("A84", "A67")):
        params = {"documentType": doc, "processType": process, "periodStart": start, "periodEnd": end}
        if doc == "A24":
            params.update(area_Domain=NORD if process != "A67" else fb.IT_DOMAIN, curveType="A03")
        else:
            params.update(businessType="A96", controlArea_Domain=NORD if process != "A67" else fb.IT_DOMAIN)
            if process == "A67":
                params.update(Standard_MarketProduct="A01", periodEnd=str(int(start) + 10000))
        try:
            root = fb.entsoe_get(token, params)
        except Exception as error:
            print(f"  {doc} {process}: {error!r}"[:200])
            continue
        stats = defaultdict(lambda: [0, 0, 0.0])
        for ts in fb._series(root):
            key = (fb._text(ts, "standard_MarketProduct.marketProductType") and "standard") or \
                  (fb._text(ts, "original_MarketProduct.marketProductType") and "original A02") or "?"
            key += " " + fb.DIRECTIONS.get(fb._text(ts, "flowDirection.direction"), "?")
            tag = "secondaryQuantity" if doc == "A24" else "activation_Price.amount"
            for _, _, value in fb.expand_points(ts, lambda p: fb.number(p, tag)):
                s = stats[key]
                s[0] += 1
                s[1] += 1 if value else 0
                s[2] += abs(value)
        print(f"  {doc} {process}: " + ("no data" if root is None else
              "; ".join(f"{k}: {n} pts, {nz} nonzero, sum {total:,.0f}" for k, (n, nz, total) in sorted(stats.items()))))
