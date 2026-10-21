# Discord Bot → Shared DuckDB Migration Spec

**Date:** Feb 19, 2026  
**Author:** Wilma  
**Priority:** 🔴 HIGH — bot data feed has been broken since Feb 5 (scraper crash); this migration makes it robust  
**Goal:** Unify all bot + dashboard data access through a single persistent DuckDB file, and move bot code into the main repo

---

## Problem Statement

The Discord bot currently:
1. Scans raw CSV files in `staging/queue_times/` for live wait times (slow, fragile)
2. Reads parquet files directly from scattered pipeline paths (WTI, forecasts, fact tables)
3. Lives in a separate repo (`~/tpcr-discord-bot/`) while importing code from the main pipeline repo
4. Has no single data layer — every query builds a throwaway DuckDB instance

The queue-times scraper writes CSVs that the bot scans. When the scraper died (deleted wrapper script), the bot silently served stale data for 2 weeks. Nobody noticed because there's no health check on data freshness.

## Target Architecture

```
Queue-Times Scraper (every 5 min)
    │
    ├──→ INSERT INTO tpcr_live.duckdb:live_waits
    │
Daily Pipeline (6am)
    │
    ├──→ INSERT/REPLACE INTO tpcr_live.duckdb:forecasts
    ├──→ INSERT/REPLACE INTO tpcr_live.duckdb:wti
    ├──→ INSERT/REPLACE INTO tpcr_live.duckdb:entities
    │
Discord Bot ←── SELECT FROM tpcr_live.duckdb (millisecond reads)
Dashboard API ←── SELECT FROM tpcr_live.duckdb (same source)
```

**Single file:** `/mnt/data/pipeline/tpcr_live.duckdb`

---

## Phase 1: Create DuckDB Schema + Scraper Migration

### 1.1 Database Schema

Create `/mnt/data/pipeline/tpcr_live.duckdb` with these tables:

```sql
-- Live wait times (scraper writes here every 5 min)
CREATE TABLE IF NOT EXISTS live_waits (
    entity_code     VARCHAR NOT NULL,
    observed_at     TIMESTAMP WITH TIME ZONE NOT NULL,
    wait_time_type  VARCHAR DEFAULT 'POSTED',
    wait_time_minutes INTEGER NOT NULL,
    park_date       DATE NOT NULL,
    inserted_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (entity_code, observed_at, wait_time_type)
);

-- Index for the bot's most common query: latest wait per entity for a park today
CREATE INDEX IF NOT EXISTS idx_live_waits_park_date 
    ON live_waits (park_date, entity_code);

-- WTI scores (daily pipeline writes here)
CREATE TABLE IF NOT EXISTS wti (
    park_code       VARCHAR NOT NULL,
    park_date       DATE NOT NULL,
    time_slot       VARCHAR,          -- NULL = daily average
    wti             DOUBLE NOT NULL,
    source          VARCHAR DEFAULT 'forecast',  -- 'forecast' or 'observed'
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (park_code, park_date, time_slot)
);

-- Forecasts (daily pipeline writes here)
CREATE TABLE IF NOT EXISTS forecasts (
    entity_code     VARCHAR NOT NULL,
    park_date       DATE NOT NULL,
    time_slot       VARCHAR NOT NULL,
    predicted_actual DOUBLE,
    predicted_posted DOUBLE,
    model_version   VARCHAR,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (entity_code, park_date, time_slot)
);

-- Entity metadata (refreshed by pipeline dimension step)
CREATE TABLE IF NOT EXISTS entities (
    entity_code     VARCHAR PRIMARY KEY,
    entity_name     VARCHAR,
    short_name      VARCHAR,
    park_code       VARCHAR,
    property_code   VARCHAR,
    category        VARCHAR,           -- 'ride', 'show', etc.
    has_wait_times  BOOLEAN DEFAULT TRUE,
    wait_time_type  VARCHAR DEFAULT 'standby',  -- 'standby', 'priority'
    is_extinct      BOOLEAN DEFAULT FALSE,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Data freshness tracking (for health checks)
CREATE TABLE IF NOT EXISTS data_freshness (
    source          VARCHAR PRIMARY KEY,  -- 'scraper', 'pipeline', 'wti', 'forecasts'
    last_updated    TIMESTAMP NOT NULL,
    row_count       INTEGER,
    notes           VARCHAR
);
```

### 1.2 Update Queue-Times Scraper

**File:** `src/get_wait_times_from_queue_times.py`

After writing CSVs to staging (keep this for backward compat), also INSERT into DuckDB:

