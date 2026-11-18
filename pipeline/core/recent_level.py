"""Recent-level feature (TPCR-ACCURACY-001), column name `recent_level_28d`.

Single shared implementation used by s07 (training), s08 (forecast), and the
backtest harness. All constants come from PipelineConfig (global, no per-park values).

Building block, the trailing mean M(lo, hi): mean of the entity's actual_time (> 0,
non-NaN) over its training rows with park_date in [lo, hi); NaN when fewer than
min_rows qualifying rows fall in the range.

cfg.recent_level_kind selects the definition (W = window, L = lag, O = yoy offset):
  "yoy_ratio" (default): seasonally adjusted level, the trailing mean divided by the
      mean over the same window 52 weeks earlier (O = 364 keeps the day-of-week mix).
      Training row on d: M(d-L-W, d-L) / M(d-L-W-O, d-L-O).
      Forecast on as_of: M(as_of-W, as_of) / M(as_of-W-O, as_of-O).
      NaN if either side is NaN (min_rows applies to each side) or the denominator <= 0.
  "mean": the raw trailing mean. Training row on d: M(d-L-W, d-L);
      forecast on as_of: M(as_of-W, as_of).

Forecast values are fed only to target days with 0 <= (park_date - as_of).days <=
max_horizon (default 14); farther target days get NaN (XGBoost's missing branch) so a
stale level is never pushed onto far-horizon forecasts.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

FEATURE = "recent_level_28d"

KIND_YOY = "yoy_ratio"
KIND_MEAN = "mean"
KINDS = (KIND_YOY, KIND_MEAN)

DEFAULT_KIND = KIND_YOY
DEFAULT_WINDOW = 28
DEFAULT_LAG = 7
DEFAULT_MIN_ROWS = 50
DEFAULT_MAX_HORIZON = 14
DEFAULT_YOY_OFFSET = 364


def params(cfg=None) -> tuple[int, int, int]:
    """(window_days, lag_days, min_rows) from cfg, falling back to the defaults."""
    return (
        int(getattr(cfg, "recent_level_window_days", DEFAULT_WINDOW)),
        int(getattr(cfg, "recent_level_lag_days", DEFAULT_LAG)),
        int(getattr(cfg, "recent_level_min_rows", DEFAULT_MIN_ROWS)),
    )


def kind(cfg=None) -> str:
    """cfg.recent_level_kind ("yoy_ratio" default, or "mean"); anything else is an error."""
    k = str(getattr(cfg, "recent_level_kind", DEFAULT_KIND))
    if k not in KINDS:
        raise ValueError(f"recent_level_kind must be one of {KINDS}, got {k!r}")
    return k


def yoy_offset(cfg=None) -> int:
    """Days between the numerator and denominator windows of the yoy ratio (364 = 52 weeks)."""
    return int(getattr(cfg, "recent_level_yoy_offset_days", DEFAULT_YOY_OFFSET))


def max_horizon(cfg=None) -> int:
    """Forecast horizon (days after the issue date) that still receives the level."""
    return int(getattr(cfg, "recent_level_max_horizon_days", DEFAULT_MAX_HORIZON))


def enabled(cfg=None) -> bool:
    return bool(getattr(cfg, "recent_level_enabled", True))


def spec(cfg=None) -> dict:
    """Definition of the feature under cfg, recorded in each model's metadata."""
    w, lag, m = params(cfg)
    return {
        "recent_level_kind": kind(cfg),
        "recent_level_window_days": w,
        "recent_level_lag_days": lag,
        "recent_level_min_rows": m,
        "recent_level_yoy_offset_days": yoy_offset(cfg),
    }


