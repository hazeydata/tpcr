"""Pipeline v3 Configuration — Single source of truth.

All configuration lives here. No scattered constants, no env var surprises.
Override via CLI args to pipeline.py, not by editing this file in production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PipelineConfig:
    """All pipeline configuration in one place."""

    # === Paths ===
    output_base: Path = Path("/home/wilma/hazeydata/pipeline")
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
    prod_output_base: Path = field(default=None)  # Preserved for shadow reads

    # === Sync ===
    s01_sync_timeout_s: int = field(default_factory=lambda: int(os.environ.get("S01_SYNC_TIMEOUT_S", "1800")))
    s3_bucket: str = "touringplans_stats"
    s3_sync_sources: list[str] = field(default_factory=lambda: ["wait_times", "fastpass_times"])

    # === Forecast ===
    # v3.0: 365 days (reduced from 730 to save memory during OOM era)
    # v4.1: restored to 730 days — per-park chunking in s06 solved memory issues
    #        (peak 11GB vs 50GB+), plenty of headroom on 64GB server.
    #        Real user demand: teachers planning 2027 trips need 730-day forecasts.
    forecast_days: int = 730
    forecast_workers: int = 1  # Sequential by default. Memory-safe.

    # === Training ===
    training_min_obs: int = 500
    training_min_obs_lite: int = 100
    training_max_depth: int = 10
    training_eta: float = 0.1
    training_rounds: int = 2000
    training_early_stopping: int = 20
    geo_decay_halflife_days: int = 730
    min_training_year: int = 0  # Global default: no cutoff
    min_training_year_per_park: dict = field(default_factory=lambda: {
        # 2014-2015 Disney actual-time API data contamination: high-frequency
        # sampling (45 obs/entity/day vs ~2 modern) with poor-quality actuals
        # overwhelms geo-decay weighting. See Issue #48.
        "AK": 2016,
        "MK": 2016,
        "EP": 2016,
        "HS": 2016,
    })

    # === WTI ===
    # VALIDATED: Higher real weighting improves accuracy (2026-03-06 experiment)
    # inverse_freq MAE=6.96 (best), uniform_20 MAE=6.99, uniform_3.5 MAE=7.04
    # Using 10.0x as practical compromise: simpler than full inverse_freq, most of the benefit
    real_actual_weight: float = 10.0  # Increased from 3.5 based on weighting experiment
    synthetic_weight: float = 1.0
    wti_min_wait: float = 5.0  # Floor for WTI values
    exclude_fallback_ratio: bool = True
    quantile_mapping: bool = True
    quantile_mapping_max_stretch: float = 1.5  # Global default guardrail
    quantile_mapping_per_park_stretch: dict = field(default_factory=lambda: {
        # Parks with real high-variance seasonality need more stretch
        "TDL": 2.0,   # Golden Week, New Year's — genuine extreme peaks
        "TDS": 2.0,   # Same seasonal patterns as TDL
        # Parks where overprediction is a known problem need tighter caps
        "IA": 1.2,    # Persistent overprediction (+17.1 bias on 2026-03-07)
        "CA": 1.3,    # Had +34.3 error on Feb 26 from uncapped mapping
        "EU": 1.3,    # Persistent overprediction (+15.1 bias on 2026-03-07)
        # Everything else uses the global default (1.5)
    })

    # === Conversion model ===
    # v2: daily refresh (v1 was weekly Monday-only via conversion_retrain_day)
    conversion_retrain_daily: bool = True  # Retrain conversion model every run
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
    ignore_parks: list[str] = field(default_factory=lambda: ["BB", "TL", "VB"])  # Water parks

    def get_min_training_year(self, park_code: str) -> int:
        """Get minimum training year for a park (0 = no cutoff)."""
        return self.min_training_year_per_park.get(
            park_code, self.min_training_year
        )

    def get_park_stretch(self, park_code: str) -> float:
        """Get quantile mapping stretch factor for a park."""
        return self.quantile_mapping_per_park_stretch.get(
            park_code, self.quantile_mapping_max_stretch
        )

    def __post_init__(self):
        """Derive paths from output_base.

        Shadow mode: read from production, write to shadow dir.
        """
        prod_base = self.output_base
        self.prod_output_base = prod_base

        if self.shadow:
            shadow_base = self.shadow_output_base or (prod_base.parent / "pipeline_shadow")
            self.shadow_output_base = shadow_base
        else:
            shadow_base = None

        # Read paths always use production
        base = prod_base
        self.raw_data_dir = self.raw_data_dir or base / "raw"
        self.fact_tables_dir = self.fact_tables_dir or base / "fact_tables"
        self.parquet_dir = self.parquet_dir or base / "fact_tables" / "parquet"
        self.dimension_dir = self.dimension_dir or base / "dimension_tables"
        self.models_dir = self.models_dir or base / "models"
        self.forecast_dir = self.forecast_dir or base / "curves" / "forecast_parquet"

        # Write paths use shadow in shadow mode
        write_base = shadow_base if self.shadow else base
        self.wti_dir = self.wti_dir or write_base / "wti"
        self.accuracy_dir = self.accuracy_dir or write_base / "accuracy"
        self.logs_dir = self.logs_dir or write_base / "logs"
        self.state_dir = self.state_dir or write_base / "state"
        self.duckdb_path = self.duckdb_path or write_base / "tpcr_live.duckdb"


def load_config(**overrides) -> PipelineConfig:
    """Create config with optional overrides."""
    return PipelineConfig(**overrides)
