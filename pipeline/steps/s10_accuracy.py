"""Step 10: Accuracy Evaluation.

Compares archived forecasts against actuals.
Reports MAE, bias, RMSE, median AE. No MAPE (broken for near-zero actuals).

Workflow:
1. Archive current forecast + WTI for future comparison
2. Find dates where archived forecasts now have actuals available
3. Compare forecast vs actuals at slot, entity-date, and WTI levels
4. Append results to accumulating accuracy parquets
5. Generate accuracy_summary.json for dashboards

Output files:
  - accuracy/slot_accuracy.parquet            (per entity, per 5-min slot)
  - accuracy/entity_daily_accuracy.parquet    (per entity, per day — aggregated)
  - accuracy/wti_accuracy.parquet             (per park, per day — WTI level)
  - accuracy/archive/forecast_YYYY-MM-DD.parquet
  - accuracy/archive/wti_YYYY-MM-DD.parquet
  - accuracy/accuracy_summary.json
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from pipeline.config import PipelineConfig
from pipeline.core.db import read_connection
from pipeline.core.logging import PipelineLogger
from pipeline.models import bias_guard


def _extract_date(filename: str) -> str | None:
    """Extract YYYY-MM-DD from a filename."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", filename)
    return m.group(1) if m else None


def _archive_forecast(cfg: PipelineConfig, log: PipelineLogger, run_date: str):
    """Archive current forecast + WTI before they get overwritten."""
    archive_dir = cfg.accuracy_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Archive forecast — check both new and legacy filenames
    forecast_path = cfg.forecast_dir / "all_forecasts.parquet"
    if not forecast_path.exists():
        forecast_path = cfg.forecast_dir / "all_forecasts_v3.parquet"  # legacy fallback
    if forecast_path.exists():
        archive_path = archive_dir / f"forecast_{run_date}.parquet"
        if not archive_path.exists():
            with log.timed("archive forecast"):
                cutoff = (datetime.strptime(run_date, "%Y-%m-%d") + timedelta(days=14)).strftime("%Y-%m-%d")
                with read_connection() as con:
                    con.execute(f"""
                        COPY (
                            SELECT entity_code, park_date, time_slot,
                                   predicted_actual, prediction_method,
                                   '{run_date}' as forecast_made_date
                            FROM read_parquet('{forecast_path}')
                            WHERE park_date <= '{cutoff}'::DATE
                        ) TO '{archive_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
                    """)
                log.info(f"Archived forecast for {run_date}")
        else:
            log.info(f"Forecast archive already exists for {run_date}")
    else:
        log.info("No forecast file to archive yet")

    # Archive WTI forecast — check both new and legacy filenames
    wti_path = cfg.wti_dir / "wti.parquet"
    if not wti_path.exists():
        wti_path = cfg.wti_dir / "wti_v3.parquet"  # legacy fallback
    if wti_path.exists():
        wti_archive = archive_dir / f"wti_{run_date}.parquet"
        if not wti_archive.exists():
            with log.timed("archive WTI"):
                cutoff = (datetime.strptime(run_date, "%Y-%m-%d") + timedelta(days=14)).strftime("%Y-%m-%d")
                with read_connection() as con:
                    con.execute(f"""
                        COPY (
                            SELECT park_code, park_date, wti, source,
                                   '{run_date}' as wti_made_date
                            FROM read_parquet('{wti_path}')
                            WHERE source = 'forecast'
                            AND park_date <= '{cutoff}'::TIMESTAMP
                        ) TO '{wti_archive}' (FORMAT PARQUET, COMPRESSION ZSTD)
                    """)
                log.info(f"Archived WTI forecast for {run_date}")


