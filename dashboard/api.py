"""
Dashboard API - Serves data for stream dashboard frontend.

Provides REST API endpoints for WTI, live wait times, forecast data,
crowd level calculations, predictions, and pro tips.

Usage:
    python dashboard/api.py
    # Runs on http://localhost:8051
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import duckdb
import numpy as np
import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

OUTPUT_BASE = Path("/home/wilma/hazeydata/pipeline")

# ---------------------------------------------------------------------------
# Park / Property reference data
# ---------------------------------------------------------------------------
PARK_INFO = {
    "MK": {"name": "Magic Kingdom",              "property": "wdw"},
    "EP": {"name": "EPCOT",                      "property": "wdw"},
    "HS": {"name": "Hollywood Studios",           "property": "wdw"},
    "AK": {"name": "Animal Kingdom",              "property": "wdw"},
    "DL": {"name": "Disneyland",                  "property": "dlr"},
    "CA": {"name": "California Adventure",        "property": "dlr"},
    "IA": {"name": "Islands of Adventure",        "property": "uor"},
    "UF": {"name": "Universal Studios Florida",   "property": "uor"},
    "EU": {"name": "Epic Universe",               "property": "uor"},
    "UH": {"name": "Universal Studios Hollywood", "property": "ush"},
    "TDL": {"name": "Tokyo Disneyland",             "property": "tdr"},
    "TDS": {"name": "Tokyo DisneySea",              "property": "tdr"},
}

PROPERTY_NAMES = {
    "wdw": "Walt Disney World",
    "dlr": "Disneyland Resort",
    "uor": "Universal Orlando Resort",
    "ush": "Universal Studios Hollywood",
    "tdr": "Tokyo Disney Resort",
}

# Map dashboard park codes (e.g. ioa, usf) to entity_code prefix (e.g. IA, UF)
PARK_CODE_TO_PREFIX = {
    "ioa": "IA", "usf": "UF", "ush": "UH", "uh": "UH",
    "mk": "MK", "ep": "EP", "hs": "HS", "ak": "AK",
    "dl": "DL", "ca": "CA", "eu": "EU",
    "tdl": "TDL", "tds": "TDS",
}

# ---------------------------------------------------------------------------
# Cached data – loaded once at startup
# ---------------------------------------------------------------------------
ENTITIES_DF: pd.DataFrame = pd.DataFrame()       # hazeydata_entities.csv
DIMENTITY_DF: pd.DataFrame = pd.DataFrame()       # dimentity.csv (code→name lookup)
WTI_DF: pd.DataFrame = pd.DataFrame()             # wti.parquet
DATE_GROUP_MAP: dict[str, str] = {}                # park_date str → date_group_id
TRAINED_CODES: set[str] = set()                    # entity codes with model dirs
CODE_TO_NAME: dict[str, str] = {}                  # entity_code (upper) → display name
CODE_TO_SHORT: dict[str, str] = {}                 # entity_code (upper) → short_name
FASTPASS_BOOTH_CODES: set[str] = set()             # entity codes that are fastpass/LL kiosks (not standby)
PARK_HOURS_DF: pd.DataFrame = pd.DataFrame()      # dimparkhours.csv


def _find_column(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    """Return first column name from candidates that exists in df, or None."""
    if df is None or df.empty:
        return None
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _load_startup_data():
    """Load dimension tables, WTI, and model list into module globals."""
    global ENTITIES_DF, DIMENTITY_DF, WTI_DF, DATE_GROUP_MAP, TRAINED_CODES
    global CODE_TO_NAME, CODE_TO_SHORT, FASTPASS_BOOTH_CODES

    # 1. hazeydata_entities.csv
    ent_path = OUTPUT_BASE / "dimension_tables" / "hazeydata_entities.csv"
    if ent_path.exists():
        ENTITIES_DF = pd.read_csv(ent_path, low_memory=False)
        ENTITIES_DF["park_code"] = ENTITIES_DF["park_code"].astype(str).str.strip().str.lower()
        ENTITIES_DF["touringplans_code"] = ENTITIES_DF["touringplans_code"].astype(str).str.strip().str.upper()
        ENTITIES_DF["is_active"] = ENTITIES_DF["is_active"].astype(str).str.strip().str.lower() == "true"
        ENTITIES_DF["has_wait_times"] = ENTITIES_DF["has_wait_times"].astype(str).str.strip().str.lower() == "true"
        logger.info("Loaded %d entities from hazeydata_entities.csv", len(ENTITIES_DF))
    else:
        logger.warning("hazeydata_entities.csv not found at %s", ent_path)

    # 2. dimentity.csv (for code→name fallback)
    dim_path = OUTPUT_BASE / "dimension_tables" / "dimentity.csv"
    if dim_path.exists():
        DIMENTITY_DF = pd.read_csv(dim_path, low_memory=False)
        logger.info("Loaded %d rows from dimentity.csv", len(DIMENTITY_DF))
    else:
        logger.warning("dimentity.csv not found at %s", dim_path)

    # 2b. Build set of fastpass booth entity codes (these are kiosks, not standby rides)
    _dim_code_col = _find_column(DIMENTITY_DF, ["code", "entity_code", "attraction_code"]) if not DIMENTITY_DF.empty else None
    if not DIMENTITY_DF.empty and _dim_code_col and "fastpass_booth" in DIMENTITY_DF.columns:
        fp_mask = DIMENTITY_DF["fastpass_booth"].astype(str).str.strip().str.lower() == "true"
        FASTPASS_BOOTH_CODES = set(
            DIMENTITY_DF.loc[fp_mask, _dim_code_col].astype(str).str.strip().str.upper()
        )
        logger.info("Identified %d fastpass booth entities (excluded from standby list)", len(FASTPASS_BOOTH_CODES))

    # Build name lookups: touringplans_code → name/short_name
    # Primary: hazeydata_entities
    for _, row in ENTITIES_DF.iterrows():
        code = str(row.get("touringplans_code", "")).strip().upper()
        if code and code != "NAN":
            name = str(row.get("name", "")).strip()
            short = str(row.get("short_name", "")).strip()
            if name:
                CODE_TO_NAME[code] = name
            if short:
                CODE_TO_SHORT[code] = short

    # Fallback: dimentity.csv (fills gaps) — support multiple column name variations
    if not DIMENTITY_DF.empty and _dim_code_col:
        _dim_name_col = _find_column(DIMENTITY_DF, ["name", "entity_name", "short_name"])
        _dim_short_col = _find_column(DIMENTITY_DF, ["display_name", "short_name", "name", "entity_name"])
        for _, row in DIMENTITY_DF.iterrows():
            code = str(row.get(_dim_code_col, "")).strip().upper()
            if code and code != "NAN":
                if code not in CODE_TO_NAME and _dim_name_col:
                    name = str(row.get(_dim_name_col, "")).strip()
                    if name:
                        CODE_TO_NAME[code] = name
                if code not in CODE_TO_SHORT and _dim_short_col:
                    short = str(row.get(_dim_short_col, "")).strip()
                    if short:
                        CODE_TO_SHORT[code] = short

    logger.info("Built name lookup for %d entity codes", len(CODE_TO_NAME))

    # 3. WTI
    wti_path = OUTPUT_BASE / "wti" / "wti.parquet"
    if wti_path.exists():
        WTI_DF = pd.read_parquet(wti_path)
        WTI_DF["park_code"] = WTI_DF["park_code"].astype(str).str.strip().str.upper()
        WTI_DF["park_date"] = pd.to_datetime(WTI_DF["park_date"]).dt.date
        logger.info("Loaded %d WTI rows", len(WTI_DF))
    else:
        logger.warning("wti.parquet not found")

    # 4. Date group mapping
    dgid_path = OUTPUT_BASE / "dimension_tables" / "dimdategroupid.csv"
    if dgid_path.exists():
        dgid = pd.read_csv(dgid_path, usecols=["park_date", "date_group_id"], low_memory=False)
        DATE_GROUP_MAP = dict(zip(dgid["park_date"].astype(str), dgid["date_group_id"].astype(str)))
        logger.info("Loaded %d date_group_id mappings", len(DATE_GROUP_MAP))

    # 5. Trained model codes
    models_dir = OUTPUT_BASE / "models"
    if models_dir.exists():
        TRAINED_CODES = {
            d.name.upper()
            for d in models_dir.iterdir()
            if d.is_dir()
        }
        logger.info("Found %d trained model directories", len(TRAINED_CODES))

    # 6. Park hours
    global PARK_HOURS_DF
    ph_path = OUTPUT_BASE / "dimension_tables" / "dimparkhours.csv"
    if ph_path.exists():
        PARK_HOURS_DF = pd.read_csv(ph_path, low_memory=False)
        PARK_HOURS_DF["park"] = PARK_HOURS_DF["park"].astype(str).str.strip().str.upper()
        logger.info("Loaded %d park-hour rows", len(PARK_HOURS_DF))
    else:
        logger.warning("dimparkhours.csv not found")


# Run on import
_load_startup_data()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _park_upper(park_code: str) -> str:
    """Normalise any park code to uppercase."""
    return park_code.strip().upper()


def _entity_name(code: str) -> str:
    """Return display name for an entity code, falling back to code itself."""
    return CODE_TO_NAME.get(code.upper(), code.upper())


def _entities_for_park(park_code_upper: str) -> list[str]:
    """Return list of touringplans_codes for a park (uppercase)."""
    pc_lower = park_code_upper.lower()
    mask = ENTITIES_DF["park_code"] == pc_lower
    return ENTITIES_DF.loc[mask, "touringplans_code"].dropna().tolist()


def _trained_entities_for_park(park_code_upper: str) -> list[dict]:
    """Return [{entity_code, entity_name}] for active, has_wait_times, standby-only entities with trained models.
    
    Excludes fastpass booth / Lightning Lane kiosk entities (fastpass_booth=True in dimentity).
    When hazeydata_entities is empty, falls back to trained models + dimentity for entity list.
    """
    # Normalize park: e.g. ioa -> IA, usf -> UF
    park_norm = PARK_CODE_TO_PREFIX.get(park_code_upper.lower(), park_code_upper)
    prefix = park_norm if len(park_norm) >= 2 else park_code_upper

    # Primary: hazeydata_entities
    if not ENTITIES_DF.empty:
        pc_lower = park_code_upper.lower()
        mask = (
            (ENTITIES_DF["park_code"] == pc_lower)
            & ENTITIES_DF["is_active"]
            & ENTITIES_DF["has_wait_times"]
            & ENTITIES_DF["touringplans_code"].isin(TRAINED_CODES)
            & ~ENTITIES_DF["touringplans_code"].isin(FASTPASS_BOOTH_CODES)
        )
        subset = ENTITIES_DF.loc[mask].copy()
        results = []
        for _, row in subset.iterrows():
            code = row["touringplans_code"]
            name = str(row["name"]).strip() if pd.notna(row.get("name")) and str(row.get("name")).strip() else _entity_name(code)
            results.append({"entity_code": code, "entity_name": name})
        results.sort(key=lambda x: x["entity_name"])
        if results:
            return results

    # Fallback: trained models + dimentity (when hazeydata_entities missing or empty)
    park_entity_codes = [c for c in TRAINED_CODES if c.startswith(prefix)]
    park_entity_codes = [c for c in park_entity_codes if c not in FASTPASS_BOOTH_CODES]
    results = [{"entity_code": c, "entity_name": _entity_name(c)} for c in sorted(park_entity_codes)]
    results.sort(key=lambda x: x["entity_name"])
    return results


def _duckdb_query(sql: str):
    """Run a DuckDB SQL query, return list of tuples."""
    con = duckdb.connect()
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _duckdb_df(sql: str) -> pd.DataFrame:
    """Run a DuckDB SQL query, return DataFrame."""
    con = duckdb.connect()
    try:
        return con.execute(sql).fetchdf()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# WTI / Crowd Level helpers
# ---------------------------------------------------------------------------

def _wti_for_park_date(park_upper: str, target_date: date,
                       fallback_nearest: bool = False) -> Optional[dict]:
    """Get WTI row for a park + date. Returns {wti, n_entities, source} or None.
    
    If fallback_nearest=True and no exact match, return the nearest date's WTI.
    """
    if WTI_DF.empty:
        return None
    park_rows = WTI_DF[WTI_DF["park_code"] == park_upper]
    if park_rows.empty:
        return None

    mask = park_rows["park_date"] == target_date
    exact = park_rows.loc[mask]
    if not exact.empty:
        row = exact.iloc[0]
        return {
            "wti": float(row["wti"]),
            "n_entities": int(row["n_entities"]) if pd.notna(row.get("n_entities")) else None,
            "source": str(row.get("source", "")),
        }

    if not fallback_nearest:
        return None

    # Nearest date fallback
    dates = pd.Series(park_rows["park_date"].values)
    diffs = (dates - target_date).abs()
    idx = diffs.idxmin()
    row = park_rows.iloc[idx]
    return {
        "wti": float(row["wti"]),
        "n_entities": int(row["n_entities"]) if pd.notna(row.get("n_entities")) else None,
        "source": str(row.get("source", "")) + "_nearest",
    }


def _wti_to_crowd_level(wti_minutes: float, park_upper: str) -> int:
    """Convert WTI minutes → 1-10 crowd level using historical percentiles."""
    if pd.isna(wti_minutes) or wti_minutes < 0:
        return 1
    if WTI_DF.empty:
        return _fixed_crowd_level(wti_minutes)
    hist = WTI_DF.loc[
        (WTI_DF["park_code"] == park_upper) & (WTI_DF["park_date"] < date.today()),
        "wti",
    ]
    if hist.empty:
        return _fixed_crowd_level(wti_minutes)
    # Decile approach
    pct = (hist < wti_minutes).mean()  # fraction of historical days below this value
    level = int(np.clip(np.ceil(pct * 10), 1, 10))
    return level


def _fixed_crowd_level(wti: float) -> int:
    """Fallback fixed-threshold crowd level."""
    if wti <= 10:
        return 1
    elif wti <= 18:
        return 2
    elif wti <= 24:
        return 3
    elif wti <= 30:
        return 4
    elif wti <= 36:
        return 5
    elif wti <= 42:
        return 6
    elif wti <= 48:
        return 7
    elif wti <= 55:
        return 8
    elif wti <= 65:
        return 9
    else:
        return 10


# ---------------------------------------------------------------------------
# Park Hours helpers
# ---------------------------------------------------------------------------

def _get_park_hours(park_upper: str, target_date: date) -> tuple[str, str, str]:
    """
    Return (open_hhmm, close_hhmm, hours_source) for a park on a date.
    hours_source: "official" if real hours, "expected" if from donor date, "fallback" if default.
    """
    if PARK_HOURS_DF.empty:
        return ("08:00", "23:00", "fallback")

    date_str = target_date.isoformat()
    rows = PARK_HOURS_DF[
        (PARK_HOURS_DF["park"] == park_upper) &
        (PARK_HOURS_DF["date"] == date_str)
    ]
    is_nearest = False
    if rows.empty:
        # Try nearest date for this park
        park_rows = PARK_HOURS_DF[PARK_HOURS_DF["park"] == park_upper]
        if park_rows.empty:
            return ("08:00", "23:00", "fallback")
        # Find closest date
        park_dates = pd.to_datetime(park_rows["date"]).dt.date
        diffs = abs(park_dates - target_date)
        nearest_idx = diffs.idxmin()
        rows = park_rows.loc[[nearest_idx]]
        is_nearest = True

    row = rows.iloc[0]
    try:
        open_ts = pd.to_datetime(row["opening_time"])
        open_hm = f"{open_ts.hour:02d}:{open_ts.minute:02d}"
    except Exception:
        open_hm = "08:00"
    try:
        close_ts = pd.to_datetime(row["closing_time"])
        close_hm = f"{close_ts.hour:02d}:{close_ts.minute:02d}"
    except Exception:
        close_hm = "23:00"

    # Determine source from is_official column (set by S3 sync = True, imputer = False)
    if is_nearest:
        hours_source = "expected"
    elif "is_official" in row.index:
        is_off = row.get("is_official")
        hours_source = "official" if (is_off is True or str(is_off).strip().lower() == "true") else "expected"
    else:
        # Fallback if column doesn't exist yet
        hours_source = "official" if target_date <= date.today() else "expected"

    return (open_hm, close_hm, hours_source)


def _filter_curve_to_park_hours(curve: list[dict], park_upper: str,
                                  target_date: date,
                                  buffer_minutes: int = 60) -> list[dict]:
    """
    Filter a 24h curve down to park operating hours ± buffer.
    Returns only the time slots within that window.
    """
    if not curve:
        return curve

    open_hm, close_hm, _ = _get_park_hours(park_upper, target_date)

    # Parse to minutes from midnight
    def hm_to_min(hm: str) -> int:
        parts = hm.split(":")
        return int(parts[0]) * 60 + int(parts[1])

    open_min = max(0, hm_to_min(open_hm) - buffer_minutes)
    close_min = min(24 * 60 - 1, hm_to_min(close_hm) + buffer_minutes)

    # Handle midnight crossing (close after midnight)
    if close_min <= open_min:
        close_min = 24 * 60 - 1  # extend to end of day

    filtered = []
    for pt in curve:
        ts = pt["time_slot"]
        try:
            pt_min = hm_to_min(ts)
        except (ValueError, IndexError):
            continue
        if open_min <= pt_min <= close_min:
            filtered.append(pt)

    return filtered if filtered else curve  # fallback to full curve if filter yields nothing


# ---------------------------------------------------------------------------
# Daily Curve helpers
# ---------------------------------------------------------------------------

FORECAST_PARQUET = OUTPUT_BASE / "curves" / "forecast_parquet" / "all_forecasts.parquet"
MODEL_AGG_PARQUET = OUTPUT_BASE / "aggregates" / "model_aggregates.parquet"
# Forecast start date – dates >= this use forecast parquet
FORECAST_START = date(2026, 2, 10)


def _slot_int_to_time(slot: int) -> str:
    """Convert 5-min slot integer (0-287) to HH:MM string."""
    total_minutes = slot * 5
    h = total_minutes // 60
    m = total_minutes % 60
    return f"{h:02d}:{m:02d}"


def _time_to_hhmm(t) -> str:
    """Convert various time representations to HH:MM string."""
    if isinstance(t, str):
        # Already a string like HH:MM:SS or HH:MM
        return t[:5]
    if hasattr(t, "strftime"):
        return t.strftime("%H:%M")
    return str(t)[:5]


def _curve_from_forecasts(park_upper: str, start_d: date, end_d: date,
                           entity_code: Optional[str] = None) -> list[dict]:
    """Build curve from all_forecasts.parquet via DuckDB."""
    if not FORECAST_PARQUET.exists():
        return []
    fp = str(FORECAST_PARQUET)
    start_s = start_d.isoformat()
    end_s = end_d.isoformat()

    if entity_code:
        ec = entity_code.upper()
        sql = f"""
            SELECT time_slot, AVG(predicted_actual) AS avg_wait
            FROM read_parquet('{fp}')
            WHERE entity_code = '{ec}'
              AND park_date >= '{start_s}' AND park_date <= '{end_s}'
            GROUP BY time_slot ORDER BY time_slot
        """
    else:
        sql = f"""
            SELECT time_slot, AVG(predicted_actual) AS avg_wait
            FROM read_parquet('{fp}')
            WHERE entity_code LIKE '{park_upper}%'
              AND park_date >= '{start_s}' AND park_date <= '{end_s}'
            GROUP BY time_slot ORDER BY time_slot
        """
    try:
        rows = _duckdb_query(sql)
        return [{"time_slot": _time_to_hhmm(r[0]), "avg_wait": round(float(r[1]), 1)} for r in rows]
    except Exception as e:
        logger.error("Forecast curve query failed: %s", e)
        return []


def _agg_slot_to_hhmm(slot: int) -> str:
    """15-minute clock slot (0-95) from model_aggregates to HH:MM."""
    if not 0 <= slot < 96:
        raise ValueError(f"model_aggregates time_slot out of range: {slot}")
    return f"{slot // 4:02d}:{(slot % 4) * 15:02d}"


def _curve_from_model_aggregates(park_upper: str, target_date: date,
                                  entity_code: Optional[str] = None) -> list[dict]:
    """Build curve from model_aggregates.parquet using date_group_id for the target date.

    model_aggregates.time_slot is the builder's 15-minute clock slot,
    0-95 = hour*4 + minute//15 in park-local time (pipeline/core/model_aggregates.py),
    so slot 42 is 10:30 regardless of the park's opening time.
    """
    if not MODEL_AGG_PARQUET.exists():
        return []
    date_str = target_date.isoformat()
    dgid = DATE_GROUP_MAP.get(date_str)
    if not dgid:
        logger.warning("No date_group_id for %s", date_str)
        return []

    fp = str(MODEL_AGG_PARQUET)
    if entity_code:
        ec = entity_code.upper()
        where = f"entity_code = '{ec}' AND date_group_id = '{dgid}'"
    else:
        where = f"entity_code LIKE '{park_upper}%' AND date_group_id = '{dgid}'"

    sql = f"""
        SELECT time_slot, AVG(wait_mean_weighted) AS avg_wait
        FROM read_parquet('{fp}')
        WHERE {where}
        GROUP BY time_slot ORDER BY time_slot
    """
    try:
        rows = _duckdb_query(sql)
        return [{"time_slot": _agg_slot_to_hhmm(int(r[0])), "avg_wait": round(float(r[1]), 1)} for r in rows]
    except Exception as e:
        logger.error("Model aggregates curve failed: %s", e)
        return []


def _curve_from_fact_tables(park_upper: str, target_date: date,
                             entity_code: Optional[str] = None,
                             wait_type: str = "actual") -> list[dict]:
    """Build curve from fact_tables/clean CSV for a historical date.
    
    wait_type="actual" → use ACTUAL observations only (caller fills gaps with predictions)
    wait_type="posted" → use POSTED observations for the curve
    """
    pc_lower = park_upper.lower()
    ym = target_date.strftime("%Y-%m")
    date_str = target_date.strftime("%Y-%m-%d")
    csv_path = OUTPUT_BASE / "fact_tables" / "clean" / ym / f"{pc_lower}_{date_str}.csv"
    if not csv_path.exists():
        return []
    try:
        df = pd.read_csv(csv_path, low_memory=False)
    except Exception as e:
        logger.warning("Could not read %s: %s", csv_path, e)
        return []

    if df.empty:
        return []

    # Filter by entity if specified
    if entity_code:
        df = df[df["entity_code"].astype(str).str.upper() == entity_code.upper()]

    # Filter by requested wait_time_type
    if "wait_time_type" in df.columns:
        if wait_type == "posted":
            posted = df[df["wait_time_type"].astype(str).str.upper() == "POSTED"]
            if not posted.empty:
                df = posted
            else:
                df = df[df["wait_time_type"].astype(str).str.upper() != "PRIORITY"]
        else:
            # "actual" mode — only use ACTUAL observations (may be sparse; gaps
            # will be filled by model predictions in _build_daily_curve)
            actual = df[df["wait_time_type"].astype(str).str.upper() == "ACTUAL"]
            df = actual  # may be empty — that's OK, predictions will fill in

    if df.empty or "observed_at" not in df.columns or "wait_time_minutes" not in df.columns:
        return []

    # Parse observed_at WITHOUT converting to UTC — the ISO strings include the
    # local timezone offset (e.g., -08:00 for Pacific).  pandas parses the offset
    # correctly but .dt.hour still returns the *local* hour from the string, which
    # is exactly what we want for time-of-day bucketing.
    df["observed_at_local"] = pd.to_datetime(df["observed_at"], errors="coerce")
    df = df.dropna(subset=["observed_at_local", "wait_time_minutes"])
    df["wait_time_minutes"] = pd.to_numeric(df["wait_time_minutes"], errors="coerce")
    df = df.dropna(subset=["wait_time_minutes"])

    # Bucket into 5-min slots using local park time
    df["slot_min"] = (df["observed_at_local"].dt.hour * 60 + df["observed_at_local"].dt.minute) // 5 * 5
    agg = df.groupby("slot_min")["wait_time_minutes"].agg(["mean", "count"]).reset_index()
    agg.columns = ["slot_min", "avg_wait", "n_obs"]
    agg = agg.sort_values("slot_min")

    # For park-wide curves (no specific entity), require min 3 observations per bin
    # to prevent single-ride outliers from spiking the average
    min_obs = 1 if entity_code else 3
    agg = agg[agg["n_obs"] >= min_obs]

    return [
        {
            "time_slot": f"{int(r['slot_min']) // 60:02d}:{int(r['slot_min']) % 60:02d}",
            "avg_wait": round(float(r["avg_wait"]), 1),
            "n_obs": int(r["n_obs"]),
        }
        for _, r in agg.iterrows()
    ]


def _curve_from_synthetic_actuals(park_upper: str, target_date: date,
                                   entity_code: Optional[str] = None) -> list[dict]:
    """Build curve from synthetic_actuals parquet files for a historical date.
    
    Synthetic actuals are POSTED observations converted to estimated ACTUAL waits
    using the trained conversion model. Much denser than raw ACTUAL observations.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    synth_dir = OUTPUT_BASE / "synthetic_actuals"
    if not synth_dir.exists():
        return []
    
    try:
        # Build glob pattern for entity files
        if entity_code:
            pattern = str(synth_dir / f"{entity_code.upper()}.parquet")
        else:
            pattern = str(synth_dir / f"{park_upper}*.parquet")
        
        con = duckdb.connect()
        # Check if files exist
        files = con.execute(f"SELECT * FROM glob('{pattern}')").fetchall()
        if not files:
            con.close()
            return []
        
        query = f"""
            SELECT 
                strftime(observed_at::TIMESTAMP, '%H:%M') as time_slot,
                AVG(synthetic_actual) as avg_wait,
                COUNT(*) as n_obs
            FROM '{pattern}'
            WHERE park_date = '{date_str}'
              AND synthetic_actual > 0
              AND synthetic_actual < 300
            GROUP BY time_slot
            ORDER BY time_slot
        """
        rows = con.execute(query).fetchall()
        con.close()
        
        if not rows:
            return []
        
        # For park-wide curves, require min 3 observations per slot
        min_obs = 1 if entity_code else 3
        return [
            {"time_slot": r[0], "avg_wait": round(float(r[1]), 1), "n_obs": int(r[2])}
            for r in rows if r[2] >= min_obs
        ]
    except Exception as e:
        logger.warning("Synthetic actuals curve failed for %s on %s: %s", park_upper, date_str, e)
        return []


