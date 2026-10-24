# Pipeline V4 Design Specification

**Version:** 1.0 APPROVED
**Date:** 2026-03-21
**Authors:** Barney (architect) + Fred (decision-maker)
**Status:** APPROVED by Fred 2026-03-21
**Based on:** Complete pipeline audit of 2026-03-21 (37 questions, 10 sections)

---

## Design Philosophy

**Call things what they are.** No version numbers in production filenames. No "julia", "v2", "v3" in anything that's currently running. If it's the baseline model, call it "baseline." If it's a challenger testing day-of-week features, call it "day_of_week_v1."

**The baseline is pure.** The production pipeline runs the simplest thing that works: data in → XGBoost → predictions out → WTI aggregation → done. No bias correction, no quantile mapping, no post-processing. Those are hypotheses that earn their way into production by winning in the competition framework.

**One pipeline, one truth.** No parallel execution, no ghost crons, no dead scripts in the active codebase. If it's not running, it's archived.

---

## Current State (from audit)

### What's Running
- **Orchestrator:** `pipeline_v3/pipeline.py` via system crontab at 6 AM ET
- **Training:** `pipeline_v3/steps/s07_training.py` — Python XGBoost, 5 features, geo-decay
- **Forecast:** `pipeline_v3/steps/s08_forecast.py` — loads `model_v3.json` per entity
- **WTI:** `pipeline_v3/steps/s09_wti.py` — adaptive quantile mapping (TO BE REMOVED)
- **Accuracy:** `pipeline_v3/steps/s10_accuracy.py`

### What's Dead (to be archived)
- `scripts/run_daily_pipeline.sh` — V2 shell orchestrator
- `scripts/hybrid_pipeline_v2.py` — already missing from disk
- `scripts/forecast_vectorized.py` — V2 forecast (48KB of dead code)
- `scripts/calculate_wti_simple.py` — V2 WTI with old quantile mapping + dead bias correction
- `scripts/bias_correction_framework.py` — the script that caused the March 17 disaster
- Clawdbot `pipeline-daily` cron — ghost V2 job failing 3x daily
- Ghost model files: `model_julia.json`, `model_julia_v2.json`, `model_julia_actuals.json`, `model.json`, `ngboost_model.pkl` in each entity directory

### Key Numbers
- 431 entities with trained models (79.5% of 542)
- 5 features: mins_since_6am, mins_since_open, date_group_id_encoded, season_encoded, season_year_encoded
- XGBoost: max_depth=10, eta=0.1, n_estimators=2000, early_stopping=20
- Geo-decay half-life: 730 days
- WDW parks: min_training_year=2016
- Overall MAE: ~8.63 (inflated by bias correction period; true baseline TBD)
- WTI MAE: 6.72
- 448 synthetic actuals files, 272 entities with per-entity fallback ratios

---

## V4 Directory Structure

```
pipeline/                           # was: pipeline_v3/
├── __init__.py
├── config.py                       # All configuration in one place
├── run.py                          # Orchestrator (was: pipeline.py)
├── core/
│   ├── db.py                       # DuckDB helpers
│   ├── logging.py                  # Structured logging
│   ├── metrics.py                  # MAE, bias, accuracy calculations
│   ├── park_codes.py               # Entity → park mapping
│   ├── paths.py                    # All path constants
│   └── validation.py               # Data quality checks
├── steps/
│   ├── step_01_sync.py             # S3 data sync
│   ├── step_02_etl.py              # CSV extraction, incremental load
│   ├── step_03_dimensions.py       # Dimension tables (dates, seasons, hours, entities)
│   ├── step_04_build_posted_lookup.py  # Posted wait time lookup table (entity × date_group × timeslot)
│   ├── step_05_conversion.py       # POSTED → ACTUAL conversion model (weekly)
│   ├── step_06_synthetic.py        # Generate synthetic actuals from conversion model
│   ├── step_07_training.py         # XGBoost model training (baseline)
│   ├── step_08_forecast.py         # Generate predictions from trained models
│   ├── step_09_wti.py              # WTI aggregation (PURE — no post-processing)
│   ├── step_10_accuracy.py         # Accuracy evaluation + archiving
│   ├── step_11_deploy.py           # DuckDB writes, API refresh, GitHub Pages
│   └── step_12_validate.py         # Post-run data quality checks
├── competition/                    # Model competition framework
│   ├── challenger_registry.py      # Register/manage challenger models
│   ├── shadow_runner.py            # Run challengers in parallel
│   └── promote.py                  # Promote winning challenger to baseline
├── diagnostics/                    # Analysis tools (not in daily run)
│   ├── entity_accuracy.py         # PRIMARY: per-entity MAE, bias, trends, worst performers
│   ├── entity_deep_dive.py        # Single-entity forensic: prediction vs actual curves, error by hour/day/season
│   ├── park_rollup.py             # Park-level aggregation of entity accuracy
│   ├── wti_rollup.py              # WTI-level aggregation
│   ├── accuracy_drift.py          # Regression detection: is any entity/park getting worse?
│   └── model_coverage.py          # Which entities have models vs fallback, training data depth
└── tests/
    └── ...
```

