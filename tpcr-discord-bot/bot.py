"""
Theme Park Crowd Report — Discord Bot (Hazey)

MVP: /crowd, /best-day, /today slash commands
Reads from pipeline forecast data on disk.
"""

import os
import sys
import logging
import discord
from discord import app_commands
from dotenv import load_dotenv

import json as _json
from datetime import datetime as _dt, timezone as _tz

def _log_bot_error(command: str, error_type: str, detail: str, resolution: str):
    """Append error to bot_errors.jsonl for monitoring."""
    try:
        entry = {
            "timestamp": _dt.now(_tz.utc).isoformat(),
            "command": command,
            "error_type": error_type,
            "error_detail": detail[:500],
            "resolution": resolution,
        }
        log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "bot_errors.jsonl"), "a") as f:
            f.write(_json.dumps(entry) + "\n")
    except Exception:
        pass  # Never let logging break the bot

import duckdb
from datetime import datetime, date as date_type, timedelta
from dateutil import parser
import pandas as pd
import re
import io
from pathlib import Path
from forecast_image import generate_forecast_image

logger = logging.getLogger("tpcr-bot")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
from calendar_image import generate_calendar_image
# Load ~/.env BEFORE importing ask_agent so its QWEN_* settings see it
# (ask_agent also reads them at call time; this is defense in depth).
load_dotenv(os.path.expanduser("~/.env"))
from ask_agent import ask_agent, data_path, _duckdb_path
data_path()

# Add theme-park-crowd-report/src to sys.path for live inference model
theme_park_src = os.path.expanduser("~/theme-park-crowd-report/src")
if theme_park_src not in sys.path:
    sys.path.insert(0, theme_park_src)

try:
    from processors.live_inference import LiveInferenceModel
    LIVE_INFERENCE_AVAILABLE = True
except ImportError as e:
    print(f"⚠️ Live inference model not available: {e}")
    LiveInferenceModel = None
    LIVE_INFERENCE_AVAILABLE = False

# ~/.env was loaded above, before the ask_agent import
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Data paths
# --- Data paths ---
# Primary: shared DuckDB (populated by scraper + daily pipeline)
DUCKDB_PATH = _duckdb_path()

# Fallback: direct file paths (used only if DuckDB is unavailable)
WTI_PATH = data_path("wti/wti.parquet")
FORECASTS_PATH = data_path("curves/forecast_parquet/all_forecasts.parquet")
ENTITIES_PATH = data_path("dimension_tables/dimentity.csv")
STAGING_DIR = data_path("staging/queue_times")
FACT_PARQUET_DIR = data_path("fact_tables/parquet")

# DuckDB availability flag
_USE_DUCKDB = os.path.exists(DUCKDB_PATH)


def _parquet_df(query: str):
    """Run a parquet-fallback query on a dedicated in-memory connection.

    The module-level duckdb.sql() uses the shared default connection, which
    DuckDB 2.x refuses to reuse while an earlier execute() result is still
    unfinished (ResourceInUseException). A short-lived connection avoids that.
    """
    con = duckdb.connect()
    try:
        return con.execute(query).fetchdf()
    finally:
        con.close()


def get_db(max_retries: int = 5):
    """Get a read-only DuckDB connection to the shared database (retries on lock)."""
    import time as _time
    for attempt in range(max_retries):
        try:
            return duckdb.connect(DUCKDB_PATH, read_only=True)
        except Exception as e:
            if "lock" in str(e).lower() and attempt < max_retries - 1:
                logger.warning(f"⏳ DuckDB locked (attempt {attempt+1}/{max_retries}), retrying in {2*(attempt+1)}s...")
                _time.sleep(2 * (attempt + 1))
                continue
            raise


async def safe_defer(interaction: discord.Interaction) -> bool:
    """Defer an interaction, returning False if it's already expired/handled."""
    try:
        await interaction.response.defer()
        return True
    except discord.errors.NotFound:
        logger.warning(f"⚠️ Interaction already expired (likely duplicate from restart)")
        return False


# Global entity name mapping - loaded at startup
entity_names = {}

# Global live inference model - loaded at startup
live_inference_model = None

# Park name mapping — accept everything a human might type
PARK_NAMES = {
    # --- Walt Disney World ---
    # Magic Kingdom
    "magic kingdom": ("MK", "Magic Kingdom"),
    "magic-kingdom": ("MK", "Magic Kingdom"),
    "magickingdom": ("MK", "Magic Kingdom"),
    "mk": ("MK", "Magic Kingdom"),
    # EPCOT
    "epcot": ("EP", "EPCOT"),
    "ep": ("EP", "EPCOT"),
    # Hollywood Studios
    "hollywood studios": ("HS", "Hollywood Studios"),
    "hollywood-studios": ("HS", "Hollywood Studios"),
    "hollywoodstudios": ("HS", "Hollywood Studios"),
    "hollywood": ("HS", "Hollywood Studios"),
    "hs": ("HS", "Hollywood Studios"),
    # Animal Kingdom
    "animal kingdom": ("AK", "Animal Kingdom"),
    "animal-kingdom": ("AK", "Animal Kingdom"),
    "animalkingdom": ("AK", "Animal Kingdom"),
    "ak": ("AK", "Animal Kingdom"),
    # --- Disneyland Resort ---
    # Disneyland
    "disneyland": ("DL", "Disneyland"),
    "dl": ("DL", "Disneyland"),
    # California Adventure
    "california adventure": ("CA", "California Adventure"),
    "california-adventure": ("CA", "California Adventure"),
    "californiaadventure": ("CA", "California Adventure"),
    "dca": ("CA", "California Adventure"),
    "ca": ("CA", "California Adventure"),
    # --- Universal Orlando ---
    # Universal Studios Florida
    "universal studios florida": ("UF", "Universal Studios Florida"),
    "universal studios": ("UF", "Universal Studios Florida"),
    "universal florida": ("UF", "Universal Studios Florida"),
    "usf": ("UF", "Universal Studios Florida"),
    "uf": ("UF", "Universal Studios Florida"),
    # Islands of Adventure
    "islands of adventure": ("IA", "Islands of Adventure"),
    "islands-of-adventure": ("IA", "Islands of Adventure"),
    "ioa": ("IA", "Islands of Adventure"),
    "ia": ("IA", "Islands of Adventure"),
    # Epic Universe
    "epic universe": ("EU", "Epic Universe"),
    "epic": ("EU", "Epic Universe"),
    "eu": ("EU", "Epic Universe"),
    # --- Universal Hollywood ---
    "universal hollywood": ("UH", "Universal Studios Hollywood"),
    "universal studios hollywood": ("UH", "Universal Studios Hollywood"),
    "ush": ("UH", "Universal Studios Hollywood"),
    "uh": ("UH", "Universal Studios Hollywood"),
    # --- Tokyo Disney Resort ---
    # Tokyo Disneyland (WTI uses combined "TD" but entities are TDL*)
    "tokyo disneyland": ("TDL", "Tokyo Disneyland"),
    "tokyo-disneyland": ("TDL", "Tokyo Disneyland"),
    "tdl": ("TDL", "Tokyo Disneyland"),
    # Tokyo DisneySea (WTI uses combined "TD" but entities are TDS*)
    "tokyo disneysea": ("TDS", "Tokyo DisneySea"),
    "tokyo-disneysea": ("TDS", "Tokyo DisneySea"),
    "disneysea": ("TDS", "Tokyo DisneySea"),
    "tds": ("TDS", "Tokyo DisneySea"),
    # Combined Tokyo shortcuts → default to Disneyland
    "tokyo disney": ("TDL", "Tokyo Disneyland"),
    "tokyo disney resort": ("TDL", "Tokyo Disneyland"),
    "tdr": ("TDL", "Tokyo Disneyland"),
    "td": ("TDL", "Tokyo Disneyland"),
}

# WTI code mapping — some parks use different codes for WTI vs entities
# (TDL and TDS now have their own WTI scores)
WTI_CODE_MAP = {
    # No mappings needed currently — all park codes match WTI directly
}

# Entity prefix mapping — some parks have multiple entity prefixes
# UH has both UH* and USH* entities
ENTITY_PREFIXES = {
    "UH": ["UH", "USH"],  # Universal Hollywood uses both UH and USH prefixes
}

