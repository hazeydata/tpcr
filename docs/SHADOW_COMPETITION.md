# Shadow Competition (Rolling Shadow Run)

Challenger models run daily against production on the same entities and slots. A winner is only ever *flagged*; promotion is always manual. Governing spec: TPCR-SHADOW-REVIVE-001. Phases 1-3 (local run, `shadow_days` fix, xgb-dow retired, full-entity retrains, identical-set scoring, fallback-entity MAE, fallback-v2, the scheduled job) and Phase 4 (the flag rule and the removal of the Discord path) are described as built. The launchd job is written but **not installed**.

## What runs

`scripts/rolling_shadow.py` runs entirely on the local machine (no remote hosts). For each runnable challenger in the registry:

0. **Retrain** (only when due, see below) — `python -m pipeline.competition.train_challenger --challenger <module> --output-base <pipeline base> --run-date <run date>` (subprocess, cwd = repo, 2 h timeout). Not run for `fallback-v2`.
1. **Forecast** — `python -m pipeline.competition.forecast_challenger --challenger <module> --output-base <pipeline base>` (subprocess, 300 s timeout). `fallback-v2` is dispatched to `python -m pipeline.competition.fallback_v2 --output-base <pipeline base>` (900 s timeout) instead.
2. **Archive** — in-process DuckDB copies the 1-day-ahead predictions (`park_date = run_date + 1`) for challenger and baseline into `shadow/<name>/{challenger,baseline}_<reference_date>.parquet`. Files are named by the date the predictions are FOR.
3. **Evaluate** — `python -m pipeline.competition.shadow_evaluate --scores-out <staging file>` for `reference_date = run_date - 1` (300 s timeout). Actuals methodology is identical to `s10_accuracy.py`; the script never computes its own error metrics. Production and challenger are scored on the identical (entity, slot) set (see below).
4. **Update registry** — see the `shadow_days` rule below. There is no auto-retire or promotion check any more; the only status decision is the Phase 4 flag rule. For challengers with `score_scope = "fallback"` the registry tracks the fallback-entity MAE (all-entity MAE is kept alongside as `all_entity_*`).
5. **Write the day's outputs** — `scores/<eval date>.parquet` and `summaries/<eval date>.json` (with the flag fields), both atomic (tmp file + `os.replace`) and overwritten on rerun.
6. **Apply the flag rule** — set the registry status (`flagged` / back to `active`) from the summary. See Phase 4. Nothing is posted anywhere (see "Discord removal").

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

`challenger_registry.json` has `settings` (`min_shadow_days`, `auto_retire_if_worse_by`; both are legacy and no longer read by the code), `challengers` and `retired`. Entries are normalized on every load (idempotent) and carry the spec fields alongside the legacy ones (`name`, `module_name`, `shadow_days`, `daily_metrics`, `metrics`):

| Field | Notes |
|---|---|
| `id` | equals `name` |
| `status` | `active`, `retired`, `flagged`. Legacy `shadow` and `promotion_candidate` map to `active`, `auto_retired` to `retired`. `flagged` is set only by the Phase 4 flag rule |
| `flagged_on` | only while `flagged`: the evaluated date the flag was first set |
| `trained_at` | UTC ISO time of the last full-entity retrain; `null` before the first one (always `null` for fallback-v2) |
| `entity_count` | models actually trained in that retrain; `null` before the first one |
| `notes` | free text; retired reason for xgb-dow |
| `score_scope` | optional; `fallback` for fallback-v2 (default `all`) |

Only statuses `active`, `shadow` and `flagged` are run (a flagged challenger keeps competing).

## Full-entity retrains (AC5)

A challenger is retrained when its model dir (`competition/models/<module>`) is missing or empty, its registry `trained_at` is absent or older than 7 days, or `--retrain` is given.