### Naming Rules
- Steps are `step_NN_verb.py` — the verb tells you what it does
- No "v2", "v3", "v4", "julia" in any production filename
- Model files: `model_baseline.json` + `metadata_baseline.json` per entity
- Challenger model files: `model_{challenger_name}.json` (e.g., `model_day_of_week_v1.json`)
- Config: one `config.py`, no scattered constants
- Logs: `pipeline_YYYY-MM-DD.log` (one log, one pipeline)

### Display Rules
- **Every report that shows entity codes MUST also show entity names** (from dimentity.csv). Entity codes are internal identifiers; attraction names are how humans recognize what they're looking at. Example: "MK01 — Space Mountain: MAE 15.2", never just "MK01: MAE 15.2"
- Park-level reports show park name alongside park code (e.g., "MK — Magic Kingdom")
- Error reports lead with the human-readable name, code in parentheses if needed

---

## V4 Step-Line

### Step 1: Sync
**Script:** `step_01_sync.py`
**Does:** Pulls raw wait time data from S3 to local storage.
**Inputs:** S3 bucket
**Outputs:** `raw/` directory with fresh CSVs

### Step 2: ETL
**Script:** `step_02_etl.py`
**Does:** Incremental load of new CSVs into parquet fact tables.
**Inputs:** `raw/` CSVs
**Outputs:** `fact_tables/parquet/` — append-only parquet files

### Step 3: Dimensions
**Script:** `step_03_dimensions.py`
**Does:** Builds/refreshes dimension tables (dates, seasons, park hours, entity metadata, closures, operating calendar).
**Inputs:** TouringPlans S3, existing dimension CSVs
**Outputs:** `dimension_tables/` — dimdategroupid.csv, dimseason.csv, dimparkhours.csv, dimentity.csv, `operating_calendar/operating_calendar.parquet`

### Step 4: Build Posted Lookup
**Script:** `step_04_build_posted_lookup.py`
**Does:** Builds a lookup table of typical posted wait times: one value per entity × date_group × 15-minute time slot. Used as a feature in the conversion model and as fallback predictions for entities without trained models.
**Inputs:** `fact_tables/parquet/`, `dimension_tables/`
**Outputs:** `posted_lookup/posted_lookup.parquet`

### Step 5: Conversion Model
**Script:** `step_05_conversion.py`
**Does:** Trains/refreshes the global POSTED → ACTUAL conversion model (weekly, or if model missing).
**Inputs:** Paired POSTED/ACTUAL observations from fact tables
**Outputs:** `models/_conversion/model.json`, `models/_conversion/metadata.json`

### Step 6: Synthetic Actuals
**Script:** `step_06_synthetic.py`
**Does:** Applies conversion model to all historical POSTED observations to generate estimated actuals.
**Inputs:** `fact_tables/parquet/`, `models/_conversion/model.json`
**Outputs:** `synthetic_actuals/{entity_code}.parquet`