# WTI color thresholds — median-anchored, data-driven
# p1=8, median=22, p99=50
# Blue = rare good days, White = normal Disney, Pink = plan carefully
def wti_emoji(wti: float) -> str:
    """Benedictus-aligned emoji — median-anchored"""
    if wti <= 12:
        return "❄️"   # Shortest waits
    elif wti <= 18:
        return "💎"   # Below average — great day
    elif wti <= 25:
        return "⚪"   # Normal Disney — typical crowds
    elif wti <= 34:
        return "🌸"   # Above average — plan ahead
    elif wti <= 42:
        return "🔥"   # Busy — rope drop essential
    elif wti <= 50:
        return "🔴"   # Very busy — consider another day
    else:
        return "💀"   # Extreme — avoid at all costs

def _priority_queue_name(park_code: str = "") -> str:
    """Return the park-appropriate priority queue product name."""
    if park_code in ("UF", "IA", "EU", "UH"):
        return "Universal Express"
    elif park_code in ("TDL", "TDS"):
        return "Priority Pass"
    else:
        return "Lightning Lane"

def wti_label(wti: float, park_code: str = "") -> str:
    pq = _priority_queue_name(park_code)
    if wti <= 12:
        return "Well below avg WTI — best day to visit"
    elif wti <= 18:
        return "Below avg WTI — great day to visit"
    elif wti <= 25:
        return "Typical day — moderate waits, plan headliners"
    elif wti <= 34:
        return f"Above average — rope drop and {pq} recommended"
    elif wti <= 42:
        return "Well above average — arrive early, plan strategically"
    elif wti <= 50:
        return "Very high WTI — consider another day"
    else:
        return "Extreme WTI — plan carefully or reschedule"

def get_embed_color(wti: float) -> int:
    """Get embed color based on WTI score — median-anchored Benedictus"""
    if wti <= 12:
        return 0x0A2F8F  # Deep blue — shortest waits
    elif wti <= 18:
        return 0x3C78D2  # Blue — low wait times
    elif wti <= 25:
        return 0xD2C8DC  # Lavender — normal Disney
    elif wti <= 34:
        return 0xFFB1C9  # Light pink — above average
    elif wti <= 42:
        return 0xEB427B  # Hot pink — busy
    elif wti <= 50:
        return 0xA60038  # Deep red — very busy
    else:
        return 0x50001E  # Near-black crimson — extreme

def parse_date(date_str: str) -> date_type:
    """Parse date string using dateutil, handling common formats"""
    date_str = date_str.lower().strip()
    
    # Handle relative dates
    if date_str == "today":
        return date_type.today()
    elif date_str == "tomorrow":
        return date_type.today() + timedelta(days=1)
    elif date_str == "yesterday":
        return date_type.today() - timedelta(days=1)
    
    # Handle date formats like "feb-15", "2026-02-20"
    try:
        # If it's just month-day, assume current year
        if re.match(r'^[a-z]{3}-\d{1,2}$', date_str):
            current_year = date_type.today().year
            date_str = f"{current_year}-{date_str}"
        
        parsed = parser.parse(date_str, default=datetime.now())
        return parsed.date()
    except (ValueError, TypeError):
        raise ValueError(f"Could not parse date: {date_str}")

def load_entity_names():
    """Load entity names from DuckDB (with CSV fallback). Retries on lock."""
    import time as _time
    try:
        if _USE_DUCKDB:
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    con = get_db()
                    df = con.execute("""
                        SELECT entity_code, entity_name, short_name, is_extinct, has_posted
                        FROM entities
                    """).fetchdf()
                    con.close()
                    break
                except Exception as db_err:
                    if "lock" in str(db_err).lower() and attempt < max_retries - 1:
                        logger.warning(f"⏳ DuckDB locked loading entities (attempt {attempt+1}/{max_retries}), retrying...")
                        _time.sleep(3 * (attempt + 1))
                        continue
                    raise
            mapping = {}
            for _, row in df.iterrows():
                code = row['entity_code']
                name = row['entity_name'] if pd.notna(row['entity_name']) else code
                short = row['short_name'] if pd.notna(row['short_name']) else name
                extinct = bool(row['is_extinct']) if pd.notna(row['is_extinct']) else False
                has_posted = bool(row['has_posted']) if pd.notna(row['has_posted']) else False
                mapping[code] = (name, short, extinct, has_posted)
            logger.info(f"📋 Loaded {len(mapping)} entity names from DuckDB")
            return mapping

        # Fallback: CSV
        entities_df = pd.read_csv(ENTITIES_PATH)
        mapping = {}
        for _, row in entities_df.iterrows():
            code = row['code']
            name = row['name'] if pd.notna(row['name']) else code
            if 'display_name' in entities_df.columns and pd.notna(row.get('display_name')):
                short_name = row['display_name']
            elif pd.notna(row.get('short_name')):
                short_name = row['short_name']
            else:
                short_name = name
            is_extinct = pd.notna(row.get('extinct_on'))
            mapping[code] = (name, short_name, is_extinct)
        return mapping
    except Exception as e:
        logger.error(f"⚠️ Error loading entity names: {e}")
        return {}

def get_wti_score(park_code: str, target_date: date_type) -> float:
    """Get WTI score for park and date (DuckDB with parquet fallback)."""
    try:
        wti_code = WTI_CODE_MAP.get(park_code, park_code)

        if _USE_DUCKDB:
            try:
                con = get_db()
                result = con.execute("""
                    SELECT wti FROM wti
                    WHERE park_code = ? AND park_date = ?
                    LIMIT 1
                """, [wti_code, target_date]).fetchone()
                con.close()
                return float(result[0]) if result else None
            except Exception as e:
                if "lock" in str(e).lower():
                    logger.warning(f"⚠️ DuckDB locked for get_wti_score({park_code}), falling back to parquet")
                else:
                    raise

        # Fallback: parquet (also used when DuckDB is locked)
        query = f"""
            SELECT wti 
            FROM read_parquet('{WTI_PATH}')
            WHERE park_code = '{wti_code}' 
              AND park_date = '{target_date}'
            LIMIT 1
        """
        result = _parquet_df(query)
        if len(result) > 0:
            return float(result.iloc[0]['wti'])
        return None
    except Exception as e:
        print(f"⚠️ Error getting WTI score: {e}")
        return None

def _entity_filter_sql(park_code: str) -> str:
    """Build SQL WHERE clause for entity_code matching, handling multi-prefix parks."""
    prefixes = ENTITY_PREFIXES.get(park_code, [park_code])
    if len(prefixes) == 1:
        return f"entity_code LIKE '{prefixes[0]}%'"
    conditions = " OR ".join(f"entity_code LIKE '{p}%'" for p in prefixes)
    return f"({conditions})"


def get_forecasts(park_code: str, target_date: date_type):
    """Get entity forecasts for park and date (DuckDB with parquet fallback)."""
    try:
        entity_filter = _entity_filter_sql(park_code)

        if _USE_DUCKDB:
            con = get_db()
            result = con.execute(f"""
                SELECT entity_code, 
                       AVG(predicted_actual) as avg_wait,
                       MAX(predicted_actual) as peak_wait
                FROM forecasts
                WHERE {entity_filter}
                  AND park_date = ?
                  AND CAST(time_slot AS VARCHAR) BETWEEN '08:00' AND '22:00'
                GROUP BY entity_code
                ORDER BY avg_wait DESC
            """, [target_date]).fetchdf()
            con.close()
            return result

        # Fallback: parquet
        query = f"""
            SELECT entity_code, 
                   AVG(predicted_actual) as avg_wait,
                   MAX(predicted_actual) as peak_wait
            FROM read_parquet('{FORECASTS_PATH}')
            WHERE {entity_filter}
              AND park_date = '{target_date}'
              AND time_slot BETWEEN '08:00' AND '22:00'
            GROUP BY entity_code
            ORDER BY avg_wait DESC
        """
        result = _parquet_df(query)
        return result
    except Exception as e:
        print(f"⚠️ Error getting forecasts: {e}")
        return pd.DataFrame()

def get_archived_forecasts(park_code: str, target_date: date_type):
    """Get forecast from archived forecast files (for today/past dates not in current forecast)."""
    try:
        archive_dir = data_path("accuracy/archive")
        if not os.path.exists(archive_dir):
            return pd.DataFrame()
        
        # Find archived forecasts made BEFORE the target date
        archive_files = sorted([
            os.path.join(archive_dir, f)
            for f in os.listdir(archive_dir)
            if f.startswith("forecast_") and f.endswith(".parquet")
            and f.replace("forecast_", "").replace(".parquet", "") <= str(target_date)
        ])
        
        if not archive_files:
            return pd.DataFrame()
        
        # Use the most recent archive that was made before/on the target date
        forecast_archive = archive_files[-1]
        print(f"📂 Using archived forecast: {os.path.basename(forecast_archive)} for {target_date}")
        
        entity_filter = _entity_filter_sql(park_code)
        query = f"""
            SELECT entity_code,
                   AVG(predicted_actual) as avg_wait,
                   MAX(predicted_actual) as peak_wait
            FROM read_parquet('{forecast_archive}')
            WHERE {entity_filter}
              AND park_date = '{target_date}'
              AND time_slot BETWEEN '08:00' AND '22:00'
            GROUP BY entity_code
            ORDER BY avg_wait DESC
        """
        result = _parquet_df(query)
        return result
    except Exception as e:
        print(f"⚠️ Error getting archived forecasts: {e}")
        return pd.DataFrame()


