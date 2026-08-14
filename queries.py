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
these functions unchecked. See Tools._period / _dimension / _filters.
"""

from __future__ import annotations

from metrics import DEFAULT_MEASURE, DEFAULT_SCOPE, MEASURES, METRICS

MAX_ROWS = 25

# The four measures a comparison pivots. Deltas and percentages are computed in
# SQL, not in Python, so no number reaches the user through arithmetic that
# happens outside the query whose text is shown as evidence.
_PIVOT = ("shipped_units", "sold_units", "shrink_units", "shrink_cost")


# --- Building the SQL ------------------------------------------------------


def _lit(v) -> str:
    """One value as a SQL literal. Ints bare, everything else quoted."""
    if isinstance(v, bool):
        raise ValueError("booleans are not filterable values")
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _in(col: str, vals) -> str:
    return f"{col} IN ({', '.join(_lit(v) for v in vals)})"


def _select(months, group_by=(), filters=None) -> str:
    """
    One month-grained aggregate, optionally split by one dimension.

    Every question in this file starts here, so the columns mean the same thing
    everywhere and every aggregate comes out of METRICS verbatim.
    """
    dims = list(group_by)
    cols = ["strftime(date, '%Y-%m') AS month", *dims, *(
        f"{METRICS[m]} AS {alias}" for m, alias in (
            ("units_shipped", "shipped_units"),
            ("units_sold", "sold_units"),
            ("shrink_units", "shrink_units"),
            ("shrink_cost", "shrink_cost"),
            ("shrink_rate", "shrink_rate"),
            ("cost_per_shrunk_unit", "cost_per_shrunk_unit"),
        )
    )]
    where = [_in("strftime(date, '%Y-%m')", months)]
    for col, vals in (filters or {}).items():
        where.append(_in(col, vals))

    return (
        "SELECT " + ", ".join(cols)
        + " FROM daily WHERE " + " AND ".join(where)
        + " GROUP BY " + ", ".join(["month", *dims])
    )


def _compare_sql(period, prior, group_by=(), filters=None,
                 order=None, limit=None, share_of=None) -> str:
    """
    Two months side by side, one row per group.

    Pivoted in SQL rather than in Python so the query shown as evidence is the
    query that produced every number in the answer, deltas included.
    """
    dims = list(group_by)
    keys = ", ".join(dims)
    lead = keys + ", " if dims else ""

    held = []
    for c in (*_PIVOT, "cost_per_shrunk_unit"):
        for suffix, month in (("_prior", prior), ("", period)):
            held.append(
                f"COALESCE(MAX(CASE WHEN month = {_lit(month)} THEN {c} END), 0) "
                f"AS {c}{suffix}"
            )

    final = list(dims)
    for c in _PIVOT:
        final += [
            f"{c}_prior", c,
            f"{c} - {c}_prior AS {c}_delta",
            f"({c} - {c}_prior) / NULLIF(ABS({c}_prior), 0) AS {c}_pct",
        ]
    final += ["cost_per_shrunk_unit_prior", "cost_per_shrunk_unit"]
    if share_of:
        final.append(
            f"({share_of} - {share_of}_prior) / "
            f"NULLIF(SUM({share_of} - {share_of}_prior) OVER (), 0) AS share_of_change"
        )

    sql = (
        "WITH m AS (" + _select([prior, period], dims, filters) + "), "
        "p AS (SELECT " + lead + ", ".join(held) + " FROM m"
        + (f" GROUP BY {keys}" if dims else "") + ") "
        "SELECT " + ", ".join(final) + " FROM p"
    )
    if order:
        sql += f" ORDER BY {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return sql


# --- The four named questions ---------------------------------------------


def shrink(sem, period, scope=DEFAULT_SCOPE, filters=None,
           group_by=(), limit=MAX_ROWS) -> dict:
    """How much shrink, for one month, optionally split one way."""
    sql = _select([period], group_by, filters)
    if group_by:
        sql += f" ORDER BY shrink_units DESC LIMIT {int(limit)}"
    return _run(sem, sql, scope, limit)


def compare(sem, period, prior, scope=DEFAULT_SCOPE, filters=None,
            group_by=(), limit=MAX_ROWS) -> dict:
    """Two months side by side, with deltas on both measures."""
    sql = _compare_sql(
        period, prior, group_by, filters,
        order="shrink_units_delta DESC" if group_by else None,
        limit=limit if group_by else None,
    )
    return _run(sem, sql, scope, limit)


def rank(sem, period, by, measure=DEFAULT_MEASURE, scope=DEFAULT_SCOPE,
         filters=None, limit=10, ascending=False) -> dict:
    """
    Top or bottom N of one dimension.

    Every row carries both measures regardless of which one it sorted by, so
    one call is enough to see whether the ranking would change under the other
    definition.
    """
    limit = min(int(limit), MAX_ROWS)
    sql = (_select([period], (by,), filters)
           + f" ORDER BY {MEASURES[measure]['metric']} "
           + ("ASC" if ascending else "DESC")
           + f" LIMIT {limit}")
    return _run(sem, sql, scope, limit)


def drivers(sem, period, prior, dimension, measure=DEFAULT_MEASURE,
            scope=DEFAULT_SCOPE, filters=None, limit=5) -> dict:
    """
    Decompose a month-over-month move.

    Answers two things a ranking cannot: which slices account for the change,
    and for each of them whether shipments rose or sales fell. Those imply
    opposite corrective actions -- cut the order, versus move the product -- so
    the split is the point of the tool.
    """
    limit = min(int(limit), MAX_ROWS)
    col = MEASURES[measure]["metric"]
    out = _run(sem, _compare_sql(
        period, prior, (dimension,), filters,
        order=f"{col}_delta DESC", limit=limit, share_of=col,
    ), scope, limit)
    out["causes"] = [_cause(r, dimension, col) for r in out["rows"]]
    return out


# --- Reading the rows ------------------------------------------------------
#
# The two functions below are the only judgement calls in this file. Neither
# invents a number: they read deltas the query already computed and say what
# kind of problem the shape implies.


def _cause(row, dimension, col) -> dict:
    """Ordering problem or demand problem, for one slice of a decomposition."""
    ship, sold = row["shipped_units_delta"], row["sold_units_delta"]
    if ship > 0 and sold < 0:
        cause = "shipments up and sales down"
    elif abs(sold) > abs(ship) * 1.5:
        cause = "sales moved while shipments held roughly flat"
    elif abs(ship) > abs(sold) * 1.5:
        cause = "shipments moved while sales held roughly flat"
    else:
        cause = "shipments and sales both moved"
    return {
        "group": row[dimension],
        "delta": row[f"{col}_delta"],
        "share_of_change": row.get("share_of_change"),
        "shipped_units_delta": ship,
        "sold_units_delta": sold,
        "cause": cause,
    }


def mix_effect(row) -> str | None:
    """
    Why units and cost can disagree, read off an ungrouped `compare` row.

    When unit shrink rises and cost shrink does not, the average cost of a
    shrunk unit is the number that explains it: the extra loss landed on cheaper
    product. Returns None when the two measures agree and there is nothing to
    reconcile.
    """
    u, c = row.get("shrink_units_pct"), row.get("shrink_cost_pct")
    if u is None or c is None or not (u > 0.02 > c or c > 0.02 > u):
        return None
    before, after = row["cost_per_shrunk_unit_prior"], row["cost_per_shrunk_unit"]
    direction = "fell" if after < before else "rose"
    return (
        f"The two measures disagree this period: units {u:+.1%} against cost "
        f"{c:+.1%}. The gap is mix -- the average cost of a shrunk unit "
        f"{direction} from ${before:,.2f} to ${after:,.2f}. Report both, and say "
        "the choice of measure changes the conclusion."
    )


def _run(sem, sql: str, scope: str, limit: int) -> dict:
    """Run one built query through the governed view. Scope is bound there."""
    out = sem.run(sql, scope, limit)
    return {"sql": sql, "columns": out["columns"], "rows": out["rows"]}
