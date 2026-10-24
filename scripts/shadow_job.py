#!/usr/bin/env python3
"""Shadow job — scheduled wrapper around scripts/rolling_shadow.py (TPCR-SHADOW-REVIVE-001 Phase 3).

Run by launchd at about 06:20 local, after the daily pipeline (S1-S11). Order:

  1. Take a non-blocking flock on <competition>/locks/shadow_job.lock. A second
     shadow job exits 0 at once. This lock is shadow-only; it is NOT shared with
     S1-S11 or the live_waits scraper.
  2. Wait (poll 60 s, at most --max-marker-wait-min, default 30) for
     <pipeline base>/logs/pipeline_metrics_<date>.json, the same marker the daily
     report reads. status "done" -> go on. "failed" / "sync_partial" ->
     run_status=skipped_pipeline. No terminal status in time -> skipped_no_marker.
  3. Wait (at most --lock-wait-min, default 20) while the live_waits scraper lock
     (<pipeline base>/state/processing_queue_times.lock) is held. Still held ->
     run_status=skipped_lock.
  4. Run rolling_shadow.run_day with every output (scores, summary, registry)
     written into a staging dir. Re-check the scraper lock, then commit the staged
     files with os.replace. Lock taken mid-run -> discard staging, skipped_lock.

The scraper lock is only ever READ (stat). This job never creates, removes or
touches it and never imports the pipeline's lock helpers.

Skipped and failed runs write only summaries/<eval date>.json (eval date =
run date - 1, the same key rolling_shadow uses). Skips exit 0; failure exits 1.
A rerun for the same date overwrites that date's files.

Usage:
    python scripts/shadow_job.py [--date YYYY-MM-DD] [--dry-run]
"""

import argparse
import fcntl
import json
import os
import shutil
import sys
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rolling_shadow as rs  # noqa: E402
from pipeline.competition.io_utils import atomic_write_json  # noqa: E402

TERMINAL_STATUSES = ("done", "failed", "sync_partial")  # same set as daily_report.py
SCRAPER_LOCK_NAME = "processing_queue_times.lock"  # src/get_wait_times_from_queue_times.py
SCRAPER_LOCK_STALE_S = 86400  # the scraper's own rule: a lock older than 24 h is stale
SHADOW_LOCK_NAME = "shadow_job.lock"
NICE_INCREMENT = 10

RUN_OK = "ok"
RUN_FAILED = "failed"
RUN_SKIPPED_PIPELINE = "skipped_pipeline"
RUN_SKIPPED_NO_MARKER = "skipped_no_marker"
RUN_SKIPPED_LOCK = "skipped_lock"


# --- Pipeline completion marker (read-only) ---

def marker_path(pipeline_base, run_date):
    """Same file pipeline/pipeline.py writes and tpcr-discord-bot/daily_report.py reads."""
    return Path(pipeline_base) / "logs" / f"pipeline_metrics_{run_date.isoformat()}.json"


def read_marker_status(pipeline_base, run_date):
    """Marker `status` string, or None if the file is missing or unreadable."""
    try:
        data = json.loads(marker_path(pipeline_base, run_date).read_text())
    except (OSError, ValueError):
        return None
    status = data.get("status") if isinstance(data, dict) else None
    return str(status) if status is not None else None


