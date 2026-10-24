"""Shadow Evaluation — Compare archived shadow predictions against actuals.

Uses the EXACT SAME evaluation methodology as s10_accuracy.py:
  - Actuals from wait_time_type = 'ACTUAL' (NOT POSTED)
  - TIME_BUCKET with 2.5-minute midpoint rounding for time slot alignment
  - Synthetic actuals as fallback when real actuals are missing
  - Entity-weighted MAE (average of per-entity MAEs, not flat slot average)

This ensures shadow run results are directly comparable to pipeline accuracy.

MAE methodology (aligned with s10_accuracy.py in Session 26):
  1. Compute per-entity MAE = avg absolute error across that entity's time slots
  2. Overall MAE = avg of per-entity MAEs (each entity weighted equally)
  This matches how s10 computes entity_daily_accuracy then averages to overall_mae.
  The old slot-level flat average over-weighted high-traffic entities open longer hours.

ARCHIVE FILE NAMING CONVENTION (Session 27):
  Archive files are named by REFERENCE DATE — the date the predictions are FOR.
    baseline_{reference_date}.parquet   → predictions for reference_date
    challenger_{reference_date}.parquet → predictions for reference_date

  Example: baseline_2026-04-05.parquet contains predictions FOR April 5.
  The filename tells you exactly what's inside. No mental arithmetic needed.

  To evaluate actuals for a given date, open the archive with that same date:
    --reference-date 2026-04-03 opens baseline_2026-04-03.parquet
    which contains predictions FOR 2026-04-03, compared against actuals from 2026-04-03.

Usage (CLI):
    cd ~/theme-park-crowd-report
    source .venv/bin/activate
    python -m pipeline.competition.shadow_evaluate \
        --challenger xgb-highLR \
        --reference-date 2026-04-03 \
        --shadow-dir ~/hazeydata/pipeline/competition/shadow/xgb-highLR \
        --output-base ~/hazeydata/pipeline

Output: JSON report to stdout (RESULT:{...}) for parsing by the orchestrator.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path


def evaluate_shadow_day(
    reference_date: str,
    challenger_parquet: str,
    baseline_parquet: str,
    output_base: str,
) -> dict | None:
    """Compare one day of archived shadow predictions against actuals.

    Uses identical methodology to s10_accuracy.py:
      - wait_time_type = 'ACTUAL' (real actuals, not POSTED)
      - TIME_BUCKET(INTERVAL '5 minutes', ts + 2m30s) for slot alignment
      - Synthetic actuals fallback for entities without real actuals
      - Entity-weighted MAE (average of per-entity MAEs)

    Args:
        reference_date: The date the predictions are FOR (YYYY-MM-DD).
                        Actuals for this date are now available.
                        Archive files are named with this date.
        challenger_parquet: Path to archived challenger predictions for reference_date.
        baseline_parquet: Path to archived baseline predictions for reference_date.
        output_base: Pipeline output base (e.g., ~/hazeydata/pipeline).

    Returns:
        Dict with evaluation metrics, or None if insufficient data.
    """
    import duckdb

    output_base_path = Path(output_base)
    parquet_dir = output_base_path / "fact_tables" / "parquet"
    synth_dir = output_base_path / "synthetic_actuals"

    # Find recent parquet files for actuals
    recent_parquets = sorted(
        f for f in parquet_dir.glob("*.parquet")
        if f.name >= "2026-01"
    )[-3:]  # last 3 months

    if not recent_parquets:
        return None

    parquet_glob = "', '".join(str(f) for f in recent_parquets)
    has_synth = synth_dir.exists() and any(synth_dir.glob("*.parquet"))
    synth_glob = str(synth_dir).replace("\\", "/") + "/*.parquet"

    con = duckdb.connect()

    # --- Build actuals CTE: EXACT SAME as s10_accuracy.py ---
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
        AND park_date::VARCHAR = '{reference_date}'
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
        WHERE park_date::VARCHAR = '{reference_date}'
        AND synthetic_actual > 0
        GROUP BY entity_code, park_date, time_slot
    ),
    actuals AS (
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
    actuals AS (
        SELECT entity_code, park_date, time_slot, actual_wait, n_obs
        FROM actuals_raw
    )"""

    # Check if archived predictions exist
    chal_path = Path(challenger_parquet)
    base_path = Path(baseline_parquet)
    if not chal_path.exists() or not base_path.exists():
        return None

    # --- Build the matched CTE (reused by all queries) ---
    matched_cte = f"""
            {actuals_cte},
            challenger AS (
                SELECT entity_code, time_slot, predicted_actual
                FROM read_parquet('{challenger_parquet}')
            ),
            baseline AS (
                SELECT entity_code, time_slot, predicted_actual
                FROM read_parquet('{baseline_parquet}')
            ),
            matched AS (
                SELECT
                    a.entity_code,
                    a.time_slot,
                    a.actual_wait,
                    b.predicted_actual as baseline_pred,
                    c.predicted_actual as challenger_pred,
                    ABS(b.predicted_actual - a.actual_wait) as baseline_ae,
                    ABS(c.predicted_actual - a.actual_wait) as challenger_ae,
                    (b.predicted_actual - a.actual_wait) as baseline_error,
                    (c.predicted_actual - a.actual_wait) as challenger_error
                FROM actuals a
                JOIN baseline b ON a.entity_code = b.entity_code AND a.time_slot = b.time_slot
                JOIN challenger c ON a.entity_code = c.entity_code AND a.time_slot = c.time_slot
            )"""

    # --- Per-entity breakdown (PRIMARY — this drives the overall MAE) ---
    try:
        entity_rows = con.execute(f"""
            {matched_cte},
            entity_metrics AS (
                SELECT
                    entity_code,
                    ROUND(AVG(baseline_ae), 2) as entity_baseline_mae,
                    ROUND(AVG(challenger_ae), 2) as entity_challenger_mae,
                    ROUND(AVG(baseline_error), 2) as entity_baseline_bias,
                    ROUND(AVG(challenger_error), 2) as entity_challenger_bias,
                    COUNT(*) as entity_slots
                FROM matched
                GROUP BY entity_code
            )
            SELECT
                entity_code,
                entity_baseline_mae,
                entity_challenger_mae,
                entity_baseline_bias,
                entity_challenger_bias,
                entity_slots
            FROM entity_metrics
            ORDER BY entity_code
        """).fetchall()
    except Exception as e:
        print(f"EVAL_ERROR (entity): {e}", file=sys.stderr)
        return None

    if not entity_rows:
        return None

    # --- Derive overall MAE from entity-level (s10-aligned methodology) ---
    # Each entity gets equal weight, regardless of how many time slots it has.
    # This matches s10_accuracy.py which computes entity_daily_accuracy then averages.
    n_entities = len(entity_rows)
    baseline_mae = round(sum(r[1] for r in entity_rows) / n_entities, 2)
    challenger_mae = round(sum(r[2] for r in entity_rows) / n_entities, 2)
    baseline_bias = round(sum(r[3] for r in entity_rows) / n_entities, 2)
    challenger_bias = round(sum(r[4] for r in entity_rows) / n_entities, 2)
    total_slots = sum(r[5] for r in entity_rows)

    entity_challenger_wins = sum(1 for r in entity_rows if r[2] < r[1])
    entity_baseline_wins = sum(1 for r in entity_rows if r[1] < r[2])
    entity_ties = n_entities - entity_challenger_wins - entity_baseline_wins

    # --- Slot-level totals (for reference, NOT used as primary MAE) ---
    try:
        slot_result = con.execute(f"""
            {matched_cte}
            SELECT
                ROUND(AVG(baseline_ae), 2) as slot_baseline_mae,
                ROUND(AVG(challenger_ae), 2) as slot_challenger_mae,
                COUNT(*) as n_slots,
                SUM(CASE WHEN challenger_ae < baseline_ae THEN 1 ELSE 0 END) as challenger_slot_wins,
                SUM(CASE WHEN baseline_ae < challenger_ae THEN 1 ELSE 0 END) as baseline_slot_wins
            FROM matched
        """).fetchone()
    except Exception:
        slot_result = (None, None, total_slots, 0, 0)

    # --- Per-park breakdown (entity-weighted within each park) ---
    try:
        park_rows = con.execute(f"""
            {matched_cte},
            entity_park AS (
                SELECT
                    entity_code,
                    entity_code[:2] as park_code,
                    AVG(baseline_ae) as entity_baseline_mae,
                    AVG(challenger_ae) as entity_challenger_mae
                FROM matched
                GROUP BY entity_code
            )
            SELECT
                park_code,
                ROUND(AVG(entity_baseline_mae), 2) as park_baseline_mae,
                ROUND(AVG(entity_challenger_mae), 2) as park_challenger_mae,
                COUNT(*) as park_entities
            FROM entity_park
            GROUP BY park_code
            ORDER BY park_code
        """).fetchall()
    except Exception:
        park_rows = []

    con.close()

    report = {
        "reference_date": reference_date,
        # Primary MAE: entity-weighted (aligned with s10_accuracy.py)
        "baseline_mae": float(baseline_mae),
        "challenger_mae": float(challenger_mae),
        "baseline_bias": float(baseline_bias),
        "challenger_bias": float(challenger_bias),
        # Slot-level MAE: for reference only (flat average, over-weights high-traffic entities)
        "slot_baseline_mae": float(slot_result[0]) if slot_result[0] else None,
        "slot_challenger_mae": float(slot_result[1]) if slot_result[1] else None,
        "n_matched": total_slots,
        "n_entities": n_entities,
        # Entity-level win/loss
        "entity_challenger_wins": entity_challenger_wins,
        "entity_baseline_wins": entity_baseline_wins,
        "entity_ties": entity_ties,
        # Slot-level win/loss
        "challenger_slot_wins": slot_result[3] if slot_result else 0,
        "baseline_slot_wins": slot_result[4] if slot_result else 0,
        "parks": {
            r[0]: {
                "baseline_mae": r[1],
                "challenger_mae": r[2],
                "entities": r[3],
            }
            for r in park_rows
        },
    }
    return report


