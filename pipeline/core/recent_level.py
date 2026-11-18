"""Recent-level feature (TPCR-ACCURACY-001): trailing mean of an entity's own actual_time.

Single shared implementation used by s07 (training), s08 (forecast), and the
backtest harness. All constants come from PipelineConfig (global, no per-park values).

Training row on park_date d: mean of actual_time (>0, non-NaN) over the entity's
training rows with park_date in [d - lag - window, d - lag).
Forecast issued on as_of: same mean over [as_of - window, as_of), fed only to target
days with 0 <= (park_date - as_of).days <= max_horizon (default 14); farther target days
get NaN (XGBoost's missing branch) so a stale level is never pushed onto far-horizon
forecasts. Missing (NaN) when fewer than min_rows qualifying rows are in the window.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

FEATURE = "recent_level_28d"

DEFAULT_WINDOW = 28
DEFAULT_LAG = 7
DEFAULT_MIN_ROWS = 50
DEFAULT_MAX_HORIZON = 14


def params(cfg=None) -> tuple[int, int, int]:
    """(window_days, lag_days, min_rows) from cfg, falling back to the defaults."""
    return (
        int(getattr(cfg, "recent_level_window_days", DEFAULT_WINDOW)),
        int(getattr(cfg, "recent_level_lag_days", DEFAULT_LAG)),
        int(getattr(cfg, "recent_level_min_rows", DEFAULT_MIN_ROWS)),
    )


def max_horizon(cfg=None) -> int:
    """Forecast horizon (days after the issue date) that still receives the level."""
    return int(getattr(cfg, "recent_level_max_horizon_days", DEFAULT_MAX_HORIZON))


def forecast_values(park_date, level: float, as_of: date,
                    max_horizon_days: int = DEFAULT_MAX_HORIZON) -> np.ndarray:
    """Per-row forecast feature: level where 0 <= (park_date - as_of).days <= max_horizon_days, else NaN.

    Computed per unique date (forecast grids repeat each date for every time slot).
    """
    pdates = pd.Series(park_date).reset_index(drop=True)
    out = np.full(len(pdates), np.nan, dtype=np.float64)
    if len(pdates) == 0 or not np.isfinite(level):
        return out
    codes, uniques = pd.factorize(pdates)
    h = (pd.to_datetime(pd.Series(uniques)).dt.normalize() - pd.Timestamp(as_of).normalize()).dt.days.to_numpy()
    in_range = (h >= 0) & (h <= int(max_horizon_days))
    keep = (codes >= 0) & in_range[np.clip(codes, 0, None)]
    out[keep] = float(level)
    return out


def enabled(cfg=None) -> bool:
    return bool(getattr(cfg, "recent_level_enabled", True))


def _day_numbers(park_date) -> np.ndarray:
    """park_date values -> integer days since 1970-01-01."""
    d = pd.to_datetime(pd.Series(park_date).reset_index(drop=True)).dt.normalize()
    return (d - pd.Timestamp("1970-01-01")).dt.days.to_numpy(dtype=np.int64)


def _qualifying(actual_time) -> np.ndarray:
    y = pd.to_numeric(pd.Series(actual_time).reset_index(drop=True), errors="coerce").to_numpy(dtype=np.float64)
    return np.isfinite(y) & (y > 0), y


def recent_level_for_rows(park_date, actual_time, window: int = DEFAULT_WINDOW,
                          lag: int = DEFAULT_LAG, min_rows: int = DEFAULT_MIN_ROWS) -> np.ndarray:
    """Training feature for each row (one entity's rows). Vectorized, order-preserving.

    Per-day sum/count over a daily calendar, then cumulative sums: the value for a
    row on day d uses only days in [d-lag-window, d-lag), so nothing at or after
    d-lag (including d itself) can influence it.
    """
    n_rows = len(park_date)
    out = np.full(n_rows, np.nan, dtype=np.float64)
    if n_rows == 0:
        return out
    day = _day_numbers(park_date)
    ok, y = _qualifying(actual_time)
    base = day.min()
    off = day - base
    n_days = int(off.max()) + 1
    sums = np.bincount(off[ok], weights=y[ok], minlength=n_days)
    cnts = np.bincount(off[ok], minlength=n_days).astype(np.float64)
    csum = np.concatenate([[0.0], np.cumsum(sums)])   # csum[k] = total of calendar days < k
    ccnt = np.concatenate([[0.0], np.cumsum(cnts)])
    hi = np.clip(off - lag, 0, n_days)
    lo = np.clip(off - lag - window, 0, n_days)
    s = csum[hi] - csum[lo]
    c = ccnt[hi] - ccnt[lo]
    good = c >= max(min_rows, 1)
    out[good] = s[good] / c[good]
    return out


def recent_level_at(park_date, actual_time, as_of: date, window: int = DEFAULT_WINDOW,
                    min_rows: int = DEFAULT_MIN_ROWS) -> float:
    """Forecast-time value: mean over rows with park_date in [as_of-window, as_of); NaN if too few."""
    if len(park_date) == 0:
        return float("nan")
    day = _day_numbers(park_date)
    ok, y = _qualifying(actual_time)
    t = (pd.Timestamp(as_of).normalize() - pd.Timestamp("1970-01-01")).days
    m = ok & (day >= t - window) & (day < t)
    if m.sum() < max(min_rows, 1):
        return float("nan")
    return float(y[m].mean())


def load_recent_levels(training_dir: Path, as_of: date, window: int = DEFAULT_WINDOW,
                       min_rows: int = DEFAULT_MIN_ROWS, combined_path: Path | None = None) -> dict[str, float]:
    """entity_code -> recent level at forecast issue date as_of, from the s07 training data.

    Reads only entity_code/park_date/actual_time and only rows in [as_of-window, as_of).
    Entities with too few rows are absent from the dict (callers treat absent as NaN).
    """
    import duckdb

    files = sorted(Path(training_dir).glob("*.parquet")) if Path(training_dir).is_dir() else []
    if not files and combined_path is not None and Path(combined_path).exists():
        files = [Path(combined_path)]
    if not files:
        return {}
    lo = (as_of - timedelta(days=window)).isoformat()
    hi = as_of.isoformat()
    paths = [str(f).replace("'", "''") for f in files]
    listing = ", ".join(f"'{p}'" for p in paths)
    con = duckdb.connect()
    try:
        rows = con.execute(f"""
            SELECT entity_code, AVG(CAST(actual_time AS DOUBLE)) AS lvl, COUNT(*) AS n
            FROM read_parquet([{listing}])
            WHERE CAST(park_date AS DATE) >= DATE '{lo}' AND CAST(park_date AS DATE) < DATE '{hi}'
              AND actual_time IS NOT NULL AND NOT isnan(CAST(actual_time AS DOUBLE))
              AND CAST(actual_time AS DOUBLE) > 0
            GROUP BY entity_code
        """).fetchall()
    finally:
        con.close()
    return {e: float(l) for e, l, n in rows if n >= max(min_rows, 1)}