def _build_daily_curve(park_upper: str, start_d: date, end_d: date,
                        entity_code: Optional[str] = None,
                        wait_type: str = "actual") -> list[dict]:
    """
    Unified daily curve builder:
      - Future dates (>= FORECAST_START) → all_forecasts.parquet (predicted_actual)
      - Historical dates → fact_tables CSVs (filtered by wait_type)
      - For wait_type="actual": uses synthetic actuals (dense) as primary source,
        with raw ACTUAL observations taking priority where available,
        then fills remaining gaps with model predictions.
      - For wait_type="posted": uses POSTED observations directly.
    Results are filtered to park operating hours ± 1h buffer.
    """
    curve = []

    if start_d >= FORECAST_START:
        curve = _curve_from_forecasts(park_upper, start_d, end_d, entity_code)

    if not curve and start_d == end_d:
        # Single date – get observations from fact tables
        fact_curve = _curve_from_fact_tables(park_upper, start_d, entity_code, wait_type=wait_type)

        if wait_type == "actual":
            # For "actual" mode, data priority:
            #   1. Raw ACTUAL observations (ground truth, sparse)
            #   2. Synthetic actuals (POSTED→ACTUAL conversion, dense)
            #   3. Model predictions (forecasts/aggregates, fills remaining gaps)
            
            # Get synthetic actuals (dense coverage from POSTED data)
            synth_curve = _curve_from_synthetic_actuals(park_upper, start_d, entity_code)
            
            # Get model predictions as final fallback
            prediction_curve = _curve_from_model_aggregates(park_upper, start_d, entity_code)
            if not prediction_curve:
                prediction_curve = _curve_from_forecasts(park_upper, start_d, end_d, entity_code)

            # Merge layers: raw actual > synthetic actual > model prediction
            # Start with predictions as base layer
            merged_slots = {}
            if prediction_curve:
                for pt in prediction_curve:
                    merged_slots[pt["time_slot"]] = pt["avg_wait"]
            # Overlay synthetic actuals (better than predictions)
            if synth_curve:
                for pt in synth_curve:
                    merged_slots[pt["time_slot"]] = pt["avg_wait"]
            # Overlay raw actuals (best available — ground truth)
            if fact_curve:
                for pt in fact_curve:
                    merged_slots[pt["time_slot"]] = pt["avg_wait"]
            
            if merged_slots:
                curve = [
                    {"time_slot": ts, "avg_wait": avg}
                    for ts, avg in sorted(merged_slots.items())
                ]
            elif fact_curve:
                curve = fact_curve
        else:
            # "posted" mode: just use the POSTED observations directly
            curve = fact_curve
            if len(curve) < 10:
                # Sparse posted data — try forecast as fallback
                forecast_curve = _curve_from_forecasts(park_upper, FORECAST_START, FORECAST_START, entity_code)
                if forecast_curve:
                    curve = forecast_curve

    elif not curve:
        # Multi-day historical range: aggregate fact tables
        all_slots: dict[str, list[float]] = {}
        current = start_d
        while current <= end_d:
            day_curve = _curve_from_fact_tables(park_upper, current, entity_code, wait_type=wait_type)
            for pt in day_curve:
                all_slots.setdefault(pt["time_slot"], []).append(pt["avg_wait"])
            current += timedelta(days=1)
        if all_slots:
            curve = [
                {"time_slot": ts, "avg_wait": round(sum(vs) / len(vs), 1)}
                for ts, vs in sorted(all_slots.items())
            ]

    # Filter to park operating hours (± 1h buffer)
    if curve:
        curve = _filter_curve_to_park_hours(curve, park_upper, start_d)

    return curve


