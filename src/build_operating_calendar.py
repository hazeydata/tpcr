#!/usr/bin/env python3
"""
Operating Calendar Builder (Incremental)

================================================================================
PURPOSE
================================================================================
Combines permanent closures (extinct_on from dimentity) and temporary closures
(from raw_closures/*.csv) into a single operating calendar. Used by training,
forecasting, and WTI to exclude closed attractions.

Input: dimentity.csv, raw_closures/*.csv
Output: operating_calendar/operating_calendar.parquet

================================================================================
INCREMENTAL LOGIC
================================================================================
On first run (no existing calendar): builds full history from earliest observation.
On subsequent runs: only rebuilds a recent window (today - 7d to today + FORECAST_DAYS)
and merges with the existing calendar. Historical dates are stable and untouched.

Use --full to force a complete rebuild from scratch.

================================================================================
USAGE
================================================================================
  python src/build_operating_calendar.py
  python src/build_operating_calendar.py --full
  python src/build_operating_calendar.py --output-base /path --start-date 2025-01-01 --end-date 2026-12-31

"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from utils import get_output_base
from utils.forecast_horizon import get_forecast_end_date

# Sentinel dates per spec
SENTINEL_UNKNOWN_START = "1900-01-01"
SENTINEL_OPEN_END = "9999-12-31"

# Incremental refresh window
LOOKBACK_DAYS = 7  # Refresh this many days back (catches late closure updates)


# =============================================================================
# LOGGING
# =============================================================================
def setup_logging(log_dir: Path) -> logging.Logger:
    from datetime import datetime
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"build_operating_calendar_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger = logging.getLogger(__name__)
    logger.info("Log file: %s", log_file)
    return logger


def _get_earliest_observation(base: Path, logger: logging.Logger) -> str | None:
    """Auto-detect the earliest park_date in fact table parquets."""
    parquet_dir = base / "fact_tables" / "parquet"
    if not parquet_dir.exists() or not list(parquet_dir.glob("*.parquet")):
        return None
    pq_str = str(parquet_dir).replace("\\", "/")
    con = duckdb.connect()
    earliest = con.execute(
        f"SELECT MIN(park_date) FROM read_parquet('{pq_str}/*.parquet')"
    ).fetchone()[0]
    con.close()
    if earliest:
        result = str(earliest)[:10]
        logger.info("Auto-detected earliest observation: %s", result)
        return result
    return None


def _build_calendar_for_range(
    con: duckdb.DuckDBPyConnection,
    start_date: str,
    end_date: str,
    logger: logging.Logger,
) -> pd.DataFrame:
    """Build operating calendar for a date range using registered entities_df and closures_df."""
    calendar = con.execute(f"""
        WITH date_range AS (
            SELECT unnest(generate_series(DATE '{start_date}'::DATE, DATE '{end_date}'::DATE, INTERVAL '1 day'))::DATE as park_date
        ),
        entity_dates AS (
            SELECT e.entity_code, d.park_date
            FROM entities_df e
            CROSS JOIN date_range d
        ),
        permanent AS (
            SELECT ed.entity_code, ed.park_date,
                   (ed.park_date < CAST(e.extinct_on AS DATE)) as is_perm
            FROM entity_dates ed
            JOIN entities_df e ON ed.entity_code = e.entity_code
        ),
        temp_closed AS (
            SELECT ed.entity_code, ed.park_date,
                   BOOL_OR(ed.park_date >= CAST(c.closure_start AS DATE)
                          AND ed.park_date <= CAST(c.closure_end AS DATE)) as is_closed
            FROM entity_dates ed
            LEFT JOIN closures_df c ON ed.entity_code = c.entity_code
            GROUP BY ed.entity_code, ed.park_date
        ),
        combined AS (
            SELECT p.entity_code, p.park_date,
                   p.is_perm AND COALESCE(NOT t.is_closed, TRUE) as is_operating
            FROM permanent p
            LEFT JOIN temp_closed t ON p.entity_code = t.entity_code AND p.park_date = t.park_date
        )
        SELECT entity_code, park_date, is_operating FROM combined ORDER BY entity_code, park_date
    """).fetchdf()
    return calendar


# =============================================================================
# MAIN
# =============================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="Build operating calendar from dimentity + closures")
    ap.add_argument("--output-base", type=Path, default=get_output_base())
    ap.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Start date YYYY-MM-DD (overrides auto-detection)",
    )
    ap.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="End date YYYY-MM-DD (default: today + FORECAST_DAYS from forecast_horizon.py)",
    )
    ap.add_argument("--full", action="store_true", help="Force full rebuild (ignore existing calendar)")
    ap.add_argument("--force", action="store_true", help="Alias for --full (backward compat)")
    args = ap.parse_args()

    full_rebuild = args.full or args.force

    base = args.output_base.resolve()
    log_dir = base / "logs"
    logger = setup_logging(log_dir)

    today = date.today()
    # Use shared forecast horizon — same 730-day window as config.py and dim tables
    end_date = args.end_date or get_forecast_end_date().isoformat()

    dim_path = base / "dimension_tables" / "dimentity.csv"
    raw_closures_dir = base / "raw_closures"
    out_dir = base / "operating_calendar"
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = out_dir / "operating_calendar.parquet"
    csv_path = out_dir / "operating_calendar.csv"

    if not dim_path.exists():
        logger.error("dimentity.csv not found at %s", dim_path)
        sys.exit(1)

    # Decide: incremental or full rebuild
    existing_calendar = None
    if not full_rebuild and parquet_path.exists() and not args.start_date:
        # Incremental mode: refresh recent window only, merge with existing
        incremental = True
        refresh_start = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
        start_date = refresh_start
        logger.info("Incremental mode: refreshing %s to %s (existing calendar preserved for earlier dates)", start_date, end_date)
    else:
        # Full rebuild
        incremental = False
        if args.start_date:
            start_date = args.start_date
        else:
            earliest = _get_earliest_observation(base, logger)
            start_date = earliest or (today - timedelta(days=30)).isoformat()

    logger.info("=" * 60)
    logger.info("Operating calendar build (%s)", "incremental" if incremental else "full")
    logger.info("=" * 60)
    logger.info("Output base: %s", base)
    logger.info("Date range: %s to %s", start_date, end_date)

    con = duckdb.connect()
    dim_str = str(dim_path).replace("\\", "/")

    # Resolve entity code column
    dim_sample = con.execute(f"SELECT * FROM read_csv('{dim_str}', AUTO_DETECT=TRUE) LIMIT 1").fetchdf()
    code_col = "code" if "code" in dim_sample.columns else "entity_code" if "entity_code" in dim_sample.columns else None
    extinct_col = "extinct_on" if "extinct_on" in dim_sample.columns else None

    if not code_col:
        logger.error("dimentity has no 'code' or 'entity_code' column")
        sys.exit(1)

    # Step 1: Load entities with extinct_on
    extinct_expr = f"COALESCE({extinct_col}, '{SENTINEL_OPEN_END}')" if extinct_col else f"'{SENTINEL_OPEN_END}'"
    entities_sql = f"""
        SELECT {code_col} as entity_code, {extinct_expr} as extinct_on
        FROM read_csv('{dim_str}', AUTO_DETECT=TRUE)
    """
    entities = con.execute(entities_sql).fetchdf()
    logger.info("Loaded %d entities from dimentity", len(entities))

    # Step 2: Load temporary closures (if any)
    closure_files = list(raw_closures_dir.glob("*.csv")) if raw_closures_dir.exists() else []
    if closure_files:
        paths = [str(p).replace("\\", "/") for p in closure_files]
        paths_str = ", ".join(f"'{p}'" for p in paths)
        try:
            sample = con.execute(f"SELECT * FROM read_csv([{paths_str}], AUTO_DETECT=TRUE) LIMIT 1").fetchdf()
            type_filter = "WHERE object_type = 'attraction'" if "object_type" in sample.columns else ""
        except Exception:
            type_filter = ""
        closures_sql = f"""
            SELECT object_code as entity_code,
                   COALESCE(start_date, '{SENTINEL_UNKNOWN_START}') as closure_start,
                   COALESCE(finish_date, '{SENTINEL_OPEN_END}') as closure_end
            FROM read_csv([{paths_str}], AUTO_DETECT=TRUE)
            {type_filter}
        """
        try:
            closures = con.execute(closures_sql).fetchdf()
            logger.info("Loaded %d temporary closure records", len(closures))
        except Exception as e:
            logger.warning("Could not load closures (using empty): %s", e)
            closures = pd.DataFrame(columns=["entity_code", "closure_start", "closure_end"])
    else:
        logger.info("No raw_closures/*.csv found; using permanent closures only")
        closures = pd.DataFrame(columns=["entity_code", "closure_start", "closure_end"])

    # Register as DuckDB tables
    con.register("entities_df", entities)
    con.register("closures_df", closures)

    # Step 3: Build calendar for the target date range
    new_calendar = _build_calendar_for_range(con, start_date, end_date, logger)
    logger.info("Built %d rows for %s to %s", len(new_calendar), start_date, end_date)

    # Step 4: Merge with existing calendar (incremental mode)
    if incremental and parquet_path.exists():
        existing = pd.read_parquet(parquet_path)
        # Ensure park_date types match for comparison
        existing["park_date"] = pd.to_datetime(existing["park_date"]).dt.date
        new_calendar["park_date"] = pd.to_datetime(new_calendar["park_date"]).dt.date
        refresh_start_date = date.fromisoformat(start_date)

        # Keep existing rows BEFORE the refresh window, replace everything from refresh_start onward
        historical = existing[existing["park_date"] < refresh_start_date]
        calendar = pd.concat([historical, new_calendar], ignore_index=True)
        calendar = calendar.sort_values(["entity_code", "park_date"]).reset_index(drop=True)

        logger.info("Merged: %d historical rows + %d refreshed rows = %d total",
                     len(historical), len(new_calendar), len(calendar))
    else:
        calendar = new_calendar

    logger.info("Calendar: %d rows", len(calendar))

    closed_pct = (1 - calendar["is_operating"].mean()) * 100
    if closed_pct > 20:
        logger.warning("> %.1f%% of entity-days are closed; verify data", closed_pct)

    calendar.to_parquet(parquet_path, index=False)
    logger.info("Wrote %s", parquet_path)

    # Only write CSV debug backup on full rebuilds (it's large)
    if not incremental:
        calendar.to_csv(csv_path, index=False)
        logger.info("Wrote %s (debug backup)", csv_path)

    logger.info("Done.")


if __name__ == "__main__":
    main()
