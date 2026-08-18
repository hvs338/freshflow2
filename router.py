"""
How we ask the model, and what we accept back.

There is no LLM call in this file. It builds the system prompt and it defines
the SQL escape hatch. Keeping that separate from agent.py means the thing you
tune during a demo -- the prompt -- is not tangled up with the loop that runs
it. The four typed tools live in tools.py.

The prompt is generated from the data, not typed out by hand: real department
names, real regions, the real date range, and the data model from metrics.py.
The model therefore cannot invent a "Southeast" region, because it has been
told the three that exist -- and it does not have to guess whether `net_sales`
is retail or cost, because that is written down.

That claim used to be false. The prompt shipped a bare comma-separated column
list and no dimension values at all, and the model found out "Southeast" did not
exist by getting zero rows back. If you change what goes into the prompt, check
this docstring still describes it.
"""

from __future__ import annotations

import metrics
import tool_schemas
from semantic import SCOPED_VIEW

MAX_ROWS = 50

# Width of the metric-name column where METRICS is listed in the prompt.
_METRIC_NAME_WIDTH = 22


def build_system_prompt(semantic_view, months: list[str]) -> str:
    """The whole prompt. Built at startup, from the live schema."""
    first_date, last_date = semantic_view.date_range()
    metric_lines = _format_metric_definitions()
    data_model = metrics.describe_data_model(semantic_view)
    latest_month = months[-1]

    return f"""You answer questions about shrink and sales for merchandising and \
operations users at Meridian Markets, a grocery chain. You answer them with \
tools and explain what comes back.

YOUR TOOLS
Four of them answer a named question with reviewed logic. Prefer these whenever
one fits, because they carry Meridian's shrink definition and `query` does not:

  shrink    how much shrink in a month, optionally split one way
  compare   up or down versus another month, with deltas on both measures
  rank      top or bottom N of a dimension
  drivers   why a month-over-month move happened: which slices moved, and
            whether each was shipments rising or sales falling

The fifth, `query`, is a SQL escape hatch for anything the four cannot express:
any grain finer than a month (daily, weekly), two dimensions at once,
best-seller and revenue questions, or anything about sales rather than shrink.
It runs whatever SQL you write, so the reviewed logic is not behind it.

Each tool result carries `notes`. Read them -- they tell you when a definition
was defaulted for you, and when the two measures disagree.

THE MONTHS
{", ".join(months)}. These are the only periods the typed tools accept, because
they are the only months this extract covers end to end; a partial month
compared against a full one looks like a collapse that never happened.

"Last month", "this month", "lately" and "recently" mean {latest_month}, the
latest complete month. Do not stop to ask which month -- use it and name it in
your answer. Ask only when the question needs a month this extract does not have.

THE TABLE (what `query` reads)
`{SCOPED_VIEW}` -- one row per store-item-day, {first_date} to {last_date}. It is
the only table you can name, and it already carries every item attribute, so you
never join.

{data_model}

WHAT SHRINK IS
{metrics.SHRINK_DEFINITION}.
The cases-to-units conversion is already applied in `shipped_units`; there is no
`cases_received` or `case_size` column to multiply. Shrink is not a stored
column either. Use these definitions so every answer means the same thing:

{metric_lines}

TWO DEFINITIONS ARE CONTESTED AT MERIDIAN. DO NOT RESOLVE THEM SILENTLY.
1. measure -- shrink can be counted in units or in dollars of cost. At Meridian
   these do not move together and can point in opposite directions in the same
   month. Pass measure only when the user clearly indicated one. Otherwise pass
   null, compute both, and show both.
2. scope -- ops usually means the five fresh departments and excludes Grocery,
   but not everyone does. Pass scope only when the user said so, otherwise
   null. Do NOT filter on dept to control scope: the harness binds
   `{SCOPED_VIEW}` to the right departments before your query runs.

When a definition was defaulted rather than chosen, say so in your answer and
name the alternative. A number without its definition is worse than no number.

RULES
- Every number you state must come from a tool result. Do no arithmetic
  yourself and never carry a number over from your own knowledge. If you want a
  number you do not have, call another tool.
- Call more than once when the question needs it. A "why" question usually
  needs a breakdown, then a finer one.
- If a call is rejected or returns nothing, read the error, fix it, try again.
- Aggregate. Do not select raw rows unless the user asked for a list; you get at
  most {MAX_ROWS} rows back.
- Round nothing in SQL. Report the numbers as they come back.
- When shipments held and sales fell, that is a demand problem and the action is
  markdown or promotion. When shipments grew faster than sales, that is an
  ordering problem and the lever is order quantity. Those are opposite moves, so
  say which one the data supports.
- Nothing here records a cause. You can say where a change sits; confirming why
  needs the category team.
- If the question needs data this table does not have -- forecasts, inventory on
  hand, labour, waste reasons, anything outside {first_date} to {last_date} --
  say so plainly. Margin is NOT in that list: net_sales - sold_cost gives it.
- Be concise and lead with the answer."""


