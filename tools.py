"""
What the model is allowed to ask for.

Five tools. Four of them are the named questions from metrics.py -- how much,
up or down, top N, and why -- and the fifth is `query`, the SQL escape hatch for
everything they do not express. The typed four carry the reviewed shrink logic;
`query` does not, which is why the prompt tells the model to prefer them.

Two properties are load-bearing:

  * `measure` and `scope` are nullable on every tool, and the descriptions say
    null means the user did not specify. A model that cannot decline to resolve
    Meridian's two contested definitions will always guess at them.

  * Every argument is checked against the actual data before any SQL is built.
    An unresolvable region or a month outside the extract comes back as an error
    the model reads and corrects, never as a silent guess -- and never as a
    filter quietly dropped, which would mean a number computed over the wrong
    rows while still looking right.

That second property is also what makes the string interpolation in metrics.py
safe: nothing reaches a SQL builder that did not first match a real value.
"""

from __future__ import annotations

import metrics as M

# Hoisted here rather than written twice: `query` in router.py takes the same
# two arguments, and the wording of "null is a valid answer" is the grounding
# mechanism for the whole assignment. It should exist once.
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

_FILTERS = {
    "type": "object",
    "description": (
        "Row filters. Omit any key that does not apply. Values are matched "
        "against the data; anything that does not resolve comes back as an "
        "error rather than being ignored."
    ),
    "properties": {
        d: {"type": "array", "items": {"type": "integer" if d == "store_id" else "string"}}
        for d in M.DIMENSIONS
    },
}

_DIMENSION = {
    "type": "string",
    "enum": list(M.DIMENSIONS),
    "description": "A dimension to split by. 'description' means item level.",
}


def _month(what: str) -> dict:
    return {"type": "string", "description": what + " Format YYYY-MM."}


SCHEMAS = {
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
                "filters": _FILTERS,
                "group_by": {**_DIMENSION, "description": _DIMENSION["description"] + " Optional."},
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
                "prior": _month("The month to compare against. Defaults to the month before."),
                "measure": NULLABLE_MEASURE,
                "scope": NULLABLE_SCOPE,
                "filters": _FILTERS,
                "group_by": {**_DIMENSION, "description": _DIMENSION["description"] + " Optional."},
            },
            "required": ["period"],
        },
    },
    "rank": {
        "description": (
            "Top or bottom N of some dimension by shrink, for one month. Needs "
            "one column to sort by, so a null measure is defaulted to units and "
            "the result says so. Every row carries BOTH units and cost "
            "regardless of which it sorted by -- one call is enough to compare "
            "the two, so do not call this twice."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "period": _month("The month to rank within."),
                "by": _DIMENSION,
                "measure": NULLABLE_MEASURE,
                "scope": NULLABLE_SCOPE,
                "filters": _FILTERS,
                "limit": {"type": "integer", "description": "How many rows. Default 10."},
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
                "prior": _month("The month to compare against. Defaults to the month before."),
                "dimension": {**_DIMENSION, "description": "Dimension to decompose the change by."},
                "measure": NULLABLE_MEASURE,
                "scope": NULLABLE_SCOPE,
                "filters": _FILTERS,
                "limit": {"type": "integer", "description": "How many contributors. Default 5."},
            },
            "required": ["period", "dimension"],
        },
    },
}


class ToolError(Exception):
    """A bad argument. Shown to the model as a result, never raised at the user."""


