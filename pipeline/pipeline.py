#!/usr/bin/env python3
"""HazeyData Pipeline — Single entry point.

Usage:
    python pipeline/pipeline.py                        # Full run
    python pipeline/pipeline.py --shadow               # Shadow mode
    python pipeline/pipeline.py --step s09_wti         # Single step
    python pipeline/pipeline.py --step s07_training --park MK  # Single park
    python pipeline/pipeline.py --output-base /path    # Custom output
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from importlib import import_module
from pathlib import Path

# Ensure pipeline and src/ are importable
_repo_root = str(Path(__file__).parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
_src_dir = str(Path(__file__).parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from pipeline.config import load_config
from pipeline.core.logging import PipelineLogger
from pipeline.core.metrics import PipelineMetrics
from pipeline.core.paths import log_events_path
from pipeline.steps import STEP_ORDER
from pipeline.steps.s01_sync import EXIT_SYNC_PARTIAL, SyncPartial, downstream_blocked

# Legacy bridge: keep pipeline_status.json updated for monitoring scripts
try:
    from utils.pipeline_status import pipeline_start as _ps_start, step_done as _ps_step_done, step_failed as _ps_step_failed
    _HAS_PIPELINE_STATUS = True
except ImportError:
    _HAS_PIPELINE_STATUS = False

# Map step names to legacy step names used by pipeline_status.json
_LEGACY_STEP_MAP = {
    "s01_sync": None,        # no legacy equivalent
    "s02_etl": "etl",
    "s03_dimensions": "dimensions",
    "s04_aggregates": "aggregates",
    "s05_conversion": "report",
    "s06_synthetic": None,   # no legacy equivalent
    "s07_training": "training",
    "s08_forecast": "forecast",
    "s09_wti": "wti",
    "s10_accuracy": None,    # no legacy equivalent
    "s11_deploy": None,      # no legacy equivalent
    "s12_validate": None,    # no legacy equivalent
    "s14_content": None,     # no legacy equivalent
}


def main():
    parser = argparse.ArgumentParser(description="HazeyData Pipeline")
    parser.add_argument("--output-base", type=Path, default=None)
    parser.add_argument("--shadow", action="store_true", help="Shadow mode: compare, don't deploy")
    parser.add_argument("--step", type=str, default=None, help="Run a single step (e.g. s09_wti)")
    parser.add_argument("--park", type=str, default=None, help="Limit to one park (e.g. MK)")
    parser.add_argument("--days", type=int, default=None, help="Override forecast days")
    args = parser.parse_args()

    # Build config
    overrides = {}
    if args.output_base:
        overrides["output_base"] = args.output_base
    if args.shadow:
        overrides["shadow"] = True
    if args.days:
        overrides["forecast_days"] = args.days
    cfg = load_config(**overrides)

    # Issue #6: Shadow mode runs at reduced priority to avoid starving production
    if cfg.shadow:
        try:
            os.nice(10)
        except OSError:
            pass  # nice() may fail in some environments

    run_date = date.today().isoformat()
    log = PipelineLogger("pipeline", cfg.logs_dir)
    metrics = PipelineMetrics(
        run_date=run_date,
        run_start=datetime.now(timezone.utc).isoformat(),
        shadow_mode=cfg.shadow,
    )

    log.info("=" * 60)
    log.info(f"HAZEYDATA PIPELINE {'(SHADOW MODE — nice +10)' if cfg.shadow else ''}")
    log.info(f"Run date: {run_date}")
    log.info(f"Output: {cfg.output_base}")
    if cfg.shadow:
        log.info(f"Shadow output: {cfg.shadow_output_base}")
    if args.step:
        log.info(f"Single-step mode: {args.step}")
    log.info("=" * 60)

    start_time = time.time()
    metrics.status = "running"

    # Determine which steps to run
    single_step = args.step is not None
    if single_step:
        if args.step not in STEP_ORDER:
            log.error(f"Unknown step: {args.step}. Available: {STEP_ORDER}")
            return 1
        steps = [args.step]
    else:
        steps = STEP_ORDER

    # Update legacy pipeline_status.json for monitoring scripts
    if _HAS_PIPELINE_STATUS and not single_step and not cfg.shadow:
        try:
            _ps_start(cfg.output_base)
            log.info("Legacy pipeline_status.json initialized")
        except Exception as e:
            log.warning(f"Could not init pipeline_status.json: {e}")

    # Execute steps
    failed = False
    partial = False
    for step_name in steps:
        if step_name != "s01_sync":
            blocked = downstream_blocked(cfg)
            if blocked:
                log.warning(f"Skipping {step_name}: s01_sync status is {blocked}")
                metrics.skip_step(step_name, blocked)
                partial = True
                continue
        step_log = PipelineLogger(step_name, cfg.logs_dir)
        metrics.start_step(step_name)

        try:
            module = import_module(f"pipeline.steps.{step_name}")
            if not hasattr(module, "run"):
                log.warning(f"Step {step_name} has no run() function — skipping")
                metrics.skip_step(step_name, "no run() function")
                continue

            result = module.run(cfg, step_log)
            rows_out = result.get("rows", 0) if isinstance(result, dict) else 0
            metrics.end_step(step_name, rows_out=rows_out)

            # Update legacy pipeline_status.json
            if _HAS_PIPELINE_STATUS and not cfg.shadow:
                legacy = _LEGACY_STEP_MAP.get(step_name)
                if legacy:
                    try:
                        _ps_step_done(cfg.output_base, legacy)
                    except Exception:
                        pass

            # Save step events
            step_log.save_events(log_events_path(cfg, run_date))

        except SyncPartial as e:
            log.warning(f"Step {step_name} PARTIAL: {e}")
            metrics.fail_step(step_name, str(e))
            step_log.save_events(log_events_path(cfg, run_date))
            partial = True

        except Exception as e:
            log.error(f"Step {step_name} FAILED: {e}")
            metrics.fail_step(step_name, str(e))
            failed = True

            # Update legacy pipeline_status.json on failure
            if _HAS_PIPELINE_STATUS and not cfg.shadow:
                legacy = _LEGACY_STEP_MAP.get(step_name)
                if legacy:
                    try:
                        _ps_step_failed(cfg.output_base, legacy)
                    except Exception:
                        pass
            # In shadow mode, continue on failure. In production, stop.
            if not cfg.shadow:
                break

    # Finalize
    total = round(time.time() - start_time, 2)
    metrics.total_duration_sec = total
    metrics.status = "failed" if failed else ("sync_partial" if partial else "done")

    log.info("=" * 60)
    log.info(f"PIPELINE {metrics.status.upper()} in {total}s")
    log.info("=" * 60)

    # Save metrics — but DON'T overwrite the full-run metrics with a
    # single-step re-run. Single-step metrics go to a separate file.
    if single_step:
        metrics_path = cfg.logs_dir / f"pipeline_metrics_{run_date}_{args.step}.json"
        log.info(f"Single-step mode: metrics saved to {metrics_path.name} (full-run metrics preserved)")
    else:
        metrics_path = cfg.logs_dir / f"pipeline_metrics_{run_date}.json"
    metrics.save(metrics_path)
    log.info(f"Metrics saved: {metrics_path}")

    # Save all pipeline-level events
    log.save_events(log_events_path(cfg, run_date))

    if failed:
        return 1
    return EXIT_SYNC_PARTIAL if partial else 0


if __name__ == "__main__":
    sys.exit(main())
