# Shadow Competition (Rolling Shadow Run)

Challenger models run daily against production on the same entities and slots. A winner is only ever *flagged*; promotion is always manual. Governing spec: TPCR-SHADOW-REVIVE-001. Phase 1 (local run, `shadow_days` fix, xgb-dow retired) is described as built; everything else is marked planned.

## What runs

`scripts/rolling_shadow.py` runs entirely on the local machine (no remote hosts). For each runnable challenger in the registry:

1. **Forecast** — `python -m pipeline.competition.forecast_challenger --challenger <module> --output-base <pipeline base>` (subprocess, cwd = repo, 300 s timeout).
2. **Archive** — in-process DuckDB copies the 1-day-ahead predictions (`park_date = run_date + 1`) for challenger and baseline into `shadow/<name>/{challenger,baseline}_<reference_date>.parquet`. Files are named by the date the predictions are FOR.
3. **Evaluate** — `python -m pipeline.competition.shadow_evaluate` for `reference_date = run_date - 1` (120 s timeout). Methodology is identical to `s10_accuracy.py`; the script never computes its own error metrics.
4. **Update registry** — see the `shadow_days` rule below; auto-retire / `promotion_candidate` checks apply once `shadow_days >= min_shadow_days`.
5. **Report** — still posts to Discord (removal is Phase 4, planned, TPCR-SHADOW-REVIVE-001). `requests` is imported lazily, only when posting.

## Paths

| What | Default | Override |
|---|---|---|
| Pipeline base | `~/hazeydata/pipeline` | `--pipeline-base`, env `TPCR_PIPELINE_BASE` |
| Competition base | `<pipeline base>/competition` (`DEFAULT_COMPETITION_BASE` in `pipeline/competition/config.py`) | follows pipeline base |
| Repo | `~/theme-park-crowd-report` | `--tpcr-dir`, env `TPCR_DIR` |
| Python | `<repo>/.venv/bin/python` | `--python` |
| Run date | today | `--run-date YYYY-MM-DD` |

Everything the run writes lives under the competition base:

```
competition/
  challenger_registry.json     # atomic write (tmp file + os.replace)
  forecasts/<module>/          # challenger forecasts
  shadow/<name>/               # archived predictions by reference date
  scores/                      # created; daily score rows planned (Phase 2)
  summaries/                   # created; daily summaries planned (Phase 2)
```

## Registry shape

`challenger_registry.json` has `settings` (`min_shadow_days`, `auto_retire_if_worse_by`), `challengers` and `retired`. Entries are normalized on every load (idempotent) and carry the spec fields alongside the legacy ones (`name`, `module_name`, `shadow_days`, `daily_metrics`, `metrics`):

| Field | Notes |
|---|---|
| `id` | equals `name` |
| `status` | `active`, `retired`, `flagged`; also `promotion_candidate` in the current code. Legacy `shadow` maps to `active`, `auto_retired` to `retired` |
| `trained_at` | `null` until full-entity retrains land (Phase 2, planned) |
| `entity_count` | `null` until Phase 2 (planned) |
| `notes` | free text; retired reason for xgb-dow |

Only statuses `active`, `shadow` and `promotion_candidate` are run.

## `shadow_days` rule

`shadow_days` counts only evaluations that matched at least one (entity, slot) pair. `shadow_evaluate` reports `n_matched`; `matched_pairs(metrics)` returns it (0 for `"no_data"`, `None` or a missing key). A day with `no_data`, `None` or `n_matched == 0` does not increment `shadow_days` and is not appended to `daily_metrics`.

## Retired: xgb-dow

`xgb-dow` is retired ("in production via #470"). The registry normalizer forces its status to `retired` with that note, the orchestrator never forecasts, archives or scores it, `pipeline/competition/challengers/xgb_dow.py` declares `STATUS = "retired"`, and `ChallengerRegistry.active_challengers()` skips it.

## Running manually

```bash
cd ~/theme-park-crowd-report
.venv/bin/python scripts/rolling_shadow.py                      # today
.venv/bin/python scripts/rolling_shadow.py --run-date 2026-10-24
.venv/bin/python scripts/rolling_shadow.py --pipeline-base /tmp/pipe --tpcr-dir "$PWD"   # sandbox
```

Tests: `pytest pipeline/tests/test_shadow_revive_p1.py`.

## Planned (TPCR-SHADOW-REVIVE-001)

- Phase 2: full-entity retrains matching production S7 (`entity_count`), same-set (entity, slot) scoring with dropped-pair counts, separate fallback-entity MAE, `fallback-v2` challenger, `scores/YYYY-MM-DD.parquet` and `summaries/YYYY-MM-DD.json` outputs. Planned.
- Phase 3: launchd job after S11 completes, scraper-lock handling (`run_status=skipped_lock`), `run_status=failed` with `errors[]`, idempotent reruns. Planned.
- Phase 4: flag rule (7-day streak, seeded paired bootstrap CI excluding 0, gain of at least 0.3 min MAE) and removal of the Discord path. Planned.