### Step 7: Training
**Script:** `step_07_training.py`
**Does:** Trains one XGBoost model per entity using real actuals (weighted 10×) + synthetic actuals (weighted 1×). Geo-decay weighting. Per-park min_training_year cutoff. Saves as `model_baseline.json`.
**Inputs:** `synthetic_actuals/`, `fact_tables/parquet/`, `dimension_tables/`, `state/encoding_mappings.json`
**Outputs:** `models/{entity_code}/model_baseline.json`, `models/{entity_code}/metadata_baseline.json`
**Eligibility (TPCR-S8-FALLBACK-001):** computed after the per-park min_training_year cutoff, in this order: `extinct` (dimentity `extinct_on` ≤ run date) → `not_operating` (no operating-calendar day in [today, today+forecast_days]) → `below_min_obs` (post-cutoff rows < `training_min_obs_lite`). Ignored-park entities are out of scope. During training, `no_training_data` (park file missing, no valid rows/features) and `below_min_obs` (valid rows < min_obs or validation split < 10) are skips, not failures.
**Skip log:** every skip logs `SKIP stage=<s07|s08> entity=<code> reason=<reason>` (reason ∈ extinct, not_operating, no_training_data, below_min_obs, `error:<ExceptionClass>`), then one `SKIP_COUNT stage=… reason=… count=…` per reason, and writes `logs/skips_<stage>_<run_date>.csv` (`stage,entity_code,reason`). Post-eligibility skip counts sum to eligible − trained.
**`failed`:** counts only real errors (`error:<class>`). **S7 warns and continues:** if failed > 0 it does not raise; it logs `S7 TRAINING ERRORS: N entities failed (error:<class>); S8 will use existing models for them` plus one error line per failed entity, and S8 runs on existing models. Return dict includes `failed`, `failed_entities` (codes), `eligible` and `skipped` (reason → count); `pipeline.py` logs a warning `Step <name> completed with N failed entities` for any step reporting failed > 0 without marking the run failed. (S8 differs, see Step 8.)
**Pre-2005 `extinct_on`:** an `extinct_on` before 2005-01-01 (including the `2000-01-01` sentinel) is treated as extinct and skipped as `extinct` through the normal `extinct_on` ≤ run date path. It is never an error: S7 logs one warning per entity (`PRE2005_EXTINCT_ON entity=<code> extinct_on=<date> treated_as=extinct`, plus `sentinel=true` for 2000-01-01) and a `SKIP_NOTE stage=s07 pre2005_extinct_on count=<n>` line / `s07_pre2005_extinct_on` metric; the skips CSV is unchanged. `src/build_operating_calendar.py` does not warn (S7 is the single owner, exactly one warning per entity per run) and its calendar contents are unchanged (S8 output unchanged). There is no overrides file: an earlier HS29 override to NULL was dropped because the 2000-01-01 sentinel means extinct, consistent with the 8 other sentinel rows (CA11, CA18, DL83, DL84, UH12, EP58, EP78, HS09); production also has three real-looking pre-2005 dates (CA32, DL82, MK81).
**Features:** mins_since_6am, mins_since_open, date_group_id_encoded, season_encoded, season_year_encoded
**Hyperparameters:** max_depth=10, eta=0.1, n_estimators=2000, subsample=0.8, colsample_bytree=0.8, early_stopping=20, geo_decay_halflife=730

