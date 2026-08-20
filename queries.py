"""
The SQL for the four questions worth naming.

Nothing here decides what a number means -- that is metrics.py, and every
aggregate below is interpolated from it rather than retyped. This file decides
what query to write, and semantic.py decides whether it may run. Three files,
three jobs: meaning, query, execution.

`shrink`, `compare`, `rank` and `drivers` are the shapes a merchandiser actually
asks for. They exist so the common questions are answered by reviewed code
rather than by whatever SQL the model improvised this turn -- in particular so
that "why did it move" always decomposes the same way and always separates an
ordering problem from a demand one.

Each is a thin wrapper: build a string, hand it to the view, return the rows.
No pandas, no arithmetic. The only judgement in the file is at the bottom, where
two functions read the rows that came back and say what kind of problem the
shape implies.

The SQL is assembled by string interpolation. That is only safe because every
interpolated value -- months, dimensions, filter values -- is validated against
the actual data in tools.py before it gets here. Nothing user-authored reaches
these functions unchecked. See Tools.validate_period / _dimension / _filters.
"""

from __future__ import annotations

import metrics

MAX_ROWS = 25

# The measures a comparison pivots into prior/current/delta/percent. Deltas are
# computed in SQL, not in Python, so no number reaches the user through
# arithmetic that happens outside the query whose text is shown as evidence.
COMPARED_MEASURES = (
    "shipped_units",
    "sold_units",
    "shrink_units",
    "shrink_cost",
    "revenue",
)

# Carried through a comparison for context but not differenced: the change in an
# average is not the average of the changes, so a delta here would mislead.
CONTEXT_MEASURES = ("cost_per_shrunk_unit",)

# The columns every built query selects, as (metric name in metrics.METRICS,
# output column name). Written once so all four questions return the same shape.
SELECTED_METRICS = (
    ("units_shipped", "shipped_units"),
    ("units_sold", "sold_units"),
    ("revenue", "revenue"),
    ("shrink_units", "shrink_units"),
    ("shrink_cost", "shrink_cost"),
    ("shrink_rate", "shrink_rate"),
    ("cost_per_shrunk_unit", "cost_per_shrunk_unit"),
)

MONTH_EXPRESSION = "strftime(date, '%Y-%m')"

# A slice whose sales fell this much harder than its shipments (or vice versa)
# is called a demand problem rather than a mixed one. Judgement, not arithmetic.
DOMINANCE_RATIO = 1.5

# Below this, a percentage move is treated as flat when deciding whether the two
# measures genuinely disagree.
DISAGREEMENT_THRESHOLD = 0.02


# --- Building the SQL ------------------------------------------------------


def _as_sql_literal(value) -> str:
    """One value as a SQL literal. Integers bare, everything else quoted."""
    if isinstance(value, bool):
        raise ValueError("booleans are not filterable values")
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _as_in_clause(column: str, values) -> str:
    """`column IN (...)`, with every value quoted correctly."""
    literals = ", ".join(_as_sql_literal(value) for value in values)
    return f"{column} IN ({literals})"


def _build_where_clause(months, filters) -> str:
    """The row filter: always a month restriction, plus whatever was asked for."""
    conditions = [_as_in_clause(MONTH_EXPRESSION, months)]
    for column, values in (filters or {}).items():
        conditions.append(_as_in_clause(column, values))
    return " AND ".join(conditions)


def build_monthly_aggregate(months, group_by=(), filters=None) -> str:
    """
    One month-grained aggregate, optionally split by one dimension.

    Every question in this file starts here, so the columns mean the same thing
    everywhere and every aggregate comes out of metrics.METRICS verbatim.
    """
    dimensions = list(group_by)
    selected = [
        f"{MONTH_EXPRESSION} AS month",
        *dimensions,
        *(
            f"{metrics.METRICS[metric_name]} AS {output_name}"
            for metric_name, output_name in SELECTED_METRICS
        ),
    ]
    grouping = ", ".join(["month", *dimensions])

    return (
        f"SELECT {', '.join(selected)} "
        f"FROM daily "
        f"WHERE {_build_where_clause(months, filters)} "
        f"GROUP BY {grouping}"
    )