def get_live_waits(park_code: str, target_date: date_type):
    """Get actual posted wait times for today/past dates (DuckDB with file fallback)."""
    try:
        entity_filter = _entity_filter_sql(park_code)

        if _USE_DUCKDB:
            con = get_db()
            result = con.execute(f"""
                SELECT entity_code,
                       ROUND(AVG(wait_time_minutes), 0) as avg_wait,
                       MAX(wait_time_minutes) as peak_wait,
                       MAX(observed_at) as latest_obs
                FROM live_waits
                WHERE {entity_filter}
                  AND park_date = ?
                  AND wait_time_type = 'POSTED'
                  AND wait_time_minutes > 0
                GROUP BY entity_code
                ORDER BY avg_wait DESC
            """, [target_date]).fetchdf()
            con.close()
            return result

        # Fallback: CSV/parquet file scanning
        month_str = target_date.strftime("%Y-%m")
        staging_month = os.path.join(STAGING_DIR, month_str)
        sources = []
        if os.path.exists(staging_month):
            date_str = target_date.strftime("%Y-%m-%d")
            staging_files = [
                os.path.join(staging_month, f)
                for f in os.listdir(staging_month)
                if date_str in f and f.endswith(".csv")
            ]
            if staging_files:
                sources.extend(staging_files)
        if not sources:
            parquet_file = os.path.join(FACT_PARQUET_DIR, f"{month_str}.parquet")
            if os.path.exists(parquet_file):
                sources.append(parquet_file)
            else:
                return pd.DataFrame()
        csv_sources = [s for s in sources if s.endswith(".csv")]
        pq_sources = [s for s in sources if s.endswith(".parquet")]
        parts = []
        if csv_sources:
            file_list = "', '".join(csv_sources)
            df = duckdb.sql(f"""
                SELECT entity_code, ROUND(AVG(wait_time_minutes), 0) as avg_wait,
                       MAX(wait_time_minutes) as peak_wait, MAX(observed_at) as latest_obs
                FROM read_csv(['{file_list}'], AUTO_DETECT=TRUE)
                WHERE {entity_filter} AND wait_time_type = 'POSTED' AND wait_time_minutes > 0
                GROUP BY entity_code ORDER BY avg_wait DESC
            """).fetchdf()
            parts.append(df)
        if pq_sources:
            file_list = "', '".join(pq_sources)
            df = duckdb.sql(f"""
                SELECT entity_code, ROUND(AVG(wait_time_minutes), 0) as avg_wait,
                       MAX(wait_time_minutes) as peak_wait, MAX(observed_at_ts)::VARCHAR as latest_obs
                FROM read_parquet(['{file_list}'])
                WHERE {entity_filter} AND wait_time_type = 'POSTED' AND park_date = '{target_date}' AND wait_time_minutes > 0
                GROUP BY entity_code ORDER BY avg_wait DESC
            """).fetchdf()
            parts.append(df)
        if parts:
            result = pd.concat(parts, ignore_index=True)
            result = result.sort_values("latest_obs", ascending=False).drop_duplicates("entity_code")
            return result.sort_values("avg_wait", ascending=False)
        return pd.DataFrame()
    except Exception as e:
        print(f"⚠️ Error getting live waits: {e}")
        return pd.DataFrame()


def get_current_waits(park_code: str, target_date: date_type):
    """Get most recent posted wait times (not daily averages) for live display.
    DuckDB primary, CSV/parquet fallback. Falls back to CSV if DuckDB is locked."""
    try:
        entity_filter = _entity_filter_sql(park_code)

        if _USE_DUCKDB:
            try:
                con = get_db()
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
                    SELECT w.entity_code,
                           w.wait_time_minutes as current_wait,
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
                if "lock" in str(e).lower():
                    logger.warning(f"⚠️ DuckDB locked for get_current_waits({park_code}), falling back to CSV")
                else:
                    raise

        # Fallback: CSV/parquet file scanning (also used when DuckDB is locked)
        month_str = target_date.strftime("%Y-%m")
        staging_month = os.path.join(STAGING_DIR, month_str)
        sources = []
        if os.path.exists(staging_month):
            date_str = target_date.strftime("%Y-%m-%d")
            staging_files = [
                os.path.join(staging_month, f)
                for f in os.listdir(staging_month)
                if date_str in f and f.endswith(".csv")
            ]
            if staging_files:
                sources.extend(staging_files)
        if not sources:
            parquet_file = os.path.join(FACT_PARQUET_DIR, f"{month_str}.parquet")
            if os.path.exists(parquet_file):
                sources.append(parquet_file)
            else:
                return pd.DataFrame()
        csv_sources = [s for s in sources if s.endswith(".csv")]
        pq_sources = [s for s in sources if s.endswith(".parquet")]
        parts = []
        if csv_sources:
            file_list = "', '".join(csv_sources)
            df = duckdb.sql(f"""
                WITH latest_obs AS (
                    SELECT entity_code, MAX(observed_at) as max_observed_at
                    FROM read_csv(['{file_list}'], AUTO_DETECT=TRUE)
                    WHERE {entity_filter} AND wait_time_type = 'POSTED' AND wait_time_minutes > 0
                    GROUP BY entity_code
                )
                SELECT w.entity_code, w.wait_time_minutes as current_wait, w.observed_at as latest_obs
                FROM read_csv(['{file_list}'], AUTO_DETECT=TRUE) w
                INNER JOIN latest_obs l ON w.entity_code = l.entity_code AND w.observed_at = l.max_observed_at
                WHERE w.{entity_filter} AND w.wait_time_type = 'POSTED' AND w.wait_time_minutes > 0
                ORDER BY w.wait_time_minutes DESC
            """).fetchdf()
            parts.append(df)
        if pq_sources:
            file_list = "', '".join(pq_sources)
            df = duckdb.sql(f"""
                WITH latest_obs AS (
                    SELECT entity_code, MAX(observed_at_ts) as max_observed_at_ts
                    FROM read_parquet(['{file_list}'])
                    WHERE {entity_filter} AND wait_time_type = 'POSTED' AND park_date = '{target_date}' AND wait_time_minutes > 0
                    GROUP BY entity_code
                )
                SELECT w.entity_code, w.wait_time_minutes as current_wait, w.observed_at_ts::VARCHAR as latest_obs
                FROM read_parquet(['{file_list}']) w
                INNER JOIN latest_obs l ON w.entity_code = l.entity_code AND w.observed_at_ts = l.max_observed_at_ts
                WHERE w.{entity_filter} AND w.wait_time_type = 'POSTED' AND w.park_date = '{target_date}' AND w.wait_time_minutes > 0
                ORDER BY w.wait_time_minutes DESC
            """).fetchdf()
            parts.append(df)
        if parts:
            result = pd.concat(parts, ignore_index=True)
            result = result.sort_values("latest_obs", ascending=False).drop_duplicates("entity_code")
            return result.sort_values("current_wait", ascending=False)
        return pd.DataFrame()
    except Exception as e:
        print(f"⚠️ Error getting current waits: {e}")
        return pd.DataFrame()


