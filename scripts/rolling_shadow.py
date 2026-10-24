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
  5. Write scores/YYYY-MM-DD.parquet and summaries/YYYY-MM-DD.json (atomic),
     with per-challenger win / streak / bootstrap CI / gain_min / flagged
  6. Apply the flag rule (pipeline.competition.flag_rule) to the registry status.
     A flag only sets registry status and a summary field; nothing is promoted.

Nothing is posted anywhere.

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
from pipeline.competition import flag_rule  # noqa: E402
from pipeline.competition.flag_rule import (  # noqa: E402,F401
    BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, MIN_GAIN_MIN, MIN_STREAK,
)
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

# Challengers that are never forecast, archived or scored: name -> reason.
RETIRED = {"xgb-dow": "in production via #470"}
# Registry statuses the orchestrator will run.
RUNNABLE_STATUSES = ("active", "shadow", "flagged")
# Legacy registry status -> current status. The old promotion_candidate flag came
# from a different rule (average MAE below baseline), so it is not carried over.
LEGACY_STATUS = {"shadow": "active", "auto_retired": "retired", "promotion_candidate": "active"}
STATUS_FLAGGED = "flagged"


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
    """Update challenger metrics. Returns "active" or "not_found".

    No retire/promote decision is made here; flagging is apply_flags (flag rule).
    shadow_days only counts evaluations with at least one matched pair.
    With eval_date, daily_metrics is keyed by date: a rerun for the same date
    replaces that day's entry and does not increment shadow_days again.
    """
    for c in registry["challengers"]:
        if c["name"] != challenger_name:
            continue

        if matched_pairs(metrics) < 1:
            return "active"

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

        return "active"
    return "not_found"


def apply_flags(registry, summary):
    """Set registry status from the day's flag decisions. Returns the flagged ids.

    A challenger that is active or flagged becomes (or stays) `flagged` when its
    summary block says flagged, and goes back to `active` when it does not (also
    when it has no block that day). Retired challengers are never touched.
    Changes the registry only; nothing is promoted.
    """
    blocks = summary.get("challengers") or {}
    flagged = []
    for c in registry["challengers"]:
        if c["name"] in RETIRED or c.get("status") not in ("active", STATUS_FLAGGED):
            continue
        if (blocks.get(c["name"]) or {}).get("flagged") is True:
            if c["status"] != STATUS_FLAGGED:
                c["flagged_on"] = summary.get("date")
            c["status"] = STATUS_FLAGGED
            flagged.append(c["name"])
        else:
            c["status"] = "active"
            c.pop("flagged_on", None)
    return flagged


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

NO_FLAG_FIELDS = {"win": False, "streak": 0, "ci95_low": None, "ci95_high": None,
                  "gain_min": None, "flagged": False}


def load_window_scores(name, eval_date, out_base, days=MIN_STREAK):
    """Pooled score rows for one challenger over the last `days` dates ending at eval_date.

    The eval date's rows come from out_base (the file just written; the staging dir
    under shadow_job). Earlier dates come from the committed competition scores/.
    Dates without a scores file are skipped. Returns a DataFrame (maybe empty).
    """
    import pandas as pd

    end = date.fromisoformat(eval_date)
    frames = []
    for back in range(days):
        day = (end - timedelta(days=back)).isoformat()
        path = Path(out_base if back == 0 else COMPETITION_BASE) / "scores" / f"{day}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        frames.append(df[df["challenger_id"] == name])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_summary(day):
    """A committed summary for an earlier date, or None when missing/unreadable."""
    try:
        return json.loads((COMPETITION_BASE / "summaries" / f"{day}.json").read_text())
    except (OSError, ValueError):
        return None


def flag_fields(name, block, eval_date, run_ok, out_base):
    """win / streak / ci95_low / ci95_high / gain_min / flagged for one challenger-day.

    win and gain_min use the scope's MAEs (fallback entities for fallback-v2).
    The bootstrap pools the matched pairs of the last MIN_STREAK dates (scores/*.parquet,
    scope-filtered) so a flag always rests on the same 7 days as the streak. A failed
    run (run_ok False) is never a win, so it breaks the streak and cannot flag.
    """
    if block.get("matched_pairs", 0) < 1:
        return dict(NO_FLAG_FIELDS)
    fallback = block.get("score_scope") == "fallback"
    mae_prod = block.get("mae_fallback_prod" if fallback else "mae_prod")
    mae_chal = block.get("mae_fallback_chal" if fallback else "mae_chal")
    win = run_ok and flag_rule.is_win(mae_prod, mae_chal)
    gain = flag_rule.gain_min(mae_prod, mae_chal)
    streak = flag_rule.streak_length(eval_date, win, load_summary, name)

    pool = load_window_scores(name, eval_date, out_base)
    if fallback and len(pool):
        pool = pool[pool["is_fallback_entity"].astype(bool)]
    low = high = None
    if len(pool):
        gains = flag_rule.per_entity_gain(pool["entity_code"], pool["actual"],
                                          pool["prod_pred"], pool["chal_pred"])
        low, high = flag_rule.bootstrap_ci(gains, BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED)
    return {
        "win": bool(win), "streak": streak,
        "ci95_low": None if low is None else round(low, 6),
        "ci95_high": None if high is None else round(high, 6),
        "gain_min": None if gain is None else round(gain, 6),
        "flagged": flag_rule.should_flag(win, streak, low, gain),
    }


