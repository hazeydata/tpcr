"""Competition Framework Configuration.

Defines competition-specific paths and settings, completely separate from baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_OUTPUT_BASE = Path.home() / "hazeydata" / "pipeline"
DEFAULT_COMPETITION_BASE = Path.home() / "hazeydata" / "pipeline" / "competition"


@dataclass
class CompetitionConfig:
    """Competition framework configuration."""
    
    # Base paths
    output_base: Path  # Same as baseline (default: ~/hazeydata/pipeline)
    competition_base: Path = None  # Derived: {output_base}/competition
    
    # Competition-specific output directories  
    models_dir: Path = None         # {competition_base}/models
    forecasts_dir: Path = None      # {competition_base}/forecasts
    accuracy_dir: Path = None       # {competition_base}/accuracy  
    reports_dir: Path = None        # {competition_base}/reports
    logs_dir: Path = None           # {competition_base}/logs
    
    # Shared input paths (read from baseline namespace)
    baseline_output_base: Path = None  # Same as output_base for input data
    
    def __post_init__(self):
        """Derive competition paths from output_base."""
        self.baseline_output_base = self.output_base
        self.competition_base = self.output_base / "competition"
        
        # Competition output directories
        self.models_dir = self.competition_base / "models"
        self.forecasts_dir = self.competition_base / "forecasts"
        self.accuracy_dir = self.competition_base / "accuracy"
        self.reports_dir = self.competition_base / "reports"
        self.logs_dir = self.competition_base / "logs"
        
        # Create directories if they don't exist
        for dir_path in [
            self.competition_base,
            self.models_dir,
            self.forecasts_dir, 
            self.accuracy_dir,
            self.reports_dir,
            self.logs_dir
        ]:
            dir_path.mkdir(parents=True, exist_ok=True)
    
    def get_challenger_models_dir(self, challenger_name: str) -> Path:
        """Get models directory for a specific challenger."""
        return self.models_dir / challenger_name
    
    def get_challenger_forecasts_dir(self, challenger_name: str) -> Path:
        """Get forecasts directory for a specific challenger."""
        return self.forecasts_dir / challenger_name
        
    def get_challenger_accuracy_dir(self, challenger_name: str) -> Path:
        """Get accuracy directory for a specific challenger."""
        return self.accuracy_dir / challenger_name
        
    def get_baseline_fact_tables_dir(self) -> Path:
        """Get baseline fact tables directory for reading shared data."""
        return self.baseline_output_base / "fact_tables"
        
    def get_baseline_dimension_dir(self) -> Path:
        """Get baseline dimensions directory for reading shared data."""
        return self.baseline_output_base / "dimension_tables"
        
    def get_baseline_state_dir(self) -> Path:
        """Get baseline state directory for reading shared data."""
        return self.baseline_output_base / "state"


def load_competition_config(output_base: Path | str | None = None) -> CompetitionConfig:
    """Create competition config with the given output base (default ~/hazeydata/pipeline)."""
    if output_base is None:
        output_base = DEFAULT_OUTPUT_BASE
    if isinstance(output_base, str):
        output_base = Path(output_base)
    return CompetitionConfig(output_base=output_base)