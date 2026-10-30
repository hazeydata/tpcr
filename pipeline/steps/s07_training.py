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

Training data: matched_pairs/actuals_training_v2/{PARK}.parquet under the write
base (rebuilt by s06b the same run; the shadow dir in shadow mode). Geo-decay
half-life is per park via cfg.get_halflife_days (default 730).

See: docs/PIPELINE_V4_DESIGN.md

=== END V4 DESIGN PRINCIPLE ===
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import xgboost as xgb
except ImportError:
    xgb = None

from pipeline.config import PipelineConfig
from pipeline.core import paths
from pipeline.core.logging import PipelineLogger
from pipeline.core.park_codes import entity_to_park
from pipeline.core.validation import ValidationError


# V4 baseline features — the ONLY features used for production models
BASELINE_FEATURES = [
    "mins_since_6am",
    "mins_since_open",
    "date_group_id_encoded",
    "season_encoded",
    "season_year_encoded",
    "day_of_week",
]


def run(cfg: PipelineConfig, log: PipelineLogger, as_of: date | None = None) -> dict:
    """Train one XGBoost model per entity — V4 pure baseline."""
    as_of = as_of or date.today()

    if xgb is None:
        raise ValidationError("XGBoost is required. pip install xgboost")

    log.info("=" * 60)
    log.info("STEP 7: MODEL TRAINING (baseline, one model per entity)")
    log.info("=" * 60)
    log.info(f"Features: {BASELINE_FEATURES}")

    # Look for per-park training data (preferred) or single file
    actuals_dir = paths.training_data_dir(cfg)
    actuals_single = paths.training_data_combined_path(cfg)

    park_files = sorted(actuals_dir.glob("*.parquet")) if actuals_dir.is_dir() else []
    use_park_chunks = len(park_files) > 0

    if not use_park_chunks and not actuals_single.exists():
        raise ValidationError(
            f"No training data found at {actuals_dir} or {actuals_single}."
        )

    log.info(f"Training data: {'per-park chunks' if use_park_chunks else 'single file'}")

    # Scan entity counts
    with log.timed("scan entity counts"):
        entity_counts = _scan_entity_counts(park_files if use_park_chunks else [actuals_single])

    min_obs = getattr(cfg, "training_min_obs_lite", 100)
    eligible = [e for e, c in entity_counts.items() if c >= min_obs]
    log.info(f"Eligible entities: {len(eligible)} (min_obs={min_obs})")

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
        if park_code in cfg.ignore_parks:
            continue
        park_entities = park_entity_map[park_code]

        with log.timed(f"train park {park_code} ({len(park_entities)} entities)"):
            if use_park_chunks:
                park_file = actuals_dir / f"{park_code}.parquet"
                if not park_file.exists():
                    log.warning(f"  No training data for park {park_code}")
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
                        entity_code, park_df, cfg, min_obs,
                        as_of=as_of, halflife_days=cfg.get_halflife_days(park_code),
                    )
                    if result is not None:
                        successful += 1
                        total_mae += result["mae"]
                    else:
                        failed += 1
                except Exception as e:
                    log.warning(f"  {entity_code}: {e}")
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
    log.info("=" * 60)

    return {
        "rows": successful,
        "successful": successful,
        "failed": failed,
        "avg_mae": round(avg_mae, 2),
    }


def _train_baseline_model(
    entity_code: str,
    park_df: pd.DataFrame,
    cfg: PipelineConfig,
    min_samples: int,
    as_of: date | None = None,
    halflife_days: int | None = None,
) -> dict | None:
    """Train a single baseline XGBoost model for one entity and save it."""
    entity_df = park_df[park_df["entity_code"] == entity_code]
    halflife_days = halflife_days or cfg.geo_decay_halflife_days
    fitted = fit_baseline(entity_df, cfg, min_samples, as_of or date.today(), halflife_days)
    if fitted is None:
        return None
    bst, stats = fitted

    model_dir = cfg.models_dir / entity_code
    model_dir.mkdir(parents=True, exist_ok=True)

    model_path = model_dir / "model_baseline.json"
    bst.save_model(str(model_path))

    metadata = {
        "model_label": "BASELINE",
        "entity_code": entity_code,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_train": stats["n_train"],
        "n_val": stats["n_val"],
        "n_total": stats["n_train"] + stats["n_val"],
        "mae": round(stats["mae"], 3),
        "best_trees": stats["best_trees"],
        "features": BASELINE_FEATURES,
        "uses_geo_decay_weights": True,
        "geo_decay_halflife_days": halflife_days,
        "backend": "Python xgboost",
        "hyperparameters": stats["params"],
    }
    meta_path = model_dir / "metadata_baseline.json"
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2, default=str)

    return {"mae": stats["mae"], "n_samples": stats["n_train"], "best_trees": stats["best_trees"]}


def baseline_matrix(entity_df: pd.DataFrame) -> pd.DataFrame:
    """Rows ready for the baseline features: day_of_week derived, numeric, NaN-free."""
    df = entity_df.reset_index(drop=True).copy()
    if "day_of_week" not in df.columns:
        df["day_of_week"] = pd.to_datetime(df["park_date"]).dt.dayofweek.astype(np.float32)
    for col in BASELINE_FEATURES:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    df["actual_time"] = pd.to_numeric(df["actual_time"], errors="coerce")
    return df


def fit_baseline(
    entity_df: pd.DataFrame,
    cfg: PipelineConfig,
    min_samples: int,
    as_of: date,
    halflife_days: int,
):
    """Fit the baseline model with geo-decay weights relative to as_of. None if too little data."""
    if len(entity_df) < min_samples:
        return None
    if any(f not in entity_df.columns for f in BASELINE_FEATURES if f != "day_of_week"):
        return None
    entity_df = baseline_matrix(entity_df)

    X_all = entity_df[BASELINE_FEATURES].values.astype(np.float32)
    y_all = entity_df["actual_time"].values.astype(np.float32)

    valid_mask = ~np.isnan(y_all) & (y_all > 0) & ~np.any(np.isnan(X_all), axis=1)
    if valid_mask.sum() < min_samples:
        return None

    X = X_all[valid_mask]
    y = y_all[valid_mask]

    valid_dates = pd.to_datetime(entity_df.loc[valid_mask, "park_date"]).dt.date
    days_old = np.array([(as_of - d).days for d in valid_dates], dtype=np.float32)
    weights = np.float32(0.5) ** (days_old / halflife_days)

    if "is_synthetic" in entity_df.columns:
        is_synth = entity_df.loc[valid_mask, "is_synthetic"].values.astype(bool)
        real_weight = getattr(cfg, "real_actual_weight", 10.0)
        synth_weight = getattr(cfg, "synthetic_weight", 1.0)
        weights = weights * np.where(is_synth, synth_weight, real_weight)

    n = len(y)
    split = int(n * 0.85)
    if n - split < 10:
        return None

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

    pred = bst.predict(dval)
    mae = float(np.mean(np.abs(y_val - pred)))
    best_trees = getattr(bst, "best_iteration", cfg.training_rounds)
    return bst, {"n_train": split, "n_val": n - split, "mae": mae,
                 "best_trees": best_trees, "params": params}


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
