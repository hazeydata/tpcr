#!/usr/bin/env python3
"""Rolling Competition Shadow Run — Daily challenger orchestration (local).

Runs after the baseline pipeline completes. For each active challenger in the
registry:
  1. Generate shadow predictions (pipeline.competition.forecast_challenger)
  2. Archive 1-day-ahead predictions for both baseline and challenger
  3. Evaluate yesterday's predictions using pipeline.competition.shadow_evaluate
     (SAME methodology as s10_accuracy.py: ACTUAL wait_time_type, TIME_BUCKET,
      synthetic actuals fallback)
  4. Update registry metrics and shadow_days (only days with >= 1 matched pair)
  5. Auto-retire or flag for promotion at min_shadow_days
  6. Post daily comparison report to Discord (removal planned: Phase 4)

Everything runs on this machine. Defaults:
  pipeline base : ~/hazeydata/pipeline            (--pipeline-base / TPCR_PIPELINE_BASE)
  competition   : <pipeline base>/competition     (registry, shadow/, scores/, summaries/)
  repo          : ~/theme-park-crowd-report       (--tpcr-dir / TPCR_DIR)
  python        : <repo>/.venv/bin/python         (--python)

Usage:
    python scripts/rolling_shadow.py [--run-date YYYY-MM-DD]

IMPORTANT: Evaluation is delegated to shadow_evaluate.py in the TPCR repo,
which uses identical SQL to s10_accuracy.py. This script NEVER computes its
own error metrics — it only orchestrates and reports.

ARCHIVE FILE NAMING CONVENTION (Session 27):
===========================================================================
Archive files are named by REFERENCE DATE — the date the predictions are FOR.

  baseline_{reference_date}.parquet   → predictions for reference_date
  challenger_{reference_date}.parquet → predictions for reference_date

  Example: baseline_2026-04-05.parquet contains predictions FOR April 5.
  The filename tells you exactly what's inside.

  ARCHIVE step (run on Apr 4):
    reference_date = run_date + 1 = Apr 5
    Creates: baseline_2026-04-05.parquet (predictions FOR Apr 5)

  EVALUATE step (run on Apr 4):
    reference_date = run_date - 1 = Apr 3
    Opens:   baseline_2026-04-03.parquet (predictions FOR Apr 3)
    Compares against actuals from Apr 3

  To evaluate actuals for date Y, open the archive named Y:
    --reference-date Y opens baseline_Y.parquet
===========================================================================
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

# --- Configuration ---
ENV_PIPELINE_BASE = "TPCR_PIPELINE_BASE"
ENV_TPCR_DIR = "TPCR_DIR"

DISCORD_CHANNEL_ID = "1479351574177513576"
DISCORD_API = "https://discord.com/api/v10"

# Challengers that are never forecast, archived or scored: name -> reason.
RETIRED = {"xgb-dow": "in production via #470"}
# Registry statuses the orchestrator will run.
RUNNABLE_STATUSES = ("active", "shadow", "promotion_candidate")
# Legacy registry status -> current status.
LEGACY_STATUS = {"shadow": "active", "auto_retired": "retired"}


def default_paths(env=None):
    """Resolve (pipeline_base, tpcr_dir, python) from env, else home defaults."""
    env = os.environ if env is None else env
    pipeline_base = Path(env.get(ENV_PIPELINE_BASE) or Path.home() / "hazeydata" / "pipeline")
    tpcr_dir = Path(env.get(ENV_TPCR_DIR) or Path.home() / "theme-park-crowd-report")
    return pipeline_base, tpcr_dir, tpcr_dir / ".venv" / "bin" / "python"


# Module-level values are plain Paths; configure() re-points them (CLI / tests).
PIPELINE_BASE, TPCR_DIR, PYTHON = default_paths()
COMPETITION_BASE = PIPELINE_BASE / "competition"
SHADOW_DIR = COMPETITION_BASE / "shadow"
REGISTRY_PATH = COMPETITION_BASE / "challenger_registry.json"


def configure(pipeline_base=None, tpcr_dir=None, python=None):
    """Override paths. Unset values fall back to env / home defaults."""
    global PIPELINE_BASE, TPCR_DIR, PYTHON, COMPETITION_BASE, SHADOW_DIR, REGISTRY_PATH
    base, repo, py = default_paths()
    PIPELINE_BASE = Path(pipeline_base) if pipeline_base else base
    TPCR_DIR = Path(tpcr_dir) if tpcr_dir else repo
    PYTHON = Path(python) if python else TPCR_DIR / ".venv" / "bin" / "python"
    COMPETITION_BASE = PIPELINE_BASE / "competition"
    SHADOW_DIR = COMPETITION_BASE / "shadow"
    REGISTRY_PATH = COMPETITION_BASE / "challenger_registry.json"


def load_env():
    env_file = Path.home() / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                if key.strip() not in os.environ:
                    os.environ[key.strip()] = val.strip().strip('"')


def run_module(module, args, timeout):
    """Run `python -m module args...` in the TPCR repo. Returns CompletedProcess."""
    return subprocess.run(
        [str(PYTHON), "-m", module, *args],
        cwd=str(TPCR_DIR), capture_output=True, text=True, timeout=timeout,
    )


# --- Registry I/O ---

def normalize_registry(registry):
    """Bring registry entries to the spec shape. Idempotent."""
    registry.setdefault("retired", [])
    for entry in registry.get("challengers", []) + registry["retired"]:
        name = entry["name"]
        entry["id"] = name
        status = entry.get("status", "active")
        entry["status"] = LEGACY_STATUS.get(status, status)
        entry.setdefault("trained_at", None)
        entry.setdefault("entity_count", None)
        entry.setdefault("notes", "")
        if name in RETIRED:
            entry["status"] = "retired"
            entry["notes"] = RETIRED[name]
    return registry


def load_registry(path=None):
    path = Path(path or REGISTRY_PATH)
    try:
        return normalize_registry(json.loads(path.read_text()))
    except (OSError, ValueError):
        return None


def write_registry(registry, path=None):
    """Atomic write: tmp file in the same dir, then os.replace."""
    path = Path(path or REGISTRY_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(registry, indent=2))
    os.replace(tmp, path)


def post_discord(message):
    import requests  # lazy: Discord removal is Phase 4

    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        print("WARNING: No DISCORD_BOT_TOKEN")
        return
    # Discord limit is 2000 chars
    if len(message) > 1990:
        message = message[:1990] + "..."
    requests.post(
        f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages",
        json={"content": message},
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        timeout=15,
    )


# --- Step 1: Generate shadow forecasts ---

def generate_forecasts(challenger):
    """Run challenger forecast locally."""
    name = challenger["name"]
    module_name = challenger.get("module_name", name)
    print(f"  Forecasting: {name} (module: {module_name})...")

    try:
        result = run_module(
            "pipeline.competition.forecast_challenger",
            ["--challenger", module_name, "--output-base", str(PIPELINE_BASE)],
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        print("  FAILED: forecast timed out")
        return False
    if result.returncode != 0:
        print(f"  FAILED: {(result.stderr or result.stdout)[-200:]}")
        return False
    print("  " + "\n  ".join(result.stdout.strip().splitlines()[-3:]))
    return True


# --- Step 2: Archive predictions ---

def archive_predictions(challenger, run_date):
    """Archive 1-day-ahead predictions for baseline and challenger.

    Files are named by REFERENCE DATE (the date predictions are FOR):
        reference_date = run_date + 1
        baseline_{reference_date}.parquet   → contains predictions for reference_date
        challenger_{reference_date}.parquet  → contains predictions for reference_date

    Baseline path: {PIPELINE_BASE}/curves/forecast_parquet/all_forecasts.parquet
    Challenger path: {COMPETITION_BASE}/forecasts/{module_name}/all_forecasts_{module_name}.parquet
    """
    name = challenger["name"]
    module_name = challenger.get("module_name", name)

    reference_date = (run_date + timedelta(days=1)).isoformat()  # what the predictions are FOR

    shadow_path = SHADOW_DIR / name
    pairs = [
        ("challenger", COMPETITION_BASE / "forecasts" / module_name / f"all_forecasts_{module_name}.parquet",
         shadow_path / f"challenger_{reference_date}.parquet"),
        ("baseline", PIPELINE_BASE / "curves" / "forecast_parquet" / "all_forecasts.parquet",
         shadow_path / f"baseline_{reference_date}.parquet"),
    ]

    try:
        import duckdb

        con = duckdb.connect()
        shadow_path.mkdir(parents=True, exist_ok=True)
        for label, src, dst in pairs:
            con.execute(
                f"""
                COPY (
                    SELECT entity_code, park_date, time_slot, predicted_actual, prediction_method
                    FROM read_parquet('{src}')
                    WHERE park_date = '{reference_date}'
                ) TO '{dst}' (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )
            n, n_ent = con.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT entity_code) FROM read_parquet('{dst}')"
            ).fetchone()
            print(f"  Archived {label}: {n:,} preds, {n_ent} entities (for {reference_date})")
    except Exception as e:
        print(f"  Archive FAILED: {str(e)[-200:]}")
        return False
    return True


