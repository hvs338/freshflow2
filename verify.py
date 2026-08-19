"""
Independent check on the semantic layer.

Recomputes shrink straight from the raw CSVs in pandas, deliberately not
touching semantic.py, and asserts the view agrees. If these pass, the numbers
the model narrates are the numbers in the files.

The join is the thing being checked. Shipments are deliveries and sales are
daily, so the two facts must be summed independently over a period -- never
row-matched on date. Anchoring sales to shipment dates drops 77% of sales and
overstates June shrink by about 860%, and the result looks entirely plausible.

Nothing here imports the code it is checking beyond the entry points: the
pandas ground truth is written out longhand on purpose, so a wrong SQL builder
cannot hide behind a wrong view or the reverse.

Run: python verify.py
"""

import pandas as pd

import metrics
import queries
from semantic import QueryRejected, Semantic

MONTHS_IN_EXTRACT = ("2026-04", "2026-05", "2026-06")
BOTH_SCOPES = ("fresh", "all")
BOTH_MEASURES = ("units", "cost")

# Money comparisons are floating point sums over ~180k rows; half a unit of
# slack is well inside the noise and well outside a real disagreement.
DEFAULT_TOLERANCE = 0.5


# --- Recording results -----------------------------------------------------


class CheckLog:
    """
    Every assertion, counted and printed as it happens.

    Holds the tally so no function needs a global, and so the summary at the
    end cannot drift from what was actually run.
    """

    def __init__(self):
        self.total = 0
        self.failures = []

    def record(self, label: str, passed: bool, detail: str = "") -> None:
        self.total += 1
        if not passed:
            self.failures.append(label)
        status = "PASS" if passed else "FAIL"
        print(f"  {status}  {label}{': ' + detail if detail else ''}")

    def close_enough(
        self, label: str, actual, expected, tolerance: float = DEFAULT_TOLERANCE
    ) -> None:
        """Two numbers that should agree, printed side by side either way."""
        passed = abs(float(actual) - float(expected)) <= tolerance
        self.record(label, passed, f"{actual:,.2f} vs {expected:,.2f}")

    @staticmethod
    def note(message: str) -> None:
        """Context worth printing that is not itself an assertion."""
        print(f"  INFO  {message}")

    @property
    def passed(self) -> int:
        return self.total - len(self.failures)

    def report(self) -> None:
        print(f"\n{self.passed}/{self.total} checks passed")
        if self.failures:
            raise SystemExit("FAILED: " + ", ".join(self.failures))


# --- The pandas ground truth -----------------------------------------------


def load_raw_facts():
    """The three CSVs, with the derived columns computed by hand in pandas."""
    items = pd.read_csv("data/items.csv")
    shipments = pd.read_csv("data/shipments.csv", parse_dates=["date"])
    sales = pd.read_csv("data/sales_daily.csv", parse_dates=["date"])

    case_size = dict(zip(items.item_id, items.case_size))
    unit_cost = dict(zip(items.item_id, items.unit_cost))
    department = dict(zip(items.item_id, items.dept))

    shipments["units"] = shipments.item_id.map(case_size) * shipments.cases_received
    shipments["cost"] = shipments.units * shipments.item_id.map(unit_cost)
    shipments["dept"] = shipments.item_id.map(department)
    shipments["month"] = shipments.date.dt.to_period("M").astype(str)

    sales["cost"] = sales.units_sold * sales.item_id.map(unit_cost)
    sales["dept"] = sales.item_id.map(department)
    sales["month"] = sales.date.dt.to_period("M").astype(str)

    return shipments, sales


shipment_facts, sales_facts = load_raw_facts()
semantic_view = Semantic()
log = CheckLog()


def shrink_from_raw_csvs(month, departments, measure, **extra_filters):
    """
    Shrink computed in pandas, without touching semantic.py or queries.py.

    Shipments and sales are summed independently and then subtracted, which is
    the whole point: row-matching them on date is the mistake being guarded
    against.
    """
    shipped = shipment_facts[shipment_facts.month == month]
    sold = sales_facts[sales_facts.month == month]

    shipped = shipped[shipped.dept.isin(departments)]
    sold = sold[sold.dept.isin(departments)]

    for column, values in extra_filters.items():
        shipped = shipped[shipped[column].isin(values)]
        sold = sold[sold[column].isin(values)]

    if measure == "units":
        return shipped.units.sum() - sold.units_sold.sum()
    return shipped.cost.sum() - sold.cost.sum()


def shrink_from_view(month, scope, measure, extra_where=""):
    """The same number, through the governed view, using metrics.METRICS."""
    metric_expression = metrics.METRICS[f"shrink_{measure}"]
    sql = (
        f"SELECT {metric_expression} AS value FROM daily "
        f"WHERE strftime(date, '%Y-%m') = '{month}' {extra_where}"
    )
    return semantic_view.run(sql, scope)["rows"][0]["value"]


