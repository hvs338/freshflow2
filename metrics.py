"""
What the numbers and the columns mean. Nothing here runs a query.

Meridian's business logic, written as SQL fragments and plain dicts so that
exactly one statement of "what shrink is" exists. This file has no imports and
no database: it is the bottom of the stack, and everything else reads from it.

  * semantic.py builds the view from DERIVED_COLUMNS and binds scope from SCOPES
  * queries.py interpolates METRICS into every query it writes
  * router.py puts METRICS, SHRINK_DEFINITION and describe_data_model() in the
    prompt, so the model neither invents five spellings of "shrink rate" nor has
    to guess whether net_sales is retail or cost
  * router.resolve_definitions reads SCOPES and MEASURES to tell the user which
    definition an answer used

Change a definition here and it changes everywhere at once: the view, the
generated SQL, the prompt, and what the user is told about which definition
they got.

The queries built from these definitions live in queries.py. The split is
deliberate -- meaning here, SQL there, execution in semantic.py -- but the two
files stay coupled on purpose: a builder that stopped reading METRICS would
quietly emit last month's definition of shrink.
"""

# --- The grain of the data ------------------------------------------------

FRESH_DEPARTMENTS = ("Produce", "Dairy", "Meat", "Bakery", "Deli")
ALL_DEPARTMENTS = FRESH_DEPARTMENTS + ("Grocery",)

# --- Derived columns, in SQL ----------------------------------------------
#
# Meridian ships CASES; every question is about UNITS. That conversion is the
# most commonly fumbled thing in this dataset, so it is written out once here
# rather than assumed anywhere.