# ---------------------------------------------------------------------------
# Live wait times helpers
# ---------------------------------------------------------------------------

def _load_live_wait_times(park_upper: str) -> pd.DataFrame:
    """Load latest wait times from staging/queue_times for a park."""
    pc_lower = park_upper.lower()
    staging_dir = OUTPUT_BASE / "staging" / "queue_times"
    if not staging_dir.exists():
        return pd.DataFrame()

    pattern = f"{pc_lower}_*.csv"
    csvs = sorted(staging_dir.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    if not csvs:
        return pd.DataFrame()

    frames = []
    for csv_path in csvs[:5]:
        try:
            df = pd.read_csv(csv_path, low_memory=False)
            if not df.empty:
                frames.append(df)
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)

    # Keep only POSTED
    if "wait_time_type" in combined.columns:
        combined = combined[combined["wait_time_type"].astype(str).str.upper() == "POSTED"]

    if combined.empty:
        return combined

    combined["observed_at"] = pd.to_datetime(combined["observed_at"], errors="coerce")
    combined = combined.sort_values("observed_at", ascending=False)
    combined = combined.drop_duplicates(subset=["entity_code"], keep="first")
    return combined


# =====================================================================
# API ENDPOINTS
# =====================================================================

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


@app.route("/api/park-wti-distributions", methods=["GET"])
def get_park_wti_distributions():
    """
    Return per-park WTI distributions (p5, p25, median, p75, p95, min, max, n_days).
    Used for per-park Benedictus color scaling — "red" means busy FOR that park.
    """
    paths = [
        OUTPUT_BASE / "state" / "park_wti_distributions.json",
        Path("/mnt/data/pipeline/state/park_wti_distributions.json"),
    ]
    for p in paths:
        if p.exists():
            try:
                with open(p, "r") as f:
                    data = json.load(f)
                return jsonify(data)
            except Exception as e:
                logger.warning("Could not read park_wti_distributions: %s", e)
    return jsonify({}), 404


