#!/usr/bin/env python3
"""Rolling Competition Shadow Run — Daily challenger orchestration.

Runs after baseline pipeline completes (~7:00 AM ET).
For each active challenger in the registry:
  1. Generate shadow predictions via SSH to wilma-server
  2. Archive 1-day-ahead predictions for both baseline and challenger
  3. Evaluate yesterday's predictions using pipeline.competition.shadow_evaluate
     (SAME methodology as s10_accuracy.py: ACTUAL wait_time_type, TIME_BUCKET,
      synthetic actuals fallback)
  4. Update registry metrics and shadow_days
  5. Auto-retire or flag for promotion at Day 7+
  6. Post daily comparison report to Discord

Cron: 0 7 * * * python3.12 rolling_shadow.py

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

import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

# --- Configuration ---
SSH_HOST = "wilma@192.168.2.75"
PIPELINE_BASE = "/home/wilma/hazeydata/pipeline"
SHADOW_DIR = f"{PIPELINE_BASE}/competition/shadow"
REGISTRY_PATH = f"{PIPELINE_BASE}/competition/challenger_registry.json"
TPCR_DIR = "/home/wilma/theme-park-crowd-report"
DISCORD_CHANNEL_ID = "1479351574177513576"
DISCORD_API = "https://discord.com/api/v10"


def load_env():
    env_file = Path.home() / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                if key.strip() not in os.environ:
                    os.environ[key.strip()] = val.strip().strip('"')


def ssh_run(cmd, timeout=300):
    result = subprocess.run(
        ["ssh", SSH_HOST, cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return result


def ssh_read_json(path):
    result = ssh_run(f"cat '{path}'", timeout=15)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def ssh_write_json(path, data):
    payload = json.dumps(data, indent=2)
    result = ssh_run(f"cat > '{path}' << 'JSONEOF'\n{payload}\nJSONEOF", timeout=15)
    return result.returncode == 0


def post_discord(message):
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
    """Run challenger forecast on wilma-server."""
    name = challenger["name"]
    module_name = challenger.get("module_name", name)
    print(f"  Forecasting: {name} (module: {module_name})...")

    cmd = (
        f"cd {TPCR_DIR} && source .venv/bin/activate && "
        f"python -m pipeline.competition.forecast_challenger "
        f"--challenger {module_name} "
        f"--output-base {PIPELINE_BASE} 2>&1 | tail -3"
    )
    result = ssh_run(cmd, timeout=300)
    if result.returncode != 0:
        print(f"  FAILED: {result.stderr[-200:]}")
        return False
    print(f"  {result.stdout.strip()}")
    return True


# --- Step 2: Archive predictions ---

def archive_predictions(challenger, run_date):
    """Archive 1-day-ahead predictions for baseline and challenger.

    Files are named by REFERENCE DATE (the date predictions are FOR):
        reference_date = run_date + 1
        baseline_{reference_date}.parquet   → contains predictions for reference_date
        challenger_{reference_date}.parquet  → contains predictions for reference_date

    Baseline path: {PIPELINE_BASE}/curves/forecast_parquet/all_forecasts.parquet
    Challenger path: {PIPELINE_BASE}/competition/forecasts/{module_name}/all_forecasts_{module_name}.parquet
    """
    name = challenger["name"]
    module_name = challenger.get("module_name", name)

    reference_date = (run_date + timedelta(days=1)).isoformat()  # what the predictions are FOR

    shadow_path = f"{SHADOW_DIR}/{name}"
    chal_src = f"{PIPELINE_BASE}/competition/forecasts/{module_name}/all_forecasts_{module_name}.parquet"
    chal_dst = f"{shadow_path}/challenger_{reference_date}.parquet"
    base_src = f"{PIPELINE_BASE}/curves/forecast_parquet/all_forecasts.parquet"
    base_dst = f"{shadow_path}/baseline_{reference_date}.parquet"

    script = f"""
import duckdb
from pathlib import Path

con = duckdb.connect()
Path("{shadow_path}").mkdir(parents=True, exist_ok=True)