DERIVED_COLUMNS = {
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
        "departments": FRESH_DEPARTMENTS,
        "label": "five fresh departments (Grocery excluded)",
        "why": (
            "Ops means the fresh departments when they say shrink. In Grocery, "
            "shipped-minus-sold is mostly inventory build rather than waste."
        ),
    },
    "all": {
        "departments": ALL_DEPARTMENTS,
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
DIMENSIONS = (
    "dept",
    "category",
    "region",
    "banner",
    "store_id",
    "description",
    "unit_of_measure",
)

# --- The data model, for the model that writes SQL ------------------------
#
# A column list is not a data model. `net_sales` and `sold_cost` are both
# dollars and mean entirely different things, `sold_units` counts pounds for
# some items and eaches for others, and neither fact is guessable from a name.
# The typed tools do not need this -- their arguments are validated against the
# data -- but `query` writes SQL from scratch against this table and nothing
# else, so what is not written here is not known.
#
# One line per column, stating the trap rather than restating the name.

COLUMN_MEANINGS = {
    "date": (
        "Calendar date, a real DATE. Any grain works: date_trunc('week', date), "
        "strftime(date, '%Y-%m'), or the raw day."
    ),
    "store_id": "Store. Ten of them, 101-110.",
    "item_id": (
        "Item. Primary key of the item list; joins nothing here, the attributes "
        "are already on the row."
    ),
    "banner": "Which of Meridian's two brands the store trades under.",
    "region": "Store region. Three of them.",
    "dept": (
        "Department. Scope is bound on this column by the harness -- do not "
        "filter on it to control scope."
    ),
    "category": "Sub-grouping within a department.",
    "description": "Item name as merchandising says it. Use ILIKE for partial matches.",
    "unit_of_measure": (
        "LB or EA. The unit BOTH shipped_units and sold_units are counted in, so "
        "a SUM of units adds pounds to eaches. See the warning below."
    ),
    "unit_cost": (
        "Meridian's cost per unit, in dollars. One value per item, stable across "
        "the whole window, so cost columns do not reflect price changes over time."
    ),
    "shipped_units": (
        "Units delivered that day, already converted from cases. Zero on days "
        "with no delivery."
    ),
    "shipped_cost": "shipped_units * unit_cost. COST dollars.",
    "sold_units": "Units sold that day. Zero on days with no movement.",
    "sold_cost": "sold_units * unit_cost. COST dollars, not retail.",
    "net_sales": (
        "Retail dollars taken that day. The ONLY retail column -- every other "
        "dollar column is cost."
    ),
}

# Facts about the table that are not about any one column. Each of these has
# been wrong in a draft answer at least once.
GRAIN_NOTES = (
    "(date, store_id, item_id) is unique, so COUNT(*) counts store-item-days, "
    "not sales.",
    "Rows exist where sold_units = 0 and shipped_units > 0: product delivered "
    "and never sold. They are the highest-shrink rows in the extract and they "
    "are deliberately kept, so an AVG over this table averages over "
    "days-with-no-movement too.",
    "Gross margin IS available: net_sales - sold_cost. Margin rate is that over "
    "net_sales.",
    "Nothing here records a reason. There is no waste, markdown, spoilage or "
    "count column.",
)

UNIT_OF_MEASURE_WARNING = (
    "SUMMING UNITS MIXES POUNDS AND EACHES. 70 of 200 items are sold by LB and "
    "130 by EA, and shrink_units adds them together. That is Meridian's own "
    "definition of shrink, so the number is not wrong -- but it is not a count "
    "of things. If a question turns on how much product moved, split or filter "
    "by unit_of_measure and say which you used."
)

# Dimensions small enough to list exhaustively in the prompt. The rest get a
# count and a sample, because 200 item names is prompt weight for little gain.
DIMENSIONS_LISTED_IN_FULL = (
    "dept",
    "region",
    "banner",
    "unit_of_measure",
    "category",
)
SAMPLE_VALUES_SHOWN = 6

# Width of the name column in the prompt's aligned listings.
_NAME_COLUMN_WIDTH = 16


def describe_data_model(semantic_view) -> str:
    """
    The table, described for whoever has to write SQL against it.

    Column meanings are written above; the values are read from the data at
    startup, so they cannot drift from what is actually there. A model told the
    three regions that exist cannot invent a fourth.
    """
    return "\n\n".join([
        _describe_columns(semantic_view.columns()),
        _describe_grain(),
        UNIT_OF_MEASURE_WARNING,
        _describe_dimension_values(semantic_view),
    ])


def _describe_columns(column_names) -> str:
    """One aligned line per column, in the order the view presents them."""
    return "\n".join(
        f"  {name:<{_NAME_COLUMN_WIDTH}} {COLUMN_MEANINGS.get(name, 'no description')}"
        for name in column_names
    )


def _describe_grain() -> str:
    """The facts that are about the table rather than any one column."""
    return "ABOUT THE GRAIN\n" + "\n".join(f"- {note}" for note in GRAIN_NOTES)


def _describe_dimension_values(semantic_view) -> str:
    """
    Every value each dimension actually takes, read from the data.

    This is the half that stops the model inventing a "Southeast" region. Small
    dimensions are listed in full; large ones get a count and a sample, because
    naming all 200 items costs prompt weight without buying accuracy.
    """
    present_columns = set(semantic_view.columns())
    lines = []
    for dimension in DIMENSIONS:
        if dimension not in present_columns:
            continue
        values = semantic_view.values_of(dimension)
        lines.append(
            f"  {dimension:<{_NAME_COLUMN_WIDTH}} {_summarize_values(dimension, values)}"
        )

    return (
        "EVERY VALUE THESE COLUMNS TAKE. Anything not listed does not exist in "
        "this extract, so a filter naming it returns nothing rather than being "
        "wrong quietly.\n" + "\n".join(lines)
    )


def _summarize_values(dimension: str, values: list) -> str:
    """How one dimension's values are shown: a range, the full list, or a sample."""
    if dimension == "store_id":
        return f"{min(values)}-{max(values)}"
    if dimension in DIMENSIONS_LISTED_IN_FULL:
        return ", ".join(str(value) for value in values)

    sample = ", ".join(str(value) for value in values[:SAMPLE_VALUES_SHOWN])
    return f"{len(values)} values, e.g. {sample} -- use ILIKE if unsure"
