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
import queries as Q
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

print("\n6. The named metric functions agree with the raw files")
# Sections 1-5 check the view. These check the four functions built on top of
# it, because those are what the typed tools answer with. Same pandas ground
# truth, so a wrong SQL builder cannot hide behind a wrong view or vice versa.
for month in ("2026-04", "2026-05", "2026-06"):
    for scope in ("fresh", "all"):
        depts = M.SCOPES[scope]["depts"]
        row = Q.shrink(sem, month, scope)["rows"][0]
        for measure in ("units", "cost"):
            check(f"Q.shrink {month} {scope} {measure}",
                  row[f"shrink_{measure}"], raw(month, depts, measure))

cmp_row = Q.compare(sem, "2026-06", "2026-05", "fresh")["rows"][0]
for measure in ("units", "cost"):
    check(f"Q.compare delta {measure}", cmp_row[f"shrink_{measure}_delta"],
          raw("2026-06", M.FRESH_DEPTS, measure) - raw("2026-05", M.FRESH_DEPTS, measure))

# The whole point of carrying both measures: they disagree, in opposite
# directions, over the same two months and the same rows.
check("Q.compare units pct", cmp_row["shrink_units_pct"] * 100, 12.68, tol=0.05)
check("Q.compare cost pct", cmp_row["shrink_cost_pct"] * 100, -0.57, tol=0.05)
checks += 1
if Q.mix_effect(cmp_row) is None:
    failures.append("mix_effect silent")
    print("  FAIL  mix_effect did not flag the disagreement")
else:
    print("  PASS  mix_effect flags the units/cost disagreement")

# rank: the top department by June unit shrink, against a pandas groupby.
top = Q.rank(sem, "2026-06", "dept", "units", "fresh", limit=1)["rows"][0]
want = max(
    ((d, raw("2026-06", [d], "units")) for d in M.FRESH_DEPTS), key=lambda x: x[1]
)
checks += 1
print(f"  {'PASS' if top['dept'] == want[0] else 'FAIL'}  Q.rank top dept "
      f"{top['dept']} vs {want[0]}")
if top["dept"] != want[0]:
    failures.append("rank top dept")
check("Q.rank top value", top["shrink_units"], want[1])

# drivers: the shares of the change must account for the whole change.
dr = Q.drivers(sem, "2026-06", "2026-05", "region", "units", "fresh",
               {"dept": ["Dairy"]}, limit=10)
check("Q.drivers shares sum to 1", sum(c["share_of_change"] for c in dr["causes"]),
      1.0, tol=0.001)
lead = dr["causes"][0]
checks += 1
demand = lead["sold_units_delta"] < 0 and abs(lead["sold_units_delta"]) > abs(
    lead["shipped_units_delta"]) * 1.5
print(f"  {'PASS' if demand else 'FAIL'}  Q.drivers reads {lead['group']} Dairy as "
      f"a demand problem: {lead['cause']}")
if not demand:
    failures.append("drivers cause")

print("\n7. The data model the prompt states is the data model that exists")
# The prompt tells the SQL-writing model what the columns mean. These check the
# three claims in it that would silently corrupt an answer if they were wrong.

# Units mix LB and EA. The split must reconcile to the headline number, or the
# warning in the prompt is describing a decomposition that does not hold.
split = Q.shrink(sem, "2026-06", "fresh", group_by=("unit_of_measure",))["rows"]
total = Q.shrink(sem, "2026-06", "fresh")["rows"][0]["shrink_units"]
check("LB + EA reconcile to total units", sum(r["shrink_units"] for r in split), total)
print(f"  INFO  {' + '.join(f'''{r['unit_of_measure']} {r['shrink_units']:,}''' for r in split)}"
      f" = {total:,} units")

# net_sales is the only retail column; every other dollar column is cost. If
# that is backwards, margin comes out negative and the prompt is lying.
m = sem.run("SELECT SUM(net_sales) rev, SUM(sold_cost) cogs, "
            "SUM(net_sales - sold_cost) margin FROM daily "
            "WHERE strftime(date,'%Y-%m') = '2026-06'", "fresh")["rows"][0]
checks += 1
sane = m["margin"] > 0 and 0.15 < m["margin"] / m["rev"] < 0.55
print(f"  {'PASS' if sane else 'FAIL'}  margin is derivable and plausible: "
      f"${m['margin']:,.0f} on ${m['rev']:,.0f} = {m['margin'] / m['rev']:.1%}")
if not sane:
    failures.append("margin")

# unit_cost is stated as one value per item, stable across the window. Cost
# columns are meaningless as time series if that is false.
n = sem.run("SELECT MAX(n) m FROM (SELECT item_id, COUNT(DISTINCT unit_cost) n "
            "FROM daily GROUP BY 1)", "all")["rows"][0]["m"]
check("unit_cost is one value per item", n, 1, tol=0)

# Every column the prompt describes must exist, and every column that exists
# must be described. A drifted data model is worse than none.
described, actual = set(M.COLUMNS), set(sem.columns())
checks += 1
if described == actual:
    print(f"  PASS  data model describes all {len(actual)} columns, no extras")
else:
    failures.append("data model drift")
    print(f"  FAIL  data model drift: missing {actual - described}, stale {described - actual}")

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    raise SystemExit("FAILED: " + ", ".join(failures))