def _get_eval_dates(cfg: PipelineConfig, log: PipelineLogger) -> list[str]:
    """Find dates where we have both archived forecasts and actuals, minus already-evaluated."""
    archive_dir = cfg.accuracy_dir / "archive"

    # Collect ALL archived forecasts (new naming + legacy v3 naming)
    # Sort by extracted date, not filename — forecast_v3_* sorts after forecast_* alphabetically
    # but may be older by date (#472)
    archive_files = sorted(
        (f for f in archive_dir.glob("forecast_*.parquet")
         if not f.name.endswith(".CONTAMINATED_BACKUP")),
        key=lambda f: _extract_date(f.name) or ""
    )
    if not archive_files:
        log.info("No archived forecasts found")
        return []

    archive_glob = "', '".join(str(f) for f in archive_files)

    with read_connection() as con:
        # All dates that appear in any archived forecast
        forecast_dates = con.execute(f"""
            SELECT DISTINCT park_date::VARCHAR as park_date
            FROM read_parquet(['{archive_glob}'])
            ORDER BY park_date
        """).fetchdf()["park_date"].tolist()

        if not forecast_dates:
            return []

        log.info(f"Archived forecasts cover {len(forecast_dates)} unique dates ({forecast_dates[0]} to {forecast_dates[-1]})")

        # Find which dates have actual data in fact tables
        parquet_dir = cfg.parquet_dir
        recent_parquets = sorted(
            f for f in parquet_dir.glob("*.parquet")
            if f.name >= "2026-01"
        )[-3:]  # last 3 months

        if not recent_parquets:
            log.warning("No recent fact table parquets found")
            return []

        parquet_glob = "', '".join(str(f) for f in recent_parquets)
        actual_dates = con.execute(f"""
            SELECT DISTINCT park_date::VARCHAR as park_date
            FROM read_parquet(['{parquet_glob}'])
            WHERE wait_time_type = 'ACTUAL'
            AND park_date::VARCHAR >= '{forecast_dates[0]}'
        """).fetchdf()["park_date"].tolist()

        eval_dates = sorted(set(str(d) for d in forecast_dates) & set(str(d) for d in actual_dates))
        log.info(f"Dates with both forecast and actuals: {len(eval_dates)}")

        # Exclude already-evaluated dates
        entity_accuracy_path = cfg.accuracy_dir / "entity_daily_accuracy.parquet"
        if entity_accuracy_path.exists():
            already_done = set(con.execute(f"""
                SELECT DISTINCT park_date::VARCHAR as park_date
                FROM read_parquet('{entity_accuracy_path}')
            """).fetchdf()["park_date"].astype(str).tolist())
            eval_dates = [d for d in eval_dates if d not in already_done]

    return eval_dates