### Step 8: Forecast
**Script:** `step_08_forecast.py`
**Does:** Generates predictions for all entities × all dates × all time slots. Loads `model_baseline.json` (or fallback to aggregate if no model).
**Inputs:** `models/{entity}/model_baseline.json`, `dimension_tables/`, `aggregates/`, `operating_calendar/`
**Outputs:** `forecasts/all_forecasts.parquet`
**Skips:** every input entity with no output is logged with a reason (`extinct` = calendar entry with zero operating dates; `not_operating` = no dates in the forecast window; `error:<class>` = exception, counted as failed). Skip counts sum to input entities − output entities; failed > 0 raises `StageFailed` after outputs are written. **S8 failure handling (intended behaviour, Fred, Oct 24 12:28 AM ET):** any S8 entity error raises `StageFailed` after outputs are written. `pipeline.py` stops, so S9-S11 do not run, and the process exits nonzero. This applies in `--shadow` mode too (Spec R5, Fred, Oct 24 1:34 AM ET): an S8 entity error is the one failure that stops a shadow run; any other failed step in shadow mode is logged and the run continues (the run still exits nonzero). This contrasts with S7, which warns and continues. Same log format as S7. Forecast values and rows are unchanged by this (fixture before/after test). Single-rider parent model and daily fallback_ratios rebuild (spec phases 2–3) are deferred to shadow challengers under TPCR-SHADOW-REVIVE-001; S8 still uses `state/fallback_ratios.json` / global 0.678.
**NO POST-PROCESSING.** Raw model output only. No bias correction, no quantile mapping.

### Step 9: WTI
**Script:** `step_09_wti.py`
**Does:** Aggregates entity-level predictions to park-level Wait Time Index. Historical WTI from actuals + synthetic. Future WTI from forecasts.
**Inputs:** `forecasts/all_forecasts.parquet`, `fact_tables/parquet/`, `synthetic_actuals/`
**Outputs:** `wti/wti.parquet`
**PURE AGGREGATION ONLY.** WTI = average predicted_actual per park per day. No quantile mapping, no bias adjustment, no distribution stretching.

### Step 10: Accuracy
**Script:** `step_10_accuracy.py`
**Does:** Archives today's forecasts, compares yesterday's archived forecast against actual observations, computes MAE/bias per entity/park/horizon.
**Inputs:** `forecasts/all_forecasts.parquet`, `fact_tables/parquet/`, `accuracy/archive/`
**Outputs:** `accuracy/accuracy_summary.json`, `accuracy/daily_accuracy.json`, `accuracy/entity_scores.json`, `accuracy/archive/forecast_YYYY-MM-DD.parquet`

### Step 11: Deploy
**Script:** `step_11_deploy.py`
**Does:** Writes results to DuckDB for bot/dashboard, refreshes API, pushes to GitHub Pages.
**Inputs:** `forecasts/`, `wti/`, `accuracy/`
**Outputs:** `tpcr_live.duckdb` updates, `docs/analytics-data/` JSON files

### Step 12: Validate
**Script:** `step_12_validate.py`
**Does:** Post-run data quality checks. Verifies forecasts exist, WTI is reasonable, no regressions.
**Inputs:** All outputs
**Outputs:** Pass/fail + alert if fail

---

## Accuracy Hierarchy: Entity First

**The models are entity-level. The accuracy analysis must be entity-level first.**

Park-level MAE and WTI MAE are useful dashboards, but they're aggregations — they hide the real story. An overall MAE of 7.0 might mean every entity is at 7.0 (uniform quality), or it might mean 80% of entities are at 4.0 and 10 headliner rides are at 20.0 (concentrated failure). Those require completely different interventions.

### The Accuracy Stack (bottom-up)

```
Entity MAE per entity per day     ← This is where you diagnose and fix
  ↓ aggregate by park
Park MAE per park per day          ← This is where you spot park-level patterns
  ↓ aggregate across parks
WTI MAE per day                    ← This is what users see
  ↓ aggregate over time
Overall MAE                        ← This is the headline number
```

**Every accuracy report should show the entity layer.** Specifically:

1. **Worst 20 entities by MAE** — these are the highest-leverage improvements. If MK01 (Space Mountain) has MAE 15.0 while most entities are at 5.0, fixing MK01's model cuts overall MAE more than any architectural change. **Always display entity name alongside entity code** (join from dimentity.csv). Fred needs to see "MK01 — Space Mountain: MAE 15.2" not just "MK01: MAE 15.2." Entity codes are internal — attraction names are how humans think.

2. **Entity MAE distribution** — histogram showing how many entities are at each error level. A tight distribution means the model approach is working. A long tail means specific entities need attention.

