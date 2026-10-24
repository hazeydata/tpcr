#!/usr/bin/env python3
"""Rolling Competition Shadow Run — Daily challenger orchestration (local).

Runs after the baseline pipeline completes. For each active challenger in the
registry:
  0. Retrain on every production-eligible entity (pipeline.competition.train_challenger)
     when the model dir is missing, trained_at is older than 7 days, or --retrain
     is given. fallback-v2 has no model and is never retrained.
  1. Generate shadow predictions (pipeline.competition.forecast_challenger;
     fallback-v2 is dispatched to pipeline.competition.fallback_v2)
  2. Archive 1-day-ahead predictions for both baseline and challenger
  3. Evaluate yesterday's predictions using pipeline.competition.shadow_evaluate
     (SAME methodology as s10_accuracy.py: ACTUAL wait_time_type, TIME_BUCKET,
      synthetic actuals fallback), on the identical (entity, slot) set
  4. Update registry metrics and shadow_days (only days with >= 1 matched pair)
  5. Auto-retire or flag for promotion at min_shadow_days
  6. Write scores/YYYY-MM-DD.parquet and summaries/YYYY-MM-DD.json (atomic)
  7. Post daily comparison report to Discord (removal planned: Phase 4)

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
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.competition.io_utils import atomic_write_json, atomic_write_parquet  # noqa: E402

# --- Configuration ---
ENV_PIPELINE_BASE = "TPCR_PIPELINE_BASE"
ENV_TPCR_DIR = "TPCR_DIR"

# fallback-v2 is dispatched to pipeline.competition.fallback_v2 (no model, no retrain).
FALLBACK_V2 = "fallback-v2"
# Challengers scored mainly on fallback entities (name -> score_scope).
SCORE_SCOPES = {FALLBACK_V2: "fallback"}
RETRAIN_MAX_AGE_DAYS = 7
RETRAIN_TIMEOUT = 7200

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


def score_scope(challenger):
    """'fallback' for fallback-v2, else 'all'. A registry `score_scope` field wins."""
    return challenger.get("score_scope") or SCORE_SCOPES.get(challenger["name"], "all")


def register_fallback_v2(registry):
    """Add the fallback-v2 entry if missing. Returns True when it was added."""
    known = {c["name"] for c in registry["challengers"] + registry["retired"]}
    if FALLBACK_V2 in known:
        return False
    registry["challengers"].append({
        "name": FALLBACK_V2, "id": FALLBACK_V2, "status": "active", "shadow_days": 0,
        "score_scope": SCORE_SCOPES[FALLBACK_V2], "trained_at": None, "entity_count": None,
        "notes": "TPCR-S8-FALLBACK-001 Phases 2-3 as a challenger; no model, never retrained",
    })
    return True


# --- Step 0: Retrain on every production-eligible entity ---

def models_dir(challenger):
    return COMPETITION_BASE / "models" / challenger.get("module_name", challenger["name"])


def parse_timestamp(value):
    """ISO timestamp -> aware datetime (naive treated as UTC); None if unparseable."""
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def needs_retrain(challenger, force=False, now=None):
    """Reason string if a retrain is due, else None.

    Due when forced, the model dir is missing or empty, trained_at is absent, or
    trained_at is older than RETRAIN_MAX_AGE_DAYS. fallback-v2 has no model.
    """
    if challenger["name"] == FALLBACK_V2:
        return None
    if force:
        return "forced (--retrain)"
    mdir = models_dir(challenger)
    if not mdir.is_dir() or not any(mdir.iterdir()):
        return "model dir missing"
    trained = parse_timestamp(challenger.get("trained_at"))
    if trained is None:
        return "never trained"
    now = now or datetime.now(timezone.utc)
    if now - trained > timedelta(days=RETRAIN_MAX_AGE_DAYS):
        return f"trained_at older than {RETRAIN_MAX_AGE_DAYS} days"
    return None


def retrain_challenger(challenger, run_date):
    """Run train_challenger on the full eligible set. Returns its RESULT dict or None."""
    name = challenger["name"]
    module_name = challenger.get("module_name", name)
    print(f"  Retraining: {name} (module: {module_name})...")
    try:
        result = run_module(
            "pipeline.competition.train_challenger",
            ["--challenger", module_name, "--output-base", str(PIPELINE_BASE),
             "--run-date", run_date.isoformat()],
            timeout=RETRAIN_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"  Retrain FAILED: {type(e).__name__}: {e}")
        return None
    if result.returncode != 0:
        print(f"  Retrain FAILED: {(result.stderr or result.stdout)[-200:]}")
        return None
    for line in result.stdout.splitlines():
        if line.startswith("RESULT:"):
            return json.loads(line[7:])
    print("  Retrain FAILED: no RESULT line")
    return None


def production_trained_entities(run_date, base=None):
    """Entity codes production S7 trained in the run for run_date, or None if not found.

    S7 writes models/<entity>/metadata_baseline.json (with trained_at) per trained
    entity. The run's set is every entity whose trained_at falls on run_date (UTC).
    """
    mroot = (base or PIPELINE_BASE) / "models"
    if not mroot.is_dir():
        return None
    trained = set()
    for meta in mroot.glob("*/metadata_baseline.json"):
        try:
            ts = parse_timestamp(json.loads(meta.read_text()).get("trained_at"))
        except (OSError, ValueError, AttributeError):
            continue
        if ts is not None and ts.astimezone(timezone.utc).date() == run_date:
            trained.add(meta.parent.name)
    return trained or None


def record_training(challenger, result, run_date, base=None):
    """Write trained_at / entity_count to the registry entry.

    Returns an error string when the challenger's trained entity set differs from
    the production S7 trained set of the same run (the diff is printed), or when
    that set cannot be found; else None.
    """
    challenger["trained_at"] = result["trained_at"]
    challenger["entity_count"] = result["entity_count"]
    name = challenger["name"]
    prod = production_trained_entities(run_date, base)
    if prod is None:
        print(f"  No production S7 trained set found for {run_date}")
        return f"{name}: production S7 trained set not found for {run_date}"
    trained = set(result.get("trained_entities") or [])
    if trained == prod:
        return None
    missing, extra = sorted(prod - trained), sorted(trained - prod)
    skipped = result.get("skipped") or {}
    detail = ", ".join(f"{e}={r}" for e, r in sorted(skipped.items())) or "none recorded"
    print(f"  entity set != production S7 trained set ({len(trained)} vs {len(prod)}); "
          f"missing: {missing}; extra: {extra}; skipped: {detail}")
    return (f"{name}: entity set != production S7 trained set for {run_date} "
            f"(missing {missing}, extra {extra}; skipped: {detail})")


# --- Step 1: Generate shadow forecasts ---

def generate_forecasts(challenger):
    """Run challenger forecast locally (fallback-v2 goes to its own module)."""
    name = challenger["name"]
    module_name = challenger.get("module_name", name)
    print(f"  Forecasting: {name} (module: {module_name})...")

    if name == FALLBACK_V2:
        module, args = "pipeline.competition.fallback_v2", ["--output-base", str(PIPELINE_BASE)]
    else:
        module = "pipeline.competition.forecast_challenger"
        args = ["--challenger", module_name, "--output-base", str(PIPELINE_BASE)]

    try:
        result = run_module(module, args, timeout=900 if name == FALLBACK_V2 else 300)
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

def staging_path(name, eval_date, base=None):
    """Per-challenger score rows for one eval date, merged into scores/ at the end of the run.

    base: root holding scores/ (default: the competition dir; shadow_job passes its stage dir).
    """
    return Path(base or COMPETITION_BASE) / "scores" / f".staging-{eval_date}" / f"{name}.parquet"


def evaluate_previous_day(challenger, run_date, out_base=None):
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

    scores_out = staging_path(name, reference_date, out_base)
    scores_out.parent.mkdir(parents=True, exist_ok=True)
    scores_out.unlink(missing_ok=True)

    try:
        result = run_module(
            "pipeline.competition.shadow_evaluate",
            ["--challenger", name,
             "--reference-date", reference_date,
             "--shadow-dir", str(SHADOW_DIR / name),
             "--output-base", str(PIPELINE_BASE),
             "--scores-out", str(scores_out)],
            timeout=300,
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


def update_registry(registry, challenger_name, metrics, eval_date=None):
    """Update challenger metrics and check promotion/retirement criteria.

    shadow_days only counts evaluations with at least one matched pair.
    With eval_date, daily_metrics is keyed by date: a rerun for the same date
    replaces that day's entry and does not increment shadow_days again.
    """
    settings = registry["settings"]

    for c in registry["challengers"]:
        if c["name"] != challenger_name:
            continue

        if matched_pairs(metrics) < 1:
            return "shadow"

        # Store daily metrics (one entry per date when eval_date is given)
        daily = c.get("daily_metrics", [])
        if eval_date is None:
            c["shadow_days"] = c.get("shadow_days", 0) + 1
            daily.append(metrics)
        else:
            seen = any(m.get("date") == eval_date for m in daily)
            daily = [m for m in daily if m.get("date") != eval_date]
            daily.append(dict(metrics, date=eval_date))
            if not seen:
                c["shadow_days"] = c.get("shadow_days", 0) + 1
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


def registry_metrics(metrics, scope):
    """Metrics the registry should track for a challenger's score scope.

    'all': unchanged. 'fallback': baseline_mae / challenger_mae become the
    fallback-entity MAEs (all-entity values kept as all_entity_*), and a day with
    no fallback entities counts as no matched pair.
    """
    if scope != "fallback" or not isinstance(metrics, dict):
        return metrics
    out = dict(metrics, all_entity_baseline_mae=metrics.get("baseline_mae"),
               all_entity_challenger_mae=metrics.get("challenger_mae"))
    if metrics.get("mae_fallback_prod") is None or metrics.get("mae_fallback_chal") is None:
        out["n_matched"] = 0
        return out
    out["baseline_mae"] = metrics["mae_fallback_prod"]
    out["challenger_mae"] = metrics["mae_fallback_chal"]
    return out


# --- Step 5: Daily scores + summary ---

def challenger_summary(challenger, metrics):
    """One challenger's block of the daily summary."""
    entry = {"score_scope": score_scope(challenger),
             "trained_at": challenger.get("trained_at"),
             "entity_count": challenger.get("entity_count")}
    if matched_pairs(metrics) < 1:
        return dict(entry, matched_pairs=0, status="no_data")
    return dict(
        entry,
        matched_pairs=matched_pairs(metrics),
        n_entities=metrics.get("n_entities"),
        coverage_pct=metrics.get("coverage_pct"),
        mae_prod=metrics.get("baseline_mae"),
        mae_chal=metrics.get("challenger_mae"),
        mae_fallback_prod=metrics.get("mae_fallback_prod"),
        mae_fallback_chal=metrics.get("mae_fallback_chal"),
        n_fallback_entities=metrics.get("n_fallback_entities"),
        dropped=metrics.get("dropped", {}),
        forecast_entities=metrics.get("forecast_entities"),
    )