```python
import duckdb

DUCKDB_PATH = "/mnt/data/pipeline/tpcr_live.duckdb"

def write_to_duckdb(df: pd.DataFrame):
    """Append new wait time observations to persistent DuckDB."""
    con = duckdb.connect(DUCKDB_PATH)
    try:
        # Upsert: INSERT OR REPLACE handles duplicates via primary key
        con.execute("""
            INSERT OR REPLACE INTO live_waits 
            (entity_code, observed_at, wait_time_type, wait_time_minutes, park_date)
            SELECT entity_code, observed_at::TIMESTAMPTZ, wait_time_type, 
                   wait_time_minutes, park_date::DATE
            FROM df
        """)
        # Update freshness
        con.execute("""
            INSERT OR REPLACE INTO data_freshness (source, last_updated, row_count)
            VALUES ('scraper', CURRENT_TIMESTAMP, (SELECT COUNT(*) FROM live_waits))
        """)
    finally:
        con.close()
```

**Keep CSV output too** — the daily pipeline's ETL merge step still reads from staging CSVs. We can remove that later.

### 1.3 Init Script

Create `scripts/init_live_duckdb.py`:

```python
"""Initialize the shared DuckDB database with schema + backfill from existing data."""
import duckdb

DUCKDB_PATH = "/mnt/data/pipeline/tpcr_live.duckdb"

def init():
    con = duckdb.connect(DUCKDB_PATH)
    
    # Create tables (idempotent)
    con.execute("""...""")  # All CREATE TABLE statements from above
    
    # Backfill live_waits from existing staging CSVs
    con.execute("""
        INSERT OR IGNORE INTO live_waits 
        SELECT entity_code, observed_at::TIMESTAMPTZ, wait_time_type,
               wait_time_minutes::INTEGER, 
               observed_at::DATE as park_date
        FROM read_csv('/mnt/data/pipeline/staging/queue_times/**/*.csv', 
                      AUTO_DETECT=TRUE)
        WHERE wait_time_minutes > 0
    """)
    
    # Backfill WTI from parquet
    con.execute("""
        INSERT OR IGNORE INTO wti
        SELECT park_code, park_date, time_slot, wti, 'forecast' as source,
               CURRENT_TIMESTAMP as updated_at
        FROM read_parquet('/mnt/data/pipeline/wti/wti.parquet')
    """)
    
    # Backfill entities from dimentity
    con.execute("""
        INSERT OR IGNORE INTO entities (entity_code, entity_name, short_name, park_code)
        SELECT code, name, short_name, park_code
        FROM read_csv('/mnt/data/pipeline/dimension_tables/dimentity.csv', AUTO_DETECT=TRUE)
    """)
    
    # Backfill forecasts from parquet
    con.execute("""
        INSERT OR IGNORE INTO forecasts (entity_code, park_date, time_slot, predicted_actual)
        SELECT entity_code, park_date, time_slot, predicted_actual,
               CURRENT_TIMESTAMP as updated_at
        FROM read_parquet('/mnt/data/pipeline/curves/forecast_parquet/all_forecasts.parquet')
    """)
    
    con.close()
    print("✅ DuckDB initialized and backfilled")

if __name__ == "__main__":
    init()
```

---

## Phase 2: Update Daily Pipeline to Write to DuckDB

**Files to modify:**

### 2.1 WTI Calculation (`scripts/calculate_wti_simple.py`)
After writing `wti.parquet`, also write to DuckDB:
```python
# After writing parquet (keep parquet for backward compat)
con = duckdb.connect(DUCKDB_PATH)
con.execute("DELETE FROM wti WHERE park_date >= ?", [min_date])
con.execute("INSERT INTO wti SELECT ... FROM wti_df")
con.execute("INSERT OR REPLACE INTO data_freshness VALUES ('wti', CURRENT_TIMESTAMP, ...)")
con.close()
```

### 2.2 Forecast Generation (`scripts/forecast_vectorized.py`)
After writing forecast parquet, also write to DuckDB:
```python
con = duckdb.connect(DUCKDB_PATH)
con.execute("DELETE FROM forecasts WHERE park_date >= ?", [today])
con.execute("INSERT INTO forecasts SELECT ... FROM forecast_df")
con.execute("INSERT OR REPLACE INTO data_freshness VALUES ('forecasts', CURRENT_TIMESTAMP, ...)")
con.close()
```

