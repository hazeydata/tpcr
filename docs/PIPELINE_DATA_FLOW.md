# Pipeline Data Flow — Complete Documentation

**Created:** 2026-02-07  
**Last full audit:** 2026-02-18 (by Wilma)  
**Purpose:** Explain every step of the theme park wait time prediction pipeline in plain language. Intended audience: Fred, Barney (Claude on claude.ai), and future Wilma sessions.

---

## Table of Contents

1. [Big Picture — What This Pipeline Does](#1-big-picture)
2. [Key Concepts](#2-key-concepts)
3. [Data Sources](#3-data-sources)
4. [Pipeline Execution Order](#4-pipeline-execution-order)
5. [Step 0: S3 Sync](#step-0-s3-sync)
6. [Step 1: ETL (Extract, Transform, Load)](#step-1-etl)
7. [Step 1b: CSV → Parquet Conversion](#step-1b-csv-to-parquet)
8. [Step 2: Dimension Fetches](#step-2-dimension-fetches)
9. [Step 2a: Closures Module](#step-2a-closures-module)
10. [Step 2b: Impute Park Hours](#step-2b-impute-park-hours)
11. [Step 3: Posted Aggregates](#step-3-posted-aggregates)
12. [Step 4: Wait Time DB Report](#step-4-wait-time-db-report)
13. [Step 4b: Forecast Accuracy Evaluation](#step-4b-forecast-accuracy-evaluation)
14. [Step 4c: Synthetic Actuals Generation](#step-4c-synthetic-actuals-generation)
15. [Step 5: Matched Pairs + Model Training](#step-5-matched-pairs-and-training)
16. [Step 6: Forecast Generation](#step-6-forecast-generation)
17. [Step 7: WTI Calculation](#step-7-wti-calculation)
18. [Step 8: Post-Pipeline Outputs](#step-8-post-pipeline-outputs)
19. [Scheduled Jobs (Cron)](#scheduled-jobs)
20. [Skip-If-Unchanged Logic](#skip-if-unchanged-logic)
21. [Pipeline Timing](#pipeline-timing)
22. [Data Volumes](#data-volumes)
23. [Entity System & Filtering](#entity-system)
24. [Scripts Reference](#scripts-reference)
25. [Questions for Fred ❓](#questions-for-fred)
26. [TODOs 📋](#todos)

---

<a name="1-big-picture"></a>
## 1. Big Picture — What This Pipeline Does

This pipeline takes **raw wait time observations** from Disney, Universal, and other theme parks, and turns them into:

1. **Historical crowd analysis** — How busy was each park on every day going back to 2009?
2. **Two-year crowd forecasts** — How busy will each park be on any future day?
3. **A "Wait Time Index" (WTI)** — A single number per park per day that says "how busy is this park?" (like a stock index for crowds)

The pipeline runs daily at **6:00 AM ET**, takes about **10–12 minutes**, and feeds:
- A public website at **hazeydata.ai** (year-view heatmaps)
- A **Discord bot** that answers "how busy will Magic Kingdom be next Tuesday?"
- A **Twitch stream dashboard** with live crowd data
- A **REST API** at `localhost:8051` for custom queries

### The Flow in One Sentence

Raw S3 data → clean fact tables → matched pairs → trained ML models → forecasts for 2 years → WTI scores per park per day.

---

<a name="2-key-concepts"></a>
## 2. Key Concepts

### POSTED vs ACTUAL Wait Times

- **POSTED** = The wait time Disney puts on the sign in front of the ride (or shows in the app). Disney controls this number and they **intentionally overestimate** — if the sign says 60 minutes, you'll usually wait ~40–45 minutes. This is about 90 million rows in our data.
- **ACTUAL** = The wait time measured by TouringPlans field researchers who physically stand in line with stopwatches. This is ground truth. About 2.5 million rows — much rarer and more valuable.
- **PRIORITY** = Lightning Lane / Genie+ return times. Not yet used in models, about 25 million rows.

The whole point of the ML models is: **given a POSTED wait time + context (time of day, season, etc.), predict what the ACTUAL wait time really is.**

### Synthetic Actuals

Since we only have 2.5 million ACTUAL observations but 90 million POSTED observations, there are many days and times where we have POSTED data but no ACTUAL measurement. **Synthetic actuals** are POSTED times run through a trained conversion model to estimate what the ACTUAL would have been. They fill the gaps.

The conversion model (a global XGBoost model trained on all matched POSTED/ACTUAL pairs with geo-decay weighting) learns the systematic bias patterns: Disney's overestimation, lag in updating signs, time-of-day effects, etc. Geo-decay ensures the model learns the *current* POSTED→ACTUAL relationship, since the ratio has shifted over time.

### Wait Time Index (WTI)

WTI = a single number representing "average wait time across all rides at a park on a given day." Think of it like the Dow Jones — instead of tracking individual stocks, it gives you one number for the whole market (park). Higher WTI = busier day.

### Entity Codes

Each attraction has a unique code: `MK01` = Space Mountain (Magic Kingdom ride #01), `AK07` = Kilimanjaro Safaris (Animal Kingdom ride #07), etc. The first 2 characters usually identify the park (MK, EP, HS, AK, DL, CA, UF, IA, EU, UH, TDL, TDS).

### Operating Calendar

Not every ride operates every day. Some are seasonal, some close for refurbishment. The operating calendar tracks which entity is open on which day, so we don't include closed rides in WTI calculations or forecasts.

---

<a name="3-data-sources"></a>
## 3. Data Sources

| Source | What It Provides | How Accessed |
|--------|------------------|--------------|
| **TouringPlans S3 bucket** (`touringplans_stats`) | Raw wait time files (standby + fastpass), entity table, park hours, closures, events | AWS S3 sync |
| **Queue-Times** (live scraper) | Real-time posted wait times, scraped periodically | Staged locally in `staging/queue_times/`, merged during ETL |
| **Dimension tables** (built locally) | Calendar groups (`dimdategroupid`), seasons (`dimseason`), park hours (`dimparkhours`) | Built from S3 data + local logic |

### Key S3 Paths

- `export/wait_times/` → Raw standby wait time files
- `export/fastpass_times/` → Lightning Lane / Genie+ data
- `export/closures/` → Temporary closure CSVs
- Entity table, park hours, events, metatable → individual S3 keys

---

<a name="4-pipeline-execution-order"></a>
## 4. Pipeline Execution Order

The master orchestrator is **`scripts/run_daily_pipeline.sh`**. Here's the full order:

```
 Step 0:  S3 Sync              → Get new raw data from TouringPlans
 Step 1:  ETL                  → Parse raw data → clean CSVs
 Step 1b: CSV → Parquet        → Convert CSVs to fast-read parquet files  ⚡ FUTURE: refactor ETL to output parquet directly, skip CSVs
 Step 2:  Dimension Fetches    → Get/build entity table, park hours, seasons, calendar groups
 Step 2a: Closures Module      → Download closures from S3 + build operating calendar
 Step 2b: Impute Park Hours    → Fill missing future park hours from historical donors
 Step 3:  Posted Aggregates    → Compute historical average posted wait times per entity/time/season  ⚡ REVIEW: see note below
 Step 4:  Wait Time DB Report  → Generate a summary report of the database state
 Step 4b: Forecast Accuracy    → Compare yesterday's forecast vs what actually happened (BEFORE new forecasts overwrite old ones)
 Step 4c: Synthetic Actuals    → Run all historical POSTED through conversion model → estimated actuals
 Step 5:  Matched Pairs + Training  → Pair ACTUAL with POSTED, train per-entity XGBoost models  ⚡ RETHINK: see architectural note below
 Step 6:  Forecast Generation  → Generate 2-year predictions at 5-minute resolution
 Step 7:  WTI Calculation      → Compute Wait Time Index per park per day
 Step 8:  Post-Pipeline        → Landing chart, year-view export, deploy to hazeydata.ai, validation, API restart
```

---

<a name="step-0-s3-sync"></a>
## Step 0: S3 Sync

**Script:** `scripts/sync_s3_data.sh`  
**Time:** ~10 seconds  
**What it does:** Copies new raw data files from the TouringPlans S3 bucket to local storage so the rest of the pipeline reads from disk (fast, reliable, resumable).

- **Input:** S3 bucket `touringplans_stats` (paths: `export/wait_times/`, `export/fastpass_times/`)
- **Output:** `{output_base}/raw/export/wait_times/`, `{output_base}/raw/export/fastpass_times/`
- **Key detail:** Uses `aws s3 sync` which only downloads new/changed files. Requires `$HOME/.local/bin` in PATH for the AWS CLI (important for cron).

---

<a name="step-1-etl"></a>
## Step 1: ETL (Extract, Transform, Load)

**Script:** `scripts/run_etl.sh` → calls `src/get_tp_wait_time_data_from_s3.py`  
**Time:** ~1 minute  
**What it does:** The heart of data ingestion. Takes raw S3 files and queue-times scrapes and turns them into clean, deduplicated CSV fact tables.

### Process:

1. **Merge staged queue-times** — Any real-time scraper files in `staging/queue_times/` get merged into the fact tables, then the staged files are deleted.
2. **Read raw S3 files** — Parses the TouringPlans raw wait time files. Each file is classified as Standby, New Fastpass, or Old Fastpass format.
3. **Parse and classify** — Extracts `entity_code`, `observed_at`, `wait_time_type` (POSTED/ACTUAL/PRIORITY), and `wait_time_minutes`.
4. **Deduplicate** — A persistent SQLite database tracks seen rows so the same observation is never written twice across runs.
5. **Write clean CSVs** — One CSV per (park, date), organized as `fact_tables/clean/YYYY-MM/{park}_{YYYY-MM-DD}.csv`.

### Incremental Logic:
- Only processes new or modified S3 files (tracked in `state/`)
- Files that fail 3+ times are skipped permanently (OLD_FILE_DAYS threshold)
- Process lock prevents concurrent runs

- **Input:** `{output_base}/raw/export/` (from S3 sync) + `staging/queue_times/`
- **Output:** `{output_base}/fact_tables/clean/YYYY-MM/*.csv` (~50K files, 5.4 GB total)

### Fact Table Schema:

| Column | Type | Description |
|--------|------|-------------|
| `entity_code` | string | Attraction ID (e.g., MK01, AK07) |
| `observed_at` | ISO timestamp | When the observation was recorded (with timezone) |
| `wait_time_type` | string | `POSTED`, `ACTUAL`, or `PRIORITY` |
| `wait_time_minutes` | int | Wait time in minutes |

---

<a name="step-1b-csv-to-parquet"></a>
## Step 1b: CSV → Parquet Conversion

**Script:** `scripts/convert_to_parquet.py`  
**Time:** Variable (runs after ETL to capture new CSVs)  
**What it does:** Combines all CSVs from a calendar month into a single Parquet file. Parquet is a columnar format that's 10–50× faster to query than scanning thousands of tiny CSVs.

- **Input:** `fact_tables/clean/YYYY-MM/*.csv` (~50K files)
- **Output:** `fact_tables/parquet/YYYY-MM.parquet` (~202 monthly files, 611 MB total)
- **Added columns:** `observed_at_ts` (UTC timestamp), `park_date` (date), `park_code`
- **Key detail:** All downstream steps read parquet, never CSVs. This conversion is what makes the pipeline fast.

---

<a name="step-2-dimension-fetches"></a>
## Step 2: Dimension Fetches

**Script:** `scripts/run_dimension_fetches.sh`  
**Time:** ~30 seconds  
**What it does:** Fetches and builds the reference tables that add context to raw wait time data.

### Sub-scripts (run in order):

1. **`src/get_entity_table_from_s3.py`** → `dimentity.csv` — The master list of all attractions: entity codes, names, park, whether it's a Lightning Lane booth (fastpass_booth), and extinct_on dates.
2. **`src/get_park_hours_from_s3.py`** → `dimparkhours.csv` — Park opening/closing times for every date. Includes early entry hours and evening extra hours.
3. **`src/get_events_from_s3.py`** → Events data (holidays, special events).
4. **`src/get_metatable_from_s3.py`** → Metadata table.
5. **`src/build_dimdategroupid.py`** → `dimdategroupid.csv` — Assigns every date a "date group ID" like `FEB_WEEK2_TUE` or `THANKSGIVING`. This groups similar days together for modeling (a Tuesday in February 2024 behaves similarly to a Tuesday in February 2025).
6. **`src/build_dimseason.py`** → `dimseason.csv` — Assigns every date a season (`WINTER`, `SUMMER_PEAK`, `CHRISTMAS`, `THANKSGIVING`, etc.) and a season_year (`WINTER_2026`).

- **Output location:** `{output_base}/dimension_tables/`

### Why These Matter:

The ML models use `date_group_id`, `season`, and `season_year` as features. A model doesn't know what "February 15" means, but it can learn that `FEB_WEEK3_SAT` during `WINTER` season at a particular ride typically has certain crowd patterns.

---

<a name="step-2a-closures-module"></a>
## Step 2a: Closures Module

**Scripts:** `src/get_closures_from_s3.py` + `src/build_operating_calendar.py`  
**Time:** ~9 seconds (incremental)  
**What it does:** Determines which rides are open on which days.

### Part 1: Download Closures

Downloads temporary closure CSV files from S3 (`export/closures/`). These cover WDW, DLR, UOR, TDR, and USH parks. Each file lists rides that are temporarily closed with start and end dates.

### Part 2: Build Operating Calendar

Combines two sources:
1. **Permanent closures** — `extinct_on` dates from `dimentity.csv` (e.g., Splash Mountain closed permanently in 2023). An `extinct_on` before 2005-01-01 (including the `2000-01-01` sentinel) is treated as extinct. The calendar build does not warn, fail or change the calendar for it; S7 logs the single `PRE2005_EXTINCT_ON` warning per entity per run.
2. **Temporary closures** — From the S3 closure files (refurbishments, seasonal closures).

Creates a single table: for every (entity_code, park_date) combination, is that ride operating? `TRUE` or `FALSE`.

### Incremental Logic:
- First run: builds full history from earliest observation (2009) to today + 365 days.
- Subsequent runs: only refreshes a 7-day lookback window through today + 365 days. Historical dates are preserved untouched.
- Use `--full` to force a complete rebuild.

- **Input:** `dimentity.csv`, `raw_closures/*.csv`
- **Output:** `operating_calendar/operating_calendar.parquet`

### Used By:
- **Matched pairs** — Excludes closed entity-dates from pairing
- **Forecasts** — Doesn't generate predictions for closed rides
- **WTI** — Only includes operating rides in the park average

---

<a name="step-2b-impute-park-hours"></a>
## Step 2b: Impute Park Hours

**Script:** `scripts/impute_park_hours.py`  
**Time:** ~1 second  
**What it does:** Future dates often don't have official park hours yet (Disney hasn't announced them). This step fills in the gaps using historical data.

### How It Works:

1. **Find dates missing hours** — Look at `dimparkhours.csv` for future dates with no data.
2. **Match by calendar pattern** — Find all historical dates with the same `date_group_id` (e.g., `MAR_WEEK2_SAT`) that *do* have hours.
3. **Weight by recency:**
   - ≤1 year ago: weight 1.0
   - 2–4 years ago: weight 0.8 → 0.4
   - 5+ years ago: weight 0.1
4. **Pick weighted mode** — The most common opening/closing time combo, weighted by recency.
5. **Filter outlier donors** — Exclude abnormal park hours from the donor pool to prevent corporate events and early closures from contaminating imputation. Rules: reject any opening after 9:30 AM, reject closings before 5:00 PM for parks where that's abnormal (e.g., MK closing at 4 PM for a corporate event). Optionally require minimum frequency so that a rare (open, close) combo can't become a donor.
6. **Fallback** — If no matching date_group_id donors exist, use the mode park hours from the last 12 months for that park.

- **Input:** `dimparkhours.csv` + `dimdategroupid.csv`
- **Output:** Updated `dimparkhours.csv` with imputed rows (marked with `donor_date`)
- **Accuracy tracking:** `parkhours_donations.csv` logs which historical date donated hours to which future date, enabling accuracy measurement later.

> 📋 **TODO:** Add park hours imputation accuracy to the daily reporting pipeline.
---

<a name="step-3-posted-aggregates"></a>
## Step 3: Posted Aggregates

**Script:** `scripts/build_posted_aggregates_fast.py`  
**Time:** ~7 seconds  
**What it does:** Computes historical average POSTED wait times, grouped by entity × date_group_id × hour of day. These averages are used by the forecast step as the "expected posted time" for future predictions.

### Grouping:

For every combination of (entity_code, date_group_id, hour), this computes:
- Median posted wait time
- Mean posted wait time (with geo-decay weighting: recent data counts more, 2-year half-life)
- Count of observations

### Example:
> "Space Mountain (MK01) on a FEB_WEEK3_TUE at hour 14 (2 PM) typically has a posted wait of 55 minutes."

- **Input:** `fact_tables/parquet/*.parquet` + `dimdategroupid.csv`
- **Output:** `aggregates/posted_aggregates.parquet` (~1.7M rows, 19 MB)

### Also: Model Aggregates

A separate script (`scripts/build_model_aggregates.py`) computes similar aggregates at 15-minute resolution (time_slot 0–95) with additional stats (percentiles, std dev). These are used as fallback predictions for entities without trained models.

- **Output:** `aggregates/model_aggregates.parquet` (~6.4M rows, 85 MB)

---

<a name="step-4-wait-time-db-report"></a>
## Step 4: Wait Time DB Report

**Script:** `scripts/report_wait_time_db.py`  
**Time:** ~1 second  
**What it does:** Generates a quick summary report of the database state — how many rows, date ranges, entity counts, recent data freshness. Uses `--quick --lookback-days 14` in the daily pipeline for speed.

- **Output:** `reports/wait_time_db_report.md`
- **Purpose:** Debugging and monitoring. Not used by other pipeline steps.

---

<a name="step-4b-forecast-accuracy-evaluation"></a>
## Step 4b: Forecast Accuracy Evaluation

**Script:** `src/evaluate_forecast_accuracy.py`  
**Time:** Variable  
**What it does:** **Runs BEFORE new forecasts are generated** (critical timing!) to measure how accurate yesterday's predictions were now that we have actual data.

### Workflow:

1. **Archive current forecast** — Save the next 14 days of the current forecast file to `accuracy/archive/forecast_YYYY-MM-DD.parquet` before step 6 overwrites it. Also archives WTI predictions.
2. **Find evaluation dates** — Look for dates where we have both an archived forecast AND fresh actual observations in the fact tables.
3. **Compare forecast vs actuals (and synthetic actuals):**

   > 📋 **TODO:** Currently only compares against raw ACTUAL observations. Should also include synthetic actuals (POSTED→converted) as ground truth, which would dramatically increase evaluation coverage. May need to run conversion before this step if synthetic actuals for the evaluation dates don't exist yet.
   - Bucket actual observations into 5-minute slots (to match forecast granularity)
   - Join on (entity_code, park_date, time_slot)
   - Compute: signed error, absolute error, percentage error, forecast horizon (how many days ahead was the prediction?)
4. **Aggregate results** at three levels:
   - **Slot-level:** Every 5-minute time slot for every entity-date
   - **Entity-date level:** MAE, bias, RMSE, MAPE per entity per day
   - **WTI level:** Compares park-level WTI forecast vs actual WTI

### Output files (accumulating — appended each run):

| File | Level | Description |
|------|-------|-------------|
| `accuracy/slot_accuracy.parquet` | Per entity, per 5-min slot | Finest grain |
| `accuracy/entity_daily_accuracy.parquet` | Per entity, per day | Aggregated |
| `accuracy/wti_accuracy.parquet` | Per park, per day | WTI forecast vs actual |
| `accuracy/accuracy_summary.json` | Overall | Dashboard-friendly summary |
| `accuracy/archive/forecast_*.parquet` | — | Archived forecasts for future comparison |

### Key Metrics:
- **MAE** (Mean Absolute Error) — Average minutes off
- **Bias** — Systematic over/under-prediction (positive = forecast too high)
- **MAPE** — Percentage error
- **Horizon analysis** — Accuracy broken down by 1-day, 7-day, and 30-day forecast horizons

**Non-fatal:** If this step fails, the pipeline continues. Accuracy tracking is important but shouldn't block data production.

---

<a name="step-4c-synthetic-actuals-generation"></a>
## Step 4c: Synthetic Actuals Generation

**Script:** `scripts/generate_synthetic_actuals.py` → `src/processors/synthetic_actuals.py`  
**Time:** Variable (processes all parks in chunks)  
**What it does:** Runs every historical POSTED observation through the global POSTED→ACTUAL conversion model to produce estimated "synthetic actual" wait times.

### Why This Exists:

We have 90M POSTED observations but only 2.5M ACTUAL observations. Many entity-dates have POSTED data but no ground truth ACTUAL. Synthetic actuals fill these gaps, enabling:
- More complete historical WTI (using both real actuals + synthetic actuals)
- Dashboard curve display for dates without real actuals

### How It Works:

1. **Load conversion model** — A global XGBoost model (from `src/processors/posted_to_actual.py`) trained on all matched POSTED↔ACTUAL pairs across all entities, with geo-decay sample weights (730-day half-life) so recent patterns dominate. See `docs/XGBOOST_PARAMS.md` for full parameter details.
2. **Compute rolling features** via DuckDB window functions:
   - `posted_delta_15m`, `posted_delta_30m`, `posted_delta_60m` (how has the posted time changed recently?)
   - `posted_rolling_mean_30m`, `posted_rolling_mean_60m` (recent average)
   - `posted_volatility_30m` (how jumpy is the posted time?)
3. **Join with dimension tables** — Add date_group_id, season, park hours, encode categoricals.
4. **Run model inference** — Predict actual from posted + context features.
5. **Clip results** to 0–300 minutes (sanity bounds).
6. **Save per-entity** parquet files.

### Filtering:
- Only STANDBY entities (excludes Lightning Lane booths via `dimentity.csv` `fastpass_booth = FALSE`)
- Only entities with ≥500 POSTED observations
- Processed in park-level chunks to avoid OOM (90M rows is a lot!)

- **Input:** `fact_tables/parquet/*.parquet`, conversion model from `models/conversion_model/`
- **Output:** `synthetic_actuals/{entity_code}.parquet` per entity
- **Key columns:** `entity_code`, `park_date`, `observed_at`, `posted_time`, `synthetic_actual`, `source="synthetic"`

**Non-fatal:** If this step fails, the pipeline continues. Dashboard falls back to raw actuals.

---

<a name="step-5-matched-pairs-and-training"></a>
## Step 5: Matched Pairs + Model Training

**Script:** `scripts/hybrid_pipeline_v2.py` (called with `--skip-scoring`)  
**Time:** ~6 seconds (pairs) + ~10–30 seconds (training, dirty entities only)

This is the core ML step, split into two sub-steps.

### Step 5a: Matched Pairs

**What it does:** For each ACTUAL observation, find the closest POSTED observation for the same ride on the same day within a ±15-minute window. This creates training data: "when the sign said X, the actual wait was Y."

#### Process:
1. Load all ACTUAL observations from fact tables
2. For each ACTUAL, find all POSTED for the same entity and park_date within ±15 minutes
3. Pick the POSTED with the smallest time difference (best temporal match)
4. Join with dimension tables for features: `date_group_id`, `season`, `season_year`, park hours
5. Compute time features: `hour_of_day`, `mins_since_6am`, `mins_since_open`
6. Label-encode categorical features (encodings extended incrementally — new categories get next ID, existing preserved)
7. Append new pairs to existing file

#### Filtering:
- Excludes Lightning Lane booth entities (`fastpass_booth = FALSE`)
- Excludes entity-dates where the operating calendar says the ride was closed
- Only pairs where both posted and actual wait times are > 0

#### Incremental Logic:
- Tracks `last_paired_at` timestamp in `state/matched_pairs_state.json`
- Only pairs ACTUAL observations newer than last run
- Appends to existing `all_pairs_v2.parquet`
- Use `--full-pairs` to force a complete rebuild

#### Fallback Ratios:
After pairing, computes dynamic fallback ratios from all cumulative pairs:
- Per-entity ratio: `actual_sum / posted_sum` (if entity has ≥50 pairs)
- Global ratio: weighted average across all pairs (currently **~0.678**, meaning ACTUALs average 68% of POSTED)
- Saved to `state/fallback_ratios.json`

- **Input:** `fact_tables/parquet/*.parquet`, dimension tables, operating calendar
- **Output:** `matched_pairs/all_pairs_v2.parquet` (~2.4M pairs, ~120 MB)

#### Matched Pairs Schema:

| Column | Type | Description |
|--------|------|-------------|
| `entity_code` | string | Attraction ID |
| `observed_at` | timestamp | ACTUAL observation time |
| `park_date` | date | Operating date |
| `actual_time` | float | Actual wait (target) |
| `posted_time` | float | Posted wait (feature) |
| `date_group_id` | string | Calendar group (e.g., `FEB_WEEK2_TUE`) |
| `date_group_id_encoded` | int | Encoded calendar group |
| `season` / `season_encoded` | string/int | Season |
| `season_year` / `season_year_encoded` | string/int | Season+year |
| `hour_of_day` | int | Hour (0–23) |
| `mins_since_6am` | int | Minutes since 6 AM |
| `mins_since_open` | int | Minutes since park opened |

**Note:** Geo decay weights are NOT stored in pairs — they're computed fresh at training time. This is intentional: pairs are static facts ("this ACTUAL matched this POSTED"), but the importance weight should reflect how recent the data is *right now*, not when it was paired.

### Step 5b: Model Training (Julia XGBoost)

**What it does:** Trains one XGBoost model per entity (ride) to predict ACTUAL wait from POSTED wait + context features.

#### Training Features (7 features):

| Feature | Description |
|---------|-------------|
| `posted_time` | Posted wait time (primary predictor) |
| `mins_since_6am` | Minutes since 6 AM |
| `mins_since_open` | Minutes since park opened |
| `hour_of_day` | Hour of day (0–23) |
| `date_group_id_encoded` | Calendar group |
| `season_encoded` | Season |
| `season_year_encoded` | Season+year |

#### Geo Decay Weighting:
```
weight = 0.5^(days_since_observed / 730)
```
- Half-life: 730 days (2 years)
- Recent data matters more. A pair from yesterday weighs ~1.0; from 2 years ago weighs 0.5; from 4 years ago weighs 0.25.
- Computed at training time from `park_date`, not stored in pairs.

#### Training Process:
1. Python queries `entity_index.sqlite` for **dirty entities** (new data since last training)
2. Writes dirty entity codes to `state/entities_to_train.txt`
3. Julia loads all pairs, filters to eligible (≥500 pairs) AND dirty entities
4. For each entity:
   - Load matched pairs
   - Compute geo_decay_weight from park_date
   - 85% train / 15% validation (chronological split)
   - Train XGBoost with early stopping (20 rounds patience)
   - Save model as `models/{entity}/model_julia_v2.json` with metadata
5. Python marks retrained entities as modeled (`last_modeled_at = now`)

#### Eligibility Gate:
- Entity needs ≥500 cumulative matched pairs to get its own model
- ~142 entities are eligible; ~600+ use fallback

#### Synthetic Pairs (optional, enabled in cron):
When `--use-synthetic` flag is passed:
1. `scripts/build_synthetic_pairs.py` creates training pairs from synthetic actuals (same schema as real pairs, with `is_synthetic = True`)
2. Synthetic pairs are combined with real pairs for training
3. Julia handles both — synthetic data augments training for entities with sparse real actuals

#### Hyperparameters:

| Parameter | Value |
|-----------|-------|
| `num_round` | 2000 (max) |
| `max_depth` | 10 |
| `eta` (learning rate) | 0.1 |
| `subsample` | 0.8 |
| `colsample_bytree` | 0.8 |
| `early_stopping_rounds` | 20 |

- **Input:** `matched_pairs/all_pairs_v2.parquet`, `state/entities_to_train.txt`
- **Output:** `models/{entity}/model_julia_v2.json` + `metadata_julia_v2.json`
- **Model label:** `XGBOOST_BASE_MODEL`

---

### Step 5c: Scope-and-Scale Group Models (Python XGBoost)

**Script:** `scripts/train_scope_scale_models.py`  
**Time:** ~15 seconds  
**What it does:** Trains pooled XGBoost models by `scope_and_scale` category (from `dimentity.csv`). These group models serve as fallbacks for EU (Epic Universe) entities that lack per-entity models.

#### Why:
EU entities opened May 2025 and have limited training data. Entities without enough observations (e.g., EU03, EU08, EU12) can't get per-entity models but can benefit from group-level patterns learned from hundreds of similar entities across all parks.

#### How:
1. Reads matched pairs + dimentity.csv scope_and_scale mapping
2. For each of 5 scope categories (Super Headliner, Headliner, Major Attraction, Minor Attraction, Diversion):
   - Pools ALL observations from ALL entities with that scope_and_scale
   - Adds `entity_code_encoded` as a categorical feature (integer encoding)
   - Trains **actuals model** (6 features: mins_since_6am, mins_since_open, date_group_id_encoded, season_encoded, season_year_encoded, entity_code_encoded)
   - Trains **V2 model** (8 features: + posted_time, hour_of_day)
   - Uses geo_decay weights (same as per-entity training)
3. Saves models to `models/_scope_scale_{category}/`

#### Training Data Volumes (Feb 2026):
| Category | Entities | Observations | Actuals MAE |
|----------|----------|--------------|-------------|
| Super Headliner | 36 | 464K | 17.0 min |
| Headliner | 42 | 582K | 11.8 min |
| Major Attraction | 63 | 558K | 11.5 min |
| Minor Attraction | 107 | 779K | 8.4 min |
| Diversion | 17 | 15K | 9.7 min |

#### Output Files (per scope category):
- `models/_scope_scale_{category}/model_scope_scale_actuals.json` — actuals model
- `models/_scope_scale_{category}/model_scope_scale_v2.json` — V2 model
- `models/_scope_scale_{category}/entity_code_mapping.json` — entity→integer encoding
- `models/_scope_scale_{category}/metadata.json` — training metadata

#### Note:
For EU entities not in the training data (unseen entity_code), XGBoost uses group-level patterns without entity-specific adjustment — exactly what we want for cold start.

---

<a name="step-6-forecast-generation"></a>
## Step 6: Forecast Generation

**Script:** `scripts/forecast_vectorized.py --days 730`  
**Time:** ~8 minutes  
**What it does:** Generates predicted actual wait times for every ride, every 5 minutes, for the next 2 years. About 159 million predictions total.

### How It Works:

1. **Load encodings** from matched pairs (so forecast features use the same encoding as training)
2. **Load park hours** (including imputed hours for future dates)
3. **Load model aggregates** (for estimating what the posted time would be on future dates)
4. **Load operating calendar** (to skip closed rides)
5. **Generate time grid** — For each future date, create a row for every 5-minute slot within park operating hours
6. **For each entity:**
   - Filter time grid to this park's operating hours
   - Estimate `posted_time` from model aggregates: "What is the typical posted wait for this ride at this time on this type of day?"
   - Compute `mins_since_open` from park hours
   - If entity has a trained model → predict using XGBoost
   - If no model → use aggregate median × fallback ratio
   - Cap predictions at entity's historical p95 posted wait (prevents unreasonable outliers)

### Prediction Priority:

| Priority | Method | When Used |
|----------|--------|-----------|
| 1 | **Actuals Model** | Entity has `model_julia_actuals.json` (actuals-first, no posted_time) |
| 2 | **V2 Model** | Entity has `model_julia_v2.json` (with posted_time) |
| 3 | **Scope-Scale Group Model** | EU entity with no per-entity model, has `scope_and_scale` value with trained group model |
| 4 | **Aggregate** | No model, but aggregate data exists for (entity, date_group_id, time_slot) |
| 5 | **Fallback Ratio** | No model, no aggregate. Uses `posted_estimate × entity_ratio` |

### Entity Filtering:
- Excludes Lightning Lane booths (`fastpass_booth = FALSE`)
- Excludes extinct/fully-closed entities (zero operating dates in calendar) — logged as `SKIP … reason=extinct`; entities with no dates in the window are logged as `not_operating`
- Entities not in the operating calendar at all are assumed operating (graceful fallback)
- **S8 failure handling:** intended behaviour (Fred, Oct 24 12:28 AM ET): any S8 entity error (`error:<class>`) raises `StageFailed` after outputs are written. `pipeline.py` stops, so S9-S11 do not run, and the process exits nonzero. This applies in `--shadow` mode too (Spec R5, Fred, Oct 24 1:34 AM ET): an S8 entity error is the one failure that stops a shadow run; any other failed step in shadow mode is logged and the run continues (the run still exits nonzero). Contrast with S7, which warns and continues on entity errors.
- **EU entities with `scope_and_scale` are added even without POSTED data** (use scope-scale group models)

### Model Type Detection:
Checks model metadata for `model_label`. Lite models (4 features: posted_time, mins_since_6am, mins_since_open, hour_of_day) get different feature selection than full V2 models (7 features).

- **Input:** Models, model aggregates, park hours, operating calendar, dimension tables, fallback ratios
- **Output:** `curves/forecast_parquet/all_forecasts.parquet` (~159M rows, ~44 MB)

### Forecast Schema:

| Column | Type | Description |
|--------|------|-------------|
| `entity_code` | string | Attraction ID |
| `park_date` | date | Future date |
| `time_slot` | time | 5-minute slot (e.g., 09:00:00, 09:05:00) |
| `predicted_actual` | int | Predicted actual wait (rounded integer) |
| `prediction_method` | string | `model_v2`, `model_lite`, `aggregate`, or `fallback_ratio` |

---

<a name="step-7-wti-calculation"></a>
## Step 7: WTI Calculation

**Script:** `scripts/calculate_wti_simple.py`  
**Time:** ~2 seconds  
**What it does:** Computes a single Wait Time Index number per park per day, combining historical observations and future forecasts.

### ⚠️ KEY PRINCIPLE (Fred confirmed 2026-02-18):

> For days where we have actual observations (past), we do NOT use forecasted values when calculating WTI. Forecasts should only be used for dates where we have not yet observed any wait times.

### Historical WTI (Past Days):

Uses two data sources combined with weighting:

1. **Synthetic actuals** (POSTED → converted via model) — weight **1.0**
2. **Real ACTUAL observations** (ground truth) — weight **3.5**

The 3.5× weight on real actuals reflects that ground truth is far more reliable than model-converted posted times.

#### Process:
1. Load all synthetic actuals from `synthetic_actuals/*.parquet`
2. Load all real ACTUAL observations from fact tables
3. Compute weighted average per entity per day (using the 3.5:1 weighting)
4. Average across all entities for that park-date → WTI

**Fallback:** If synthetic actuals aren't available, falls back to COALESCE logic: prefer ACTUAL average, fall back to POSTED average per entity per day.

❓ **Question for Fred:** The current code uses POSTED raw times as the fallback (when no synthetic actuals exist), but the comment says "NEVER use raw POSTED times." Is the fallback path just for initial bootstrap before the first synthetic actuals run? Should it be removed now that synthetic actuals exist?

### Forecast WTI (Future Days):

Simply averages `predicted_actual` from the forecast file, filtered by the operating calendar (only includes rides that are operating).

### Bias Correction (Adaptive):

After computing raw WTI values, the script applies an **adaptive per-park bias correction** to forecast WTI:

1. Load `accuracy/wti_accuracy.parquet` (from the accuracy evaluation step)
2. Compute average forecast error per park over the last 14 days
3. Apply correction: `corrected_wti = forecast_wti - avg_bias`
4. Floor at 5.0 (WTI shouldn't go below a reasonable minimum)
5. Parks without enough accuracy data (< 2 dates) fall back to overall bias correction

**The idea:** If our forecasts have been consistently running 5 points too high for Magic Kingdom lately, subtract 5 from future MK WTI predictions. As models improve, the bias shrinks to zero and corrections disappear.

### Deduplication (Historical Wins):

When combining historical and forecast WTI, if a park-date appears in BOTH (overlap period):
- Sort by source: `historical` comes before `forecast` alphabetically
- Drop duplicates, keeping first → **historical always wins**

This ensures that past days always use observed data, never forecasts.

- **Input:** Synthetic actuals, fact tables (for real actuals), forecasts, operating calendar, accuracy data
- **Output:** `wti/wti.parquet`

### WTI Schema:

| Column | Type | Description |
|--------|------|-------------|
| `park_code` | string | Park ID (MK, EP, HS, etc.) |
| `park_date` | date | Date |
| `wti` | float | Wait Time Index (rounded to 1 decimal) |
| `n_entities` | int | Number of entities included |
| `source` | string | `historical` or `forecast` |

---

<a name="step-8-post-pipeline-outputs"></a>
## Step 8: Post-Pipeline Outputs

After the core pipeline completes, several downstream steps run:

### Landing Page Chart
**Script:** `scripts/generate_landing_chart.py`  
Generates a 7-day MK forecast chart image for the hazeydata.ai landing page. Uses the forecast_image module from the Discord bot.

### Year-View Data Export
**Script:** `scripts/export_year_view_data.py`  
Exports per-park JSON files for the hazeydata.ai interactive heatmap:
- Reads WTI parquet
- Enriches with headliner ride peak waits from forecast curves (top 3 rides per park)
- Outputs to `/home/wilma/hazeydata.ai/year-view-data/<PARK_CODE>.json`
- Commits and deploys to Cloudflare Pages

### Park WTI Distributions
**Script:** `scripts/compute_park_wti_distributions.py`  
Computes per-park percentile distributions (p5, p25, median, p75, p95) from historical WTI. Used by all visual surfaces for "Benedictus" color scaling — a "red day" at MK means busy *for MK*, not compared to all parks.
- **Output:** `state/park_wti_distributions.json`

### Validation
**Script:** `src/validate_pipeline_output.py`  
Runs data quality checks:
- Forecast coverage: Does tomorrow have forecast curves for all active parks?
- WTI anomaly: Any dates where WTI jumps >30% from neighbors?
- Entity coverage: Non-extinct entities without trained models?
- Forecast date range: Do forecasts extend ≥7 days into the future?

### API Restart
If the dashboard API (`dashboard/api.py`) is running, it gets restarted to pick up new data.

### Discord Daily Report
**Script:** `tpcr-discord-bot/daily_report.py` (Mac Mini launchd job `com.hazeydata.tpcr-crowd-report`, 7:00 AM; see `launchd/README.md`)  
Waits up to 90 min for `<root>/logs/pipeline_metrics_<date>.json` with `status: done`, then posts the daily crowd report to Discord. A `--dry-run` flag previews it; a posted sentinel prevents duplicates.

---

<a name="scheduled-jobs"></a>
## Scheduled Jobs (Cron)

| Schedule | Job | Script |
|----------|-----|--------|
| **6:00 AM ET** | Daily pipeline | `run_daily_pipeline.sh --skip-dropbox-check --skip-if-unchanged --use-synthetic` |
| **7:00 AM** | Discord daily report (launchd) | `launchd/com.hazeydata.tpcr-crowd-report.plist` → `tpcr-discord-bot/daily_report.py --wait-for-pipeline 90` |
| **9:00 AM** | Crowd report watchdog (launchd) | `launchd/com.hazeydata.tpcr-crowd-report-watchdog.plist` → `scripts/tpcr_crowd_report_watchdog.py --check-only` |
| **3:00 AM ET** | B2 cloud backup | `/home/wilma/backup-to-b2.sh` |

### Pipeline Flags in Production:
- `--skip-dropbox-check` — Output isn't on Dropbox
- `--skip-if-unchanged` — Skip training/forecast/WTI if no new data (fast incremental mode)
- `--use-synthetic` — Include synthetic actuals in training data

---

<a name="skip-if-unchanged-logic"></a>
## Skip-If-Unchanged Logic

**Flag:** `--skip-if-unchanged` on `run_daily_pipeline.sh`  
**Script:** `scripts/pipeline_state.py`

When enabled, the pipeline skips expensive steps if data hasn't changed:

```
ETL → updates entity_index.sqlite with latest_observed_at
  ↓
Training → skip if NO dirty entities (latest_observed_at > last_modeled_at)
  ↓
Forecast → skip if training was skipped (no new models = forecasts still valid)
  ↓
WTI → skip if forecast was skipped (no new forecasts = WTI still valid)
```

### Entity Index (`state/entity_index.sqlite`):
Tracks per-entity:
- `latest_observed_at` — Timestamp of newest observation from ETL
- `last_modeled_at` — Timestamp when we last trained a model for this entity

An entity is **dirty** when `last_modeled_at IS NULL` or `latest_observed_at > last_modeled_at`.

### Run Manifest (`state/run_manifest.json`):
Each pipeline run records which steps actually executed, enabling cascade logic (if training didn't run → forecast can skip too).

---

<a name="pipeline-timing"></a>
## Pipeline Timing

### Daily Incremental (Typical Production Run)

| Step | Script | Time |
|------|--------|------|
| S3 Sync | `sync_s3_data.sh` | ~10s |
| ETL | `run_etl.sh` | ~1 min |
| CSV→Parquet | `convert_to_parquet.py` | ~varies |
| Dimensions | `run_dimension_fetches.sh` | ~30s |
| Operating Calendar | `build_operating_calendar.py` | ~9s |
| Impute Park Hours | `impute_park_hours.py` | ~1s |
| Posted Aggregates | `build_posted_aggregates_fast.py` | ~7s |
| Report | `report_wait_time_db.py` | ~1s |
| Forecast Accuracy | `evaluate_forecast_accuracy.py` | ~varies |
| Synthetic Actuals | `generate_synthetic_actuals.py` | ~varies |
| Matched Pairs | `hybrid_pipeline_v2.py` | ~6s |
| Training (Julia) | via `hybrid_pipeline_v2.py` | ~10–30s (dirty only) |
| Forecast | `forecast_vectorized.py` | ~8 min |
| WTI | `calculate_wti_simple.py` | ~2s |
| **TOTAL** | | **~10–12 min** |

### Full Rebuild (All Steps, No Skipping)

| Step | Time |
|------|------|
| Matched Pairs | ~102s |
| Training (all entities) | ~93s |
| Scoring | ~180s |
| **Total** | **~6 min** (for just pairs+training+scoring) |

*Previously took 8+ hours before the incremental optimizations.*

---

<a name="data-volumes"></a>
## Data Volumes

| Data | Rows | Size | Files | Format |
|------|------|------|-------|--------|
| Fact Tables (parquet) | ~120M | 611 MB | 202 | Monthly parquet |
| Fact Tables (CSV) | ~120M | 5.4 GB | ~50K | ⚠️ Don't scan! |
| Matched Pairs | ~2.4M | ~120 MB | 1 | Parquet |
| Forecasts | ~159M | ~44 MB | 1 | Parquet |
| Model Aggregates | ~6.4M | 85 MB | 1 | Parquet |
| Posted Aggregates | ~1.7M | 19 MB | 1 | Parquet |
| WTI | ~48K+ | ~1 MB | 1 | Parquet |
| Synthetic Actuals | ~90M | varies | per-entity | Parquet |
| Models | ~142 | varies | per-entity | JSON |

### Date Range:
- Historical data: **2009-03-02** to present
- Forecasts: Tomorrow to +2 years (731 days)
- Parks: 12 (MK, EP, HS, AK, DL, CA, UF, IA, EU, UH, TDL, TDS)

---

<a name="entity-system"></a>
## Entity System & Filtering

### Method Breakdown:

| Method | Entities | Criteria |
|--------|----------|----------|
| **Model (XGBOOST_BASE_MODEL)** | ~142 | ≥500 matched pairs |
| **Fallback (ratio/aggregate)** | ~600+ | <500 pairs |
| **Total** | ~750 | All entities with POSTED data |

### Filtering Rules Applied Throughout:
- **Lightning Lane booths excluded** — `dimentity.csv` `fastpass_booth = FALSE`. These record LL return times, not standby waits.
- **Extinct entities** — Rides with `extinct_on` dates are excluded from S7 training (via `extinct_on`; pre-2005 values, including the 2000-01-01 sentinel, count as extinct and log a `PRE2005_EXTINCT_ON` warning plus a `SKIP_NOTE … pre2005_extinct_on count=<n>` line) and from future forecasts (via the operating calendar). Skips are logged per entity with a reason and are not counted as `failed`.
- **Operating calendar** — Temporarily closed rides excluded from forecasts and WTI.
- **Minimum observation thresholds** — 500 matched pairs for model training, 50 pairs for per-entity fallback ratio, 500 POSTED observations for synthetic actuals.

---

<a name="scripts-reference"></a>
## Scripts Reference

### ✅ Production Scripts (Used in Daily Pipeline)

| Script | Purpose |
|--------|---------|
| `scripts/run_daily_pipeline.sh` | Master orchestrator |
| `scripts/sync_s3_data.sh` | S3 data sync |
| `scripts/run_etl.sh` | ETL wrapper |
| `scripts/convert_to_parquet.py` | CSV→Parquet |
| `scripts/run_dimension_fetches.sh` | Dimension table fetches |
| `src/build_operating_calendar.py` | Operating calendar |
| `scripts/impute_park_hours.py` | Park hours imputation |
| `scripts/build_posted_aggregates_fast.py` | Posted aggregates |
| `scripts/report_wait_time_db.py` | DB report |
| `src/evaluate_forecast_accuracy.py` | Accuracy evaluation |
| `scripts/generate_synthetic_actuals.py` | Synthetic actuals |
| `scripts/hybrid_pipeline_v2.py` | Matched pairs + training |
| `julia-ml/train_v2.jl` | Julia XGBoost training |
| `scripts/forecast_vectorized.py` | Forecast generation |
| `scripts/calculate_wti_simple.py` | WTI calculation |
| `scripts/pipeline_state.py` | Skip-if-unchanged logic |
| `scripts/update_pipeline_status.py` | Dashboard status updates |
| `scripts/generate_landing_chart.py` | Landing page chart |
| `scripts/export_year_view_data.py` | Year-view JSON export |
| `scripts/compute_park_wti_distributions.py` | Per-park WTI percentiles |
| `src/validate_pipeline_output.py` | Post-run validation |

### ❌ DO NOT USE (Slow/Broken)

| Script | Problem | Use Instead |
|--------|---------|-------------|
| `build_posted_aggregates.py` | Scans 50K CSVs, crashes | `build_posted_aggregates_fast.py` |
| `generate_forecast.py` | Non-vectorized, takes hours | `forecast_vectorized.py` |
| `hybrid_pipeline.py` | V1, outdated | `hybrid_pipeline_v2.py` |

### Utility / Analysis Scripts

| Script | Purpose |
|--------|---------|
| `scripts/build_model_aggregates.py` | Model aggregates (fallback predictions) |
| `scripts/build_synthetic_pairs.py` | Synthetic pairs for training augmentation |
| `scripts/daily_accuracy_report.py` | Telegram-friendly accuracy report |
| `scripts/entity_wti_diagnostics.py` | Per-entity WTI diagnostics |
| `scripts/train_conversion_model.py` | Train POSTED→ACTUAL conversion model |
| `scripts/score_historical.py` | Score all historical POSTED observations |

---

<a name="decisions-log"></a>
## Decisions Log

### 2026-03-04 — Conversion Model Overhaul

Deep audit found synthetic actuals were systematically inflated (8–17 min/hour above ground truth for high-wait rides). Root cause: conversion model was over-regularized (stopped at 19 trees) and had no geo-decay weighting, so it learned a blended historical POSTED→ACTUAL ratio rather than the current one.

| # | Decision | Status |
|---|----------|--------|
| 1 | **Add geo-decay to conversion model** — Same 730-day half-life as per-entity models. Ratio has shifted significantly over time. | ✅ **DONE** |
| 2 | **Fix conversion model hyperparameters** — `min_child_weight` 10→3, `subsample` 0.5→0.8, `colsample_bytree` 1.0→0.8, `max_depth` 6→8, `early_stopping` 50→20. Aligned with proven per-entity settings. | ✅ **DONE** |
| 3 | **Retrain conversion model** — Required after param changes. | ✅ **DONE** |
| 4 | **Revisit synthetic vs real actuals weighting in actuals-first training** — Current: equal weight per row (98% synthetic drowns real signal). Need to investigate after conversion model is fixed. | 📋 TODO |

### 2026-02-18 — Fred's Architecture Review

Fred reviewed all open questions. Resolved decisions:

| # | Decision | Status |
|---|----------|--------|
| 1 | **Remove WTI fallback to raw POSTED** — Stale now that synthetic actuals exist. If we have raw POSTED, make synthetic actuals. Should be impossible to have POSTED without synthetic. | ✅ Remove fallback |
| 2 | **Model aggregates** — May not be needed anymore. Review whether anything still depends on them. | ⚡ Review & potentially remove |
| 3 | **Historical scoring** — Build once, skip daily. Rebuild only when needed (e.g., predictions outside park hours). | ✅ Keep as-is |
| 4 | **P95 cap on forecasts** — Removed. Models underpredict anyway, XGBoost handles outliers natively. | ✅ **DONE** (code updated 2026-02-18) |
| 5 | **Conversion model retraining** — Schedule monthly. | ✅ Cron added (1st of each month) |
| 6 | **3.5× weight on real actuals** — Reasonable initial guess, keep it. Real actuals have measurement error too. | ✅ Keep as-is, revisit if needed |
| 7 | **14-day bias correction window** — Fine for now. Revisit periodically. | ✅ Keep, revisit quarterly |
| 8 | **Horizon-specific bias correction** — Track accuracy over time, don't act yet. | 📊 Monitor |

### Fred's Architectural Notes (same date):

- **ETL → Parquet directly:** Why produce CSVs then convert? Future refactor: ETL should output parquet directly, eliminating Step 1b.
- **Posted aggregates review:** Are these still needed? They feed the forecast step's "expected posted time" for entities without models. If we move to synthetic-actuals-based training, this step may change.
- **"Actuals-First" method:** With synthetic actuals, we're dropping POSTED as a training feature. The POSTED→ACTUAL relationship is already captured by the conversion model (separation of concerns). Forecast models will train on temporal features only (mins_since_6am, mins_since_open, date_group_id, season, season_year), predicting actual wait times directly. POSTED times exist solely to feed the conversion model. This is a significant architectural upgrade — A/B comparison required before replacing production models.
- **Accuracy eval ground truth:** Include synthetic actuals as "observations" in accuracy evaluation, not just raw ACTUAL. This dramatically increases evaluation coverage.
- **Park hours donor filtering:** Exclude outlier hours from donor pool (e.g., MK closing at 4 PM for corporate events). Reject openings after 9:30 AM, closings before 5 PM where abnormal. Consider minimum frequency filter.
- **Park hours accuracy reporting:** Add imputation accuracy to the daily reporting pipeline.

---

<a name="todos"></a>
## TODOs 📋

1. 📋 **TODO: Remove WTI POSTED fallback** — Delete the COALESCE fallback path in `calculate_wti_simple.py`. If POSTED data exists without synthetic actuals, generate synthetic actuals first.

2. 📋 **TODO: Review model aggregates necessity** — Determine if `build_model_aggregates.py` is still needed or if it can be retired.

3. 📋 **TODO: Ride closure handling in data collection** — The live scraper (`get_wait_times_from_queue_times.py`) silently skips rides where `is_open: false`. This means closed rides disappear from output. Two types of closures need different handling:
   - **Scheduled closures** (refurbishments) — known in advance, predictable
   - **Temporary closures** (breakdowns, weather) — unplanned, short duration
   - **Decision needed:** Should the Discord bot show closed rides with a "Closed" label? Should forecasts exclude scheduled closures?

4. 📋 **TODO: Park WTI distributions in daily pipeline** — `compute_park_wti_distributions.py` updates the color scaling thresholds, but it's not explicitly called in `run_daily_pipeline.sh`. It should run after WTI calculation so the web/Discord/stream always have fresh per-park scales.

5. ~~**TODO: Accuracy eval with synthetic actuals**~~ ✅ **DONE** — `evaluate_forecast_accuracy.py` now includes synthetic actuals as ground truth (FULL OUTER JOIN with raw ACTUAL). Dramatically increases evaluation coverage.

6. 📋 **TODO: Park hours donor filtering** — Add outlier filtering to `impute_park_hours.py`: reject abnormal openings (after 9:30 AM) and abnormal closings (before 5 PM where unusual for that park). Consider minimum frequency threshold.

7. 📋 **TODO: Park hours accuracy reporting** — Add imputation accuracy metrics to the daily reporting pipeline.

8. 📋 **TODO: ETL → Parquet refactor** — Refactor ETL to output parquet directly, eliminating the CSV intermediate step and Step 1b.

9. ~~**TODO: Training architecture rethink**~~ ✅ **IMPLEMENTED** — ACTUALS-FIRST methodology: `--actuals-only` in `hybrid_pipeline_v2.py` trains on actuals only (5 features, no posted_time). `forecast_vectorized.py` prefers `model_julia_actuals.json` when present. A/B comparison recommended before full rollout.

10. 📋 **TODO: Daily accuracy report integration** — `daily_accuracy_report.py` exists and generates Telegram-friendly reports. Consider adding it to the daily pipeline or cron.

11. 📋 **TODO: Accuracy-driven model improvements** — The accuracy evaluation step accumulates detailed per-entity, per-horizon accuracy data. This could drive:
    - Automatic flagging of entities whose models have degraded
    - Horizon-specific bias corrections
    - Model retraining triggers based on accuracy thresholds

---

## Key Files Quick Reference

| Purpose | Path |
|---------|------|
| Pipeline output base | `/home/wilma/hazeydata/pipeline/` (aliased as `{output_base}`) |
| Fact tables (fast) | `{output_base}/fact_tables/parquet/*.parquet` |
| Matched pairs | `{output_base}/matched_pairs/all_pairs_v2.parquet` |
| Actuals training data | `{output_base}/matched_pairs/actuals_training_v2.parquet` (ACTUALS-FIRST) |
| Models | `{output_base}/models/{entity}/model_julia_v2.json` or `model_julia_actuals.json` |
| Forecasts | `{output_base}/curves/forecast_parquet/all_forecasts.parquet` |
| WTI | `{output_base}/wti/wti.parquet` |
| Operating calendar | `{output_base}/operating_calendar/operating_calendar.parquet` |
| Synthetic actuals | `{output_base}/synthetic_actuals/{entity}.parquet` |
| Dimension tables | `{output_base}/dimension_tables/` |
| Aggregates | `{output_base}/aggregates/` |
| State files | `{output_base}/state/` |
| Accuracy data | `{output_base}/accuracy/` |
| Logs | `{output_base}/logs/` |
| Config | `config/config.json` |

---

## Three-Day Feedback Loop

**Critical concept:** When making significant changes to models or training data, the first
accuracy signal doesn't appear for **three days**.

```
Day 0: Make the change (e.g., retrain conversion model, adjust features)
Day 1: Pipeline runs → new training data → retrains entity models → generates new forecasts
Day 2: The forecasted day happens — actuals are observed in parks
Day 3: Pipeline ingests Day 2 actuals → evaluates forecast vs observed → FIRST ACCURACY SIGNAL
```

**Example:** Conversion model retrained on Mar 4 (Day 0). New synthetic actuals + entity
model retraining on Mar 5 (Day 1). Forecasts are for Mar 6+ (Day 2). Mar 6 actuals are
observed and ingested on Mar 7 (Day 3). First evaluable accuracy: **Mar 7**.

**Why this matters:**
- Don't panic if accuracy doesn't improve the day after a fix
- Don't stack multiple changes within a 3-day window — you won't know which one helped
- When evaluating a change, look at accuracy starting Day 3, not before
- Document change dates so you can correlate accuracy shifts to specific fixes

**Recommendation:** Tag significant changes in the pipeline logs or a changelog so future
accuracy analysis can overlay "change markers" on accuracy trend charts.

---

## See Also

- [MODELING_AND_WTI_METHODOLOGY.md](MODELING_AND_WTI_METHODOLOGY.md) — Statistical methodology and WTI formula
- [ARCHITECTURE.md](ARCHITECTURE.md) — DuckDB patterns, file layout
- [ENTITY_SYSTEM.md](ENTITY_SYSTEM.md) — Entity codes and park mappings
- [CLOSURES_MODULE_SPEC.md](CLOSURES_MODULE_SPEC.md) — Closure handling details
- [PIPELINE_STATE.md](PIPELINE_STATE.md) — Skip-if-unchanged deep dive
- [SCHEMAS.md](SCHEMAS.md) — Table schemas
- [PREDICTIONS-API.md](PREDICTIONS-API.md) — REST API documentation
