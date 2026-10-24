#!/usr/bin/env python3
"""
Build Model Aggregates - For prediction model without posted times

Groups by: entity_code, date_group_id, time_slot (15-min intervals)

Features aligned with model:
- time_slot: 15-min intervals (0-95)
- hour_of_day: 0-23

Join with dimension tables separately for:
- season, season_year (via date_group_id → date → dimseason)
- mins_since_open (via park hours)

Output: /home/wilma/hazeydata/pipeline/aggregates/model_aggregates.parquet

Usage:
    python scripts/build_model_aggregates.py
    python scripts/build_model_aggregates.py --output-base /path/to/pipeline
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Add src to path
for _p in (Path(__file__).parent.parent / "src", Path(__file__).parent.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from utils.paths import get_output_base


def setup_logging(log_dir: Path) -> logging.Logger:
    """Set up file and console logging."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"build_model_aggregates_{datetime.now(ZoneInfo('UTC')).strftime('%Y%m%d_%H%M%S')}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized. Log file: {log_file}")
    return logger


def build_model_aggregates(output_base: Path, logger: logging.Logger) -> int:
    """Build model aggregates with 15-minute time slots.

    Thin wrapper over pipeline.core.model_aggregates, the same code the daily
    S4 step runs (dedupe on the natural key, min 5 observations per cell,
    atomic write, schema validation). Returns the number of rows written.
    """
    from pipeline.core.model_aggregates import AggregateBuildError, build_model_aggregates as _build

    try:
        res = _build(output_base, log=logger)
    except AggregateBuildError as e:
        logger.error(f"Build failed, previous file kept: {e}")
        return 0
    logger.info(f"Aggregate rows: {res.rows:,}; entities: {res.entities}; "
                f"duplicates dropped: {res.duplicate_rows_dropped:,}; {res.duration_sec}s")
    return res.rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Build model aggregates (15-min time slots)")
    ap.add_argument(
        "--output-base",
        type=Path,
        default=get_output_base(),
        help="Output base directory",
    )
    args = ap.parse_args()

    base = args.output_base.resolve()
    log_dir = base / "logs"
    logger = setup_logging(log_dir)

    try:
        row_count = build_model_aggregates(base, logger)
        if row_count > 0:
            logger.info("Success!")
            sys.exit(0)
        else:
            logger.error("No aggregates created")
            sys.exit(1)
    except Exception as e:
        logger.exception(f"Failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