### 2.3 Dimension Refresh (`scripts/run_dimension_fetches.sh` or relevant script)
After writing dimentity.csv, also refresh entities table:
```python
con = duckdb.connect(DUCKDB_PATH)
con.execute("DELETE FROM entities")
con.execute("INSERT INTO entities SELECT ... FROM read_csv('dimentity.csv')")
con.close()
```

### 2.4 Data Retention

Add to pipeline or as cron job — prune old live_waits data:
```sql
-- Keep 90 days of live wait data; older data is in fact_tables parquet
DELETE FROM live_waits WHERE park_date < CURRENT_DATE - INTERVAL 90 DAY;
```

---

## Phase 3: Update Discord Bot to Read from DuckDB

### 3.1 Move Bot into Main Repo

```bash
# Move bot code
cp ~/tpcr-discord-bot/bot.py ~/theme-park-crowd-report/tpcr-discord-bot/bot.py
cp ~/tpcr-discord-bot/forecast_image.py ~/theme-park-crowd-report/tpcr-discord-bot/forecast_image.py
cp ~/tpcr-discord-bot/daily_report.py ~/theme-park-crowd-report/tpcr-discord-bot/daily_report.py

# Historical (Wilma, systemd user service). The bot now runs on the Mac Mini under
# launchd (com.hazeydata.tpcr-bot); see tpcr-discord-bot/README.md "Host".
# Update systemd service to point to new location
# ExecStart: WorkingDirectory=/home/wilma/theme-park-crowd-report/tpcr-discord-bot
```

### 3.2 Replace File-Based Queries

**Current (bot.py line 48-52):**
```python
WTI_PATH = "/mnt/data/pipeline/wti/wti.parquet"
FORECASTS_PATH = "/mnt/data/pipeline/curves/forecast_parquet/all_forecasts.parquet"
ENTITIES_PATH = "/mnt/data/pipeline/dimension_tables/dimentity.csv"
STAGING_DIR = "/mnt/data/pipeline/staging/queue_times"
FACT_PARQUET_DIR = "/mnt/data/pipeline/fact_tables/parquet"
```

**New:**
```python
DUCKDB_PATH = "/mnt/data/pipeline/tpcr_live.duckdb"

def get_db():
    """Get a read-only DuckDB connection."""
    return duckdb.connect(DUCKDB_PATH, read_only=True)
```

### 3.3 Rewrite Key Functions

**`get_current_waits()` — currently 100 lines of CSV/parquet scanning:**
```python
def get_current_waits(park_code: str, target_date: date_type):
    """Get most recent posted wait times from DuckDB."""
    try:
        con = get_db()
        entity_filter = _entity_filter_sql(park_code)
        result = con.execute(f"""
            WITH latest AS (
                SELECT entity_code, MAX(observed_at) as max_obs
                FROM live_waits
                WHERE {entity_filter}
                  AND park_date = ?
                  AND wait_time_type = 'POSTED'
                  AND wait_time_minutes > 0
                GROUP BY entity_code
            )
            SELECT w.entity_code, w.wait_time_minutes as current_wait,
                   w.observed_at as latest_obs
            FROM live_waits w
            JOIN latest l ON w.entity_code = l.entity_code 
                         AND w.observed_at = l.max_obs
            WHERE w.wait_time_type = 'POSTED'
              AND w.wait_time_minutes > 0
            ORDER BY w.wait_time_minutes DESC
        """, [target_date]).fetchdf()
        con.close()
        return result
    except Exception as e:
        print(f"⚠️ Error getting current waits: {e}")
        return pd.DataFrame()
```

**`get_wti_score()` — currently reads parquet each time:**
```python
def get_wti_score(park_code: str, target_date: date_type):
    con = get_db()
    result = con.execute("""
        SELECT AVG(wti) as wti FROM wti
        WHERE park_code = ? AND park_date = ?
    """, [park_code, target_date]).fetchone()
    con.close()
    return result[0] if result else None
```

**`get_wti_range()` — currently reads forecasts parquet:**
```python
def get_wti_range(park_code: str, target_date: date_type):
    con = get_db()
    result = con.execute("""
        SELECT time_slot, AVG(predicted_actual) as slot_avg
        FROM forecasts
        WHERE entity_code LIKE ? || '%'
          AND park_date = ?
          AND time_slot BETWEEN '08:00' AND '22:00'
        GROUP BY time_slot ORDER BY time_slot
    """, [park_code, target_date]).fetchdf()
    con.close()
    if len(result) == 0:
        return None
    return {"wti_low": float(result["slot_avg"].min()), ...}
```

