"""Entity eligibility and skip logging shared by S7 (training) and S8 (forecast).

- dimentity extinct_on loading
- warning (never an error) for extinct_on earlier than 2005-01-01; those entities are treated as extinct
- operating-calendar index + window check
- SkipLog: every skipped entity gets a reason, plus per-reason counts at stage end
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pandas as pd

from pipeline.core.logging import PipelineLogger
from pipeline.core.validation import ValidationError

MIN_VALID_EXTINCT_ON = date(2005, 1, 1)
EXTINCT_SENTINEL = date(2000, 1, 1)

SKIP_REASONS = {"extinct", "not_operating", "no_training_data", "below_min_obs"}


def load_dimentity_extinct(dim_csv_path: Path) -> dict[str, date | None]:
    """Return entity_code -> extinct_on (date or None)."""
    df = pd.read_csv(dim_csv_path, dtype=str)
    code_col = "code" if "code" in df.columns else "entity_code"
    if code_col not in df.columns:
        raise ValidationError(f"dimentity has no 'code' or 'entity_code' column: {dim_csv_path}")
    if "extinct_on" in df.columns:
        dates = pd.to_datetime(df["extinct_on"], errors="coerce")
    else:
        dates = pd.Series(pd.NaT, index=df.index)
    mapping: dict[str, date | None] = {
        str(code): (None if pd.isna(d) else d.date()) for code, d in zip(df[code_col], dates)
    }

    return mapping


def warn_pre2005_extinct_on(mapping: dict[str, date | None], log) -> list[tuple[str, date]]:
    """Warn once per entity whose extinct_on is before 2005-01-01. Never raises.

    Such entities are treated as extinct (extinct_on <= run date). The 2000-01-01
    value is a sentinel that also means extinct. Returns the (code, date) list.
    """
    found = sorted((c, d) for c, d in mapping.items() if d is not None and d < MIN_VALID_EXTINCT_ON)
    for code, d in found:
        sentinel = " sentinel=true" if d == EXTINCT_SENTINEL else ""
        log.warning(
            f"PRE2005_EXTINCT_ON entity={code} extinct_on={d.isoformat()} treated_as=extinct{sentinel}"
        )
    return found


def load_operating_calendar_indexed(cfg, log: PipelineLogger) -> dict | None:
    """Load operating calendar PRE-INDEXED by entity code.

    Returns dict[entity_code_upper] -> set(dates) or None if no OC.
    O(1) lookup per entity instead of scanning 9.3M tuples.
    """
    oc_path = cfg.output_base / "operating_calendar" / "operating_calendar.parquet"
    if not oc_path.exists():
        log.info("No operating calendar — assuming all entities operating")
        return None

    oc_df = pd.read_parquet(oc_path)
    oc_df = oc_df[oc_df["is_operating"] == True]
    oc_df["entity_upper"] = oc_df["entity_code"].astype(str).str.upper()
    oc_df["date_val"] = pd.to_datetime(oc_df["park_date"]).dt.date

    # Pre-index: entity → set of operating dates
    indexed = {}
    for entity, group in oc_df.groupby("entity_upper"):
        indexed[entity] = set(group["date_val"])

    log.info(f"Operating calendar: {len(oc_df):,} entries, {len(indexed)} entities indexed")
    return indexed


def is_operating_in_window(
    entity_code: str, oc_by_entity: dict | None, start: date, end: date
) -> bool:
    """Mirror of S8's calendar logic: unknown entity/no calendar -> operating."""
    if oc_by_entity is None:
        return True
    dates = oc_by_entity.get(entity_code.upper())
    if dates is None:
        return True
    return any(start <= d <= end for d in dates)


class SkipLog:
    """Records every skipped entity with a reason; summarises per-reason counts."""

    def __init__(self, stage: str, cfg, log: PipelineLogger, run_date: date | None = None):
        self.stage = stage
        self.cfg = cfg
        self.log = log
        self.run_date = run_date or date.today()
        self.rows: list[tuple[str, str, str]] = []
        self.counts: dict[str, int] = {}
        self.pre2005_extinct_on: int | None = None

    @staticmethod
    def _valid(reason: str) -> bool:
        return reason in SKIP_REASONS or (reason.startswith("error:") and len(reason) > len("error:"))

    def record(self, stage: str, entity_code: str, reason: str) -> None:
        if not self._valid(reason):
            raise ValueError(f"Invalid skip reason: {reason!r}")
        self.rows.append((stage, entity_code, reason))
        self.counts[reason] = self.counts.get(reason, 0) + 1
        self.log.info(f"SKIP stage={stage} entity={entity_code} reason={reason}")

    def note_pre2005_extinct_on(self, count: int) -> None:
        """Record the pre-2005 extinct_on count; summary() logs it (the CSV is unchanged)."""
        self.pre2005_extinct_on = count

    def total(self) -> int:
        return len(self.rows)

    def summary(self) -> dict[str, int]:
        """Log per-reason counts and write the skips CSV. Returns reason -> count."""
        for reason, n in sorted(self.counts.items()):
            self.log.info(f"SKIP_COUNT stage={self.stage} reason={reason} count={n}")
            self.log.metric(f"{self.stage}_skip_{reason}", n)
        if self.pre2005_extinct_on is not None:
            n = self.pre2005_extinct_on
            self.log.info(f"SKIP_NOTE stage={self.stage} pre2005_extinct_on count={n}")
            self.log.metric(f"{self.stage}_pre2005_extinct_on", n)
        logs_dir = Path(self.cfg.logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / f"skips_{self.stage}_{self.run_date.isoformat()}.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["stage", "entity_code", "reason"])
            w.writerows(self.rows)
        return dict(self.counts)
