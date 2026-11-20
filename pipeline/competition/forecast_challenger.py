"""Generate Challenger Forecasts.

Generates predictions using challenger models with the same methodology as baseline.
Reads the same shared data but writes to challenger-specific output paths.

Usage:
    python -m pipeline.competition.forecast_challenger --challenger hypertuned_v1 --output-base ~/hazeydata/pipeline
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import xgboost as xgb
except ImportError:
    xgb = None

from pipeline.config import PipelineConfig
from pipeline.core.logging import PipelineLogger
from pipeline.core.park_codes import entity_to_park
from pipeline.core.validation import ValidationError
from pipeline.competition.config import load_competition_config
from pipeline.competition.registry import load_registry

# Import data loading functions from baseline
from pipeline.steps.s08_forecast import (
    _load_date_features,
    _load_park_hours,
    _load_aggregates_indexed,
    _load_entity_list,
    _load_operating_calendar_indexed,
    _load_fallback_ratios,
    _generate_park_time_grid,
    FEATURES_BASELINE_V6 as FEATURES_BASELINE,  # challengers keep the 6 pre-recent-level features
)


def _forecast_challenger_entity(
    entity_code: str,
    park_code: str,
    time_grid: pd.DataFrame,
    challenger_models_dir: Path,
    challenger_name: str,
    agg_by_entity: dict,
    park_open_mins: dict,
    fallback_ratios: dict,
    oc_by_entity: dict | None,
    challenger_features: list[str],
) -> tuple[pd.DataFrame | None, str]:
    """Generate forecast for a single entity using challenger model.
    
    Based on _forecast_entity_fast but loads challenger models.
    """
    
    df = time_grid.copy()
    df["entity_code"] = entity_code

    # O(1) operating calendar filter (same as baseline)
    if oc_by_entity is not None:
        ec_upper = entity_code.upper()
        entity_dates = oc_by_entity.get(ec_upper)
        if entity_dates is not None:
            if len(entity_dates) == 0:
                return None, "extinct"  # Entity in calendar but zero dates = extinct
            df = df[df["park_date"].isin(entity_dates)]
        # If entity not in OC at all, assume operating (new entity)

    if len(df) == 0:
        return None, "no_dates"

    # O(1) aggregate lookup (same as baseline)
    entity_agg = agg_by_entity.get(entity_code)
    if entity_agg is not None and len(entity_agg) > 0:
        df = df.merge(
            entity_agg[["date_group_id", "time_slot_15min", "wait_median"]],
            on=["date_group_id", "time_slot_15min"],
            how="left",
        )
        df["posted_time"] = df["wait_median"].fillna(5.0)
        df.drop(columns=["wait_median"], inplace=True)
    else:
        df["posted_time"] = 5.0

    # Vectorized mins_since_open (same as baseline)
    df["_open_mins"] = df["park_date"].map(park_open_mins).fillna(6 * 60)
    df["mins_since_open"] = (df["mins_since_6am"] + 6 * 60 - df["_open_mins"]).clip(lower=0)
    df.drop(columns=["_open_mins"], inplace=True)

    # Look for challenger model
    entity_dir = challenger_models_dir / entity_code
    challenger_model_path = entity_dir / f"model_{challenger_name}.json"
    
    fallback_ratio = fallback_ratios.get(entity_code, fallback_ratios.get("__global__", 0.678))

    if challenger_model_path.exists():
        model = xgb.XGBRegressor()
        model.load_model(str(challenger_model_path))
        
        features = challenger_features
        method = f"model_{challenger_name}"
        feat_key = f"challenger_{challenger_name}"
        
        # Compute derived features if requested
        if "day_of_week" in features and "day_of_week" not in df.columns:
            import pandas as _pd
            df["day_of_week"] = _pd.to_datetime(df["park_date"]).dt.dayofweek.astype("float32")

        # Check if all features are available
        missing = [f for f in features if f not in df.columns]
        if missing:
            # Fallback to fallback ratio if features are missing
            df["predicted_actual"] = (df["posted_time"] * fallback_ratio).round().astype(int)
            df["prediction_method"] = "fallback_ratio_missing_features"
            return df[["entity_code", "park_date", "time_slot", "predicted_actual", "prediction_method"]], "fallback"
        
        # Run model prediction
        X = df[features].values.astype(np.float32)
        predictions = model.predict(X)
        predictions = np.clip(predictions, 0, 300)
        df["predicted_actual"] = np.round(predictions).astype(int)
        df["prediction_method"] = method

        return df[["entity_code", "park_date", "time_slot", "predicted_actual", "prediction_method"]], feat_key

    else:
        # No challenger model — use fallback ratio
        df["predicted_actual"] = (df["posted_time"] * fallback_ratio).round().astype(int)
        df["prediction_method"] = "fallback_ratio_no_model"
        return df[["entity_code", "park_date", "time_slot", "predicted_actual", "prediction_method"]], "fallback"


def run_challenger_forecasting(challenger_name: str, output_base: Path) -> dict:
    """Generate forecasts using challenger models."""
    
    if xgb is None:
        raise ValidationError("XGBoost is required for forecasting. pip install xgboost")
    
    # Load configurations
    baseline_cfg = PipelineConfig(output_base=output_base)
    competition_cfg = load_competition_config(output_base)
    
    # Set up logging
    log = PipelineLogger(f'forecast_challenger_{challenger_name}', competition_cfg.logs_dir)
    
    log.info("=" * 60)
    log.info(f"CHALLENGER FORECASTING: {challenger_name}")
    log.info("=" * 60)
    
    # Load challenger registry
    challengers_dir = Path(__file__).parent / "challengers"
    registry = load_registry(challengers_dir)
    challenger = registry.get_challenger(challenger_name)
    
    # Determine features to use
    challenger_features = FEATURES_BASELINE if challenger.features is None else challenger.features
    log.info(f"Challenger: {challenger.description}")
    log.info(f"Features: {challenger_features}")
    
    start_date = date.today()  # Include today — users ask about "today" and need predicted values
    end_date = start_date + timedelta(days=baseline_cfg.forecast_days)
    log.info(f"Forecast range: {start_date} to {end_date} ({baseline_cfg.forecast_days} days)")

    # Load shared data (same as baseline)
    with log.timed("load shared data"):
        date_features = _load_date_features(baseline_cfg, log)
        park_hours = _load_park_hours(baseline_cfg, log)
        agg_by_entity = _load_aggregates_indexed(baseline_cfg, log)
        entity_list = _load_entity_list(baseline_cfg, log)
        oc_by_entity = _load_operating_calendar_indexed(baseline_cfg, log)
        fallback_ratios = _load_fallback_ratios(baseline_cfg, log)

    # Group entities by park (same as baseline)
    park_entities: dict[str, list[str]] = {}
    for entity in entity_list:
        park = entity_to_park(entity)
        if park in baseline_cfg.ignore_parks:
            continue
        park_entities.setdefault(park, []).append(entity)

    log.info(f"Total entities: {len(entity_list)} across {len(park_entities)} parks")

    # Set up challenger output directories
    challenger_forecasts_dir = competition_cfg.get_challenger_forecasts_dir(challenger_name)
    challenger_forecasts_dir.mkdir(parents=True, exist_ok=True)
    challenger_models_dir = competition_cfg.get_challenger_models_dir(challenger_name)
    
    temp_dir = challenger_forecasts_dir / "_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    total_predictions = 0
    total_entities = 0
    failed_entities = 0
    batch_files = []

    # Import park timezone and time grid function from baseline
    from pipeline.core.park_codes import PARK_TIMEZONE

    for park_code in sorted(park_entities.keys()):
        entities = park_entities[park_code]
        with log.timed(f"park {park_code} ({len(entities)} entities)"):
            park_results = []

            # Generate time grid for this park (same as baseline)
            park_tz = PARK_TIMEZONE.get(park_code, "America/New_York")
            time_grid = _generate_park_time_grid(
                start_date, end_date, date_features, park_hours, park_code, park_tz
            )

            if time_grid is None or len(time_grid) == 0:
                log.warning(f"  {park_code}: no time grid generated, skipping")
                continue

            # Pre-compute park hours lookup for mins_since_open (vectorized)
            park_open_mins = {}
            for d in pd.date_range(start_date, end_date).date:
                hours_tuple = park_hours.get((park_code, d))
                if hours_tuple and hours_tuple[0] is not None:
                    park_open_mins[d] = hours_tuple[0]
                else:
                    park_open_mins[d] = 6 * 60  # default

            for entity_code in entities:
                try:
                    result, feat_key = _forecast_challenger_entity(
                        entity_code, park_code, time_grid,
                        challenger_models_dir, challenger_name,
                        agg_by_entity, park_open_mins,
                        fallback_ratios, oc_by_entity,
                        challenger_features
                    )
                    if result is not None and len(result) > 0:
                        park_results.append(result)
                        total_entities += 1
                except Exception as e:
                    log.warning(f"  {entity_code}: failed — {e}")
                    failed_entities += 1

            # Flush this park to temp parquet
            if park_results:
                park_df = pd.concat(park_results, ignore_index=True)
                batch_file = temp_dir / f"{park_code}.parquet"
                park_df.to_parquet(batch_file, index=False)
                batch_files.append(batch_file)
                total_predictions += len(park_df)
                log.info(f"  {park_code}: {len(park_df):,} predictions from {len(park_results)} entities")
                del park_df, park_results  # Release memory

    # Combine all parks into final output
    if batch_files:
        with log.timed("combine park files"):
            chunks = [pd.read_parquet(f) for f in batch_files]
            combined = pd.concat(chunks, ignore_index=True)
            del chunks

            output_file = challenger_forecasts_dir / f"all_forecasts_{challenger_name}.parquet"
            combined.to_parquet(output_file, index=False)

            # Log method breakdown
            method_counts = combined["prediction_method"].value_counts()
            for method, count in method_counts.items():
                log.info(f"  {method}: {count:,}")

            del combined

        # Cleanup temp files
        for f in batch_files:
            f.unlink()
        try:
            temp_dir.rmdir()
        except OSError:
            pass

    log.info("=" * 60)
    log.info(f"CHALLENGER FORECASTING COMPLETE: {challenger_name}")
    log.info(f"Entities processed: {total_entities}, Failed: {failed_entities}")
    log.info(f"Total predictions: {total_predictions:,}")
    log.info(f"Output: {output_file}")
    log.info("=" * 60)

    return {
        "challenger_name": challenger_name,
        "entities_processed": total_entities,
        "failed_entities": failed_entities,
        "total_predictions": total_predictions,
    }


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Generate challenger forecasts")
    parser.add_argument("--challenger", required=True, help="Challenger name (e.g., hypertuned_v1)")
    parser.add_argument("--output-base", type=Path, required=True, help="Pipeline output base directory")
    
    args = parser.parse_args()
    
    try:
        result = run_challenger_forecasting(args.challenger, args.output_base)
        print(f"✅ Challenger forecasting completed: {result}")
        return 0
    except Exception as e:
        print(f"❌ Challenger forecasting failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    # Add repo root to Python path for imports
    repo_root = Path(__file__).parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    
    sys.exit(main())