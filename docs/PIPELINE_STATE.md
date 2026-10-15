# Pipeline State - Where We Are

Single reference for the current Theme Park pipeline setup (Linux, user **fred**). Updated when config, cron, or services change.

**When you change** config paths, cron, queue-times, or dashboard setup, **update this file.** (A project rule reminds the AI to keep it in sync.)

---

## 1. Current setup (summary)

| Item | Value |
|------|--------|
| **User** | fred |
| **Repo** | `/home/fred/Desktop/theme-park-crowd-report` |
| **Output base (data & logs)** | `/home/fred/TouringPlans.com Dropbox/fred hazelton/stats team/pipeline/hazeydata/theme-park-crowd-report` |
| **Cron** | Single daily run at **6:00 AM Eastern** (`run_daily_pipeline.sh`); **hourly** docs check (`check_docs_for_instructions.sh`) |
| **Queue-times** | Continuous loop (every 5 min); **systemd service**, starts on boot |
| **Dropbox** | Synced under fred's home: `~/TouringPlans.com Dropbox/` |

---

## 2. Config

- **File:** `config/config.json`
- **Important key:** `output_base` - all pipeline data and logs go under this path.
- **Current value:**
  `/home/fred/TouringPlans.com Dropbox/fred hazelton/stats team/pipeline/hazeydata/theme-park-crowd-report`
- **AWS:** Scripts and cron use `~/.aws/credentials` and `~/.aws/config` (needed for S3 ETL and dimension fetches).
- **DEV_MODE:** Set `DEV_MODE=true` (env) to use **repo/pipeline_dev** as output base and to filter ETL to 37 dev entities only. See `config/dev_config.py`. Shell (`scripts/common.sh`) and Python (`src/utils/paths.py`) both respect DEV_MODE. Run: `export DEV_MODE=true && ./scripts/run_daily_pipeline.sh` (use `--skip-dropbox-check` if output is not on Dropbox).

---

## 3. What runs when

### 3.1 Daily pipeline (cron, 6:00 AM Eastern)