def get_wti_range(park_code: str, target_date: date_type) -> dict:
    """Get low/avg/high WTI for a park on a date.
    
    For forecast dates: uses the pre-computed WTI table (which has quantile mapping
    applied for proper distribution). Derives low/high from time-slot entity forecasts
    scaled to match the mapped WTI average.
    
    For historical dates: uses time-slot level forecasts directly.
    DuckDB primary, parquet fallback."""
    try:
        entity_filter = _entity_filter_sql(park_code)
        wti_code = WTI_CODE_MAP.get(park_code, park_code)

        # Get entity-level time-slot data for low/high range
        if _USE_DUCKDB:
            con = get_db()
            result = con.execute(f"""
                SELECT CAST(time_slot AS VARCHAR) as time_slot, AVG(predicted_actual) as slot_avg
                FROM forecasts
                WHERE {entity_filter}
                  AND park_date = ?
                  AND CAST(time_slot AS VARCHAR) BETWEEN '08:00' AND '22:00'
                GROUP BY time_slot
                ORDER BY time_slot
            """, [target_date]).fetchdf()
            
            # Get the quantile-mapped WTI value for this date
            wti_row = con.execute("""
                SELECT wti FROM wti
                WHERE park_code = ? AND park_date = ?
            """, [wti_code, target_date]).fetchone()
            con.close()
        else:
            query = f"""
                SELECT time_slot, AVG(predicted_actual) as slot_avg
                FROM read_parquet('{FORECASTS_PATH}')
                WHERE {entity_filter}
                  AND park_date = '{target_date}'
                  AND time_slot BETWEEN '08:00' AND '22:00'
                GROUP BY time_slot
                ORDER BY time_slot
            """
            result = _parquet_df(query)
            # Dedicated connection + execute(): DuckDB 2.x relations (duckdb.sql)
            # have no fetchone(), and the shared default connection rejects new
            # work while a prior execute() result is unfinished.
            _con = duckdb.connect()
            try:
                wti_row = _con.execute(
                    "SELECT wti FROM read_parquet(?) WHERE park_code = ? AND park_date = ?",
                    [WTI_PATH, wti_code, target_date],
                ).fetchone()
            finally:
                _con.close()

        if len(result) == 0:
            return None
        
        raw_low = float(result["slot_avg"].min())
        raw_avg = float(result["slot_avg"].mean())
        raw_high = float(result["slot_avg"].max())
        
        # If we have a mapped WTI value, scale the range to match it
        # This preserves the shape (low/avg/high ratio) but uses the
        # quantile-mapped average for proper distribution
        if wti_row and raw_avg > 0:
            mapped_avg = float(wti_row[0])
            scale = mapped_avg / raw_avg
            return {
                "wti_low": round(raw_low * scale, 1),
                "wti_avg": round(mapped_avg, 1),
                "wti_high": round(raw_high * scale, 1),
            }
        
        return {
            "wti_low": raw_low,
            "wti_avg": raw_avg,
            "wti_high": raw_high,
        }
    except Exception as e:
        print(f"⚠️ Error getting WTI range: {e}")
        return None


@client.event
async def on_ready():
    global entity_names, live_inference_model
    print(f"✅ Logged in as {client.user} (ID: {client.user.id})")
    print(f"💾 Data mode: {'DuckDB (' + DUCKDB_PATH + ')' if _USE_DUCKDB else 'File fallback (parquet/CSV)'}")
    
    # Load entity names at startup
    print("📋 Loading entity names...")
    entity_names = load_entity_names()
    print(f"✅ Loaded {len(entity_names)} entity names")
    
    # Load live inference model at startup
    if LIVE_INFERENCE_AVAILABLE:
        try:
            print("🤖 Loading live inference model...")
            live_inference_model = LiveInferenceModel(output_base=Path(data_path()))
            print("✅ Live inference model loaded successfully")
        except Exception as e:
            print(f"⚠️ Failed to load live inference model: {e}")
            live_inference_model = None
    else:
        print("⚠️ Live inference model not available")
    
    # Sync to test guild for instant updates (no global rate limit)
    TEST_GUILD = discord.Object(id=1471374656253591695)
    try:
        # Clear existing guild commands first to force refresh
        tree.clear_commands(guild=TEST_GUILD)
        tree.copy_global_to(guild=TEST_GUILD)
        synced = await tree.sync(guild=TEST_GUILD)
        print(f"✅ Slash commands synced to test guild: {[c.name for c in synced]}")
        # Verify what Discord actually has
        fetched = await tree.fetch_commands(guild=TEST_GUILD)
        for cmd in fetched:
            print(f"   → /{cmd.name}: {cmd.description}")
            for opt in cmd.options:
                print(f"     param '{opt.name}': {opt.description} | choices={[c.name for c in opt.choices] if opt.choices else 'none'}")
    except Exception as e:
        print(f"❌ Command sync failed: {e}")
    await client.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="the queues 👀"
        )
    )


@client.event
async def on_message(message: discord.Message):
    # Ignore our own messages
    if message.author == client.user:
        return

    # Catch messages that look like failed slash commands (e.g. "/MK", "/ mk", "/help")
    text = message.content.strip()
    if text.startswith("/") and len(text) > 1 and not text.startswith("//"):
        help_text = (
            "👋 Looks like you're trying a command! Here are the ones I know:\n\n"
            "🏰 `/now` — Live wait times for a park right now\n"
            "📊 `/crowd` — Crowd forecast (WTI) for a specific date\n"
            "📅 `/today` — Today's crowd levels across all parks\n"
            "🗓️ `/best-day` — Find the least crowded days to visit\n"
            "🤖 `/ask` — Ask anything about crowds & wait times\n"
            "ℹ️ `/about` — What is WTI and how does this work?\n\n"
            "💡 **Tip:** Type `/` and wait for the menu to pop up, then pick a command!"
        )
        await message.reply(help_text, delete_after=30)


# Autocomplete choices for park parameter
PARK_CHOICES = [
    # Walt Disney World
    app_commands.Choice(name="Magic Kingdom", value="magic kingdom"),
    app_commands.Choice(name="EPCOT", value="epcot"),
    app_commands.Choice(name="Hollywood Studios", value="hollywood studios"),
    app_commands.Choice(name="Animal Kingdom", value="animal kingdom"),
    # Disneyland Resort
    app_commands.Choice(name="Disneyland", value="disneyland"),
    app_commands.Choice(name="California Adventure", value="california adventure"),
    # Universal Orlando
    app_commands.Choice(name="Universal Studios Florida", value="universal studios florida"),
    app_commands.Choice(name="Islands of Adventure", value="islands of adventure"),
    app_commands.Choice(name="Epic Universe", value="epic universe"),
    # Universal Hollywood
    app_commands.Choice(name="Universal Studios Hollywood", value="universal hollywood"),
    # Tokyo Disney Resort
    app_commands.Choice(name="Tokyo Disneyland", value="tokyo disneyland"),
    app_commands.Choice(name="Tokyo DisneySea", value="tokyo disneysea"),
]

@tree.command(name="crowd", description="Get the crowd forecast for a park on a given date")
@app_commands.describe(
    park="Which park?",
    date="Date to check (e.g., tomorrow, feb 15, 07/17/2026, 2026-02-20)"
)
@app_commands.choices(park=PARK_CHOICES)
async def crowd_command(interaction: discord.Interaction, park: str, date: str = "today"):
    print(f"📥 /crowd called: park='{park}' date='{date}' by {interaction.user}")
    park_key = park.lower().strip()
    
    if park_key not in PARK_NAMES:
        await interaction.response.send_message(
            f"🤔 I don't recognize **{park}**. Try: `Magic Kingdom`, `EPCOT`, `Hollywood Studios`, `Animal Kingdom`, `Disneyland`, or `California Adventure`",
            ephemeral=True
        )
        return
    
    park_code, park_full = PARK_NAMES[park_key]
    
    # Parse the date
    try:
        target_date = parse_date(date)
    except ValueError as e:
        await interaction.response.send_message(
            f"🤔 I couldn't understand that date. Try: `today`, `tomorrow`, `feb-15`, or `2026-02-20`",
            ephemeral=True
        )
        return
    
    # Defer response since we're doing database queries
    if not await safe_defer(interaction):
        return
    
    # For today: try archived forecasts first (yesterday's prediction for today)
    # For future: use current forecast file
    # For past: use archived forecasts
    forecasts_df = get_forecasts(park_code, target_date)
    
    if len(forecasts_df) == 0 and target_date <= date_type.today():
        # Not in current forecast file — check archived forecasts
        forecasts_df = get_archived_forecasts(park_code, target_date)
    
    if len(forecasts_df) == 0:
        await interaction.followup.send(
            f"😔 No forecast data available for **{park_full}** on **{target_date.strftime('%B %d, %Y')}**.\n"
            f"This usually means the pipeline hasn't generated forecasts for this date yet.\n"
            f"Try **tomorrow** or a future date!",
            ephemeral=True
        )
        return
    
    # Get WTI score
    wti_score = get_wti_score(park_code, target_date)
    
    if wti_score is None:
        await interaction.followup.send(
            f"😔 I don't have forecast data for **{park_full}** on **{target_date.strftime('%B %d, %Y')}**.\n"
            f"Try a different date or park!",
            ephemeral=True
        )
        return
    
    # Process forecasts to get headliners and low waits (skip extinct entities)
    headliners = []
    low_waits = []
    
    for _, row in forecasts_df.iterrows():
        entity_code = row['entity_code']
        avg_wait = row['avg_wait']
        
        # Skip extinct entities or entities without posted wait times
        if entity_code in entity_names:
            is_extinct = entity_names[entity_code][2]
            has_posted = entity_names[entity_code][3] if len(entity_names[entity_code]) > 3 else True
            if is_extinct or not has_posted:
                continue
            display_name = entity_names[entity_code][1]
        else:
            display_name = entity_code
        if avg_wait > 25:
            headliners.append((display_name, int(avg_wait)))
        elif avg_wait > 0:
            low_waits.append((display_name, int(avg_wait)))
    
    headliners = sorted(headliners, key=lambda x: x[1], reverse=True)[:5]
    low_waits = sorted(low_waits, key=lambda x: x[1])[:5]
    
    embed = discord.Embed(
        title=f"{wti_emoji(wti_score)} {park_full} — {target_date.strftime('%b %d')}",
        url=f"https://hazeydata.ai/theme-park-crowd-report/year-view?park={park_code}",
        description=f"**{wti_label(wti_score, park_code)}.** WTI {wti_score:.0f} — expect {int(wti_score*0.7)}-{int(wti_score*1.2)} min on headliners.",
        color=get_embed_color(wti_score),
    )
    
    if headliners:
        headliner_text = "\n".join([f"{name}: {wait} min" for name, wait in headliners])
        embed.add_field(name="🎢 Headliners", value=headliner_text, inline=True)
    if low_waits:
        low_wait_text = "\n".join([f"{name}: {wait} min" for name, wait in low_waits])
        embed.add_field(name="✨ Low Waits", value=low_wait_text, inline=True)
    
    # Park-appropriate priority queue tips
    priority_tip = _priority_queue_name(park_code)

    if wti_score > 50:
        tip = f"💡 Very busy day — use {priority_tip} and arrive early"
    elif wti_score > 35:
        tip = "💡 Hit headliners at rope drop or after 7pm"
    else:
        tip = "💡 Great day to enjoy everything at your pace"
    
    embed.set_footer(text=f"{tip}  •  Theme Park Crowd Report")
    
    await interaction.followup.send(embed=embed)


