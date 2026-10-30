#!/usr/bin/env python3
"""
Queue-Times.com Wait Time Fetcher

================================================================================
PURPOSE
================================================================================
Fetches wait time data from queue-times.com API and transforms it to match
the existing wait time fact table format.

  1. Fetches park list from https://queue-times.com/parks.json
  2. For each park, fetches wait times from https://queue-times.com/parks/{id}/queue_times.json
  3. Transforms data to match existing schema:
     - entity_code (str, uppercase)
     - observed_at (str, ISO with timezone)
     - wait_time_type ("POSTED")
     - wait_time_minutes (int)
  4. Deduplicates using SQLite (same as S3 pipeline)
  5. Writes to staging/queue_times/YYYY-MM/{park}_{YYYY-MM-DD}.csv (not fact_tables)
  6. Morning ETL merges yesterday's staging into fact_tables/clean at the start of its run

================================================================================
OUTPUT
================================================================================
  - Scraper: staging/queue_times/YYYY-MM/{park}_{YYYY-MM-DD}.csv (4 columns, same schema)
  - Morning ETL: merges yesterday's staging into fact_tables/clean, then deletes staged files
  - Each CSV has 4 columns:
      entity_code       (str, uppercase)  e.g. "MK101", "EP09"
      observed_at       (str, ISO with timezone)  e.g. "2024-01-15T10:30:00-05:00"
      wait_time_type    "POSTED"
      wait_time_minutes (int)  wait time in minutes

================================================================================
KEY FEATURES
================================================================================
  - API-based: Fetches live data from queue-times.com
  - HOURS-BASED FILTER: Uses dimparkhours to only call the API when a park is
    in-window (open-90 to close+90 in park TZ). If no parks in-window, exits
    without API calls. Use --no-hours-filter to disable.
  - DEDUPLICATION: SQLite DB ensures no duplicate rows across runs
  - SAME OUTPUT FORMAT: Compatible with existing S3 pipeline output
  - PROCESS LOCK: Prevents multiple instances from running at once
  - STAGING (not fact_tables): Raw fact_tables stay static for modelling; scraper writes
    to staging/queue_times; morning ETL appends yesterday's staging into fact_tables.
  - CONTINUOUS MODE: --interval SECS runs a loop (fetch, write to staging, sleep).
    e.g. --interval 300 for every 5 minutes. Staging is also available for live use.
  - OBSERVED_AT: Taken from queue-times last_updated (per ride). The API can return
    stale timestamps; observed_at may be older than the fetch time. The scraper logs
    when observed_at is more than 24h older than fetch time (STALE_OBSERVED_AT_THRESHOLD_HOURS).
    See docs/STALE_OBSERVED_AT.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
from zoneinfo import ZoneInfo

try:
    import duckdb
except ImportError:
    duckdb = None

# ----- Ensure we can import from src/ when run from project root -----
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

# Import shared utilities from S3 pipeline
from get_tp_wait_time_data_from_s3 import (
    PARK_CODE_MAP,
    acquire_lock,
    derive_park_date,
    ensure_sqlite,
    get_output_directories,
    get_park_code,
    insert_new_mask,
    release_lock,
    setup_logging,
    write_grouped_csvs,
)
from utils import get_output_base

# =============================================================================
# CONFIGURATION CONSTANTS
# =============================================================================

QUEUE_TIMES_BASE_URL = "https://queue-times.com"
PARKS_ENDPOINT = f"{QUEUE_TIMES_BASE_URL}/parks.json"
QUEUE_TIMES_ENDPOINT_FMT = f"{QUEUE_TIMES_BASE_URL}/parks/{{park_id}}/queue_times.json"

# State and output file names
DEDUPE_DB_NAME = "dedupe_queue_times.sqlite"
LOCK_FILE_NAME = "processing_queue_times.lock"

# Request settings
REQUEST_TIMEOUT = 30
REQUEST_RETRIES = 3
REQUEST_RETRY_DELAY = 2

# observed_at comes from queue-times last_updated; if older than this vs fetch time, we log a warning
STALE_OBSERVED_AT_THRESHOLD_HOURS = 24

# =============================================================================
# Maps queue-times.com park IDs to our park codes.
# Based on supported parks: wdw, dlr, uor, ush, tdr
# Park codes match PARK_CODE_MAP in get_tp_wait_time_data_from_s3.py
# Restored after accidental removal in c4d11f2d (#464)
# =============================================================================

QUEUE_TIMES_PARK_MAP: Dict[int, str] = {
    # Walt Disney World (wdw)
    6: "mk",   # Disney Magic Kingdom
    5: "ep",   # Epcot
    7: "hs",   # Disney Hollywood Studios
    8: "ak",   # Animal Kingdom

    # Disneyland Resort (dlr)
    16: "dl",  # Disneyland
    17: "ca",  # Disney California Adventure

    # Universal Orlando Resort (uor)
    64: "ia",  # Islands Of Adventure
    65: "uf",  # Universal Studios Orlando
    334: "eu", # Epic Universe

    # Universal Studios Hollywood (ush)
    66: "uh",  # Universal Studios Hollywood

    # Tokyo Disney Resort (tdr)
    274: "tdl", # Tokyo Disneyland
    275: "tds", # Tokyo DisneySea
}

# =============================================================================
# DUCKDB DUAL-WRITE (live_waits for bot + dashboard)
# =============================================================================

# Writes go through live_duckdb_writes (constraint-free: works whether or not
# live_waits / data_freshness have PKs); the subprocess runs write_scraper_batch.
# Body of the script run via `python -c` (see _live_write_script).
_LIVE_WRITE_SCRIPT_BODY = """sys.path.insert(0, SRC_DIR)
try:
    import live_duckdb_writes
    con = duckdb.connect(DB_PATH)
    try:
        dropped = live_duckdb_writes.write_scraper_batch(con, TMP_PATH)
    finally:
        con.close()
    os.unlink(TMP_PATH)
    print("DROPPED:" + str(dropped))
    print("OK")