- **What:** One cron job runs `scripts/run_daily_pipeline.sh --skip-dropbox-check --skip-if-unchanged --use-synthetic`.
- **Order:** S3 sync → ETL → Dimension fetches → Closures → Impute park hours → Posted aggregates → Wait time DB report → Batch training → Forecast → WTI → **Validation** (post-run data quality checks).
- **Runs as:** wilma (wilma's crontab).
- **Log:** `output_base/logs/daily_pipeline_YYYY-MM-DD.log`
- **Lock:** `state/daily_pipeline.lock` — only one run at a time. If the previous run is still in progress (e.g. still training), the next 6 AM run skips cleanly (exit 0) so it doesn't kill or conflict with the other run.
- **Training:** Uses **hybrid pipeline** (Julia XGBoost) — trains 141 models in ~67 seconds. See **docs/HYBRID_PIPELINE.md** and **docs/PIPELINE_TIMING_AND_PARALLELIZATION.md**.
- **Skip-if-unchanged:** Data-driven cascade. Training skips only if no entities have new observations (entity_index.sqlite). Forecast skips only if training was skipped. WTI skips only if forecast was skipped. See **docs/PIPELINE_DATA_FLOW.md § Skip-If-Unchanged Logic**.

### 3.2 Queue-times loop (systemd, on boot + always)

- **What:** Fetches wait times from queue-times.com every 5 minutes; writes to `output_base/staging/queue_times/`. Morning ETL later merges staging into `fact_tables/clean`.
- **Service:** `queue-times-loop.service` (user fred, project dir = repo path).
- **Starts:** Automatically on boot if you ran `sudo bash scripts/install_queue_times_service.sh`.
- **Log (systemd):** `sudo journalctl -u queue-times-loop -f`
  Optional file log: `output_base/logs/queue_times_loop.log` if started manually with redirect.

### 3.3 Docs check (cron, hourly, survives reboot)

- **What:** Pulls latest from git, checks WILMA-BAMBAM.md Active Items for changes. Logs when new instructions detected.
- **Script:** `scripts/check_docs_for_instructions.sh`
- **Schedule:** Every hour at minute 0 (cron). Cron daemon starts on boot, so job runs after reboot.
- **Install:** `bash scripts/install_docs_check_cron.sh` (adds to current user's crontab)
- **Log:** `output_base/logs/docs_check.log` (or `repo/logs/docs_check.log` if output_base unavailable)
- **State:** `repo/state/docs_check_last_hash` — hash of Active Items for change detection

---

## 4. Key paths

All under **output_base** unless noted.

| Path | Purpose |
|------|--------|
| `output_base/` | Root for all pipeline data (see config) |
| `output_base/logs/` | Pipeline logs (daily pipeline, queue_times_loop, docs_check, etc.) |
| `output_base/dimension_tables/` | dimentity, dimparkhours, dimdategroupid, dimseason, etc. |
| `output_base/raw_closures/` | Closure CSVs from S3 (current_wdw_closures.csv, etc.); input for operating calendar |
| `output_base/operating_calendar/` | operating_calendar.parquet (entity_code, park_date, is_operating); used by training, forecast, WTI |
| `output_base/pipeline_validation/` | validation_report.json, validation_report.txt (post-run checks) |
| `output_base/fact_tables/clean/` | Cleaned wait time fact CSVs (by date) |
| `output_base/staging/queue_times/` | Queue-times fetcher output (before ETL merge) |
| `output_base/aggregates/` | posted_aggregates.parquet (for forecast) |
| `output_base/models/` | Per-entity XGBoost (or mean) models |
| `output_base/curves/forecast/` | Forecast curves (actual/posted predicted) |
| `output_base/state/` | entity_index.sqlite, pipeline_state.json, run_manifest.json, pipeline_status.json, daily_pipeline.lock, encoding_mappings.json, **etl_last_run.json** (timestamp of last successful ETL; ETL only processes files modified since), processed_files.json, failed_files.json, dedupe.sqlite, etc. |
| `output_base/tpcr_live.duckdb` | Shared DuckDB for bot + dashboard (live_waits, wti, forecasts, entities, data_freshness). Created by `init_live_duckdb.py`; dual-written by scraper, WTI, forecast, dimension scripts. |
| `output_base/raw/` | Synced S3 data: `raw/export/wait_times/`, `raw/export/fastpass_times/` (ETL reads from here only; sync-only, no S3 streaming). |
| `output_base/reports/` | wait_time_db_report.md, etc. |

---

## 5. Commands reference

### Cron (daily pipeline)

```bash
# View cron
crontab -l

# Install single 6 AM daily pipeline (current setup)
bash scripts/install_cron.sh --daily-master

# Remove cron
bash scripts/install_cron.sh --remove

# Preview what would be installed
bash scripts/install_cron.sh --show
```

### Docs check (hourly, survives reboot)

```bash
# Install hourly docs-check job (WILMA-BAMBAM.md Active Items)
bash scripts/install_docs_check_cron.sh

# Preview
bash scripts/install_docs_check_cron.sh --show

# Remove
bash scripts/install_docs_check_cron.sh --remove

# View log
tail -f output_base/logs/docs_check.log
```

### Init DuckDB (bot + dashboard)

```bash
# Run once after pipeline has produced wti, forecasts, dimentity, staging
python scripts/init_live_duckdb.py [--output-base PATH]
```

Creates `output_base/tpcr_live.duckdb` with schema and backfills from existing data. Scraper, WTI, forecast, and dimension scripts dual-write to it when it exists.

### Manual daily run

```bash
cd /home/fred/Desktop/theme-park-crowd-report
./scripts/run_daily_pipeline.sh
# Or in background with log:
nohup ./scripts/run_daily_pipeline.sh >> "output_base/logs/daily_pipeline_$(date +%Y-%m-%d).log" 2>&1 &
```

**S3 sync:** The pipeline runs `scripts/sync_s3_data.sh` before ETL to sync `wait_times` and `fastpass_times` from S3 into `output_base/raw/`. ETL is sync-only and always reads from `output_base/raw/` (no S3 streaming). Use `--skip-sync` only if you have already synced; ETL will still read from `raw/`.

**ETL incremental:** ETL only processes files modified since the last successful run (`state/etl_last_run.json`). Reduces daily processing from ~36 files to ~5-7 (skips old 2013-2019 files that fail with "No columns to parse"). Use `--full-rebuild` to process all files.

**Dropbox:** If `output_base` is under Dropbox, the pipeline force-quits Dropbox before starting (to avoid file locks / partial reads). It runs `dropbox stop` (or `pkill -TERM` if no CLI), waits up to 15s for exit, then proceeds. Dropbox stays stopped until you start it again (or next login if it auto-starts). Use `--skip-dropbox-check` to skip stopping Dropbox (e.g. if output is not on Dropbox).

**Single-park test:** To run the pipeline for one park only (training, forecast, WTI), use `--park PARK` so the run finishes in a reasonable time during development. Example: `./scripts/run_daily_pipeline.sh --park MK`. Exclude water parks (TL) when choosing; see **docs/SINGLE_PARK_TEST.md** for the entity-count query.

### Queue-times (systemd)

```bash
# Install and enable on boot (run once; needs sudo)
sudo bash scripts/install_queue_times_service.sh

# Status
sudo systemctl status queue-times-loop

# Logs (live)
sudo journalctl -u queue-times-loop -f

# Stop
sudo systemctl stop queue-times-loop

# Remove service and disable on boot
sudo bash scripts/install_queue_times_service.sh --remove
```

### Queue-times (manual, no systemd)

```bash
cd /home/fred/Desktop/theme-park-crowd-report
nohup bash scripts/run_queue_times_loop.sh --interval 300 >> "output_base/logs/queue_times_loop.log" 2>&1 &
```

### Prerequisites / quick check

```bash
python scripts/check_prerequisites.py
```

### Dashboard (pipeline + queue-times + entities)

Single-page status dashboard (Python Dash). Refreshes every 5 minutes. Optional Basic Auth for sharing (e.g. with wilma).

```bash
# Run (no auth)
python dashboard/app.py

# Run with auth (share URL + credentials)
DASH_USER=admin DASH_PASSWORD=your-secret python dashboard/app.py
```

- **URL:** http://localhost:8050 or http://\<this-machine-ip\>:8050 (binds to 0.0.0.0)
- **Data:** Reads `output_base/state/pipeline_status.json` (written by daily pipeline and train_batch_entities) and `output_base/state/entity_index.sqlite`; checks queue-times process via `pgrep`
- See **dashboard/README.md** for details

**Stream dashboard** (wait-time overlay): `./scripts/start-stream.sh` or `python3 dashboard/stream_server.py` → http://localhost:8889/stream-dashboard.html. Data from API (wilma-server:8051). See **dashboard/README_STREAM.md**.

---

## 6. Other docs

- **docs/DAILY_DOCUMENTATION_REVIEW.md** - End-of-day checklist to keep PIPELINE_STATE, README, and key docs in sync.
- **LINUX_CRON_SETUP.md** - Cron options (five separate jobs vs single daily master), queue-times service, log paths.
- **docs/REFRESH_READINESS.md** - Full refresh order, what's in/out of cron, common gaps.
- **scripts/README.md** - All scripts (run_daily_pipeline.sh, install_cron.sh, install_docs_check_cron.sh, install_queue_times_service.sh, etc.).

---

## 7. Known outages / disabled jobs

### 7.1 Discord daily crowd report — DISABLED (Sept 9, 2026)

| Field | Value |
|-------|-------|
| **Script** | `tpcr-discord-bot/daily_report.py` |
| **What it does** | One-shot cron job: reads WTI from DuckDB, runs quality gate, posts embed to `#crowd-reports` (`1478240066382860298`) via Discord REST API |
| **Last successful post** | **2026-09-07** |
| **Sept 8 failure** | Quality gate correctly rejected: "Missing WTI for MK" — post skipped (working as designed) |
| **Cron status** | **Commented out** on wilma-server crontab on 2026-09-09 by Paul — `# DISABLED 2026-09-09 by Paul — Fred: kill Discord nightly reviews/reports` |
| **Original cron line** | `15 7 * * * ... daily_report.py` (7:15 AM ET) |
| **Log** | `/tmp/daily_report.log` on wilma-server (mtime Sept 8 07:15) |
| **Not the live bot** | This is separate from `bot.py` (see 7.2 below). `daily_report.py` is a cron one-shot; `bot.py` is a long-running systemd service handling slash commands. |

**Fred decisions required:**
1. Re-enable the cron line on wilma-server? (The quality gate will skip posts if pipeline data is stale or missing.)
2. Investigate the Sept 8 "Missing WTI for MK" — was it a one-day pipeline gap, or is WTI generation still broken?
3. Post a gap notice to `#announcements` per Domain 4 of `docs/TPCR_CUSTOMER_SERVICE_DESIGN_SPEC.md`?

### 7.2 Live Discord bot (tpcr-discord-bot.service) — CRASH-LOOPING

| Field | Value |
|-------|-------|
| **Script** | `tpcr-discord-bot/bot.py` |
| **What it does** | Long-running systemd service: handles `/today`, `/crowd`, `/best-day`, `/now`, `/ask`, `/health`, `/about` slash commands |
| **Service** | `tpcr-discord-bot` (systemd user service on wilma-server) |
| **Error** | `discord.errors.LoginFailure: Improper token has been passed.` (HTTP 401 Unauthorized) |
| **Restart count** | 55+ (crash-loop) |
| **Token source** | `DISCORD_BOT_TOKEN` from `~/.env` on wilma-server |

**Fred decisions required:**
1. Regenerate the Discord bot token in the [Discord Developer Portal](https://discord.com/developers/applications) and update `~/.env` on wilma-server?
2. Restart the service after token fix: `systemctl --user restart tpcr-discord-bot`

### 7.3 Bot health-check cron — DISABLED

| Field | Value |
|-------|-------|
| **Script** | `scripts/tpcr_bot_health_check.py` |
| **Cron status** | Commented out on wilma-server |

**Fred decision required:** Re-enable after bot token is fixed?

---

## 8. Changes from default (historical)

- **Cron:** We use the **single daily master** at 6 AM (not the five separate jobs).
- **Output base:** Set to **fred's Dropbox** path under home (not `/media/fred/...`).
- **Queue-times:** Configured as a **systemd service** for user fred, starts on boot; unit file and install script are in `scripts/`.
- **Wilma:** No theme-park cron or queue-times under wilma; everything runs as **fred**.