class Tools:
    """
    Validate arguments against the data, then call the metric layer.

    Scope is deliberately not handled here. It stays enforced where it already
    was -- `Semantic.run` rebinds `daily` to the right departments before every
    query -- so the typed tools inherit that property rather than reimplementing
    it and getting a second chance to disagree.
    """

    def __init__(self, sem):
        self.sem = sem
        self.vocab = {d: sem.values_of(d) for d in M.DIMENSIONS}
        self.months = _complete_months(sem)

    def specs(self) -> list[dict]:
        import router  # imported here: router imports this module for its schemas

        return [
            {"name": n, "description": s["description"], "schema": s["schema"]}
            for n, s in SCHEMAS.items()
        ] + [router.query_tool()]

    def call(self, name: str, args: dict) -> dict:
        """
        Run one typed tool. Returns {sql, columns, rows, notes, ...}.

        Raises ToolError for anything the model got wrong, which agent.py hands
        straight back so it can read the reason and try again.
        """
        handler = {"shrink": self._shrink, "compare": self._compare,
                   "rank": self._rank, "drivers": self._drivers}.get(name)
        if handler is None:
            raise ToolError(f"No such tool {name!r}.")
        return handler(args or {})

    # -- argument validation ------------------------------------------------

    def _period(self, raw, field: str, required: bool = True) -> str | None:
        if raw in (None, ""):
            if required:
                raise ToolError(f"{field} is required, as YYYY-MM.")
            return None
        month = str(raw).strip()
        if month not in self.months:
            raise ToolError(
                f"{month} is not a complete month in this extract. Available: "
                f"{', '.join(self.months)}."
            )
        return month

    def _prior(self, raw, period: str) -> str:
        """The month before `period`, unless the model named one."""
        named = self._period(raw, "prior", required=False)
        if named:
            return named
        i = self.months.index(period)
        if i == 0:
            raise ToolError(
                f"{period} is the first month in the extract, so there is "
                "nothing before it to compare against."
            )
        return self.months[i - 1]

    @staticmethod
    def _measure(raw) -> str | None:
        if raw in (None, "", "null"):
            return None
        if raw not in M.MEASURES:
            raise ToolError(f"measure must be 'units', 'cost', or null; got {raw!r}.")
        return raw

    @staticmethod
    def _scope(raw) -> str | None:
        if raw in (None, "", "null"):
            return None
        if raw not in M.SCOPES:
            raise ToolError(f"scope must be 'fresh', 'all', or null; got {raw!r}.")
        return raw

    @staticmethod
    def _dimension(raw, field: str) -> str:
        if raw not in M.DIMENSIONS:
            raise ToolError(
                f"{field} must be one of {', '.join(M.DIMENSIONS)}; got {raw!r}."
            )
        return raw

    def _filters(self, raw) -> dict:
        """
        Resolve every filter value against the real column values.

        Unresolvable values are an error, not a drop. A silently dropped filter
        is a number computed over the wrong rows, which is worse than no answer
        because it still looks like one.
        """
        raw = raw or {}
        if not isinstance(raw, dict):
            raise ToolError("filters must be an object.")

        out: dict[str, list] = {}
        for key, vals in raw.items():
            if key not in self.vocab:
                raise ToolError(
                    f"Cannot filter on {key!r}. Filterable: {', '.join(M.DIMENSIONS)}."
                )
            if not isinstance(vals, list):
                vals = [vals]
            kept, missed = [], []
            for v in vals:
                match = self._resolve(key, v)
                (kept if match is not None else missed).append(
                    match if match is not None else v
                )
            if missed:
                raise ToolError(
                    f"No {key} matching: {', '.join(str(m) for m in missed)}. "
                    f"Valid values: {_sample(self.vocab[key])}."
                )
            if kept:
                out[key] = kept
        return out

    def _resolve(self, key: str, value):
        """Exact, then case-insensitive, then a substring that matches one thing."""
        options = self.vocab[key]
        if key == "store_id":
            try:
                value = int(value)
            except (TypeError, ValueError):
                return None
            return value if value in options else None

        value = str(value).strip()
        if value in options:
            return value
        lower = {str(o).lower(): o for o in options}
        if value.lower() in lower:
            return lower[value.lower()]
        hits = [o for o in options if value.lower() in str(o).lower()]
        return hits[0] if len(hits) == 1 else None

    def _sorted_measure(self, measure, what: str, notes: list[str]) -> str:
        """
        A ranking or a decomposition needs one column. Null cannot survive here,
        so it becomes a disclosed default rather than a silent one.
        """
        if measure is not None:
            return measure
        used = M.DEFAULT_MEASURE
        notes.append(
            f"No measure was specified. {what} by {M.MEASURES[used]['label']} "
            f"because this needs a single column. Tell the user that was a "
            f"default, not their choice, and name the alternative."
        )
        return used

    # -- the four tools -----------------------------------------------------

    def _shrink(self, a: dict) -> dict:
        period = self._period(a.get("period"), "period")
        measure, scope = self._measure(a.get("measure")), self._scope(a.get("scope"))
        filters = self._filters(a.get("filters"))
        group_by = (self._dimension(a["group_by"], "group_by"),) if a.get("group_by") else ()

        out = M.shrink(self.sem, period, scope or M.DEFAULT_SCOPE, filters, group_by)
        return _result(out, measure, scope, [])

    def _compare(self, a: dict) -> dict:
        period = self._period(a.get("period"), "period")
        prior = self._prior(a.get("prior"), period)
        measure, scope = self._measure(a.get("measure")), self._scope(a.get("scope"))
        filters = self._filters(a.get("filters"))
        group_by = (self._dimension(a["group_by"], "group_by"),) if a.get("group_by") else ()

        out = M.compare(self.sem, period, prior, scope or M.DEFAULT_SCOPE, filters, group_by)
        notes = [f"{period} versus {prior}."]
        # The planted story: units and cost disagree May to June. Surface it
        # rather than hoping the model spots it in four decimal places.
        if not group_by and out["rows"]:
            mix = M.mix_effect(out["rows"][0])
            if mix:
                notes.append(mix)
        return _result(out, measure, scope, notes)

    def _rank(self, a: dict) -> dict:
        period = self._period(a.get("period"), "period")
        by = self._dimension(a.get("by"), "by")
        measure, scope = self._measure(a.get("measure")), self._scope(a.get("scope"))
        filters = self._filters(a.get("filters"))
        notes: list[str] = []
        used = self._sorted_measure(measure, "Ranked", notes)

        out = M.rank(self.sem, period, by, used, scope or M.DEFAULT_SCOPE, filters,
                     _int(a.get("limit"), 10), bool(a.get("ascending")))
        return _result(out, measure, scope, notes, sorted_by=M.MEASURES[used]["metric"])

    def _drivers(self, a: dict) -> dict:
        period = self._period(a.get("period"), "period")
        prior = self._prior(a.get("prior"), period)
        dimension = self._dimension(a.get("dimension"), "dimension")
        measure, scope = self._measure(a.get("measure")), self._scope(a.get("scope"))
        filters = self._filters(a.get("filters"))
        notes = [f"{period} versus {prior}."]
        used = self._sorted_measure(measure, "Decomposed", notes)

        # Decomposing by a dimension the question already pinned to one value
        # returns one row at 100% of the change, which tells the user nothing.
        # Say so rather than switching dimensions behind the model's back.
        if len(filters.get(dimension, ())) == 1:
            notes.append(
                f"The filters already pin {dimension} to a single value, so this "
                f"returns one row at 100%. Call drivers again with a finer "
                f"dimension to learn anything."
            )

        out = M.drivers(self.sem, period, prior, dimension, used,
                        scope or M.DEFAULT_SCOPE, filters, _int(a.get("limit"), 5))
        return _result(out, measure, scope, notes, causes=out["causes"])


