"""
What the numbers mean, and the four questions worth naming.

The top half is definitions: Meridian's business logic written as SQL fragments
so that exactly one statement of "what shrink is" exists. It gets used four
ways:

  * semantic.py builds the view from DERIVED
  * router.py puts METRICS in the prompt, so the model does not invent five
    spellings of "shrink rate"
  * router.resolve reads SCOPES and MEASURES to tell the user which definition
    an answer used
  * the bottom half of this file assembles them into SQL

The bottom half executes. `shrink`, `compare`, `rank` and `drivers` are the four
shapes a merchandiser actually asks for, built from the fragments above and run
through the same governed view the model queries directly. They exist so the
common questions are answered by reviewed code rather than by whatever SQL the
model improvised this turn -- in particular so that "why did it move" always
decomposes the same way and always splits an ordering problem from a demand one.

Change a definition here and it changes everywhere: the view, the prompt, these
functions, and what the user is told.
"""

# --- The grain of the data ------------------------------------------------

FRESH_DEPTS = ("Produce", "Dairy", "Meat", "Bakery", "Deli")
ALL_DEPTS = FRESH_DEPTS + ("Grocery",)

# --- Derived columns, in SQL ----------------------------------------------
#
# Meridian ships CASES; every question is about UNITS. That conversion is the
# most commonly fumbled thing in this dataset, so it is written out once here
# rather than assumed anywhere.

DERIVED = {
    "shipped_units": "cases_received * case_size",
    "shipped_cost": "cases_received * case_size * unit_cost",
    "sold_units": "units_sold",
    "sold_cost": "units_sold * unit_cost",
}

# --- Named metrics --------------------------------------------------------
#
# The aggregates a merchandiser actually asks for. These go into the prompt
# verbatim. Naming them means "shrink rate" is one expression rather than
# whatever the model improvises this time.

METRICS = {
    "shrink_units": "SUM(shipped_units - sold_units)",
    "shrink_cost": "SUM(shipped_cost - sold_cost)",
    "shrink_rate": "SUM(shipped_units - sold_units) / NULLIF(SUM(shipped_units), 0)",
    "sell_through": "SUM(sold_units) / NULLIF(SUM(shipped_units), 0)",
    "cost_per_shrunk_unit": (
        "SUM(shipped_cost - sold_cost) / NULLIF(SUM(shipped_units - sold_units), 0)"
    ),
    "units_sold": "SUM(sold_units)",
    "revenue": "SUM(net_sales)",
    "units_shipped": "SUM(shipped_units)",
}

# --- The two contested definitions ----------------------------------------
#
# Meridian's data dictionary calls both of these out as genuinely ambiguous.
# Neither has a right answer, so the system never resolves one silently: it
# picks a documented default and says so. `why` is shown to the user verbatim.

SCOPES = {
    "fresh": {
        "depts": FRESH_DEPTS,
        "label": "five fresh departments (Grocery excluded)",
        "why": (
            "Ops means the fresh departments when they say shrink. In Grocery, "
            "shipped-minus-sold is mostly inventory build rather than waste."
        ),
    },
    "all": {
        "depts": ALL_DEPTS,
        "label": "all six departments (Grocery included)",
        "why": "Includes center store, where the gap is largely inventory build.",
    },
}

MEASURES = {
    "units": {
        "label": "units",
        "metric": "shrink_units",
        "why": "Counts physical product lost, regardless of what it cost.",
    },
    "cost": {
        "label": "cost",
        "metric": "shrink_cost",
        "why": "Weights each lost unit by cost, so it tracks dollars at risk.",
    },
}

DEFAULT_SCOPE = "fresh"
DEFAULT_MEASURE = "units"

SHRINK_DEFINITION = (
    "shrink = units shipped (cases_received x case_size) minus units sold, "
    "summed over whatever period and grouping the question needs"
)

# Dimensions a question may split or filter on. Anything outside this list is
# rejected by tools.py rather than guessed at.
DIMENSIONS = ("dept", "category", "region", "banner", "store_id", "description")


# ==========================================================================
# Executing the definitions
# ==========================================================================
#
# Everything below builds SQL and runs it through the same Semantic view the
# model queries directly, so scope is still bound by the harness and the join is
# still stated exactly once, in semantic.py.
#
# The SQL is assembled by string interpolation. That is only safe because every
# interpolated value -- months, dimensions, filter values -- is validated
# against the actual data in tools.py before it gets here. Nothing user-authored
# reaches these functions unchecked. See Tools._month / _dimension / _filters.

MAX_ROWS = 25

# The four measures a comparison pivots. Deltas and percentages are computed in
# SQL, not in Python, so no number reaches the user through arithmetic that
# happens outside the query whose text is shown as evidence.
_PIVOT = ("shipped_units", "sold_units", "shrink_units", "shrink_cost")


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

    Every tool in this file starts here, so the columns mean the same thing
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