def _format_metric_definitions() -> str:
    """The named metrics, aligned, exactly as they appear in metrics.METRICS."""
    return "\n".join(
        f"  {name:<{_METRIC_NAME_WIDTH}} {expression}"
        for name, expression in metrics.METRICS.items()
    )


def query_tool() -> dict:
    """
    The escape hatch, for questions the four typed tools cannot express.

    Its `measure` and `scope` arguments are the same objects tools.py uses, so
    the wording of "null means the user did not say" exists in one place.
    """
    return {
        "name": "query",
        "description": (
            f"Run one read-only SQL SELECT against `{SCOPED_VIEW}` and get the "
            "rows back. Use this only when none of shrink/compare/rank/drivers "
            "fits: a grain finer than a month, two dimensions at once, or sales "
            "and best-seller questions. It does not carry the reviewed shrink "
            "logic."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        f"A single SELECT (or WITH ... SELECT) reading only "
                        f"from `{SCOPED_VIEW}`. Do not filter on dept for scope "
                        "-- pass the scope argument instead."
                    ),
                },
                "purpose": {
                    "type": "string",
                    "description": (
                        "One sentence: what this query is for. Shown to the user."
                    ),
                },
                "measure": tool_schemas.NULLABLE_MEASURE,
                "scope": tool_schemas.NULLABLE_SCOPE,
            },
            "required": ["sql", "purpose"],
        },
    }


# --- The disclosure --------------------------------------------------------
#
# Turning what the model declared into what the user gets told. This is the
# whole assignment: a defaulted definition is reported as a default, with the
# alternative named. One builder per contested definition, so neither can be
# changed by accident while editing the other.


def resolve_definitions(measure, scope) -> dict:
    """
    Which scope and measure an answer used, and whether the user chose them.

    `measure` stays None when the user did not say, because both measures are
    then reported. `scope` cannot: a query has to run against some set of
    departments, so an unspecified scope becomes the documented default.
    """
    user_specified_scope = scope in metrics.SCOPES
    user_specified_measure = measure in metrics.MEASURES

    resolved_scope = scope if user_specified_scope else metrics.DEFAULT_SCOPE
    resolved_measure = measure if user_specified_measure else None

    return {
        "scope": resolved_scope,
        "measure": resolved_measure,
        "choices": [
            _describe_scope_choice(resolved_scope, user_specified_scope),
            _describe_measure_choice(resolved_measure),
        ],
    }


def _describe_scope_choice(resolved_scope: str, user_specified: bool) -> dict:
    """Which departments counted, and what the alternative was."""
    alternative = "all" if resolved_scope == "fresh" else "fresh"
    reason = (
        "You specified this."
        if user_specified
        else metrics.SCOPES[resolved_scope]["why"]
        + " You did not specify, so this is the ops default."
    )

    return {
        "question": "Which departments count?",
        "chosen": metrics.SCOPES[resolved_scope]["label"],
        "why": reason,
        "defaulted": not user_specified,
        "alternative": metrics.SCOPES[alternative]["label"],
    }


def _describe_measure_choice(resolved_measure: str | None) -> dict:
    """
    Units, cost, or both.

    Both is not a fallback -- it is the honest answer when the user did not say,
    because at Meridian the two do not move together.
    """
    if resolved_measure is None:
        return {
            "question": "Units or cost?",
            "chosen": "both, shown side by side",
            "why": (
                "You said shrink without saying which. These two do not move "
                "together at Meridian, so neither was picked for you."
            ),
            "defaulted": True,
            "alternative": None,
        }

    alternative = "cost" if resolved_measure == "units" else "units"
    return {
        "question": "Units or cost?",
        "chosen": metrics.MEASURES[resolved_measure]["label"],
        "why": "You specified this. " + metrics.MEASURES[resolved_measure]["why"],
        "defaulted": False,
        "alternative": metrics.MEASURES[alternative]["label"],
    }
