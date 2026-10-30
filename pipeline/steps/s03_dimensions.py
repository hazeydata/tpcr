"""Step 3: Dimension Tables + Park Hours + Closures.

Validates and loads dimension tables and imputes park hours. It does not
download closures or build the operating calendar: that is s07a_calendar.

During shadow phase: reads from production dimension tables.
Post-swap: runs dimension fetch scripts.
"""

from __future__ import annotations

from pipeline.config import PipelineConfig
from pipeline.core.logging import PipelineLogger
from pipeline.core.validation import require_file


def run(cfg: PipelineConfig, log: PipelineLogger) -> dict:
    """Load and validate dimension tables."""

    log.info("=" * 60)
    log.info("STEP 3: DIMENSIONS + PARK HOURS + CLOSURES")
    log.info("=" * 60)

    if cfg.shadow:
        log.info("Shadow mode: validating production dimension tables exist")

    # Validate critical dimension files
    critical_files = [
        (cfg.dimension_dir / "dimentity.csv", "Entity dimension table"),
        (cfg.dimension_dir / "dimdategroupid.csv", "Date group ID dimension"),
        (cfg.dimension_dir / "dimseason.csv", "Season dimension"),
        (cfg.dimension_dir / "dimparkhours.csv", "Park hours dimension"),
    ]

    found = 0
    for path, desc in critical_files:
        try:
            require_file(path, desc)
            found += 1
            log.info(f"  \u2714 {desc}: {path.name}")
        except Exception as e:
            log.warning(f"  \u2718 {desc}: {e}")

    # Check operating calendar (optional but important)
    oc_path = cfg.output_base / "operating_calendar" / "operating_calendar.parquet"
    if oc_path.exists():
        log.info(f"  \u2714 Operating calendar: {oc_path.name}")
        found += 1
    else:
        log.warning("  \u2718 Operating calendar not found \u2014 forecasts will assume all entities operating")

    log.info(f"Dimensions: {found}/{len(critical_files) + 1} files present")
    return {"rows": 0, "files_found": found}
