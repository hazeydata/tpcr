"""Step 11: Deploy — Load results into DuckDB.

The ONLY step that writes to DuckDB. All previous steps work with parquet.

In shadow mode: skip deployment entirely (just compare outputs).
In production: load forecasts + WTI into tpcr_live.duckdb, then RO-assert
today's WTI row counts (outline D1 #6). Fail the step if assert fails.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from pipeline.config import PipelineConfig
from pipeline.core.db import write_connection
from pipeline.core.logging import PipelineLogger

# Parks expected in daily WTI (core 12). Soft floor — assert fails if far below.
_EXPECTED_PARKS_MIN = 8
_WTI_ROWS_FLOOR = 1000  # total table should stay large after reload


def _upsert_freshness(con: duckdb.DuckDBPyConnection, source: str, notes: str) -> None:
    """data_freshness may lack a PRIMARY KEY after CTAS rebuild — avoid OR REPLACE."""
    con.execute("DELETE FROM data_freshness WHERE source = ?", [source])
    con.execute(
        """
        INSERT INTO data_freshness (source, last_updated, row_count, notes)
        SELECT ?, CURRENT_TIMESTAMP,
               CASE
                 WHEN ? = 'wti' THEN (SELECT COUNT(*) FROM wti)
                 WHEN ? = 'forecasts' THEN (SELECT COUNT(*) FROM forecasts)
                 ELSE 0
               END,
               ?
        """,
        [source, source, source, notes],
    )


def assert_live_wti(db_path: Path, log: PipelineLogger | None = None) -> dict:
    """RO post-deploy assertion for live WTI (D1 #6).

    Returns a dict suitable for pipeline_metrics step.extra / top-level deploy_assert.
    Raises RuntimeError if hard gates fail.
    """
    today = date.today()
    info: dict = {
        "asserted_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "today": today.isoformat(),
        "ok": False,
    }
    if not db_path.exists():
        info["error"] = "duckdb_missing"
        raise RuntimeError(f"post-deploy assert: DuckDB missing at {db_path}")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        total = con.execute("SELECT COUNT(*) FROM wti").fetchone()[0]
        parks_today = con.execute(
            "SELECT COUNT(DISTINCT park_code) FROM wti WHERE park_date = ?",
            [today],
        ).fetchone()[0]
        rows_today = con.execute(
            "SELECT COUNT(*) FROM wti WHERE park_date = ?",
            [today],
        ).fetchone()[0]
        sample = con.execute(
            """
            SELECT park_code, wti FROM wti
            WHERE park_date = ?
            ORDER BY park_code
            LIMIT 20
            """,
            [today],
        ).fetchall()
        max_d, min_d = con.execute(
            "SELECT max(park_date), min(park_date) FROM wti"
        ).fetchone()
        fc_n = con.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
    finally:
        con.close()

    info.update(
        {
            "wti_total_rows": int(total),
            "wti_rows_today": int(rows_today),
            "wti_parks_today": int(parks_today),
            "wti_sample_today": [
                {"park_code": p, "wti": float(w) if w is not None else None}
                for p, w in sample
            ],
            "wti_date_min": str(min_d) if min_d else None,
            "wti_date_max": str(max_d) if max_d else None,
            "forecasts_total_rows": int(fc_n),
        }
    )

    errors = []
    if total < _WTI_ROWS_FLOOR:
        errors.append(f"wti_total_rows {total} < floor {_WTI_ROWS_FLOOR}")
    if parks_today < _EXPECTED_PARKS_MIN:
        errors.append(
            f"wti_parks_today {parks_today} < min {_EXPECTED_PARKS_MIN} for {today}"
        )
    if rows_today < _EXPECTED_PARKS_MIN:
        errors.append(f"wti_rows_today {rows_today} too low for {today}")
    if fc_n < 1:
        errors.append("forecasts table empty after deploy")

    if errors:
        info["ok"] = False
        info["errors"] = errors
        msg = "post-deploy WTI assert FAILED: " + "; ".join(errors)
        if log:
            log.error(msg)
        raise RuntimeError(msg)

    info["ok"] = True
    if log:
        log.info(
            f"post-deploy WTI assert OK: total={total:,} today_parks={parks_today} "
            f"today_rows={rows_today} forecasts={fc_n:,}"
        )
    return info


def run(cfg: PipelineConfig, log: PipelineLogger) -> dict:
    """Deploy pipeline outputs + RO assert."""

    log.info("=" * 60)
    log.info("STEP 11: DEPLOY")
    log.info("=" * 60)

    if cfg.shadow:
        log.info(
            "Shadow mode: skipping deployment. Run shadow/compare_wti.py to compare outputs."
        )
        return {"rows": 0, "action": "skipped_shadow"}

    wti_rows = 0
    fc_rows = 0

    # Load WTI into DuckDB — check both new and legacy filenames
    wti_path = cfg.wti_dir / "wti.parquet"
    if not wti_path.exists():
        wti_path = cfg.wti_dir / "wti_v3.parquet"  # legacy fallback
    if wti_path.exists():
        with log.timed("deploy WTI to DuckDB"):
            wti_df = pd.read_parquet(wti_path)
            with write_connection(cfg.duckdb_path) as con:
                min_d = wti_df["park_date"].min()
                max_d = wti_df["park_date"].max()
                con.execute(
                    "DELETE FROM wti WHERE park_date >= ? AND park_date <= ?",
                    [min_d, max_d],
                )
                con.register("_wti", wti_df)
                con.execute(
                    """
                    INSERT INTO wti (park_code, park_date, time_slot, wti, source, updated_at)
                    SELECT park_code, park_date::DATE, 'daily', wti,
                           COALESCE(source, 'forecast'), CURRENT_TIMESTAMP
                    FROM _wti
                    """
                )
                _upsert_freshness(con, "wti", "pipeline")
            wti_rows = len(wti_df)
            log.info(f"WTI deployed: {wti_rows:,} rows")
    else:
        log.warning("No WTI file to deploy")

    # Load forecasts into DuckDB — check both new and legacy filenames
    forecast_path = cfg.forecast_dir / "all_forecasts.parquet"
    if not forecast_path.exists():
        forecast_path = cfg.forecast_dir / "all_forecasts_v3.parquet"  # legacy fallback
    if forecast_path.exists():
        with log.timed("deploy forecasts to DuckDB"):
            fc_df = pd.read_parquet(forecast_path)
            with write_connection(cfg.duckdb_path) as con:
                min_d = fc_df["park_date"].min()
                max_d = fc_df["park_date"].max()
                if hasattr(min_d, "date"):
                    min_d = min_d.date()
                if hasattr(max_d, "date"):
                    max_d = max_d.date()
                con.execute(
                    "DELETE FROM forecasts WHERE park_date >= ? AND park_date <= ?",
                    [min_d, max_d],
                )
                con.register("_fc", fc_df)
                con.execute(
                    """
                    INSERT INTO forecasts (
                        entity_code, park_date, time_slot,
                        predicted_actual, prediction_method, updated_at
                    )
                    SELECT entity_code, park_date::DATE, CAST(time_slot AS VARCHAR),
                           predicted_actual, COALESCE(prediction_method, 'model'),
                           CURRENT_TIMESTAMP
                    FROM _fc
                    """
                )
                _upsert_freshness(con, "forecasts", "pipeline")
            fc_rows = len(fc_df)
            log.info(f"Forecasts deployed: {fc_rows:,} rows")
            del fc_df
    else:
        log.warning("No forecast file to deploy")

    # D1 #6 — RO assert; failure fails the step (pipeline marks failed + local alert)
    deploy_assert = assert_live_wti(Path(cfg.duckdb_path), log)

    # Also drop a sidecar next to metrics for operators who only read logs/
    try:
        side = Path(cfg.logs_dir) / f"deploy_assert_{date.today().isoformat()}.json"
        import json

        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text(json.dumps(deploy_assert, indent=2, default=str) + "\n")
        log.info(f"deploy assert sidecar: {side}")
    except Exception as e:
        log.warning(f"could not write deploy_assert sidecar: {e}")

    return {
        "rows": wti_rows + fc_rows,
        "action": "deployed",
        "wti_rows_deployed": wti_rows,
        "forecast_rows_deployed": fc_rows,
        "deploy_assert": deploy_assert,
    }