con.execute(\"\"\"
    COPY (
        SELECT entity_code, park_date, time_slot, predicted_actual, prediction_method
        FROM read_parquet('{chal_src}')
        WHERE park_date = '{reference_date}'
    ) TO '{chal_dst}' (FORMAT PARQUET, COMPRESSION ZSTD)
\"\"\")
stats = con.execute("SELECT COUNT(*), COUNT(DISTINCT entity_code) FROM read_parquet('{chal_dst}')").fetchone()
print(f"challenger: {{stats[0]:,}} preds, {{stats[1]}} entities (for {reference_date})")

con.execute(\"\"\"
    COPY (
        SELECT entity_code, park_date, time_slot, predicted_actual, prediction_method
        FROM read_parquet('{base_src}')
        WHERE park_date = '{reference_date}'
    ) TO '{base_dst}' (FORMAT PARQUET, COMPRESSION ZSTD)
\"\"\")
stats2 = con.execute("SELECT COUNT(*), COUNT(DISTINCT entity_code) FROM read_parquet('{base_dst}')").fetchone()
print(f"baseline: {{stats2[0]:,}} preds, {{stats2[1]}} entities (for {reference_date})")
"""

    result = ssh_run(f"python3 << 'PYEOF'\n{script}\nPYEOF", timeout=60)
    if result.returncode != 0:
        print(f"  Archive FAILED: {result.stderr[-200:]}")
        return False
    print(f"  Archived: {result.stdout.strip()}")
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

    shadow_path = f"{SHADOW_DIR}/{name}"

    cmd = (
        f"cd {TPCR_DIR} && source .venv/bin/activate && "
        f"python -m pipeline.competition.shadow_evaluate "
        f"--challenger {name} "
        f"--reference-date {reference_date} "
        f"--shadow-dir {shadow_path} "
        f"--output-base {PIPELINE_BASE} "
        f"2>&1"
    )

    result = ssh_run(cmd, timeout=120)
    if result.returncode != 0:
        print(f"  Eval FAILED: {result.stderr[-200:]}")
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

def update_registry(registry, challenger_name, metrics):
    """Update challenger metrics and check promotion/retirement criteria."""
    settings = registry["settings"]

    for c in registry["challengers"]:
        if c["name"] != challenger_name:
            continue

        c["shadow_days"] = c.get("shadow_days", 0) + 1

        if metrics and metrics != "no_data":
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
                c["status"] = "auto_retired"
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
                    c["status"] = "auto_retired"
                    c["retired_reason"] = f"Borderline after 14 days: {avg_chal:.2f} vs {avg_base:.2f}"
                    registry["retired"].append(c.copy())
                    registry["challengers"] = [x for x in registry["challengers"] if x["name"] != challenger_name]
                    return "retired"

        return "shadow"
    return "not_found"


# --- Main ---

def main():
    load_env()
    run_date = date.today()

    archive_ref = (run_date + timedelta(days=1)).isoformat()
    eval_ref = (run_date - timedelta(days=1)).isoformat()

    print(f"Rolling Shadow Run — {run_date.isoformat()}")
    print(f"  Archive: predictions FOR {archive_ref} → baseline_{archive_ref}.parquet")
    print(f"  Evaluate: actuals FOR {eval_ref} vs baseline_{eval_ref}.parquet")
    print("=" * 50)

    # Load registry
    registry = ssh_read_json(REGISTRY_PATH)
    if not registry:
        print("ERROR: Could not load challenger registry")
        sys.exit(1)

    active = [c for c in registry["challengers"] if c["status"] in ("shadow", "promotion_candidate")]
    print(f"Active challengers: {len(active)}")

    if not active:
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
        if metrics and metrics != "no_data" and metrics is not None:
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
            report_lines.append(f"  >> PROMOTION CANDIDATE — flagged for review")

    report_lines.append("```")

    # Save updated registry
    ssh_write_json(REGISTRY_PATH, registry)
    print("\nRegistry updated.")

    # Post to Discord
    post_discord("\n".join(report_lines))
    print("Report posted to Discord.")


if __name__ == "__main__":
    main()