TIMEFRAME_CHOICES = [
    app_commands.Choice(name="Next 7 days", value=7),
    app_commands.Choice(name="Next 30 days", value=30),
    app_commands.Choice(name="Next 90 days — heat map", value=90),
    app_commands.Choice(name="Next 1 year — heat map", value=380),  # #469: extended from 365
]

@tree.command(name="best-day", description="Find the best day to visit a park (lowest wait times)")
@app_commands.describe(
    park="Which park?",
    timeframe="How far ahead to look (default: 7 days)"
)
@app_commands.choices(park=PARK_CHOICES, timeframe=TIMEFRAME_CHOICES)
async def best_day_command(interaction: discord.Interaction, park: str, timeframe: int = 7):
    print(f"📥 /best-day called: park='{park}' timeframe={timeframe} by {interaction.user}")

    # NOTE: Premium gate removed during alpha — all features free
    # TODO: Re-enable premium gates when ready to monetize

    park_key = park.lower().strip()

    if park_key not in PARK_NAMES:
        await interaction.response.send_message(
            f"🤔 I don't recognize **{park}**. Try the dropdown!",
            ephemeral=True
        )
        return

    park_code, park_full = PARK_NAMES[park_key]
    if not await safe_defer(interaction):
        return

    from datetime import timedelta
    start = date_type.today() + timedelta(days=1)  # tomorrow
    end = date_type.today() + timedelta(days=timeframe)

    try:
        wti_code = WTI_CODE_MAP.get(park_code, park_code)
        if _USE_DUCKDB:
            con = get_db()
            df = con.execute("""
                SELECT park_date, wti FROM wti
                WHERE park_code = ? AND park_date BETWEEN ? AND ?
                ORDER BY park_date
            """, [wti_code, start, end]).fetchdf()
            con.close()
        else:
            query = f"""
                SELECT park_date, wti
                FROM read_parquet('{WTI_PATH}')
                WHERE park_code = '{wti_code}'
                  AND park_date BETWEEN '{start}' AND '{end}'
                ORDER BY park_date
            """
            df = _parquet_df(query)
    except Exception as e:
        print(f"⚠️ Error in /best-day: {e}")
        _log_bot_error("best-day", type(e).__name__, str(e), "shown_error")
        await interaction.followup.send("I hit a temporary glitch — please try your question again in a moment. If it keeps happening, drop a note in #feedback and we'll look into it. 🎢", ephemeral=True)
        _log_bot_error("best-day", "query_error", str(e), "shown_error")
        return

    if len(df) == 0:
        await interaction.followup.send(
            f"I don't have enough forecast data for **{park_full}** in the next {timeframe} days to find the best day. Try a shorter window or different park! 🎢",
            ephemeral=True
        )
        return

    # Build per-day WTI ranges (low/avg/high from time slots)
    # For large timeframes, use a single batch query to avoid 365 separate DB connections
    # (which causes DuckDB lock contention and blocks the Discord heartbeat)
    days_data = []
    if timeframe >= 90:
        # Batch query: get all time-slot data + WTI values in two queries
        try:
            entity_filter = _entity_filter_sql(park_code)
            wti_code = WTI_CODE_MAP.get(park_code, park_code)
            con = get_db()
            
            # Get per-date time-slot stats (min/mean/max of slot averages)
            slot_stats = con.execute(f"""
                WITH slot_avgs AS (
                    SELECT park_date, 
                           CAST(time_slot AS VARCHAR) as ts,
                           AVG(predicted_actual) as slot_avg
                    FROM forecasts
                    WHERE {entity_filter}
                      AND park_date BETWEEN ? AND ?
                      AND CAST(time_slot AS VARCHAR) BETWEEN '08:00' AND '22:00'
                    GROUP BY park_date, time_slot
                )
                SELECT park_date, 
                       MIN(slot_avg) as raw_low,
                       AVG(slot_avg) as raw_avg, 
                       MAX(slot_avg) as raw_high
                FROM slot_avgs
                GROUP BY park_date
                ORDER BY park_date
            """, [start, end]).fetchdf()
            
            # Get WTI mapped values
            wti_vals = con.execute("""
                SELECT park_date, wti FROM wti
                WHERE park_code = ? AND park_date BETWEEN ? AND ?
            """, [wti_code, start, end]).fetchdf()
            con.close()
            
            # Build lookup for WTI mapped values
            wti_map = {}
            for _, wrow in wti_vals.iterrows():
                wd = wrow['park_date']
                if isinstance(wd, pd.Timestamp):
                    wd = wd.date()
                wti_map[wd] = float(wrow['wti'])
            
            # Build days_data from batch results
            for _, srow in slot_stats.iterrows():
                d = srow['park_date']
                if isinstance(d, pd.Timestamp):
                    d = d.date()
                raw_low = float(srow['raw_low'])
                raw_avg = float(srow['raw_avg'])
                raw_high = float(srow['raw_high'])
                
                if d in wti_map and raw_avg > 0:
                    mapped_avg = wti_map[d]
                    scale = mapped_avg / raw_avg
                    days_data.append({
                        "date": d,
                        "wti_low": round(raw_low * scale, 1),
                        "wti_avg": round(mapped_avg, 1),
                        "wti_high": round(raw_high * scale, 1),
                    })
                elif raw_avg > 0:
                    days_data.append({
                        "date": d,
                        "wti_low": raw_low,
                        "wti_avg": raw_avg,
                        "wti_high": raw_high,
                    })
            print(f"📊 Batch query: {len(days_data)} days loaded for {park_full} ({timeframe}-day view)")
        except Exception as e:
            print(f"⚠️ Batch WTI query failed, falling back to per-day: {e}")
            days_data = []  # Fall through to per-day below
    
    # Per-day fallback (for small timeframes or if batch failed)
    if not days_data:
        for _, row in df.iterrows():
            d = row['park_date']
            if isinstance(d, pd.Timestamp):
                d = d.date()
            wti_range = get_wti_range(park_code, d)
            if wti_range:
                days_data.append({
                    "date": d,
                    "wti_low": wti_range["wti_low"],
                    "wti_avg": wti_range["wti_avg"],
                    "wti_high": wti_range["wti_high"],
                })

    if not days_data:
        await interaction.followup.send(
            f"😔 No detailed forecasts for **{park_full}** in the next {timeframe} days yet.",
            ephemeral=True
        )
        return

    # Sort by date (chronological)
    days_data.sort(key=lambda x: x["date"])

    # Find best day
    best = min(days_data, key=lambda x: x["wti_avg"])

    # Build text description
    THIN_LINE = "─" * 35

    from collections import defaultdict

    if timeframe <= 7:
        # Flat list for 7-day view
        day_lines = []
        for d in days_data:
            day_name = d["date"].strftime("%a")
            date_str = d["date"].strftime("%b %d")
            wti = d["wti_avg"]
            label = wti_label(wti, park_code)
            marker = "  ◄ **Best**" if d["date"] == best["date"] else ""
            day_lines.append(f"**{day_name}** {date_str} — WTI **{wti:.0f}** (*{label}*){marker}")
        description = THIN_LINE + "\n\n" + "\n".join(day_lines)

    elif timeframe <= 30:
        # Weekly grouping with daily detail for 30-day view
        weeks = defaultdict(list)
        for d in days_data:
            week_start = d["date"] - timedelta(days=d["date"].weekday())
            weeks[week_start].append(d)

        desc_parts = []
        for week_start in sorted(weeks.keys()):
            week_days = weeks[week_start]
            week_label = f"Week of {week_start.strftime('%b %d')}"
            day_lines = []
            for d in week_days:
                day_name = d["date"].strftime("%a")
                date_str = d["date"].strftime("%b %d")
                wti = d["wti_avg"]
                label = wti_label(wti, park_code)
                marker = "  ◄ **Best**" if d["date"] == best["date"] else ""
                day_lines.append(f"  **{day_name}** {date_str} — WTI **{wti:.0f}** (*{label}*){marker}")
            desc_parts.append(THIN_LINE + "\n" + week_label + "\n\n" + "\n".join(day_lines))
        description = "\n\n".join(desc_parts)

    else:
        # 90+ day views: calendar image instead of text
        # Try pre-generated image first, fall back to live rendering
        try:
            pre_gen_path = data_path("calendar_images", f"{park_code}_{timeframe}.png")
            if os.path.exists(pre_gen_path):
                file = discord.File(pre_gen_path, filename="calendar.png")
                print(f"📸 Serving pre-generated image: {pre_gen_path}")
            else:
                import asyncio
                loop = asyncio.get_event_loop()
                cal_buf = await loop.run_in_executor(None, generate_calendar_image, park_full, days_data)
                file = discord.File(cal_buf, filename="calendar.png")
                print(f"📸 Generated image live for {park_code}_{timeframe} (no pre-gen found)")

            tip = f"💡 Best day: {best['date'].strftime('%A %b %d')} — {wti_label(best['wti_avg'], park_code).lower()}"
            embed = discord.Embed(
                title=f"📅 {park_full} — Next {timeframe} Days",
                url=f"https://hazeydata.ai/theme-park-crowd-report/year-view?park={park_code}",
                color=get_embed_color(best["wti_avg"]),
            )
            embed.set_image(url="attachment://calendar.png")
            embed.set_footer(text=f"{tip}  •  Theme Park Crowd Report")

            await interaction.followup.send(embed=embed, file=file)
            return
        except Exception as e:
            print(f"⚠️ Calendar image failed, falling back to text: {e}")
            # Fall through to text fallback below

        # Text fallback if image generation fails
        description = f"📊 Calendar view unavailable — showing summary\n\n"
        weeks = defaultdict(list)
        for d in days_data:
            week_start = d["date"] - timedelta(days=d["date"].weekday())
            weeks[week_start].append(d)
        desc_parts = []
        for week_start in sorted(weeks.keys()):
            week_days = weeks[week_start]
            week_end = max(d["date"] for d in week_days)
            avg_wti = sum(d["wti_avg"] for d in week_days) / len(week_days)
            label = wti_label(avg_wti, park_code)
            week_best = min(week_days, key=lambda x: x["wti_avg"])
            best_name = week_best["date"].strftime("%a %b %d")
            best_wti_val = week_best["wti_avg"]
            is_overall_best = week_best["date"] == best["date"]
            marker = "  ⭐" if is_overall_best else ""
            week_range = f"{week_start.strftime('%b %d')}–{week_end.strftime('%b %d')}"
            desc_parts.append(
                f"**{week_range}** — Avg WTI **{avg_wti:.0f}** (*{label}*)\n"
                f"  ▸ Best: **{best_name}** (WTI {best_wti_val:.0f}){marker}"
            )
        description += "\n\n".join(desc_parts)

    if timeframe < 90:
        embed = discord.Embed(
            title=f"📅 {park_full} — Next {timeframe} Days",
            url=f"https://hazeydata.ai/theme-park-crowd-report/year-view?park={park_code}",
            description=description,
            color=get_embed_color(best["wti_avg"]),
        )

        tip = f"💡 Best day: {best['date'].strftime('%A %b %d')} — {wti_label(best['wti_avg'], park_code).lower()}"
        embed.set_footer(text=f"{tip}  •  Theme Park Crowd Report")

        await interaction.followup.send(embed=embed)


