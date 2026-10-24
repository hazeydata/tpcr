# Scripts Directory

Standalone utility scripts that can be run independently of the main pipeline.

## Current Scripts

### Dropbox Scheduler

**`dropbox_scheduler.sh`** - Controls Dropbox start/stop  
**`install_dropbox_scheduler.sh`** - Install daily job (starts at 4:00 AM, stops at 4:30 AM)  
**`uninstall_dropbox_scheduler.sh`** - Remove the scheduled job

**Installation:**
```bash
./scripts/install_dropbox_scheduler.sh
```

**Schedule:**
- Starts Dropbox: Daily at 4:00 AM
- Stops Dropbox: Daily at 4:30 AM

**Check status:**
```bash
launchctl list | grep dropbox
```

**Uninstall:**
```bash
./scripts/uninstall_dropbox_scheduler.sh
```

---

### Stream Dashboard — `start-stream.sh`

**`start-stream.sh`** — Starts the local stream dashboard server on Fred's Mac so you can preview the dashboard (and so Streamlabs can use it at localhost:8889).

**Run from repo root:**
```bash
./scripts/start-stream.sh
```

- Serves the dashboard on **http://localhost:8889**
- Preview URL: **http://localhost:8889/stream-dashboard.html**
- Data comes from **http://wilma-server:8051/api** (no need to run the API locally)
- Works when run in the foreground or in the background; stop with Ctrl+C

See **dashboard/README_STREAM.md** for full stream dashboard docs.

---

### `register_scheduled_tasks.ps1`

Registers the **5 AM**, **6 AM**, and **7 AM Eastern** Windows scheduled tasks.

**IMPORTANT: Must be run as Administrator** to register scheduled tasks.

**Run once** (or after changing Python path / project root):

1. **Right-click PowerShell** and select **"Run as Administrator"**
2. Navigate to the project directory
3. Run:
```powershell
powershell -ExecutionPolicy Bypass -File scripts/register_scheduled_tasks.ps1
```

If you get "Access is denied" errors, you need Administrator privileges. The script will continue trying to register remaining tasks even if some fail, and will warn you if it detects you're not running as Administrator.

Creates:
- **ThemeParkWaitTimeETL_5am** — Daily at 5:00 AM (wait-time ETL, primary)
- **ThemeParkWaitTimeReport_530am** — Daily at 5:30 AM (wait-time DB report, after 5am ETL)
- **ThemeParkDimensionFetch_6am** — Daily at 6:00 AM (entity, park hours, events, metatable from S3; build dimdategroupid, dimseason)
- **ThemeParkPostedAccuracyReport_Sunday** — Weekly Sunday at 6:30 AM (posted prediction accuracy report)
- **ThemeParkLogCleanup_Sunday** — Weekly Sunday at 7:00 AM (log cleanup: deletes logs older than 30 days, keeps 10 most recent per type)
- **ThemeParkWaitTimeETL_7am** — Daily at 7:00 AM (wait-time ETL, backup)