# --- Step 3: Evaluate using shadow_evaluate.py ---

def evaluate_previous_day(challenger, run_date):
    """Evaluate yesterday's predictions against actuals.

    The reference_date for evaluation = run_date - 1 (yesterday).
    We open baseline_{reference_date}.parquet which contains predictions
    FOR that date, and compare them against actuals from that date.

    DELEGATES to pipeline.competition.shadow_evaluate which uses the EXACT SAME
    methodology as s10_accuracy.py (ACTUAL wait_time_type, TIME_BUCKET with
    midpoint rounding, synthetic actuals fallback, entity-weighted MAE).

    No evaluation SQL lives in this script — it's all in the TPCR repo module.
    """
    name = challenger["name"]
    reference_date = (run_date - timedelta(days=1)).isoformat()  # yesterday = the date to evaluate

    try:
        result = run_module(
            "pipeline.competition.shadow_evaluate",
            ["--challenger", name,
             "--reference-date", reference_date,
             "--shadow-dir", str(SHADOW_DIR / name),
             "--output-base", str(PIPELINE_BASE)],
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        print("  Eval FAILED: timed out")
        return None
    if result.returncode != 0:
        print(f"  Eval FAILED: {(result.stderr or result.stdout)[-200:]}")
        return None

    output = result.stdout.strip()
    if "NO_DATA" in output or "NO_MATCH" in output:
        print(f"  {output}")
        return "no_data"

    for line in output.split("\n"):
        if line.startswith("RESULT:"):
            return json.loads(line[7:])
    return None


# --- Step 4: Update registry ---

def matched_pairs(metrics):
    """Number of (entity, slot) pairs behind an evaluation; 0 for no_data/None."""
    if not isinstance(metrics, dict):
        return 0
    return int(metrics.get("n_matched") or 0)


def update_registry(registry, challenger_name, metrics):
    """Update challenger metrics and check promotion/retirement criteria.

    shadow_days only counts evaluations with at least one matched pair.
    """
    settings = registry["settings"]

    for c in registry["challengers"]:
        if c["name"] != challenger_name:
            continue

        if matched_pairs(metrics) < 1:
            return "shadow"

        c["shadow_days"] = c.get("shadow_days", 0) + 1

        # Store daily metrics
        daily = c.get("daily_metrics", [])
        daily.append(metrics)
        c["daily_metrics"] = daily

        # Compute running averages
        all_base = [m["baseline_mae"] for m in daily if m.get("baseline_mae")]
        all_chal = [m["challenger_mae"] for m in daily if m.get("challenger_mae")]
        if all_base and all_chal:
            c["metrics"] = {
                "avg_baseline_mae": round(sum(all_base) / len(all_base), 2),
                "avg_challenger_mae": round(sum(all_chal) / len(all_chal), 2),
                "total_days_evaluated": len(daily),
            }

        # Check auto-retire / promotion at min_shadow_days
        if c["shadow_days"] >= settings["min_shadow_days"] and c.get("metrics"):
            avg_base = c["metrics"].get("avg_baseline_mae", 0)
            avg_chal = c["metrics"].get("avg_challenger_mae", 0)

            if avg_chal > avg_base + settings["auto_retire_if_worse_by"]:
                c["status"] = "retired"
                c["retired_reason"] = f"MAE {avg_chal:.2f} > baseline {avg_base:.2f} + {settings['auto_retire_if_worse_by']}"
                registry["retired"].append(c.copy())
                registry["challengers"] = [x for x in registry["challengers"] if x["name"] != challenger_name]
                return "retired"
            elif avg_chal < avg_base:
                c["status"] = "promotion_candidate"
                return "promotion_candidate"
            else:
                # Borderline — extend to 14 days
                if c["shadow_days"] >= 14:
                    c["status"] = "retired"
                    c["retired_reason"] = f"Borderline after 14 days: {avg_chal:.2f} vs {avg_base:.2f}"
                    registry["retired"].append(c.copy())
                    registry["challengers"] = [x for x in registry["challengers"] if x["name"] != challenger_name]
                    return "retired"

        return "shadow"
    return "not_found"


# --- Main ---

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Rolling competition shadow run (local)")
    p.add_argument("--pipeline-base", help="default: $TPCR_PIPELINE_BASE or ~/hazeydata/pipeline")
    p.add_argument("--tpcr-dir", help="default: $TPCR_DIR or ~/theme-park-crowd-report")
    p.add_argument("--python", help="default: <tpcr-dir>/.venv/bin/python")
    p.add_argument("--run-date", type=date.fromisoformat, help="YYYY-MM-DD (default: today)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configure(args.pipeline_base, args.tpcr_dir, args.python)
    load_env()
    run_date = args.run_date or date.today()

    archive_ref = (run_date + timedelta(days=1)).isoformat()
    eval_ref = (run_date - timedelta(days=1)).isoformat()

    print(f"Rolling Shadow Run — {run_date.isoformat()}")
    print(f"  Archive: predictions FOR {archive_ref} → baseline_{archive_ref}.parquet")
    print(f"  Evaluate: actuals FOR {eval_ref} vs baseline_{eval_ref}.parquet")
    print("=" * 50)

    for sub in ("shadow", "scores", "summaries"):
        (COMPETITION_BASE / sub).mkdir(parents=True, exist_ok=True)

    # Load registry
    registry = load_registry()
    if not registry:
        print("ERROR: Could not load challenger registry")
        sys.exit(1)

    active = [c for c in registry["challengers"]
              if c["name"] not in RETIRED and c["status"] in RUNNABLE_STATUSES]
    print(f"Active challengers: {len(active)}")

    if not active:
        write_registry(registry)  # persist normalization (e.g. retired xgb-dow)
        print("No active challengers — nothing to do")
        return

    report_lines = [f":bar_chart: **Rolling Shadow Report — {run_date.isoformat()}**\n"]
    report_lines.append(f"Active challengers: {len(active)}\n```")

    for challenger in active:
        name = challenger["name"]
        print(f"\n--- {name} (Day {challenger.get('shadow_days', 0) + 1}) ---")

        # Step 1: Generate forecasts
        forecast_ok = generate_forecasts(challenger)
        if not forecast_ok:
            report_lines.append(f"{name}: FORECAST FAILED")
            continue

        # Step 2: Archive predictions (named by reference_date = run_date + 1)
        archive_ok = archive_predictions(challenger, run_date)
        if not archive_ok:
            report_lines.append(f"{name}: ARCHIVE FAILED")
            continue

        # Step 3: Evaluate previous day (reference_date = run_date - 1)
        metrics = evaluate_previous_day(challenger, run_date)

        # Step 4: Update registry
        status = update_registry(registry, name, metrics)

        # Build report line
        day = challenger.get("shadow_days", 0)
        if matched_pairs(metrics) >= 1:
            b = metrics["baseline_mae"]
            c_mae = metrics["challenger_mae"]
            delta = b - c_mae
            winner = "+" if c_mae < b else "-"
            n_ent = metrics.get("n_entities", "?")
            report_lines.append(
                f"{name} Day {day}: base={b:.1f} chal={c_mae:.1f} "
                f"delta={delta:+.1f} [{winner}] entities={n_ent}"
            )
        else:
            report_lines.append(f"{name} Day {day}: no comparison data yet")

        if status == "retired":
            report_lines.append(f"  >> AUTO-RETIRED: {challenger.get('retired_reason', '?')}")
        elif status == "promotion_candidate":
            report_lines.append("  >> PROMOTION CANDIDATE — flagged for review")

    report_lines.append("```")

    # Save updated registry
    write_registry(registry)
    print("\nRegistry updated.")

    # Post to Discord (Phase 4 removes this)
    try:
        post_discord("\n".join(report_lines))
        print("Report posted to Discord.")
    except Exception as e:
        print(f"WARNING: Discord post failed: {e}")


if __name__ == "__main__":
    main()