@tree.command(name="today", description="Quick overview of all parks today")
async def today_command(interaction: discord.Interaction):
    print(f"📥 /today called by {interaction.user}")
    if not await safe_defer(interaction):
        return

    target_date = date_type.today()
    date_display = target_date.strftime("%A, %B %d")

    # Parks grouped by resort
    park_groups = [
        ("🏰 Walt Disney World", [
            ("MK", "Magic Kingdom"),
            ("EP", "EPCOT"),
            ("HS", "Hollywood Studios"),
            ("AK", "Animal Kingdom"),
        ]),
        ("🎠 Disneyland Resort", [
            ("DL", "Disneyland"),
            ("CA", "California Adventure"),
        ]),
        ("🦈 Universal Orlando", [
            ("UF", "Universal Studios Florida"),
            ("IA", "Islands of Adventure"),
            ("EU", "Epic Universe"),
        ]),
        ("🎬 Universal Hollywood", [
            ("UH", "Universal Studios Hollywood"),
        ]),
        ("🗼 Tokyo Disney Resort", [
            ("TDL", "Tokyo Disneyland"),
            ("TDS", "Tokyo DisneySea"),
        ]),
    ]


    THIN_LINE = "\u2500" * 35
    desc_parts = []
    best_picks = []

    for group_name, parks in park_groups:
        group_best_park = None
        group_best_wti = 999

        section = THIN_LINE + "\n" + group_name + "\n"
        park_lines = []
        for park_code, park_name in parks:
            wti = get_wti_score(park_code, target_date)
            if wti is not None:
                label = wti_label(wti, park_code)
                park_lines.append(f"  \u25b8 **{park_name}** \u2014 WTI **{wti:.0f}** (*{label}*)")
                if wti < group_best_wti:
                    group_best_wti = wti
                    group_best_park = park_name
            else:
                park_lines.append(f"  \u25b8 **{park_name}** \u2014 No data")

        section += "\n".join(park_lines)
        desc_parts.append(section)

        if group_best_park and len(parks) > 1:
            best_picks.append((group_name, group_best_park, group_best_wti))

    if best_picks:
        picks_section = THIN_LINE + "\n\u2b50 **Best Picks**\n"
        pick_lines = []
        for _, park, wti in best_picks:
            pick_lines.append(f"  \u25b8 **{park}** (WTI {wti:.0f})")
        picks_section += "\n".join(pick_lines)
        desc_parts.append(picks_section)

    embed = discord.Embed(
        title=f"\U0001f3f0 All Parks \u2014 {date_display}",
        url="https://hazeydata.ai/theme-park-crowd-report/",
        description="\n\n".join(desc_parts),
        color=0x0A2F8F,
    )

    embed.set_footer(text="\U0001f4a1 /now [park] for live waits \u2022 /crowd [park] for forecasts \u2022 hazeydata.ai")

    await interaction.followup.send(embed=embed)


def _wait_dot(minutes: int) -> str:
    """Color dot based on estimated actual wait time."""
    if minutes >= 60:
        return "\U0001f534"
    elif minutes >= 30:
        return "\U0001f7e0"
    elif minutes >= 15:
        return "\U0001f7e1"
    return "\U0001f7e2"


# ── Park hours lookup for smarter "no data" messages ──
DIMPARKHOURS_PATH = data_path("dimension_tables/dimparkhours.csv")

# Park code → dimparkhours park code mapping
_PARK_HOURS_CODE = {
    "MK": "MK", "EP": "EP", "HS": "HS", "AK": "AK",
    "DL": "DL", "CA": "CA",
    "TDL": "TDL", "TDS": "TDS",
    "UF": "UF", "UH": "UH",
}

# Park code → IANA timezone
_PARK_TZ = {
    "MK": "America/New_York", "EP": "America/New_York",
    "HS": "America/New_York", "AK": "America/New_York",
    "UF": "America/New_York",
    "DL": "America/Los_Angeles", "CA": "America/Los_Angeles",
    "UH": "America/Los_Angeles",
    "TDL": "Asia/Tokyo", "TDS": "Asia/Tokyo",
}