except Exception as e:
    print("ERR:" + str(e), file=sys.stderr)
    sys.exit(1)
"""


def _live_write_script(db_path, tmp_path) -> str:
    """Build the `python -c` script; paths are embedded as repr() literals."""
    return (
        "import duckdb, os, sys\n"
        f"DB_PATH = {str(db_path)!r}\n"
        f"TMP_PATH = {str(tmp_path)!r}\n"
        f"SRC_DIR = {str(Path(__file__).resolve().parent)!r}\n"
        + _LIVE_WRITE_SCRIPT_BODY
    )


def _write_to_live_duckdb(
    df: pd.DataFrame,
    park_tz: ZoneInfo,
    output_base: Path,
    logger: logging.Logger,
    max_retries: int = 5,
) -> bool:
    """
    Append new wait time rows to tpcr_live.duckdb live_waits table.
    Uses subprocess to ensure DuckDB file handles are fully released after each write.
    The subprocess imports duckdb, writes, and exits — no persistent lock (#463).
    """
    import time as _time
    import subprocess
    import tempfile

    if duckdb is None or df.empty:
        return False
    db_path = output_base / "tpcr_live.duckdb"
    if not db_path.exists():
        logger.debug("Skipping DuckDB write: tpcr_live.duckdb not found")
        return False

    # Prepare data
    raw_count = len(df)
    try:
        df = df.copy()
        df["park_date"] = derive_park_date(df["observed_at"], park_tz)
        df = df.dropna(subset=["park_date"])
        if df.empty:
            return False
        df = df[df["wait_time_minutes"] > 0]
        if df.empty:
            return False
    except Exception as e:
        logger.warning(f"DuckDB data prep failed: {e}")
        return False

    # Write data to temp CSV, then use subprocess to INSERT into DuckDB
    # Subprocess ensures DuckDB connection is fully closed (no persistent file handles)
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
            tmp_path = tmp.name
            df[["entity_code", "observed_at", "wait_time_type", "wait_time_minutes", "park_date"]].to_csv(tmp, index=False)
    except Exception as e:
        logger.warning(f"Failed to write temp CSV: {e}")
        return False

    write_script = _live_write_script(db_path, tmp_path)

    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                [sys.executable, "-c", write_script],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and "OK" in result.stdout:
                m = re.search(r"DROPPED:(\d+)", result.stdout)
                total_dropped = (raw_count - len(df)) + (int(m.group(1)) if m else 0)
                if total_dropped > 0:
                    logger.warning(
                        f"live_waits: dropped {total_dropped} row(s) with NULL/empty "
                        "entity_code, observed_at or wait_time_type"
                    )
                logger.debug(f"Wrote {len(df)} rows to live_waits (subprocess)")
                return True
            raise RuntimeError(result.stderr[:300] if result.stderr else "subprocess failed")
        except Exception as e:
            error_str = str(e).lower()
            is_lock = any(kw in error_str for kw in ("lock", "busy", "io error", "could not set", "blocked"))
            if is_lock and attempt < max_retries - 1:
                wait_s = 1.0 * (attempt + 1)
                logger.debug(f"DuckDB write lock (attempt {attempt + 1}/{max_retries}), retrying in {wait_s}s...")
                _time.sleep(wait_s)
                continue
            logger.warning(f"DuckDB write failed after {attempt + 1} attempts: {e}")
            try:
                import os as _os
                _os.unlink(tmp_path)
            except OSError:
                pass
            return False

    return False

def load_queue_times_mapping(config_dir: Path) -> Optional[pd.DataFrame]:
    """
    Load the master mapping table that connects entity_code to queue_times_id.
    This is the authoritative source for ID-based mapping.
    """
    mapping_path = config_dir / "queue_times_entity_mapping.csv"
    if mapping_path.exists():
        try:
            df = pd.read_csv(mapping_path)
            # Ensure queue_times_id is integer
            if "queue_times_id" in df.columns:
                df["queue_times_id"] = pd.to_numeric(df["queue_times_id"], errors="coerce")
            return df
        except Exception as e:
            logging.warning(f"Could not load queue-times mapping: {e}")
    return None


# Scraping window: 90 minutes before earliest open (including EMH) to 90 minutes after close.
SCRAPE_WINDOW_BEFORE_OPEN_MIN = 90
SCRAPE_WINDOW_AFTER_CLOSE_MIN = 90


def load_dimparkhours(output_base: Path) -> Optional[pd.DataFrame]:
    """
    Load dimparkhours from dimension_tables/dimparkhours.csv.
    Used to determine if a park is within its scraping window (open-90 to close+90 in park TZ).
    Returns None if missing or on error.
    """
    path = output_base / "dimension_tables" / "dimparkhours.csv"
    if not path.exists():
        return None
    try:
        return pd.read_csv(path, low_memory=False)
    except Exception as e:
        logging.warning(f"Could not load dimparkhours: {e}")
        return None


def _first_column(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    """Return the first column name in candidates that exists in df, or None."""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _parse_time_to_minutes(s) -> Optional[int]:
    """Parse a time string (HH:MM, H:MM, HH:MM:SS) to minutes since midnight. Returns None on failure."""
    if pd.isna(s) or s is None or (isinstance(s, str) and not str(s).strip()):
        return None
    s = str(s).strip()
    parts = re.split(r"[\s:]+", s, 3)
    if len(parts) < 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, TypeError):
        pass
    return None


def _parse_time_to_minutes_local(s, park_tz: ZoneInfo) -> Optional[int]:
    """
    Parse time from dimparkhours: supports HH:MM or full timestamp (EST).
    Full timestamps are converted to park local time before extracting.
    """
    simple = _parse_time_to_minutes(s)
    if simple is not None:
        return simple
    try:
        ts = pd.to_datetime(s)
        if pd.isna(ts):
            return None
        est = ZoneInfo("America/New_York")
        if ts.tzinfo is None:
            ts = ts.tz_localize(est)
        local = ts.astimezone(park_tz)
        return int(local.hour * 60 + local.minute)
    except Exception:
        return None


def _get_park_date_local(now: datetime, tz: ZoneInfo) -> str:
    """Park operational date in park TZ using 6am rule: if hour < 6, use previous calendar date."""
    local = now.astimezone(tz)
    if local.hour < 6:
        d = (local.date() - timedelta(days=1))
    else:
        d = local.date()
    return d.strftime("%Y-%m-%d")


def get_in_window_park_ids(
    dimparkhours: pd.DataFrame,
    parks: List[dict],
    now: datetime,
    logger: logging.Logger,
) -> List[int]:
    """
    For each park in QUEUE_TIMES_PARK_MAP, determine if 'now' falls in the scraping window
    (earliest_open - 90 min, close + 90 min) in that park's timezone. Park date uses 6am rule.
    Returns list of queue-times.com park IDs that are in-window.
    """
    date_col = _first_column(dimparkhours, ["park_date", "date"])
    park_col = _first_column(dimparkhours, ["park", "park_code", "code"])
    open_col = _first_column(dimparkhours, ["opening_time", "open", "open_time", "open_time_1"])
    close_col = _first_column(dimparkhours, ["closing_time", "close", "close_time", "close_time_1"])
    emh_col = _first_column(dimparkhours, ["opening_time_with_emh", "emh_open", "early_entry", "early_entry_open", "extra_magic_hours", "emh"])

    if not date_col or not park_col or not open_col or not close_col:
        logger.warning(
            "dimparkhours missing required columns (need date, park, open, close). "
            "Treating all mapped parks as in-window."
        )
        return [p.get("id") for p in parks if p.get("id") in QUEUE_TIMES_PARK_MAP]

    # Normalize date to YYYY-MM-DD
    dimparkhours = dimparkhours.copy()
    dimparkhours["_pd_norm"] = pd.to_datetime(dimparkhours[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
    dimparkhours["_park_norm"] = dimparkhours[park_col].astype(str).str.strip().str.lower()

    in_window: List[int] = []
    for park in parks:
        park_id = park.get("id")
        if park_id not in QUEUE_TIMES_PARK_MAP:
            continue
        park_code = QUEUE_TIMES_PARK_MAP[park_id]
        tz_str = park.get("timezone", "UTC")
        try:
            tz = ZoneInfo(tz_str)
        except Exception:
            tz = ZoneInfo("UTC")
        park_date = _get_park_date_local(now, tz)

        rows = dimparkhours[
            (dimparkhours["_pd_norm"] == park_date) &
            (dimparkhours["_park_norm"] == park_code.lower())
        ]
        if rows.empty:
            logger.debug(f"dimparkhours: no row for park={park_code} date={park_date}; treating as in-window")
            in_window.append(park_id)
            continue

        # Aggregate if multiple rows: earliest open, latest close; earliest EMH if present
        # Use _parse_time_to_minutes_local for EST timestamps (fixes Tokyo TDL/TDS)
        open_min = None
        close_min = None
        emh_min = None
        for _, r in rows.iterrows():
            o = _parse_time_to_minutes_local(r.get(open_col), tz)
            c = _parse_time_to_minutes_local(r.get(close_col), tz)
            if o is not None:
                open_min = o if open_min is None else min(open_min, o)
            if c is not None:
                close_min = c if close_min is None else max(close_min, c)
            if emh_col and r.get(emh_col) is not None:
                e = _parse_time_to_minutes_local(r.get(emh_col), tz)
                if e is not None:
                    emh_min = e if emh_min is None else min(emh_min, e)

        if open_min is None or close_min is None:
            logger.debug(f"dimparkhours: could not parse open/close for {park_code} {park_date}; treating as in-window")
            in_window.append(park_id)
            continue

        earliest_open = min(open_min, emh_min) if emh_min is not None else open_min
        window_start_min = earliest_open - SCRAPE_WINDOW_BEFORE_OPEN_MIN
        window_end_min = close_min + SCRAPE_WINDOW_AFTER_CLOSE_MIN

        park_date_obj = date.fromisoformat(park_date)
        base = datetime(park_date_obj.year, park_date_obj.month, park_date_obj.day, 0, 0, 0, tzinfo=tz)
        window_start_dt = base + timedelta(minutes=window_start_min)
        window_end_dt = base + timedelta(minutes=window_end_min)
        now_park = now.astimezone(tz)
        in_range = window_start_dt <= now_park <= window_end_dt

        if in_range:
            in_window.append(park_id)
        else:
            logger.debug(
                f"Out of window: {park_code} (id={park_id}) "
                f"park_date={park_date} window {window_start_dt.strftime('%H:%M')}-{window_end_dt.strftime('%H:%M')} (local) "
                f"now={now_park.strftime('%H:%M')}"
            )

    return in_window


def load_entity_table(output_base: Path) -> Optional[pd.DataFrame]:
    """
    Load entity table if available to help map ride names to entity codes.
    Filters out priority queue entities (starting with "Get Lightning Lane") 
    since queue-times.com only provides standby wait times.
    """
    entity_path = output_base / "dimension_tables" / "dimentity.csv"
    if entity_path.exists():
        try:
            df = pd.read_csv(entity_path)
            
            # Add park column if it doesn't exist (derive from code prefix)
            if "park" not in df.columns:
                import re
                df["park"] = df["code"].apply(
                    lambda ec: next(
                        (code for prefix, code in PARK_CODE_MAP.items() 
                         if str(ec).upper().startswith(prefix)), 
                        ""
                    )
                )
            
            # Filter out priority queue entities - queue-times.com only has standby
            if "name" in df.columns:
                df = df[~df["name"].str.startswith("Get Lightning Lane", na=False)]
            
            return df
        except Exception as e:
            logging.warning(f"Could not load entity table: {e}")
    return None


# =============================================================================
# API FETCHING
# =============================================================================

def fetch_with_retry(
    url: str,
    logger: logging.Logger,
    max_retries: int = REQUEST_RETRIES,
    proxies: Optional[dict] = None,
) -> Optional[dict]:
    """Fetch JSON from URL with retries. Use proxies={\"http\": None, \"https\": None} to bypass env proxy."""
    for attempt in range(max_retries):
        try:
            kwargs: dict = {"timeout": REQUEST_TIMEOUT}
            if proxies is not None:
                kwargs["proxies"] = proxies
            response = requests.get(url, **kwargs)
            response.raise_for_status()
            return response.json()
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON from {url}: {e}")
            return None
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = REQUEST_RETRY_DELAY * (attempt + 1)
                logger.warning(f"Request failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                logger.error(f"Request failed after {max_retries} attempts: {e}")
                return None
    return None


def fetch_parks(logger: logging.Logger, proxies: Optional[dict] = None) -> Optional[List[dict]]:
    """Fetch park list from queue-times.com."""
    logger.info(f"Fetching parks from {PARKS_ENDPOINT}")
    data = fetch_with_retry(PARKS_ENDPOINT, logger, proxies=proxies)
    if data is None:
        return None
    
    # Flatten the structure: data is a list of park groups, each with a "parks" list
    all_parks = []
    for group in data:
        if "parks" in group:
            all_parks.extend(group["parks"])
        else:
            # Some entries might be parks directly
            all_parks.append(group)
    
    logger.info(f"Found {len(all_parks)} parks")
    return all_parks


def fetch_park_wait_times(park_id: int, logger: logging.Logger, proxies: Optional[dict] = None) -> Optional[dict]:
    """Fetch wait times for a specific park."""
    url = QUEUE_TIMES_ENDPOINT_FMT.format(park_id=park_id)
    logger.debug(f"Fetching wait times for park {park_id} from {url}")
    return fetch_with_retry(url, logger, proxies=proxies)


# =============================================================================
# DATA TRANSFORMATION
# =============================================================================

def map_ride_to_entity_code(
    ride_id: int, 
    ride_name: str, 
    park_code: str, 
    queue_times_mapping: Optional[pd.DataFrame]
) -> str:
    """
    Map a queue-times.com ride ID to an entity code using the master mapping table.
    
    Strategy:
    1. Look up ride_id in the master mapping table (ID-based, most reliable)
    2. If not found, generate a fallback code from park_code + ride_id
    
    Args:
        ride_id: Queue-Times.com ride ID
        ride_name: Queue-Times.com ride name (for logging/debugging)
        park_code: Park code (e.g., "mk", "ep")
        queue_times_mapping: Master mapping DataFrame with entity_code, park_code, queue_times_id
    
    Returns:
        Entity code (e.g., "MK01", "EP05")
    """
    if queue_times_mapping is not None:
        # Look up by queue_times_id and park_code
        # Convert ride_id to float for comparison (CSV stores as float)
        matches = queue_times_mapping[
            (queue_times_mapping["queue_times_id"].astype(float) == float(ride_id)) &
            (queue_times_mapping["park_code"].str.lower() == park_code.lower())
        ]
        if not matches.empty:
            # If multiple matches, take the first one (shouldn't happen, but handle it)
            entity_code = str(matches.iloc[0]["entity_code"]).upper()
            return entity_code
    
    # Fallback: generate entity code from park_code + ride_id
    # Format: {PARK_PREFIX}{ride_id}
    park_prefix = park_code.upper()
    # Reverse lookup park prefix from park code
    for prefix, code in PARK_CODE_MAP.items():
        if code.lower() == park_code.lower():
            park_prefix = prefix
            break
    
    return f"{park_prefix}{ride_id}"


def transform_queue_times_data(
    park_id: int,
    park_name: str,
    queue_times_data: dict,
    park_tz: ZoneInfo,
    queue_times_mapping: Optional[pd.DataFrame],
    logger: logging.Logger,
    fetch_time_utc: Optional[datetime] = None,
) -> pd.DataFrame:
    """
    Transform queue-times.com data to our format.
    
    observed_at is taken from each ride's last_updated (queue-times API). If
    fetch_time_utc is provided and observed_at is older than
    STALE_OBSERVED_AT_THRESHOLD_HOURS vs fetch_time_utc, a warning is logged
    (queue-times can return stale last_updated).
    
    Input format:
    {
        "lands": [
            {
                "id": int,
                "name": str,
                "rides": [
                    {
                        "id": int,
                        "name": str,
                        "is_open": bool,
                        "wait_time": int (minutes),
                        "last_updated": str (ISO 8601 UTC)
                    }
                ]
            }
        ],
        "rides": []  # Sometimes rides are at top level
    }
    
    Output format:
    - entity_code (str, uppercase)
    - observed_at (str, ISO with timezone)
    - wait_time_type ("POSTED")
    - wait_time_minutes (int)
    """
    rows = []
    
    # Determine park code from park_id or name
    park_code = QUEUE_TIMES_PARK_MAP.get(park_id)
    if park_code is None:
        # Try to derive from park name or use a default
        # This is a placeholder - may need better mapping
        logger.warning(f"No park code mapping for park_id={park_id}, name={park_name}. Skipping.")
        return pd.DataFrame(columns=["entity_code", "observed_at", "wait_time_type", "wait_time_minutes"])
    
    # Collect all rides from lands and top-level
    all_rides = []
    stale_examples: List[Tuple[str, str, float, int]] = []  # (entity_code, observed_at, age_hours, wait_min)
    
    # Rides in lands
    if "lands" in queue_times_data:
        for land in queue_times_data["lands"]:
            if "rides" in land:
                all_rides.extend(land["rides"])
    
    # Top-level rides
    if "rides" in queue_times_data:
        all_rides.extend(queue_times_data["rides"])
    
    # Transform each ride
    for ride in all_rides:
        if not ride.get("is_open", False):
            continue  # Skip closed rides
        
        ride_id = ride.get("id")
        ride_name = ride.get("name", "")
        wait_time = ride.get("wait_time")
        last_updated = ride.get("last_updated")
        
        if wait_time is None or last_updated is None:
            continue
        
        # Map ride to entity code using ID-based mapping
        entity_code = map_ride_to_entity_code(ride_id, ride_name, park_code, queue_times_mapping)
        
        # Parse and convert timestamp to park timezone.
        # observed_at comes from queue-times last_updated; we use it as-is. The API can return
        # stale values (observed_at much older than fetch time). See docs/STALE_OBSERVED_AT.md.
        try:
            observed_at_utc = pd.to_datetime(last_updated, utc=True)
            observed_at_local = observed_at_utc.tz_convert(park_tz)
            observed_at_str = observed_at_local.isoformat()
        except Exception as e:
            logger.warning(f"Could not parse timestamp {last_updated} for ride {ride_name}: {e}")
            continue
        
        # Audit: log when observed_at is much older than fetch time (API returning stale last_updated)
        if fetch_time_utc is not None:
            fetch_ts = pd.Timestamp(fetch_time_utc)
            age_hours = (fetch_ts - observed_at_utc).total_seconds() / 3600
            if age_hours > STALE_OBSERVED_AT_THRESHOLD_HOURS:
                stale_examples.append((entity_code, observed_at_str, age_hours, int(wait_time)))
        
        rows.append({
            "entity_code": entity_code,
            "observed_at": observed_at_str,
            "wait_time_type": "POSTED",
            "wait_time_minutes": int(wait_time),
        })
    
    if stale_examples:
        samples = stale_examples[:3]  # log up to 3 for audit
        sample_str = "; ".join(
            f"entity={s[0]} observed_at={s[1]} age_h={s[2]:.1f} min={s[3]}"
            for s in samples
        )
        logger.warning(
            f"Stale observed_at: {len(stale_examples)} rows had last_updated > {STALE_OBSERVED_AT_THRESHOLD_HOURS}h "
            f"older than fetch time (queue-times API can return stale timestamps). "
            f"Samples: {sample_str}. See docs/STALE_OBSERVED_AT.md."
        )
    
    if not rows:
        return pd.DataFrame(columns=["entity_code", "observed_at", "wait_time_type", "wait_time_minutes"])
    
    df = pd.DataFrame(rows)
    logger.info(f"Transformed {len(df)} wait time records for park {park_name} (id={park_id})")
    return df


# =============================================================================
# MAIN PROCESSING
# =============================================================================

def process_queue_times(
    output_base: Path,
    park_ids: Optional[List[int]] = None,
    use_hours_filter: bool = True,
    logger: logging.Logger = None,
) -> int:
    """
    Main processing function: fetch and process queue-times.com data.
    
    Always bypasses HTTP/HTTPS proxy (proxies={}) so the fetcher runs reliably in
    Cursor, scheduled tasks, and environments with broken or missing proxy config.
    
    Args:
        output_base: Base output directory
        park_ids: Optional list of park IDs to process. If None, processes all parks.
        use_hours_filter: If True, load dimparkhours and only scrape parks in-window (open-90 to close+90).
        logger: Logger instance
    
    Returns:
        Total number of rows written
    """
    proxies = {}  # always bypass env proxy so we can run everywhere (Cursor, tasks, etc.)
    if logger is None:
        logger = setup_logging(output_base / "logs")
    
    # Setup directories
    dirs = get_output_directories(output_base)
    
    # Acquire lock
    lock_file = dirs["state"] / LOCK_FILE_NAME
    if not acquire_lock(lock_file, logger):
        logger.error("Could not acquire lock. Exiting.")
        return 0
    
    try:
        # Setup deduplication database
        dedupe_db = dirs["state"] / DEDUPE_DB_NAME
        conn = sqlite3.connect(str(dedupe_db))
        ensure_sqlite(conn)
        
        # Load queue-times mapping table (ID-based mapping)
        config_dir = Path(__file__).parent.parent / "config"
        queue_times_mapping = load_queue_times_mapping(config_dir)
        if queue_times_mapping is not None:
            logger.info(f"Loaded queue-times mapping with {len(queue_times_mapping)} mappings")
        else:
            logger.warning("No queue-times mapping table found. Will use fallback ID generation.")
        
        # Fetch parks
        parks = fetch_parks(logger, proxies=proxies)
        if parks is None:
            logger.error("Failed to fetch parks list")
            return 0
        
        # Filter to parks in scraping window (open-90 to close+90 in park TZ) using dimparkhours
        if use_hours_filter:
            dimph = load_dimparkhours(output_base)
            if dimph is not None:
                now = datetime.now(ZoneInfo("UTC"))
                in_window = get_in_window_park_ids(dimph, parks, now, logger)
                if not in_window:
                    logger.info("No parks in scraping window (open-90 to close+90); exiting without API calls.")
                    return 0
                parks = [p for p in parks if p.get("id") in in_window]
                logger.info(f"Hours filter: {len(in_window)} parks in-window, processing those")
            else:
                logger.info("dimparkhours not found; processing all mapped parks (no hours filter)")
        
        # Filter to requested park IDs if specified
        if park_ids is not None:
            parks = [p for p in parks if p.get("id") in park_ids]
            logger.info(f"Filtered to {len(parks)} requested parks")
        
        # Process each park (wrap in try/except so one bad park doesn't kill the run)
        total_rows = 0
        for park in parks:
            try:
                park_id = park.get("id")
                park_name = park.get("name", f"Park {park_id}")
                
                if park_id is None:
                    logger.warning(f"Skipping park without ID: {park_name}")
                    continue
                
                # Check if we have a mapping for this park
                if park_id not in QUEUE_TIMES_PARK_MAP:
                    logger.debug(f"Skipping park {park_name} (id={park_id}) - no park code mapping")
                    continue
                
                park_code = QUEUE_TIMES_PARK_MAP[park_id]
                park_tz_str = park.get("timezone", "UTC")
                
                try:
                    park_tz = ZoneInfo(park_tz_str)
                except Exception as e:
                    logger.warning(f"Invalid timezone {park_tz_str} for park {park_name}: {e}. Using UTC.")
                    park_tz = ZoneInfo("UTC")
                
                # Fetch wait times
                queue_times_data = fetch_park_wait_times(park_id, logger, proxies=proxies)
                if queue_times_data is None:
                    logger.warning(f"Failed to fetch wait times for park {park_name} (id={park_id})")
                    continue
                fetch_time_utc = datetime.now(ZoneInfo("UTC"))
                
                # Transform data (pass fetch_time_utc to audit stale observed_at from queue-times last_updated)
                df = transform_queue_times_data(
                    park_id, park_name, queue_times_data, park_tz, queue_times_mapping, logger,
                    fetch_time_utc=fetch_time_utc,
                )
                
                if df.empty:
                    logger.info(f"No wait time data for park {park_name} (id={park_id})")
                    continue
                
                # Deduplicate
                new_mask = insert_new_mask(conn, df)
                new_df = df[new_mask]
                
                if new_df.empty:
                    logger.info(f"All {len(df)} rows for park {park_name} were duplicates")
                    continue
                
                logger.info(f"Processing {len(new_df)} new rows for park {park_name} (id={park_id})")
                
                # Write to staging (morning ETL merges yesterday's staging into fact_tables)
                rows_written = write_grouped_csvs(new_df, dirs["staging_queue_times"], park_tz, logger)
                total_rows += rows_written
                # Dual-write to DuckDB for bot + dashboard (if init_live_duckdb has been run)
                if rows_written > 0:
                    _write_to_live_duckdb(new_df, park_tz, output_base, logger)
            except Exception as e:
                logger.exception(f"Error processing park {park.get('name', park.get('id', '?'))} (id={park.get('id')}): {e}")
        
        logger.info(f"Total rows written: {total_rows}")
        return total_rows
    
    finally:
        release_lock(lock_file, logger)
        if 'conn' in locals():
            conn.close()


# =============================================================================
# COMMAND-LINE INTERFACE
# =============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fetch wait times from queue-times.com API"
    )
    ap.add_argument(
        "--output-base",
        type=Path,
        default=get_output_base(),
        help="Output base directory (from config/config.json or default)",
    )
    ap.add_argument(
        "--park-ids",
        type=str,
        help="Comma-separated list of park IDs to process (e.g., '2,3,4'). If not specified, processes all mapped parks.",
    )
    ap.add_argument(
        "--interval",
        type=int,
        default=0,
        metavar="SECS",
        help="Run continuously: fetch, write to staging/queue_times, then sleep SECS and repeat. Default 0 = one-shot. e.g. --interval 300 for every 5 minutes.",
    )
    ap.add_argument(
        "--no-hours-filter",
        action="store_true",
        help="Do not filter by park hours; scrape all mapped parks. By default uses dimparkhours to only scrape when open-90 to close+90 in park TZ.",
    )
    args = ap.parse_args()

    output_base = args.output_base.resolve()

    # Setup logging
    dirs = get_output_directories(output_base)
    logger = setup_logging(dirs["logs"])

    logger.info("=" * 60)
    logger.info("Queue-Times.com Wait Time Fetcher")
    logger.info("=" * 60)
    logger.info(f"Output base: {output_base}")

    # Parse park IDs if provided
    park_ids = None
    if args.park_ids:
        try:
            park_ids = [int(x.strip()) for x in args.park_ids.split(",")]
            logger.info(f"Processing specific parks: {park_ids}")
        except ValueError as e:
            logger.error(f"Invalid park IDs format: {e}")
            sys.exit(1)

    if args.interval and args.interval < 1:
        logger.error("--interval must be >= 1 (seconds)")
        sys.exit(1)

    use_hours_filter = not args.no_hours_filter

    def run_once() -> int:
        return process_queue_times(
            output_base, park_ids,
            use_hours_filter=use_hours_filter,
            logger=logger,
        )

    if args.interval:
        logger.info(f"Continuous mode: interval={args.interval}s. Ctrl+C to stop.")
        run = 0
        try:
            while True:
                run += 1
                logger.info(f"--- Run #{run} ---")
                
                # ops #35 - Check for backup lock file before any database operations
                if os.path.exists('/tmp/b2_backup.lock'):
                    logger.info("Backup in progress, skipping cycle")
                    logger.info(f"Sleeping {args.interval}s...")
                    time.sleep(args.interval)
                    continue
                
                try:
                    total_rows = run_once()
                    if total_rows > 0:
                        logger.info(f"Wrote {total_rows} rows to staging/queue_times")
                    else:
                        logger.info("No new rows this run")
                except Exception as e:
                    logger.exception(f"Run #{run} failed: {e}")
                logger.info(f"Sleeping {args.interval}s...")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("Stopped by user (Ctrl+C)")
    else:
        total_rows = run_once()
        if total_rows > 0:
            logger.info(f"Successfully processed {total_rows} rows")
        else:
            logger.warning("No rows were processed")
        logger.info("Done.")


if __name__ == "__main__":
    main()