See [README.md](../README.md#scheduling) for details.

### `run_dimension_fetches.ps1`

Runs the dimension-table fetch scripts and builds in sequence: `get_entity_table_from_s3.py`, `get_park_hours_from_s3.py`, `get_events_from_s3.py`, `get_metatable_from_s3.py`, `build_dimdategroupid.py`, `build_dimseason.py` (metatable from S3; dimdategroupid and dimseason built locally). Writes to the same **output_base** as the ETL (from `config/config.json` or default): `dimension_tables/` and `logs/` under that path. Invoked by **ThemeParkDimensionFetch_6am**; can also be run manually:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_dimension_fetches.ps1
```

Exits with an error if any script fails.

### Queue-Times.com fetcher and `run_queue_times_loop.ps1`

**`src/get_wait_times_from_queue_times.py`** fetches live wait times from the queue-times.com API, maps ride IDs to TouringPlans `entity_code` via `config/queue_times_entity_mapping.csv`, deduplicates with SQLite, and **writes to `staging/queue_times/YYYY-MM/{park}_{date}.csv`** (not `fact_tables`). The **morning ETL** (S3 run) merges **yesterday's** staging into `fact_tables/clean` at the start of each run. Staging is also used for live content (e.g. Twitch/YouTube).

- **One-shot** (single fetch and write to staging):
  ```powershell
  python src/get_wait_times_from_queue_times.py
  python src/get_wait_times_from_queue_times.py --output-base "D:\Path\output" --park-ids 6,5,7,8
  ```
- **Continuous (constantly runs)**: `--interval SECS` runs a loop: fetch → write to staging → sleep → repeat. Stop with Ctrl+C.
  ```powershell
  python src/get_wait_times_from_queue_times.py --interval 600
  ```

**Live DuckDB write (`tpcr_live.duckdb` `live_waits`):** each batch is also inserted into the live DB (only if the file exists), skipping rows already present by `(entity_code, observed_at, wait_time_type)`. A locked DB (e.g. s11 deploy ~6:15 AM ET) is retried with backoff for ~3 minutes. If still locked, the batch CSV is kept in `output_base/state/live_waits_pending/` (never deleted on failure) and flushed by the next write; after an exhausted batch, new batches are parked without retrying for 60s. In continuous mode, once per America/New_York day at the first cycle at/after 04:30 ET (before the morning ETL removes yesterday's staging), `src/replay_live_waits.py` backfills missing rows from staging. Run it manually (idempotent):
  ```bash
  python src/replay_live_waits.py --output-base /path/to/output --days 2
  python src/replay_live_waits.py --date 2026-10-22 --date 2026-10-23 --budget-s 180
  ```

**`run_queue_times_loop.ps1`** is a thin wrapper that runs the fetcher with `--interval 300` (5 minutes) by default. It uses the same **output_base** as the rest of the pipeline (from `config/config.json` or default) unless you pass `-OutputBase`. You can run it in a console or register a Windows task “At log on” / “At startup” to keep it running across reboots. The fetcher uses **dimparkhours** (`opening_time` / `closing_time` when present) to only scrape parks in-window (open−90 to close+90 in park TZ); use `--no-hours-filter` to disable. The fetcher always bypasses HTTP/HTTPS proxy; run the loop from **PowerShell or Command Prompt** (not Cursor's terminal) if your IDE forces a local proxy.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_queue_times_loop.ps1
powershell -ExecutionPolicy Bypass -File scripts/run_queue_times_loop.ps1 -IntervalSeconds 300 -OutputBase "D:\Path\output"
```

**Troubleshooting:** If the loop was stopped by closing the window, delete `state/processing_queue_times.lock` under your output base before starting again. Check `output_base/logs/get_tp_wait_time_data_*.log` for errors.

**Stream Deck button** — To start the queue-times loop from a Stream Deck button (window stays open, use Ctrl+C to stop):

1. Add an **Open** action.
2. **App / File:** full path to `scripts\start_queue_times_stream_deck.bat`  
   Example: `D:\GitHub\hazeydata\theme-park-crowd-report\scripts\start_queue_times_stream_deck.bat`
3. **Arguments:** leave blank.
4. **Start in:** leave blank.

**Option B — if no window appears:** Use **Open** with **App / File:** `cmd.exe` and **Arguments:** `/k "D:\GitHub\hazeydata\theme-park-crowd-report\scripts\start_queue_times_stream_deck.bat"` (use your actual path). That forces a visible cmd window that runs the same script.

The `.bat` uses `start "Queue-Times Loop" powershell ...` so a titled PowerShell window should appear. The script uses Python 3.11 when available. If Python isn’t found, edit `scripts\stream_deck_queue_times.ps1` and set `$PythonExe` to the full path to your `python.exe`.

### `validate_wait_times.py`

Validates wait time fact table CSVs under `fact_tables/clean/`:

- **Schema**: Required columns (`entity_code`, `observed_at`, `wait_time_type`, `wait_time_minutes`), valid `wait_time_type` values.
- **Ranges**:
  - **POSTED / ACTUAL**: 0–1000 (invalid outside); **outlier** if ≥ 300.
  - **PRIORITY**: -100–2000 or 8888 (sold out); **outlier** if &lt; -100 or &gt; 2000 and ≠ 8888.

Writes a JSON report to `validation/validate_wait_times_YYYYMMDD_HHMMSS.json` under the output base. Exits **1** if any invalid rows; **0** otherwise. Outliers are flagged but do not fail the run.

**Run**:

```powershell
python scripts/validate_wait_times.py
python scripts/validate_wait_times.py --lookback-days 14
python scripts/validate_wait_times.py --all
python scripts/validate_wait_times.py --output-base "D:\Path" --report validation/custom.json
```

**Options**: `--output-base`, `--lookback-days` (default 7), `--all`, `--report`.

### `report_wait_time_db.py`

Produces an **easily consumable Markdown report** of what's in the wait time fact table. Scans `fact_tables/clean/YYYY-MM/{park}_{date}.csv` (same layout as ETL output) and writes a report with:

1. **Summary** — Date range, parks, park-day (file) count, total rows (or “—” with `--quick`)
2. **By park** — Per-park: file count, row count (or omitted with `--quick`), date range
3. **Recent coverage** — Grid: last N days × parks. Cells show ✓ (file exists) / — (no file) with `--quick`, or row counts otherwise.

Report path: `reports/wait_time_db_report.md` under the output base. **Overwritten each run** so you can always open the same file for daily or ad-hoc checks.

**Scheduled**: Runs automatically at **5:30 AM Eastern** (after 5am ETL) via **ThemeParkWaitTimeReport_530am** scheduled task. Uses `--quick` for fast execution.

**When to use `--quick`**: Skip row counts; grid shows ✓/— only. Faster on slow or remote paths (e.g. Dropbox). Use for quick daily coverage checks.

**Run**:

```powershell
python scripts/report_wait_time_db.py
python scripts/report_wait_time_db.py --quick --lookback-days 7
python scripts/report_wait_time_db.py --output-base "D:\Path" --report reports/db_report.md
```

**Options**: `--output-base`, `--report`, `--lookback-days` (default 14), `--quick`.

### `report_queue_times_unmapped.py`

Lists **queue-times.com attractions that have no row** in `config/queue_times_entity_mapping.csv`. Fetches parks and `queue_times` from the API, collects all rides in mapped parks, left-joins to the mapping, and writes unattributed `(park_code, queue_times_id, queue_times_name, last_seen)` to `reports/queue_times_unmapped.csv` for review. `last_seen` is the report run date (YYYY-MM-DD).

**Run** (requires network for queue-times.com API):

```powershell
python scripts/report_queue_times_unmapped.py
python scripts/report_queue_times_unmapped.py --output-base "D:\Path" --report reports/unmapped.csv
```

**Options**: `--output-base`, `--report` (default: `output_base/reports/queue_times_unmapped.csv`).

### `build_entity_index.py`

**Location**: `src/build_entity_index.py` (not in scripts/, but a utility script)

Builds or rebuilds the **entity metadata index** (`state/entity_index.sqlite`) by scanning all fact table CSVs in `fact_tables/clean/`. The index tracks per-entity metadata (latest observation date, latest timestamp, row counts) to enable efficient modeling workflows. Useful for initial index creation or rebuilding after corruption.

**Run**:

```powershell
# Build index from all CSVs
python src/build_entity_index.py

# Rebuild (delete existing and start fresh)
python src/build_entity_index.py --rebuild

# Custom output base
python src/build_entity_index.py --output-base "D:\Path"
```

**Note**: The index updates automatically during ETL runs, so a full rebuild is only needed for initial creation or recovery. See [docs/ENTITY_INDEX.md](../docs/ENTITY_INDEX.md).

### `check_prerequisites.py`

Checks if all prerequisites are met for running the modeling pipeline:
- Required Python packages (pandas, xgboost, sklearn, etc.)
- Trained models (with-POSTED and without-POSTED)
- Posted aggregates
- Entity index
- Versioned park hours (optional)

Optionally installs missing packages.

**Run**:

```powershell
# Check prerequisites
python scripts/check_prerequisites.py

# Check and install missing packages
python scripts/check_prerequisites.py --install-missing

# Custom output base
python scripts/check_prerequisites.py --output-base "D:\\Path"
```

**Options**: `--output-base`, `--install-missing` (install missing packages), `--min-models` (default: 1).

### `train_entity_model.py`

Trains XGBoost models for a single entity. Automatically detects queue type (STANDBY vs PRIORITY via `fastpass_booth` in dimentity) and uses appropriate wait time observations:
- **STANDBY queues** (`fastpass_booth = FALSE`): Uses ACTUAL observations, trains both with-POSTED and without-POSTED models
- **PRIORITY queues** (`fastpass_booth = TRUE`): Uses PRIORITY observations, trains only without-POSTED model (no POSTED equivalent)

**Observation threshold:** Entities with ≥ 500 observations get XGBoost models; entities with < 500 observations get mean-based models (simple average).

**Usage:**
```powershell
python scripts/train_entity_model.py --entity AK01
python scripts/train_entity_model.py --entity MK101 --output-base "D:\Path"
python scripts/train_entity_model.py --entity AK01 --sample 10000  # Faster testing
```

**Options**: `--entity` (required), `--output-base`, `--train-ratio` (default: 0.7), `--val-ratio` (default: 0.15), `--skip-encoding`, `--sample` (for testing), `--skip-park-hours`.

### `train_batch_entities.py`

Batch trains models for multiple entities. Can query entity index for entities needing training, or train a specified list.

**Usage:**
```powershell
# Train all entities that need modeling (from entity index)
python scripts/train_batch_entities.py

# Train specific entities
python scripts/train_batch_entities.py --entities MK101 MK102 AK01

# Train entities from a file (one entity code per line)
python scripts/train_batch_entities.py --entity-list entities.txt

# Train only entities with data at least 24 hours old
python scripts/train_batch_entities.py --min-age-hours 24

# Limit number of entities to train
python scripts/train_batch_entities.py --max-entities 10
```

**Options**: `--entities`, `--entity-list`, `--output-base`, `--min-age-hours` (default: 0), `--max-entities`, `--workers` (default: 1; use 4–8 to finish within 24h), `--train-ratio`, `--val-ratio`, `--skip-encoding`, `--sample`, `--skip-park-hours`, `--min-observations` (default: 500), `--python`.

**Note:** All entity-related logging includes short names from dimentity (e.g., "AK03 - Greeting Trails") for improved readability.

### `test_modeling_pipeline.py`

Tests the complete modeling pipeline (forecast, backfill, WTI) with a small subset of entities and dates. Useful for validating the pipeline before running on full datasets.

**Features**:
- Automatically selects test entities from entity index
- Generates mix of past and future test dates
- Runs all three pipeline stages sequentially
- Validates output file formats and required columns
- Provides clear pass/fail summary

**Run**:

```powershell
# Test with default settings (2 entities, 7 dates)
python scripts/test_modeling_pipeline.py

# Test specific entity with more dates
python scripts/test_modeling_pipeline.py --entity MK101 --num-dates 14

# Test with more entities
python scripts/test_modeling_pipeline.py --num-entities 5 --num-dates 10
```

**Options**: `--output-base`, `--entity` (test specific entity), `--num-entities` (default: 2), `--num-dates` (default: 7).

### `inspect_entity_index.py`

Inspects the entity metadata index to show entities, their latest observations, and which ones need re-modeling.

**Run**:

```powershell
# Show all entities (first 20)
python scripts/inspect_entity_index.py

# Show entities needing modeling
python scripts/inspect_entity_index.py --needing-modeling

# Custom limit
python scripts/inspect_entity_index.py --limit 50
```

**Options**: `--output-base`, `--needing-modeling` (show only entities needing re-modeling), `--limit` (default: 20).

## Additional Scripts (summary)

- **Modeling pipeline**: `build_posted_aggregates.py`, `train_batch_entities.py`, `train_entity_model.py`, `generate_forecast.py`, `generate_backfill.py`, `calculate_wti.py`, `test_modeling_pipeline.py` — see [README.md](../README.md) and [docs/MODELING_AND_WTI_METHODOLOGY.md](../docs/MODELING_AND_WTI_METHODOLOGY.md).
- **Pipeline status**: `update_pipeline_status.py` — used by `run_daily_pipeline.sh` and `train_batch_entities.py` to write `state/pipeline_status.json` for the dashboard.

## Running Scripts

Scripts in this directory can be run directly:

```powershell
python scripts/script_name.py
```

Or with the virtual environment activated:

```powershell
.\venv\Scripts\Activate.ps1
python scripts/script_name.py
```

### `cleanup_logs.py`

Cleans up old log files from the pipeline. Removes logs older than a specified number of days, optionally keeping the N most recent logs per log type.

**Usage:**
```powershell
# Dry run (show what would be deleted)
python scripts/cleanup_logs.py --dry-run

# Delete logs older than 30 days, keep 10 most recent per type
python scripts/cleanup_logs.py --days 30 --keep-recent 10

# Delete all logs older than 7 days
python scripts/cleanup_logs.py --days 7

# Delete specific log pattern (e.g., all train_entity_model logs older than 14 days)
python scripts/cleanup_logs.py --days 14 --pattern "train_entity_model_*.log"
```

**Options**: `--output-base`, `--days` (default: 30), `--keep-recent` (keep N most recent per type), `--pattern` (glob pattern filter), `--dry-run`, `--log-dir` (override log directory).

**Features:**
- Groups logs by type (e.g., `train_batch_entities`, `get_park_hours`) automatically
- Keeps most recent logs per type even if older than cutoff (if `--keep-recent` specified)
- Dry-run mode to preview deletions
- Detailed logging of what's deleted/kept

## Note

Scripts here should be standalone and not depend on the main pipeline structure. They can import from `src/` if needed, but should be runnable independently.

---

## Linux Scripts

Bash equivalents of the PowerShell scripts for running on Linux.

### `common.sh`

Shared functions sourced by other bash scripts:
- `get_project_root` — Find project root directory
- `get_output_base` — Read output_base from config/config.json
- `get_python` — Find python3 or python executable
- `log_info` / `log_error` — Timestamped logging

### `run_daily_pipeline.sh` (Linux)

Master script that runs the full pipeline in order: **S3 sync** (wait_times + fastpass_times to `output_base/raw`) → ETL (sync-only: reads from `raw/` only) → Dimension fetches → Posted aggregates → Wait time DB report → Batch training → Forecast → WTI. Use for a single daily run (e.g. from cron at 6:00 AM).

```bash
./scripts/run_daily_pipeline.sh
./scripts/run_daily_pipeline.sh --no-stop-on-error   # continue on step failure
./scripts/run_daily_pipeline.sh --skip-etl --skip-training
./scripts/run_daily_pipeline.sh --skip-sync   # skip S3 sync (use existing raw/ or S3 streaming)
```

Options: `--output-base PATH`, `--no-stop-on-error`, `--skip-sync`, `--skip-etl`, `--skip-dimensions`, `--skip-aggregates`, `--skip-report`, `--skip-training`, `--skip-forecast`, `--skip-wti`. See [LINUX_CRON_SETUP.md](../LINUX_CRON_SETUP.md) and `install_cron.sh --daily-master`.

### `sync_s3_data.sh` (Linux)

Syncs TouringPlans S3 `export/wait_times/` and `export/fastpass_times/` to `output_base/raw/export/...`. Run before ETL so the pipeline reads from local files (reliable, resumable). Used automatically by `run_daily_pipeline.sh`; can also run manually or on a separate schedule (e.g. 5:30 AM).

```bash
./scripts/sync_s3_data.sh
./scripts/sync_s3_data.sh --output-base /path/to/output
```

### `run_etl.sh`

Main ETL script wrapper. ETL is **sync-only**: it requires `--local-source` (e.g. `output_base/raw`) and reads only from local files. Run `sync_s3_data.sh` first.

```bash
# After syncing (required)
./scripts/sync_s3_data.sh --output-base /path/to/output
./scripts/run_etl.sh --output-base /path/to/output --local-source /path/to/output/raw

# Full rebuild
./scripts/run_etl.sh --output-base /path/to/output --local-source /path/to/output/raw --full-rebuild
```

### `run_dimension_fetches.sh`

Fetches all dimension tables from S3 and builds local dimensions.

```bash
./scripts/run_dimension_fetches.sh
./scripts/run_dimension_fetches.sh --output-base /path/to/output
```

### `run_queue_times_loop.sh`

Continuous Queue-Times.com fetcher. Runs until Ctrl+C.

```bash
# Default 5-minute interval
./scripts/run_queue_times_loop.sh

# Custom interval
./scripts/run_queue_times_loop.sh --interval 600
```

### `install_cron.sh`

Install/remove cron jobs equivalent to Windows scheduled tasks.

```bash
# Preview
./scripts/install_cron.sh --show

# Install five separate jobs (5am ETL, 5:30 report, 6am dimensions, 7am ETL, 8am training)
./scripts/install_cron.sh

# Install single daily pipeline at 6:00 AM (run_daily_pipeline.sh)
./scripts/install_cron.sh --daily-master

# Remove
./scripts/install_cron.sh --remove
```

### `check_docs_for_instructions.sh` and `install_docs_check_cron.sh`

Hourly job that pulls latest from git and checks WILMA-BAMBAM.md Active Items for changes. Logs when new instructions are detected. Cron survives reboot.

```bash
# Install hourly docs-check job
./scripts/install_docs_check_cron.sh

# Preview / remove
./scripts/install_docs_check_cron.sh --show
./scripts/install_docs_check_cron.sh --remove
```

### `install_queue_times_service.sh`

Install or remove the queue-times loop as a systemd service (starts on boot).

```bash
# Install and enable (run once; needs sudo)
sudo bash scripts/install_queue_times_service.sh

# Remove service and disable on boot
sudo bash scripts/install_queue_times_service.sh --remove
```

The service uses user **fred** and project path from the script; edit `scripts/queue-times-loop.service` if needed.

### `queue-times-loop.service`

Systemd unit file; installed by `install_queue_times_service.sh`. To install manually:

```bash
sudo cp scripts/queue-times-loop.service /etc/systemd/system/
# Edit paths/user if needed, then:
sudo systemctl daemon-reload
sudo systemctl enable --now queue-times-loop
```

See [LINUX_CRON_SETUP.md](../LINUX_CRON_SETUP.md) and [docs/PIPELINE_STATE.md](../docs/PIPELINE_STATE.md) for complete Linux setup.
