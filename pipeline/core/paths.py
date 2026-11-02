"""Path utilities — resolve all file locations from config.

Every file the pipeline reads or writes is defined here.
No hardcoded paths in step modules.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.config import PipelineConfig


def entity_model_dir(cfg: PipelineConfig, entity_code: str) -> Path:
    """Path to a specific entity's model directory."""
    return cfg.models_dir / entity_code


def entity_model_path(cfg: PipelineConfig, entity_code: str) -> Path:
    """Path to entity's trained XGBoost model file."""
    return cfg.models_dir / entity_code / "model_v3.json"


def entity_metadata_path(cfg: PipelineConfig, entity_code: str) -> Path:
    """Path to entity's model metadata."""
    return cfg.models_dir / entity_code / "metadata_v3.json"


def conversion_model_path(cfg: PipelineConfig) -> Path:
    """Path to the production POSTED→ACTUAL conversion model (read-only in shadow)."""
    return cfg.prod_output_base / "models" / "_conversion" / "model_v3.json"


def conversion_model_backup_path(cfg: PipelineConfig) -> Path:
    """Path to the previous production conversion model (rollback)."""
    return cfg.prod_output_base / "models" / "_conversion" / "model_v3_previous.json"


def forecast_output_path(cfg: PipelineConfig) -> Path:
    """Path to the combined forecast parquet."""
    return cfg.forecast_dir / "all_forecasts_v3.parquet"


def wti_output_path(cfg: PipelineConfig) -> Path:
    """Path to the WTI parquet."""
    return cfg.wti_dir / "wti_v3.parquet"


def log_events_path(cfg: PipelineConfig, run_date: str) -> Path:
    """Path to structured log events for a run."""
    return cfg.logs_dir / f"v3_events_{run_date}.jsonl"


def shadow_comparison_path(cfg: PipelineConfig, run_date: str) -> Path:
    """Path to shadow comparison report."""
    return cfg.accuracy_dir / f"v3_shadow_comparison_{run_date}.json"


def prefer_written(cfg: PipelineConfig, relpath: str) -> Path:
    """The write-base copy of a file if this run (or a prior shadow run) wrote one, else production."""
    written = cfg.write_base / relpath
    if written.exists():
        return written
    return cfg.prod_output_base / relpath


def training_data_dir(cfg: PipelineConfig) -> Path:
    """Per-park actuals training parquets written by s06b and read by s07."""
    return cfg.write_base / "matched_pairs" / "actuals_training_v2"


def training_data_combined_path(cfg: PipelineConfig) -> Path:
    return cfg.write_base / "matched_pairs" / "actuals_training_v2.parquet"


def synthetic_actuals_dir(cfg: PipelineConfig) -> Path:
    """Synthetic actuals from s06 (shadow copy if s06 wrote one, else production)."""
    return prefer_written(cfg, "synthetic_actuals")


ENCODING_MAPPINGS = "state/encoding_mappings.json"
OPERATING_CALENDAR = "operating_calendar/operating_calendar.parquet"
DIMENTITY = "dimension_tables/dimentity.csv"
RAW_CLOSURES = "raw_closures"


def encoding_mappings_write_path(cfg: PipelineConfig) -> Path:
    return cfg.write_base / ENCODING_MAPPINGS


def encoding_mappings_path(cfg: PipelineConfig) -> Path:
    return prefer_written(cfg, ENCODING_MAPPINGS)


def operating_calendar_path(cfg: PipelineConfig) -> Path:
    return prefer_written(cfg, OPERATING_CALENDAR)


def dimentity_path(cfg: PipelineConfig) -> Path:
    written = cfg.write_base / DIMENTITY
    if written.exists():
        return written
    return cfg.dimension_dir / "dimentity.csv"


def step_receipt_path(cfg: PipelineConfig, step: str, run_date: str) -> Path:
    return cfg.logs_dir / f"receipt_{step}_{run_date}.json"