def challenger_summary(challenger, metrics):
    """One challenger's block of the daily summary."""
    entry = {"score_scope": score_scope(challenger),
             "trained_at": challenger.get("trained_at"),
             "entity_count": challenger.get("entity_count")}
    if matched_pairs(metrics) < 1:
        return dict(entry, matched_pairs=0, status="no_data", **NO_FLAG_FIELDS)
    return dict(
        entry,
        **NO_FLAG_FIELDS,
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

    Each challenger block gets win, streak, ci95_low, ci95_high, gain_min and
    flagged (flag rule); the summary gets significant_gain and
    significant_gain_text. Earlier days' scores and summaries are read from the
    committed competition dir. The registry status is applied by the caller.
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

    run_ok = not errors
    for name, block in blocks.items():
        block.update(flag_fields(name, block, eval_date, run_ok, out_base))
    flagged = [name for name, block in blocks.items() if block["flagged"]]

    summary = {
        "date": eval_date,
        "matched_pairs": max((b["matched_pairs"] for b in scored), default=0),
        "forecast_entities": max((b.get("forecast_entities") or 0 for b in scored), default=0),
        # worst coverage across scored challengers
        "coverage_pct": min((b.get("coverage_pct") or 0.0 for b in scored), default=0.0),
        "challengers": blocks,
        "significant_gain": flagged,
        "significant_gain_text": flag_rule.significant_gain_text(flagged),
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


def run_day(run_date, retrain=False, register_fallback=False, out_base=None):
    """Run one day. Returns the day's summary dict (None when no challenger is active).

    out_base: where the registry, scores/ and summaries/ are written. Default is
    the competition dir (the CLI behavior). shadow_job passes a staging dir and
    commits it itself. Models, forecasts and shadow/ archives are always written
    in place; they are keyed by date or challenger and are overwritten on rerun.
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
                continue
            mismatch = record_training(challenger, train_result, run_date)
            if mismatch:
                errors.append(mismatch)
            write_registry(registry, registry_out)

        # Step 1: Generate forecasts
        forecast_ok = generate_forecasts(challenger)
        if not forecast_ok:
            errors.append(f"{name}: forecast failed")
            continue

        # Step 2: Archive predictions (named by reference_date = run_date + 1)
        archive_ok = archive_predictions(challenger, run_date)
        if not archive_ok:
            errors.append(f"{name}: archive failed")
            continue

        # Step 3: Evaluate previous day (reference_date = run_date - 1)
        metrics = evaluate_previous_day(challenger, run_date, out_base)
        if metrics is None:
            errors.append(f"{name}: evaluation failed")
        results.append((challenger, metrics))

        # Step 4: Update registry (fallback-scope challengers are tracked on fallback MAE)
        scoped = registry_metrics(metrics, score_scope(challenger))
        update_registry(registry, name, scoped, eval_ref)

        # Console line (log only)
        day = challenger.get("shadow_days", 0)
        if matched_pairs(scoped) >= 1:
            b = scoped["baseline_mae"]
            c_mae = scoped["challenger_mae"]
            print(f"  {name} Day {day}: base={b:.1f} chal={c_mae:.1f} delta={b - c_mae:+.1f} "
                  f"[{'+' if c_mae < b else '-'}] entities={scoped.get('n_entities', '?')}")
        else:
            print(f"  {name} Day {day}: no comparison data yet")

    # Daily scores + summary for the evaluated date (includes the flag decisions)
    summary = write_day_outputs(eval_ref, results, errors, out_base)
    print(f"Wrote scores/summary for {eval_ref}: run_status={summary['run_status']}, "
          f"matched_pairs={summary['matched_pairs']}, coverage={summary['coverage_pct']}%")
    for err in errors:
        print(f"  ERROR: {err}")

    # Flag rule -> registry status (changes nothing in production), then save the registry
    apply_flags(registry, summary)
    write_registry(registry, registry_out)
    print(f"Registry updated. {summary['significant_gain_text']}")
    return summary


def main(argv=None):
    args = parse_args(argv)
    configure(args.pipeline_base, args.tpcr_dir, args.python)
    try:
        run_day(args.run_date or date.today(), retrain=args.retrain,
                register_fallback=args.register_fallback_v2)
    except RegistryError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