def _get_park_hours(park_code: str, target_date: date_type):
    """Get opening/closing time for a park on a given date from dimparkhours.csv.
    Returns (open_time, close_time) as datetime objects in park local tz, or (None, None)."""
    try:
        import csv as _csv
        from zoneinfo import ZoneInfo
        hours_code = _PARK_HOURS_CODE.get(park_code, park_code)
        date_str = str(target_date)
        with open(DIMPARKHOURS_PATH) as f:
            for row in _csv.DictReader(f):
                if row['park'] == hours_code and row['date'] == date_str and row.get('is_official') == 'True':
                    open_str = row.get('opening_time', '')
                    close_str = row.get('closing_time', '')
                    if open_str and close_str:
                        open_dt = parser.parse(open_str)
                        close_dt = parser.parse(close_str)
                        return open_dt, close_dt
    except Exception as e:
        logger.warning(f"⚠️ Could not load park hours for {park_code}: {e}")
    return None, None


def _get_no_data_message(park_code: str, park_full: str, target_date: date_type, wti_str: str) -> str:
    """Generate a smart 'no data' message based on park hours instead of 'park may be closed'."""
    from zoneinfo import ZoneInfo
    open_time, close_time = _get_park_hours(park_code, target_date)
    tz_name = _PARK_TZ.get(park_code, "America/New_York")
    now_local = datetime.now(ZoneInfo(tz_name))

    if open_time and close_time:
        open_local = open_time.astimezone(ZoneInfo(tz_name))
        close_local = close_time.astimezone(ZoneInfo(tz_name))
        open_fmt = open_local.strftime("%-I:%M %p")
        close_fmt = close_local.strftime("%-I:%M %p")

        if now_local < open_local:
            # Before park opens
            return (
                f"⏰ **{park_full}**{wti_str} hasn't opened yet today. "
                f"Hours: {open_fmt} – {close_fmt}. Live data will appear once the park opens."
            )
        elif now_local > close_local:
            # After park closes
            return (
                f"🌙 **{park_full}**{wti_str} has closed for the day "
                f"(hours were {open_fmt} – {close_fmt})."
            )
        else:
            # Park should be open — data issue
            return (
                f"⚠️ No live wait time data for **{park_full}**{wti_str} right now, "
                f"but the park is open ({open_fmt} – {close_fmt}). Data may be temporarily delayed."
            )
    else:
        # No hours data — generic but no "may be closed" guess
        return (
            f"😴 No live wait time data for **{park_full}**{wti_str} right now. "
            f"Data may be temporarily unavailable."
        )


@tree.command(name="now", description="Live wait times right now for a park")
@app_commands.describe(park="Which park?")
@app_commands.choices(park=PARK_CHOICES)
async def now_command(interaction: discord.Interaction, park: str):
    print(f"\U0001f4e5 /now called: park='{park}' by {interaction.user}")
    if not await safe_defer(interaction):
        return

    park_key = park.lower().strip()
    if park_key not in PARK_NAMES:
        await interaction.followup.send(
            f"\U0001f914 I don't recognize **{park}**. Try the dropdown!", ephemeral=True
        )
        return

    park_code, park_full = PARK_NAMES[park_key]
    target_date = date_type.today()

    # Get current waits
    current_waits = get_current_waits(park_code, target_date)

    if current_waits.empty:
        wti = get_wti_score(park_code, target_date)
        wti_str = f" (WTI {wti:.0f})" if wti is not None else ""
        no_data_msg = _get_no_data_message(park_code, park_full, target_date, wti_str)
        await interaction.followup.send(
            f"{no_data_msg}\n\n\U0001f4a1 Try `/crowd {park}` for today's forecast.",
            ephemeral=True
        )
        return

    # Get WTI for header
    wti = get_wti_score(park_code, target_date)
    wti_str = f" \u2014 WTI {wti:.0f}" if wti is not None else ""

    embed = discord.Embed(
        title=f"\u23f1\ufe0f {park_full}{wti_str}",
        url=f"https://hazeydata.ai/theme-park-crowd-report/year-view?park={park_code}",
        color=0x0A2F8F,
    )

    ride_lines = []
    for _, row in current_waits.iterrows():
        entity_code = row['entity_code']
        posted_wait = int(row['current_wait'])

        # Get ride name — skip unmapped or non-ride entities
        if entity_code in entity_names:
            is_extinct = entity_names[entity_code][2]
            has_posted = entity_names[entity_code][3] if len(entity_names[entity_code]) > 3 else True
            if is_extinct or not has_posted:
                continue
            ride_name = entity_names[entity_code][1]  # short_name
        else:
            continue

        # Convert posted -> estimated actual
        estimated = posted_wait
        if live_inference_model is not None:
            try:
                obs_dt = parser.parse(str(row['latest_obs']))
                pred = live_inference_model.predict(entity_code, float(posted_wait), obs_dt)
                estimated = pred['predicted_actual']
            except Exception as e:
                print(f"\u26a0\ufe0f Live inference failed for {entity_code}: {e}")

        dot = _wait_dot(estimated)
        ride_lines.append((ride_name, estimated, dot))

    # Sort by wait time descending
    ride_lines.sort(key=lambda x: -x[1])

    # Build the display with tier headers
    TIER_HEADERS = {
        "\U0001f534": "\U0001f534 **Extreme Waits**",
        "\U0001f7e0": "\U0001f7e0 **Long Waits**",
        "\U0001f7e1": "\U0001f7e1 **Moderate Waits**",
        "\U0001f7e2": "\U0001f7e2 **Short Waits**",
    }
    lines_text = []
    prev_dot = None
    for name, wait, dot in ride_lines:
        if wait > 0:
            if dot != prev_dot:
                if prev_dot is not None:
                    lines_text.append("")  # blank line before new tier
                lines_text.append(TIER_HEADERS.get(dot, ""))
            lines_text.append(f"  \u25b8 **{name}** \u2014 {wait} min")
            prev_dot = dot

    # Use embed description (4096 char limit) instead of fields to avoid mid-tier splits
    if lines_text:
        full_text = "\n".join(lines_text)
        if len(full_text) <= 4000:
            embed.description = "Live estimated wait times\n\n" + full_text
        else:
            # Fallback: split into fields if somehow over 4096
            embed.description = "Live estimated wait times"
            chunk = []
            chunk_len = 0
            for line in lines_text:
                if chunk_len + len(line) + 1 > 1000:
                    embed.add_field(name="\u200b", value="\n".join(chunk), inline=False)
                    chunk = []
                    chunk_len = 0
                chunk.append(line)
                chunk_len += len(line) + 1
            if chunk:
                embed.add_field(name="\u200b", value="\n".join(chunk), inline=False)

    embed.set_footer(text="\u23f1\ufe0f Updated every 5 min  \u2022  /crowd for forecasts  \u2022  hazeydata.ai")

    await interaction.followup.send(embed=embed)



@tree.command(name="ping", description="Check if the bot is alive")
async def ping_command(interaction: discord.Interaction):
    latency = round(client.latency * 1000)
    await interaction.response.send_message(f"🏰 Pong! {latency}ms — I'm watching the queues.")