def main():
    """CLI entry point for shadow evaluation."""
    parser = argparse.ArgumentParser(
        description="Evaluate shadow predictions against actuals (same methodology as s10_accuracy)"
    )
    parser.add_argument("--challenger", required=True, help="Challenger name (e.g., xgb-highLR)")
    parser.add_argument("--reference-date", required=True,
                        help="The date the predictions are FOR (YYYY-MM-DD). "
                             "Archive files are named with this date. "
                             "Actuals for this date are compared against the archived predictions.")
    parser.add_argument("--shadow-dir", required=True, help="Directory containing shadow archives")
    parser.add_argument("--output-base", required=True, help="Pipeline output base directory")

    args = parser.parse_args()
    reference_date = args.reference_date

    shadow_dir = Path(args.shadow_dir)
    challenger_parquet = str(shadow_dir / f"challenger_{reference_date}.parquet")
    baseline_parquet = str(shadow_dir / f"baseline_{reference_date}.parquet")

    if not Path(challenger_parquet).exists():
        print(f"NO_DATA: No challenger archive for {reference_date}")
        return 0
    if not Path(baseline_parquet).exists():
        print(f"NO_DATA: No baseline archive for {reference_date}")
        return 0

    result = evaluate_shadow_day(
        reference_date=reference_date,
        challenger_parquet=challenger_parquet,
        baseline_parquet=baseline_parquet,
        output_base=args.output_base,
    )

    if result is None:
        print(f"NO_MATCH: No matched prediction-actual pairs for {reference_date}")
        return 0

    print("RESULT:" + json.dumps(result))
    return 0


if __name__ == "__main__":
    repo_root = Path(__file__).parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    sys.exit(main())
