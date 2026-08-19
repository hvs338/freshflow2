"""
One table for the model to query, and the gate in front of it.

The model writes SQL. That is only safe because of two things this file does:

  1. It states the join once. Shipments are deliveries -- episodic. Sales are
     daily. 77% of sales rows have no same-day shipment, so joining the two
     facts ON the date throws most sales away and overstates shrink by ~860%.
     Getting this wrong is easy and the error looks plausible, so the model is
     never given the raw tables to join for itself.

  2. It refuses anything that is not one read-only SELECT over one view.

Scope -- one of Meridian's two contested definitions -- is bound here, not in
the model's WHERE clause. `daily` is rebuilt before every query to hold only
the departments the resolved scope includes. Identical SQL therefore returns
different numbers under different scopes, and the harness knows which it used.

This file knows nothing about what shrink means. It reads metrics.py for the
derived-column expressions and the department lists, and that is the whole of
its business knowledge.
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb

import metrics

DATA_DIR = Path(__file__).parent / "data"

# The only table name the model may write. Rebound per query.
SCOPED_VIEW = "daily"

# The base view, holding every department. The model cannot name it.
BASE_VIEW = "daily_facts"

# The CSVs, registered under these names. Prefixed with an underscore so the
# gate's "only `daily` is readable" rule reads naturally in its error message.
SOURCE_TABLES = ("items", "shipments", "sales_daily")

# Anything that writes, reads a file, or reaches the network. The connection
# also has external access switched off, so this is the second lock.
FORBIDDEN_TOKENS = (
    "attach", "detach", "copy", "install", "load", "pragma", "export", "import",
    "insert", "update", "delete", "drop", "create", "alter", "replace",
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_text", "read_blob", "glob", "system",
)

DEFAULT_ROW_LIMIT = 50


class QueryRejected(Exception):
    """A query the gate would not run. The message is written for the model."""


class Semantic:
    """
    The one place a query executes.

    Everything -- the four typed questions in queries.py and the model's own
    SQL -- arrives at `run`. That is deliberate: a single door means a typed
    tool and a hand-written query cannot disagree about which departments count.
    """

    def __init__(self, data_dir: Path = DATA_DIR):
        self.connection = duckdb.connect(":memory:")
        self.connection.execute("SET enable_external_access=false")
        self._load_source_tables(data_dir)
        self._build_base_view()
        self.bind_scope(metrics.DEFAULT_SCOPE)

    # -- setup -------------------------------------------------------------

    def _load_source_tables(self, data_dir: Path) -> None:
        """
        Read the CSVs once, into DuckDB.

        External access is disabled for the model's queries, so the files are
        read through pandas here rather than with DuckDB's read_csv.
        """
        import pandas as pd

        for table_name in SOURCE_TABLES:
            frame = pd.read_csv(data_dir / f"{table_name}.csv")
            if "date" in frame.columns:
                frame["date"] = pd.to_datetime(frame["date"])
            self.connection.register(f"_{table_name}", frame)

    def _build_base_view(self) -> None:
        """
        The join, stated once.

        Shipments and sales are aggregated from their own tables and then joined
        on the full store-item-day key, never on a shipment date alone. See the
        comment inside the SQL: this is the single most load-bearing decision in
        the codebase.
        """
        derived = metrics.DERIVED_COLUMNS
        self.connection.execute(f"""
            CREATE VIEW {BASE_VIEW} AS
            WITH shipment_rows AS (
                SELECT s.date, s.store_id, s.item_id, s.banner, s.region,
                       {derived['shipped_units']} AS shipped_units,
                       {derived['shipped_cost']}  AS shipped_cost
                FROM _shipments s JOIN _items i USING (item_id)
            ),
            sales_rows AS (
                SELECT s.date, s.store_id, s.item_id, s.banner, s.region,
                       {derived['sold_units']} AS sold_units,
                       {derived['sold_cost']}  AS sold_cost,
                       s.net_sales
                FROM _sales_daily s JOIN _items i USING (item_id)
            )
            -- FULL OUTER JOIN, and never ON a shipment date. This builds a
            -- zero-filled store-item-day spine so that SUM over any period or
            -- grouping is correct, and so the rows that shipped and never sold
            -- -- the highest-shrink rows in the extract -- are not dropped.
            SELECT
                COALESCE(shipped.date, sold.date)          AS date,
                COALESCE(shipped.store_id, sold.store_id)  AS store_id,
                COALESCE(shipped.item_id, sold.item_id)    AS item_id,
                COALESCE(shipped.banner, sold.banner)      AS banner,
                COALESCE(shipped.region, sold.region)      AS region,
                item.dept, item.category, item.description,
                -- LB or EA. Carried because units are not comparable across it:
                -- summing shrink units adds pounds to eaches, which is Meridian's
                -- own definition but is not visible unless this column is here.
                item.unit_of_measure, item.unit_cost,
                COALESCE(shipped.shipped_units, 0)   AS shipped_units,
                COALESCE(shipped.shipped_cost, 0.0)  AS shipped_cost,
                COALESCE(sold.sold_units, 0)         AS sold_units,
                COALESCE(sold.sold_cost, 0.0)        AS sold_cost,
                COALESCE(sold.net_sales, 0.0)        AS net_sales
            FROM shipment_rows shipped
            FULL OUTER JOIN sales_rows sold
              ON shipped.date = sold.date
             AND shipped.store_id = sold.store_id
             AND shipped.item_id = sold.item_id
            JOIN _items item ON item.item_id = COALESCE(shipped.item_id, sold.item_id)
        """)

    # -- the scope binding -------------------------------------------------

    def bind_scope(self, scope: str) -> None:
        """
        Point `daily` at the departments this scope includes.

        Called before every query, so the contested scope definition is resolved
        by the harness rather than by whatever WHERE clause the model wrote.
        """
        departments = metrics.SCOPES[scope]["departments"]
        department_list = ", ".join(f"'{name}'" for name in departments)
        self.connection.execute(
            f"CREATE OR REPLACE TEMP VIEW {SCOPED_VIEW} AS "
            f"SELECT * FROM {BASE_VIEW} WHERE dept IN ({department_list})"
        )

    # -- the gate ----------------------------------------------------------

    def run(self, sql: str, scope: str, row_limit: int = DEFAULT_ROW_LIMIT) -> dict:
        """Validate, bind the scope, execute, cap. Raises QueryRejected."""
        validated_sql = validate(sql)
        self.bind_scope(scope)

        try:
            cursor = self.connection.execute(
                f"SELECT * FROM ({validated_sql}) AS wrapped LIMIT {int(row_limit)}"
            )
        except duckdb.Error as error:
            raise QueryRejected(f"Query failed: {error}") from error

        return self._read_cursor(cursor)

    @staticmethod
    def _read_cursor(cursor) -> dict:
        """DuckDB's tuples, turned into the {columns, rows} shape callers expect."""
        column_names = [description[0] for description in cursor.description]
        rows = [dict(zip(column_names, values)) for values in cursor.fetchall()]
        return {"columns": column_names, "rows": rows}

    # -- introspection, for the prompt and the tool vocabulary --------------

    def columns(self) -> list[str]:
        """The scoped view's column names, in order."""
        described = self.connection.execute(f"DESCRIBE {SCOPED_VIEW}").fetchall()
        return [row[0] for row in described]

    def date_range(self) -> tuple[str, str]:
        """First and last date in the extract, as YYYY-MM-DD."""
        earliest, latest = self.connection.execute(
            f"SELECT MIN(date), MAX(date) FROM {BASE_VIEW}"
        ).fetchone()
        return str(earliest)[:10], str(latest)[:10]

    def values_of(self, column: str) -> list:
        """
        Every distinct value in one column, sorted.

        Read from the unscoped base view on purpose: the tool vocabulary should
        recognise a Grocery category even when the current scope excludes it, so
        the user gets "that is out of scope" rather than "no such category".
        """
        rows = self.connection.execute(
            f"SELECT DISTINCT {column} FROM {BASE_VIEW} ORDER BY 1"
        ).fetchall()
        return [row[0] for row in rows]