def build_month_comparison(
    period,
    prior,
    group_by=(),
    filters=None,
    order_by=None,
    row_limit=None,
    share_of=None,
) -> str:
    """
    Two months side by side, one row per group.

    Pivoted in SQL rather than in Python so the query shown as evidence is the
    query that produced every number in the answer, deltas included. Three
    stages, each a CTE, so the generated text stays readable when someone opens
    the evidence panel:

        monthly  -- one row per month per group, from build_monthly_aggregate
        pivoted  -- one row per group, prior and current side by side
        (final)  -- the deltas and percentages derived from those pairs
    """
    dimensions = list(group_by)
    return (
        f"WITH monthly AS ({build_monthly_aggregate([prior, period], dimensions, filters)}), "
        f"pivoted AS ({_build_pivot(period, prior, dimensions)}) "
        f"{_build_deltas(dimensions, share_of)}"
        + (f" ORDER BY {order_by}" if order_by else "")
        + (f" LIMIT {int(row_limit)}" if row_limit else "")
    )


def _build_pivot(period: str, prior: str, dimensions: list) -> str:
    """Collapse two month rows per group into one row holding both months."""
    held = []
    for measure in (*COMPARED_MEASURES, *CONTEXT_MEASURES):
        for suffix, month in (("_prior", prior), ("", period)):
            held.append(
                f"COALESCE(MAX(CASE WHEN month = {_as_sql_literal(month)} "
                f"THEN {measure} END), 0) AS {measure}{suffix}"
            )

    leading = ", ".join(dimensions) + ", " if dimensions else ""
    grouping = f" GROUP BY {', '.join(dimensions)}" if dimensions else ""
    return f"SELECT {leading}{', '.join(held)} FROM monthly{grouping}"


def _build_deltas(dimensions: list, share_of: str | None) -> str:
    """Derive change and percent change from the prior/current pairs."""
    selected = list(dimensions)
    for measure in COMPARED_MEASURES:
        selected += [
            f"{measure}_prior",
            measure,
            f"{measure} - {measure}_prior AS {measure}_delta",
            f"({measure} - {measure}_prior) / NULLIF(ABS({measure}_prior), 0) "
            f"AS {measure}_pct",
        ]
    for measure in CONTEXT_MEASURES:
        selected += [f"{measure}_prior", measure]

    if share_of:
        # What fraction of the total move this group accounts for. The window
        # runs over every group, so the shares still sum to 1 after the LIMIT.
        selected.append(
            f"({share_of} - {share_of}_prior) / "
            f"NULLIF(SUM({share_of} - {share_of}_prior) OVER (), 0) AS share_of_change"
        )

    return f"SELECT {', '.join(selected)} FROM pivoted"


# --- The four named questions ---------------------------------------------


def shrink(
    semantic_view,
    period,
    scope=metrics.DEFAULT_SCOPE,
    filters=None,
    group_by=(),
    row_limit=MAX_ROWS,
) -> dict:
    """How much shrink, for one month, optionally split one way."""
    sql = build_monthly_aggregate([period], group_by, filters)
    if group_by:
        sql += f" ORDER BY shrink_units DESC LIMIT {int(row_limit)}"
    return _execute(semantic_view, sql, scope, row_limit)


def compare(
    semantic_view,
    period,
    prior,
    scope=metrics.DEFAULT_SCOPE,
    filters=None,
    group_by=(),
    row_limit=MAX_ROWS,
) -> dict:
    """Two months side by side, with deltas on both measures."""
    is_grouped = bool(group_by)
    sql = build_month_comparison(
        period,
        prior,
        group_by,
        filters,
        order_by="shrink_units_delta DESC" if is_grouped else None,
        row_limit=row_limit if is_grouped else None,
    )
    return _execute(semantic_view, sql, scope, row_limit)


def rank(
    semantic_view,
    period,
    by,
    sort_by=metrics.DEFAULT_MEASURE,
    scope=metrics.DEFAULT_SCOPE,
    filters=None,
    row_limit=10,
    ascending=False,
) -> dict:
    """
    Top or bottom N of one dimension.

    Every row carries both measures regardless of which one it sorted by, so
    one call is enough to see whether the ranking would change under the other
    definition.
    """
    row_limit = min(int(row_limit), MAX_ROWS)
    sort_column = metrics.SORT_BY[sort_by]["metric"]
    direction = "ASC" if ascending else "DESC"

    sql = (
        build_monthly_aggregate([period], (by,), filters)
        + f" ORDER BY {sort_column} {direction} LIMIT {row_limit}"
    )
    return _execute(semantic_view, sql, scope, row_limit)


