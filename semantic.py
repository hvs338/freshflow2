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
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb

import metrics as M

DATA_DIR = Path(__file__).parent / "data"

# The only table name the model may write. Rebound per query.
VIEW = "daily"

# The base view, holding every department. The model cannot name it.
BASE = "daily_facts"

# Anything that writes, reads a file, or reaches the network. The connection
# also has external access switched off, so this is the second lock.
FORBIDDEN = (
    "attach", "detach", "copy", "install", "load", "pragma", "export", "import",
    "insert", "update", "delete", "drop", "create", "alter", "replace",
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_text", "read_blob", "glob", "system",
)


class QueryRejected(Exception):
    """A query the gate would not run. The message is written for the model."""


class Semantic:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.con = duckdb.connect(":memory:")
        self.con.execute("SET enable_external_access=false")
        self._load(data_dir)
        self._build_base()
        self.bind_scope(M.DEFAULT_SCOPE)

    # -- setup -------------------------------------------------------------

    def _load(self, data_dir: Path) -> None:
        """
        Read the CSVs once, into DuckDB.

        External access is disabled for the model's queries, so the files are
        read through pandas here rather than with DuckDB's read_csv.
        """
        import pandas as pd

        for name in ("items", "shipments", "sales_daily"):
            df = pd.read_csv(data_dir / f"{name}.csv")
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
            self.con.register(f"_{name}", df)

    def _build_base(self) -> None:
        d = M.DERIVED
        self.con.execute(f"""
            CREATE VIEW {BASE} AS
            WITH ship AS (
                SELECT s.date, s.store_id, s.item_id, s.banner, s.region,
                       {d['shipped_units']} AS shipped_units,
                       {d['shipped_cost']}  AS shipped_cost
                FROM _shipments s JOIN _items i USING (item_id)
            ),
            sale AS (
                SELECT s.date, s.store_id, s.item_id, s.banner, s.region,
                       {d['sold_units']} AS sold_units,
                       {d['sold_cost']}  AS sold_cost,
                       s.net_sales
                FROM _sales_daily s JOIN _items i USING (item_id)
            )
            -- FULL OUTER JOIN, and never ON a shipment date. This builds a
            -- zero-filled store-item-day spine so that SUM over any period or
            -- grouping is correct, and so the rows that shipped and never sold
            -- -- the highest-shrink rows in the extract -- are not dropped.
            SELECT
                COALESCE(sh.date, sa.date)          AS date,
                COALESCE(sh.store_id, sa.store_id)  AS store_id,
                COALESCE(sh.item_id, sa.item_id)    AS item_id,
                COALESCE(sh.banner, sa.banner)      AS banner,
                COALESCE(sh.region, sa.region)      AS region,
                i.dept, i.category, i.description, i.unit_cost,
                COALESCE(sh.shipped_units, 0)       AS shipped_units,
                COALESCE(sh.shipped_cost, 0.0)      AS shipped_cost,
                COALESCE(sa.sold_units, 0)          AS sold_units,
                COALESCE(sa.sold_cost, 0.0)         AS sold_cost,
                COALESCE(sa.net_sales, 0.0)         AS net_sales
            FROM ship sh
            FULL OUTER JOIN sale sa
              ON sh.date = sa.date AND sh.store_id = sa.store_id
             AND sh.item_id = sa.item_id
            JOIN _items i ON i.item_id = COALESCE(sh.item_id, sa.item_id)
        """)

    # -- the scope binding -------------------------------------------------

    def bind_scope(self, scope: str) -> None:
        """Point `daily` at the departments this scope includes."""
        depts = ", ".join(f"'{d}'" for d in M.SCOPES[scope]["depts"])
        self.con.execute(
            f"CREATE OR REPLACE TEMP VIEW {VIEW} AS "
            f"SELECT * FROM {BASE} WHERE dept IN ({depts})"
        )

    # -- the gate ----------------------------------------------------------

    def run(self, sql: str, scope: str, limit: int = 50) -> dict:
        """Validate, bind the scope, execute, cap. Raises QueryRejected."""
        clean = validate(sql)
        self.bind_scope(scope)
        try:
            cur = self.con.execute(f"SELECT * FROM ({clean}) AS q LIMIT {int(limit)}")
        except duckdb.Error as e:
            raise QueryRejected(f"Query failed: {e}") from e

        columns = [c[0] for c in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        return {"columns": columns, "rows": rows}

    def columns(self) -> list[str]:
        return [r[0] for r in self.con.execute(f"DESCRIBE {VIEW}").fetchall()]

    def date_range(self) -> tuple[str, str]:
        lo, hi = self.con.execute(f"SELECT MIN(date), MAX(date) FROM {BASE}").fetchone()
        return str(lo)[:10], str(hi)[:10]

    def values_of(self, column: str) -> list:
        return [r[0] for r in self.con.execute(
            f"SELECT DISTINCT {column} FROM {BASE} ORDER BY 1"
        ).fetchall()]


def validate(sql: str) -> str:
    """
    The whole gate, in one function you can read aloud.

    Errors are phrased for the model, because it is the one that has to fix
    them and try again.
    """
    raw = (sql or "").strip().rstrip(";").strip()
    if not raw:
        raise QueryRejected("No SQL was provided.")
    if ";" in raw:
        raise QueryRejected("Send one statement only; remove the ';'.")
    if not re.match(r"^(select|with)\b", raw.lower()):
        raise QueryRejected("Only SELECT (or WITH ... SELECT) is allowed.")

    words = set(re.findall(r"[a-z_]+", raw.lower()))
    banned = sorted(words & set(FORBIDDEN))
    if banned:
        raise QueryRejected(
            f"Not allowed here: {', '.join(banned)}. This reads; it cannot "
            "change anything or touch files."
        )

    # Every table reference must be the bound view. CTE names are fine, since
    # they resolve inside the statement.
    ctes = set(re.findall(r"(?:with|,)\s+([a-z_]\w*)\s+as\s*\(", raw.lower()))
    tables = set(re.findall(r"\b(?:from|join)\s+([a-z_]\w*)", raw.lower()))
    unknown = tables - ctes - {VIEW}
    if unknown:
        raise QueryRejected(
            f"Unknown table(s): {', '.join(sorted(unknown))}. "
            f"The only table you can read is `{VIEW}`."
        )
    return raw