def write_day_outputs(eval_date, results, errors, out_base=None):
    """Write scores/<date>.parquet and summaries/<date>.json (atomic, overwrite).

    out_base: root for scores/ and summaries/ (default: the competition dir).

    results: [(challenger, metrics)] for the challengers that ran. The scores
    file holds every challenger's rows for the date and replaces any earlier
    file for that date; it is left untouched when no challenger produced rows.
    """
    blocks = {c["name"]: challenger_summary(c, m) for c, m in results}
    scored = [b for b in blocks.values() if b["matched_pairs"] >= 1]

    out_base = Path(out_base or COMPETITION_BASE)
    staged = [staging_path(c["name"], eval_date, out_base) for c, _ in results]
    staged = [p for p in staged if p.exists()]
    if staged:
        import pandas as pd

        scores = pd.concat([pd.read_parquet(p) for p in staged], ignore_index=True)
        atomic_write_parquet(scores, out_base / "scores" / f"{eval_date}.parquet")

    summary = {
        "date": eval_date,
        "matched_pairs": max((b["matched_pairs"] for b in scored), default=0),
        "forecast_entities": max((b.get("forecast_entities") or 0 for b in scored), default=0),
        # worst coverage across scored challengers
        "coverage_pct": min((b.get("coverage_pct") or 0.0 for b in scored), default=0.0),
        "challengers": blocks,
        "run_status": "failed" if errors else "ok",
        "errors": list(errors),
    }
    atomic_write_json(out_base / "summaries" / f"{eval_date}.json", summary)
    shutil.rmtree(out_base / "scores" / f".staging-{eval_date}", ignore_errors=True)
    return summary


