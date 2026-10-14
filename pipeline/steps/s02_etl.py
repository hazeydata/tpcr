"""Step 2: ETL + CSV→Parquet conversion.

During shadow phase: reads from production parquet files (no separate ETL).
Post-swap: runs ETL and converts CSVs to Parquet.

Returns observation counts for the Pipeline Run Report:
  - new_observations: rows added for yesterday's date
  - total_parquet_files: number of parquet files after ETL
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from pipeline.config import PipelineConfig
from pipeline.core.logging import PipelineLogger
from pipeline.core.validation import require_file


def _count_yesterday_observations(cfg: PipelineConfig, log: PipelineLogger) -> int:
    """Count new observations from yesterday in the fact tables.

    This is the DATA HEALTH signal for the Pipeline Run Report.
    Zero observations = data feed is broken.
    """
    try:
        from pipeline.core.db import read_connection

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        parquet_dir = cfg.parquet_dir
        recent_parquets = sorted(parquet_dir.glob("*.parquet"))[-3:]

        if not recent_parquets:
            log.warning("No parquet files found for observation count")
            return 0

        parquet_glob = "', '".join(str(f) for f in recent_parquets)

        with read_connection() as con:
            result = con.execute(f"""
                SELECT COUNT(*) as n
                FROM read_parquet(['{parquet_glob}'])
                WHERE park_date::VARCHAR = '{yesterday}'
            """).fetchone()
            count = result[0] if result else 0

        log.info(f"Yesterday's observations ({yesterday}): {count:,}")
        return count

    except Exception as e:
        log.warning(f"Could not count yesterday's observations: {e}")
        return -1  # Signal that count failed, distinct from 0


def run(cfg: PipelineConfig, log: PipelineLogger) -> dict:
    """Run ETL and convert to parquet."""

    log.info("=" * 60)
    log.info("STEP 2: ETL + CSV→PARQUET")
    log.info("=" * 60)

    if cfg.shadow:
        # Verify production parquet exists
        parquet_files = list(cfg.parquet_dir.glob("*.parquet"))
        if not parquet_files:
            log.error(f"No parquet files found at {cfg.parquet_dir}")
            raise FileNotFoundError(f"Shadow mode requires production parquet at {cfg.parquet_dir}")
        log.info(f"Shadow mode: using {len(parquet_files)} production parquet files")
        return {"rows": 0, "action": "skipped_shadow", "parquet_files": len(parquet_files)}

    # Production mode: run ETL scripts
    project_root = Path(cfg.output_base).parent.parent / "theme-park-crowd-report"
    etl_script = project_root / "scripts" / "run_etl.sh"
    convert_script = project_root / "scripts" / "convert_to_parquet.py"

    if etl_script.exists():
        with log.timed("ETL"):
            result = subprocess.run(
                ["bash", str(etl_script), "--output-base", str(cfg.output_base),
                 "--local-source", str(cfg.raw_data_dir)],
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode != 0:
                raise RuntimeError(f"ETL failed: {result.stderr[:500]}")

    if convert_script.exists():
        with log.timed("CSV→Parquet"):
            result = subprocess.run(
                [sys.executable, str(convert_script),
                 "--input", str(cfg.fact_tables_dir / "clean"),
                 "--output", str(cfg.parquet_dir),
                 "--recent-months", "2", "--workers", "2"],
                capture_output=True, text=True, timeout=1800,
            )
            if result.returncode != 0:
                raise RuntimeError(f"CSV→Parquet failed: {result.stderr[:500]}")

    parquet_files = list(cfg.parquet_dir.glob("*.parquet"))
    log.info(f"ETL complete: {len(parquet_files)} parquet files")

    # Count yesterday's observations for the Pipeline Run Report
    new_obs = _count_yesterday_observations(cfg, log)

    return {
        "rows": new_obs if new_obs > 0 else 0,
        "action": "completed",
        "parquet_files": len(parquet_files),
        "new_observations": new_obs,
    }
