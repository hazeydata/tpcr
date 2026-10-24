"""
Shared helpers for writing wait-time rows to tpcr_live.duckdb live_waits.

Used by the queue-times scraper (get_wait_times_from_queue_times.py) and the daily
staging replay (replay_live_waits.py). Idempotency is done with an anti-join on the key,
not INSERT OR IGNORE, because the live table may have been rebuilt without a PRIMARY KEY.
"""

from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

_SRC_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SRC_DIR.parent
for _p in (str(_SRC_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from get_tp_wait_time_data_from_s3 import derive_park_date
from pipeline.core.park_codes import PARK_TIMEZONE

LIVE_WAITS_KEY = ("entity_code", "observed_at", "wait_time_type")
LIVE_WAITS_COLUMNS = LIVE_WAITS_KEY + ("wait_time_minutes", "park_date")


def park_tz_for_park_code(code: str) -> ZoneInfo:
    """Park timezone for a park code (case-insensitive, e.g. "mk", "UH", "tdl")."""
    tz_name = PARK_TIMEZONE.get(str(code).strip().upper())
    if tz_name is None:
        raise ValueError(f"Unknown park code: {code!r}")
    return ZoneInfo(tz_name)


def prepare_live_waits_rows(df: pd.DataFrame, park_tz: ZoneInfo) -> pd.DataFrame:
    """Add park_date (6 AM rule), keep wait > 0 and non-empty keys; return LIVE_WAITS_COLUMNS."""
    df = df.copy()
    for col in LIVE_WAITS_KEY:
        df[col] = df[col].where(df[col].notna() & (df[col].astype(str).str.strip() != ""))
    df = df.dropna(subset=list(LIVE_WAITS_KEY))
    df["park_date"] = derive_park_date(df["observed_at"], park_tz)
    df = df.dropna(subset=["park_date"])
    df = df[df["park_date"] != "NaT"]  # unparseable observed_at
    df["wait_time_minutes"] = pd.to_numeric(df["wait_time_minutes"], errors="coerce")
    df = df[df["wait_time_minutes"] > 0]
    return df[list(LIVE_WAITS_COLUMNS)]


def insert_missing_live_waits(con, df: pd.DataFrame) -> int:
    """Insert rows of df whose (entity_code, observed_at, wait_time_type) is not in live_waits.

    Idempotent, and works whether or not live_waits has a PRIMARY KEY. Returns rows inserted.
    """
    if df.empty:
        return 0
    con.register("_live_waits_new", df[list(LIVE_WAITS_COLUMNS)])
    try:
        row = con.execute("""
            INSERT INTO live_waits (entity_code, observed_at, wait_time_type, wait_time_minutes, park_date)
            SELECT entity_code, observed_at, wait_time_type, wait_time_minutes, park_date
            FROM (
                SELECT DISTINCT ON (entity_code, observed_at, wait_time_type)
                       entity_code,
                       observed_at::TIMESTAMPTZ AS observed_at,
                       wait_time_type,
                       wait_time_minutes::INTEGER AS wait_time_minutes,
                       park_date::DATE AS park_date
                FROM _live_waits_new
            ) n
            WHERE NOT EXISTS (
                SELECT 1 FROM live_waits w
                WHERE w.entity_code = n.entity_code
                  AND w.observed_at = n.observed_at
                  AND w.wait_time_type = n.wait_time_type
            )
        """).fetchone()
        return int(row[0]) if row else 0
    finally:
        con.unregister("_live_waits_new")