def drivers(
    semantic_view,
    period,
    prior,
    dimension,
    measure=metrics.DEFAULT_MEASURE,
    scope=metrics.DEFAULT_SCOPE,
    filters=None,
    row_limit=5,
) -> dict:
    """
    Decompose a month-over-month move.

    Answers two things a ranking cannot: which slices account for the change,
    and for each of them whether shipments rose or sales fell. Those imply
    opposite corrective actions -- cut the order, versus move the product -- so
    the split is the point of the tool.
    """
    row_limit = min(int(row_limit), MAX_ROWS)
    measure_column = metrics.MEASURES[measure]["metric"]

    sql = build_month_comparison(
        period,
        prior,
        (dimension,),
        filters,
        order_by=f"{measure_column}_delta DESC",
        row_limit=row_limit,
        share_of=measure_column,
    )
    result = _execute(semantic_view, sql, scope, row_limit)
    result["causes"] = [
        _classify_cause(row, dimension, measure_column) for row in result["rows"]
    ]
    return result


# --- Reading the rows ------------------------------------------------------
#
# The two functions below are the only judgement calls in this file. Neither
# invents a number: they read deltas the query already computed and say what
# kind of problem the shape implies.


def _classify_cause(row: dict, dimension: str, measure_column: str) -> dict:
    """Ordering problem or demand problem, for one slice of a decomposition."""
    shipped_change = row["shipped_units_delta"]
    sold_change = row["sold_units_delta"]

    return {
        "group": row[dimension],
        "delta": row[f"{measure_column}_delta"],
        "share_of_change": row.get("share_of_change"),
        "shipped_units_delta": shipped_change,
        "sold_units_delta": sold_change,
        "cause": _describe_cause(shipped_change, sold_change),
    }


def _describe_cause(shipped_change: float, sold_change: float) -> str:
    """
    Which side of the equation moved, in words.

    Kept separate from _classify_cause so the rule can be read, and changed,
    without picking it out of a dict literal.
    """
    if shipped_change > 0 and sold_change < 0:
        return "shipments up and sales down"
    if abs(sold_change) > abs(shipped_change) * DOMINANCE_RATIO:
        return "sales moved while shipments held roughly flat"
    if abs(shipped_change) > abs(sold_change) * DOMINANCE_RATIO:
        return "shipments moved while sales held roughly flat"
    return "shipments and sales both moved"


def mix_effect(row: dict) -> str | None:
    """
    Why units and cost can disagree, read off an ungrouped `compare` row.

    When unit shrink rises and cost shrink does not, the average cost of a
    shrunk unit is the number that explains it: the extra loss landed on cheaper
    product. Returns None when the two measures agree and there is nothing to
    reconcile.
    """
    units_change = row.get("shrink_units_pct")
    cost_change = row.get("shrink_cost_pct")
    if not _measures_disagree(units_change, cost_change):
        return None

    cost_before = row["cost_per_shrunk_unit_prior"]
    cost_after = row["cost_per_shrunk_unit"]
    direction = "fell" if cost_after < cost_before else "rose"

    return (
        f"The two measures disagree this period: units {units_change:+.1%} "
        f"against cost {cost_change:+.1%}. The gap is mix -- the average cost of "
        f"a shrunk unit {direction} from ${cost_before:,.2f} to ${cost_after:,.2f}. "
        "Report both, and say the choice of measure changes the conclusion."
    )


def _measures_disagree(units_change, cost_change) -> bool:
    """True when one measure rose meaningfully while the other did not."""
    if units_change is None or cost_change is None:
        return False
    units_rose_alone = units_change > DISAGREEMENT_THRESHOLD > cost_change
    cost_rose_alone = cost_change > DISAGREEMENT_THRESHOLD > units_change
    return units_rose_alone or cost_rose_alone


def _execute(semantic_view, sql: str, scope: str, row_limit: int) -> dict:
    """
    Run one built query through the governed view. Scope is bound there.

    The SQL is returned alongside the rows because it is shown to the user as
    evidence: for a typed tool the model never saw this text, so the panel is
    the only place it appears.
    """
    result = semantic_view.run(sql, scope, row_limit)
    return {
        "sql": sql,
        "columns": result["columns"],
        "rows": result["rows"],
    }
