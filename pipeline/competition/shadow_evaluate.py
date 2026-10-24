"""Shadow Evaluation — Compare archived shadow predictions against actuals.

Uses the EXACT SAME evaluation methodology as s10_accuracy.py:
  - Actuals from wait_time_type = 'ACTUAL' (NOT POSTED)
  - TIME_BUCKET with 2.5-minute midpoint rounding for time slot alignment
  - Synthetic actuals as fallback when real actuals are missing
  - Entity-weighted MAE (average of per-entity MAEs, not flat slot average)

This ensures shadow run results are directly comparable to pipeline accuracy.

Identical set (TPCR-SHADOW-REVIVE-001 AC6): production and challenger are scored
on the same (entity, slot) pairs, the intersection where both have a prediction
and an actual exists. Pairs dropped on any side are counted by reason
(no_actual, no_prod_pred, no_chal_pred) and logged. The reported MAEs are
computed from that paired set (pipeline.competition.paired_scores).

Fallback entities (AC7): single-rider queues plus entities production served by
ratio. Their MAE is reported separately for both sides.

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
        --output-base ~/hazeydata/pipeline \
        [--scores-out /path/to/scores.parquet]

Output: JSON report to stdout (RESULT:{...}) for parsing by the orchestrator.
--scores-out additionally writes the paired score rows (parquet) for the run.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from pipeline.competition.fallback_entities import fallback_entities
from pipeline.competition.io_utils import atomic_write_parquet
from pipeline.competition.paired_scores import build_paired_scores, summarize

log = logging.getLogger(__name__)


def _actuals_cte(reference_date: str, output_base: Path) -> str | None:
    """Actuals CTE: EXACT SAME as s10_accuracy.py. None if no fact tables."""
    parquet_dir = output_base / "fact_tables" / "parquet"
    synth_dir = output_base / "synthetic_actuals"

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
    return actuals_cte


def load_day_frames(reference_date: str, challenger_parquet: str, baseline_parquet: str,
                    output_base: str):
    """Load archived baseline/challenger predictions and actuals for one date.

    Returns (prod_df, chal_df, actual_df) keyed by entity_code, slot (HH:MM:SS
    string) with prod_pred / chal_pred / actual columns, or None when there are
    no fact tables or an archive is missing. prod_df also carries
    prediction_method (used to find ratio-served entities).
    """
    import duckdb

    chal_path = Path(challenger_parquet)
    base_path = Path(baseline_parquet)
    if not chal_path.exists() or not base_path.exists():
        return None

    actuals_cte = _actuals_cte(reference_date, Path(output_base))
    if actuals_cte is None:
        return None

    con = duckdb.connect()
    try:
        actual_df = con.execute(f"""
            {actuals_cte}
            SELECT entity_code, CAST(time_slot AS VARCHAR) AS slot, actual_wait AS actual
            FROM actuals
        """).fetchdf()
        prod_df = con.execute(f"""
            SELECT entity_code, CAST(time_slot AS VARCHAR) AS slot,
                   predicted_actual AS prod_pred, prediction_method
            FROM read_parquet('{baseline_parquet}')
        """).fetchdf()
        chal_df = con.execute(f"""
            SELECT entity_code, CAST(time_slot AS VARCHAR) AS slot,
                   predicted_actual AS chal_pred
            FROM read_parquet('{challenger_parquet}')
        """).fetchdf()
    finally:
        con.close()
    return prod_df, chal_df, actual_df


def load_entities(output_base: str):
    """dimentity.csv as a DataFrame (code, name, ...), or None if missing."""
    path = Path(output_base) / "dimension_tables" / "dimentity.csv"
    if not path.exists():
        return None
    return pd.read_csv(path)


def _round(x, nd=2):
    return None if x is None else round(float(x), nd)


def build_report(reference_date: str, challenger_id: str, scores: pd.DataFrame,
                 dropped: dict, forecast_entities: int) -> dict | None:
    """Evaluation report computed from the paired score rows. None if no pairs."""
    if len(scores) == 0:
        return None

    summary = summarize(scores, dropped, forecast_entities)
    cs = summary["challengers"][challenger_id]

    err_p = scores["prod_pred"] - scores["actual"]
    err_c = scores["chal_pred"] - scores["actual"]
    ae_p, ae_c = err_p.abs(), err_c.abs()

    per_entity = pd.DataFrame({
        "entity_code": scores["entity_code"],
        "ae_p": ae_p, "ae_c": ae_c, "err_p": err_p, "err_c": err_c,
    }).groupby("entity_code").mean()
    per_entity["slots"] = scores.groupby("entity_code").size()

    entity_chal_wins = int((per_entity["ae_c"] < per_entity["ae_p"]).sum())
    entity_base_wins = int((per_entity["ae_p"] < per_entity["ae_c"]).sum())
    n_entities = len(per_entity)

    parks = per_entity.assign(park_code=per_entity.index.str[:2]).groupby("park_code")
    park_report = {
        park: {
            "baseline_mae": _round(g["ae_p"].mean()),
            "challenger_mae": _round(g["ae_c"].mean()),
            "entities": int(len(g)),
        }
        for park, g in parks
    }

    return {
        "reference_date": reference_date,
        # Primary MAE: entity-weighted (aligned with s10_accuracy.py), paired set only
        "baseline_mae": _round(cs["mae_prod"]),
        "challenger_mae": _round(cs["mae_chal"]),
        "baseline_bias": _round(per_entity["err_p"].mean()),
        "challenger_bias": _round(per_entity["err_c"].mean()),
        # Slot-level MAE: for reference only (flat average, over-weights high-traffic entities)
        "slot_baseline_mae": _round(ae_p.mean()),
        "slot_challenger_mae": _round(ae_c.mean()),
        "n_matched": int(len(scores)),
        "n_entities": n_entities,
        # Entity-level win/loss
        "entity_challenger_wins": entity_chal_wins,
        "entity_baseline_wins": entity_base_wins,
        "entity_ties": n_entities - entity_chal_wins - entity_base_wins,
        # Slot-level win/loss
        "challenger_slot_wins": int((ae_c < ae_p).sum()),
        "baseline_slot_wins": int((ae_p < ae_c).sum()),
        "parks": park_report,
        # Identical-set and fallback reporting
        "matched_pairs": int(len(scores)),
        "forecast_entities": summary["forecast_entities"],
        "coverage_pct": cs["coverage_pct"],
        "dropped": cs["dropped"],
        "mae_fallback_prod": _round(cs["mae_fallback_prod"]),
        "mae_fallback_chal": _round(cs["mae_fallback_chal"]),
        "n_fallback_entities": cs["n_fallback_entities"],
    }


def evaluate_paired(reference_date: str, challenger_id: str, challenger_parquet: str,
                    baseline_parquet: str, output_base: str):
    """Load archives + actuals and build the paired set.

    Returns (scores_df, dropped, forecast_entities) or None if inputs are missing.
    """
    frames = load_day_frames(reference_date, challenger_parquet, baseline_parquet, output_base)
    if frames is None:
        return None
    prod_df, chal_df, actual_df = frames
    fb = fallback_entities(prod_df, load_entities(output_base))
    scores, dropped = build_paired_scores(
        prod_df, chal_df, actual_df, challenger_id, reference_date, fb
    )
    return scores, dropped, int(prod_df["entity_code"].nunique())


def evaluate_shadow_day(
    reference_date: str,
    challenger_parquet: str,
    baseline_parquet: str,
    output_base: str,
    challenger_id: str = "challenger",
    scores_out: str | None = None,
) -> dict | None:
    """Compare one day of archived shadow predictions against actuals.

    Same actuals methodology as s10_accuracy.py (ACTUAL type, TIME_BUCKET
    slots, synthetic fallback, entity-weighted MAE), computed on the identical
    (entity, slot) set.

    Args:
        reference_date: The date the predictions are FOR (YYYY-MM-DD).
        challenger_parquet: Archived challenger predictions for reference_date.
        baseline_parquet: Archived baseline predictions for reference_date.
        output_base: Pipeline output base (e.g., ~/hazeydata/pipeline).
        challenger_id: written to the score rows.
        scores_out: if given, paired score rows are written here (atomic parquet).

    Returns:
        Dict with evaluation metrics, or None if there are no matched pairs.
    """
    try:
        paired = evaluate_paired(
            reference_date, challenger_id, challenger_parquet, baseline_parquet, output_base
        )
    except Exception as e:
        print(f"EVAL_ERROR: {e}", file=sys.stderr)
        return None
    if paired is None:
        return None
    scores, dropped, forecast_entities = paired

    report = build_report(reference_date, challenger_id, scores, dropped, forecast_entities)
    if report is None:
        return None
    if scores_out:
        atomic_write_parquet(scores, scores_out)
    return report


def main():
    """CLI entry point for shadow evaluation."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
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
    parser.add_argument("--scores-out", help="Write paired score rows (parquet) to this path")

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
        challenger_id=args.challenger,
        scores_out=args.scores_out,
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