def scalar_from_view(sql, scope="all"):
    """One number from one query, for the checks that only need a count."""
    return list(semantic_view.run(sql, scope)["rows"][0].values())[0]


# --- 1. The view agrees with the files -------------------------------------

print("\n1. Monthly totals, both measures, both scopes")
for month in MONTHS_IN_EXTRACT:
    for scope in BOTH_SCOPES:
        departments = metrics.SCOPES[scope]["departments"]
        for measure in BOTH_MEASURES:
            log.close_enough(
                f"{month} {scope} {measure}",
                shrink_from_view(month, scope, measure),
                shrink_from_raw_csvs(month, departments, measure),
            )

# --- 2. Scope is enforced by the harness, not the query --------------------

print("\n2. Scope changes the number, and the harness controls it")
fresh_june = shrink_from_view("2026-06", "fresh", "units")
all_june = shrink_from_view("2026-06", "all", "units")
log.close_enough(
    "Grocery adds",
    all_june - fresh_june,
    shrink_from_raw_csvs("2026-06", metrics.ALL_DEPARTMENTS, "units")
    - shrink_from_raw_csvs("2026-06", metrics.FRESH_DEPARTMENTS, "units"),
)
log.note(f"identical SQL, different scope: {fresh_june:,.0f} vs {all_june:,.0f}")

# --- 3. The planted anomaly ------------------------------------------------

print("\n3. Northeast Dairy, the anomaly in the extract")
NORTHEAST_DAIRY = "AND dept='Dairy' AND region='Northeast'"
for month in ("2026-05", "2026-06"):
    log.close_enough(
        f"NE Dairy {month}",
        shrink_from_view(month, "fresh", "units", NORTHEAST_DAIRY),
        shrink_from_raw_csvs(month, ["Dairy"], "units", region=["Northeast"]),
    )

northeast_dairy_rate = semantic_view.run(
    f"SELECT {metrics.METRICS['shrink_rate']} AS rate FROM daily "
    f"WHERE strftime(date,'%Y-%m')='2026-06' {NORTHEAST_DAIRY}",
    "fresh",
)["rows"][0]["rate"]
log.note(f"NE Dairy June shrink rate {northeast_dairy_rate:.1%}")

# --- 4. The join keeps the rows that matter --------------------------------

print("\n4. No-movement rows survive the join")
# Shipped, never sold. An inner join or a date-anchored join loses these, and
# they are the highest-shrink rows in the extract.
no_movement_rows = scalar_from_view(
    "SELECT COUNT(*) AS n FROM daily WHERE sold_units = 0 AND shipped_units > 0"
)
log.note(f"{no_movement_rows:,} no-movement store-item-days present")
log.record("no-movement rows not dropped", no_movement_rows > 0)

# --- 5. The gate -----------------------------------------------------------

print("\n5. The gate refuses what it should")
REJECTED_QUERIES = [
    ("DROP TABLE _items", "DROP"),
    ("SELECT 1; DROP TABLE _items", "two statements"),
    ("SELECT * FROM read_csv('/etc/passwd')", "reading a file"),
    ("SELECT * FROM _shipments", "an un-allowlisted table"),
    ("SELECT * FROM daily_facts", "the unscoped base view"),
]
for sql, label in REJECTED_QUERIES:
    try:
        semantic_view.run(sql, "fresh")
        log.record(f"blocked {label}", False, "it was EXECUTED")
    except QueryRejected as rejection:
        log.record(f"blocked {label}", True, str(rejection)[:56])

# --- 6. The named questions agree with the files ---------------------------

print("\n6. The named metric functions agree with the raw files")
# Sections 1-5 check the view. These check the four functions built on top of
# it, because those are what the typed tools answer with. Same pandas ground
# truth, so a wrong SQL builder cannot hide behind a wrong view or vice versa.
for month in MONTHS_IN_EXTRACT:
    for scope in BOTH_SCOPES:
        departments = metrics.SCOPES[scope]["departments"]
        totals = queries.shrink(semantic_view, month, scope)["rows"][0]
        for measure in BOTH_MEASURES:
            log.close_enough(
                f"queries.shrink {month} {scope} {measure}",
                totals[f"shrink_{measure}"],
                shrink_from_raw_csvs(month, departments, measure),
            )

june_vs_may = queries.compare(semantic_view, "2026-06", "2026-05", "fresh")["rows"][0]
for measure in BOTH_MEASURES:
    log.close_enough(
        f"queries.compare delta {measure}",
        june_vs_may[f"shrink_{measure}_delta"],
        shrink_from_raw_csvs("2026-06", metrics.FRESH_DEPARTMENTS, measure)
        - shrink_from_raw_csvs("2026-05", metrics.FRESH_DEPARTMENTS, measure),
    )

