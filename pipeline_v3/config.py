"""Pipeline v3 Configuration — Single source of truth.

All configuration lives here. No scattered constants, no env var surprises.
Override via CLI args to pipeline.py, not by editing this file in production.

When output_base is not passed, uses project config (config/config.json) or
src.utils.paths.get_output_base() for local/dev runs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_output_base() -> Path:
    """Use project output_base when available (config.json or get_output_base)."""
    try:
        from src.utils.paths import get_output_base
        return get_output_base()
    except ImportError:
        pass
    root = Path(__file__).resolve().parent.parent
    cfg_path = root / "config" / "config.json"
    if cfg_path.exists():
        import json
        try:
            with open(cfg_path, encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("output_base") if isinstance(data, dict) else None
            if raw and isinstance(raw, str) and raw.strip():
                return Path(raw.strip()).resolve()
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return Path("/home/wilma/hazeydata/pipeline")


@dataclass
class PipelineConfig:
    """All pipeline configuration in one place."""

    # === Paths ===
    output_base: Path = field(default_factory=_default_output_base)
    raw_data_dir: Path = field(default=None)  # set in __post_init__
    fact_tables_dir: Path = field(default=None)
    parquet_dir: Path = field(default=None)
    dimension_dir: Path = field(default=None)
    models_dir: Path = field(default=None)
    forecast_dir: Path = field(default=None)
    wti_dir: Path = field(default=None)
    accuracy_dir: Path = field(default=None)
    logs_dir: Path = field(default=None)
    state_dir: Path = field(default=None)
    duckdb_path: Path = field(default=None)

    # === Shadow mode ===
    shadow: bool = False
    shadow_output_base: Path = field(default=None)

    # === Forecast ===
    forecast_days: int = 365
    forecast_workers: int = 1  # Sequential by default. Memory-safe.

    # === Training ===
    training_min_obs: int = 500
    training_min_obs_lite: int = 100
    training_max_depth: int = 10
    training_eta: float = 0.1
    training_rounds: int = 2000
    training_early_stopping: int = 20
    geo_decay_halflife_days: int = 730

    # === WTI ===
    real_actual_weight: float = 3.5  # TODO: switch to inverse_freq when validated
    synthetic_weight: float = 1.0
    wti_min_wait: float = 5.0  # Floor for WTI values
    exclude_fallback_ratio: bool = True
    quantile_mapping: bool = True
    quantile_mapping_max_stretch: float = 1.5  # Guardrail: max 50% stretch

    # === Conversion model ===
    conversion_retrain_day: int = 0  # 0=Monday
    conversion_holdout_fraction: float = 0.15
    conversion_max_mae_regression: float = 1.0  # Only deploy if MAE doesn't worsen by >1 min

    # === Accuracy ===
    report_mape: bool = False  # MAPE is broken for near-zero actuals. Use MAE.

    # === Infrastructure ===
    memory_limit_gb: float = 50.0  # Stay under this on a 62GB box
    duckdb_read_only: bool = True  # All queries read-only; single write step at end

    # === Parks ===
    parks: list[str] = field(default_factory=lambda: [
        "MK", "EP", "HS", "AK",  # WDW
        "DL", "CA",               # Disneyland Resort
        "UF", "IA", "EU",         # Universal Orlando
        "UH",                     # Universal Hollywood
        "TDL", "TDS",             # Tokyo
    ])
    ignore_parks: list[str] = field(default_factory=lambda: ["BB"])  # Blizzard Beach

    def __post_init__(self):
        """Derive paths from output_base. In shadow mode: read from production, write to shadow."""
        prod = self.output_base
        if self.shadow:
            shadow_base = self.shadow_output_base or (prod.parent / "pipeline_v3_shadow")
            self.shadow_output_base = shadow_base
            write_base = shadow_base
        else:
            write_base = prod

        self.raw_data_dir = self.raw_data_dir or prod / "raw"
        self.fact_tables_dir = self.fact_tables_dir or prod / "fact_tables"
        self.parquet_dir = self.parquet_dir or prod / "fact_tables" / "parquet"
        self.dimension_dir = self.dimension_dir or prod / "dimension_tables"
        self.models_dir = self.models_dir or write_base / "models"
        self.forecast_dir = self.forecast_dir or write_base / "curves" / "forecast_parquet"
        self.wti_dir = self.wti_dir or write_base / "wti"
        self.accuracy_dir = self.accuracy_dir or write_base / "accuracy"
        self.logs_dir = self.logs_dir or write_base / "logs"
        self.state_dir = self.state_dir or write_base / "state"
        self.duckdb_path = self.duckdb_path or write_base / "tpcr_live.duckdb"

        # Shadow mode: s09 reads production forecast when running --step s09_wti alone
        self._production_forecast_dir = prod / "curves" / "forecast_parquet"


def load_config(**overrides) -> PipelineConfig:
    """Create config with optional overrides."""
    return PipelineConfig(**overrides)
