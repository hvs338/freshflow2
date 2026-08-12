"""
Independent check on the semantic layer.

Recomputes shrink straight from the raw CSVs in pandas, deliberately not
touching semantic.py, and asserts the view agrees. If these pass, the numbers
the model narrates are the numbers in the files.

The join is the thing being checked. Shipments are deliveries and sales are
daily, so the two facts must be summed independently over a period -- never
row-matched on date. Anchoring sales to shipment dates drops 77% of sales and
overstates June shrink by about 860%, and the result looks entirely plausible.

Run: python verify.py
"""

import pandas as pd

import metrics as M
from semantic import Semantic

items = pd.read_csv("data/items.csv")
ship = pd.read_csv("data/shipments.csv", parse_dates=["date"])
sale = pd.read_csv("data/sales_daily.csv", parse_dates=["date"])

case = dict(zip(items.item_id, items.case_size))
cost = dict(zip(items.item_id, items.unit_cost))
dept = dict(zip(items.item_id, items.dept))

ship["units"] = ship.item_id.map(case) * ship.cases_received
ship["cost"] = ship.units * ship.item_id.map(cost)
ship["dept"] = ship.item_id.map(dept)
ship["month"] = ship.date.dt.to_period("M").astype(str)

sale["cost"] = sale.units_sold * sale.item_id.map(cost)
sale["dept"] = sale.item_id.map(dept)
sale["month"] = sale.date.dt.to_period("M").astype(str)

sem = Semantic()
checks, failures = 0, []


def check(name, got, want, tol=0.5):
    global checks
    checks += 1
    ok = abs(float(got) - float(want)) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got:,.2f} vs {want:,.2f}")
    if not ok:
        failures.append(name)


def raw(month, depts, measure, **filters):
    s, t = ship[ship.month == month], sale[sale.month == month]
    s, t = s[s.dept.isin(depts)], t[t.dept.isin(depts)]
    for col, vals in filters.items():
        s, t = s[s[col].isin(vals)], t[t[col].isin(vals)]
    if measure == "units":
        return s.units.sum() - t.units_sold.sum()
    return s.cost.sum() - t.cost.sum()


def view(month, scope, measure, where=""):
    metric = M.METRICS[f"shrink_{measure}"]
    sql = (f"SELECT {metric} AS v FROM daily "
           f"WHERE strftime(date, '%Y-%m') = '{month}' {where}")
    return sem.run(sql, scope)["rows"][0]["v"]


print("\n1. Monthly totals, both measures, both scopes")
for month in ("2026-04", "2026-05", "2026-06"):
    for scope in ("fresh", "all"):
        depts = M.SCOPES[scope]["depts"]
        for measure in ("units", "cost"):
            check(f"{month} {scope} {measure}",
                  view(month, scope, measure), raw(month, depts, measure))

print("\n2. Scope changes the number, and the harness controls it")
gap = view("2026-06", "all", "units") - view("2026-06", "fresh", "units")
check("Grocery adds", gap,
      raw("2026-06", M.ALL_DEPTS, "units") - raw("2026-06", M.FRESH_DEPTS, "units"))
print(f"  INFO  identical SQL, different scope: "
      f"{view('2026-06', 'fresh', 'units'):,.0f} vs {view('2026-06', 'all', 'units'):,.0f}")

print("\n3. Northeast Dairy, the anomaly in the extract")
for month in ("2026-05", "2026-06"):
    check(f"NE Dairy {month}",
          view(month, "fresh", "units", "AND dept='Dairy' AND region='Northeast'"),
          raw(month, ["Dairy"], "units", region=["Northeast"]))
rate = sem.run(
    f"SELECT {M.METRICS['shrink_rate']} AS r FROM daily "
    "WHERE strftime(date,'%Y-%m')='2026-06' AND dept='Dairy' AND region='Northeast'",
    "fresh")["rows"][0]["r"]
print(f"  INFO  NE Dairy June shrink rate {rate:.1%}")

print("\n4. No-movement rows survive the join")
# Shipped, never sold. An inner join or a date-anchored join loses these, and
# they are the highest-shrink rows in the extract.
n = sem.run("SELECT COUNT(*) AS n FROM daily WHERE sold_units = 0 AND shipped_units > 0",
            "all")["rows"][0]["n"]
print(f"  INFO  {n:,} no-movement store-item-days present")
checks += 1
if n <= 0:
    failures.append("no-movement rows")
print(f"  {'PASS' if n > 0 else 'FAIL'}  no-movement rows not dropped")

print("\n5. The gate refuses what it should")
from semantic import QueryRejected

for sql, label in [
    ("DROP TABLE _items", "DROP"),
    ("SELECT 1; DROP TABLE _items", "two statements"),
    ("SELECT * FROM read_csv('/etc/passwd')", "reading a file"),
    ("SELECT * FROM _shipments", "an un-allowlisted table"),
    ("SELECT * FROM daily_facts", "the unscoped base view"),
]:
    checks += 1
    try:
        sem.run(sql, "fresh")
        failures.append(label)
        print(f"  FAIL  {label} was EXECUTED")
    except QueryRejected as e:
        print(f"  PASS  blocked {label}: {str(e)[:56]}")

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    raise SystemExit("FAILED: " + ", ".join(failures))
