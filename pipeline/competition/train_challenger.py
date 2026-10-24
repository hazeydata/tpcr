"""Train Challenger Models.

Trains a specific challenger using the same input data as baseline but with
challenger-specific hyperparameters and weighting methods.

Usage:
    python -m pipeline.competition.train_challenger --challenger hypertuned_v1 --output-base ~/hazeydata/pipeline
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timezone
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
from pipeline.competition.eligibility import eligibility_report


# Import baseline features from s07_training
from pipeline.steps.s07_training import BASELINE_FEATURES


def apply_inverse_freq_weighting(weights: np.ndarray, is_synth: np.ndarray) -> np.ndarray:
    """Apply inverse frequency weighting for synthetic actuals.
    
    Formula: weight = 1.0 / log2(n_real + 1) for synthetic, 1.0 for real
    Where n_real is the number of real actuals in the dataset.
    """
    n_real = np.sum(~is_synth)
    inverse_freq_weight = 1.0 / math.log2(n_real + 1) if n_real > 0 else 1.0
    
    # Apply weighting: real actuals get 1.0, synthetic get inverse_freq
    synthetic_weights = weights * np.where(is_synth, inverse_freq_weight, 1.0)
    return synthetic_weights


def train_challenger_model(
    entity_code: str,
    park_df: pd.DataFrame,
    challenger,
    baseline_cfg: PipelineConfig,
    competition_cfg,
    min_samples: int,
) -> dict | None:
    """Train a single challenger model for one entity.
    
    Based on baseline _train_baseline_model but with challenger parameters.
    """
    
    entity_df = park_df[park_df["entity_code"] == entity_code].copy()
    entity_df = entity_df.reset_index(drop=True)  # CRITICAL: align index with position

    if len(entity_df) < min_samples:
        return None

    # Use baseline features (challenger doesn't add new features)
    features = BASELINE_FEATURES if challenger.features is None else challenger.features
    
    # Compute derived features if requested but not in data
    if "day_of_week" in features and "day_of_week" not in entity_df.columns:
        entity_df["day_of_week"] = pd.to_datetime(entity_df["park_date"]).dt.dayofweek.astype(np.float32)

    # Validate features exist
    missing = [f for f in features if f not in entity_df.columns]
    if missing:
        return None

    # Clean numeric columns
    for col in features:
        entity_df[col] = pd.to_numeric(entity_df[col], errors="coerce").fillna(0)
    entity_df["actual_time"] = pd.to_numeric(entity_df["actual_time"], errors="coerce")

    # Build arrays
    X_all = entity_df[features].values.astype(np.float32)
    y_all = entity_df["actual_time"].values.astype(np.float32)

    # Filter invalid rows
    valid_mask = ~np.isnan(y_all) & (y_all > 0) & ~np.any(np.isnan(X_all), axis=1)
    if valid_mask.sum() < min_samples:
        return None

    # Apply mask to get clean arrays
    X = X_all[valid_mask]
    y = y_all[valid_mask]

    # Geo-decay weights (using the valid rows from the reset-indexed DataFrame)
    today = date.today()
    valid_dates = pd.to_datetime(entity_df.loc[valid_mask, "park_date"]).dt.date
    days_old = np.array([(today - d).days for d in valid_dates], dtype=np.float32)
    geo_decay_halflife = getattr(challenger, 'geo_decay_halflife', baseline_cfg.geo_decay_halflife_days)
    weights = np.float32(0.5) ** (days_old / geo_decay_halflife)

    # Apply challenger-specific weighting
    if "is_synthetic" in entity_df.columns:
        is_synth = entity_df.loc[valid_mask, "is_synthetic"].values.astype(bool)
        
        if challenger.has_weighting_changes():
            weighting_method = challenger.get_weighting_method()
            if weighting_method == "inverse_freq":
                weights = apply_inverse_freq_weighting(weights, is_synth)
            else:
                # Default to baseline weighting if unknown method
                real_weight = getattr(baseline_cfg, "real_actual_weight", 10.0)
                synth_weight = getattr(baseline_cfg, "synthetic_weight", 1.0)
                weights = weights * np.where(is_synth, synth_weight, real_weight)
        else:
            # Use baseline weighting
            real_weight = getattr(baseline_cfg, "real_actual_weight", 10.0)
            synth_weight = getattr(baseline_cfg, "synthetic_weight", 1.0)
            weights = weights * np.where(is_synth, synth_weight, real_weight)

    # Train/val split (85/15)
    n = len(y)
    split = int(n * 0.85)
    if n - split < 10:
        return None

    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]
    w_train = weights[:split]

    dtrain = xgb.DMatrix(X_train, label=y_train, weight=w_train)
    dval = xgb.DMatrix(X_val, label=y_val)

    # Build parameters using challenger hyperparameters
    params = {
        "max_depth": challenger.get_hyperparam("max_depth", baseline_cfg.training_max_depth),
        "eta": challenger.get_hyperparam("eta", baseline_cfg.training_eta),
        "min_child_weight": challenger.get_hyperparam("min_child_weight", 1),
        "subsample": challenger.get_hyperparam("subsample", 0.8),
        "colsample_bytree": challenger.get_hyperparam("colsample_bytree", 0.8),
        "objective": "reg:squarederror",
        "seed": 42,
        "verbosity": 0,
    }

    # Training rounds and early stopping
    n_estimators = challenger.get_hyperparam("n_estimators", baseline_cfg.training_rounds)
    early_stopping = challenger.get_hyperparam("early_stopping", baseline_cfg.training_early_stopping)

    bst = xgb.train(
        params, dtrain,
        num_boost_round=n_estimators,
        evals=[(dtrain, "train"), (dval, "eval")],
        early_stopping_rounds=early_stopping,
        verbose_eval=False,
    )

    # Evaluate
    pred = bst.predict(dval)
    mae = float(np.mean(np.abs(y_val - pred)))
    best_trees = getattr(bst, "best_iteration", n_estimators)

    # Save model to challenger-specific directory
    model_dir = competition_cfg.get_challenger_models_dir(challenger.name) / entity_code
    model_dir.mkdir(parents=True, exist_ok=True)

    model_path = model_dir / f"model_{challenger.name}.json"
    bst.save_model(str(model_path))

    metadata = {
        "model_label": challenger.name.upper(),
        "entity_code": entity_code,
        "challenger_name": challenger.name,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_train": split,
        "n_val": n - split,
        "n_total": n,
        "mae": round(mae, 3),
        "best_trees": best_trees,
        "features": features,
        "uses_geo_decay_weights": True,
        "geo_decay_halflife_days": geo_decay_halflife,
        "weighting_method": challenger.get_weighting_method() if challenger.has_weighting_changes() else "baseline",
        "backend": "Python xgboost",
        "hyperparameters": params,
        "challenger_hyperparams": challenger.hyperparams,
        "challenger_description": challenger.description,
    }
    meta_path = model_dir / f"metadata_{challenger.name}.json"
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2, default=str)

    return {"mae": mae, "n_samples": split, "best_trees": best_trees}


def run_challenger_training(challenger_name: str, output_base: Path,
                            run_date: date | None = None) -> dict:
    """Train a challenger on every entity production S7 would train for run_date.

    No subsetting: the entity set is production_eligible_entities (after the
    extinct exclusion). The result records trained_at (UTC ISO), entity_count
    (models actually trained), trained_entities (their codes), eligible_count and skipped {entity: reason}.
    """
    run_date = run_date or date.today()

    if xgb is None:
        raise ValidationError("XGBoost is required. pip install xgboost")
    
    # Load configurations
    baseline_cfg = PipelineConfig(output_base=output_base)
    competition_cfg = load_competition_config(output_base)
    
    # Set up logging
    log = PipelineLogger(f'train_challenger_{challenger_name}', competition_cfg.logs_dir)
    
    log.info("=" * 60)
    log.info(f"TRAINING CHALLENGER: {challenger_name}")
    log.info("=" * 60)
    
    # Load challenger registry
    challengers_dir = Path(__file__).parent / "challengers"
    registry = load_registry(challengers_dir)
    challenger = registry.get_challenger(challenger_name)
    
    log.info(f"Challenger: {challenger.description}")
    log.info(f"Features: {BASELINE_FEATURES if challenger.features is None else challenger.features}")
    log.info(f"Hyperparams: {challenger.hyperparams}")
    if challenger.has_weighting_changes():
        log.info(f"Weighting: {challenger.weighting}")
    
    # Look for training data (same paths as baseline)
    actuals_dir = baseline_cfg.output_base / "matched_pairs" / "actuals_training_v2"
    actuals_single = baseline_cfg.output_base / "matched_pairs" / "actuals_training_v2.parquet"

    park_files = sorted(actuals_dir.glob("*.parquet")) if actuals_dir.is_dir() else []
    use_park_chunks = len(park_files) > 0

    if not use_park_chunks and not actuals_single.exists():
        raise ValidationError(
            f"No training data found at {actuals_dir} or {actuals_single}."
        )

    log.info(f"Training data: {'per-park chunks' if use_park_chunks else 'single file'}")
    
    # Import entity counting from baseline
    from pipeline.steps.s07_training import _scan_entity_counts
    entity_counts = _scan_entity_counts(park_files if use_park_chunks else [actuals_single])

    min_obs = getattr(baseline_cfg, "training_min_obs_lite", 100)
    report = eligibility_report(output_base, run_date, cfg=baseline_cfg, entity_counts=entity_counts)
    eligible = report["eligible"]
    skipped: dict[str, str] = {}
    extinct = sorted(e for e, r in report["excluded"].items() if r == "extinct")
    log.info(f"Eligible entities: {len(eligible)} (min_obs={min_obs}, run_date={run_date}, "
             f"extinct excluded={len(extinct)})")

    # Group by park
    park_entity_map: dict[str, list[str]] = {}
    for e in eligible:
        park = entity_to_park(e)
        park_entity_map.setdefault(park, []).append(e)

    # Train challenger models
    successful = 0
    failed = 0
    total_mae = 0.0
    trained: list[str] = []

    for park_code in sorted(park_entity_map.keys()):
        park_entities = park_entity_map[park_code]

        with log.timed(f"train park {park_code} ({len(park_entities)} entities)"):
            if use_park_chunks:
                park_file = actuals_dir / f"{park_code}.parquet"
                if not park_file.exists():
                    log.warning(f"  No training data for park {park_code}")
                    for e in park_entities:
                        skipped[e] = "no_park_data"
                    continue
                park_df = pd.read_parquet(park_file)
            else:
                park_df = pd.read_parquet(actuals_single)
                park_df = park_df[park_df["entity_code"].isin(park_entities)]

            # Per-park min_training_year cutoff (same as baseline)
            min_year = baseline_cfg.get_min_training_year(park_code)
            if min_year > 0 and "park_date" in park_df.columns:
                before = len(park_df)
                park_df = park_df[pd.to_datetime(park_df["park_date"]).dt.year >= min_year]
                dropped = before - len(park_df)
                if dropped > 0:
                    log.info(f"  {park_code}: min_training_year={min_year} dropped {dropped:,} rows ({dropped/before*100:.1f}%)")

            for entity_code in park_entities:
                try:
                    result = train_challenger_model(
                        entity_code, park_df, challenger, baseline_cfg, competition_cfg, min_obs
                    )
                    if result is not None:
                        successful += 1
                        trained.append(entity_code)
                        total_mae += result["mae"]
                    else:
                        failed += 1
                        skipped[entity_code] = "not_trained"
                except Exception as e:
                    log.warning(f"  {entity_code}: {e}")
                    failed += 1
                    skipped[entity_code] = f"error:{type(e).__name__}"

            del park_df

    avg_mae = total_mae / successful if successful > 0 else 0

    log.info("=" * 60)
    log.info(f"CHALLENGER TRAINING COMPLETE: {challenger_name}")
    log.info(f"Successful: {successful}, Failed: {failed}")
    log.info(f"Average MAE: {avg_mae:.2f} min")
    log.info("=" * 60)

    for e, reason in sorted(skipped.items()):
        log.warning(f"  skipped {e}: {reason}")

    return {
        "challenger_name": challenger_name,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "entity_count": successful,
        "trained_entities": sorted(trained),
        "eligible_count": len(eligible),
        "skipped": skipped,
        "successful": successful,
        "failed": failed,
        "avg_mae": round(avg_mae, 2),
    }


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Train challenger models")
    parser.add_argument("--challenger", required=True, help="Challenger name (e.g., hypertuned_v1)")
    parser.add_argument("--output-base", type=Path, required=True, help="Pipeline output base directory")
    
    parser.add_argument("--run-date", type=date.fromisoformat, default=None,
                        help="Run date for the extinct exclusion (default: today)")

    args = parser.parse_args()

    try:
        result = run_challenger_training(args.challenger, args.output_base, args.run_date)
        print(f"✅ Challenger training completed: {result['entity_count']}/{result['eligible_count']} entities")
        print("RESULT:" + json.dumps(result))
        return 0
    except Exception as e:
        print(f"❌ Challenger training failed: {e}")
        return 1


if __name__ == "__main__":
    # Add repo root to Python path for imports
    repo_root = Path(__file__).parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    
    sys.exit(main())