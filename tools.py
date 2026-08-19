"""
Validating what the model asked for, then calling the query layer.

The schemas live in tool_schemas.py; this file enforces them. Every argument is
checked against the actual data before any SQL is built, which is the property
the whole design rests on:

  * An unresolvable region or a month outside the extract comes back as an error
    the model reads and corrects, never as a silent guess.

  * A filter is never quietly dropped. A dropped filter means a number computed
    over the wrong rows while still looking right, which is the worst available
    failure for this product.

That second point is also what makes the string interpolation in queries.py
safe: nothing reaches a SQL builder that did not first match a real value.

The file reads in three parts: the validators, the four handlers that use them,
and the result shape they all return.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass

import metrics
import queries
import router
from tool_schemas import DEFAULT_DRIVER_ROWS, DEFAULT_RANK_ROWS, TOOL_DEFINITIONS

# How many valid values an error message lists before it summarises.
VALUES_SHOWN_IN_ERRORS = 8


class ToolError(Exception):
    """A bad argument. Shown to the model as a result, never raised at the user."""


@dataclass(frozen=True)
class CommonArguments:
    """
    The four arguments every typed tool accepts, already validated.

    Parsed once and passed around rather than re-read from the raw input in
    each handler, so there is exactly one place that decides what "no scope
    given" means.
    """

    period: str
    measure: str | None
    scope: str | None
    filters: dict

    @property
    def scope_to_query(self) -> str:
        """The scope a query actually runs under. Null becomes the ops default."""
        return self.scope or metrics.DEFAULT_SCOPE


class Tools:
    """
    Validate arguments against the data, then call the query layer.

    Scope is deliberately not handled here. It stays enforced where it already
    was -- `Semantic.run` rebinds `daily` to the right departments before every
    query -- so the typed tools inherit that property rather than reimplementing
    it and getting a second chance to disagree.
    """

    def __init__(self, semantic_view):
        self.view = semantic_view
        self.known_values = {
            dimension: semantic_view.values_of(dimension)
            for dimension in metrics.DIMENSIONS
        }
        self.months = find_complete_months(semantic_view)

    def specs(self) -> list[dict]:
        """Every tool the model is offered, typed four first then the escape hatch."""
        typed_specs = [
            {
                "name": name,
                "description": definition["description"],
                "schema": definition["schema"],
            }
            for name, definition in TOOL_DEFINITIONS.items()
        ]
        return typed_specs + [router.query_tool()]

    def call(self, name: str, tool_input: dict) -> dict:
        """
        Run one typed tool. Returns {sql, columns, rows, notes, ...}.

        Raises ToolError for anything the model got wrong, which agent.py hands
        straight back so it can read the reason and try again.
        """
        handlers = {
            "shrink": self._handle_shrink,
            "compare": self._handle_compare,
            "rank": self._handle_rank,
            "drivers": self._handle_drivers,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ToolError(
                f"No such tool {name!r}. Available: {', '.join(handlers)}."
            )
        return handler(tool_input or {})

    # -- argument validation ------------------------------------------------

    def _parse_common_arguments(self, tool_input: dict) -> CommonArguments:
        """The four arguments every typed tool shares, validated together."""
        return CommonArguments(
            period=self.validate_period(tool_input.get("period"), "period"),
            measure=self.validate_measure(tool_input.get("measure")),
            scope=self.validate_scope(tool_input.get("scope")),
            filters=self.validate_filters(tool_input.get("filters")),
        )

    def validate_period(
        self, raw_value, field_name: str, required: bool = True
    ) -> str | None:
        """A month the extract covers end to end, or an error naming the ones it does."""
        if raw_value in (None, ""):
            if required:
                raise ToolError(f"{field_name} is required, as YYYY-MM.")
            return None

        month = str(raw_value).strip()
        if month not in self.months:
            raise ToolError(
                f"{month} is not a complete month in this extract. Available: "
                f"{', '.join(self.months)}."
            )
        return month

    def validate_prior_period(self, raw_value, period: str) -> str:
        """The month before `period`, unless the model named one."""
        named_month = self.validate_period(raw_value, "prior", required=False)
        if named_month:
            return named_month

        position = self.months.index(period)
        if position == 0:
            raise ToolError(
                f"{period} is the first month in the extract, so there is "
                "nothing before it to compare against."
            )
        return self.months[position - 1]

    @staticmethod
    def validate_measure(raw_value) -> str | None:
        """Units, cost, or null. Null is a valid answer, not a missing one."""
        if raw_value in (None, "", "null"):
            return None
        if raw_value not in metrics.MEASURES:
            raise ToolError(
                f"measure must be 'units', 'cost', or null; got {raw_value!r}."
            )
        return raw_value

    @staticmethod
    def validate_scope(raw_value) -> str | None:
        """Fresh, all, or null. Null is a valid answer, not a missing one."""
        if raw_value in (None, "", "null"):
            return None
        if raw_value not in metrics.SCOPES:
            raise ToolError(
                f"scope must be 'fresh', 'all', or null; got {raw_value!r}."
            )
        return raw_value

    @staticmethod
    def validate_dimension(raw_value, field_name: str) -> str:
        """A column the data can actually be split by."""
        if raw_value not in metrics.DIMENSIONS:
            raise ToolError(
                f"{field_name} must be one of {', '.join(metrics.DIMENSIONS)}; "
                f"got {raw_value!r}."
            )
        return raw_value

    def parse_optional_group_by(self, tool_input: dict) -> tuple:
        """`group_by` as the tuple the query layer wants, or empty if absent."""
        requested = tool_input.get("group_by")
        if not requested:
            return ()
        return (self.validate_dimension(requested, "group_by"),)

    def validate_filters(self, raw_filters) -> dict:
        """
        Resolve every filter value against the real column values.

        Unresolvable values are an error, not a drop. A silently dropped filter
        is a number computed over the wrong rows, which is worse than no answer
        because it still looks like one.
        """
        raw_filters = raw_filters or {}
        if not isinstance(raw_filters, dict):
            raise ToolError("filters must be an object.")

        resolved_filters: dict[str, list] = {}
        for column, requested_values in raw_filters.items():
            if column not in self.known_values:
                raise ToolError(
                    f"Cannot filter on {column!r}. Filterable: "
                    f"{', '.join(metrics.DIMENSIONS)}."
                )
            matches = self._resolve_filter_values(column, requested_values)
            if matches:
                resolved_filters[column] = matches
        return resolved_filters

    def _resolve_filter_values(self, column: str, requested_values) -> list:
        """Every requested value for one column, or an error naming the failures."""
        if not isinstance(requested_values, list):
            requested_values = [requested_values]

        matched, unmatched = [], []
        for requested in requested_values:
            match = self._match_one_value(column, requested)
            if match is None:
                unmatched.append(requested)
            else:
                matched.append(match)

        if unmatched:
            raise ToolError(
                f"No {column} matching: "
                f"{', '.join(str(value) for value in unmatched)}. "
                f"Valid values: {_summarize_options(self.known_values[column])}."
            )
        return matched

    def _match_one_value(self, column: str, value):
        """Exact, then case-insensitive, then a substring that matches one thing."""
        options = self.known_values[column]

        if column == "store_id":
            try:
                store_id = int(value)
            except (TypeError, ValueError):
                return None
            return store_id if store_id in options else None

        text = str(value).strip()
        if text in options:
            return text

        by_lowercase = {str(option).lower(): option for option in options}
        if text.lower() in by_lowercase:
            return by_lowercase[text.lower()]

        substring_matches = [
            option for option in options if text.lower() in str(option).lower()
        ]
        return substring_matches[0] if len(substring_matches) == 1 else None

    @staticmethod
    def resolve_sort_measure(measure: str | None, action: str) -> tuple[str, list[str]]:
        """
        A ranking or a decomposition needs one column to sort on.

        Null cannot survive here, so it becomes a disclosed default: the measure
        to use, plus the note telling the model to say it was not the user's
        choice. Returned rather than appended to a caller's list, so nothing is
        mutated behind the caller's back.
        """
        if measure is not None:
            return measure, []

        defaulted = metrics.DEFAULT_MEASURE
        note = (
            f"No measure was specified. {action} by "
            f"{metrics.MEASURES[defaulted]['label']} because this needs a single "
            "column. Tell the user that was a default, not their choice, and "
            "name the alternative."
        )
        return defaulted, [note]

    # -- the four tools -----------------------------------------------------

    def _handle_shrink(self, tool_input: dict) -> dict:
        arguments = self._parse_common_arguments(tool_input)
        group_by = self.parse_optional_group_by(tool_input)

        result = queries.shrink(
            self.view,
            arguments.period,
            arguments.scope_to_query,
            arguments.filters,
            group_by,
        )
        return _build_result(result, arguments, notes=[])

    def _handle_compare(self, tool_input: dict) -> dict:
        arguments = self._parse_common_arguments(tool_input)
        prior = self.validate_prior_period(tool_input.get("prior"), arguments.period)
        group_by = self.parse_optional_group_by(tool_input)

        result = queries.compare(
            self.view,
            arguments.period,
            prior,
            arguments.scope_to_query,
            arguments.filters,
            group_by,
        )
        notes = [f"{arguments.period} versus {prior}."]
        notes += _mix_effect_notes(result, is_grouped=bool(group_by))
        return _build_result(result, arguments, notes)

    def _handle_rank(self, tool_input: dict) -> dict:
        arguments = self._parse_common_arguments(tool_input)
        rank_by = self.validate_dimension(tool_input.get("by"), "by")
        sort_measure, notes = self.resolve_sort_measure(arguments.measure, "Ranked")

        result = queries.rank(
            self.view,
            arguments.period,
            rank_by,
            sort_measure,
            arguments.scope_to_query,
            arguments.filters,
            _as_int(tool_input.get("limit"), DEFAULT_RANK_ROWS),
            bool(tool_input.get("ascending")),
        )
        return _build_result(
            result,
            arguments,
            notes,
            sorted_by=metrics.MEASURES[sort_measure]["metric"],
        )

    def _handle_drivers(self, tool_input: dict) -> dict:
        arguments = self._parse_common_arguments(tool_input)
        prior = self.validate_prior_period(tool_input.get("prior"), arguments.period)
        dimension = self.validate_dimension(tool_input.get("dimension"), "dimension")
        sort_measure, measure_notes = self.resolve_sort_measure(
            arguments.measure, "Decomposed"
        )

        notes = [f"{arguments.period} versus {prior}."] + measure_notes
        notes += _pinned_dimension_notes(dimension, arguments.filters)

        result = queries.drivers(
            self.view,
            arguments.period,
            prior,
            dimension,
            sort_measure,
            arguments.scope_to_query,
            arguments.filters,
            _as_int(tool_input.get("limit"), DEFAULT_DRIVER_ROWS),
        )
        return _build_result(result, arguments, notes, causes=result["causes"])


# --- Notes the harness adds without being asked ----------------------------


def _mix_effect_notes(result: dict, is_grouped: bool) -> list[str]:
    """
    The planted story: units and cost disagree May to June.

    Surfaced here rather than left for the model to spot in four decimal places.
    Only meaningful on an ungrouped comparison, where there is one row to read.
    """
    if is_grouped or not result["rows"]:
        return []
    explanation = queries.mix_effect(result["rows"][0])
    return [explanation] if explanation else []


def _pinned_dimension_notes(dimension: str, filters: dict) -> list[str]:
    """
    Decomposing by a dimension the question already pinned to one value returns
    one row at 100% of the change, which tells the user nothing. Say so rather
    than switching dimensions behind the model's back.
    """
    if len(filters.get(dimension, ())) != 1:
        return []
    return [
        f"The filters already pin {dimension} to a single value, so this "
        "returns one row at 100%. Call drivers again with a finer dimension to "
        "learn anything."
    ]


# --- The shape every tool returns ------------------------------------------


def _build_result(
    result: dict,
    arguments: CommonArguments,
    notes: list[str],
    **extra_fields,
) -> dict:
    """One shape for every typed tool, so agent.py has one thing to render."""
    if not result["rows"]:
        notes = notes + ["No rows. Check the filters before reporting a zero."]

    return {
        "sql": result["sql"],
        "columns": result["columns"],
        "rows": result["rows"],
        "notes": notes,
        "measure": arguments.measure,
        "scope": arguments.scope,
        **extra_fields,
    }


# --- Reading the calendar --------------------------------------------------


def find_complete_months(semantic_view) -> list[str]:
    """
    Months the extract covers end to end.

    A partial trailing month is the trap here: comparing twelve days against a
    full prior month looks like a collapse in shrink and is entirely an
    artefact. Better to refuse the month than to answer it.
    """
    first_date, last_date = semantic_view.date_range()
    months = _months_present(semantic_view)

    return [
        month
        for month in months
        if _month_is_complete(month, first_date, last_date)
    ]


def _months_present(semantic_view) -> list[str]:
    """Every month with at least one row, oldest first."""
    result = semantic_view.run(
        "SELECT DISTINCT strftime(date, '%Y-%m') AS month FROM daily ORDER BY 1",
        "all",
        row_limit=240,
    )
    return [row["month"] for row in result["rows"]]


def _month_is_complete(month: str, first_date: str, last_date: str) -> bool:
    """
    True unless this is a partly covered month at either end of the extract.

    Months in the middle are complete by construction: the extract is
    contiguous, so only the first and last can be clipped.
    """
    starts_mid_month = month == first_date[:7] and not _is_first_of_month(first_date)
    ends_mid_month = month == last_date[:7] and not _is_last_of_month(last_date)
    return not starts_mid_month and not ends_mid_month


def _is_first_of_month(date_text: str) -> bool:
    return date_text[8:] == "01"


def _is_last_of_month(date_text: str) -> bool:
    year, month, day = (int(part) for part in date_text.split("-"))
    days_in_month = monthrange(year, month)[1]
    return day == days_in_month


# --- Small shared helpers --------------------------------------------------


def _as_int(value, default: int) -> int:
    """A row limit the model sent, or the default when it sent nonsense."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _summarize_options(options, shown: int = VALUES_SHOWN_IN_ERRORS) -> str:
    """The first few valid values, and how many there are in total."""
    options = list(options)
    listed = ", ".join(str(option) for option in options[:shown])
    if len(options) <= shown:
        return listed
    return f"{listed}, ... ({len(options)} in total)"