3. **Entity-level bias direction** — which entities systematically over-predict vs under-predict? Consistent directional bias at the entity level suggests a training data problem for that entity (not enough recent observations, contaminated historical data, seasonal shift).

4. **Entity coverage gaps** — the 20.5% of entities without V3 models are on fallback (aggregate × 0.678). These are guaranteed to be worse. How much do they drag down overall accuracy?

5. **Entity-level trend** — is each entity's MAE improving, stable, or degrading over the last 14 days? Catch regressions at the entity level before they show up in park-level or WTI-level numbers.

### Diagnostics Tools

| Tool | Purpose | Primary Audience |
|------|---------|-----------------|
| `entity_accuracy.py` | Per-entity MAE, bias, worst-N list, distribution | Fred, Barney |
| `entity_deep_dive.py` | Single-entity forensic: predicted vs actual curves by hour, by day-of-week, by season | Barney, Wilma |
| `park_rollup.py` | Park-level accuracy aggregated from entity data | Fred (overview) |
| `wti_rollup.py` | WTI-level accuracy | Fred (headline) |
| `accuracy_drift.py` | Regression detection: any entity/park trending worse? | Gazoo (automated alert) |
| `model_coverage.py` | Which entities lack models, training data depth per entity | Barney (gap analysis) |

**The entity deep dive is the tool for model improvement.** When you want to understand why AK01 (Expedition Everest) is at MAE 12.0, you run `entity_deep_dive.py AK01` and see: prediction vs actual curves for the last 30 days, error broken down by time of day (is the model wrong at park open? at peak?), error by day of week (worse on weekends?), error by season (spring break problem?), training data volume and recency. That tells you exactly what the model is missing for that entity.

### How You Access This (Delivery Tiers)

**Tier 1 — Daily accuracy highlights in Discord (automatic)**

Step 10's daily output includes entity-level highlights. When accuracy posts to #pipeline (or via morning briefing), it shows:
- Worst 5 entities by name: "Expedition Everest (AK01) — MAE 14.2"
- Any entity whose MAE spiked >50% vs its 7-day average
- Any entity in the worst-20 for 3+ consecutive days (persistent problem)
- Entity coverage: how many on baseline models vs fallback

Short, scannable, human names. You see a problem entity in your morning glance, you know immediately what needs attention.

**Tier 2 — On-demand deep dive via Discord (you trigger it)**

You ask Wilma in any channel: "Run a deep dive on Expedition Everest" — or use Execute GO.py for a formal task. Wilma runs `entity_deep_dive.py AK01` and posts a structured report directly in the channel:

```
🔍 ENTITY DEEP DIVE: Expedition Everest (AK01)
Park: Animal Kingdom | Model: baseline | Training samples: 847

ACCURACY (last 30 days):
  MAE: 14.2 min | Bias: -8.7 min (under-predicting)
  Trend: ⬆️ WORSENING (was 9.1 two weeks ago)

WHERE THE ERROR LIVES:
  By time of day:  11 AM-2 PM worst (MAE 19.3)
                   Morning/evening acceptable (MAE 7.1)
  By day of week:  Sat-Sun MAE 18.4 vs Mon-Fri MAE 9.8
  By season:       Spring break period MAE 22.1 vs normal 8.3

TRAINING DATA:
  Total samples: 847 (412 real actual, 435 synthetic)
  Most recent: 2026-03-19
  Gap: No observations from Dec 2024-Jan 2025 (seasonal closure?)

DIAGNOSIS: Model severely under-predicts during peak hours on
busy days. Likely cause: insufficient high-crowd training data
for spring break periods. The 2016 min_training_year cutoff
removes exactly the pre-Pandora era that had lower crowds —
but current spring break data is still thin.
```

From that report, you either investigate further or fire off an Execute GO.py with a specific fix. The diagnosis section is the gold — it connects the numbers to a likely cause.

**Tier 3 — The Quarry dashboard (visual, future)**