# The whole point of carrying both measures: they disagree, in opposite
# directions, over the same two months and the same rows.
log.close_enough(
    "queries.compare units pct", june_vs_may["shrink_units_pct"] * 100, 12.68, 0.05
)
log.close_enough(
    "queries.compare cost pct", june_vs_may["shrink_cost_pct"] * 100, -0.57, 0.05
)
log.record(
    "mix_effect flags the units/cost disagreement",
    queries.mix_effect(june_vs_may) is not None,
)

# rank: the top department by June unit shrink, against a pandas groupby.
top_department = queries.rank(
    semantic_view, "2026-06", "dept", "units", "fresh", row_limit=1
)["rows"][0]
expected_name, expected_units = max(
    (
        (department, shrink_from_raw_csvs("2026-06", [department], "units"))
        for department in metrics.FRESH_DEPARTMENTS
    ),
    key=lambda pair: pair[1],
)
log.record(
    "queries.rank top dept",
    top_department["dept"] == expected_name,
    f"{top_department['dept']} vs {expected_name}",
)
log.close_enough(
    "queries.rank top value", top_department["shrink_units"], expected_units
)

# drivers: the shares of the change must account for the whole change.
dairy_drivers = queries.drivers(
    semantic_view,
    "2026-06",
    "2026-05",
    "region",
    "units",
    "fresh",
    {"dept": ["Dairy"]},
    row_limit=10,
)
log.close_enough(
    "queries.drivers shares sum to 1",
    sum(cause["share_of_change"] for cause in dairy_drivers["causes"]),
    1.0,
    tolerance=0.001,
)

largest_contributor = dairy_drivers["causes"][0]
sold_change = largest_contributor["sold_units_delta"]
shipped_change = largest_contributor["shipped_units_delta"]
reads_as_demand_problem = sold_change < 0 and abs(sold_change) > abs(shipped_change) * 1.5
log.record(
    f"queries.drivers reads {largest_contributor['group']} Dairy as a demand problem",
    reads_as_demand_problem,
    largest_contributor["cause"],
)

# --- 7. The prompt's data model matches the table --------------------------

print("\n7. The data model the prompt states is the data model that exists")
# The prompt tells the SQL-writing model what the columns mean. These check the
# claims in it that would silently corrupt an answer if they were wrong.

# Units mix LB and EA. The split must reconcile to the headline number, or the
# warning in the prompt is describing a decomposition that does not hold.
by_unit_of_measure = queries.shrink(
    semantic_view, "2026-06", "fresh", group_by=("unit_of_measure",)
)["rows"]
june_total_units = queries.shrink(semantic_view, "2026-06", "fresh")["rows"][0][
    "shrink_units"
]
log.close_enough(
    "LB + EA reconcile to total units",
    sum(row["shrink_units"] for row in by_unit_of_measure),
    june_total_units,
)
log.note(
    " + ".join(
        f"{row['unit_of_measure']} {row['shrink_units']:,}"
        for row in by_unit_of_measure
    )
    + f" = {june_total_units:,} units"
)

# net_sales is the only retail column; every other dollar column is cost. If
# that is backwards, margin comes out negative and the prompt is lying.
june_margin = semantic_view.run(
    "SELECT SUM(net_sales) AS revenue, SUM(sold_cost) AS cost_of_goods, "
    "SUM(net_sales - sold_cost) AS margin FROM daily "
    "WHERE strftime(date,'%Y-%m') = '2026-06'",
    "fresh",
)["rows"][0]
margin_rate = june_margin["margin"] / june_margin["revenue"]
log.record(
    "margin is derivable and plausible",
    june_margin["margin"] > 0 and 0.15 < margin_rate < 0.55,
    f"${june_margin['margin']:,.0f} on ${june_margin['revenue']:,.0f} = {margin_rate:.1%}",
)

# unit_cost is stated as one value per item, stable across the window. Cost
# columns are meaningless as time series if that is false.
distinct_costs_per_item = scalar_from_view(
    "SELECT MAX(distinct_costs) AS worst FROM "
    "(SELECT item_id, COUNT(DISTINCT unit_cost) AS distinct_costs "
    "FROM daily GROUP BY 1)"
)
log.close_enough(
    "unit_cost is one value per item", distinct_costs_per_item, 1, tolerance=0
)

# Every column the prompt describes must exist, and every column that exists
# must be described. A drifted data model is worse than none.
described_columns = set(metrics.COLUMN_MEANINGS)
actual_columns = set(semantic_view.columns())
log.record(
    "data model describes every column, no extras",
    described_columns == actual_columns,
    f"{len(actual_columns)} columns"
    if described_columns == actual_columns
    else f"missing {actual_columns - described_columns}, "
    f"stale {described_columns - actual_columns}",
)

log.report()
