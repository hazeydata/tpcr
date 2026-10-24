# Shadow Competition (Rolling Shadow Run)

Challenger models run daily against production on the same entities and slots. A winner is only ever *flagged*; promotion is always manual. Governing spec: TPCR-SHADOW-REVIVE-001. Phases 1-3 (local run, `shadow_days` fix, xgb-dow retired, full-entity retrains, identical-set scoring, fallback-entity MAE, fallback-v2, the scheduled job) are described as built; Phase 4 is marked planned. The launchd job is written but **not installed**.

## What runs

`scripts/rolling_shadow.py` runs entirely on the local machine (no remote hosts). For each runnable challenger in the registry:

0. **Retrain** (only when due, see below) — `python -m pipeline.competition.train_challenger --challenger <module> --output-base <pipeline base> --run-date <run date>` (subprocess, cwd = repo, 2 h timeout). Not run for `fallback-v2`.
1. **Forecast** — `python -m pipeline.competition.forecast_challenger --challenger <module> --output-base <pipeline base>` (subprocess, 300 s timeout). `fallback-v2` is dispatched to `python -m pipeline.competition.fallback_v2 --output-base <pipeline base>` (900 s timeout) instead.
2. **Archive** — in-process DuckDB copies the 1-day-ahead predictions (`park_date = run_date + 1`) for challenger and baseline into `shadow/<name>/{challenger,baseline}_<reference_date>.parquet`. Files are named by the date the predictions are FOR.
3. **Evaluate** — `python -m pipeline.competition.shadow_evaluate --scores-out <staging file>` for `reference_date = run_date - 1` (300 s timeout). Actuals methodology is identical to `s10_accuracy.py`; the script never computes its own error metrics. Production and challenger are scored on the identical (entity, slot) set (see below).
4. **Update registry** — see the `shadow_days` rule below; auto-retire / `promotion_candidate` checks apply once `shadow_days >= min_shadow_days`. For challengers with `score_scope = "fallback"` the registry tracks the fallback-entity MAE (all-entity MAE is kept alongside as `all_entity_*`).
5. **Write the day's outputs** — `scores/<eval date>.parquet` and `summaries/<eval date>.json`, both atomic (tmp file + `os.replace`) and overwritten on rerun.
6. **Report** — still posts to Discord (removal is Phase 4, planned, TPCR-SHADOW-REVIVE-001). `requests` is imported lazily, only when posting.

## Paths

| What | Default | Override |
|---|---|---|
| Pipeline base | `~/hazeydata/pipeline` | `--pipeline-base`, env `TPCR_PIPELINE_BASE` |
| Competition base | `<pipeline base>/competition` (`DEFAULT_COMPETITION_BASE` in `pipeline/competition/config.py`) | follows pipeline base |
| Repo | `~/theme-park-crowd-report` | `--tpcr-dir`, env `TPCR_DIR` |
| Python | `<repo>/.venv/bin/python` | `--python` |
| Run date | today | `--run-date YYYY-MM-DD` |
| Force retrain | off | `--retrain` |
| Add fallback-v2 to the registry | off | `--register-fallback-v2` |

Everything the run writes lives under the competition base:

```
competition/
  challenger_registry.json     # atomic write (tmp file + os.replace)
  models/<module>/<entity>/    # challenger models (train_challenger)
  forecasts/<module>/          # challenger forecasts
  forecasts/fallback-v2/       # fallback-v2 forecast (all_forecasts_fallback-v2.parquet)
  state/                       # fallback-v2 only: single_rider_parent.csv, fallback_ratios.json
  shadow/<name>/               # archived predictions by reference date
  scores/<YYYY-MM-DD>.parquet  # paired score rows, all challengers for that eval date
  summaries/<YYYY-MM-DD>.json  # daily summary
```

Production `state/` is never written. `scores/.staging-<date>/` holds per-challenger rows during a run and is removed at the end.

## Registry shape

`challenger_registry.json` has `settings` (`min_shadow_days`, `auto_retire_if_worse_by`), `challengers` and `retired`. Entries are normalized on every load (idempotent) and carry the spec fields alongside the legacy ones (`name`, `module_name`, `shadow_days`, `daily_metrics`, `metrics`):

| Field | Notes |
|---|---|
| `id` | equals `name` |
| `status` | `active`, `retired`, `flagged`; also `promotion_candidate` in the current code. Legacy `shadow` maps to `active`, `auto_retired` to `retired` |
| `trained_at` | UTC ISO time of the last full-entity retrain; `null` before the first one (always `null` for fallback-v2) |
| `entity_count` | models actually trained in that retrain; `null` before the first one |
| `notes` | free text; retired reason for xgb-dow |
| `score_scope` | optional; `fallback` for fallback-v2 (default `all`) |

Only statuses `active`, `shadow` and `promotion_candidate` are run.