def model_spec_matches(meta: dict, cfg_spec: dict) -> bool:
    """True if a model's metadata describes the same forecast-time value as cfg_spec.

    Models trained before recent_level_kind existed carry no kind key; they were
    trained on the raw mean with the default window, so they are read as kind "mean".
    Lag and min_rows do not change the forecast-time definition and are not compared.
    """
    mk = str(meta.get("recent_level_kind", KIND_MEAN))
    if mk != cfg_spec["recent_level_kind"]:
        return False
    if int(meta.get("recent_level_window_days", DEFAULT_WINDOW)) != int(cfg_spec["recent_level_window_days"]):
        return False
    if mk == KIND_YOY and (int(meta.get("recent_level_yoy_offset_days", DEFAULT_YOY_OFFSET))
                           != int(cfg_spec["recent_level_yoy_offset_days"])):
        return False
    return True


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


def _day_numbers(park_date) -> np.ndarray:
    """park_date values -> integer days since 1970-01-01."""
    d = pd.to_datetime(pd.Series(park_date).reset_index(drop=True)).dt.normalize()
    return (d - pd.Timestamp("1970-01-01")).dt.days.to_numpy(dtype=np.int64)


def _qualifying(actual_time) -> tuple[np.ndarray, np.ndarray]:
    y = pd.to_numeric(pd.Series(actual_time).reset_index(drop=True), errors="coerce").to_numpy(
        dtype=np.float64, na_value=np.nan)
    return np.isfinite(y) & (y > 0), y


def _ratio(num, den) -> np.ndarray:
    """num / den; NaN where either side is NaN or den <= 0."""
    num = np.asarray(num, dtype=np.float64)
    den = np.asarray(den, dtype=np.float64)
    out = np.full(np.broadcast(num, den).shape, np.nan, dtype=np.float64)
    ok = np.isfinite(num) & np.isfinite(den) & (den > 0)
    out[ok] = num[ok] / den[ok]
    return out


def recent_level_for_rows(park_date, actual_time, window: int = DEFAULT_WINDOW,
                          lag: int = DEFAULT_LAG, min_rows: int = DEFAULT_MIN_ROWS) -> np.ndarray:
    """Kind "mean" training feature for each row (one entity's rows). Vectorized, order-preserving.

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


def yoy_ratio_for_rows(park_date, actual_time, window: int = DEFAULT_WINDOW, lag: int = DEFAULT_LAG,
                       min_rows: int = DEFAULT_MIN_ROWS, offset: int = DEFAULT_YOY_OFFSET) -> np.ndarray:
    """Kind "yoy_ratio" training feature: M(d-lag-window, d-lag) / M(d-lag-window-offset, d-lag-offset).

    Both windows end at or before d-lag, so nothing at or after d-lag can influence it.
    """
    num = recent_level_for_rows(park_date, actual_time, window, lag, min_rows)
    den = recent_level_for_rows(park_date, actual_time, window, lag + offset, min_rows)
    return _ratio(num, den)


def feature_for_rows(park_date, actual_time, cfg=None) -> np.ndarray:
    """Training feature for one entity's rows under cfg (kind, window, lag, min_rows, offset)."""
    w, lag, m = params(cfg)
    if kind(cfg) == KIND_MEAN:
        return recent_level_for_rows(park_date, actual_time, w, lag, m)
    return yoy_ratio_for_rows(park_date, actual_time, w, lag, m, yoy_offset(cfg))


def recent_level_at(park_date, actual_time, as_of: date, window: int = DEFAULT_WINDOW,
                    min_rows: int = DEFAULT_MIN_ROWS) -> float:
    """Kind "mean" forecast-time value: mean over rows with park_date in [as_of-window, as_of)."""
    if len(park_date) == 0:
        return float("nan")
    day = _day_numbers(park_date)
    ok, y = _qualifying(actual_time)
    t = (pd.Timestamp(as_of).normalize() - pd.Timestamp("1970-01-01")).days
    m = ok & (day >= t - window) & (day < t)
    if m.sum() < max(min_rows, 1):
        return float("nan")
    return float(y[m].mean())