# --- The gate --------------------------------------------------------------
#
# Split into one function per rule. Each says what it rejects and why, and the
# messages are written for the model, because it is the one that has to fix
# them and try again.


def validate(sql: str) -> str:
    """The whole gate, as four checks you can read aloud."""
    statement = (sql or "").strip().rstrip(";").strip()

    _reject_if_empty(statement)
    _reject_if_multiple_statements(statement)
    _reject_if_not_read_only(statement)
    _reject_forbidden_tokens(statement)
    _reject_unknown_tables(statement)

    return statement


def _reject_if_empty(statement: str) -> None:
    if not statement:
        raise QueryRejected("No SQL was provided.")


def _reject_if_multiple_statements(statement: str) -> None:
    if ";" in statement:
        raise QueryRejected("Send one statement only; remove the ';'.")


def _reject_if_not_read_only(statement: str) -> None:
    if not re.match(r"^(select|with)\b", statement.lower()):
        raise QueryRejected("Only SELECT (or WITH ... SELECT) is allowed.")


def _reject_forbidden_tokens(statement: str) -> None:
    """Anything that writes, reads a file, or reaches the network."""
    words = set(re.findall(r"[a-z_]+", statement.lower()))
    banned = sorted(words & set(FORBIDDEN_TOKENS))
    if banned:
        raise QueryRejected(
            f"Not allowed here: {', '.join(banned)}. This reads; it cannot "
            "change anything or touch files."
        )


def _reject_unknown_tables(statement: str) -> None:
    """
    Every table reference must be the bound view.

    CTE names are fine, since they resolve inside the statement. Blocking the
    unscoped base view is the important half: naming it would sidestep the
    scope binding entirely.
    """
    lowered = statement.lower()
    cte_names = set(re.findall(r"(?:with|,)\s+([a-z_]\w*)\s+as\s*\(", lowered))
    referenced = set(re.findall(r"\b(?:from|join)\s+([a-z_]\w*)", lowered))

    unknown = referenced - cte_names - {SCOPED_VIEW}
    if unknown:
        raise QueryRejected(
            f"Unknown table(s): {', '.join(sorted(unknown))}. "
            f"The only table you can read is `{SCOPED_VIEW}`."
        )
