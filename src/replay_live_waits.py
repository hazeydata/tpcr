#!/usr/bin/env python3
"""
Replay queue-times staging CSVs into tpcr_live.duckdb live_waits (TPCR-S11-LOCK-001).

The scraper's dual-write can lose batches when s11_deploy holds the DuckDB write lock.
Staging (staging/queue_times/YYYY-MM/{park}_{YYYY-MM-DD}.csv) always has every row, so this
inserts whatever is missing (anti-join on entity_code, observed_at, wait_time_type).
Idempotent: a second run inserts 0 rows.

Usage:
    python src/replay_live_waits.py [--output-base PATH] [--days N | --date YYYY-MM-DD ...] [--budget-s SECS]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List
from zoneinfo import ZoneInfo

import pandas as pd

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import duckdb

from live_waits_common import (
    insert_missing_live_waits,
    park_tz_for_park_code,
    prepare_live_waits_rows,
)
from utils import get_output_base

DEFAULT_BUDGET_S = 180
BACKOFF_START_S = 2
BACKOFF_MAX_S = 30
LOCK_KEYWORDS = ("lock", "busy", "io error", "could not set", "blocked")
STAGING_COLUMNS = ["entity_code", "observed_at", "wait_time_type", "wait_time_minutes"]


def _connect_with_retry(db_path: Path, budget_s: float, logger: logging.Logger):
    """duckdb.connect with time-budgeted backoff on lock errors."""
    start = time.monotonic()
    backoff = BACKOFF_START_S
    while True:
        try:
            return duckdb.connect(str(db_path))
        except Exception as e:
            remaining = budget_s - (time.monotonic() - start)
            if not any(kw in str(e).lower() for kw in LOCK_KEYWORDS) or remaining <= 0:
                raise
            wait_s = min(backoff, BACKOFF_MAX_S, remaining)
            logger.debug(f"live DB locked, retrying in {wait_s:.1f}s")
            time.sleep(wait_s)
            backoff = min(backoff * 2, BACKOFF_MAX_S)


def replay_staging_to_live(
    db_path: Path,
    staging_dir: Path,
    park_dates: List[str],
    logger: logging.Logger,
    budget_s: float = DEFAULT_BUDGET_S,
) -> Dict:
    """Insert staging rows missing from live_waits for the given park dates (YYYY-MM-DD)."""
    frames = []
    result: Dict = {"files": 0, "candidate_rows": 0, "inserted": 0, "per_date": {}}
    for park_date in park_dates:
        ym = park_date[:7]
        n_files = n_rows = 0
        for f in sorted((staging_dir / ym).glob(f"*_{park_date}.csv")):
            park = f.stem.rsplit("_", 1)[0]
            try:
                raw = pd.read_csv(f, usecols=STAGING_COLUMNS)
                prepared = prepare_live_waits_rows(raw, park_tz_for_park_code(park), logger)
            except Exception as e:
                logger.warning(f"Skipping {f.name}: {e}")
                continue
            n_files += 1
            n_rows += len(prepared)
            frames.append((park_date, prepared))
        result["files"] += n_files
        result["candidate_rows"] += n_rows
        result["per_date"][park_date] = {"files": n_files, "candidate_rows": n_rows, "inserted": 0}

    if not frames:
        return result

    con = _connect_with_retry(Path(db_path), budget_s, logger)
    try:
        for park_date, df in frames:
            n = insert_missing_live_waits(con, df)
            result["inserted"] += n
            result["per_date"][park_date]["inserted"] += n
        if result["inserted"]:
            con.execute("""
                INSERT OR REPLACE INTO data_freshness (source, last_updated, row_count, notes)
                VALUES ('scraper', CURRENT_TIMESTAMP, (SELECT COUNT(*) FROM live_waits), 'replay')
            """)
    finally:
        con.close()
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay staging queue-times CSVs into live_waits")
    ap.add_argument("--output-base", type=Path, default=get_output_base())
    ap.add_argument("--days", type=int, default=2, help="Yesterday..today in America/New_York (default 2)")
    ap.add_argument("--date", action="append", default=None, metavar="YYYY-MM-DD",
                    help="Park date to replay (repeatable; overrides --days)")
    ap.add_argument("--budget-s", type=float, default=DEFAULT_BUDGET_S, help="Lock retry budget (s)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("replay_live_waits")

    output_base = args.output_base.resolve()
    if args.date:
        park_dates = sorted(set(args.date))
    else:
        today = datetime.now(ZoneInfo("America/New_York")).date()
        park_dates = [(today - timedelta(days=i)).isoformat() for i in range(max(args.days, 1) - 1, -1, -1)]

    db_path = output_base / "tpcr_live.duckdb"
    if not db_path.exists():
        logger.error(f"{db_path} not found")
        sys.exit(1)
    try:
        r = replay_staging_to_live(db_path, output_base / "staging" / "queue_times",
                                   park_dates, logger, budget_s=args.budget_s)
    except Exception as e:
        logger.error(f"live_waits replay failed: {e}")
        sys.exit(1)
    logger.info(f"replay_live_waits: dates={park_dates} files={r['files']} "
                f"candidate_rows={r['candidate_rows']} inserted={r['inserted']}")
    sys.exit(0)


if __name__ == "__main__":
    main()
