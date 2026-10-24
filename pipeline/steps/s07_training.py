"""Step 7: Model Training — V4 Pure Baseline.

=== V4 DESIGN PRINCIPLE: ONE MODEL PER ENTITY ===

The baseline pipeline trains ONE XGBoost model per entity using 6 features:
  - mins_since_6am
  - mins_since_open
  - date_group_id_encoded
  - season_encoded
  - season_year_encoded
  - day_of_week  (promoted from xgb-dow challenger, TPCR #470, 2026-04-22)

No multi-candidate selection. No posted_time features. No lite fallback.
The baseline is the simplest thing that works.

Multi-candidate model competition (actuals_first vs full_feature vs lite)
was removed 2026-03-21. It tripled training time and caused OOM crashes.
It returns later as a shadow-mode feature in the competition framework.

See: docs/PIPELINE_V4_DESIGN.md

=== END V4 DESIGN PRINCIPLE ===
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import xgboost as xgb
except ImportError:
    xgb = None

from pipeline.config import PipelineConfig
from pipeline.core.eligibility import (
    SkipLog,
    check_extinct_on_sanity,
    is_operating_in_window,
    load_dimentity_extinct,
    load_operating_calendar_indexed,
)
from pipeline.core.logging import PipelineLogger
from pipeline.core.park_codes import entity_to_park
from pipeline.core.validation import StageFailed, ValidationError


# V4 baseline features — the ONLY features used for production models
BASELINE_FEATURES = [
    "mins_since_6am",
    "mins_since_open",
    "date_group_id_encoded",
    "season_encoded",
    "season_year_encoded",
    "day_of_week",
]


def run(cfg: PipelineConfig, log: PipelineLogger) -> dict:
    """Train one XGBoost model per entity — V4 pure baseline."""

    if xgb is None:
        raise ValidationError("XGBoost is required. pip install xgboost")

    log.info("=" * 60)
    log.info("STEP 7: MODEL TRAINING (baseline, one model per entity)")
    log.info("=" * 60)
    log.info(f"Features: {BASELINE_FEATURES}")

    # Look for per-park training data (preferred) or single file
    actuals_dir = cfg.output_base / "matched_pairs" / "actuals_training_v2"
    actuals_single = cfg.output_base / "matched_pairs" / "actuals_training_v2.parquet"

    park_files = sorted(actuals_dir.glob("*.parquet")) if actuals_dir.is_dir() else []
    use_park_chunks = len(park_files) > 0

    if not use_park_chunks and not actuals_single.exists():
        raise ValidationError(
            f"No training data found at {actuals_dir} or {actuals_single}."
        )

    log.info(f"Training data: {'per-park chunks' if use_park_chunks else 'single file'}")

    run_date = date.today()
    window_end = run_date + timedelta(days=cfg.forecast_days)
    extinct_map = load_dimentity_extinct(cfg.dimension_dir / "dimentity.csv")
    check_extinct_on_sanity(extinct_map)
    oc_by_entity = load_operating_calendar_indexed(cfg, log)
    skips = SkipLog("s07", cfg, log, run_date=run_date)

    # Scan entity counts AFTER the per-park min_training_year cutoff
    with log.timed("scan entity counts"):
        entity_counts = _scan_post_cutoff_counts(
            park_files if use_park_chunks else [actuals_single], cfg
        )

    min_obs = getattr(cfg, "training_min_obs_lite", 100)

    # Ignored parks (water parks) are out of scope: excluded before eligibility
    in_scope = {e: c for e, c in entity_counts.items() if entity_to_park(e) not in cfg.ignore_parks}
    log.info(f"Ignored-park entities excluded: {len(entity_counts) - len(in_scope)}")

    eligible = []
    for e in sorted(in_scope):
        extinct_on = extinct_map.get(e)
        if extinct_on is not None and extinct_on <= run_date:
            skips.record("s07", e, "extinct")
        elif not is_operating_in_window(e, oc_by_entity, run_date, window_end):
            skips.record("s07", e, "not_operating")
        elif in_scope[e] < min_obs:
            skips.record("s07", e, "below_min_obs")
        else:
            eligible.append(e)
    pre_skips = skips.total()
    log.info(f"Eligible entities: {len(eligible)} (min_obs={min_obs}, pre-eligibility skips={pre_skips})")

    # Group by park
    park_entity_map: dict[str, list[str]] = {}
    for e in eligible:
        park = entity_to_park(e)
        park_entity_map.setdefault(park, []).append(e)

    # Train one baseline model per entity
    successful = 0
    failed = 0
    total_mae = 0.0

    for park_code in sorted(park_entity_map.keys()):
        park_entities = park_entity_map[park_code]

        with log.timed(f"train park {park_code} ({len(park_entities)} entities)"):
            if use_park_chunks:
                park_file = actuals_dir / f"{park_code}.parquet"
                if not park_file.exists():
                    log.warning(f"  No training data for park {park_code}")
                    for entity_code in park_entities:
                        skips.record("s07", entity_code, "no_training_data")
                    continue
                park_df = pd.read_parquet(park_file)
            else:
                park_df = pd.read_parquet(actuals_single)
                park_df = park_df[park_df["entity_code"].isin(park_entities)]

            # Per-park min_training_year cutoff (Issue #48)
            min_year = cfg.get_min_training_year(park_code)
            if min_year > 0 and "park_date" in park_df.columns:
                before = len(park_df)
                park_df = park_df[pd.to_datetime(park_df["park_date"]).dt.year >= min_year]
                dropped = before - len(park_df)
                if dropped > 0:
                    log.info(f"  {park_code}: min_training_year={min_year} dropped {dropped:,} rows ({dropped/before*100:.1f}%)")

            for entity_code in park_entities:
                try:
                    result = _train_baseline_model(
                        entity_code, park_df, cfg, min_obs
                    )
                    if isinstance(result, str):
                        skips.record("s07", entity_code, result)
                    else:
                        successful += 1
                        total_mae += result["mae"]
                except Exception as e:
                    log.warning(f"  {entity_code}: {e}")
                    skips.record("s07", entity_code, f"error:{type(e).__name__}")
                    failed += 1

            del park_df

    avg_mae = total_mae / successful if successful > 0 else 0

    log.info("=" * 60)
    log.info("TRAINING COMPLETE (baseline)")
    log.info(f"Successful: {successful}, Failed: {failed}")
    log.info(f"Average MAE: {avg_mae:.2f} min")
    log.metric("training_successful", successful)
    log.metric("training_failed", failed)
    log.metric("training_avg_mae", round(avg_mae, 2))
    log.metric("training_eligible", len(eligible))
    skipped = skips.summary()
    log.info("=" * 60)

    post_skips = skips.total() - pre_skips
    if post_skips != len(eligible) - successful:
        raise ValidationError(
            f"S7 post-eligibility skips ({post_skips}) != eligible ({len(eligible)}) - trained ({successful})"
        )
    if failed > 0:
        raise StageFailed(f"S7: {failed} entities failed training with errors (see skips_s07 log)")

    return {
        "rows": successful,
        "successful": successful,
        "failed": failed,
        "eligible": len(eligible),
        "skipped": skipped,
        "avg_mae": round(avg_mae, 2),
    }


def _train_baseline_model(
    entity_code: str,
    park_df: pd.DataFrame,
    cfg: PipelineConfig,
    min_samples: int,
) -> dict | str:
    """Train a single baseline XGBoost model for one entity.

    Returns the result dict, or a skip-reason string (no_training_data / below_min_obs).
    """

    entity_df = park_df[park_df["entity_code"] == entity_code].copy()
    entity_df = entity_df.reset_index(drop=True)  # CRITICAL: align index with position

    if len(entity_df) == 0:
        return "no_training_data"
    if len(entity_df) < min_samples:
        return "below_min_obs"

    # Compute derived features if requested but not in data
    if "day_of_week" in BASELINE_FEATURES and "day_of_week" not in entity_df.columns:
        entity_df["day_of_week"] = pd.to_datetime(entity_df["park_date"]).dt.dayofweek.astype(np.float32)

    # Validate features exist
    missing = [f for f in BASELINE_FEATURES if f not in entity_df.columns]
    if missing:
        return "no_training_data"

    # Clean numeric columns
    for col in BASELINE_FEATURES:
        entity_df[col] = pd.to_numeric(entity_df[col], errors="coerce").fillna(0)
    entity_df["actual_time"] = pd.to_numeric(entity_df["actual_time"], errors="coerce")

    # Build arrays
    X_all = entity_df[BASELINE_FEATURES].values.astype(np.float32)
    y_all = entity_df["actual_time"].values.astype(np.float32)

    # Filter invalid rows
    valid_mask = ~np.isnan(y_all) & (y_all > 0) & ~np.any(np.isnan(X_all), axis=1)
    if valid_mask.sum() == 0:
        return "no_training_data"
    if valid_mask.sum() < min_samples:
        return "below_min_obs"

    # Apply mask to get clean arrays
    X = X_all[valid_mask]
    y = y_all[valid_mask]

    # Geo-decay weights (using the valid rows from the reset-indexed DataFrame)
    today = date.today()
    valid_dates = pd.to_datetime(entity_df.loc[valid_mask, "park_date"]).dt.date
    days_old = np.array([(today - d).days for d in valid_dates], dtype=np.float32)
    weights = np.float32(0.5) ** (days_old / cfg.geo_decay_halflife_days)

    # Synthetic weighting: real actuals get higher weight
    if "is_synthetic" in entity_df.columns:
        is_synth = entity_df.loc[valid_mask, "is_synthetic"].values.astype(bool)
        real_weight = getattr(cfg, "real_actual_weight", 10.0)
        synth_weight = getattr(cfg, "synthetic_weight", 1.0)
        weights = weights * np.where(is_synth, synth_weight, real_weight)

    # Train/val split (85/15)
    n = len(y)
    split = int(n * 0.85)
    if n - split < 10:
        return "below_min_obs"

    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]
    w_train = weights[:split]

    dtrain = xgb.DMatrix(X_train, label=y_train, weight=w_train)
    dval = xgb.DMatrix(X_val, label=y_val)

    params = {
        "max_depth": cfg.training_max_depth,
        "eta": cfg.training_eta,
        "min_child_weight": 1,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "objective": "reg:squarederror",
        "seed": 42,
        "verbosity": 0,
    }

    bst = xgb.train(
        params, dtrain,
        num_boost_round=cfg.training_rounds,
        evals=[(dtrain, "train"), (dval, "eval")],
        early_stopping_rounds=cfg.training_early_stopping,
        verbose_eval=False,
    )

    # Evaluate
    pred = bst.predict(dval)
    mae = float(np.mean(np.abs(y_val - pred)))
    best_trees = getattr(bst, "best_iteration", cfg.training_rounds)

    # Save model
    model_dir = cfg.models_dir / entity_code
    model_dir.mkdir(parents=True, exist_ok=True)

    model_path = model_dir / "model_baseline.json"
    bst.save_model(str(model_path))

    metadata = {
        "model_label": "BASELINE",
        "entity_code": entity_code,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_train": split,
        "n_val": n - split,
        "n_total": n,
        "mae": round(mae, 3),
        "best_trees": best_trees,
        "features": BASELINE_FEATURES,
        "uses_geo_decay_weights": True,
        "geo_decay_halflife_days": cfg.geo_decay_halflife_days,
        "backend": "Python xgboost",
        "hyperparameters": params,
    }
    meta_path = model_dir / "metadata_baseline.json"
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2, default=str)

    return {"mae": mae, "n_samples": split, "best_trees": best_trees}


def _scan_entity_counts(parquet_files: list[Path]) -> dict[str, int]:
    """Count observations per entity across parquet files."""
    counts: dict[str, int] = {}
    for path in parquet_files:
        if not path.exists():
            continue
        df = pd.read_parquet(path, columns=["entity_code"])
        for entity, count in df["entity_code"].value_counts().items():
            counts[entity] = counts.get(entity, 0) + count
    return counts


def _scan_post_cutoff_counts(parquet_files: list[Path], cfg: PipelineConfig) -> dict[str, int]:
    """Count rows per entity AFTER the per-park min_training_year cutoff.

    Every scanned entity is a key (count 0 if the cutoff removed all its rows).
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    counts: dict[str, int] = {}
    for path in parquet_files:
        if not path.exists():
            continue
        table = pq.read_table(path, columns=["entity_code", "park_date"])
        pd_col = table.column("park_date")
        if not (pa.types.is_date(pd_col.type) or pa.types.is_timestamp(pd_col.type)):
            pd_col = pc.cast(pd_col, "timestamp[ns]")
        years = pc.year(pd_col).to_numpy(zero_copy_only=False)
        enc = pc.dictionary_encode(table.column("entity_code")).combine_chunks()
        uniq = enc.dictionary.to_pylist()
        idx = enc.indices.to_numpy(zero_copy_only=False)
        cut = np.array([cfg.get_min_training_year(entity_to_park(e)) for e in uniq])
        kept = np.bincount(idx[years >= cut[idx]], minlength=len(uniq))
        for entity, count in zip(uniq, kept):
            counts[entity] = counts.get(entity, 0) + int(count)
    return counts