## Full-entity retrains (AC5)

A challenger is retrained when its model dir (`competition/models/<module>`) is missing or empty, its registry `trained_at` is absent or older than 7 days, or `--retrain` is given.

`pipeline/competition/eligibility.py::production_eligible_entities(output_base, run_date)` returns the sorted entities production S7 would train for the run date: entities with at least `training_min_obs_lite` observations in the S7 training data (same scan helper as S7, read-only), skipping `ignore_parks`, **minus entities with `dimentity.extinct_on <= run_date`**. If `pipeline.core.eligibility` becomes importable and exposes `production_eligible_entities` or `eligible_entities` (PR #522), it is tried first (called as `fn(cfg=cfg, run_date=run_date)`; any failure falls back to the S7 mirror) and the extinct filter is still applied. Until then the selection mirrors S7.

`train_challenger` trains every eligible entity (no subsetting) and prints `RESULT:{...}` with `trained_at`, `entity_count` (trained), `eligible_count`, and `skipped {entity: reason}` (`not_trained`, `no_park_data`, `error:<Class>`). `rolling_shadow` writes `trained_at` / `entity_count` into the registry entry and, if `entity_count != eligible_count`, logs the diff and records an error in the day's summary.

## Identical (entity, slot) set (AC6)

`pipeline/competition/paired_scores.py` (pure pandas):

- `build_paired_scores(prod_df, chal_df, actual_df, challenger_id, date, fallback_entities) -> (scores_df, dropped)`. Inputs are keyed by `entity_code`, `slot` with `prod_pred` / `chal_pred` / `actual`. Rows are kept only where all three exist. `dropped` counts every other pair seen on any side under the first missing item in the order `no_actual`, `no_prod_pred`, `no_chal_pred`, and logs it.
- `summarize(scores_df, dropped, forecast_entities)` returns `matched_pairs`, `forecast_entities`, `coverage_pct` (entities with at least one matched pair / production forecast entities x 100) and per challenger `mae_prod`, `mae_chal`, `mae_fallback_prod`, `mae_fallback_chal` (all entity-weighted: mean of per-entity MAE, as in s10), `n_entities`, `n_fallback_entities`, `dropped`. Fallback MAEs are `null` when the day has no fallback entity rows. At the top level `matched_pairs` is the largest and `coverage_pct` the smallest per-challenger value.

`shadow_evaluate` loads the archived baseline/challenger parquets and the actuals (same SQL as s10: ACTUAL wait type, `TIME_BUCKET` 5-minute slots with midpoint rounding, synthetic fallback), calls `build_paired_scores`, and keeps its `RESULT` keys (`baseline_mae`, `challenger_mae`, `n_matched`, `n_entities`, ...), now computed from the paired set, plus `matched_pairs`, `forecast_entities`, `coverage_pct`, `dropped`, `mae_fallback_prod`, `mae_fallback_chal`, `n_fallback_entities`. `forecast_entities` is the number of entities in the production archive for the date.

## Fallback entities (AC7)

`pipeline/competition/fallback_entities.py`. An entity is a fallback entity (`is_fallback_entity`) when it is a single-rider queue or production served it by ratio.

- `is_single_rider(entity_code, entities=None)`: an explicit `is_single_rider` column in dimentity if present, else the dimentity `name` containing "single rider" (case-insensitive).
- Ratio-served: any production row whose `prediction_method` starts with `fallback_ratio` (production S8 emits exactly `fallback_ratio`; challenger forecasters emit `fallback_ratio_no_model` / `fallback_ratio_missing_features`) or `ratio_` (fallback-v2).

## fallback-v2 challenger (AC8)

TPCR-S8-FALLBACK-001 Phases 2-3 (single-rider parent model, per-entity/category ratios), built as a challenger per R1. Code: `pipeline/competition/fallback_v2.py`; registry metadata: `pipeline/competition/challengers/fallback_v2.py` (`NAME = "fallback-v2"`, `STATUS = "active"`, `SCORE_SCOPE = "fallback"`). It is competition-only: nothing in `pipeline/steps` or the production pipeline imports it (a test checks this), and production S7/S8 are unchanged. `rolling_shadow` never retrains it. Add it to a registry with `--register-fallback-v2`.

Each run rebuilds, from fact tables (last 365 days) and dimentity:

- `state/single_rider_parent.csv`: `child_entity_code, parent_entity_code, scale_factor, scale_source, n_obs, computed_at`. One non-single-rider parent per child, matched on park plus base name with "single rider" stripped. `scale_factor` = median(child posted / parent posted) over same date+slot pairs, with at least 30 pairs (`scale_source=observed`), else 1.0 (`scale_source=default`, logged). Children with no unique parent stay in the file as named failures: empty parent, `scale_source=unmapped:no_parent` or `unmapped:ambiguous`.
- `state/fallback_ratios.json`: `{ "<entity_code>": {"ratio", "level": "entity"|"category"|"global", "n_obs", "window": "YYYY-MM-DD..YYYY-MM-DD"}, "_category": {...}, "_global": {...} }`. Ratio = median(ACTUAL / POSTED) on same date + 5-minute slot. Level `entity` needs at least 30 observations, else `category` (show, parade, waypoint, hhn_house; at least 30 observations across the category), else `global` (0.678 when there is no data at all).
- `forecasts/fallback-v2/all_forecasts_fallback-v2.parquet`, same schema as other challengers (`entity_code, park_date, time_slot, predicted_actual, prediction_method`):
  - single-rider child with a mapped parent: parent production forecast x `scale_factor`, `parent_scaled`. A child whose parent has no forecast is skipped (OQ3 default), not sent to the ratio.
  - other rows production served by ratio: posted x entity/category/global ratio, `ratio_<level>`. Posted is the `posted_time` column if the production forecast has one, else recovered as `prediction / production ratio` (accurate to the rounding of the production integer prediction).
  - everything else: copied from production unchanged, so non-fallback scores tie.

Scoring: `score_scope = "fallback"` — the summary marks it, and the registry's win/retire logic uses fallback-entity MAE. All-entity `mae_prod` / `mae_chal` are always reported too, so the no-regression check from OQ3 can be applied later.

## Daily outputs (file shapes)

`scores/YYYY-MM-DD.parquet` (YYYY-MM-DD is the evaluated date, `run_date - 1`): one row per matched pair for every challenger that day, columns exactly `date, challenger_id, entity_code, slot, actual, prod_pred, chal_pred, is_fallback_entity`. The whole file is rewritten on each run, so reruns never duplicate rows. If no challenger produced rows, an existing file is left alone.

`summaries/YYYY-MM-DD.json`:

```json
{
  "date": "2026-10-23",
  "matched_pairs": 61234,
  "forecast_entities": 333,
  "coverage_pct": 99.1,
  "challengers": {
    "<id>": {
      "score_scope": "all", "trained_at": "...", "entity_count": 333,
      "matched_pairs": 61234, "n_entities": 330, "coverage_pct": 99.1, "forecast_entities": 333,
      "mae_prod": 0.0, "mae_chal": 0.0, "mae_fallback_prod": 0.0, "mae_fallback_chal": 0.0,
      "n_fallback_entities": 40, "dropped": {"no_actual": 0, "no_prod_pred": 0, "no_chal_pred": 0}
    }
  },
  "run_status": "ok",
  "errors": []
}
```

A challenger with no matched pairs gets `{"score_scope", "trained_at", "entity_count", "matched_pairs": 0, "status": "no_data"}`. `run_status` is `ok` with no errors, else `failed` (any retrain/forecast/archive/evaluation error, or an `entity_count` mismatch). `win`, `streak`, `ci95_low`, `ci95_high`, `gain_min` arrive with the Phase 4 flag rule. `rolling_shadow.py` run by hand keeps its old exit code; the scheduled job's run_status values and exit codes are in the Phase 3 section below.

## `shadow_days` rule

`shadow_days` counts only evaluations that matched at least one (entity, slot) pair. `shadow_evaluate` reports `n_matched`; `matched_pairs(metrics)` returns it (0 for `"no_data"`, `None` or a missing key). A day with `no_data`, `None` or `n_matched == 0` does not increment `shadow_days` and is not appended to `daily_metrics`. For fallback-scope challengers a day with no fallback entity rows also does not count.

## Retired: xgb-dow

`xgb-dow` is retired ("in production via #470"). The registry normalizer forces its status to `retired` with that note, the orchestrator never forecasts, archives or scores it, `pipeline/competition/challengers/xgb_dow.py` declares `STATUS = "retired"`, and `ChallengerRegistry.active_challengers()` skips it.

## Running manually

```bash
cd ~/theme-park-crowd-report
.venv/bin/python scripts/rolling_shadow.py                      # today
.venv/bin/python scripts/rolling_shadow.py --run-date 2026-10-24
.venv/bin/python scripts/rolling_shadow.py --retrain            # retrain every active challenger now
.venv/bin/python scripts/rolling_shadow.py --register-fallback-v2
.venv/bin/python scripts/rolling_shadow.py --pipeline-base /tmp/pipe --tpcr-dir "$PWD"   # sandbox
```

Tests: `pytest pipeline/tests/test_shadow_revive_p1.py pipeline/tests/test_shadow_revive_p2.py pipeline/tests/test_shadow_revive_p3.py`.

## Phase 3: scheduled job (AC9-AC11)

`scripts/shadow_job.py` wraps `rolling_shadow.run_day` (stdlib plus `rolling_shadow`). `launchd/com.hazeydata.tpcr-shadow.plist` starts it at 06:20 local with `Nice` 10 and `LowPriorityIO`; the script also calls `os.nice(10)`. Logs go to `~/hazeydata/pipeline/competition/logs/`.

**Not installed.** Installing the plist is a deploy step that needs Fred's go. The install commands are in `launchd/README.md` as documentation only.

```bash
.venv/bin/python scripts/shadow_job.py [--date YYYY-MM-DD] [--pipeline-base DIR] [--tpcr-dir DIR] \
    [--max-marker-wait-min 30] [--lock-wait-min 20] [--dry-run]
```

`--dry-run` prints the marker status and scraper-lock state once, waits for nothing and writes nothing.

### Order of checks

1. **Job lock.** A non-blocking `flock` on `competition/locks/shadow_job.lock`. If another shadow job holds it, this one exits 0 at once and writes nothing. The lock is shadow-only: S1-S11 and the scraper never use it.
2. **Pipeline marker.** The job polls (60 s) for `<pipeline base>/logs/pipeline_metrics_<run date>.json`, the file `pipeline/pipeline.py` writes at the end of a full run and `tpcr-discord-bot/daily_report.py` waits for. It runs only on `status == "done"`. `failed` or `sync_partial` gives `skipped_pipeline` immediately. No terminal status within `--max-marker-wait-min` (default 30) gives `skipped_no_marker`.
3. **Scraper lock.** The live_waits scraper's lock is `<pipeline base>/state/processing_queue_times.lock`, a plain pid file (no flock) taken by `acquire_lock` in `src/get_tp_wait_time_data_from_s3.py`. The job only `stat`s it. It follows the scraper's own staleness rule: a lock older than 24 h is stale and ignored. While the lock is held the job polls up to `--lock-wait-min` (default 20); still held gives `skipped_lock`.
4. **Run into a staging dir.** `run_day(out_base=<competition>/staging/<eval date>, post=False)` writes the scores file, summary and registry there. The scraper lock is checked again before committing. If it was taken during the run, the staging dir is deleted and the run is recorded as `skipped_lock`. Otherwise the files are moved into place with `os.replace` (scores, registry, then summary last).

Models, forecasts and `shadow/` archives are written in place by the subprocesses. They are keyed by date or challenger and overwritten on rerun, so a skipped run can leave them behind. A retrain whose registry update was discarded simply happens again on the next run.

### run_status values (summary `run_status`)

| Value | Meaning | Files written | Exit |
|---|---|---|---|
| `ok` | ran, no errors | scores, summary, registry | 0 |
| `failed` | exception, or any retrain/forecast/archive/eval error | exception: summary only (`errors[]` = `{"type", "message"}`). Soft error from a challenger: that day's outputs are committed with `errors[]` as strings | 1 |
| `skipped_pipeline` | marker `failed` / `sync_partial` | summary only | 0 |
| `skipped_no_marker` | no terminal marker in time | summary only | 0 |
| `skipped_lock` | scraper lock held after the wait, or taken mid-run | summary only (`errors` = `[]`); no scores file, registry unchanged | 0 |

Skip and failure summaries have `matched_pairs` 0, empty `challengers`, a `run_date`, and (for skips) a `detail` string. Summaries are keyed by the evaluated date (`run date - 1`), same as the scores file.

### Idempotence

A rerun for the same date overwrites that date's scores file and summary (no duplicate rows). The registry's `daily_metrics` is keyed by `date`, so a rerun replaces that day's entry. `shadow_days` counts distinct dates with at least one matched pair and is not incremented again on a rerun. A failed or skipped rerun overwrites that date's summary but leaves the scores file from an earlier good run. Other dates' files are never touched on failure.

### No shared lock, no slowdown (AC11)

The job never takes the scraper lock or any lock S1-S11 uses. It does not import `acquire_lock`/`release_lock` or any pipeline step. `pipeline/tests/test_shadow_revive_p3.py` checks this statically (AST) for `shadow_job.py` and `rolling_shadow.py`. It runs at 06:20, after the marker says S11 finished, at `nice` 10 with low-priority I/O.

Not measured here: that the 06:00 pipeline total is unchanged within ±30 s. That can only be measured on the Mini, so it is left for the Mini replay (compare `total_duration_sec` in `pipeline_metrics_<date>.json` with and without the job installed).

Because it only runs after the marker is `done`, the job should not overlap S1-S11 on a normal day. Overlap can still happen with the next scraper run, which is what the lock check is for.

## Planned (TPCR-SHADOW-REVIVE-001)

- Phase 4: flag rule (7-day streak, seeded paired bootstrap CI excluding 0, gain of at least 0.3 min MAE; adds `win`, `streak`, `ci95_low`, `ci95_high`, `gain_min` to the summary) and removal of the Discord path. Planned.