@tree.command(name="health", description="Check data pipeline health and freshness")
async def health_command(interaction: discord.Interaction):
    """Show data freshness status — alerts if data is stale."""
    from datetime import datetime, timezone
    await interaction.response.defer(ephemeral=True)

    if not _USE_DUCKDB:
        await interaction.followup.send(
            "⚠️ DuckDB not initialized — running in fallback file mode.", ephemeral=True
        )
        return

    try:
        con = get_db()
        now = datetime.now(timezone.utc)

        # Data freshness from tracking table
        freshness = con.execute("SELECT source, last_updated, row_count, notes FROM data_freshness").fetchdf()

        # Latest live wait observation
        latest_live = con.execute("""
            SELECT MAX(observed_at) as latest, COUNT(DISTINCT entity_code) as entities
            FROM live_waits WHERE park_date = CURRENT_DATE
        """).fetchone()

        # Table counts
        counts = {}
        for table in ['live_waits', 'wti', 'forecasts', 'entities']:
            counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        con.close()

        # Build status
        lines = ["**📊 Data Pipeline Health**\n"]

        # Scraper status
        scraper_row = freshness[freshness['source'] == 'scraper']
        if not scraper_row.empty:
            scraper_ts = pd.to_datetime(scraper_row.iloc[0]['last_updated'])
            if scraper_ts.tzinfo is None:
                scraper_ts = scraper_ts.tz_localize('UTC')
            age_min = (now - scraper_ts).total_seconds() / 60
            if age_min > 15:
                lines.append(f"🔴 **Scraper:** Last update {age_min:.0f} min ago — STALE")
            elif age_min > 10:
                lines.append(f"🟡 **Scraper:** Last update {age_min:.0f} min ago")
            else:
                lines.append(f"🟢 **Scraper:** Last update {age_min:.0f} min ago")
        else:
            lines.append("⚪ **Scraper:** No data")

        # Pipeline status
        for source in ['wti', 'forecasts']:
            row = freshness[freshness['source'] == source]
            if not row.empty:
                ts = pd.to_datetime(row.iloc[0]['last_updated'])
                if ts.tzinfo is None:
                    ts = ts.tz_localize('UTC')
                age_hr = (now - ts).total_seconds() / 3600
                status = "🟢" if age_hr < 26 else "🔴" if age_hr > 48 else "🟡"
                lines.append(f"{status} **{source.upper()}:** Updated {age_hr:.1f}h ago ({row.iloc[0]['row_count']:,} rows)")
            else:
                lines.append(f"⚪ **{source.upper()}:** No data")

        # Live data today
        if latest_live and latest_live[0]:
            lines.append(f"\n**Today:** {latest_live[1]} entities reporting, latest obs: {str(latest_live[0])[:19]}")

        # Table sizes
        lines.append(f"\n**Tables:** {counts['live_waits']:,} waits · {counts['wti']:,} WTI · {counts['forecasts']:,} forecasts · {counts['entities']:,} entities")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Health check failed: {e}", ephemeral=True)


@tree.command(name="about", description="Learn about Theme Park Crowd Report")
async def about_command(interaction: discord.Interaction):
    THIN_LINE = "─" * 30
    embed = discord.Embed(
        title="🏰 Theme Park Crowd Report",
        description=(
            "ML-powered crowd predictions for 12 theme parks, updated daily.\n\n"
            "Built by **Fred Hazelton**, former TouringPlans analyst.\n\n"
            f"{THIN_LINE}\n\n"
            "\n"
            "**What's Inside**\n\n"
            "▸ **WTI Score** — one number = whole-park wait time level\n"
            "▸ **Ride-by-ride** forecasts — top headliners and low-wait picks\n"
            "▸ **12 parks** — WDW · Disneyland · Universal · Tokyo Disney\n"
            "▸ **Updated daily** from real queue data\n\n"
            f"{THIN_LINE}\n\n"
            "\n"
            "🤖 **Commands**\n\n"
            "▸ `/today` — All parks at a glance\n"
            "▸ `/crowd [park] [date]` — Detailed park forecast\n"
            "▸ `/best-day [park] [days]` — Find lowest-crowd days\n"
            "▸ `/now [park]` — Live wait times\n"
            "▸ 🆕 `/ask [question]` — **AI-powered Q&A** — ask anything about "
            "crowds, wait times, and visit planning\n\n"
            f"{THIN_LINE}\n\n"
            "\n"
            "📡 **Data Sources**\n\n"
            "Our models and techniques are our own — trained on many great data sources. "
            "Big thanks to [TouringPlans](https://touringplans.com), "
            "[Queue-Times](https://queue-times.com), and "
            "[Thrill-Data](https://www.thrill-data.com)!\n\n"
            f"{THIN_LINE}\n\n"
            "\n"
            "🌐 [hazeydata.ai](https://hazeydata.ai/theme-park-crowd-report/) — year-view heatmaps, park forecasts, everything in one place"
        ),
        color=0x0A2F8F,
    )
    embed.set_footer(text="Theme Park Crowd Report  •  Beta")
    await interaction.response.send_message(embed=embed)


# ── /ask — AI-powered natural language Q&A ──────────────────────────
import json as _json

FEEDBACK_LOG = Path(os.path.expanduser(os.getenv("TPCR_ASK_FEEDBACK_LOG", "").strip()
                                        or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ask_feedback.jsonl")))


class AskFeedbackView(discord.ui.View):
    """Thumbs up/down buttons for /ask responses."""

    def __init__(self, question: str, user_id: str, username: str):
        super().__init__(timeout=300)  # 5 min to respond
        self.question = question
        self.user_id = user_id
        self.username = username

        # Add buttons dynamically (unique per instance)
        label_btn = discord.ui.Button(label="Was this helpful?", style=discord.ButtonStyle.secondary, disabled=True)
        up_btn = discord.ui.Button(emoji="👍", style=discord.ButtonStyle.secondary)
        down_btn = discord.ui.Button(emoji="👎", style=discord.ButtonStyle.secondary)

        async def on_up(interaction: discord.Interaction):
            self._log_feedback("up", interaction.user.id, str(interaction.user))
            up_btn.style = discord.ButtonStyle.success
            up_btn.disabled = True
            down_btn.disabled = True
            await interaction.response.edit_message(view=self)

        async def on_down(interaction: discord.Interaction):
            self._log_feedback("down", interaction.user.id, str(interaction.user))
            down_btn.style = discord.ButtonStyle.danger
            down_btn.disabled = True
            up_btn.disabled = True
            await interaction.response.edit_message(view=self)

        up_btn.callback = on_up
        down_btn.callback = on_down
        self.add_item(label_btn)
        self.add_item(up_btn)
        self.add_item(down_btn)

    def _log_feedback(self, rating: str, reviewer_id: str, reviewer_name: str):
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "question": self.question,
            "user_id": self.user_id,
            "username": self.username,
            "reviewer_id": str(reviewer_id),
            "reviewer_name": reviewer_name,
            "rating": rating,
        }
        try:
            with open(FEEDBACK_LOG, "a") as f:
                f.write(_json.dumps(entry) + "\n")
        except Exception as e:
            logging.getLogger(__name__).warning("ask feedback log failed (TPCR_ASK_FEEDBACK_LOG=%s): %s: %s",
                                                FEEDBACK_LOG, type(e).__name__, e)

    async def on_timeout(self):
        # Disable buttons after timeout
        for item in self.children:
            item.disabled = True


ANTHROPIC_API_KEY = None
_auth_profiles = os.path.expanduser("~/.clawdbot/agents/anthropic/agent/auth-profiles.json")
try:
    import json as _json
    _data = _json.loads(open(_auth_profiles).read())
    _profiles = _data.get("profiles", _data)  # handle nested or flat
    ANTHROPIC_API_KEY = _profiles.get("anthropic:default", {}).get("key")
except Exception as e:
    print(f"⚠️ Could not load Anthropic API key: {e}")

BOT_COMMANDS_CHANNEL = 1478240248361128079  # #bot-commands (recreated Mar 2)


FRIENDLY_ERROR = "Sorry, I couldn't answer that right now. Please try again in a bit."


@tree.command(name="ask", description="Ask anything about theme park crowds, wait times, and visit planning")
@app_commands.describe(question="Your question (e.g., 'What's the best day to visit Magic Kingdom this month?')")
async def ask_command(interaction: discord.Interaction, question: str):
    print(f"📥 /ask called: question='{question}' by {interaction.user}")

    # Ack immediately: defer, then post a placeholder we edit with the answer
    if not await safe_defer(interaction):
        return

    placeholder = None
    try:
        placeholder = await interaction.followup.send("Thinking…", wait=True)

        answer = await ask_agent(question, str(interaction.user.id), api_key=ANTHROPIC_API_KEY, username=str(interaction.user))

        # Build embed
        embed = discord.Embed(
            description=answer,
            color=0x3C78D2,
        )
        embed.set_author(name=f"Q: {question[:100]}", icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="AI-powered • Data may not be 100% accurate • hazeydata.ai")

        view = AskFeedbackView(question, str(interaction.user.id), str(interaction.user))
        try:
            await placeholder.edit(content=None, embed=embed, view=view)
        except Exception:
            logger.warning("/ask placeholder edit failed; sending new message", exc_info=True)
            await interaction.followup.send(embed=embed, view=view)

    except Exception:
        # Full details server-side only; users never see raw exception text
        logger.error("/ask failed", exc_info=True)
        try:
            if placeholder is not None:
                await placeholder.edit(content=FRIENDLY_ERROR, embed=None, view=None)
            else:
                await interaction.followup.send(FRIENDLY_ERROR, ephemeral=True)
        except Exception:
            logger.error("/ask could not deliver error message", exc_info=True)
            try:
                await interaction.followup.send(FRIENDLY_ERROR, ephemeral=True)
            except Exception:
                pass


if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_BOT_TOKEN not found in ~/.env")
        exit(1)
    client.run(TOKEN)