@app.route("/api/park-hours", methods=["GET"])
def get_park_hours():
    """
    Return park hours for all parks (or filtered) on a given date.
    Also returns the earliest open and latest close across all parks.
    
    Query params:
      date     - YYYY-MM-DD (default: today)
      property - filter by property code (e.g., wdw)
      park     - single park code (e.g., MK)
    
    Response:
      { parks: [{code, name, open, close, source}], 
        earliest_open, latest_close }
    """
    date_str = request.args.get("date", date.today().isoformat())
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        target_date = date.today()

    prop_filter = request.args.get("property", "").lower()
    park_filter = request.args.get("park", "").upper()

    results = []
    for code, info in PARK_INFO.items():
        if prop_filter and info["property"] != prop_filter:
            continue
        if park_filter and code != park_filter:
            continue
        open_hm, close_hm, source = _get_park_hours(code, target_date)
        results.append({
            "code": code,
            "name": info["name"],
            "open": open_hm,
            "close": close_hm,
            "source": source,
        })

    # Compute earliest open and latest close
    opens = [r["open"] for r in results if r["open"]]
    closes = [r["close"] for r in results if r["close"]]
    # For closes, handle after-midnight (00:xx, 01:xx, 02:xx) as late
    def close_sort_key(t):
        return t if t >= "06:00" else "Z" + t  # push after-midnight to end
    earliest_open = min(opens) if opens else "08:00"
    latest_close = max(closes, key=close_sort_key) if closes else "23:00"

    return jsonify({
        "parks": results,
        "earliest_open": earliest_open,
        "latest_close": latest_close,
        "date": date_str,
    })


# ----- Properties & Parks -----

@app.route("/api/properties", methods=["GET"])
def get_properties():
    results = [{"code": code, "name": name} for code, name in sorted(PROPERTY_NAMES.items())]
    return jsonify({"properties": results})


@app.route("/api/parks", methods=["GET"])
def get_parks():
    prop_filter = request.args.get("property", "").lower()
    results = []
    for code, info in PARK_INFO.items():
        if prop_filter and info["property"] != prop_filter:
            continue
        results.append({
            "code": code,
            "name": info["name"],
            "property_code": info["property"],
        })
    results.sort(key=lambda x: x["name"])
    return jsonify({"parks": results})


# ----- Entities -----

@app.route("/api/entities/<park_code>", methods=["GET"])
def get_entities(park_code: str):
    park = _park_upper(park_code)
    entities = _trained_entities_for_park(park)
    return jsonify({"entities": entities})