def yoy_ratio_at(park_date, actual_time, as_of: date, window: int = DEFAULT_WINDOW,
                 min_rows: int = DEFAULT_MIN_ROWS, offset: int = DEFAULT_YOY_OFFSET) -> float:
    """Kind "yoy_ratio" forecast-time value: M(as_of-window, as_of) / M(as_of-window-offset, as_of-offset)."""
    num = recent_level_at(park_date, actual_time, as_of, window, min_rows)
    den = recent_level_at(park_date, actual_time, as_of - timedelta(days=offset), window, min_rows)
    return float(_ratio(num, den))


def feature_at(park_date, actual_time, as_of: date, cfg=None) -> float:
    """Forecast-time value under cfg for one entity's rows (only rows < as_of are read)."""
    w, _, m = params(cfg)
    if kind(cfg) == KIND_MEAN:
        return recent_level_at(park_date, actual_time, as_of, w, m)
    return yoy_ratio_at(park_date, actual_time, as_of, w, m, yoy_offset(cfg))


def load_recent_levels(training_dir: Path, as_of: date, window: int = DEFAULT_WINDOW,
                       min_rows: int = DEFAULT_MIN_ROWS, combined_path: Path | None = None,
                       kind: str = DEFAULT_KIND, offset: int = DEFAULT_YOY_OFFSET) -> dict[str, float]:
    """entity_code -> forecast-time feature value at issue date as_of, from the s07 training data.

    Reads only entity_code/park_date/actual_time, and only rows in [as_of-window, as_of)
    plus, for kind "yoy_ratio", [as_of-window-offset, as_of-offset). Entities whose value
    is NaN (too few rows on either side, denominator <= 0) are absent from the dict
    (callers treat absent as NaN).
    """
    import duckdb

    if kind not in KINDS:
        raise ValueError(f"recent_level_kind must be one of {KINDS}, got {kind!r}")
    files = sorted(Path(training_dir).glob("*.parquet")) if Path(training_dir).is_dir() else []
    if not files and combined_path is not None and Path(combined_path).exists():
        files = [Path(combined_path)]
    if not files:
        return {}
    num_rng = (as_of - timedelta(days=window), as_of)
    den_rng = (as_of - timedelta(days=window + offset), as_of - timedelta(days=offset))
    rngs = [num_rng] if kind == KIND_MEAN else [num_rng, den_rng]

    def cond(r):
        return f"(d >= DATE '{r[0].isoformat()}' AND d < DATE '{r[1].isoformat()}')"

    aggs = ", ".join(f"AVG(y) FILTER (WHERE {cond(r)}), COUNT(*) FILTER (WHERE {cond(r)})" for r in rngs)
    pushdown = " OR ".join(
        f"(CAST(park_date AS DATE) >= DATE '{r[0].isoformat()}' AND CAST(park_date AS DATE) < DATE '{r[1].isoformat()}')"
        for r in rngs)
    listing = ", ".join("'" + str(f).replace("'", "''") + "'" for f in files)
    con = duckdb.connect()
    try:
        rows = con.execute(f"""
            WITH q AS (
                SELECT entity_code, CAST(park_date AS DATE) AS d, CAST(actual_time AS DOUBLE) AS y
                FROM read_parquet([{listing}])
                WHERE {pushdown}
            )
            SELECT entity_code, {aggs}
            FROM q
            WHERE y IS NOT NULL AND NOT isnan(y) AND y > 0
            GROUP BY entity_code
        """).fetchall()
    finally:
        con.close()
    floor = max(min_rows, 1)
    out: dict[str, float] = {}
    for row in rows:
        e, num, n_num = row[0], row[1], row[2]
        if n_num < floor or num is None:
            continue
        if kind == KIND_MEAN:
            val = float(num)
        else:
            den, n_den = row[3], row[4]
            if n_den < floor or den is None:
                continue
            val = float(_ratio(float(num), float(den)))
        if np.isfinite(val):
            out[e] = val
    return out