def _evaluate_slots_and_entities(
    cfg: PipelineConfig, log: PipelineLogger,
    eval_dates: list[str], run_date: str
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Compare forecast vs actuals at slot and entity-date level."""
    if not eval_dates:
        return None, None

    archive_dir = cfg.accuracy_dir / "archive"
    # All forecast archives (new naming + legacy v3 naming)
    # Sort by extracted date, not filename (#472)
    archive_files = sorted(
        (f for f in archive_dir.glob("forecast_*.parquet")
         if not f.name.endswith(".CONTAMINATED_BACKUP")),
        key=lambda f: _extract_date(f.name) or ""
    )
    if not archive_files:
        log.warning("No archived forecasts found")
        return None, None

    # Use the most recent archive made BEFORE the earliest eval date
    earliest_eval = min(eval_dates)
    valid_archives = [
        f for f in archive_files
        if (_extract_date(f.name) or "") < earliest_eval
    ]
    forecast_archive = valid_archives[-1] if valid_archives else archive_files[0]
    log.info(f"Using archived forecast: {forecast_archive.name}")

    # Build actuals CTE from fact tables + synthetic actuals
    parquet_dir = cfg.parquet_dir
    recent_parquets = sorted(
        f for f in parquet_dir.glob("*.parquet")
        if f.name >= "2026-01"
    )[-3:]
    parquet_glob = "', '".join(str(f) for f in recent_parquets)
    date_list = "', '".join(eval_dates)

    synth_dir = cfg.output_base / "synthetic_actuals"
    synth_glob = str(synth_dir).replace("\\", "/") + "/*.parquet"
    has_synth = synth_dir.exists() and any(synth_dir.glob("*.parquet"))

    actuals_cte = f"""
        WITH actuals_raw AS (
            SELECT
                entity_code,
                park_date::VARCHAR as park_date,
                TIME_BUCKET(INTERVAL '5 minutes',
                    (observed_at_ts::TIMESTAMP + INTERVAL '2 minutes 30 seconds'))::TIME as time_slot,
                AVG(wait_time_minutes) as actual_wait,
                COUNT(*) as n_obs
            FROM read_parquet(['{parquet_glob}'])
            WHERE wait_time_type = 'ACTUAL'
            AND park_date::VARCHAR IN ('{date_list}')
            GROUP BY entity_code, park_date, time_slot
        )"""

    if has_synth:
        actuals_cte += f""",
        actuals_synth AS (
            SELECT
                entity_code,
                park_date::VARCHAR as park_date,
                TIME_BUCKET(INTERVAL '5 minutes',
                    (CAST(observed_at AS TIMESTAMP) + INTERVAL '2 minutes 30 seconds'))::TIME as time_slot,
                AVG(synthetic_actual) as actual_wait,
                COUNT(*) as n_obs
            FROM read_parquet('{synth_glob}')
            WHERE park_date::VARCHAR IN ('{date_list}')
            AND synthetic_actual > 0
            GROUP BY entity_code, park_date, time_slot
        ),
        actuals_bucketed AS (
            SELECT
                COALESCE(r.entity_code, s.entity_code) as entity_code,
                COALESCE(r.park_date, s.park_date) as park_date,
                COALESCE(r.time_slot, s.time_slot) as time_slot,
                COALESCE(r.actual_wait, s.actual_wait) as actual_wait,
                COALESCE(r.n_obs, 0) + COALESCE(s.n_obs, 0) as n_obs
            FROM actuals_raw r
            FULL OUTER JOIN actuals_synth s
                ON r.entity_code = s.entity_code
                AND r.park_date = s.park_date
                AND r.time_slot = s.time_slot
        )"""
    else:
        actuals_cte += """,
        actuals_bucketed AS (
            SELECT entity_code, park_date, time_slot, actual_wait, n_obs FROM actuals_raw
        )"""

    with read_connection() as con:
        # Slot-level accuracy
        slot_df = con.execute(f"""
            {actuals_cte},
            forecasts AS (
                SELECT
                    entity_code,
                    park_date::VARCHAR as park_date,
                    time_slot,
                    predicted_actual as forecast_wait,
                    prediction_method,
                    forecast_made_date
                FROM read_parquet('{forecast_archive}')
                WHERE park_date::VARCHAR IN ('{date_list}')
            )
            SELECT
                f.entity_code,
                f.park_date,
                f.time_slot,
                f.forecast_wait,
                a.actual_wait,
                a.n_obs,
                f.prediction_method,
                f.forecast_made_date,
                (f.forecast_wait - a.actual_wait) as signed_error,
                ABS(f.forecast_wait - a.actual_wait) as absolute_error,
                DATEDIFF('day', f.forecast_made_date::DATE, f.park_date::DATE) as horizon_days,
                '{run_date}' as evaluation_date
            FROM forecasts f
            INNER JOIN actuals_bucketed a
                ON f.entity_code = a.entity_code
                AND f.park_date = a.park_date
                AND f.time_slot = a.time_slot
        """).fetchdf()

        if slot_df.empty:
            log.warning(f"No matching forecast-actual pairs found for eval dates: {eval_dates}")
            return None, None

        n_entity_dates = slot_df.groupby(["entity_code", "park_date"]).ngroups
        log.info(f"Slot-level matches: {len(slot_df)} rows across {n_entity_dates} entity-dates")

        # Entity-date level aggregation
        entity_df = con.execute("""
            SELECT
                entity_code,
                park_date,
                evaluation_date,
                forecast_made_date,
                horizon_days,
                prediction_method,
                COUNT(*) as n_slots,
                AVG(forecast_wait) as avg_forecast,
                AVG(actual_wait) as avg_actual,
                AVG(signed_error) as bias,
                AVG(absolute_error) as mae,
                SQRT(AVG(signed_error * signed_error)) as rmse,
                -- Keep MAPE for backward compat with summary, but don't trust it
                AVG(CASE WHEN actual_wait > 0
                    THEN ABS(forecast_wait - actual_wait) / actual_wait * 100
                    ELSE NULL END) as mape,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY absolute_error) as median_ae
            FROM slot_df
            GROUP BY entity_code, park_date, evaluation_date, forecast_made_date,
                     horizon_days, prediction_method
        """).fetchdf()

        log.info(f"Entity-date accuracy: {len(entity_df)} rows, MAE={entity_df['mae'].mean():.1f} min, bias={entity_df['bias'].mean():.1f} min")

    return slot_df, entity_df


def _evaluate_wti(
    cfg: PipelineConfig, log: PipelineLogger,
    eval_dates: list[str], run_date: str
) -> pd.DataFrame | None:
    """Compare archived WTI forecast vs historical WTI (computed from actuals)."""
    # Check both new and legacy WTI filenames
    wti_path = cfg.wti_dir / "wti.parquet"
    if not wti_path.exists():
        wti_path = cfg.wti_dir / "wti_v3.parquet"  # legacy fallback
    if not wti_path.exists():
        log.info("No WTI data — skipping WTI accuracy")
        return None

    archive_dir = cfg.accuracy_dir / "archive"
    # Collect all WTI archives (new wti_ + legacy wti_v3_)
    # Sort by extracted date, not filename — wti_v3_* sorts after wti_* alphabetically
    # but may be older by date (same bug as #472 for forecast archives)
    wti_archives = sorted(
        (f for f in archive_dir.glob("wti_*.parquet")
         if not f.name.endswith(".CONTAMINATED_BACKUP")),
        key=lambda f: _extract_date(f.name) or ""
    )
    if not wti_archives:
        log.info("No archived WTI files — WTI accuracy will start tomorrow")
        return None

    # Determine which dates need WTI evaluation
    wti_eval_dates = set(eval_dates)

    # Backfill: dates with entity accuracy but missing WTI accuracy
    entity_path = cfg.accuracy_dir / "entity_daily_accuracy.parquet"
    wti_accuracy_path = cfg.accuracy_dir / "wti_accuracy.parquet"

    with read_connection() as con:
        if entity_path.exists():
            try:
                all_entity_dates = set(con.execute(f"""
                    SELECT DISTINCT park_date::DATE::VARCHAR
                    FROM read_parquet('{entity_path}')
                """).fetchdf().iloc[:, 0].astype(str).tolist())

                wti_done = set()
                if wti_accuracy_path.exists():
                    wti_done = set(con.execute(f"""
                        SELECT DISTINCT park_date::DATE::VARCHAR
                        FROM read_parquet('{wti_accuracy_path}')
                    """).fetchdf().iloc[:, 0].astype(str).tolist())

                backfill = all_entity_dates - wti_done
                if backfill:
                    log.info(f"WTI backfill: {len(backfill)} dates need WTI evaluation")
                    wti_eval_dates |= backfill
            except Exception as e:
                log.warning(f"Failed to compute WTI backfill: {e}")

        if not wti_eval_dates:
            log.info("No dates need WTI evaluation")
            return None

        wti_date_list = sorted(wti_eval_dates)
        log.info(f"WTI evaluation for {len(wti_date_list)} dates: {wti_date_list[0]} to {wti_date_list[-1]}")

        # For each date, find the best archive made before that date
        all_pairs = []
        for eval_d in wti_date_list:
            valid = [f for f in wti_archives if (_extract_date(f.name) or "") < eval_d]
            if not valid:
                continue
            best = valid[-1]
            try:
                pair_df = con.execute(f"""
                    WITH forecast_wti AS (
                        SELECT park_code, park_date::DATE::VARCHAR as park_date,
                               wti as forecast_wti, wti_made_date
                        FROM read_parquet('{best}')
                        WHERE park_date::DATE::VARCHAR = '{eval_d}'
                    ),
                    actual_wti AS (
                        SELECT park_code, park_date::DATE::VARCHAR as park_date,
                               wti as actual_wti
                        FROM read_parquet('{wti_path}')
                        WHERE source = 'historical'
                        AND park_date::DATE::VARCHAR = '{eval_d}'
                    )
                    SELECT
                        f.park_code,
                        f.park_date,
                        f.forecast_wti,
                        a.actual_wti,
                        f.wti_made_date,
                        (f.forecast_wti - a.actual_wti) as wti_error,
                        ABS(f.forecast_wti - a.actual_wti) as wti_abs_error,
                        '{run_date}' as evaluation_date
                    FROM forecast_wti f
                    INNER JOIN actual_wti a
                        ON f.park_code = a.park_code AND f.park_date = a.park_date
                """).fetchdf()
                if not pair_df.empty:
                    all_pairs.append(pair_df)
            except Exception as e:
                log.warning(f"WTI accuracy failed for {eval_d}: {e}")

    if all_pairs:
        wti_df = pd.concat(all_pairs, ignore_index=True)
        log.info(f"WTI accuracy: {len(wti_df)} park-dates, avg abs error={wti_df['wti_abs_error'].mean():.1f}")
        return wti_df

    log.info("No WTI forecast-vs-actual matches found")
    return None


def _save_results(cfg: PipelineConfig, log: PipelineLogger,
                  slot_df, entity_df, wti_df) -> int:
    """Append results to accumulating accuracy parquets. Returns total new rows."""
    accuracy_dir = cfg.accuracy_dir
    total_new = 0

    dedup_keys = {
        "slot_accuracy": ["entity_code", "park_date", "time_slot", "evaluation_date"],
        "entity_daily_accuracy": ["entity_code", "park_date", "evaluation_date"],
        "wti_accuracy": ["park_code", "park_date", "evaluation_date"],
    }

    for name, df in [("slot_accuracy", slot_df),
                     ("entity_daily_accuracy", entity_df),
                     ("wti_accuracy", wti_df)]:
        if df is None or df.empty:
            continue

        path = accuracy_dir / f"{name}.parquet"
        if path.exists():
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, df], ignore_index=True)
            combined = combined.drop_duplicates(subset=dedup_keys[name])
            new_count = len(combined) - len(existing)
            combined.to_parquet(path, index=False)
            log.info(f"Updated {name}: {len(combined)} total rows (+{new_count} new)")
            total_new += new_count
        else:
            df.to_parquet(path, index=False)
            log.info(f"Created {name}: {len(df)} rows")
            total_new += len(df)

    return total_new


def _generate_summary(cfg: PipelineConfig, log: PipelineLogger) -> dict:
    """Generate accuracy_summary.json for dashboards."""
    entity_path = cfg.accuracy_dir / "entity_daily_accuracy.parquet"
    if not entity_path.exists():
        log.info("No entity accuracy data yet — skipping summary")
        return {}

    with read_connection() as con:
        summary = con.execute(f"""
            SELECT
                COUNT(DISTINCT park_date) as dates_evaluated,
                COUNT(DISTINCT entity_code) as entities_evaluated,
                AVG(mae) as overall_mae,
                AVG(bias) as overall_bias,
                AVG(mape) as overall_mape,
                AVG(rmse) as overall_rmse,
                AVG(CASE WHEN horizon_days <= 1 THEN mae END) as mae_1day,
                AVG(CASE WHEN horizon_days BETWEEN 2 AND 7 THEN mae END) as mae_7day,
                AVG(CASE WHEN horizon_days BETWEEN 8 AND 30 THEN mae END) as mae_30day,
                MIN(park_date) as first_eval_date,
                MAX(park_date) as last_eval_date,
                MAX(evaluation_date) as last_run
            FROM read_parquet('{entity_path}')
        """).fetchdf().to_dict(orient="records")[0]

        # Add WTI summary
        wti_path = cfg.accuracy_dir / "wti_accuracy.parquet"
        if wti_path.exists():
            try:
                wti_summary = con.execute(f"""
                    SELECT
                        COUNT(*) as wti_park_dates_evaluated,
                        COUNT(DISTINCT park_date) as wti_dates_evaluated,
                        AVG(wti_abs_error) as wti_mae,
                        AVG(wti_error) as wti_bias,
                        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY wti_abs_error) as wti_median_ae,
                        MIN(park_date) as wti_first_eval_date,
                        MAX(park_date) as wti_last_eval_date
                    FROM read_parquet('{wti_path}')
                """).fetchdf().to_dict(orient="records")[0]
                summary.update(wti_summary)
            except Exception as e:
                log.warning(f"Failed to add WTI summary: {e}")

    summary_path = cfg.accuracy_dir / "accuracy_summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    mae = summary.get("overall_mae", 0) or 0
    bias = summary.get("overall_bias", 0) or 0
    dates_eval = summary.get("dates_evaluated", 0) or 0
    entities_eval = summary.get("entities_evaluated", 0) or 0
    log.info(f"Summary: MAE={mae:.1f} | bias={bias:.1f} | {dates_eval} dates, {entities_eval} entities")

    return summary


def _update_bias_guard(cfg: PipelineConfig, log: PipelineLogger, run_date: str) -> list[str]:
    slot_path = cfg.accuracy_dir / "slot_accuracy.parquet"
    if not slot_path.exists():
        log.info("Bias guard: no slot_accuracy.parquet yet")
        return []
    cols = ["entity_code", "park_date", "time_slot", "prediction_method",
            "signed_error", "absolute_error", "evaluation_date"]
    slot_df = pd.read_parquet(slot_path, columns=cols)
    as_of = datetime.strptime(run_date, "%Y-%m-%d").date() - timedelta(days=1)
    result = bias_guard.evaluate(slot_df, as_of, bias_guard.load(cfg.state_dir).get("entities"))
    bias_guard.save(result, cfg.state_dir, cfg.accuracy_dir)
    for code in result["guarded"]:
        e = result["entities"][code]
        log.warning(f"Bias guard: {code} -> fallback (bias={e['bias']:+.1f}, mae={e['mae']:.1f}, n={e['n_slots']}, {e['basis']})")
    log.info(f"Bias guard: {len(result['guarded'])} entities routed to fallback ({bias_guard.REASON})")
    return result["guarded"]


def run(cfg: PipelineConfig, log: PipelineLogger) -> dict:
    """Evaluate forecast accuracy and archive current forecast."""

    log.info("=" * 60)
    log.info("STEP 10: ACCURACY EVALUATION")
    log.info("=" * 60)

    run_date = datetime.now().strftime("%Y-%m-%d")
    cfg.accuracy_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Archive current forecast + WTI
    _archive_forecast(cfg, log, run_date)

    # Step 2: Find dates to evaluate
    with log.timed("find eval dates"):
        eval_dates = _get_eval_dates(cfg, log)

    if eval_dates:
        log.info(f"Evaluating {len(eval_dates)} new dates: {eval_dates[0]} to {eval_dates[-1]}")

    # Step 3: Slot + entity-level accuracy
    with log.timed("slot/entity accuracy"):
        slot_df, entity_df = _evaluate_slots_and_entities(cfg, log, eval_dates, run_date)

    # Step 4: WTI accuracy (independent — includes backfill)
    with log.timed("WTI accuracy"):
        wti_df = _evaluate_wti(cfg, log, eval_dates, run_date)

    # Step 5: Save results
    total_new = 0
    if any(df is not None for df in [slot_df, entity_df, wti_df]):
        with log.timed("save results"):
            total_new = _save_results(cfg, log, slot_df, entity_df, wti_df)
    else:
        if not eval_dates:
            log.info("No new dates to evaluate — all archived dates already scored")
        else:
            log.warning(f"Evaluation returned no results for {len(eval_dates)} dates")

    # Step 6: Generate summary (always — even if no new data, to keep it fresh)
    with log.timed("generate summary"):
        summary = _generate_summary(cfg, log)

    with log.timed("bias guard"):
        guarded = _update_bias_guard(cfg, log, run_date)

    return {
        "rows": total_new,
        "eval_dates": len(eval_dates),
        "entity_rows": len(entity_df) if entity_df is not None else 0,
        "wti_rows": len(wti_df) if wti_df is not None else 0,
        "bias_guarded": guarded,
        "summary": {k: v for k, v in summary.items()
                    if k in ("overall_mae", "overall_bias", "dates_evaluated", "entities_evaluated")},
    }