def wait_for_marker(pipeline_base, run_date, max_wait_min=30, poll_s=60, sleep=None, clock=None):
    """Poll until the marker has a terminal status or the wait runs out.

    Returns the terminal status, or None when none appeared in time.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + max_wait_min * 60
    while True:
        status = read_marker_status(pipeline_base, run_date)
        if status in TERMINAL_STATUSES:
            return status
        if clock() >= deadline:
            return None
        print(f"Waiting for pipeline marker (status={status}) ...", flush=True)
        sleep(poll_s)


# --- Scraper lock (read-only) ---

def scraper_lock_path(pipeline_base):
    return Path(pipeline_base) / "state" / SCRAPER_LOCK_NAME


def scraper_lock_held(pipeline_base):
    """True when the scraper lock file exists and is not stale. Never modifies it.

    Mirrors acquire_lock() in src/get_tp_wait_time_data_from_s3.py: the lock is a
    plain pid file (no flock), and one older than 24 h counts as stale.
    """
    try:
        age = time.time() - scraper_lock_path(pipeline_base).stat().st_mtime
    except OSError:
        return False
    return age <= SCRAPER_LOCK_STALE_S


def wait_for_scraper_lock(pipeline_base, max_wait_min=20, poll_s=60, sleep=None, clock=None):
    """Poll until the scraper lock is free. Returns True when free, False if still held at timeout."""
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + max_wait_min * 60
    while scraper_lock_held(pipeline_base):
        if clock() >= deadline:
            return False
        print("Scraper lock held, waiting ...", flush=True)
        sleep(poll_s)
    return True


# --- Shadow-only job lock ---

def take_job_lock(competition_base):
    """Non-blocking flock on competition/locks/shadow_job.lock. Returns the open fd, or None.

    Shadow-only: no pipeline step or the scraper ever uses this file.
    """
    path = Path(competition_base) / "locks" / SHADOW_LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(path, "a")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    return fd


# --- Outputs ---

def skip_summary(eval_date, run_date, run_status, errors=(), detail=None):
    summary = {
        "date": eval_date, "run_date": run_date.isoformat(),
        "matched_pairs": 0, "forecast_entities": 0, "coverage_pct": 0.0,
        "challengers": {}, "run_status": run_status, "errors": list(errors),
    }
    if detail:
        summary["detail"] = detail
    return summary


def write_summary_only(competition_base, eval_date, summary):
    atomic_write_json(Path(competition_base) / "summaries" / f"{eval_date}.json", summary)


def commit_stage(stage, competition_base, eval_date):
    """Move the staged day outputs into place with os.replace. Summary goes last."""
    competition_base = Path(competition_base)
    stage = Path(stage)
    moves = [
        (stage / "scores" / f"{eval_date}.parquet", competition_base / "scores" / f"{eval_date}.parquet"),
        (stage / rs.REGISTRY_PATH.name, competition_base / rs.REGISTRY_PATH.name),
        (stage / "summaries" / f"{eval_date}.json", competition_base / "summaries" / f"{eval_date}.json"),
    ]
    for src, dst in moves:
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)


# --- Main ---

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Scheduled shadow job (marker + scraper-lock aware)")
    p.add_argument("--date", type=date.fromisoformat, help="run date YYYY-MM-DD (default: today)")
    p.add_argument("--pipeline-base", help="default: $TPCR_PIPELINE_BASE or ~/hazeydata/pipeline")
    p.add_argument("--tpcr-dir", help="default: $TPCR_DIR or ~/theme-park-crowd-report")
    p.add_argument("--max-marker-wait-min", type=float, default=30)
    p.add_argument("--lock-wait-min", type=float, default=20)
    p.add_argument("--dry-run", action="store_true",
                   help="report marker and lock state once; wait for nothing, write nothing")
    return p.parse_args(argv)


def main(argv=None, sleep=None, clock=None, poll_s=60):
    """Returns the exit code: 0 ok / skipped / overlapped / dry-run, 1 failed."""
    args = parse_args(argv)
    try:
        os.nice(NICE_INCREMENT)
    except (OSError, AttributeError):
        pass

    rs.configure(args.pipeline_base, args.tpcr_dir)
    pipeline_base, competition = rs.PIPELINE_BASE, rs.COMPETITION_BASE
    run_date = args.date or date.today()
    eval_date = (run_date - timedelta(days=1)).isoformat()

    if args.dry_run:
        print(f"[dry-run] run date {run_date}, eval date {eval_date}")
        print(f"[dry-run] marker {marker_path(pipeline_base, run_date)}: "
              f"status={read_marker_status(pipeline_base, run_date)}")
        print(f"[dry-run] scraper lock {scraper_lock_path(pipeline_base)}: "
              f"held={scraper_lock_held(pipeline_base)}")
        return 0

    job_lock = take_job_lock(competition)
    if job_lock is None:
        print("Another shadow job is running; exiting.")
        return 0

    stage = competition / "staging" / eval_date
    try:
        status = wait_for_marker(pipeline_base, run_date, args.max_marker_wait_min, poll_s, sleep, clock)
        if status is None:
            run_status, detail = RUN_SKIPPED_NO_MARKER, f"no terminal marker within {args.max_marker_wait_min:g} min"
        elif status != "done":
            run_status, detail = RUN_SKIPPED_PIPELINE, f"pipeline status {status}"
        elif not wait_for_scraper_lock(pipeline_base, args.lock_wait_min, poll_s, sleep, clock):
            run_status, detail = RUN_SKIPPED_LOCK, f"scraper lock held for {args.lock_wait_min:g} min"
        else:
            run_status, detail = None, None
        if run_status:
            print(f"Skipping: {run_status} ({detail})")
            write_summary_only(competition, eval_date, skip_summary(eval_date, run_date, run_status, detail=detail))
            return 0

        shutil.rmtree(stage, ignore_errors=True)
        summary = rs.run_day(run_date, out_base=stage, post=False)
        if summary is None:  # no active challenger: registry normalization only
            summary = rs.write_day_outputs(eval_date, [], [], stage)

        if scraper_lock_held(pipeline_base):
            print("Scraper lock taken mid-run; discarding staged outputs.")
            shutil.rmtree(stage, ignore_errors=True)
            write_summary_only(competition, eval_date, skip_summary(
                eval_date, run_date, RUN_SKIPPED_LOCK, detail="scraper lock taken during the run"))
            return 0

        commit_stage(stage, competition, eval_date)
        print(f"Committed outputs for {eval_date}: run_status={summary['run_status']}")
        return 1 if summary["run_status"] == RUN_FAILED else 0
    except Exception as e:  # noqa: BLE001 - any failure must land in the summary
        traceback.print_exc()
        shutil.rmtree(stage, ignore_errors=True)
        errors = [{"type": type(e).__name__, "message": str(e)}]
        try:
            write_summary_only(competition, eval_date, skip_summary(eval_date, run_date, RUN_FAILED, errors))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return 1
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        job_lock.close()  # releases the flock


if __name__ == "__main__":
    sys.exit(main())
