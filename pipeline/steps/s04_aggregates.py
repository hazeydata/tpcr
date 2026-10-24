"""Step 4: Posted Aggregates.

Rebuilds aggregates/model_aggregates.parquet every production run: the posted-wait
median, mean, percentiles and 2-year half-life weighted mean for each
entity x date_group_id x 15-minute clock slot (0-95), over all history
through yesterday. Input is de-duplicated on the natural key, cells with
fewer than 5 observations are not written, and the file is written to a
temp path, validated and atomically renamed (TPCR-S4-AGG-001).

A failed build raises: the previous file is left byte-identical, the step
fails and the run exits nonzero. There is no 'missing' success path.
A sidecar model_aggregates.meta.json records build time, row count and
input max date. Read by S8 (posted_time) and the dashboard API.

Shadow mode does not rebuild: it validates the production file's row count
AND freshness (sidecar/max park_date >= yesterday) and fails if stale.
"""

from __future__ import annotations

from pipeline.config import PipelineConfig
from pipeline.core.logging import PipelineLogger
from pipeline.core.model_aggregates import (
    AggregateBuildError,
    build_model_aggregates,
    check_aggregates_freshness,
)
from pipeline.core.validation import ValidationError, require_parquet_rows


def run(cfg: PipelineConfig, log: PipelineLogger) -> dict:
    """Rebuild (production) or validate (shadow) the posted aggregates."""

    log.info("=" * 60)
    log.info("STEP 4: POSTED AGGREGATES")
    log.info("=" * 60)

    agg_path = cfg.output_base / "aggregates" / "model_aggregates.parquet"

    if cfg.shadow:
        log.info("Shadow mode: validating production aggregates")
        try:
            n_rows = require_parquet_rows(agg_path, min_rows=10000, description="Model aggregates")
            fresh, max_date, reason = check_aggregates_freshness(agg_path)
            if not fresh:
                log.error(f"Aggregates are stale: {reason}")
                raise ValidationError(f"S4 aggregate build failed: stale aggregates file: {reason}")
            log.info(f"Aggregates: {n_rows:,} entries, {reason} \u2714")
            return {"rows": n_rows, "action": "validated", "max_park_date": max_date.isoformat()}
        except Exception as e:
            log.error(f"Aggregates validation failed: {e}")
            raise

    try:
        with log.timed("build model_aggregates.parquet"):
            res = build_model_aggregates(cfg.output_base, log=log)
    except AggregateBuildError as e:
        log.error(f"S4 aggregate build FAILED, previous file kept: {e}")
        raise ValidationError(f"S4 aggregate build failed: {e}") from e

    log.info(
        f"Aggregates rebuilt: {res.rows:,} rows, {res.entities} entities, "
        f"input max date {res.input_max_date}, {res.duplicate_rows_dropped:,} duplicates dropped"
    )
    log.metric("aggregates_rows", res.rows)
    log.metric("aggregates_duplicates_dropped", res.duplicate_rows_dropped)
    log.metric("aggregates_build_sec", res.duration_sec)
    return {
        "rows": res.rows,
        "action": "rebuilt",
        "input_max_date": res.input_max_date.isoformat(),
        "duplicates_dropped": res.duplicate_rows_dropped,
    }