def _result(out: dict, measure, scope, notes: list[str], **extra) -> dict:
    """One shape for every typed tool, so agent.py has one thing to render."""
    if not out["rows"]:
        notes = notes + ["No rows. Check the filters before reporting a zero."]
    return {
        "sql": out["sql"],
        "columns": out["columns"],
        "rows": out["rows"],
        "notes": notes,
        "measure": measure,
        "scope": scope,
        **extra,
    }


def _complete_months(sem) -> list[str]:
    """
    Months the extract covers end to end.

    A partial trailing month is the trap here: comparing twelve days against a
    full prior month looks like a collapse in shrink and is entirely an
    artefact. Better to refuse the month than to answer it.
    """
    start, end = sem.date_range()
    months = [r["month"] for r in sem.run(
        "SELECT DISTINCT strftime(date, '%Y-%m') AS month FROM daily ORDER BY 1",
        "all", 240,
    )["rows"]]
    first, last = start[:7], end[:7]
    return [
        m for m in months
        if not (m == first and start[8:] != "01") and not (m == last and not _month_end(end))
    ]


def _month_end(day: str) -> bool:
    from calendar import monthrange

    y, m, d = (int(p) for p in day.split("-"))
    return d == monthrange(y, m)[1]


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _sample(options, n: int = 8) -> str:
    shown = ", ".join(str(o) for o in list(options)[:n])
    return shown + (f", ... ({len(options)} in total)" if len(options) > n else "")