**Entity names — loaded at startup from DuckDB:**
```python
def load_entity_names():
    con = get_db()
    df = con.execute("SELECT entity_code, entity_name, short_name FROM entities").fetchdf()
    con.close()
    return {row.entity_code: (row.entity_name, row.short_name) 
            for row in df.itertuples()}
```

### 3.4 Add Health Check

New `/health` slash command (or extend `/ping`):
```python
@tree.command(name="health", description="Check data pipeline status")
async def health_command(interaction: discord.Interaction):
    con = get_db()
    freshness = con.execute("SELECT source, last_updated FROM data_freshness").fetchdf()
    latest_wait = con.execute(
        "SELECT MAX(observed_at) FROM live_waits WHERE park_date = CURRENT_DATE"
    ).fetchone()
    con.close()
    
    # Alert if scraper data is >15 min old
    # Alert if pipeline data is >26 hours old
    # Show status embed
```

---

## Phase 4: Update Dashboard API

**File:** `dashboard/api.py`

Same pattern — replace file reads with DuckDB queries. The API already uses DuckDB internally (in-memory), so this is mostly replacing `read_parquet()` / `read_csv()` calls with table queries.

```python
DUCKDB_PATH = "/mnt/data/pipeline/tpcr_live.duckdb"

# At startup, connect read-only
db = duckdb.connect(DUCKDB_PATH, read_only=True)

# Replace _load_live_wait_times() etc. with direct queries
```

---

## Migration Steps (Execution Order)

1. **[x] Create init script** — `scripts/init_live_duckdb.py` (schema + backfill)
2. **[ ] Run init** — creates DuckDB with all existing data (run on wilma-server after pipeline) _(historical: Wilma host; the bot now runs on the Mac Mini under launchd `com.hazeydata.tpcr-bot`)_
3. **[x] Update scraper** — dual-write (CSV + DuckDB) for safety
4. **[ ] Verify** — check DuckDB has fresh data after a few scraper cycles
5. **[x] Update pipeline** — WTI, forecasts, entities write to DuckDB after parquet
6. **[ ] Move bot code** — into main repo `tpcr-discord-bot/` directory
7. **[ ] Rewrite bot queries** — use DuckDB instead of file scanning
8. **[ ] Update systemd service** — point to new bot location
9. **[ ] Test all commands** — `/now`, `/today`, `/crowd`, `/best-day`, `/ping`
10. **[ ] Update dashboard API** — same DuckDB source
11. **[ ] Add freshness monitoring** — `/health` command + alerts
12. **[ ] Remove CSV dual-write** — once everything reads from DuckDB

**Backward compatibility:** Keep parquet/CSV writes during transition. Remove only after confirming everything reads from DuckDB.

---

## Concurrency Notes

- DuckDB supports **multiple concurrent readers** (read_only connections)
- **One writer at a time** — scraper writes every 5 min, pipeline writes once/day at 6am, no conflict
- Bot and dashboard API use `read_only=True` connections — never block writers
- If we ever need concurrent writes: use WAL mode (`PRAGMA enable_wal`)

---

## File Summary

| File | Action | Notes |
|------|--------|-------|
| `scripts/init_live_duckdb.py` | **CREATE** | Schema + backfill |
| `src/get_wait_times_from_queue_times.py` | **MODIFY** | Add DuckDB INSERT after CSV write |
| `scripts/calculate_wti_simple.py` | **MODIFY** | Add DuckDB write after parquet |
| `scripts/forecast_vectorized.py` | **MODIFY** | Add DuckDB write after parquet |
| `tpcr-discord-bot/bot.py` | **MOVE + REWRITE** | From ~/tpcr-discord-bot/ → main repo; replace file queries |
| `tpcr-discord-bot/forecast_image.py` | **MOVE** | Same |
| `tpcr-discord-bot/daily_report.py` | **MOVE** | Same |
| `dashboard/api.py` | **MODIFY** | Replace file reads with DuckDB |
| `/etc/systemd/system/tpcr-discord-bot.service` | **MODIFY** | Update WorkingDirectory |
| `/etc/systemd/system/queue-times-loop-wilma.service` | No change | Already points to main repo (historical: Wilma host, not the current bot host) |

---

## Success Criteria

- [ ] `/now mk` returns live data within 5 min of park open
- [ ] `/today` shows all park WTI scores
- [ ] `/crowd mk` returns forecast curve
- [ ] Data freshness tracked — stale data triggers alert
- [ ] Bot code lives in main repo
- [ ] Dashboard API and bot read from same DuckDB
- [ ] No increase in query latency (should decrease)
