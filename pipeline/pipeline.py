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

# Legacy bridge: keep pipeline_status.json updated for monitoring scripts
try:
    from utils.pipeline_status import pipeline_start as _ps_start, step_done as _ps_step_done, step_failed as _ps_step_failed
    _HAS_PIPELINE_STATUS = True
except ImportError:
    _HAS_PIPELINE_STATUS = False

# Map v4 step names → legacy step names for pipeline_status.json
_LEGACY_STEP_MAP = {
    "s01_sync": "s3_sync",
    "s02_etl": "etl",
    "s03_dimensions": "dimensions",
    "s04_aggregates": "aggregates",
    "s05_conversion": "conversion",
    "s06_synthetic": "synthetic",
    "s07_training": "training",
    "s08_forecast": "forecast",
    "s09_wti": "wti",
    "s10_accuracy": "accuracy",
    "s11_deploy": "deploy",
}


def _write_pipeline_local_alert(
    *,
    cfg,
    run_date: str,
    metrics: PipelineMetrics,
    metrics_path: Path,
    failed: bool,
) -> None:
    """D1 #7: status != success → local file alert (no Discord dependency).

    Always refreshes PIPELINE_STATUS.md. On failure, also writes
    PIPELINE_ALERT_<date>.md and latest PIPELINE_ALERT.md pointer.
    On success, clears the latest pointer so operators aren't left with a stale red flag.
    """
    logs = Path(cfg.logs_dir)
    logs.mkdir(parents=True, exist_ok=True)

    failed_steps = [
        f"- **{name}**: {s.error or s.status}"
        for name, s in metrics.steps.items()
        if s.status == "failed"
    ]
    deploy_extra = ""
    s11 = metrics.steps.get("s11_deploy")
    if s11 and isinstance(s11.extra, dict) and s11.extra.get("deploy_assert"):
        deploy_extra = "\n### deploy_assert\n```json\n" + json.dumps(
            s11.extra["deploy_assert"], indent=2, default=str
        ) + "\n```\n"

    status_body = (
        f"# Pipeline status — {run_date}\n\n"
        f"- **status:** `{'failed' if failed else 'done'}`\n"
        f"- **run_start:** {metrics.run_start}\n"
        f"- **total_duration_sec:** {metrics.total_duration_sec}\n"
        f"- **metrics:** `{metrics_path}`\n"
        f"- **shadow:** {metrics.shadow_mode}\n\n"
    )
    if failed_steps:
        status_body += "## Failed steps\n" + "\n".join(failed_steps) + "\n"
    status_body += deploy_extra
    status_body += (
        "\n_Local alert only — Discord not required. "
        "Operators: Michael / Hermes morning brief._\n"
    )
    (logs / "PIPELINE_STATUS.md").write_text(status_body)

    latest = logs / "PIPELINE_ALERT.md"
    if failed:
        alert = logs / f"PIPELINE_ALERT_{run_date}.md"
        alert.write_text(
            f"# PIPELINE ALERT — {run_date}\n\n"
            f"**status != success**\n\n"
            + status_body
        )
        latest.write_text(alert.read_text())
    elif latest.exists():
        # success clears sticky latest alert
        latest.write_text(
            f"# PIPELINE_ALERT\n\n_cleared {run_date} — last full run status=done_\n"
        )


def main() -> int:
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
    for step_name in steps:
        step_log = PipelineLogger(step_name, cfg.logs_dir)
        metrics.start_step(step_name)

        try:
            module = import_module(f"pipeline.steps.{step_name}")
            if not hasattr(module, "run"):
                log.warning(f"Step {step_name} has no run() function — skipping")
                metrics.skip_step(step_name, "no run() function")
                continue

            result = module.run(cfg, step_log)
            if isinstance(result, dict):
                rows_out = int(result.get("rows", 0) or 0)
                # Fold step extras (e.g. s11 deploy_assert) into metrics JSON
                extra = {
                    k: v
                    for k, v in result.items()
                    if k not in ("rows",)
                }
            else:
                rows_out = 0
                extra = {}
            metrics.end_step(step_name, rows_out=rows_out, **extra)

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
    metrics.status = "failed" if failed else "done"

    log.info("=" * 60)
    log.info(f"PIPELINE {'FAILED' if failed else 'COMPLETE'} in {total}s")
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

    # D1 #7 — local alert file when status != success (no Discord dependency)
    try:
        _write_pipeline_local_alert(
            cfg=cfg,
            run_date=run_date,
            metrics=metrics,
            metrics_path=metrics_path,
            failed=failed,
        )
    except Exception as e:
        log.warning(f"Could not write local pipeline alert: {e}")

    # Save all pipeline-level events
    log.save_events(log_events_path(cfg, run_date))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
