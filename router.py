"""
How we ask the model for SQL, and what we accept back.

There is no LLM call in this file. It builds the system prompt and it defines
the one tool the model gets. Keeping that separate from agent.py means the
thing you tune during a demo -- the prompt -- is not tangled up with the loop
that runs it.

The prompt is generated from the data, not typed out by hand: real department
names, real regions, the real date range, the real column list. The model
therefore cannot invent a "Southeast" region, because it has been told the
three that exist.
"""

from __future__ import annotations

import metrics as M
from semantic import VIEW

MAX_ROWS = 50


def system_prompt(sem) -> str:
    """The whole prompt. Built at startup, from the live schema."""
    start, end = sem.date_range()
    metrics = "\n".join(f"  {name:<22} {sql}" for name, sql in M.METRICS.items())
    columns = ", ".join(sem.columns())

    return f"""You answer questions about shrink and sales for merchandising and \
operations users at Meridian Markets, a grocery chain. You answer them by \
writing SQL against one table and explaining what comes back.

THE TABLE
`{VIEW}` -- one row per store-item-day, {start} to {end}.
Columns: {columns}

`date` is a real DATE, so any grain works: date_trunc('week', date),
strftime(date, '%Y-%m'), or the raw day.

WHAT SHRINK IS
{M.SHRINK_DEFINITION}.
Shrink is not a stored column. Use these definitions so every answer means the
same thing:

{metrics}

TWO DEFINITIONS ARE CONTESTED AT MERIDIAN. DO NOT RESOLVE THEM SILENTLY.
1. measure -- shrink can be counted in units or in dollars of cost. At Meridian
   these do not move together and can point in opposite directions in the same
   month. Pass measure only when the user clearly indicated one. Otherwise pass
   null, compute both, and show both.
2. scope -- ops usually means the five fresh departments and excludes Grocery,
   but not everyone does. Pass scope only when the user said so, otherwise
   null. Do NOT filter on dept to control scope: the harness binds `{VIEW}` to
   the right departments before your query runs.

When a definition was defaulted rather than chosen, say so in your answer and
name the alternative. A number without its definition is worse than no number.

RULES
- Every number you state must come from a query result. Do no arithmetic
  yourself and never carry a number over from your own knowledge. If you want a
  number you do not have, run another query.
- Query more than once when the question needs it. A "why" question usually
  needs a breakdown, then a finer one.
- If a query is rejected or returns nothing, read the error, fix it, try again.
- Aggregate. Do not select raw rows unless the user asked for a list; you get at
  most {MAX_ROWS} rows back.
- Round nothing in SQL. Report the numbers as they come back.
- When shipments held and sales fell, that is a demand problem and the action is
  markdown or promotion. When shipments grew faster than sales, that is an
  ordering problem and the lever is order quantity. Those are opposite moves, so
  say which one the data supports.
- Nothing here records a cause. You can say where a change sits; confirming why
  needs the category team.
- If the question needs data this table does not have -- margin, forecasts,
  inventory on hand, anything outside {start} to {end} -- say so plainly.
- Be concise and lead with the answer."""


def query_tool() -> dict:
    """The only tool. One shape, so the loop in agent.py stays short."""
    return {
        "name": "query",
        "description": (
            f"Run one read-only SQL SELECT against `{VIEW}` and get the rows "
            "back. This is how you get every number you report."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        f"A single SELECT (or WITH ... SELECT) reading only "
                        f"from `{VIEW}`. Do not filter on dept for scope -- "
                        "pass the scope argument instead."
                    ),
                },
                "purpose": {
                    "type": "string",
                    "description": "One sentence: what this query is for. Shown to the user.",
                },
                "measure": {
                    "type": ["string", "null"],
                    "enum": ["units", "cost", None],
                    "description": (
                        "Which reading of shrink this answers. Null means the "
                        "user did not specify, which is a valid answer."
                    ),
                },
                "scope": {
                    "type": ["string", "null"],
                    "enum": ["fresh", "all", None],
                    "description": (
                        "'fresh' excludes Grocery, 'all' includes it. Null "
                        "means the user did not specify."
                    ),
                },
            },
            "required": ["sql", "purpose"],
        },
    }


def resolve(measure, scope) -> dict:
    """
    Turn what the model declared into what the user gets told.

    This is the disclosure, and it is the whole assignment: a defaulted
    definition is reported as a default, with the alternative named.
    """
    used_scope = scope if scope in M.SCOPES else M.DEFAULT_SCOPE
    used_measure = measure if measure in M.MEASURES else None

    other_scope = "all" if used_scope == "fresh" else "fresh"
    choices = [{
        "question": "Which departments count?",
        "chosen": M.SCOPES[used_scope]["label"],
        "why": (M.SCOPES[used_scope]["why"] + " You did not specify, so this is the ops default."
                if scope not in M.SCOPES else "You specified this."),
        "defaulted": scope not in M.SCOPES,
        "alternative": M.SCOPES[other_scope]["label"],
    }]

    if used_measure:
        other = "cost" if used_measure == "units" else "units"
        choices.append({
            "question": "Units or cost?",
            "chosen": M.MEASURES[used_measure]["label"],
            "why": "You specified this. " + M.MEASURES[used_measure]["why"],
            "defaulted": False,
            "alternative": M.MEASURES[other]["label"],
        })
    else:
        choices.append({
            "question": "Units or cost?",
            "chosen": "both, shown side by side",
            "why": ("You said shrink without saying which. These two do not move "
                    "together at Meridian, so neither was picked for you."),
            "defaulted": True,
            "alternative": None,
        })

    return {"scope": used_scope, "measure": used_measure, "choices": choices}