# ----- Stats -----

@app.route("/api/stats/<park_code>", methods=["GET"])
def get_stats(park_code: str):
    park = _park_upper(park_code)

    # Accept optional ?date=YYYY-MM-DD
    date_param = request.args.get("date")
    if date_param:
        try:
            target_date = datetime.strptime(date_param, "%Y-%m-%d").date()
        except ValueError:
            target_date = date.today()
    else:
        target_date = date.today()

    wti_data = _wti_for_park_date(park, target_date, fallback_nearest=True)
    wti_val = wti_data["wti"] if wti_data else None

    # Average wait from live data (only for today)
    avg_wait = None
    if target_date == date.today():
        live = _load_live_wait_times(park)
        if not live.empty and "wait_time_minutes" in live.columns:
            nums = pd.to_numeric(live["wait_time_minutes"], errors="coerce").dropna()
            if not nums.empty:
                avg_wait = round(float(nums.mean()), 1)

    # If no live data, use WTI as proxy
    if avg_wait is None and wti_val is not None:
        avg_wait = round(wti_val, 1)

    # Best time: find time slot with lowest avg_wait from the date's curve
    best_time = None
    curve = _build_daily_curve(park, target_date, target_date)
    if curve:
        # Filter to park hours (roughly 07:00–23:59)
        park_hours = [pt for pt in curve if "07:" <= pt["time_slot"] <= "23:"]
        if not park_hours and curve:
            park_hours = curve
        if park_hours:
            best_pt = min(park_hours, key=lambda x: x["avg_wait"])
            best_time = best_pt["time_slot"]

    # Park hours for this date
    open_time, close_time, hours_source = _get_park_hours(park, target_date)

    return jsonify({
        "park_code": park_code,
        "date": target_date.isoformat(),
        "avg_wait": avg_wait,
        "best_time": best_time,
        "wti": round(wti_val, 1) if wti_val is not None else None,
        "open_time": open_time,
        "close_time": close_time,
        "hours_source": hours_source,
    })


# ----- Wait Times -----

@app.route("/api/wait-times/<park_code>", methods=["GET"])
def get_wait_times(park_code: str):
    park = _park_upper(park_code)
    limit = int(request.args.get("limit", 5))

    live = _load_live_wait_times(park)
    if live.empty:
        return jsonify({"wait_times": []})

    live["wait_time_minutes"] = pd.to_numeric(live["wait_time_minutes"], errors="coerce")
    live = live.dropna(subset=["wait_time_minutes"])
    live = live.sort_values("wait_time_minutes", ascending=False).head(limit)

    results = []
    for _, row in live.iterrows():
        ec = str(row["entity_code"]).strip().upper()
        results.append({
            "entity_code": ec,
            "entity_name": _entity_name(ec),
            "wait_minutes": int(row["wait_time_minutes"]),
            "observed_at": str(row["observed_at"]) if pd.notna(row.get("observed_at")) else None,
        })

    return jsonify({"wait_times": results})


# ----- Daily Curve -----

@app.route("/api/daily-curve/<park_code>", methods=["GET"])
def get_daily_curve(park_code: str):
    park = _park_upper(park_code)
    date_param = request.args.get("date")
    start_param = request.args.get("start")
    end_param = request.args.get("end")
    entity_param = request.args.get("entity_code") or request.args.get("entity")
    wait_type = request.args.get("wait_type", "actual").lower()
    if wait_type not in ("actual", "posted"):
        wait_type = "actual"

    if date_param:
        try:
            start_date = end_date = date.fromisoformat(date_param)
        except ValueError:
            return jsonify({"error": "Invalid date"}), 400
    elif start_param and end_param:
        try:
            start_date = date.fromisoformat(start_param)
            end_date = date.fromisoformat(end_param)
            if start_date > end_date:
                start_date, end_date = end_date, start_date
        except ValueError:
            return jsonify({"error": "Invalid start/end"}), 400
    else:
        start_date = end_date = date.today()

    curve = _build_daily_curve(park, start_date, end_date, entity_param, wait_type=wait_type)

    return jsonify({
        "curve": curve,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "entity_code": entity_param if entity_param else None,
    })


# ----- Actual Points -----

