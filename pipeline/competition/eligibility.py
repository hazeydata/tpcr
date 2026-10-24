"""Entities production S7 would train for a run date (TPCR-SHADOW-REVIVE-001 AC5).

Challenger retrains train exactly this set. Read-only with respect to
production: S7 helpers are imported, never modified.

Selection mirrors s07_training.run(): entities with >= training_min_obs_lite
observations in the training data, skipping ignored (water) parks. On top of
that, entities with dimentity.extinct_on <= run_date are excluded. When
`pipeline.core.eligibility` is importable (PR #522), its own rules are used
instead: extinct_on with config/dimentity_overrides.csv applied, plus the
not-operating-in-the-S8-window skip, so the count tracks production S7.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd

from pipeline.core.park_codes import entity_to_park

log = logging.getLogger(__name__)


def is_extinct(entity, extinct_on, run_date) -> bool:
    """The one extinct predicate: shared by the challenger eligible list and the AC5 check."""
    d = extinct_on.get(entity)
    return d is not None and d <= run_date



def select_eligible(entity_counts, min_obs, ignore_parks, extinct_on, run_date):
    """Pure selection. Returns (eligible sorted list, excluded {entity: reason})."""
    excluded: dict[str, str] = {}
    eligible: list[str] = []
    for entity, count in entity_counts.items():
        if count < min_obs:
            excluded[entity] = "below_min_obs"
        elif entity_to_park(entity) in ignore_parks:
            excluded[entity] = "ignored_park"
        elif is_extinct(entity, extinct_on, run_date):
            excluded[entity] = "extinct"
        else:
            eligible.append(entity)
    return sorted(eligible), excluded


def load_extinct_on(dimension_dir: Path) -> dict[str, date]:
    """entity_code -> extinct_on date from dimentity.csv (rows without a date omitted)."""
    path = Path(dimension_dir) / "dimentity.csv"
    if not path.exists():
        log.warning("dimentity.csv not found at %s; no extinct exclusion applied", path)
        return {}
    df = pd.read_csv(path)
    code_col = "code" if "code" in df.columns else "entity_code"
    if "extinct_on" not in df.columns:
        return {}
    dates = pd.to_datetime(df["extinct_on"], errors="coerce")
    return {
        str(c): d.date() for c, d in zip(df[code_col], dates) if pd.notna(d)
    }


def load_extinct_dates(cfg) -> dict[str, date]:
    """entity -> extinct date: #522's rules (with overrides) when available, else dimentity.csv."""
    core = _core()
    if core is not None:
        return core.load_dimentity_extinct(Path(cfg.dimension_dir) / "dimentity.csv")
    return load_extinct_on(cfg.dimension_dir)


def extinct_entities(entities, run_date, *, cfg=None, output_base=None, extinct_on=None) -> set:
    """Subset of `entities` that is_extinct on run_date (dates from load_extinct_dates unless given)."""
    if extinct_on is None:
        if cfg is None:
            from pipeline.config import PipelineConfig
            cfg = PipelineConfig(output_base=Path(output_base))
        extinct_on = load_extinct_dates(cfg)
    return {e for e in entities if is_extinct(e, extinct_on, run_date)}


def scan_training_counts(cfg) -> dict[str, int]:
    """Observation counts per entity from the S7 training data (same paths as S7)."""
    from pipeline.steps.s07_training import _scan_entity_counts

    actuals_dir = cfg.output_base / "matched_pairs" / "actuals_training_v2"
    actuals_single = cfg.output_base / "matched_pairs" / "actuals_training_v2.parquet"
    park_files = sorted(actuals_dir.glob("*.parquet")) if actuals_dir.is_dir() else []
    if park_files:
        return _scan_entity_counts(park_files)
    if actuals_single.exists():
        return _scan_entity_counts([actuals_single])
    return {}


def _core():
    """pipeline.core.eligibility (added by PR #522), or None on this base."""
    try:
        from pipeline.core import eligibility as core
    except ImportError:
        return None
    needed = ("load_dimentity_extinct", "load_operating_calendar_indexed", "is_operating_in_window")
    return core if all(callable(getattr(core, n, None)) for n in needed) else None


def _apply_core(cfg, run_date, eligible, excluded, core):
    """Mirror #522's S7 rules: extinct_on (with overrides) and not operating in the S8 window."""
    from datetime import timedelta
    from pipeline.core.logging import PipelineLogger

    extinct = load_extinct_dates(cfg)
    oc = core.load_operating_calendar_indexed(cfg, PipelineLogger("competition_eligibility"))
    window_end = run_date + timedelta(days=cfg.forecast_days)
    kept = []
    for e in eligible:
        if is_extinct(e, extinct, run_date):
            excluded[e] = "extinct"
        elif not core.is_operating_in_window(e, oc, run_date, window_end):
            excluded[e] = "not_operating"
        else:
            kept.append(e)
    return kept


def eligibility_report(output_base, run_date: date, *, cfg=None, entity_counts=None,
                       extinct_on=None, min_obs=None, ignore_parks=None) -> dict:
    """Eligible set plus exclusions. Keyword overrides exist so tests need no data files."""
    if cfg is None and (entity_counts is None or extinct_on is None
                        or min_obs is None or ignore_parks is None):
        from pipeline.config import PipelineConfig
        cfg = PipelineConfig(output_base=Path(output_base))

    core = _core() if (entity_counts is None and extinct_on is None and cfg is not None) else None
    if extinct_on is None:
        extinct_on = load_extinct_dates(cfg)
    if min_obs is None:
        min_obs = getattr(cfg, "training_min_obs_lite", 100)
    if ignore_parks is None:
        ignore_parks = cfg.ignore_parks

    if entity_counts is None:
        entity_counts = scan_training_counts(cfg)

    eligible, excluded = select_eligible(entity_counts, min_obs, set(ignore_parks), extinct_on, run_date)
    if core is not None:
        eligible = _apply_core(cfg, run_date, eligible, excluded, core)
        return {"eligible": eligible, "excluded": excluded, "source": "pipeline.core.eligibility"}
    return {"eligible": eligible, "excluded": excluded, "source": "mirror_s07"}


def production_eligible_entities(output_base, run_date: date, **kwargs) -> list[str]:
    """Sorted entities production S7 would train for run_date, after extinct exclusion."""
    return eligibility_report(output_base, run_date, **kwargs)["eligible"]
