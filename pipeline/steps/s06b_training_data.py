"""Step 6b: rebuild actuals_training_v2 before s07 trains on it.

Wraps scripts/build_actuals_training_data.py. Inputs: synthetic actuals from s06
(write base), fact tables, dimension tables and operating calendar (production
reads; the operating calendar prefers this run's copy). Encoding mappings are
refreshed first (append-only, pipeline.core.encodings), so new season_year
labels such as AUTUMN_2026 are encoded instead of dropped.

Output goes to matched_pairs/actuals_training_v2/{PARK}.parquet under the
write base: the shadow dir in shadow mode, production otherwise. Files are
renamed into place only after every park built; on any failure the step raises
and the previous complete files stay, so s07 never sees a partial file.

Receipt (logs/receipt_s06b_training_data_<date>.json and the step metrics):
status ok, or degraded when a park's max park_date is older than run_date - 2.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pipeline.config import PipelineConfig
from pipeline.core import paths
from pipeline.core.encodings import refresh_mappings_file, write_json_atomic
from pipeline.core.logging import PipelineLogger

_repo = Path(__file__).resolve().parents[2]
for _p in (_repo / "scripts", _repo / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from build_actuals_training_data import TrainingInputs, build_actuals_training_data

STEP = "s06b_training_data"
MAX_STALENESS_DAYS = 2


def stale_parks(max_dates: dict[str, str | None], run_date: date) -> dict[str, str | None]:
    floor = run_date - timedelta(days=MAX_STALENESS_DAYS)
    return {
        park: d for park, d in sorted(max_dates.items())
        if d is None or date.fromisoformat(d) < floor
    }


def run(cfg: PipelineConfig, log: PipelineLogger, run_date: date | None = None) -> dict:
    run_date = run_date or date.today()
    started = datetime.now(timezone.utc).isoformat()

    mappings = refresh_mappings_file(
        source=paths.encoding_mappings_path(cfg),
        target=paths.encoding_mappings_write_path(cfg),
        dim_dir=cfg.dimension_dir,
    )
    log.info(f"Encoding mappings: {mappings['n_added']} labels added -> {mappings['target']}")

    inputs = TrainingInputs(
        synth_dir=paths.synthetic_actuals_dir(cfg),
        parquet_dir=cfg.parquet_dir,
        dim_dir=cfg.dimension_dir,
        encodings_path=paths.encoding_mappings_write_path(cfg),
        oc_path=paths.operating_calendar_path(cfg),
    )
    built = build_actuals_training_data(
        log, inputs, cfg.write_base / "matched_pairs", cfg.get_min_training_year,
    )

    max_dates = {p: s["max_park_date"] for p, s in built["by_park"].items()}
    stale = stale_parks(max_dates, run_date)
    receipt = {
        "step": STEP,
        "status": "degraded" if stale else "ok",
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(),
        "run_date": run_date.isoformat(),
        "rows_by_park": {p: s["rows"] for p, s in built["by_park"].items()},
        "real_rows_by_park": {p: s["real_rows"] for p, s in built["by_park"].items()},
        "max_park_date_by_park": max_dates,
        "seconds_by_park": {p: s["seconds"] for p, s in built["by_park"].items()},
        "peak_rss_mb_by_park": {p: s["peak_rss_mb"] for p, s in built["by_park"].items()},
        "reason": (
            f"max park_date older than {run_date - timedelta(days=MAX_STALENESS_DAYS)}: {stale}"
            if stale else None
        ),
        "encodings": mappings,
        "output_dir": built["output_dir"],
    }
    write_json_atomic(paths.step_receipt_path(cfg, STEP, run_date.isoformat()), receipt)
    if stale:
        log.warning(f"s06b degraded: {receipt['reason']}")
    return {"rows": built["rows"], "receipt": receipt}
