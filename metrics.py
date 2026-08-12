"""
What the numbers mean. Nothing here executes; it is all definitions.

This file is the single source of truth for Meridian's business logic, written
as SQL fragments so that exactly one statement of "what shrink is" exists. It
gets used three ways:

  * semantic.py builds the view from DERIVED
  * router.py puts METRICS in the prompt, so the model does not invent five
    spellings of "shrink rate"
  * agent.py reads SCOPES and MEASURES to tell the user which definition an
    answer used

Change a definition here and it changes everywhere, including what the user is
told. That is the point of keeping it in one place.
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
