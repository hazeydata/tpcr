"""Step 7a: refresh closures, dimentity, the operating calendar and the encoding
mappings before s08 forecasts. Fail soft.

Each source script runs as a subprocess against a private staging dir under the
write base, never against live files:
  1. src/get_closures_from_s3.py   -> staging/raw_closures/*.csv
  2. src/get_entity_table_from_s3.py -> staging/dimension_tables/dimentity.csv
  3. src/build_operating_calendar.py -> staging/operating_calendar/operating_calendar.parquet
Only validated outputs are renamed into the write base (the shadow dir in shadow
mode). A failed or empty part leaves the previous file in place and makes the
step status degraded with a reason; the pipeline continues either way.
Then state/encoding_mappings.json is extended with any new labels from
dimdategroupid/dimseason (pipeline.core.encodings).

S3 credentials come only from the process environment / AWS config on the
host. Subprocess output is not copied into logs; reasons carry return codes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from pipeline.config import PipelineConfig
from pipeline.core import paths
from pipeline.core.encodings import refresh_mappings_file, write_json_atomic
from pipeline.core.logging import PipelineLogger

STEP = "s07a_calendar"
REPO = Path(__file__).resolve().parents[2]
SCRIPT_TIMEOUT_SEC = 900

Runner = Callable[[str, Path], int]


def run_script(script: str, output_base: Path) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), env.get("PYTHONPATH", "")])
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO / "src" / script), "--output-base", str(output_base)],
            cwd=str(REPO), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=SCRIPT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return -9
    return proc.returncode


@dataclass
class Outcome:
    parts: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)

    def ok(self, part: str, **detail) -> None:
        self.parts[part] = {"status": "ok", **detail}

    def degraded(self, part: str, reason: str) -> None:
        self.parts[part] = {"status": "degraded", "reason": reason}
        self.reasons.append(f"{part}: {reason}")


def _promote(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


def _valid_dimentity(path: Path) -> str | None:
    if not path.exists() or path.stat().st_size == 0:
        return "no dimentity.csv written"
    try:
        df = pd.read_csv(path)
    except Exception as e:
        return f"unreadable dimentity.csv ({type(e).__name__})"
    if "code" not in df.columns and "entity_code" not in df.columns:
        return "dimentity.csv has no code column"
    if len(df) == 0:
        return "dimentity.csv has no rows"
    return None


def _valid_calendar(path: Path) -> str | None:
    if not path.exists():
        return "no operating_calendar.parquet written"
    try:
        df = pd.read_parquet(path, columns=["entity_code", "park_date", "is_operating"])
    except Exception as e:
        return f"unreadable operating calendar ({type(e).__name__})"
    if len(df) == 0:
        return "operating calendar has no rows"
    return None


def _refresh_closures(cfg, staging: Path, runner: Runner, out: Outcome) -> None:
    rc = runner("get_closures_from_s3.py", staging)
    got = sorted((staging / paths.RAW_CLOSURES).glob("*.csv"))
    got = [p for p in got if p.stat().st_size > 0]
    if rc != 0 or not got:
        out.degraded("closures", f"S3 closures download failed (rc={rc}, files={len(got)}); previous raw_closures kept")
        return
    for p in got:
        _promote(p, cfg.write_base / paths.RAW_CLOSURES / p.name)
    out.ok("closures", files=[p.name for p in got])


def _refresh_dimentity(cfg, staging: Path, runner: Runner, out: Outcome) -> None:
    rc = runner("get_entity_table_from_s3.py", staging)
    fresh = staging / paths.DIMENTITY
    problem = f"rc={rc}" if rc != 0 else _valid_dimentity(fresh)
    if problem:
        out.degraded("dimentity", f"dimentity refresh failed ({problem}); previous dimentity.csv kept")
        return
    _promote(fresh, cfg.write_base / paths.DIMENTITY)
    out.ok("dimentity")


def _rebuild_calendar(cfg, staging: Path, runner: Runner, out: Outcome) -> None:
    dimentity = paths.dimentity_path(cfg)
    if not dimentity.exists():
        out.degraded("operating_calendar", "no dimentity.csv to build from; previous calendar kept")
        return
    _promote(dimentity, staging / paths.DIMENTITY)
    closures_dir = cfg.write_base / paths.RAW_CLOSURES
    if not closures_dir.exists():
        closures_dir = cfg.prod_output_base / paths.RAW_CLOSURES
    if closures_dir.exists():
        for p in closures_dir.glob("*.csv"):
            _promote(p, staging / paths.RAW_CLOSURES / p.name)
    previous = paths.operating_calendar_path(cfg)
    if previous.exists():
        _promote(previous, staging / paths.OPERATING_CALENDAR)
    rc = runner("build_operating_calendar.py", staging)
    fresh = staging / paths.OPERATING_CALENDAR
    problem = f"rc={rc}" if rc != 0 else _valid_calendar(fresh)
    if problem:
        out.degraded("operating_calendar", f"calendar build failed ({problem}); previous calendar kept")
        return
    _promote(fresh, cfg.write_base / paths.OPERATING_CALENDAR)
    out.ok("operating_calendar")


def _refresh_mappings(cfg, out: Outcome) -> None:
    try:
        info = refresh_mappings_file(
            source=paths.encoding_mappings_path(cfg),
            target=paths.encoding_mappings_write_path(cfg),
            dim_dir=cfg.dimension_dir,
        )
    except Exception as e:
        out.degraded("encodings", f"mapping refresh failed ({type(e).__name__}: {e}); previous mapping kept")
        return
    out.ok("encodings", n_added=info["n_added"], dimension_last_date=info["dimension_last_date"])


def run(cfg: PipelineConfig, log: PipelineLogger, runner: Runner = run_script,
        run_date: date | None = None) -> dict:
    run_date = run_date or date.today()
    started = datetime.now(timezone.utc).isoformat()
    staging = cfg.write_base / f".s07a_staging-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    out = Outcome()
    try:
        for part, fn in (("closures", _refresh_closures), ("dimentity", _refresh_dimentity),
                         ("operating_calendar", _rebuild_calendar)):
            try:
                fn(cfg, staging, runner, out)
            except Exception as e:
                out.degraded(part, f"{type(e).__name__}: {e}; previous files kept")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _refresh_mappings(cfg, out)

    receipt = {
        "step": STEP,
        "status": "degraded" if out.reasons else "ok",
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(),
        "run_date": run_date.isoformat(),
        "parts": out.parts,
        "reason": "; ".join(out.reasons) or None,
    }
    write_json_atomic(paths.step_receipt_path(cfg, STEP, run_date.isoformat()), receipt)
    for r in out.reasons:
        log.warning(f"s07a degraded: {r}")
    log.info(f"s07a status: {receipt['status']}")
    return {"rows": 0, "receipt": receipt}
