"""Shared constraint-free writes for the live DuckDB (live_waits, data_freshness).

Tables created with CREATE TABLE IF NOT EXISTS may or may not carry a PRIMARY KEY,
and DuckDB rejects INSERT OR IGNORE / INSERT OR REPLACE (Binder Error) when there
is no PK to conflict on. Every live writer goes through these helpers instead:

- insert_new_live_waits: insert-if-absent keyed by (entity_code, observed_at,
  wait_time_type); an existing row always keeps its first value.
- replace_data_freshness: delete-then-insert keyed by source.

Only the connection's execute() is used, so any DuckDB connection works. This
module imports nothing from the project so it can be loaded as either
``live_duckdb_writes`` (src on sys.path) or ``src.live_duckdb_writes``.
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Sequence

_LOG = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Source relation for a CSV written by the queue-times scraper.
CSV_WAITS_SOURCE_SQL = (
    "SELECT entity_code, observed_at, wait_time_type, wait_time_minutes, park_date "
    "FROM read_csv_auto(?)"
)

_STAGE_TABLE = "_incoming_live_waits"


def _ident(name: str) -> str:
    if not _IDENT_RE.match(name or ""):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return name


def _rollback_quietly(con) -> None:
    try:
        con.execute("ROLLBACK")
    except Exception:
        pass


def _insert_new_live_waits_stmts(con, source_sql: str, params: Sequence) -> int:
    # Stage once so source_sql (and its parameters) is evaluated a single time.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE {_STAGE_TABLE} AS
        SELECT CAST(entity_code AS VARCHAR) AS entity_code,
               TRY_CAST(observed_at AS TIMESTAMPTZ) AS observed_at,
               CAST(wait_time_type AS VARCHAR) AS wait_time_type,
               CAST(wait_time_minutes AS INTEGER) AS wait_time_minutes,
               CAST(park_date AS DATE) AS park_date
        FROM ({source_sql}) AS src
        """,
        list(params),
    )
    bad_key = (
        "entity_code IS NULL OR entity_code = '' "
        "OR observed_at IS NULL "
        "OR wait_time_type IS NULL OR wait_time_type = ''"
    )
    dropped = con.execute(
        f"SELECT COUNT(*) FROM {_STAGE_TABLE} WHERE {bad_key}"
    ).fetchone()[0]
    con.execute(
        f"""
        INSERT INTO live_waits (entity_code, observed_at, wait_time_type, wait_time_minutes, park_date)
        SELECT s.entity_code, s.observed_at, s.wait_time_type, s.wait_time_minutes, s.park_date
        FROM (
            SELECT * FROM {_STAGE_TABLE}
            WHERE NOT ({bad_key})
            QUALIFY row_number() OVER (
                PARTITION BY entity_code, observed_at, wait_time_type
                ORDER BY wait_time_minutes DESC
            ) = 1
        ) s
        WHERE NOT EXISTS (
            SELECT 1 FROM live_waits lw
            WHERE lw.entity_code = s.entity_code
              AND lw.observed_at = s.observed_at
              AND lw.wait_time_type = s.wait_time_type
        )
        """
    )
    con.execute(f"DROP TABLE IF EXISTS {_STAGE_TABLE}")
    return int(dropped)


def insert_new_live_waits(
    con,
    source_sql: str,
    params: Optional[Sequence] = None,
    *,
    manage_transaction: bool = True,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Insert rows from ``source_sql`` that are not already in live_waits.

    ``source_sql`` is a SELECT yielding entity_code, observed_at, wait_time_type,
    wait_time_minutes, park_date (``params`` bind its ``?`` placeholders).
    Existing keys (entity_code, observed_at, wait_time_type) are left untouched,
    so the first written value wins. Duplicate keys inside the batch collapse to
    the row with the largest wait. Rows whose key fields are NULL, empty, or an
    unparseable observed_at are dropped instead of failing the batch.

    Returns the number of dropped rows (warning-logged when > 0). Set
    ``manage_transaction=False`` when the caller already holds a transaction.
    """
    log = logger or _LOG
    params = params or []
    if manage_transaction:
        con.execute("BEGIN")
        try:
            dropped = _insert_new_live_waits_stmts(con, source_sql, params)
            con.execute("COMMIT")
        except Exception:
            _rollback_quietly(con)
            raise
    else:
        dropped = _insert_new_live_waits_stmts(con, source_sql, params)
    if dropped:
        log.warning(
            "live_waits: dropped %d row(s) with NULL/empty entity_code, "
            "observed_at or wait_time_type",
            dropped,
        )
    return dropped


def _replace_data_freshness_stmts(
    con,
    source: str,
    notes: str,
    row_count: Optional[int],
    count_table: Optional[str],
    last_updated_column: Optional[str],
) -> None:
    con.execute("DELETE FROM data_freshness WHERE source = ?", [source])
    cols = "(source, last_updated, row_count, notes)"
    if last_updated_column:
        if not count_table:
            raise ValueError("last_updated_column requires count_table")
        con.execute(
            f"INSERT INTO data_freshness {cols} "
            f"SELECT ?, COALESCE(MAX({_ident(last_updated_column)}), CURRENT_TIMESTAMP), "
            f"COUNT(*), ? FROM {_ident(count_table)}",
            [source, notes],
        )
    elif count_table:
        con.execute(
            f"INSERT INTO data_freshness {cols} "
            f"SELECT ?, CURRENT_TIMESTAMP, (SELECT COUNT(*) FROM {_ident(count_table)}), ?",
            [source, notes],
        )
    else:
        con.execute(
            f"INSERT INTO data_freshness {cols} "
            "VALUES (?, CURRENT_TIMESTAMP, ?, ?)",
            [source, row_count, notes],
        )


def replace_data_freshness(
    con,
    source: str,
    notes: str,
    *,
    row_count: Optional[int] = None,
    count_table: Optional[str] = None,
    last_updated_column: Optional[str] = None,
    manage_transaction: bool = True,
) -> None:
    """Replace the data_freshness row for ``source`` (delete, then insert).

    row_count is ``row_count`` if given, else COUNT(*) of ``count_table``.
    With ``last_updated_column`` the timestamp is
    COALESCE(MAX(column), CURRENT_TIMESTAMP) over ``count_table``; otherwise
    CURRENT_TIMESTAMP. The delete+insert pair is atomic unless
    ``manage_transaction=False`` (caller already holds a transaction).
    """
    args = (con, source, notes, row_count, count_table, last_updated_column)
    if not manage_transaction:
        _replace_data_freshness_stmts(*args)
        return
    con.execute("BEGIN")
    try:
        _replace_data_freshness_stmts(*args)
        con.execute("COMMIT")
    except Exception:
        _rollback_quietly(con)
        raise


def write_scraper_batch(
    con,
    csv_path: str,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Insert a scraper CSV into live_waits and refresh the 'scraper' freshness row.

    One transaction: any failure rolls back both tables. Returns the dropped count.
    """
    con.execute("BEGIN")
    try:
        dropped = insert_new_live_waits(
            con, CSV_WAITS_SOURCE_SQL, [csv_path],
            manage_transaction=False, logger=logger,
        )
        replace_data_freshness(
            con, "scraper", "queue-times",
            count_table="live_waits", manage_transaction=False,
        )
        con.execute("COMMIT")
    except Exception:
        _rollback_quietly(con)
        raise
    return dropped