@app.route("/api/actual-points/<park_code>", methods=["GET"])
def get_actual_points(park_code: str):
    park = _park_upper(park_code)
    date_str = request.args.get("date")
    entity_param = request.args.get("entity_code") or request.args.get("entity")
    if not date_str or not entity_param:
        return jsonify({"points": []})
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"points": []})

    pc_lower = park.lower()
    ym = target_date.strftime("%Y-%m")
    date_s = target_date.strftime("%Y-%m-%d")
    csv_path = OUTPUT_BASE / "fact_tables" / "clean" / ym / f"{pc_lower}_{date_s}.csv"
    if not csv_path.exists():
        return jsonify({"points": []})

    try:
        df = pd.read_csv(csv_path, low_memory=False)
    except Exception:
        return jsonify({"points": []})

    entity_upper = entity_param.strip().upper()
    mask = (
        (df["entity_code"].astype(str).str.upper().str.strip() == entity_upper)
        & (df["wait_time_type"].astype(str).str.upper() == "ACTUAL")
    )
    subset = df.loc[mask]
    if subset.empty:
        return jsonify({"points": []})

    points = []
    for _, row in subset.iterrows():
        try:
            dt = pd.to_datetime(row["observed_at"])
            minute_slot = (dt.minute // 5) * 5
            ts = f"{dt.hour:02d}:{minute_slot:02d}"
            wait = int(float(row["wait_time_minutes"]))
            points.append({"time_slot": ts, "wait_time_minutes": wait})
        except Exception:
            continue

    return jsonify({"points": points})


@app.route("/api/sample-actual-points", methods=["GET"])
def get_sample_actual_points():
    """Return one (park_code, entity_code, date) that has ACTUAL data."""
    clean_dir = OUTPUT_BASE / "fact_tables" / "clean"
    if not clean_dir.exists():
        return jsonify({"sample": None})

    month_dirs = sorted(clean_dir.iterdir(), key=lambda p: p.name, reverse=True)
    for month_dir in month_dirs[:3]:
        if not month_dir.is_dir():
            continue
        csvs = sorted(month_dir.glob("*.csv"), key=lambda p: p.name, reverse=True)
        for csv_path in csvs[:20]:
            try:
                name = csv_path.stem
                if "_" not in name:
                    continue
                park_code, date_str = name.split("_", 1)
                df = pd.read_csv(csv_path, nrows=500, low_memory=False)
                if "wait_time_type" not in df.columns:
                    continue
                actual = df[df["wait_time_type"].astype(str).str.upper() == "ACTUAL"]
                if actual.empty:
                    continue
                ec = str(actual.iloc[0]["entity_code"]).strip()
                return jsonify({
                    "sample": {
                        "park_code": park_code.upper(),
                        "entity_code": ec,
                        "date": date_str,
                    }
                })
            except Exception:
                continue

    return jsonify({"sample": None})


# ----- Crowd Level -----

@app.route("/api/crowd-level/<park_code>", methods=["GET"])
def get_crowd_level(park_code: str):
    park = _park_upper(park_code)
    date_str = request.args.get("date")
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else date.today()
    except (ValueError, TypeError):
        target_date = date.today()

    wti_data = _wti_for_park_date(park, target_date, fallback_nearest=True)
    if not wti_data:
        return jsonify({"error": f"No WTI data for {park} on {target_date}"}), 404

    crowd_level = _wti_to_crowd_level(wti_data["wti"], park)

    # Yesterday comparison
    yesterday_wti = _wti_for_park_date(park, target_date - timedelta(days=1))
    vs_yesterday = None
    if yesterday_wti and yesterday_wti["wti"] > 0:
        vs_yesterday = round(((wti_data["wti"] - yesterday_wti["wti"]) / yesterday_wti["wti"]) * 100, 1)

    return jsonify({
        "park_code": park_code,
        "park_date": target_date.isoformat(),
        "crowd_level": crowd_level,
        "wti_minutes": wti_data["wti"],
        "n_entities": wti_data["n_entities"],
        "vs_yesterday_pct": vs_yesterday,
    })


# ----- Forecast -----

@app.route("/api/forecast/<park_code>", methods=["GET"])
def get_forecast(park_code: str):
    park = _park_upper(park_code)
    days = int(request.args.get("days", 7))

    forecast = []
    for i in range(days):
        d = date.today() + timedelta(days=i)
        wti_data = _wti_for_park_date(park, d, fallback_nearest=True)
        if wti_data:
            cl = _wti_to_crowd_level(wti_data["wti"], park)
            forecast.append({
                "date": d.isoformat(),
                "crowd_level": cl,
                "wti_minutes": wti_data["wti"],
            })
        else:
            forecast.append({"date": d.isoformat(), "crowd_level": None, "wti_minutes": None})

    return jsonify({"forecast": forecast})


# ----- Tip -----

@app.route("/api/tip/<park_code>", methods=["GET"])
def get_tip(park_code: str):
    park = _park_upper(park_code)
    today = date.today()

    # Build today's curve to find best time; fall forward to next available forecast
    curve = _build_daily_curve(park, today, today)
    park_hours = [pt for pt in curve if "08:" <= pt["time_slot"] <= "22:"] if curve else []

    # If no park-hour data today, try next forecast date
    tip_date = today
    if not park_hours:
        tip_date = FORECAST_START if FORECAST_START > today else today + timedelta(days=1)
        curve = _build_daily_curve(park, tip_date, tip_date)
        park_hours = [pt for pt in curve if "08:" <= pt["time_slot"] <= "22:"] if curve else []

    if not park_hours:
        return jsonify({"tip": None})

    best = min(park_hours, key=lambda x: x["avg_wait"])
    worst = max(park_hours, key=lambda x: x["avg_wait"])

    try:
        h, m = best["time_slot"].split(":")
        hour = int(h)
        period = "AM" if hour < 12 else "PM"
        hour12 = hour % 12 or 12
        min_str = f":{m}" if m != "00" else ""
        best_str = f"{hour12}{min_str} {period}"
    except Exception:
        best_str = best["time_slot"]

    park_name = PARK_INFO.get(park, {}).get("name", park)
    day_label = "today" if tip_date == today else tip_date.strftime("%A %b %d")
    tip = (
        f"Lowest average wait at {park_name} {day_label} is around {best_str} "
        f"(~{best['avg_wait']:.0f} min avg). "
        f"Peak is around {worst['time_slot']} (~{worst['avg_wait']:.0f} min)."
    )
    return jsonify({"tip": tip})


# ----- Predictions -----

@app.route("/api/predictions/<park_code>", methods=["GET"])
def get_predictions(park_code: str):
    park = _park_upper(park_code)
    pred_path = OUTPUT_BASE / "predictions" / "historical_predictions.parquet"
    if not pred_path.exists():
        return jsonify({"error": "Historical predictions not available"}), 404

    entity_code = request.args.get("entity_code", "").upper()
    date_filter = request.args.get("date")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    limit = int(request.args.get("limit", 1000))

    conditions = [f"entity_code LIKE '{park}%'"]
    if entity_code:
        conditions.append(f"entity_code = '{entity_code}'")
    if date_filter:
        conditions.append(f"CAST(park_date AS VARCHAR) = '{date_filter}'")
    elif start_date and end_date:
        conditions.append(f"CAST(park_date AS VARCHAR) >= '{start_date}' AND CAST(park_date AS VARCHAR) <= '{end_date}'")
    elif start_date:
        conditions.append(f"CAST(park_date AS VARCHAR) >= '{start_date}'")
    elif end_date:
        conditions.append(f"CAST(park_date AS VARCHAR) <= '{end_date}'")

    where = " AND ".join(conditions)
    sql = f"""
        SELECT entity_code, observed_at, park_date, posted_time,
               predicted_actual, prediction_method, hour_of_day
        FROM read_parquet('{pred_path}')
        WHERE {where}
        ORDER BY observed_at DESC
        LIMIT {limit}
    """
    try:
        df = _duckdb_df(sql)
        results = df.to_dict(orient="records")
        # Convert timestamps to strings for JSON serialization
        for r in results:
            for k, v in r.items():
                if hasattr(v, 'isoformat'):
                    r[k] = v.isoformat()
                elif isinstance(v, (np.integer,)):
                    r[k] = int(v)
                elif isinstance(v, (np.floating,)):
                    r[k] = float(v)
        return jsonify({"park_code": park_code, "count": len(results), "predictions": results})
    except Exception as e:
        logger.error("Predictions query failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/predictions/<park_code>/daily-curve", methods=["GET"])
def get_predictions_daily_curve(park_code: str):
    entity_code = request.args.get("entity_code", "").upper()
    if not entity_code:
        return jsonify({"error": "entity_code required"}), 400
    date_filter = request.args.get("date", date.today().isoformat())

    pred_path = OUTPUT_BASE / "predictions" / "historical_predictions.parquet"
    if not pred_path.exists():
        return jsonify({"error": "Historical predictions not available"}), 404

    sql = f"""
        SELECT observed_at, hour_of_day, posted_time, predicted_actual, prediction_method
        FROM read_parquet('{pred_path}')
        WHERE entity_code = '{entity_code}'
          AND CAST(park_date AS VARCHAR) = '{date_filter}'
        ORDER BY observed_at
    """
    try:
        df = _duckdb_df(sql)
        if df.empty:
            return jsonify({"entity_code": entity_code, "date": date_filter, "curve": []})

        curve = []
        for _, row in df.iterrows():
            obs = row["observed_at"]
            curve.append({
                "time": obs.isoformat() if hasattr(obs, "isoformat") else str(obs),
                "hour": int(row["hour_of_day"]) if pd.notna(row["hour_of_day"]) else None,
                "posted": int(row["posted_time"]),
                "predicted": round(float(row["predicted_actual"]), 1),
                "method": row["prediction_method"],
            })
        return jsonify({
            "entity_code": entity_code,
            "date": date_filter,
            "method": df["prediction_method"].iloc[0] if not df.empty else None,
            "curve": curve,
        })
    except Exception as e:
        logger.error("Predictions daily curve failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/predictions/entities", methods=["GET"])
def get_prediction_entities():
    pred_path = OUTPUT_BASE / "predictions" / "historical_predictions.parquet"
    if not pred_path.exists():
        return jsonify({"error": "Historical predictions not available"}), 404

    park = request.args.get("park", "").upper()
    where = f"WHERE entity_code LIKE '{park}%'" if park else ""

    sql = f"""
        SELECT entity_code, prediction_method, COUNT(*) as prediction_count,
               MIN(CAST(park_date AS VARCHAR)) as first_date,
               MAX(CAST(park_date AS VARCHAR)) as last_date,
               AVG(posted_time) as avg_posted,
               AVG(predicted_actual) as avg_predicted
        FROM read_parquet('{pred_path}')
        {where}
        GROUP BY entity_code, prediction_method
        ORDER BY prediction_count DESC
    """
    try:
        df = _duckdb_df(sql)
        entities = []
        for _, row in df.iterrows():
            entities.append({
                "entity_code": row["entity_code"],
                "prediction_count": int(row["prediction_count"]),
                "first_date": str(row["first_date"]),
                "last_date": str(row["last_date"]),
                "avg_posted": round(float(row["avg_posted"]), 1),
                "avg_predicted": round(float(row["avg_predicted"]), 1),
                "method": row["prediction_method"],
            })
        return jsonify({"count": len(entities), "entities": entities})
    except Exception as e:
        logger.error("Prediction entities failed: %s", e)
        return jsonify({"error": str(e)}), 500


# ----- Forecast Detail -----

@app.route("/api/forecast-detail/<park_code>", methods=["GET"])
def get_forecast_detail(park_code: str):
    park = _park_upper(park_code)
    if not FORECAST_PARQUET.exists():
        return jsonify({"error": "Forecast data not available"}), 404

    entity_code = request.args.get("entity_code", "").upper()
    date_filter = request.args.get("date")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    limit = int(request.args.get("limit", 1000))

    fp = str(FORECAST_PARQUET)
    conditions = [f"entity_code LIKE '{park}%'"]
    if entity_code:
        conditions.append(f"UPPER(entity_code) = '{entity_code}'")
    if date_filter:
        conditions.append(f"CAST(park_date AS VARCHAR) = '{date_filter}'")
    elif start_date:
        conditions.append(f"CAST(park_date AS VARCHAR) >= '{start_date}'")
        if end_date:
            conditions.append(f"CAST(park_date AS VARCHAR) <= '{end_date}'")
        else:
            ed = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
            conditions.append(f"CAST(park_date AS VARCHAR) <= '{ed}'")

    where = " AND ".join(conditions)
    sql = f"""
        SELECT entity_code, park_date, time_slot, predicted_actual, prediction_method
        FROM read_parquet('{fp}')
        WHERE {where}
        ORDER BY entity_code, park_date, time_slot
        LIMIT {limit}
    """
    try:
        rows = _duckdb_query(sql)
        predictions = []
        for r in rows:
            predictions.append({
                "entity_code": r[0],
                "park_date": str(r[1]),
                "time_slot": _time_to_hhmm(r[2]),
                "predicted_actual": round(float(r[3]), 1),
                "method": r[4],
            })
        return jsonify({"park_code": park_code, "count": len(predictions), "predictions": predictions})
    except Exception as e:
        logger.error("Forecast detail failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/forecast-detail/<park_code>/daily-curve", methods=["GET"])
def get_forecast_daily_curve(park_code: str):
    if not FORECAST_PARQUET.exists():
        return jsonify({"error": "Forecast data not available"}), 404

    entity_code = request.args.get("entity_code", "").upper()
    date_filter = request.args.get("date")
    if not entity_code or not date_filter:
        return jsonify({"error": "entity_code and date are required"}), 400

    fp = str(FORECAST_PARQUET)
    sql = f"""
        SELECT time_slot, predicted_actual, prediction_method
        FROM read_parquet('{fp}')
        WHERE UPPER(entity_code) = '{entity_code}'
          AND CAST(park_date AS VARCHAR) = '{date_filter}'
        ORDER BY time_slot
    """
    try:
        rows = _duckdb_query(sql)
        if not rows:
            return jsonify({"entity_code": entity_code, "date": date_filter, "curve": []})

        curve = [{"time": _time_to_hhmm(r[0]), "predicted": round(float(r[1]), 1)} for r in rows]
        return jsonify({
            "entity_code": entity_code,
            "date": date_filter,
            "method": rows[0][2] if rows else None,
            "curve": curve,
        })
    except Exception as e:
        logger.error("Forecast daily curve failed: %s", e)
        return jsonify({"error": str(e)}), 500


# ----- Forecast Summary -----

@app.route("/api/forecast-summary", methods=["GET"])
def get_forecast_summary():
    if not FORECAST_PARQUET.exists():
        return jsonify({"error": "Forecast data not available"}), 404

    fp = str(FORECAST_PARQUET)
    sql = f"""
        SELECT COUNT(*) as total_predictions,
               COUNT(DISTINCT entity_code) as entity_count,
               MIN(CAST(park_date AS VARCHAR)) as start_date,
               MAX(CAST(park_date AS VARCHAR)) as end_date,
               COUNT(DISTINCT park_date) as date_count
        FROM read_parquet('{fp}')
    """
    try:
        rows = _duckdb_query(sql)
        r = rows[0]
        return jsonify({
            "total_predictions": int(r[0]),
            "entity_count": int(r[1]),
            "start_date": str(r[2]),
            "end_date": str(r[3]),
            "date_count": int(r[4]),
        })
    except Exception as e:
        logger.error("Forecast summary failed: %s", e)
        return jsonify({"error": str(e)}), 500


# ----- Distribution (Box Plot) -----

@app.route("/api/distribution/<park_code>", methods=["GET"])
def get_distribution(park_code: str):
    """
    Box-plot statistics for a park's WTI or an entity's daily average wait.

    Query params:
      entity_code  – if provided, compute distribution for that entity
      date         – the "current" date to highlight (today_value)

    Returns: {min, q1, median, q3, max, outliers, today_value, today_date, n_days, entity_name}
    """
    entity_code = request.args.get("entity_code", "").strip()
    date_str = request.args.get("date", "").strip()
    prop_code = request.args.get("property", "").strip().lower()
    target_date: Optional[date] = None
    if date_str:
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            target_date = None

    # ------------------------------------------------------------------
    # Entity-level distribution
    # ------------------------------------------------------------------
    if entity_code:
        ec_upper = entity_code.upper()
        end_d = target_date or date.today()
        end_s = end_d.isoformat()

        try:
            import duckdb as _ddb
            con = _ddb.connect()
            parquet_dir = str(OUTPUT_BASE / "fact_tables" / "parquet")

            # Get daily averages for this entity across ALL history
            result = con.execute(f"""
                SELECT
                    park_date,
                    AVG(wait_time_minutes) as day_avg
                FROM read_parquet('{parquet_dir}/*.parquet')
                WHERE entity_code = '{ec_upper}'
                AND wait_time_type = 'ACTUAL'
                AND park_date <= '{end_s}'
                AND wait_time_minutes > 0
                GROUP BY park_date
                ORDER BY park_date
            """).fetchdf()
            con.close()

            if result.empty:
                return jsonify({"error": f"No ACTUAL wait data for entity {ec_upper}"}), 404

            daily_avgs = result["day_avg"].tolist()
            today_value = None
            today_row = result[result["park_date"] == end_s]
            if not today_row.empty:
                today_value = round(float(today_row["day_avg"].iloc[0]), 1)

        except Exception as e:
            app.logger.error(f"DuckDB distribution query failed: {e}")
            return jsonify({"error": f"Distribution query failed for {ec_upper}"}), 500

        values = np.array(daily_avgs)
        q1 = float(np.percentile(values, 25))
        median_val = float(np.percentile(values, 50))
        q3 = float(np.percentile(values, 75))
        iqr = q3 - q1
        lower_fence = q1 - 1.5 * iqr
        upper_fence = q3 + 1.5 * iqr
        non_outliers = values[(values >= lower_fence) & (values <= upper_fence)]
        outliers = values[(values < lower_fence) | (values > upper_fence)]

        return jsonify({
            "min": round(float(non_outliers.min()), 1) if len(non_outliers) > 0 else round(float(values.min()), 1),
            "q1": round(q1, 1),
            "median": round(median_val, 1),
            "q3": round(q3, 1),
            "max": round(float(non_outliers.max()), 1) if len(non_outliers) > 0 else round(float(values.max()), 1),
            "outliers": sorted([round(float(o), 1) for o in outliers]),
            "today_value": today_value,
            "today_date": end_d.isoformat(),
            "n_days": len(daily_avgs),
            "entity_name": _entity_name(ec_upper),
        })

    # Park-level distribution (WTI)
    # ------------------------------------------------------------------
    if WTI_DF.empty:
        return jsonify({"error": "No WTI data loaded"}), 404

    park = _park_upper(park_code)
    if park == "ALL":
        if prop_code and prop_code != "all":
            # Filter WTI to parks belonging to this property
            prop_parks = [code for code, info in PARK_INFO.items()
                          if info.get("property", "").lower() == prop_code]
            wti_values = WTI_DF.loc[WTI_DF["park_code"].isin(prop_parks), "wti"].dropna().values
        else:
            wti_values = WTI_DF["wti"].dropna().values
    else:
        wti_values = WTI_DF.loc[WTI_DF["park_code"] == park, "wti"].dropna().values

    if len(wti_values) == 0:
        return jsonify({"error": f"No WTI data for park {park}"}), 404

    values = np.array(wti_values, dtype=float)
    q1 = float(np.percentile(values, 25))
    median_val = float(np.percentile(values, 50))
    q3 = float(np.percentile(values, 75))
    iqr = q3 - q1
    lower_fence = q1 - 1.5 * iqr
    upper_fence = q3 + 1.5 * iqr
    non_outliers = values[(values >= lower_fence) & (values <= upper_fence)]
    outliers = values[(values < lower_fence) | (values > upper_fence)]

    today_value = None
    td = target_date or date.today()
    if park == "ALL":
        if prop_code and prop_code != "all":
            prop_park_codes = [code for code, info in PARK_INFO.items()
                               if info.get("property", "").lower() == prop_code]
            day_vals = WTI_DF.loc[
                (WTI_DF["park_date"] == td) & (WTI_DF["park_code"].isin(prop_park_codes)),
                "wti"
            ].dropna()
        else:
            day_vals = WTI_DF.loc[WTI_DF["park_date"] == td, "wti"].dropna()
        if not day_vals.empty:
            today_value = round(float(day_vals.mean()), 1)
    else:
        wti_row = _wti_for_park_date(park, td)
        if wti_row:
            today_value = round(wti_row["wti"], 1)

    if park == "ALL" and prop_code and prop_code != "all":
        park_name = PROPERTY_NAMES.get(prop_code, prop_code.upper())
    elif park != "ALL":
        park_name = PARK_INFO.get(park, {}).get("name", park)
    else:
        park_name = "All Parks"

    return jsonify({
        "min": round(float(non_outliers.min()), 1) if len(non_outliers) > 0 else round(float(values.min()), 1),
        "q1": round(q1, 1),
        "median": round(median_val, 1),
        "q3": round(q3, 1),
        "max": round(float(non_outliers.max()), 1) if len(non_outliers) > 0 else round(float(values.max()), 1),
        "outliers": sorted([round(float(o), 1) for o in outliers]),
        "today_value": today_value,
        "today_date": td.isoformat(),
        "n_days": int(len(values)),
        "entity_name": park_name,
    })


# ----- Z-Score Trend -----

@app.route("/api/trend/<park_code>", methods=["GET"])
def get_trend(park_code: str):
    """
    Z-score trend for WTI over time, using date_group_id for seasonal comparison.

    Each day's WTI is compared against the historical mean/std of all days
    sharing the same date_group_id (e.g. FEB_WEEK2_TUE, THANKSGIVING_THU).
    This captures week-of-month, day-of-week, and holiday patterns across years.

    Query params:
      days       – number of days to return (default 90)
      date       – end date (default today)
      property   – property code filter when park_code=all

    Returns: {points: [{date, wti, z_score, seasonal_mean, seasonal_std,
                         date_group_id, n_comparable, label}], park_name}
    """
    if WTI_DF.empty:
        return jsonify({"error": "No WTI data loaded"}), 404

    days = int(request.args.get("days", 90))
    date_str = request.args.get("date", "").strip()
    prop_code = request.args.get("property", "").strip().lower()

    end_d = date.today()
    if date_str:
        try:
            end_d = date.fromisoformat(date_str)
        except ValueError:
            pass
    start_d = end_d - timedelta(days=days)

    park = _park_upper(park_code)

    # --- Select the relevant WTI rows ---
    if park == "ALL":
        if prop_code and prop_code != "all":
            prop_parks = [code for code, info in PARK_INFO.items()
                          if info.get("property", "").lower() == prop_code]
            base_df = WTI_DF[WTI_DF["park_code"].isin(prop_parks)].copy()
            park_name = PROPERTY_NAMES.get(prop_code, prop_code.upper())
        else:
            base_df = WTI_DF.copy()
            park_name = "All Parks"
        # Average across parks per day
        daily = base_df.groupby("park_date")["wti"].mean().reset_index()
    else:
        base_df = WTI_DF[WTI_DF["park_code"] == park].copy()
        park_name = PARK_INFO.get(park, {}).get("name", park)
        daily = base_df[["park_date", "wti"]].copy()

    if daily.empty:
        return jsonify({"error": f"No WTI data for {park}"}), 404

    daily = daily.sort_values("park_date").reset_index(drop=True)

    # --- Attach date_group_id to each WTI row ---
    daily["date_str"] = daily["park_date"].apply(
        lambda d: d.isoformat() if hasattr(d, "isoformat") else str(d)
    )
    daily["date_group_id"] = daily["date_str"].map(DATE_GROUP_MAP)

    # --- Compute seasonal stats per date_group_id (historical only: before today) ---
    # Use all historical data (not just the window) for robust baselines
    hist_mask = daily["park_date"] < date.today()
    hist = daily.loc[hist_mask & daily["date_group_id"].notna()].copy()
    group_stats = hist.groupby("date_group_id")["wti"].agg(["mean", "std", "count"])
    group_stats.columns = ["seasonal_mean", "seasonal_std", "n_comparable"]
    # Ensure std is never zero/NaN
    group_stats["seasonal_std"] = group_stats["seasonal_std"].fillna(0).replace(0, 1.0)

    # --- Calculate z-scores for the requested window ---
    mask = (daily["park_date"] >= start_d) & (daily["park_date"] <= end_d)
    window = daily.loc[mask].copy()

    points = []
    for _, row in window.iterrows():
        wti_val = float(row["wti"])
        dgid = row.get("date_group_id")
        
        if dgid and dgid in group_stats.index:
            ss = group_stats.loc[dgid]
            s_mean = float(ss["seasonal_mean"])
            s_std = float(ss["seasonal_std"]) if ss["seasonal_std"] > 0 else 1.0
            n_comp = int(ss["n_comparable"])
            z = (wti_val - s_mean) / s_std
        else:
            # Fallback: no date_group_id mapped — use overall park mean/std
            s_mean = float(daily["wti"].mean())
            s_std = float(daily["wti"].std()) or 1.0
            n_comp = 0
            z = (wti_val - s_mean) / s_std

        # Label for extreme values
        label = None
        if z >= 2.0:
            label = "Unusually Busy"
        elif z >= 1.5:
            label = "Above Average"
        elif z <= -2.0:
            label = "Unusually Quiet"
        elif z <= -1.5:
            label = "Below Average"

        points.append({
            "date": row["park_date"].isoformat() if hasattr(row["park_date"], "isoformat") else str(row["park_date"]),
            "wti": round(wti_val, 1),
            "z_score": round(z, 2),
            "seasonal_mean": round(s_mean, 1),
            "seasonal_std": round(s_std, 1),
            "date_group_id": dgid or "unknown",
            "n_comparable": n_comp,
            "label": label,
        })

    # Summary stats
    z_values = [p["z_score"] for p in points]
    avg_z = round(sum(z_values) / len(z_values), 2) if z_values else 0

    return jsonify({
        "points": points,
        "park_name": park_name,
        "start_date": start_d.isoformat(),
        "end_date": end_d.isoformat(),
        "n_points": len(points),
        "avg_z_score": avg_z,
    })


# ----- Debug -----

# ----- Stripe Premium -----

@app.route("/api/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """Create a Stripe Checkout Session for TPCR Premium subscription."""
    try:
        import stripe
        from dashboard import stripe_handler
    except ImportError:
        return jsonify({"error": "Stripe not configured"}), 500

    secret = os.environ.get("STRIPE_SECRET_KEY")
    price_id = os.environ.get("STRIPE_PRICE_ID")
    site_base = os.environ.get("SITE_BASE_URL", "https://hazeydata.ai").rstrip("/")

    if not secret or not price_id:
        logger.warning("STRIPE_SECRET_KEY or STRIPE_PRICE_ID not set")
        return jsonify({"error": "Checkout not configured"}), 500

    stripe.api_key = secret

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{site_base}/subscribe-success.html?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{site_base}/subscribe.html",
            custom_fields=[{
                "key": "discord_username",
                "label": {"type": "custom", "custom": "Discord username (e.g. fred or fred#1234)"},
                "type": "text",
            }],
            allow_promotion_codes=True,
        )
        return jsonify({"url": session.url})
    except Exception as e:
        logger.exception("Stripe checkout session create failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    """Handle Stripe webhook events (payment, subscription)."""
    try:
        import stripe
        from dashboard import stripe_handler
    except ImportError:
        return jsonify({"error": "Stripe not configured"}), 500

    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        logger.warning("STRIPE_WEBHOOK_SECRET not set")
        return jsonify({"error": "Webhook not configured"}), 500

    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError as e:
        logger.warning("Stripe webhook invalid payload: %s", e)
        return jsonify({"error": "Invalid payload"}), 400
    except stripe.SignatureVerificationError as e:
        logger.warning("Stripe webhook signature verification failed: %s", e)
        return jsonify({"error": "Invalid signature"}), 400

    stripe_handler.handle_stripe_event(event)
    return jsonify({"received": True}), 200


@app.route("/api/debug/entity-table", methods=["GET"])
def debug_entity_table():
    out = {
        "hazeydata_entities": {
            "loaded": not ENTITIES_DF.empty,
            "row_count": len(ENTITIES_DF),
            "columns": list(ENTITIES_DF.columns) if not ENTITIES_DF.empty else [],
            "sample": ENTITIES_DF.head(5).to_dict(orient="records") if not ENTITIES_DF.empty else [],
        },
        "dimentity": {
            "loaded": not DIMENTITY_DF.empty,
            "row_count": len(DIMENTITY_DF),
            "columns": list(DIMENTITY_DF.columns) if not DIMENTITY_DF.empty else [],
            "code_col": _find_column(DIMENTITY_DF, ["code", "entity_code", "attraction_code"]) if not DIMENTITY_DF.empty else None,
            "name_col": _find_column(DIMENTITY_DF, ["name", "entity_name", "short_name"]) if not DIMENTITY_DF.empty else None,
            "sample": DIMENTITY_DF.head(5).to_dict(orient="records") if not DIMENTITY_DF.empty else [],
        },
        "name_lookup_count": len(CODE_TO_NAME),
        "trained_model_count": len(TRAINED_CODES),
    }
    if not ENTITIES_DF.empty:
        out["hazeydata_entities"]["active_with_wait_times"] = int(
            ((ENTITIES_DF["is_active"]) & (ENTITIES_DF["has_wait_times"])).sum()
        )
    return jsonify(out)


# =====================================================================
# SSD (School Schedule Database) API
# =====================================================================

try:
    from ssd_api import ssd_bp
    app.register_blueprint(ssd_bp)
    logger.info("SSD API blueprint registered")
except ImportError as e:
    logger.warning("SSD API not available: %s", e)


# =====================================================================
# Main
# =====================================================================

if __name__ == "__main__":
    logger.info("Starting Dashboard API on port 8051")
    logger.info("Output base: %s", OUTPUT_BASE)
    logger.info("Entities: %d | Trained models: %d | WTI rows: %d",
                len(ENTITIES_DF), len(TRAINED_CODES), len(WTI_DF))
    app.run(host="0.0.0.0", port=8051, debug=False)