# --- Main ---

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Rolling competition shadow run (local)")
    p.add_argument("--pipeline-base", help="default: $TPCR_PIPELINE_BASE or ~/hazeydata/pipeline")
    p.add_argument("--tpcr-dir", help="default: $TPCR_DIR or ~/theme-park-crowd-report")
    p.add_argument("--python", help="default: <tpcr-dir>/.venv/bin/python")
    p.add_argument("--run-date", type=date.fromisoformat, help="YYYY-MM-DD (default: today)")
    p.add_argument("--retrain", action="store_true",
                   help="retrain every active challenger now (default: only when the model dir "
                        f"is missing or trained_at is older than {RETRAIN_MAX_AGE_DAYS} days)")
    p.add_argument("--register-fallback-v2", action="store_true",
                   help="add the fallback-v2 challenger to the registry if it is missing")
    return p.parse_args(argv)


class RegistryError(RuntimeError):
    """The challenger registry is missing or unreadable."""


def run_day(run_date, retrain=False, register_fallback=False, out_base=None, post=True):
    """Run one day. Returns the day's summary dict (None when no challenger is active).

    out_base: where the registry, scores/ and summaries/ are written. Default is
    the competition dir (the CLI behavior). shadow_job passes a staging dir and
    commits it itself. Models, forecasts and shadow/ archives are always written
    in place; they are keyed by date or challenger and are overwritten on rerun.
    post: post the report to Discord (shadow_job never does; removal is Phase 4).
    """
    out_base = Path(out_base) if out_base else COMPETITION_BASE
    registry_out = out_base / REGISTRY_PATH.name

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
        raise RegistryError("Could not load challenger registry")

    if register_fallback and register_fallback_v2(registry):
        print(f"Registered {FALLBACK_V2}")

    active = [c for c in registry["challengers"]
              if c["name"] not in RETIRED and c["status"] in RUNNABLE_STATUSES]
    print(f"Active challengers: {len(active)}")

    if not active:
        write_registry(registry, registry_out)  # persist normalization (e.g. retired xgb-dow)
        print("No active challengers — nothing to do")
        return None

    report_lines = [f":bar_chart: **Rolling Shadow Report — {run_date.isoformat()}**\n"]
    report_lines.append(f"Active challengers: {len(active)}\n```")

    errors = []
    results = []

    for challenger in active:
        name = challenger["name"]
        print(f"\n--- {name} (Day {challenger.get('shadow_days', 0) + 1}) ---")

        # Step 0: Retrain on every production-eligible entity when due
        reason = needs_retrain(challenger, retrain)
        if reason:
            print(f"  Retrain due: {reason}")
            train_result = retrain_challenger(challenger, run_date)
            if train_result is None:
                errors.append(f"{name}: retrain failed")
                report_lines.append(f"{name}: RETRAIN FAILED")
                continue
            mismatch = record_training(challenger, train_result, run_date)
            if mismatch:
                errors.append(mismatch)
            write_registry(registry, registry_out)

        # Step 1: Generate forecasts
        forecast_ok = generate_forecasts(challenger)
        if not forecast_ok:
            errors.append(f"{name}: forecast failed")
            report_lines.append(f"{name}: FORECAST FAILED")
            continue

        # Step 2: Archive predictions (named by reference_date = run_date + 1)
        archive_ok = archive_predictions(challenger, run_date)
        if not archive_ok:
            errors.append(f"{name}: archive failed")
            report_lines.append(f"{name}: ARCHIVE FAILED")
            continue

        # Step 3: Evaluate previous day (reference_date = run_date - 1)
        metrics = evaluate_previous_day(challenger, run_date, out_base)
        if metrics is None:
            errors.append(f"{name}: evaluation failed")
        results.append((challenger, metrics))

        # Step 4: Update registry (fallback-scope challengers are tracked on fallback MAE)
        scoped = registry_metrics(metrics, score_scope(challenger))
        status = update_registry(registry, name, scoped, eval_ref)

        # Build report line
        day = challenger.get("shadow_days", 0)
        if matched_pairs(scoped) >= 1:
            b = scoped["baseline_mae"]
            c_mae = scoped["challenger_mae"]
            delta = b - c_mae
            winner = "+" if c_mae < b else "-"
            n_ent = scoped.get("n_entities", "?")
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
    write_registry(registry, registry_out)
    print("\nRegistry updated.")

    # Daily scores + summary for the evaluated date
    summary = write_day_outputs(eval_ref, results, errors, out_base)
    print(f"Wrote scores/summary for {eval_ref}: run_status={summary['run_status']}, "
          f"matched_pairs={summary['matched_pairs']}, coverage={summary['coverage_pct']}%")
    for err in errors:
        print(f"  ERROR: {err}")

    # Post to Discord (Phase 4 removes this)
    if post:
        try:
            post_discord("\n".join(report_lines))
            print("Report posted to Discord.")
        except Exception as e:
            print(f"WARNING: Discord post failed: {e}")
    return summary


def main(argv=None):
    args = parse_args(argv)
    configure(args.pipeline_base, args.tpcr_dir, args.python)
    load_env()
    try:
        run_day(args.run_date or date.today(), retrain=args.retrain,
                register_fallback=args.register_fallback_v2)
    except RegistryError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
