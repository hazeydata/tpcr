# Theme Park Crowd Report — Project Overview for Google Doc

**How to use this file**

- This file is the **single source of truth** for "what to put in the shared Google Doc." When we update project docs (README, ENTITY_INDEX, MODELING_AND_WTI, TRAINING_TIMING_NOTES, etc.), we update **this file** too so it stays in sync.
- **You** paste from here into the Google Doc when it's convenient. I can't edit the Google Doc directly (no API access in this environment).
- **Reminder:** After major doc updates, copy this file's content (from "What We're Building" through the end) into your Google Doc so stakeholders and future readers see the latest.
- If you'd like a nudge: when we change this file, I can add "Remember to update the Google Doc from docs/GOOGLE_DOC_CONTENT.md" in my reply.

*Copy the sections below into your [Google Doc](https://docs.google.com/document/d/1nS_U7TUyxcs20dDtLuCX6EuW-bjSrbaZ3TQFFBS26s0/edit?usp=sharing) so future readers know exactly what we're creating.*

---

## What We’re Building

**Theme Park Crowd Report** is a Python pipeline that:

1. **Ingests** wait-time data (standby POSTED/ACTUAL and priority) from AWS S3 and from queue-times.com.
2. **Stores** clean fact tables by park and date, plus dimension tables (entities, park hours, dategroupid, season, etc.).
3. **Trains** per-entity models (XGBoost or mean-based) to predict ACTUAL wait times from POSTED and other features.
4. **Produces** historical backfill curves, future forecasts (predicted ACTUAL and predicted POSTED), and a **Wait Time Index (WTI)** per park-day.

Everything under one `output_base` (e.g. Dropbox) so data, state, and logs stay in one place.

---

## High-Level Outcomes

| Outcome | Description |
|--------|-------------|
| **Historical ACTUAL curves** | For each attraction and park-day in the past: full ACTUAL wait-time curve (from model + POSTED where available, or features-only where not). |
| **Future ACTUAL curves** | Predicted ACTUAL for future park-days (tomorrow out to ~2 years) using features-only models. |
| **Predicted POSTED** | Per (entity, park_date, 5‑minute slot): predicted POSTED from historical aggregates (dategroupid × hour). Used for live comparison (“we predicted X; we see Y”) and trust, **not** in WTI. |
| **Wait Time Index (WTI)** | One number per park-day: average ACTUAL across all attractions (excluding closed/null). Replaces 1–10 crowd calendar; comparable over time. |
| **Live inference (future)** | Low-latency POSTED → ACTUAL when we receive a live POSTED value. |

---

## Data Pipeline

### Sources

- **AWS S3** (`touringplans_stats`): Standby wait-time files and fastpass/priority files, by property (wdw, dlr, tdr, uor, ush).
- **queue-times.com API**: Live POSTED (and sometimes ACTUAL). Scraper writes to **staging**; morning ETL merges yesterday’s staging into fact tables.

### Fact Tables

- **Path:** `output_base/fact_tables/clean/YYYY-MM/{park}_{YYYY-MM-DD}.csv`
- **Columns:** `entity_code`, `observed_at` (ISO with TZ), `wait_time_type` (`POSTED` \| `ACTUAL` \| `PRIORITY`), `wait_time_minutes`
- **6am rule:** “Park date” = operational date from `observed_at` in park timezone (day flips at 6am).

### Dimension Tables (under `output_base/dimension_tables/`)

- **dimentity.csv** — Attractions: entity_code, short_name, park_code, fastpass_booth (TRUE = priority queue), etc.
- **dimparkhours.csv** — Official park hours by (park, date).
- **dimparkhours_with_donor.csv** — Versioned park hours: official + predicted (donor-day imputation) with valid_from/valid_until; used for feature engineering and forecast.
- **dimdategroupid.csv** — Date spine, holidays, `date_group_id` (e.g. “Easter week”, “typical Tuesday”).
- **dimseason.csv** — Season and season_year from dimdategroupid.
- **dimeventdays.csv**, **dimevents.csv**, **dimmetatable.csv** — Events and park-day metadata.

### Entity Index (`output_base/state/entity_index.sqlite`)

- **Purpose:** Know which entities need (re-)modeling and load only that park’s CSVs.
- **Columns:** entity_code, latest_park_date, latest_observed_at, row_count, **actual_count**, **posted_count**, **priority_count**, last_modeled_at, first_seen_at, updated_at.
- **Wait-type counts:** We store counts of ACTUAL, POSTED, PRIORITY per entity. Batch training filters to entities with ≥500 ACTUAL or PRIORITY so we never load data for “POSTED-only” entities (e.g. TDS36).
- **Updated when:** ETL writes fact CSVs or merges queue-times staging. Rebuild: `python src/build_entity_index.py --rebuild`.

---

## Modeling

### Queue Types

- **STANDBY** (`fastpass_booth=FALSE` in dimentity): We predict **ACTUAL** from POSTED + features. These queues have POSTED and/or ACTUAL in the data.
- **PRIORITY** (`fastpass_booth=TRUE`): We predict **PRIORITY** (return time / 8888) from features. No “ACTUAL” for priority queues.

Each entity is one type. We decide from dimentity and then train for ACTUAL or PRIORITY accordingly.

### Model Types per Entity

- **XGBoost** — If the entity has **≥500** observations of the target type (ACTUAL for STANDBY, PRIORITY for PRIORITY). We train:
  - **With-POSTED:** target ~ POSTED + features (used when same-day POSTED is available, e.g. backfill, live).
  - **Without-POSTED:** target ~ features only (used for future dates when we have no same-day POSTED).
- **Mean model** — If the entity has **<500** target observations (or zero): predict the mean of those observations (or 0 if none). Stored as metadata only; no XGBoost files.

### Features (used in both XGBoost and mean logic)

- **Time:** pred_mins_since_6am, pred_mins_since_park_open, pred_park_open_hour, pred_park_close_hour, pred_park_hours_open.
- **Calendar:** pred_dategroupid, pred_season, pred_season_year (from dimdategroupid / dimseason).
- **Identity:** park_code (from entity_code prefix), entity_code.
- **Target-related:** observed_wait_time (from wait_time_minutes for the chosen wait_time_type).
- **Park hours:** From **versioned** table (dimparkhours_with_donor). Park hours are **per (park_date, park_code)**, not per entity. We build one **(park_date, park_code) → hours** lookup per training run and merge it in, instead of calling “get hours for this date” in a loop—this keeps add_features fast even for very large entities (e.g. 450k+ rows).

### Encoding

- Categoricals (pred_dategroupid, pred_season, pred_season_year, park_code, entity_code) are label-encoded.
- Mappings in `output_base/state/encoding_mappings.json`; `handle_unknown="encode"` so new categories get new IDs at inference.

### Where Models Live

- **XGBoost:** `output_base/models/{entity_code}/` — e.g. `model_with_posted.json`, `model_without_posted.json`, `metadata_without_posted.json` (with feature list).
- **Mean:** same folder; metadata only (e.g. `metadata_mean.json` with mean and count).

---

## Forecast, Backfill, and WTI

- **Predicted POSTED:** From **aggregates**: the median historical POSTED wait by (entity, date_group_id, 15-minute clock slot). Rebuilt every run by pipeline step S4 (`pipeline/steps/s04_aggregates.py`, logic in `pipeline/core/model_aggregates.py`) → `output_base/aggregates/model_aggregates.parquet`. S8 forecast looks up `wait_median` by (date_group_id, 15-min slot); a missing value falls back to 5 minutes, and each fallback is logged with its entity and reason. `posted_aggregates.parquet` (hourly, v2) is no longer read by anything.
- **Predicted ACTUAL:** From the **without-POSTED** model (and predicted POSTED as input when we want “with-POSTED” style in future). Rounded to nearest integer for output.
- **Forecast script:** `scripts/generate_forecast.py` — writes future curves (actual_predicted, posted_predicted) for a given entity/date range.
- **Backfill script:** `scripts/generate_backfill.py` — writes historical ACTUAL curves using observed POSTED + model where we have POSTED.
- **WTI script:** `scripts/calculate_wti.py` — computes Wait Time Index per park-day from ACTUAL curves (observed + predicted).

---

## Key Scripts (reference for “what runs where”)

| Script | Purpose |
|--------|---------|
| `src/get_tp_wait_time_data_from_s3.py` | Main ETL: S3 → fact_tables/clean, dedup, entity index update. |
| `src/get_entity_table_from_s3.py` | dimentity from S3. |
| `src/get_park_hours_from_s3.py` | dimparkhours from S3. |
| `src/build_entity_index.py` | Build/rebuild entity index from all fact CSVs. |
| `scripts/train_entity_model.py` | Train one entity (XGBoost or mean). |
| `scripts/train_batch_entities.py` | Train many entities (from index or from a list); uses min_observations (e.g. 500) so POSTED-only entities are skipped. |
| `scripts/build_model_aggregates.py` | Manual run of the S4 build: (entity, date_group_id, 15-min slot) → model_aggregates.parquet. `build_posted_aggregates*.py` write the unused posted_aggregates.parquet. |
| `scripts/generate_forecast.py` | Produce future actual_predicted and posted_predicted curves. |
| `scripts/generate_backfill.py` | Produce historical ACTUAL curves. |
| `scripts/calculate_wti.py` | Compute WTI per park-day. |
| `scripts/check_batch_status.py` | Report how many entities completed/failed and which entity is currently training. |
| `scripts/cleanup_logs.py` | Delete old log files (e.g. keep last N, drop older than D days). |
| `scripts/register_scheduled_tasks.ps1` | Register Windows scheduled tasks (ETL, dimensions, queue-times, etc.). |
| `scripts/register_log_cleanup_task.ps1` | Register weekly log cleanup (run as Administrator). |
| `scripts/start_queue_times_stream_deck.bat` | Stream Deck launcher: starts the queue-times loop in a visible window; stop with Ctrl+C. See scripts/README.md. |

---

## Output Layout and Config

- **Single output root:** `output_base` from `config/config.json` (or a default path). All pipeline output goes under it: fact_tables, dimension_tables, state, logs, models, aggregates, reports.
- **Logs:** `output_base/logs/` — one place for ETL, dimension fetches, queue-times, training, etc. Log cleanup runs weekly (e.g. keep last 10 per type, drop older than 30 days).

---

## Training Timing (what to expect)

- **Typical:** ~7–15 minutes per entity for full XGBoost training (add features + encoding + train).
- **Heavy entities** (e.g. EP09, AK01): can be ~13–19 minutes.
- **If one entity is > ~25–30 minutes:** something is likely wrong. Check that entity’s log under `output_base/logs/train_entity_model_*.log`; the last line shows where it’s stuck (e.g. “Adding features…”, “Encoding…”, “Training…”).
- **Park-hours fix (Jan 2026):** We used to call “get park hours for this date” in a Python loop over every unique (park_date, park_code) in the entity’s data. For big entities (e.g. AK86 with 450k rows and thousands of dates) that took over an hour. We switched to **one batch lookup** per entity: build a (park_date, park_code) → hours table from the versioned park-hours table, then one merge. Park hours are per park, not per entity, so that one lookup covers all rows. Add-features for large entities is now minutes, not hours. **Proof:** AK86 (Flight of Passage) completed in ~12m 20s post-fix (2026-01-27); previously hung >1h in add_park_hours.

---

## Scheduled Tasks (Windows)

- **ETL / dimensions / queue-times:** Registered via `scripts/register_scheduled_tasks.ps1` (times and triggers as configured there).
- **Log cleanup:** Registered via `scripts/register_log_cleanup_task.ps1` (run as Administrator), typically weekly (e.g. Sunday 7:00 AM).

---

## Tech Stack

- **Python 3.11** (recommended for XGBoost and sklearn in this project).
- **Key packages:** pandas, boto3 (S3), XGBoost, scikit-learn, zoneinfo.

---

## Repo and Docs (for future readers)

- **Repo:** `theme-park-crowd-report` (e.g. under GitHub/hazeydata).
- **In-repo docs:** `README.md`, `PROJECT_STRUCTURE.md`, `TODO.md`, `docs/SCHEMA.md`, `docs/ENTITY_INDEX.md`, `docs/MODELING_AND_WTI_METHODOLOGY.md`, `docs/TRAINING_TIMING_NOTES.md`, `config/README.md`.

This Google Doc is the **product-level overview** so anyone can understand what the system does and how the pieces fit together, without opening the codebase first.

*This content is maintained in the repo as `docs/GOOGLE_DOC_CONTENT.md`. When we update project docs, we update that file; paste from it into this Google Doc to keep the Doc in sync.*
