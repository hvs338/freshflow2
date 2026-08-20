"""
The contract the model sees: what each tool is for, and what it accepts.

Declarations only -- no validation, no dispatch, no database. tools.py enforces
these; router.py reuses the shared argument blocks for the `query` escape hatch
so the wording exists once.

The nullable `measure` and `scope` blocks are the grounding mechanism for the
whole assignment. Both descriptions say, in as many words, that null is a valid
and expected answer: a model that cannot decline to resolve Meridian's two
contested definitions will always guess at them.
"""

from __future__ import annotations

import metrics

# Defaults for the two tools that take a row limit.
DEFAULT_RANK_ROWS = 10
DEFAULT_DRIVER_ROWS = 5


# --- Shared argument blocks ------------------------------------------------

NULLABLE_MEASURE = {
    "type": ["string", "null"],
    "enum": ["units", "cost", None],
    "description": (
        "Count shrink in physical units or in dollars of cost. At Meridian "
        "these do not move together and can point in opposite directions in "
        "the same month. Set this ONLY if the user clearly indicated one. Null "
        "means the user did not specify, which is a valid and expected answer."
    ),
}

NULLABLE_SORT_BY = {
    "type": ["string", "null"],
    "enum": list(metrics.SORT_BY) + [None],
    "description": (
        "Column to rank by: 'units' (shrink units), 'cost' (shrink cost), "
        "'revenue' (retail net_sales), or 'sold' (units sold). "
        "Null defaults to units and the result says so."
    ),
}

NULLABLE_SCOPE = {
    "type": ["string", "null"],
    "enum": ["fresh", "all", None],
    "description": (
        "'fresh' is the five fresh departments, excluding Grocery, which is "
        "what ops usually means. 'all' includes Grocery. Set this ONLY if the "
        "user said so. Null means the user did not specify, which is a valid "
        "and expected answer."
    ),
}

FILTERS = {
    "type": "object",
    "description": (
        "Row filters. Omit any key that does not apply. Values are matched "
        "against the data; anything that does not resolve comes back as an "
        "error rather than being ignored."
    ),
    "properties": {
        dimension: {
            "type": "array",
            "items": {"type": "integer" if dimension == "store_id" else "string"},
        }
        for dimension in metrics.DIMENSIONS
    },
}

DIMENSION = {
    "type": "string",
    "enum": list(metrics.DIMENSIONS),
    "description": "A dimension to split by. 'description' means item level.",
}

OPTIONAL_DIMENSION = {
    **DIMENSION,
    "description": DIMENSION["description"] + " Optional.",
}


def _month(what_it_selects: str) -> dict:
    return {"type": "string", "description": what_it_selects + " Format YYYY-MM."}


def _row_limit(what_it_counts: str, default: int) -> dict:
    return {"type": "integer", "description": f"How many {what_it_counts}. Default {default}."}


# --- The four typed tools --------------------------------------------------

TOOL_DEFINITIONS = {
    "shrink": {
        "description": (
            "Total shrink for one month, optionally split by one dimension. "
            "Returns both units and cost every time. Use this for 'how much "
            "shrink', not for comparisons."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "period": _month("The month to measure."),
                "measure": NULLABLE_MEASURE,
                "scope": NULLABLE_SCOPE,
                "filters": FILTERS,
                "group_by": OPTIONAL_DIMENSION,
            },
            "required": ["period"],
        },
    },
    "compare": {
        "description": (
            "Two months side by side with deltas and percentages on both "
            "measures. Use this for 'up or down versus last month'. When the "
            "two measures disagree it says so."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "period": _month("The month of interest."),
                "prior": _month(
                    "The month to compare against. Defaults to the month before."
                ),
                "measure": NULLABLE_MEASURE,
                "scope": NULLABLE_SCOPE,
                "filters": FILTERS,
                "group_by": OPTIONAL_DIMENSION,
            },
            "required": ["period"],
        },
    },
    "rank": {
        "description": (
            "Top or bottom N of some dimension for one month. Sorts by shrink "
            "units/cost, revenue, or units sold. A null sort_by defaults to "
            "shrink units and the result says so. Every row carries all shrink "
            "columns regardless of sort key."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "period": _month("The month to rank within."),
                "by": DIMENSION,
                "sort_by": NULLABLE_SORT_BY,
                "scope": NULLABLE_SCOPE,
                "filters": FILTERS,
                "limit": _row_limit("rows", DEFAULT_RANK_ROWS),
                "ascending": {
                    "type": "boolean",
                    "description": "True only if the user asked for the lowest or best.",
                },
            },
            "required": ["period", "by"],
        },
    },
    "drivers": {
        "description": (
            "Decompose a month-over-month move: which slices account for the "
            "change, and for each one whether shipments rose or sales fell. "
            "Those imply opposite corrective actions. Use this for 'why'."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "period": _month("The month of interest."),
                "prior": _month(
                    "The month to compare against. Defaults to the month before."
                ),
                "dimension": {
                    **DIMENSION,
                    "description": "Dimension to decompose the change by.",
                },
                "measure": NULLABLE_MEASURE,
                "scope": NULLABLE_SCOPE,
                "filters": FILTERS,
                "limit": _row_limit("contributors", DEFAULT_DRIVER_ROWS),
            },
            "required": ["period", "dimension"],
        },
    },
}