Entity-level accuracy data feeds into The Quarry as an interactive view. Click an entity card, see the deep dive visually — trend lines, error heatmaps, training data timeline. Pebbles designs, Bam-Bam builds. Phase E territory — comes after the baseline is clean and the data pipeline is feeding it.

---

## Implementation Plan

### Phase A: Cleanup (Day 1 — zero risk to production)

1. **Kill ghost V2 cron:** Remove Clawdbot `pipeline-daily` job (or point it at V3)
2. **Archive dead V2 scripts:** Move to `scripts/archive/`: `run_daily_pipeline.sh`, `forecast_vectorized.py`, `calculate_wti_simple.py`, `bias_correction_framework.py`, `hybrid_pipeline_v2.py` (already in archive)
3. **Clean ghost model files:** Per entity, delete all model files except `model_v3.json` and its metadata. Then rename `model_v3.json` → `model_baseline.json`, `metadata_v3.json` → `metadata_baseline.json`.
4. **Clean ghost forecast files:** Remove `all_forecasts_v2_backup.parquet` and other dead forecast files. Keep only the active file that V3 writes to.
5. **Clean ghost training files:** Remove old matched pairs files that aren't used.

### Phase B: Rename & Restructure (Day 2-3 — carefully sequenced)

1. **Rename `pipeline_v3/` → `pipeline/`** — update all imports, the crontab entry, and any references
2. **Rename step files** to `step_NN_verb.py` pattern
3. **Update model file naming** in training and forecast steps: `model_v3.json` → `model_baseline.json`
4. **Update config** to remove all "v3"/"v4" references, use plain names
5. **Update crontab** to point at `pipeline/run.py`
6. **Single log stream:** `pipeline_YYYY-MM-DD.log` only

### Phase C: Purify Baseline (Day 3-4)

1. **Remove quantile mapping from `step_09_wti.py`** — WTI becomes pure aggregation
2. **Remove any remaining bias correction code** from all active scripts
3. **Run clean pipeline** and record true baseline MAE
4. **Document baseline:** `docs/BASELINE_ACCURACY.md` with per-park, per-horizon breakdown

### Phase D: Measure & Document (Day 5-7)

1. **Run 5-7 clean days** to establish stable baseline MAE
2. **Stratified accuracy report:** MAE by park, by horizon (1-day, 7-day, 30-day), by crowd level, by entity type
3. **Document everything:** Each step gets a docstring header explaining inputs, outputs, what it does, what it doesn't do
4. **Freeze baseline:** This MAE number is the starting line for all future improvements

### Phase E: Competition Framework (Week 2+)

1. **First challenger:** Add `day_of_week` as 6th feature. Train as `model_day_of_week_v1.json`. Shadow-run alongside baseline for 7-14 days.
2. **Quantile mapping challenger:** If we want to test QM, it enters as a named challenger applied to baseline predictions: "baseline_with_qm_v1". It earns its way in by beating pure baseline MAE.
3. **Promote or discard:** Based on data, not intuition.

---

## What's Explicitly NOT in V4 Baseline

These are all valid improvement ideas that belong in the competition framework, not in the baseline pipeline:

- Bias correction (any form)
- Quantile mapping / distribution stretching
- Day-of-week features
- Weather features
- Autoregressive features (yesterday's actuals)
- Holiday proximity features
- NGBoost / prediction intervals
- Entity clustering / transfer learning
- Temporal models (LSTM / Transformer)

Each enters as a named challenger. Each proves itself against baseline with data.

---

## Success Criteria

| Phase | Target | Measure |
|-------|--------|---------|
| A (Cleanup) | Zero dead code in active paths | No ghost crons, no ghost scripts, no ghost models |
| B (Rename) | Every file named by function | Code review passes "what does this do?" test |
| C (Purify) | Pure baseline running | No post-processing between model output and WTI |
| D (Measure) | Baseline MAE documented | 7-day stable measurement, per-park breakdown |
| E (Compete) | First challenger deployed | day_of_week_v1 shadow-running against baseline |

---

*Barney — Chief of Pipeline, Slate Rock & Gravel Co. 🪨*