`pipeline/competition/eligibility.py::production_eligible_entities(output_base, run_date)` returns the sorted entities production S7 would train for the run date: entities with at least `training_min_obs_lite` observations in the S7 training data (same scan helper as S7, read-only), skipping `ignore_parks`, **minus entities with `dimentity.extinct_on <= run_date`**. If `pipeline.core.eligibility` becomes importable and exposes `production_eligible_entities` or `eligible_entities` (PR #522), it is tried first (called as `fn(cfg=cfg, run_date=run_date)`; any failure falls back to the S7 mirror) and the extinct filter is still applied. Until then the selection mirrors S7.

`train_challenger` trains every eligible entity (no subsetting) and prints `RESULT:{...}` with `trained_at`, `entity_count` (trained), `trained_entities` (their codes), `eligible_count`, and `skipped {entity: reason}` (`not_trained`, `no_park_data`, `error:<Class>`). `rolling_shadow` writes `trained_at` / `entity_count` into the registry entry and compares the challenger's trained entity set to the **AC5 denominator (ruling R1a): the production S7 trained set of the same UTC run date minus extinct entities**. The production set is read from `<pipeline_base>/models/*/metadata_baseline.json` (written per entity by `s07_training.py`): every entity whose `trained_at` falls on the run date (UTC). Extinct status comes from the single shared `is_extinct(entity, extinct_on, run_date)` predicate in `pipeline/competition/eligibility.py` (dates via `load_extinct_dates`), used by both the challenger eligible list and this check so they cannot drift; challengers do not train extinct entities, and one that does counts as extra. The check logs `AC5: production trained N, extinct excluded K, denominator N-K` (K is about 87 in real data). On mismatch it prints the missing/extra entity codes and records an error in the day's summary; if no production set is found for the run date, that is also an error (the run fails).

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

Scoring: `score_scope = "fallback"` — the summary marks it, and the registry tracks, and the flag rule (win, gain, bootstrap) uses, fallback-entity MAE. All-entity `mae_prod` / `mae_chal` are always reported too, so the no-regression check from OQ3 can be applied later.

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
      "n_fallback_entities": 40, "dropped": {"no_actual": 0, "no_prod_pred": 0, "no_chal_pred": 0},
      "win": true, "streak": 7, "ci95_low": 0.12, "ci95_high": 0.55, "gain_min": 0.34, "flagged": true
    }
  },
  "significant_gain": ["<id>"],
  "significant_gain_text": "Significant gain: <id>",
  "run_status": "ok",
  "errors": []
}
```

A challenger with no matched pairs gets `{"score_scope", "trained_at", "entity_count", "matched_pairs": 0, "status": "no_data"}` plus the flag fields at their defaults (`win` false, `streak` 0, the rest `null` / false). `significant_gain` lists the flagged ids and `significant_gain_text` is `"None yet"` when it is empty (a valid outcome, not a failure); skip and failure summaries carry the same two fields (`[]`, `"None yet"`). `run_status` is `ok` with no errors, else `failed` (any retrain/forecast/archive/evaluation error, or an `entity_count` mismatch). `rolling_shadow.py` run by hand keeps its old exit code; the scheduled job's run_status values and exit codes are in the Phase 3 section below.

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

Tests: `pytest pipeline/tests/test_shadow_revive_p1.py pipeline/tests/test_shadow_revive_p2.py pipeline/tests/test_shadow_revive_p3.py pipeline/tests/test_shadow_revive_p4.py`.

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
2. **Pipeline marker.** The job polls (60 s) for `<pipeline base>/logs/pipeline_metrics_<run date>.json`, the file `pipeline/pipeline.py` writes at the end of a full run (the daily report waits for it too). It runs only on `status == "done"`. `failed` or `sync_partial` gives `skipped_pipeline` immediately. No terminal status within `--max-marker-wait-min` (default 30) gives `skipped_no_marker`.
3. **Scraper lock.** The live_waits scraper's lock is `<pipeline base>/state/processing_queue_times.lock`, a plain pid file (no flock) taken by `acquire_lock` in `src/get_tp_wait_time_data_from_s3.py`. The job only `stat`s it. It follows the scraper's own staleness rule: a lock older than 24 h is stale and ignored. While the lock is held the job polls up to `--lock-wait-min` (default 20); still held gives `skipped_lock`.
4. **Run into a staging dir.** `run_day(run_date, out_base=<competition>/staging/<eval date>)` writes the scores file, summary and registry there. The scraper lock is checked again before committing. If it was taken during the run, the staging dir is deleted and the run is recorded as `skipped_lock`. Otherwise the files are moved into place with `os.replace` (scores, registry, then summary last).

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

## Phase 4: flag rule and no posting (AC12, AC13, AC15)

Code: `pipeline/competition/flag_rule.py` (numpy only, no I/O, constants and pure functions) and `scripts/rolling_shadow.py` (`flag_fields`, `write_day_outputs`, `apply_flags`).

### The rule (AC12)

A challenger becomes `flagged` only when ALL three hold on the evaluated date:

1. **Streak.** `streak >= MIN_STREAK` (7): consecutive dates ending on the evaluated date, each a win. A win is `mae_chal < mae_prod` on that day's matched (entity, slot) set. For `score_scope = "fallback"` (fallback-v2) the MAEs are the fallback-entity ones (`mae_fallback_prod` / `mae_fallback_chal`), as in the registry.
2. **Bootstrap.** A paired per-entity bootstrap of the MAE difference: per entity, the mean of `|actual - prod| - |actual - chal|` over its matched pairs; then `BOOTSTRAP_RESAMPLES` (10,000) resamples of the entities with replacement, the mean of each, and the 2.5th / 97.5th percentiles as `ci95_low` / `ci95_high`. The generator is `numpy.random.default_rng(BOOTSTRAP_SEED)` (20261024), so the same data gives the same CI. Required: `ci95_low > 0` (the whole CI favors the challenger).
3. **Gain.** `gain_min = mae_prod - mae_chal >= MIN_GAIN_MIN` (0.3 min), on the evaluated day's scope MAEs. Compared unrounded, with a 1e-9 tolerance (`GAIN_EPSILON`) so a gain of exactly 0.3 from float MAEs flags; the summary stores 6 decimals.

**Bootstrap data:** the matched pairs pooled over the last 7 dates ending on the evaluated date, read from `scores/*.parquet` (the evaluated date from the run's output dir, earlier dates from the committed `scores/`; dates with no scores file are skipped; fallback-scope challengers keep only `is_fallback_entity` rows). With a streak of 7 these are exactly the 7 streak days. When fewer than 7 score files exist (early days) the pool is whatever exists, down to the day's own set, and the streak condition blocks the flag anyway.

**Streak breaks.** A day counts toward the streak only if that day's summary has `run_status == "ok"` and the challenger's block has `win: true`. A missing summary, a `skipped_*` day, a `failed` day, a `no_data` day, a day with no block for the challenger, or a summary from before Phase 4 (no `win` field) all break the streak. Today's own run counts only when `run_status` is `ok` (any error in today's run means no win, streak 0, no flag). Lookback is capped at 60 days.

**Registry status.** After the summary is written, `apply_flags` sets the status of every `active` / `flagged` challenger: `flagged` (with `flagged_on` set on the first day) when its block says `flagged`, otherwise back to `active`. So `flagged` always means the rule holds as of the latest run; it is not sticky. Retired challengers are never touched. A flagged challenger keeps running.

**A flag changes nothing in production.** It only sets the registry status and the summary fields. There is no promotion code; promotion is always manual.

Per challenger, the summary now has `win` (bool), `streak` (int), `ci95_low`, `ci95_high`, `gain_min`, `flagged` (bool).

### Significant gain (AC15)

The summary has top-level `significant_gain` (list of flagged challenger ids) and `significant_gain_text`: `"None yet"` when the list is empty, else `"Significant gain: <id>, <id>"`. "None yet" is a valid outcome.

### Removed: old promotion / retire logic

`update_registry` no longer retires a challenger or sets `promotion_candidate` at `min_shadow_days` (avg MAE below baseline, worse than baseline plus `auto_retire_if_worse_by`, or borderline at 14 days). That logic could call a challenger a promotion candidate on an average-MAE edge with no significance test, which contradicts AC12. Retiring is now manual. Existing `promotion_candidate` registry entries are read as `active`. `update_registry` still keeps `shadow_days`, `daily_metrics` and `metrics` (running averages).

### Discord removal (AC13)

Nothing posts anywhere. Removed:

- `scripts/rolling_shadow.py`: `post_discord`, the channel / API constants, the lazy `requests` import, the report-line building, `load_env` (it only loaded the bot token from `~/.env`), and the `post` parameter of `run_day` (now `run_day(run_date, retrain=False, register_fallback=False, out_base=None)`). The per-challenger result line is printed to the log instead.
- `scripts/shadow_job.py`: the `post=False` argument.
- `pipeline/competition/evaluate.py`: `format_comparison_report` and the `--format-report` flag (they only produced a chat-ready report).

`pipeline/tests/test_shadow_revive_p4.py` greps `scripts/rolling_shadow.py`, `scripts/shadow_job.py` and `pipeline/competition/` for the three posting terms (the chat service name, the HTTP post call and the webhook word; built at runtime in the test, case-insensitive) and requires 0 hits. The production daily report in `tpcr-discord-bot/` is a separate system and was not touched.

Tests: `pipeline/tests/test_shadow_revive_p4.py` (flag only when all three hold, each single failure prevents it, deterministic bootstrap, "None yet", streak breaks, fallback scope, no-posting grep).
